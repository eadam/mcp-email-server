import fnmatch
from datetime import datetime
from email import utils as email_utils
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from mcp_email_server.config import (
    AccountAttributes,
    EmailSettings,
    ProviderSettings,
    get_settings,
)
from mcp_email_server.emails.dispatcher import dispatch_handler
from mcp_email_server.emails.models import (
    AttachmentDownloadResponse,
    EmailContentBatchResponse,
    EmailMarkResponse,
    EmailMetadataPageResponse,
    MailboxInfo,
)
from mcp_email_server.log import logger

mcp = FastMCP("email")


def _normalize_addrs(raw: list[str]) -> list[str]:
    """Extract bare lower-cased email addresses from a list of recipient strings.

    Strips display-name forms like 'Foo <bad@evil.example>' down to the angle-addr
    component before allowlist comparison. Closes the display-name bypass where a
    well-formed allowlist entry could be evaded by wrapping the real address in
    a friendly display name.
    """
    normalized: list[str] = []
    for name_addr in email_utils.getaddresses(raw):
        addr = name_addr[1].strip().lower() if name_addr[1] else ""
        if addr:
            normalized.append(addr)
    return normalized


def _sender_allowed(sender: str, patterns: list[str]) -> bool:
    """Return True if sender matches any pattern in the allowlist, or if the list is empty.

    Handles 'Name <addr>' format via email.utils.parseaddr. Matching is case-insensitive.
    Patterns support fnmatch globs (e.g. *@example.com).

    Unparseable sender strings (malformed From headers) are treated as not allowed
    when an allowlist is configured — the safe default for the threat model.
    """
    if not patterns:
        return True
    _, addr = email_utils.parseaddr(sender)  # handles "Name <addr>" and bare addresses
    addr = (addr or sender).lower()  # fallback to raw string if parse fails
    return any(fnmatch.fnmatch(addr, pattern.lower()) for pattern in patterns)


@mcp.resource("email://{account_name}")
async def get_account(account_name: str) -> EmailSettings | ProviderSettings | None:
    settings = get_settings()
    return settings.get_account(account_name, masked=True)


@mcp.tool(description="List all configured email accounts with masked credentials.")
async def list_available_accounts() -> list[AccountAttributes]:
    settings = get_settings()
    return [account.masked() for account in settings.get_accounts()]


@mcp.tool(description="Add a new email account configuration to the settings.")
async def add_email_account(email: EmailSettings) -> str:
    settings = get_settings()
    settings.add_email(email)
    settings.store()
    return f"Successfully added email account '{email.account_name}'"


@mcp.tool(
    description=(
        "List the globally allowed sender email address patterns. "
        "Returns an empty list if no allowlist is configured, meaning emails from all senders are visible. "
        "Patterns may include wildcards (e.g. *@example.com). "
        "Call this tool to understand which senders the MCP client is permitted to read."
    )
)
async def list_allowed_senders() -> list[str]:
    return get_settings().allowed_senders


@mcp.tool(
    description="List email metadata (email_id, subject, sender, recipients, date) without body content. Returns email_id for use with get_emails_content."
)
async def list_emails_metadata(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    page: Annotated[
        int,
        Field(default=1, description="The page number to retrieve (starting from 1)."),
    ] = 1,
    page_size: Annotated[int, Field(default=10, description="The number of emails to retrieve per page.")] = 10,
    before: Annotated[
        datetime | None,
        Field(default=None, description="Retrieve emails before this datetime (UTC)."),
    ] = None,
    since: Annotated[
        datetime | None,
        Field(default=None, description="Retrieve emails since this datetime (UTC)."),
    ] = None,
    subject: Annotated[str | None, Field(default=None, description="Filter emails by subject.")] = None,
    from_address: Annotated[str | None, Field(default=None, description="Filter emails by sender address.")] = None,
    to_address: Annotated[
        str | None,
        Field(default=None, description="Filter emails by recipient address."),
    ] = None,
    order: Annotated[
        Literal["asc", "desc"],
        Field(default=None, description="Order emails by field. `asc` or `desc`."),
    ] = "desc",
    mailbox: Annotated[str, Field(default="INBOX", description="The mailbox to search.")] = "INBOX",
    seen: Annotated[
        bool | None,
        Field(default=None, description="Filter by read status: True=read, False=unread, None=all."),
    ] = None,
    flagged: Annotated[
        bool | None,
        Field(default=None, description="Filter by flagged/starred status: True=flagged, False=unflagged, None=all."),
    ] = None,
    answered: Annotated[
        bool | None,
        Field(default=None, description="Filter by replied status: True=replied, False=not replied, None=all."),
    ] = None,
) -> EmailMetadataPageResponse:
    # Read settings before the IMAP call to skip the round-trip when allowlist is empty
    settings = get_settings()
    allowed = settings.allowed_senders
    # homelab hardening: fail-closed if required mode is on and no sender allowlist set
    if getattr(settings, "allowlist_required", False) is True and not allowed:
        logger.warning("allowlist_block kind=sender_read reason=required-mode-empty-allowlist")
        raise ValueError(
            "Sender allowlist is required (MCP_EMAIL_SERVER_ALLOWLIST_REQUIRED=true) "
            "but allowed_senders is empty. Configure MCP_EMAIL_SERVER_ALLOWED_SENDERS."
        )
    handler = dispatch_handler(account_name)

    result = await handler.get_emails_metadata(
        page=page,
        page_size=page_size,
        before=before,
        since=since,
        subject=subject,
        from_address=from_address,
        to_address=to_address,
        order=order,
        mailbox=mailbox,
        seen=seen,
        flagged=flagged,
        answered=answered,
    )
    if allowed:
        kept: list = []
        for e in result.emails:
            if _sender_allowed(e.sender, allowed):
                kept.append(e)
            else:
                logger.warning(f"allowlist_block kind=sender_read addr={e.sender!r}")
        result.emails = kept
    # Note: result.total reflects the IMAP server-side count and is intentionally not adjusted.
    # See "Known limitation" in the README's "Filtering Incoming Email (Sender Allowlist)" section.
    return result


@mcp.tool(
    description="Get the full content (including body) of one or more emails by their email_id. Use list_emails_metadata first to get the email_id."
)
async def get_emails_content(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    email_ids: Annotated[
        list[str],
        Field(
            description="List of email_id to retrieve (obtained from list_emails_metadata). Can be a single email_id or multiple email_ids."
        ),
    ],
    mailbox: Annotated[str, Field(default="INBOX", description="The mailbox to retrieve emails from.")] = "INBOX",
) -> EmailContentBatchResponse:
    # Read settings before the IMAP call to skip the round-trip when allowlist is empty
    settings = get_settings()
    allowed = settings.allowed_senders
    # homelab hardening: fail-closed if required mode is on and no sender allowlist set
    if getattr(settings, "allowlist_required", False) is True and not allowed:
        logger.warning("allowlist_block kind=sender_read reason=required-mode-empty-allowlist")
        raise ValueError(
            "Sender allowlist is required (MCP_EMAIL_SERVER_ALLOWLIST_REQUIRED=true) "
            "but allowed_senders is empty. Configure MCP_EMAIL_SERVER_ALLOWED_SENDERS."
        )
    handler = dispatch_handler(account_name)
    result = await handler.get_emails_content(email_ids, mailbox)
    if allowed:
        kept: list = []
        for e in result.emails:
            if _sender_allowed(e.sender, allowed):
                kept.append(e)
            else:
                logger.warning(f"allowlist_block kind=sender_read addr={e.sender!r}")
        result.emails = kept
        result.retrieved_count = len(result.emails)
        # Blocked emails are silently dropped from the response — NOT added to failed_ids.
        # Adding them would reveal their existence to the AI.
        # requested_count is intentionally left at the caller-supplied value: the AI
        # already knows what IDs it requested, so leaving it unchanged leaks nothing.
    return result


@mcp.tool(
    description=(
        "List the globally allowed recipient email addresses for sending email. "
        "Returns an empty list if no allowlist is configured, meaning all recipients are permitted. "
        "Call this tool before any send_email call to verify the intended recipients are allowed."
    )
)
async def list_allowed_recipients() -> list[str]:
    return get_settings().allowed_recipients


@mcp.tool(
    description=(
        "Send an email using the specified account. Supports replying to emails with proper threading "
        "when in_reply_to is provided. IMPORTANT: If an allowlist is configured, ALL recipients "
        "(To, CC, BCC) must appear in it — call list_allowed_recipients first to check. "
        "Sends to addresses not on the allowlist will be rejected."
    ),
)
async def send_email(
    account_name: Annotated[str, Field(description="The name of the email account to send from.")],
    recipients: Annotated[list[str], Field(description="A list of recipient email addresses.")],
    subject: Annotated[str, Field(description="The subject of the email.")],
    body: Annotated[str, Field(description="The body of the email.")],
    cc: Annotated[
        list[str] | None,
        Field(default=None, description="A list of CC email addresses."),
    ] = None,
    bcc: Annotated[
        list[str] | None,
        Field(default=None, description="A list of BCC email addresses."),
    ] = None,
    html: Annotated[
        bool,
        Field(default=False, description="Whether to send the email as HTML (True) or plain text (False)."),
    ] = False,
    attachments: Annotated[
        list[str] | None,
        Field(
            default=None,
            description="A list of absolute file paths to attach to the email. Supports common file types (documents, images, archives, etc.).",
        ),
    ] = None,
    in_reply_to: Annotated[
        str | None,
        Field(
            default=None,
            description="Message-ID of the email being replied to. Enables proper threading in email clients.",
        ),
    ] = None,
    references: Annotated[
        str | None,
        Field(
            default=None,
            description="Space-separated Message-IDs for the thread chain. Usually includes in_reply_to plus ancestors.",
        ),
    ] = None,
) -> str:
    settings = get_settings()
    # homelab hardening: fail-closed if required mode is on and no recipient allowlist set.
    # `is True` (not truthy check) so MagicMock attributes in unit tests don't accidentally
    # trip this branch — only a real True opts in.
    if getattr(settings, "allowlist_required", False) is True and not settings.allowed_recipients:
        logger.warning(f"allowlist_block kind=recipient_send addrs={recipients!r} reason=required-mode-empty-allowlist")
        raise ValueError(
            "Recipient allowlist is required (MCP_EMAIL_SERVER_ALLOWLIST_REQUIRED=true) "
            "but allowed_recipients is empty. Configure MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS."
        )
    if settings.allowed_recipients:
        # homelab hardening: normalize 'Foo <bad@evil>' down to 'bad@evil' before compare
        all_addrs = _normalize_addrs(recipients + (cc or []) + (bcc or []))
        blocked = [r for r in all_addrs if r not in settings.allowed_recipients]
        if blocked:
            for addr in blocked:
                logger.warning(f"allowlist_block kind=recipient_send addr={addr!r}")
            raise ValueError(
                f"Recipient(s) not in allowlist: {', '.join(blocked)}. "
                f"Allowed: {', '.join(settings.allowed_recipients)}"
            )
    handler = dispatch_handler(account_name)
    await handler.send_email(
        recipients,
        subject,
        body,
        cc,
        bcc,
        html,
        attachments,
        in_reply_to,
        references,
    )
    recipient_str = ", ".join(recipients)
    attachment_info = f" with {len(attachments)} attachment(s)" if attachments else ""
    return f"Email sent successfully to {recipient_str}{attachment_info}"


@mcp.tool(
    description="Compose an email and save it to an IMAP folder (e.g., Drafts). "
    "Same parameters as send_email, but saves instead of sending. "
    "Default folder is Drafts with \\Draft and \\Seen flags.",
)
async def save_to_mailbox(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    recipients: Annotated[list[str], Field(description="A list of recipient email addresses.")],
    subject: Annotated[str, Field(description="The subject of the email.")],
    body: Annotated[str, Field(description="The body of the email.")],
    mailbox: Annotated[
        str,
        Field(
            default="Drafts",
            description="The IMAP folder to save to (e.g., 'Drafts', 'INBOX.Drafts', 'Templates').",
        ),
    ] = "Drafts",
    cc: Annotated[
        list[str] | None,
        Field(default=None, description="A list of CC email addresses."),
    ] = None,
    bcc: Annotated[
        list[str] | None,
        Field(default=None, description="A list of BCC email addresses."),
    ] = None,
    html: Annotated[
        bool,
        Field(default=False, description="Whether the email body is HTML (True) or plain text (False)."),
    ] = False,
    attachments: Annotated[
        list[str] | None,
        Field(
            default=None,
            description="A list of absolute file paths to attach to the email.",
        ),
    ] = None,
    in_reply_to: Annotated[
        str | None,
        Field(
            default=None,
            description="Message-ID of the email being replied to. Enables proper threading in email clients.",
        ),
    ] = None,
    references: Annotated[
        str | None,
        Field(
            default=None,
            description="Space-separated Message-IDs for the thread chain.",
        ),
    ] = None,
    flags: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=r"IMAP flags to set on the message. Defaults to ['\\Draft', '\\Seen']. Common flags: '\\Draft', '\\Seen', '\\Flagged'.",
        ),
    ] = None,
) -> str:
    handler = dispatch_handler(account_name)
    result = await handler.save_to_mailbox(
        recipients,
        subject,
        body,
        mailbox,
        cc,
        bcc,
        html,
        attachments,
        in_reply_to,
        references,
        flags,
    )
    # result format: "<message-id>|uid:<imap-uid>"
    parts = result.split("|uid:")
    message_id = parts[0]
    email_id = parts[1] if len(parts) > 1 else "unknown"
    return f"Email saved to '{mailbox}' successfully. Message-Id: {message_id}, email_id: {email_id}"


@mcp.tool(
    description="Delete one or more emails by their email_id. Use list_emails_metadata first to get the email_id."
)
async def delete_emails(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    email_ids: Annotated[
        list[str],
        Field(description="List of email_id to delete (obtained from list_emails_metadata)."),
    ],
    mailbox: Annotated[str, Field(default="INBOX", description="The mailbox to delete emails from.")] = "INBOX",
) -> str:
    handler = dispatch_handler(account_name)
    deleted_ids, failed_ids = await handler.delete_emails(email_ids, mailbox)

    result = f"Successfully deleted {len(deleted_ids)} email(s)"
    if failed_ids:
        result += f", failed to delete {len(failed_ids)} email(s): {', '.join(failed_ids)}"
    return result


@mcp.tool(
    description="Move one or more emails between IMAP folders by their email_id. Use list_emails_metadata first to get the email_id and list_mailboxes to discover available folders."
)
async def move_emails(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    email_ids: Annotated[
        list[str],
        Field(description="List of email_id to move (obtained from list_emails_metadata)."),
    ],
    destination_mailbox: Annotated[str, Field(description="The destination mailbox/folder to move emails to.")],
    source_mailbox: Annotated[
        str, Field(default="INBOX", description="The source mailbox containing the emails.")
    ] = "INBOX",
) -> str:
    handler = dispatch_handler(account_name)
    moved_ids, failed_ids = await handler.move_emails(email_ids, source_mailbox, destination_mailbox)

    result = f"Successfully moved {len(moved_ids)} email(s) to {destination_mailbox}"
    if failed_ids:
        result += f", failed to move {len(failed_ids)} email(s): {', '.join(failed_ids)}"
    return result


@mcp.tool(
    description="List available mailboxes/folders for an email account. Returns folder names, hierarchy delimiters, and flags. Useful for discovering folder names before moving emails."
)
async def list_mailboxes(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    pattern: Annotated[
        str,
        Field(default="*", description="IMAP LIST pattern. Use '*' for all folders, 'INBOX.*' for INBOX children."),
    ] = "*",
    reference: Annotated[
        str,
        Field(default="", description="IMAP LIST reference name (namespace prefix). Usually empty."),
    ] = "",
) -> list[MailboxInfo]:
    handler = dispatch_handler(account_name)
    return await handler.list_mailboxes(pattern, reference)


@mcp.tool(description="Mark one or more emails as read or unread. Use list_emails_metadata first to get the email_id.")
async def mark_emails(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    email_ids: Annotated[
        list[str],
        Field(description="List of email_id to mark (obtained from list_emails_metadata)."),
    ],
    mark_as: Annotated[
        Literal["read", "unread"],
        Field(description="Mark emails as 'read' or 'unread'."),
    ],
    mailbox: Annotated[
        str,
        Field(
            default="INBOX",
            description="IMAP folder path. Standard: INBOX, Sent, Drafts, Trash. Provider-specific: Gmail uses '[Gmail]/...' prefix; ProtonMail Bridge uses 'Folders/<name>' and 'Labels/<name>'.",
        ),
    ] = "INBOX",
) -> EmailMarkResponse:
    handler = dispatch_handler(account_name)
    return await handler.mark_emails(email_ids, mark_as, mailbox)


@mcp.tool(
    description="Download an email attachment and save it to the specified path. This feature must be explicitly enabled in settings (enable_attachment_download=true) due to security considerations.",
)
async def download_attachment(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    email_id: Annotated[
        str, Field(description="The email ID (obtained from list_emails_metadata or get_emails_content).")
    ],
    attachment_name: Annotated[
        str, Field(description="The name of the attachment to download (as shown in the attachments list).")
    ],
    save_path: Annotated[str, Field(description="The absolute path where the attachment should be saved.")],
    mailbox: Annotated[str, Field(description="The mailbox to search in (default: INBOX).")] = "INBOX",
) -> AttachmentDownloadResponse:
    settings = get_settings()
    if not settings.enable_attachment_download:
        msg = (
            "Attachment download is disabled. Set 'enable_attachment_download=true' in settings to enable this feature."
        )
        raise PermissionError(msg)

    handler = dispatch_handler(account_name)
    return await handler.download_attachment(email_id, attachment_name, save_path, mailbox)
