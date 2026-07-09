import asyncio
import base64
import binascii
import email.utils
import mimetypes
import re
import ssl
import time
import unicodedata
from datetime import datetime, timezone
from email import encoders
from email.header import Header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from typing import Any, NamedTuple

import aioimaplib
import aiosmtplib
from bs4 import BeautifulSoup

from mcp_email_server.config import EmailServer, EmailSettings, get_settings, sender_allowed
from mcp_email_server.emails import EmailHandler
from mcp_email_server.emails.models import (
    AttachmentDownloadResponse,
    EmailBodyResponse,
    EmailContentBatchResponse,
    EmailMetadata,
    EmailMetadataPageResponse,
    InlineAttachment,
    MailboxInfo,
)
from mcp_email_server.log import logger

# Maximum body length before truncation (characters)
MAX_BODY_LENGTH = 20000

# Common Archive folder names, used as a fallback when no RFC 6154 \Archive flag is found.
_ARCHIVE_FOLDER_CANDIDATES = ("Archive", "Archives", "[Gmail]/All Mail")


# RFC 3501 system flags (except \Recent which is read-only) + custom keyword atoms
_VALID_IMAP_FLAG = re.compile(r"^\\[A-Za-z]+$|^[A-Za-z][A-Za-z0-9_-]*$")


def _validate_flags(flags: list[str]) -> str:
    """Validate and format IMAP flags into a parenthesised string.

    Accepts system flags (e.g. ``\\Draft``, ``\\Seen``) and custom keyword
    atoms.  Raises ``ValueError`` on anything that could inject IMAP protocol
    characters.
    """
    for flag in flags:
        if not _VALID_IMAP_FLAG.match(flag):
            msg = f"Invalid IMAP flag: {flag!r}"
            raise ValueError(msg)
    return "(" + " ".join(flags) + ")"


def encode_mailbox_name(mailbox: str) -> str:
    """Encode an IMAP mailbox name using RFC 3501 Modified UTF-7."""
    result: list[str] = []
    buffer: list[str] = []

    def flush_buffer() -> None:
        if not buffer:
            return
        text = "".join(buffer)
        encoded = base64.b64encode(text.encode("utf-16-be")).decode("ascii")
        result.append("&" + encoded.rstrip("=").replace("/", ",") + "-")
        buffer.clear()

    for char in mailbox:
        codepoint = ord(char)
        if char == "&":
            flush_buffer()
            result.append("&-")
        elif 0x20 <= codepoint <= 0x7E:
            flush_buffer()
            result.append(char)
        else:
            buffer.append(char)

    flush_buffer()
    return "".join(result)


def decode_mailbox_name(mailbox: str) -> str:
    """Decode an IMAP mailbox name from RFC 3501 Modified UTF-7."""
    result: list[str] = []
    index = 0

    while index < len(mailbox):
        char = mailbox[index]
        if char != "&":
            result.append(char)
            index += 1
            continue

        end = mailbox.find("-", index + 1)
        if end == -1:
            result.append(mailbox[index:])
            break
        if end == index + 1:
            result.append("&")
            index = end + 1
            continue

        encoded = mailbox[index + 1 : end].replace(",", "/")
        padding = "=" * (-len(encoded) % 4)
        try:
            decoded = base64.b64decode(encoded + padding, validate=True).decode("utf-16-be")
        except (binascii.Error, UnicodeDecodeError):
            result.append(mailbox[index : end + 1])
        else:
            result.append(decoded)
        index = end + 1

    return "".join(result)


def _skip_imap_whitespace(value: str, start: int) -> int:
    """Return the next non-whitespace index in an IMAP response line."""
    index = start
    while index < len(value) and value[index].isspace():
        index += 1
    return index


def _read_quoted_imap_token(value: str, start: int) -> tuple[str, int]:
    """Read a quoted IMAP token."""
    index = start + 1
    token: list[str] = []
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            token.append(value[index + 1])
            index += 2
            continue
        if char == '"':
            return "".join(token), index + 1
        token.append(char)
        index += 1
    return "".join(token), index


def _read_parenthesized_imap_token(value: str, start: int) -> tuple[str, int]:
    """Read a parenthesized IMAP token."""
    depth = 1
    index = start + 1
    token: list[str] = []
    while index < len(value):
        char = value[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return "".join(token), index + 1
        token.append(char)
        index += 1
    return "".join(token), index


def _read_atom_imap_token(value: str, start: int) -> tuple[str, int]:
    """Read an atom IMAP token."""
    index = start
    while index < len(value) and not value[index].isspace():
        index += 1
    return value[start:index], index


def _read_imap_list_token(value: str, start: int) -> tuple[str | None, int]:
    """Read one token from an IMAP LIST response line."""
    index = _skip_imap_whitespace(value, start)
    if index >= len(value):
        return None, index
    if value[index] == '"':
        return _read_quoted_imap_token(value, index)
    if value[index] == "(":
        return _read_parenthesized_imap_token(value, index)
    return _read_atom_imap_token(value, index)


def _parse_list_response(item: bytes | str) -> MailboxInfo | None:
    """Parse one IMAP LIST response into a MailboxInfo object."""
    item_str = item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
    item_str = item_str.strip()
    if not item_str:
        return None

    flags: list[str] = []
    position = 0
    if item_str.startswith("("):
        flags_token, position = _read_imap_list_token(item_str, position)
        flags = [flag.strip() for flag in (flags_token or "").split() if flag.strip()]

    delimiter_token, position = _read_imap_list_token(item_str, position)
    mailbox_token, _position = _read_imap_list_token(item_str, position)
    if delimiter_token is None or mailbox_token is None:
        return None

    delimiter = "" if delimiter_token.upper() == "NIL" else delimiter_token
    return MailboxInfo(name=decode_mailbox_name(mailbox_token), delimiter=delimiter, flags=flags)


def _quote_mailbox(mailbox: str) -> str:
    """Quote mailbox name for IMAP compatibility.

    Some IMAP servers (notably Proton Mail Bridge) require mailbox names
    to be quoted. This is valid per RFC 3501 and works with all IMAP servers.

    Per RFC 3501 Section 9 (Formal Syntax), quoted strings must escape
    backslashes and double-quote characters with a preceding backslash.
    Mailbox names with non-ASCII characters are encoded using Modified UTF-7
    as required by RFC 3501 Section 5.1.3.

    See: https://github.com/ai-zerolab/mcp-email-server/issues/87
    See: https://github.com/ai-zerolab/mcp-email-server/issues/172
    See: https://www.rfc-editor.org/rfc/rfc3501#section-9
    """
    encoded = encode_mailbox_name(mailbox)
    # Per RFC 3501, literal double-quote characters in a quoted string must
    # be escaped with a backslash. Backslashes themselves must also be escaped.
    escaped = encoded.replace("\\", "\\\\").replace('"', r"\"")
    return f'"{escaped}"'


def _uid_sort_key(uid: bytes | str) -> int:
    """Return a numeric sort key for IMAP UIDs."""
    value = uid.decode() if isinstance(uid, bytes) else uid
    return int(value)


def _imap_status(response: Any) -> str:
    """Return the normalized status from an aioimaplib response."""
    if hasattr(response, "result"):
        return str(response.result).upper()
    if isinstance(response, tuple) and response:
        return str(response[0]).upper()
    return str(response).upper()


def _format_imap_response_detail(response: Any) -> str:
    """Return a compact, readable IMAP response detail string."""
    status = _imap_status(response)
    lines = getattr(response, "lines", None)
    if lines is None and isinstance(response, tuple) and len(response) > 1:
        lines = response[1]

    detail_parts = []
    for line in lines or []:
        if isinstance(line, bytes):
            detail_parts.append(line.decode("utf-8", errors="replace"))
        else:
            detail_parts.append(str(line))

    detail = " ".join(part for part in detail_parts if part).strip()
    return f"{status} {detail}".strip()


def _raise_for_imap_error(response: Any, operation: str) -> None:
    """Raise when an IMAP command returns a non-OK status."""
    if _imap_status(response) != "OK":
        detail = _format_imap_response_detail(response)
        msg = f"{operation} failed" + (f": {detail}" if detail else "")
        raise RuntimeError(msg)


def _raise_for_imap_command_failure(response: Any, operation: str) -> None:
    """Raise only when an IMAP command reports an explicit failure status."""
    if _imap_status(response) in {"NO", "BAD"}:
        detail = _format_imap_response_detail(response)
        msg = f"{operation} failed" + (f": {detail}" if detail else "")
        raise RuntimeError(msg)


def _html_to_text(html: str) -> str:
    """Convert an HTML email body to readable plain text."""
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style"]):
        element.decompose()

    for link in soup.find_all("a"):
        href = str(link.get("href") or "").strip()
        normalized_href_scheme = re.sub(r"[\x00-\x20]+", "", href).lower()
        if not href or href.startswith("#") or normalized_href_scheme.startswith(("mailto:", "javascript:")):
            continue

        link_text = link.get_text(" ", strip=True)
        replacement = href if not link_text or link_text == href else f"{link_text} ({href})"
        link.replace_with(replacement)

    soup.smooth()
    text = soup.get_text(separator="\n")
    text = re.sub(r"\n\s*\n", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


async def _send_imap_id(imap: aioimaplib.IMAP4 | aioimaplib.IMAP4_SSL) -> None:
    """Send IMAP ID command with fallback for strict servers like 163.com.

    aioimaplib's id() method sends ID command with spaces between parentheses
    and content (e.g., 'ID ( "name" "value" )'), which some strict IMAP servers
    like 163.com reject with 'BAD Parse command error'.

    This function first tries the standard id() method, and if it fails,
    falls back to sending a raw command with correct format.

    See: https://github.com/ai-zerolab/mcp-email-server/issues/85
    """
    try:
        response = await imap.id(name="mcp-email-server", version="1.0.0")
        if response.result != "OK":
            # Fallback for strict servers (e.g., 163.com)
            # Send raw command with correct parenthesis format
            new_tag = imap.protocol.new_tag()
            if hasattr(new_tag, "__await__"):
                new_tag = await new_tag
            await imap.protocol.execute(
                aioimaplib.Command(
                    "ID",
                    new_tag,
                    '("name" "mcp-email-server" "version" "1.0.0")',
                )
            )
    except Exception as e:
        logger.warning(f"IMAP ID command failed: {e!s}")


async def _imap_login(
    imap: aioimaplib.IMAP4 | aioimaplib.IMAP4_SSL,
    user_name: str,
    password: str,
) -> None:
    """Authenticate to IMAP and fail loudly when the server rejects credentials.

    aioimaplib's ``login()`` returns a Response with a ``.result`` of "OK",
    "NO", or "BAD". A "NO" response (e.g. wrong credentials, account locked,
    or a transient rate-limit cool-down on servers like Proton Mail Bridge)
    does NOT raise — and an unchecked caller will happily proceed to issue
    SELECT/FETCH on a NONAUTH connection, producing the misleading error
    ``command SELECT illegal in state NONAUTH``. Worse, each tool call then
    opens a fresh TCP connection and re-attempts ``LOGIN``, which amplifies
    rate-limits on servers that count failed-login attempts and locks the
    account out for tens of minutes.

    Raise immediately on a non-OK result so callers (and end users) see the
    real error and back off, and so a one-off auth failure does not cascade
    into a multi-minute lock-out.
    """
    response = await imap.login(user_name, password)
    if response.result == "OK":
        return
    detail = " ".join(
        line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
        for line in (response.lines or [])
    ).strip()
    raise ConnectionError(
        f"IMAP login failed for {user_name!r}: {response.result}" + (f" ({detail})" if detail else "")
    )


def _create_ssl_context(verify_ssl: bool) -> ssl.SSLContext | None:
    """Create SSL context for SMTP/IMAP connections.

    Returns None for default verification, or permissive context
    for self-signed certificates when verify_ssl=False.
    """
    if verify_ssl:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _create_starttls_ssl_context(verify_ssl: bool) -> ssl.SSLContext:
    """Create a concrete SSL context for asyncio STARTTLS upgrades."""
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    if not verify_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _imap_capabilities(imap: aioimaplib.IMAP4) -> set[str]:
    """Return normalized capabilities from an aioimaplib protocol."""
    return {
        capability.decode("utf-8", errors="replace").upper()
        if isinstance(capability, bytes)
        else str(capability).upper()
        for capability in getattr(imap.protocol, "capabilities", ())
    }


async def _imap_starttls(imap: aioimaplib.IMAP4, ssl_context: ssl.SSLContext, host: str) -> None:
    """Upgrade an IMAP connection to TLS via STARTTLS."""
    capabilities = _imap_capabilities(imap)
    if "STARTTLS" not in capabilities:
        await imap.protocol.capability()
        capabilities = _imap_capabilities(imap)
    if "STARTTLS" not in capabilities:
        raise OSError("IMAP server does not advertise STARTTLS capability")

    response = await imap.protocol.execute(
        aioimaplib.Command("STARTTLS", imap.protocol.new_tag(), loop=imap.protocol.loop)
    )
    status = _imap_status(response)
    if status != "OK":
        raise OSError(f"STARTTLS command failed: {status}")

    loop = asyncio.get_running_loop()
    tls_transport = await loop.start_tls(
        imap.protocol.transport,
        imap.protocol,
        ssl_context,
        server_hostname=host,
    )
    imap.protocol.transport = tls_transport
    await imap.protocol.capability()


# Backwards-compatible alias
_create_smtp_ssl_context = _create_ssl_context


class _ResolvedAttachment(NamedTuple):
    """An attachment resolved to MIME-assembly-ready bytes.

    Inputs to the send pipeline get normalized into this shape before being
    added to a MIMEMultipart. Keeping ``maintype`` / ``subtype`` separate avoids
    the historical bug where ``MIMEApplication(_subtype=mime_type.split("/")[1])``
    converted every type into ``application/<subtype>`` — e.g. ``image/png``
    silently shipped as ``application/png``.
    """

    filename: str
    data: bytes
    maintype: str
    subtype: str


class _FetchedAttachment(NamedTuple):
    """An attachment extracted from a fetched IMAP message.

    The ``filename`` field is the value pulled from the parsed MIME part
    (already decoded by ``policy=default``), not the caller-supplied
    attachment name — so inline responses can show the same filename a mail
    client would.
    """

    data: bytes
    mime_type: str
    filename: str


# RFC 2045 token chars (no parameters, no control chars). Conservative.
_MIME_TYPE_TOKEN = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
# Filename-disallowed characters under our conservative LLM-transport policy.
# Strict because filenames are a display string only — users with unusual names
# can have the LLM rename. NUL, CR, LF, raw quote/semicolon, plus C0/C1 controls.
_FILENAME_DISALLOWED = re.compile(r"[\x00-\x1f\x7f-\x9f\";]")


def _sanitize_attachment_filename(raw: str) -> str:
    """Reduce a caller-supplied filename to a safe display string.

    Strips POSIX and Windows path separators (so ``../../etc/passwd`` becomes
    ``passwd`` and ``..\\..\\secret.txt`` becomes ``secret.txt``), rejects empty
    results, rejects ``"."`` and ``".."`` after stripping, rejects control
    characters and the conservative ``"``/``;`` set, and applies NFC
    normalization so attackers can't sneak past the comparison with mixed
    composed/decomposed forms.

    Raises:
        ValueError: If the result would be empty, ``"."``, ``".."``, or
            contains any disallowed character.
    """
    if not raw:
        raise ValueError("Attachment filename is empty")
    # Strip directory components from both separator conventions.
    stripped = raw.replace("\\", "/").rsplit("/", 1)[-1]
    if not stripped or stripped in (".", ".."):
        raise ValueError(f"Attachment filename {raw!r} resolves to an empty / dotted basename")
    normalized = unicodedata.normalize("NFC", stripped)
    if _FILENAME_DISALLOWED.search(normalized):
        raise ValueError(f"Attachment filename {raw!r} contains disallowed characters")
    return normalized


def _validate_mime_type(raw: str) -> tuple[str, str]:
    """Validate a caller-supplied MIME type and return (maintype, subtype).

    Strict RFC 2045 token only — no parameters, no whitespace, no control
    characters. Anything looser is rejected so we don't ship a corrupt
    Content-Type header.
    """
    if not _MIME_TYPE_TOKEN.match(raw):
        raise ValueError(f"Invalid MIME type {raw!r}: expected 'type/subtype' RFC 2045 tokens")
    maintype, _, subtype = raw.partition("/")
    return maintype, subtype


def _decoded_base64_length(encoded: str) -> int:
    """Estimate the decoded byte length of a base64 string without decoding it."""
    if not encoded:
        return 0
    padding = encoded[-2:].count("=")
    return (len(encoded) * 3) // 4 - padding


def _resolve_inline_attachment(item: InlineAttachment, max_per_item: int) -> _ResolvedAttachment:
    """Validate an InlineAttachment and return a _ResolvedAttachment.

    Steps, in order:

    1. Reject any whitespace in ``content_base64`` (standard base64 only —
       documented contract). This also makes step 2 meaningful since
       ``base64.b64decode(..., validate=True)`` rejects whitespace anyway.
    2. Estimate the decoded length from the encoded length and reject before
       allocating if it would exceed ``max_per_item``. The size check happens
       pre-decode so an oversized payload never gets a second buffer.
    3. Strict ``base64.b64decode(..., validate=True)``.
    4. Sanitize the filename (see :py:func:`_sanitize_attachment_filename`).
    5. Validate / auto-detect the MIME type.

    Errors are bare ``ValueError`` with stable, content-free messages — they
    never include the base64 string or any decoded bytes.
    """
    encoded = item.content_base64
    if any(c.isspace() for c in encoded):
        raise ValueError("Inline attachment base64 contains whitespace; standard base64 only (no newlines or spaces)")

    estimated = _decoded_base64_length(encoded)
    if estimated > max_per_item:
        raise ValueError(
            f"Inline attachment too large: {estimated} bytes (estimated from base64 length) "
            f"exceeds per-item cap {max_per_item}. Raise MCP_EMAIL_SERVER_MAX_INLINE_ATTACHMENT_BYTES_PER_ITEM "
            f"or split the file."
        )

    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as e:
        # binascii.Error subclasses ValueError, but spell both out for clarity.
        # Message stays content-free — never echo the payload.
        raise ValueError(f"Inline attachment base64 decode failed: {type(e).__name__}") from None

    if len(data) > max_per_item:
        # Belt-and-braces: the encoded estimate is an upper bound, but in
        # case padding rules ever drift, recheck after decode.
        raise ValueError(f"Inline attachment too large: {len(data)} bytes exceeds per-item cap {max_per_item}.")

    filename = _sanitize_attachment_filename(item.filename)

    if item.mime_type is not None:
        maintype, subtype = _validate_mime_type(item.mime_type)
    else:
        guessed, _ = mimetypes.guess_type(filename)
        if guessed is None or "/" not in guessed:
            guessed = "application/octet-stream"
        maintype, _, subtype = guessed.partition("/")

    return _ResolvedAttachment(filename=filename, data=data, maintype=maintype, subtype=subtype)


class EmailClient:
    def __init__(self, email_server: EmailServer, sender: str | None = None):
        self.email_server = email_server
        self.sender = sender or email_server.user_name

        self.imap_class = aioimaplib.IMAP4_SSL if self.email_server.use_ssl else aioimaplib.IMAP4

        self.smtp_use_tls = self.email_server.use_ssl
        self.smtp_start_tls = self.email_server.start_ssl
        self.smtp_verify_ssl = self.email_server.verify_ssl

    def _imap_connect(self) -> aioimaplib.IMAP4_SSL | aioimaplib.IMAP4:
        """Create a new IMAP connection with the configured SSL context."""
        if self.email_server.use_ssl:
            imap_ssl_context = _create_ssl_context(self.email_server.verify_ssl)
            return self.imap_class(self.email_server.host, self.email_server.port, ssl_context=imap_ssl_context)
        return self.imap_class(self.email_server.host, self.email_server.port)

    async def _connect_imap(self) -> aioimaplib.IMAP4_SSL | aioimaplib.IMAP4:
        """Create, greet, and optionally STARTTLS-upgrade an IMAP connection."""
        imap = self._imap_connect()
        return await self._prepare_imap_connection(imap, self.email_server)

    @staticmethod
    async def _prepare_imap_connection(
        imap: aioimaplib.IMAP4_SSL | aioimaplib.IMAP4,
        server: EmailServer,
    ) -> aioimaplib.IMAP4_SSL | aioimaplib.IMAP4:
        """Wait for greeting and optionally STARTTLS-upgrade an IMAP connection."""
        await imap._client_task
        await imap.wait_hello_from_server()

        if server.start_ssl:
            ssl_context = _create_starttls_ssl_context(server.verify_ssl)
            await _imap_starttls(imap, ssl_context, server.host)

        return imap

    @staticmethod
    async def _connect_imap_server(server: EmailServer) -> aioimaplib.IMAP4_SSL | aioimaplib.IMAP4:
        """Create, greet, and optionally STARTTLS-upgrade an IMAP connection."""
        if server.use_ssl:
            imap_ssl_context = _create_ssl_context(server.verify_ssl)
            imap = aioimaplib.IMAP4_SSL(server.host, server.port, ssl_context=imap_ssl_context)
        else:
            imap = aioimaplib.IMAP4(server.host, server.port)

        return await EmailClient._prepare_imap_connection(imap, server)

    def _get_smtp_ssl_context(self) -> ssl.SSLContext | None:
        """Get SSL context for SMTP connections based on verify_ssl setting."""
        return _create_ssl_context(self.smtp_verify_ssl)

    @staticmethod
    def _parse_recipients(email_message) -> list[str]:
        """Extract recipient addresses from To and Cc headers."""
        recipients = []
        to_header = email_message.get("To", "")
        if to_header:
            recipients = [addr.strip() for addr in to_header.split(",")]
        cc_header = email_message.get("Cc", "")
        if cc_header:
            recipients.extend([addr.strip() for addr in cc_header.split(",")])
        return recipients

    @staticmethod
    def _parse_date(date_str: str) -> datetime:
        """Parse email date string to datetime, with fallback to current time."""
        try:
            date_tuple = email.utils.parsedate_tz(date_str)
            if date_tuple:
                return datetime.fromtimestamp(email.utils.mktime_tz(date_tuple), tz=timezone.utc)
            return datetime.now(timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)

    @staticmethod
    def _normalize_attachment_name(name: str) -> str:
        """Normalize attachment filenames for robust MIME round-trip matching."""
        return unicodedata.normalize("NFC", name)

    @staticmethod
    def _is_attachment_part(part) -> bool:
        """Determine whether a MIME part should be treated as an attachment.

        A strict check on ``Content-Disposition: attachment`` misses a common case:
        many clients (notably Apple Mail on iOS/macOS) send images, PDFs and other
        files with ``Content-Disposition: inline`` (or no disposition header at all)
        but with a filename parameter on the part. Those parts are real, user-facing
        attachments — the user uploaded a file and expects it to show up — even
        though they're inlined into the body via Content-ID references.

        Treat a part as an attachment when:
          - the disposition explicitly says ``attachment``, OR
          - the part carries a filename (works for ``inline`` or no disposition).

        Multipart container parts and bodyless text parts have no filename and an
        empty disposition, so they are correctly excluded.
        """
        content_disposition = str(part.get("Content-Disposition", "")).lower()
        if "attachment" in content_disposition:
            return True
        filename = part.get_filename()
        # Be defensive: only trust real string filenames. (Unconfigured MagicMock
        # instances in older tests return truthy MagicMock objects from
        # ``get_filename`` and would otherwise misclassify text parts.)
        return isinstance(filename, str) and bool(filename)

    def _parse_email_data(  # noqa: C901
        self,
        raw_email: bytes,
        email_id: str | None = None,
        body_offset: int = 0,
        max_body_length: int = MAX_BODY_LENGTH,
    ) -> dict[str, Any]:
        """Parse raw email data into a structured dictionary."""
        parser = BytesParser(policy=default)
        email_message = parser.parsebytes(raw_email)

        # Extract email parts
        subject = email_message.get("Subject", "")
        sender = email_message.get("From", "")
        date_str = email_message.get("Date", "")

        # Extract Message-ID for reply threading
        message_id = email_message.get("Message-ID")

        # Extract recipients and parse date
        to_addresses = self._parse_recipients(email_message)
        date = self._parse_date(date_str)

        # Get body content
        body = ""
        html_body = ""  # Fallback if no text/plain
        attachments = []

        if email_message.is_multipart():
            for part in email_message.walk():
                content_type = part.get_content_type()

                # Handle attachments — including inline-disposition parts with a
                # filename (Apple Mail commonly sends photos this way).
                if self._is_attachment_part(part):
                    filename = part.get_filename()
                    if filename:
                        attachments.append(filename)
                # Handle text parts - prefer text/plain
                elif content_type == "text/plain":
                    body_part = part.get_payload(decode=True)
                    if body_part:
                        charset = part.get_content_charset("utf-8")
                        try:
                            body += body_part.decode(charset)
                        except UnicodeDecodeError:
                            body += body_part.decode("utf-8", errors="replace")
                # Collect HTML as fallback
                elif content_type == "text/html" and not body:
                    html_part = part.get_payload(decode=True)
                    if html_part:
                        charset = part.get_content_charset("utf-8")
                        try:
                            html_body += html_part.decode(charset)
                        except UnicodeDecodeError:
                            html_body += html_part.decode("utf-8", errors="replace")

            # Fall back to HTML if no plain text found
            if not body and html_body:
                body = _html_to_text(html_body)
        else:
            # Handle single-part emails
            content_type = email_message.get_content_type()
            payload = email_message.get_payload(decode=True)
            if payload:
                charset = email_message.get_content_charset("utf-8")
                try:
                    text = payload.decode(charset)
                except UnicodeDecodeError:
                    text = payload.decode("utf-8", errors="replace")

                body = _html_to_text(text) if content_type == "text/html" else text
        if body_offset < 0:
            raise ValueError("body_offset must be >= 0")
        if max_body_length < 1:
            raise ValueError("max_body_length must be >= 1")

        # Return at most ``max_body_length`` characters starting at ``body_offset``. When more of
        # the body remains past the window, append the ``...[TRUNCATED]`` marker so callers can page
        # through a long email by re-requesting with ``body_offset += max_body_length``.
        if body:
            window = body[body_offset : body_offset + max_body_length]
            if body_offset + max_body_length < len(body):
                window += "...[TRUNCATED]"
            body = window
        return {
            "email_id": email_id or "",
            "message_id": message_id,
            "subject": subject,
            "from": sender,
            "to": to_addresses,
            "body": body,
            "date": date,
            "attachments": attachments,
        }

    @staticmethod
    def _sanitize_imap_value(value: str) -> str:
        """Sanitize a string value for IMAP search criteria.

        For multi-word values, strips embedded double quotes (invalid per RFC 3501
        Section 4.3) and wraps in double quotes. Single-word values pass through unchanged.
        """
        if " " not in value:
            return value
        sanitized = value.replace('"', "")
        return f'"{sanitized}"'

    @staticmethod
    def _build_search_criteria(
        before: datetime | None = None,
        since: datetime | None = None,
        subject: str | None = None,
        body: str | None = None,
        text: str | None = None,
        from_address: str | None = None,
        to_address: str | None = None,
        seen: bool | None = None,
        flagged: bool | None = None,
        answered: bool | None = None,
        has_attachment: bool | None = None,
    ) -> list[str]:
        search_criteria = []
        if before:
            search_criteria.extend(["BEFORE", before.strftime("%d-%b-%Y").upper()])
        if since:
            search_criteria.extend(["SINCE", since.strftime("%d-%b-%Y").upper()])

        # Substring-match fields (IMAP keyword, value)
        text_criteria = [
            ("SUBJECT", subject),
            ("BODY", body),
            ("TEXT", text),
            ("FROM", from_address),
            ("TO", to_address),
        ]
        for keyword, value in text_criteria:
            if value:
                search_criteria.extend([keyword, EmailClient._sanitize_imap_value(value)])

        # Attachment heuristic: most attachments are carried in multipart/mixed.
        # May miss some types (e.g. inline images) or yield false positives.
        if has_attachment is True:
            search_criteria.extend(["HEADER", "Content-Type", "multipart/mixed"])
        elif has_attachment is False:
            search_criteria.extend(["NOT", "HEADER", "Content-Type", "multipart/mixed"])

        # Flag-based criteria using mapping to reduce complexity
        flag_criteria = [
            (seen, {True: "SEEN", False: "UNSEEN"}),
            (flagged, {True: "FLAGGED", False: "UNFLAGGED"}),
            (answered, {True: "ANSWERED", False: "UNANSWERED"}),
        ]
        for flag_value, criteria_map in flag_criteria:
            if flag_value in criteria_map:
                search_criteria.append(criteria_map[flag_value])

        return search_criteria or ["ALL"]

    def _parse_headers(self, email_id: str, raw_headers: bytes) -> dict[str, Any] | None:
        """Parse raw email headers into metadata dictionary.

        Note: this parses only header data (BODY.PEEK[HEADER]) so it cannot
        populate the attachments list — that requires fetching BODYSTRUCTURE
        or the full body. The attachments list is intentionally returned
        empty here; ``_parse_email_data`` populates it from the full body.
        """
        try:
            parser = BytesParser(policy=default)
            email_message = parser.parsebytes(raw_headers)

            subject = email_message.get("Subject", "")
            sender = email_message.get("From", "")
            date_str = email_message.get("Date", "")
            # Expose Message-ID for reply threading and de-duplication on the client.
            message_id = email_message.get("Message-ID")

            to_addresses = self._parse_recipients(email_message)
            date = self._parse_date(date_str)

            return {
                "email_id": email_id,
                "message_id": message_id,
                "subject": subject,
                "from": sender,
                "to": to_addresses,
                "date": date,
                "attachments": [],
            }
        except Exception as e:
            logger.error(f"Error parsing email headers: {e!s}")
            return None

    async def _fetch_dates_chunk(
        self,
        imap: aioimaplib.IMAP4_SSL | aioimaplib.IMAP4,
        chunk: list[bytes],
        chunk_num: int,
        total_chunks: int,
        timeout: float = 30.0,
    ) -> dict[str, datetime]:
        """Fetch INTERNALDATE for a single chunk of UIDs."""
        uid_list = ",".join(uid.decode() for uid in chunk)
        chunk_start = time.perf_counter()
        _, data = await asyncio.wait_for(
            imap.uid("fetch", uid_list, "(INTERNALDATE)"),
            timeout=timeout,
        )
        chunk_elapsed = time.perf_counter() - chunk_start

        chunk_dates: dict[str, datetime] = {}
        for item in data:
            if not isinstance(item, bytes) or b"INTERNALDATE" not in item:
                continue
            uid_match = re.search(rb"UID (\d+)", item)
            date_match = re.search(rb'INTERNALDATE "([^"]+)"', item)
            if uid_match and date_match:
                uid = uid_match.group(1).decode()
                date_str = date_match.group(1).decode().strip()
                chunk_dates[uid] = datetime.strptime(date_str, "%d-%b-%Y %H:%M:%S %z")

        if total_chunks > 1:
            logger.info(f"Fetched dates chunk {chunk_num}/{total_chunks}: {len(chunk)} UIDs in {chunk_elapsed:.2f}s")

        return chunk_dates

    async def _batch_fetch_dates(
        self,
        imap: aioimaplib.IMAP4_SSL | aioimaplib.IMAP4,
        email_ids: list[bytes],
        chunk_size: int = 500,
    ) -> dict[str, datetime]:
        """Batch fetch INTERNALDATE for all UIDs in sequential chunks.

        Uses a conservative chunk_size (default 500) to avoid hitting
        Python's recursion limit in aioimaplib's recursive response parser
        (see: aioimaplib _handle_responses). IMAP connections are sequential
        by protocol, so chunks must be fetched serially — not in parallel.
        """
        if not email_ids:
            return {}

        # Split into chunks
        chunks = [email_ids[i : i + chunk_size] for i in range(0, len(email_ids), chunk_size)]
        total_chunks = len(chunks)

        # Fetch chunks sequentially (IMAP protocol is sequential on a single connection)
        uid_dates: dict[str, datetime] = {}
        for chunk_num, chunk in enumerate(chunks, 1):
            chunk_dates = await self._fetch_dates_chunk(imap, chunk, chunk_num, total_chunks)
            uid_dates.update(chunk_dates)

        return uid_dates

    async def _batch_fetch_headers(
        self,
        imap: aioimaplib.IMAP4_SSL | aioimaplib.IMAP4,
        email_ids: list[bytes] | list[str],
    ) -> dict[str, dict[str, Any]]:
        """Batch fetch headers for a list of UIDs."""
        if not email_ids:
            return {}

        # Normalize to list of strings
        str_ids = [uid.decode() if isinstance(uid, bytes) else uid for uid in email_ids]
        uid_list = ",".join(str_ids)
        _, data = await imap.uid("fetch", uid_list, "BODY.PEEK[HEADER]")

        results: dict[str, dict[str, Any]] = {}
        for i, item in enumerate(data):
            if not isinstance(item, bytes) or b"BODY[HEADER]" not in item:
                continue
            # First try to find UID in the same line (standard format)
            uid_match = re.search(rb"UID (\d+)", item)
            if uid_match and i + 1 < len(data) and isinstance(data[i + 1], bytearray):
                uid = uid_match.group(1).decode()
                raw_headers = bytes(data[i + 1])
                metadata = self._parse_headers(uid, raw_headers)
                if metadata:
                    results[uid] = metadata
            # Proton Bridge format: UID comes AFTER header data in a separate item
            # Format: [i]=b'N FETCH (BODY[HEADER] {size}', [i+1]=bytearray(headers), [i+2]=b' UID xxx)'
            elif i + 2 < len(data) and isinstance(data[i + 1], bytearray):
                uid_after_match = re.search(rb"UID (\d+)", data[i + 2]) if isinstance(data[i + 2], bytes) else None
                if uid_after_match:
                    uid = uid_after_match.group(1).decode()
                    raw_headers = bytes(data[i + 1])
                    metadata = self._parse_headers(uid, raw_headers)
                    if metadata:
                        results[uid] = metadata

        return results

    async def _batch_fetch_senders(
        self,
        imap: aioimaplib.IMAP4_SSL | aioimaplib.IMAP4,
        email_ids: list[bytes] | list[str],
        chunk_size: int = 500,
    ) -> dict[str, str]:
        """Batch fetch the From header for all UIDs (chunked, sequential), for allowlist filtering.

        Returns {uid: raw From header}. Fetches only HEADER.FIELDS (FROM) to stay light and reuses
        _parse_headers (which tolerates a From-only header block).
        """
        if not email_ids:
            return {}

        chunks = [email_ids[i : i + chunk_size] for i in range(0, len(email_ids), chunk_size)]
        senders: dict[str, str] = {}
        for chunk in chunks:
            str_ids = [uid.decode() if isinstance(uid, bytes) else uid for uid in chunk]
            uid_list = ",".join(str_ids)
            fetch_response = await imap.uid("fetch", uid_list, "BODY.PEEK[HEADER.FIELDS (FROM)]")
            _raise_for_imap_command_failure(fetch_response, f"FETCH From headers for UIDs {uid_list}")
            _, data = fetch_response
            for i, item in enumerate(data):
                if not isinstance(item, bytes) or b"BODY[HEADER" not in item:
                    continue
                uid_match = re.search(rb"UID (\d+)", item)
                if uid_match and i + 1 < len(data) and isinstance(data[i + 1], bytearray):
                    meta = self._parse_headers(uid_match.group(1).decode(), bytes(data[i + 1]))
                    if meta:
                        senders[meta["email_id"]] = meta["from"]
                elif i + 2 < len(data) and isinstance(data[i + 1], bytearray):
                    uid_after = re.search(rb"UID (\d+)", data[i + 2]) if isinstance(data[i + 2], bytes) else None
                    if uid_after:
                        meta = self._parse_headers(uid_after.group(1).decode(), bytes(data[i + 1]))
                        if meta:
                            senders[meta["email_id"]] = meta["from"]
        return senders

    async def _enforce_sender_allowlist(
        self,
        imap: aioimaplib.IMAP4_SSL | aioimaplib.IMAP4,
        email_id: str,
        allowed_senders: list[str] | None,
    ) -> None:
        """Raise ValueError (identical to not-found) when sender is not on the allowlist.

        No-op when ``allowed_senders`` is empty or None (backwards-compatible).

        Only the attachment-download path uses this helper, so the block is
        audit-logged here as a structured ``allowlist_block`` warning — the
        tool layer intentionally cannot distinguish a blocked UID from a
        missing one (no existence oracle), so this is the one place the block
        is definitively known. The sender address is deliberately not logged
        alongside the block to keep the log line content-free; the UID is
        repr'd so CR/LF in attacker-controlled input can't forge log lines.
        """
        if allowed_senders:
            uid_senders = await self._batch_fetch_senders(imap, [email_id])
            if not sender_allowed(uid_senders.get(email_id, ""), allowed_senders):
                logger.warning(f"allowlist_block kind=attachment_download email_id={email_id!r}")
                msg = f"Failed to fetch email with UID {email_id}"
                logger.error(msg)
                raise ValueError(msg)

    async def _blocked_uids(
        self,
        imap: aioimaplib.IMAP4_SSL | aioimaplib.IMAP4,
        email_ids: list[str],
        allowed_senders: list[str] | None,
    ) -> set[str]:
        """UIDs whose From is not on the allowlist. Empty set when no allowlist (no IMAP work)."""
        if not allowed_senders:
            return set()
        uid_senders = await self._batch_fetch_senders(imap, email_ids)
        return {uid for uid in email_ids if not sender_allowed(uid_senders.get(uid, ""), allowed_senders)}

    async def get_emails_metadata(
        self,
        page: int = 1,
        page_size: int = 10,
        before: datetime | None = None,
        since: datetime | None = None,
        subject: str | None = None,
        from_address: str | None = None,
        to_address: str | None = None,
        order: str = "desc",
        mailbox: str = "INBOX",
        seen: bool | None = None,
        flagged: bool | None = None,
        answered: bool | None = None,
        body: str | None = None,
        text: str | None = None,
        has_attachment: bool | None = None,
        allowed_senders: list[str] | None = None,
    ) -> tuple[int, list[dict[str, Any]]]:
        imap = await self._connect_imap()
        try:
            # Login and select mailbox
            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            select_response = await imap.select(_quote_mailbox(mailbox))
            _raise_for_imap_error(select_response, f"SELECT mailbox {mailbox}")

            search_criteria = self._build_search_criteria(
                before,
                since,
                subject,
                body=body,
                text=text,
                from_address=from_address,
                to_address=to_address,
                seen=seen,
                flagged=flagged,
                answered=answered,
                has_attachment=has_attachment,
            )
            logger.info(f"Get metadata: Search criteria: {search_criteria}")

            # Search for messages - use UID SEARCH for better compatibility.
            # charset handling: aioimaplib defaults to "CHARSET utf-8", which
            # Microsoft Exchange rejects with `NO [BADCHARSET (US-ASCII)] The
            # specified charset is not supported.`, breaking all search/list
            # operations — so ASCII-only user searches omit the CHARSET token.
            # However, RFC 3501 requires a charset declaration for non-ASCII
            # search values: without it, servers such as Coremail interpret the
            # raw UTF-8 bytes as US-ASCII and silently return zero matches. Base
            # the decision only on user-supplied text fields, not on generated
            # criteria such as locale-dependent date strings.
            search_text_values = (subject, body, text, from_address, to_address)
            charset = "utf-8" if any(value and not value.isascii() for value in search_text_values) else None
            _, messages = await imap.uid_search(*search_criteria, charset=charset)

            # Handle empty or None responses
            if not messages or not messages[0]:
                logger.warning("No messages returned from search")
                return 0, []

            email_ids = messages[0].split()
            logger.info(f"Found {len(email_ids)} email IDs")

            # Sender allowlist: filter candidates BEFORE sorting/pagination so total + pages stay honest.
            if allowed_senders:
                uid_senders = await self._batch_fetch_senders(imap, email_ids)
                email_ids = [
                    uid for uid in email_ids if sender_allowed(uid_senders.get(uid.decode(), ""), allowed_senders)
                ]
                logger.info(f"Sender allowlist active: {len(email_ids)} of {len(uid_senders)} match")
                if not email_ids:
                    return 0, []

            # Phase 1: Batch fetch INTERNALDATE for sorting (sequential chunks)
            fetch_dates_start = time.perf_counter()
            uid_dates = await self._batch_fetch_dates(imap, email_ids)
            fetch_dates_elapsed = time.perf_counter() - fetch_dates_start

            missing_date_count = len(email_ids) - len(uid_dates)
            if missing_date_count:
                logger.warning(
                    f"Missing INTERNALDATE for {missing_date_count}/{len(email_ids)} searched UIDs; "
                    "falling back to UID order for those messages"
                )

            # Keep UID SEARCH results as the source of truth. Use INTERNALDATE where
            # available, and fall back to UID ordering for provider-specific
            # INTERNALDATE response formats that cannot be parsed.
            if order == "desc":
                sorted_uids = sorted(
                    (uid.decode() for uid in email_ids),
                    key=lambda uid: (
                        uid_dates.get(uid) is not None,
                        uid_dates.get(uid) or datetime.min.replace(tzinfo=timezone.utc),
                        _uid_sort_key(uid),
                    ),
                    reverse=True,
                )
            else:
                sorted_uids = sorted(
                    (uid.decode() for uid in email_ids),
                    key=lambda uid: (
                        uid_dates.get(uid) is None,
                        uid_dates.get(uid) or datetime.max.replace(tzinfo=timezone.utc),
                        _uid_sort_key(uid),
                    ),
                )

            # Paginate
            start = (page - 1) * page_size
            page_uids = sorted_uids[start : start + page_size]

            if not page_uids:
                logger.info(f"Phase 1 (dates): {len(uid_dates)} UIDs in {fetch_dates_elapsed:.2f}s, page {page} empty")
                return len(email_ids), []

            # Phase 2: Batch fetch headers for requested page only
            fetch_headers_start = time.perf_counter()
            metadata_by_uid = await self._batch_fetch_headers(imap, page_uids)
            fetch_headers_elapsed = time.perf_counter() - fetch_headers_start

            logger.info(
                f"Fetched page {page}: {fetch_dates_elapsed:.2f}s dates ({len(uid_dates)} UIDs), "
                f"{fetch_headers_elapsed:.2f}s headers ({len(page_uids)} UIDs)"
            )

            # Collect page results in sorted order
            page_emails = [metadata_by_uid[uid] for uid in page_uids if uid in metadata_by_uid]
            return len(email_ids), page_emails
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

    def _check_email_content(self, data: list) -> bool:
        """Check if the fetched data contains actual email content."""
        for item in data:
            if isinstance(item, bytes) and b"FETCH (" in item and b"RFC822" not in item and b"BODY" not in item:
                # This is just metadata, not actual content
                continue
            elif isinstance(item, bytes | bytearray) and len(item) > 100:
                # This looks like email content
                return True
        return False

    def _extract_raw_email(self, data: list) -> bytes | None:
        """Extract raw email bytes from IMAP response data."""
        # The email content is typically at index 1 as a bytearray
        if len(data) > 1 and isinstance(data[1], bytearray):
            return bytes(data[1])

        # Search through all items for email content
        for item in data:
            if isinstance(item, bytes | bytearray) and len(item) > 100:
                # Skip IMAP protocol responses
                if isinstance(item, bytes) and b"FETCH" in item:
                    continue
                # This is likely the email content
                return bytes(item) if isinstance(item, bytearray) else item
        return None

    async def _fetch_email_with_formats(self, imap, email_id: str) -> list | None:
        """Try non-mutating fetch formats to get email data."""
        fetch_formats = ["BODY.PEEK[]", "(BODY.PEEK[])"]

        for fetch_format in fetch_formats:
            try:
                response = await imap.uid("fetch", email_id, fetch_format)
                _raise_for_imap_error(response, f"FETCH email {email_id} with {fetch_format}")
                _, data = response

                if data and len(data) > 0 and self._check_email_content(data):
                    return data

            except Exception as e:
                logger.debug(f"Fetch format {fetch_format} failed: {e}")

        return None

    async def get_email_body_by_id(
        self,
        email_id: str,
        mailbox: str = "INBOX",
        mark_as_read: bool = False,
        allowed_senders: list[str] | None = None,
        body_offset: int = 0,
        max_body_length: int = MAX_BODY_LENGTH,
    ) -> dict[str, Any] | None:
        imap = await self._connect_imap()
        try:
            # Login and select mailbox
            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            select_response = await imap.select(_quote_mailbox(mailbox))
            _raise_for_imap_error(select_response, f"SELECT mailbox {mailbox}")

            # Sender allowlist: check the From header BEFORE reading the body, so a blocked
            # message is never fetched/parsed, never marked read, and is indistinguishable from
            # a missing/inaccessible one (caller sees None either way).
            if allowed_senders:
                uid_senders = await self._batch_fetch_senders(imap, [email_id])
                if not sender_allowed(uid_senders.get(email_id, ""), allowed_senders):
                    return None

            # Fetch the specific email by UID without implicitly marking it as read
            data = await self._fetch_email_with_formats(imap, email_id)
            if not data:
                logger.error(f"Failed to fetch UID {email_id} with any format")
                return None

            # Extract raw email data
            raw_email = self._extract_raw_email(data)
            if not raw_email:
                logger.error(f"Could not find email data in response for email ID: {email_id}")
                return None

            # Parse the email
            try:
                email_data = self._parse_email_data(
                    raw_email, email_id, body_offset=body_offset, max_body_length=max_body_length
                )
            except Exception as e:
                logger.error(f"Error parsing email: {e!s}")
                return None

            if mark_as_read:
                try:
                    store_response = await imap.uid("store", email_id, "+FLAGS", r"(\Seen)")
                    _raise_for_imap_error(store_response, f"STORE \\Seen for email {email_id}")
                except Exception as e:
                    logger.warning(f"Failed to mark email {email_id} as read: {e}")

            return email_data

        finally:
            # Ensure we logout properly
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

    @classmethod
    def _find_attachment_part(cls, email_message, attachment_name: str) -> _FetchedAttachment | None:
        """Walk a parsed message and return the named attachment, or None.

        Matches the parts listed by ``_parse_email_data`` — including
        inline-disposition parts with a filename (e.g. iOS Mail photos) — using
        NFC-normalized filename comparison.
        """
        if not email_message.is_multipart():
            return None
        normalized_attachment_name = cls._normalize_attachment_name(attachment_name)
        for part in email_message.walk():
            if not cls._is_attachment_part(part):
                continue
            filename = part.get_filename()
            if not isinstance(filename, str):
                continue
            if cls._normalize_attachment_name(filename) != normalized_attachment_name:
                continue
            data = part.get_payload(decode=True)
            if data is None:
                return None
            return _FetchedAttachment(data=data, mime_type=part.get_content_type(), filename=filename)
        return None

    async def _fetch_attachment_bytes(
        self,
        email_id: str,
        attachment_name: str,
        mailbox: str = "INBOX",
        *,
        allowed_senders: list[str] | None = None,
    ) -> _FetchedAttachment:
        """Fetch + parse the email, enforce the sender allowlist, extract the attachment.

        Shared core of :py:meth:`download_attachment` (disk write) and any
        inline-response mode. One IMAP connection total; the sender check runs
        before the body is fetched.

        Returns the raw bytes, MIME type, and the filename as the parsed MIME
        part reports it (which may differ from ``attachment_name`` when encoded
        headers normalize differently).

        Args:
            email_id: The UID of the email containing the attachment.
            attachment_name: The filename of the attachment to download.
            mailbox: The mailbox to search in (default: "INBOX").
            allowed_senders: Optional sender allowlist; when set, a non-allowed
                sender's message is treated as not found and its body is never
                fetched.

        Raises:
            ValueError: If the sender is not on the allowlist (identical to a
                missing UID — no existence oracle), or the email/attachment
                cannot be located.
        """
        imap = await self._connect_imap()
        try:
            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            select_response = await imap.select(_quote_mailbox(mailbox))
            _raise_for_imap_error(select_response, f"SELECT mailbox {mailbox}")

            # Read-path allowlist: check the From header before fetching the body, so a
            # blocked sender's message is never read. Blocked fails identically to a missing
            # UID (same ValueError below), so it does not reveal whether the message exists.
            await self._enforce_sender_allowlist(imap, email_id, allowed_senders)

            data = await self._fetch_email_with_formats(imap, email_id)
            if not data:
                msg = f"Failed to fetch email with UID {email_id}"
                logger.error(msg)
                raise ValueError(msg)

            raw_email = self._extract_raw_email(data)
            if not raw_email:
                msg = f"Could not find email data for email ID: {email_id}"
                logger.error(msg)
                raise ValueError(msg)

            parser = BytesParser(policy=default)
            email_message = parser.parsebytes(raw_email)

            found = self._find_attachment_part(email_message, attachment_name)
            if found is None:
                msg = f"Attachment '{attachment_name}' not found in email {email_id}"
                logger.error(msg)
                raise ValueError(msg)

            return found

        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

    async def download_attachment(
        self,
        email_id: str,
        attachment_name: str,
        save_path: str,
        mailbox: str = "INBOX",
        allowed_senders: list[str] | None = None,
    ) -> dict[str, Any]:
        """Download a specific attachment from an email and save it to disk.

        Thin wrapper over :py:meth:`_fetch_attachment_bytes` that writes the
        returned bytes to ``save_path``. The sender allowlist check and the
        IMAP fetch / parse live in the underlying helper.

        Args:
            email_id: The UID of the email containing the attachment.
            attachment_name: The filename of the attachment to download.
            save_path: The local path where the attachment will be saved.
            mailbox: The mailbox to search in (default: "INBOX").
            allowed_senders: Optional sender allowlist; when set, a non-allowed sender's
                message is treated as not found and its body is never fetched.

        Returns:
            A dictionary with download result information.
        """
        fetched = await self._fetch_attachment_bytes(
            email_id,
            attachment_name,
            mailbox,
            allowed_senders=allowed_senders,
        )

        save_file = Path(save_path)
        save_file.parent.mkdir(parents=True, exist_ok=True)
        save_file.write_bytes(fetched.data)

        logger.info(f"Attachment '{attachment_name}' saved to {save_path}")

        return {
            "email_id": email_id,
            "attachment_name": attachment_name,
            "mime_type": fetched.mime_type,
            "size": len(fetched.data),
            "saved_path": str(save_file.resolve()),
        }

    async def fetch_attachment_inline(
        self,
        email_id: str,
        attachment_name: str,
        mailbox: str = "INBOX",
        *,
        allowed_senders: list[str] | None = None,
        max_bytes: int,
    ) -> dict[str, Any]:
        """Return an attachment as base64-encoded bytes in the MCP response.

        Symmetric companion to :py:meth:`download_attachment` for remote MCP
        clients that cannot read the server's filesystem. Same IMAP fetch and
        sender allowlist enforcement as the disk-write path — the difference is
        purely where the bytes go. The returned ``attachment_name`` is the
        filename the parsed MIME part reports (what a mail client would show).

        Raises:
            ValueError: If the attachment is larger than ``max_bytes`` (stable
                message includes size + cap + remediation hint), or any error
                from :py:meth:`_fetch_attachment_bytes` (allowlist block, IMAP
                fetch failure, attachment not found).
        """
        fetched = await self._fetch_attachment_bytes(
            email_id,
            attachment_name,
            mailbox,
            allowed_senders=allowed_senders,
        )
        if len(fetched.data) > max_bytes:
            raise ValueError(
                f"Attachment too large for inline mode: {len(fetched.data)} bytes exceeds cap {max_bytes}. "
                f"Use inline=False with a writable save_path, or raise MCP_EMAIL_SERVER_MAX_INLINE_DOWNLOAD_BYTES."
            )

        encoded = base64.b64encode(fetched.data).decode("ascii")
        logger.info(f"Attachment '{fetched.filename}' returned inline ({len(fetched.data)} bytes)")
        return {
            "email_id": email_id,
            "attachment_name": fetched.filename,
            "mime_type": fetched.mime_type,
            "size": len(fetched.data),
            "content_base64": encoded,
        }

    def _validate_attachment(self, file_path: str) -> Path:
        """Validate attachment file path."""
        path = Path(file_path)
        if not path.exists():
            msg = f"Attachment file not found: {file_path}"
            logger.error(msg)
            raise FileNotFoundError(msg)

        if not path.is_file():
            msg = f"Attachment path is not a file: {file_path}"
            logger.error(msg)
            raise ValueError(msg)

        return path

    def _resolve_path_attachment(self, file_path: str) -> _ResolvedAttachment:
        """Read a file path into a _ResolvedAttachment with correct maintype/subtype.

        Replaces the old ``MIMEApplication(_subtype=mime_type.split("/")[1])``
        flow, which mangled non-application MIME types (e.g. ``image/png``
        became ``application/png``).
        """
        path = self._validate_attachment(file_path)
        with open(path, "rb") as f:
            data = f.read()

        mime_type, _ = mimetypes.guess_type(str(path))
        if mime_type is None or "/" not in mime_type:
            mime_type = "application/octet-stream"
        maintype, _, subtype = mime_type.partition("/")
        return _ResolvedAttachment(filename=path.name, data=data, maintype=maintype, subtype=subtype)

    def _build_attachment_part(self, resolved: _ResolvedAttachment) -> MIMEBase:
        """Build a MIMEBase part from a resolved attachment, preserving maintype/subtype.

        Uses MIMEBase + encoders.encode_base64 rather than MIMEApplication so
        that an ``image/png`` attachment actually has Content-Type ``image/png``.
        """
        part = MIMEBase(resolved.maintype, resolved.subtype)
        part.set_payload(resolved.data)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=resolved.filename)
        logger.info(f"Attached file: {resolved.filename} ({resolved.maintype}/{resolved.subtype})")
        return part

    def _resolve_attachments(
        self,
        path_attachments: list[str] | None,
        inline_attachments: list[InlineAttachment] | None = None,
    ) -> list[_ResolvedAttachment]:
        """Normalize the two attachment-input shapes into a single resolved list.

        Path-based attachments are validated and read first; inline attachments
        are then decoded, capped, and appended. The per-item and aggregate caps
        for the inline list are read from the live settings (so env overrides
        take effect without restart). Errors are raised with stable messages
        that never include the base64 payload.
        """
        resolved: list[_ResolvedAttachment] = []
        if path_attachments:
            for file_path in path_attachments:
                try:
                    resolved.append(self._resolve_path_attachment(file_path))
                except Exception as e:
                    logger.error(f"Failed to attach file {file_path}: {e}")
                    raise
        if not inline_attachments:
            return resolved

        settings = get_settings()
        per_item = settings.max_inline_attachment_bytes_per_item
        aggregate = settings.max_inline_attachment_bytes

        running_total = 0
        for idx, item in enumerate(inline_attachments):
            # Pre-decode aggregate check — refuse to allocate the next decode
            # buffer if the encoded-length estimate would already overshoot the
            # remaining aggregate budget. Without this, a 14 MiB attachment
            # against a 5 MiB remaining budget would still get fully decoded
            # (per-item cap satisfied) before the post-decode aggregate check
            # rejected it. _decoded_base64_length is exact for clean base64
            # (whitespace is rejected by validate=True in the resolver), so
            # the estimate matches the post-decode size for any accepted input.
            estimated = _decoded_base64_length(item.content_base64)
            if running_total + estimated > aggregate:
                raise ValueError(
                    f"inline_attachments[{idx}]: aggregate size would exceed cap {aggregate} bytes "
                    f"({running_total} already accepted, this attachment is ~{estimated} bytes by "
                    f"encoded-length estimate). Raise MCP_EMAIL_SERVER_MAX_INLINE_ATTACHMENT_BYTES "
                    f"or remove attachments."
                )
            try:
                rendered = _resolve_inline_attachment(item, per_item)
            except ValueError as e:
                raise ValueError(f"inline_attachments[{idx}]: {e}") from None
            running_total += len(rendered.data)
            resolved.append(rendered)
        return resolved

    def _create_message_with_attachments(
        self, body: str, html: bool, attachments: list[_ResolvedAttachment]
    ) -> MIMEMultipart:
        """Create multipart message with already-resolved attachments."""
        msg = MIMEMultipart()
        content_type = "html" if html else "plain"
        text_part = MIMEText(body, content_type, "utf-8")
        msg.attach(text_part)

        for attachment in attachments:
            msg.attach(self._build_attachment_part(attachment))

        return msg

    def compose_message(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        html: bool = False,
        attachments: list[str] | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
        include_bcc_header: bool = False,
        reply_to: str | None = None,
        inline_attachments: list[InlineAttachment] | None = None,
    ) -> MIMEText | MIMEMultipart:
        """Compose an email message without sending it.

        Builds MIME structure, sets headers (Subject, From, To, Cc, Date,
        Message-Id, threading headers). Synchronous — no I/O.

        Two attachment inputs that are concatenated server-side:

        - ``attachments`` is a list of absolute paths on the server's
          filesystem, resolved via :py:meth:`_resolve_path_attachment`.
        - ``inline_attachments`` is a list of
          :py:class:`~mcp_email_server.emails.models.InlineAttachment` objects
          carrying base64-encoded bytes, resolved via
          :py:func:`_resolve_inline_attachment` with the per-item and
          aggregate caps from settings.

        When ``include_bcc_header`` is True (used for local IMAP storage such
        as Drafts or Sent copies), the Bcc header is included so mail clients
        can display the BCC recipients.  When False (default, used for SMTP
        sending), the Bcc header is omitted — BCC recipients are delivered
        via the SMTP envelope only.
        """
        resolved = self._resolve_attachments(attachments, inline_attachments)
        if resolved:
            msg = self._create_message_with_attachments(body, html, resolved)
        else:
            content_type = "html" if html else "plain"
            msg = MIMEText(body, content_type, "utf-8")

        # Handle subject with special characters
        if any(ord(c) > 127 for c in subject):
            msg["Subject"] = Header(subject, "utf-8")
        else:
            msg["Subject"] = subject

        sender_name, sender_address = email.utils.parseaddr(self.sender)

        # Encode only the display name. RFC 2047 encoded-words must not cover the
        # addr-spec, otherwise strict clients may show the raw encoded blob.
        if sender_address:
            msg["From"] = email.utils.formataddr((str(Header(sender_name, "utf-8")), sender_address))
        elif any(ord(c) > 127 for c in self.sender):
            msg["From"] = Header(self.sender, "utf-8")
        else:
            msg["From"] = self.sender

        msg["To"] = ", ".join(recipients)

        # Add CC header if provided (visible to recipients)
        if cc:
            msg["Cc"] = ", ".join(cc)

        # Add BCC header when saving locally (drafts, sent copies)
        if bcc and include_bcc_header:
            msg["Bcc"] = ", ".join(bcc)

        # Set threading headers for replies
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = references
        if reply_to:
            msg["Reply-To"] = reply_to

        # Set Date and Message-Id headers
        msg["Date"] = email.utils.formatdate(localtime=True)
        sender_for_domain = sender_address or self.sender
        sender_domain = sender_for_domain.rsplit("@", 1)[-1].rstrip(">")
        msg["Message-Id"] = email.utils.make_msgid(domain=sender_domain)

        return msg

    async def send_email(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        html: bool = False,
        attachments: list[str] | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
        reply_to: str | None = None,
        inline_attachments: list[InlineAttachment] | None = None,
    ) -> MIMEText | MIMEMultipart:
        # Kwargs (not positional) since inline_attachments was appended to the
        # compose_message signature — a silent positional remap would move
        # threading headers into the wrong slots.
        msg = self.compose_message(
            recipients=recipients,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            html=html,
            attachments=attachments,
            in_reply_to=in_reply_to,
            references=references,
            include_bcc_header=False,
            reply_to=reply_to,
            inline_attachments=inline_attachments,
        )

        async with aiosmtplib.SMTP(
            hostname=self.email_server.host,
            port=self.email_server.port,
            start_tls=self.smtp_start_tls,
            use_tls=self.smtp_use_tls,
            tls_context=self._get_smtp_ssl_context(),
        ) as smtp:
            await smtp.login(self.email_server.user_name, self.email_server.password.get_secret_value())

            # Create a combined list of all recipients for delivery
            all_recipients = recipients.copy()
            if cc:
                all_recipients.extend(cc)
            if bcc:
                all_recipients.extend(bcc)

            await smtp.send_message(msg, recipients=all_recipients)

        # Return the message for potential saving to Sent folder
        return msg

    async def _find_sent_folder_by_flag(self, imap) -> str | None:
        """Find the Sent folder by searching for the \\Sent IMAP flag.

        Args:
            imap: Connected IMAP client

        Returns:
            The folder name with the \\Sent flag, or None if not found
        """
        try:
            # List all folders - aioimaplib requires reference_name and mailbox_pattern
            _, folders = await imap.list('""', "*")

            # Search for folder with \Sent flag
            for folder in folders:
                mailbox = _parse_list_response(folder)
                if mailbox and r"\Sent" in mailbox.flags:
                    logger.info(f"Found Sent folder by \\Sent flag: '{mailbox.name}'")
                    return mailbox.name
        except Exception as e:
            logger.debug(f"Error finding Sent folder by flag: {e}")

        return None

    async def append_to_sent(
        self,
        msg: MIMEText | MIMEMultipart,
        incoming_server: EmailServer,
        sent_folder_name: str | None = None,
    ) -> bool:
        """Append a sent message to the IMAP Sent folder.

        Args:
            msg: The email message that was sent
            incoming_server: IMAP server configuration for accessing Sent folder
            sent_folder_name: Override folder name, or None for auto-detection

        Returns:
            True if successfully saved, False otherwise
        """
        imap = await self._connect_imap_server(incoming_server)

        # Common Sent folder names across different providers
        sent_folder_candidates = [
            sent_folder_name,  # User-specified override (if provided)
            "Sent",
            "INBOX.Sent",
            "Sent Items",
            "Sent Mail",
            "[Gmail]/Sent Mail",
            "INBOX/Sent",
        ]
        # Filter out None values
        sent_folder_candidates = [f for f in sent_folder_candidates if f]

        try:
            await _imap_login(imap, incoming_server.user_name, incoming_server.password.get_secret_value())
            await _send_imap_id(imap)

            # Try to find Sent folder by IMAP \Sent flag first
            flag_folder = await self._find_sent_folder_by_flag(imap)
            if flag_folder and flag_folder not in sent_folder_candidates:
                # Add it at the beginning (high priority)
                sent_folder_candidates.insert(0, flag_folder)

            # Try to find and use the Sent folder
            for folder in sent_folder_candidates:
                try:
                    logger.debug(f"Trying Sent folder: '{folder}'")
                    # Try to select the folder to verify it exists
                    result = await imap.select(_quote_mailbox(folder))
                    logger.debug(f"Select result for '{folder}': {result}")

                    # aioimaplib returns (status, data) where status is a string like 'OK' or 'NO'
                    status = result[0] if isinstance(result, tuple) else result
                    if str(status).upper() == "OK":
                        # Folder exists, append the message
                        msg_bytes = msg.as_bytes()
                        logger.debug(f"Appending message to '{folder}'")
                        # aioimaplib.append signature: (message_bytes, mailbox, flags, date)
                        append_result = await imap.append(
                            msg_bytes,
                            mailbox=_quote_mailbox(folder),
                            flags=r"(\Seen)",
                        )
                        logger.debug(f"Append result: {append_result}")
                        append_status = append_result[0] if isinstance(append_result, tuple) else append_result
                        if str(append_status).upper() == "OK":
                            logger.info(f"Saved sent email to '{folder}'")
                            return True
                        else:
                            logger.warning(f"Failed to append to '{folder}': {append_status}")
                    else:
                        logger.debug(f"Folder '{folder}' select returned: {status}")
                except Exception as e:
                    logger.debug(f"Folder '{folder}' not available: {e}")
                    continue

            logger.warning("Could not find a valid Sent folder to save the message")
            return False

        except ConnectionError:
            raise
        except Exception as e:
            logger.error(f"Error saving to Sent folder: {e}")
            return False
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.debug(f"Error during logout: {e}")

    async def append_to_mailbox(
        self,
        msg: MIMEText | MIMEMultipart,
        incoming_server: EmailServer,
        mailbox: str,
        flags: str = r"(\Draft \Seen)",
    ) -> str | None:
        """Append a message to the specified IMAP folder.

        Unlike append_to_sent, this targets a single user-specified mailbox
        without folder discovery. Returns the IMAP UID of the appended message
        (if the server supports APPENDUID / RFC 4315), or ``"unknown"`` on
        success without UID, or ``None`` on failure.
        """
        imap = await self._connect_imap_server(incoming_server)

        try:
            await _imap_login(imap, incoming_server.user_name, incoming_server.password.get_secret_value())
            await _send_imap_id(imap)

            result = await imap.select(_quote_mailbox(mailbox))
            status = result[0] if isinstance(result, tuple) else result
            if str(status).upper() != "OK":
                logger.warning(f"Mailbox '{mailbox}' not found or not selectable: {status}")
                return None

            msg_bytes = msg.as_bytes()
            append_result = await imap.append(
                msg_bytes,
                mailbox=_quote_mailbox(mailbox),
                flags=flags,
            )
            append_status = append_result[0] if isinstance(append_result, tuple) else append_result
            if str(append_status).upper() == "OK":
                # Try to extract UID from APPENDUID response (RFC 4315)
                uid = None
                if isinstance(append_result, tuple) and len(append_result) > 1:
                    for part in append_result[1]:
                        part_str = part.decode("utf-8") if isinstance(part, bytes) else str(part)
                        match = re.search(r"APPENDUID\s+\d+\s+(\d+)", part_str, re.IGNORECASE)
                        if match:
                            uid = match.group(1)
                            break
                logger.info(f"Saved email to '{mailbox}'" + (f" (UID {uid})" if uid else ""))
                return uid or "unknown"
            else:
                logger.warning(f"Failed to append to '{mailbox}': {append_status}")
                return None

        except ConnectionError:
            raise
        except Exception as e:
            logger.error(f"Error saving to mailbox '{mailbox}': {e}")
            return None
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.debug(f"Error during logout: {e}")

    async def delete_emails(
        self,
        email_ids: list[str],
        mailbox: str = "INBOX",
        allowed_senders: list[str] | None = None,
        report_blocked_mutations: bool = False,
    ) -> tuple[list[str], list[str]]:
        """Delete emails by their UIDs. Returns (deleted_ids, failed_ids).

        A blocked sender's UID is never flagged \\Deleted: by default it is reported as a
        no-op success (indistinguishable from a nonexistent UID); when
        report_blocked_mutations is True it is reported in failed_ids instead.
        """
        imap = await self._connect_imap()
        deleted_ids = []
        failed_ids = []

        try:
            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            select_response = await imap.select(_quote_mailbox(mailbox))
            _raise_for_imap_error(select_response, f"SELECT mailbox {mailbox}")

            blocked = await self._blocked_uids(imap, email_ids, allowed_senders)
            deleted_any = False
            for email_id in email_ids:
                if email_id in blocked:
                    (failed_ids if report_blocked_mutations else deleted_ids).append(email_id)
                    continue
                try:
                    store_response = await imap.uid("store", email_id, "+FLAGS", r"(\Deleted)")
                    _raise_for_imap_error(store_response, f"STORE \\Deleted for email {email_id}")
                    deleted_ids.append(email_id)
                    deleted_any = True
                except Exception as e:
                    logger.error(f"Failed to delete email {email_id}: {e}")
                    failed_ids.append(email_id)

            if deleted_any:
                try:
                    expunge_response = await imap.expunge()
                    _raise_for_imap_error(expunge_response, "EXPUNGE deleted emails")
                except Exception as e:
                    logger.error(f"Failed to expunge deleted emails: {e}")
                    failed_ids.extend(deleted_ids)
                    deleted_ids = []
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

        return deleted_ids, failed_ids

    async def mark_emails_as_read(
        self,
        email_ids: list[str],
        mailbox: str = "INBOX",
        allowed_senders: list[str] | None = None,
        report_blocked_mutations: bool = False,
    ) -> tuple[list[str], list[str]]:
        """Mark emails as read by setting the \\Seen flag. Returns (marked_ids, failed_ids).

        A blocked sender's UID is never flagged \\Seen: reported as a no-op success by
        default, or in failed_ids when report_blocked_mutations is True.
        """
        imap = await self._connect_imap()
        marked_ids: list[str] = []
        failed_ids: list[str] = []

        try:
            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            select_response = await imap.select(_quote_mailbox(mailbox))
            _raise_for_imap_error(select_response, f"SELECT mailbox {mailbox}")

            blocked = await self._blocked_uids(imap, email_ids, allowed_senders)
            for email_id in email_ids:
                if email_id in blocked:
                    (failed_ids if report_blocked_mutations else marked_ids).append(email_id)
                    continue
                try:
                    store_response = await imap.uid("store", email_id, "+FLAGS", r"(\Seen)")
                    _raise_for_imap_error(store_response, f"STORE \\Seen for email {email_id}")
                    marked_ids.append(email_id)
                except Exception as e:
                    logger.error(f"Failed to mark email {email_id} as read: {e}")
                    failed_ids.append(email_id)
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

        return marked_ids, failed_ids

    async def move_emails(
        self,
        email_ids: list[str],
        source_mailbox: str,
        destination_mailbox: str,
        allowed_senders: list[str] | None = None,
        report_blocked_mutations: bool = False,
    ) -> tuple[list[str], list[str]]:
        """Move emails to a different mailbox. Uses IMAP MOVE (RFC 6851) with COPY+DELETE fallback.

        A blocked sender's UID is never copied/moved: reported as a no-op success by default,
        or in failed_ids when report_blocked_mutations is True.
        """
        imap = await self._connect_imap()
        moved_ids = []
        failed_ids = []

        try:
            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            select_response = await imap.select(_quote_mailbox(source_mailbox))
            _raise_for_imap_error(select_response, f"SELECT source mailbox {source_mailbox}")

            capabilities = {str(capability).upper() for capability in getattr(imap, "capabilities", ())}
            has_move = hasattr(imap, "move") and "MOVE" in capabilities

            blocked = await self._blocked_uids(imap, email_ids, allowed_senders)
            copied: list[str] = []  # allowed UIDs that actually completed COPY+STORE on the fallback path
            for email_id in email_ids:
                if email_id in blocked:
                    (failed_ids if report_blocked_mutations else moved_ids).append(email_id)
                    continue
                try:
                    if has_move:
                        move_response = await imap.uid("move", email_id, _quote_mailbox(destination_mailbox))
                        _raise_for_imap_error(move_response, f"MOVE email {email_id}")
                    else:
                        copy_response = await imap.uid("copy", email_id, _quote_mailbox(destination_mailbox))
                        _raise_for_imap_error(copy_response, f"COPY email {email_id}")
                        store_response = await imap.uid("store", email_id, "+FLAGS", r"(\Deleted)")
                        _raise_for_imap_error(store_response, f"STORE \\Deleted for email {email_id}")
                        copied.append(email_id)
                    moved_ids.append(email_id)
                except Exception as e:
                    logger.error(f"Failed to move email {email_id}: {e}")
                    failed_ids.append(email_id)

            if copied:  # only expunge when real COPY+STORE happened — never for blocked/no-op UIDs
                try:
                    expunge_response = await imap.expunge()
                    _raise_for_imap_error(expunge_response, "EXPUNGE moved emails")
                except Exception as e:
                    logger.error(f"Failed to expunge moved emails: {e}")
                    failed_ids.extend(copied)
                    moved_ids = [uid for uid in moved_ids if uid not in set(copied)]
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

        return moved_ids, failed_ids

    async def list_mailboxes(self, pattern: str = "*", reference: str = "") -> list[MailboxInfo]:
        """List available IMAP mailboxes with flags and delimiter."""
        imap = await self._connect_imap()
        mailboxes = []

        try:
            await _imap_login(imap, self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)

            quoted_ref = _quote_mailbox(reference) if reference else '""'
            quoted_pattern = _quote_mailbox(pattern)
            response = await imap.list(quoted_ref, quoted_pattern)
            _raise_for_imap_error(response, f"LIST mailboxes with pattern {pattern}")
            _, data = response

            for item in data:
                mailbox = _parse_list_response(item)
                if mailbox:
                    mailboxes.append(mailbox)
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

        return mailboxes


class ClassicEmailHandler(EmailHandler):
    def __init__(self, email_settings: EmailSettings):
        self.email_settings = email_settings
        self.incoming_client = EmailClient(email_settings.incoming)
        self.outgoing_client = (
            EmailClient(
                email_settings.outgoing,
                sender=f"{email_settings.full_name} <{email_settings.email_address}>",
            )
            if email_settings.outgoing
            else None
        )
        self.save_to_sent = email_settings.save_to_sent
        self.sent_folder_name = email_settings.sent_folder_name

    async def get_emails_metadata(
        self,
        page: int = 1,
        page_size: int = 10,
        before: datetime | None = None,
        since: datetime | None = None,
        subject: str | None = None,
        from_address: str | None = None,
        to_address: str | None = None,
        order: str = "desc",
        mailbox: str = "INBOX",
        seen: bool | None = None,
        flagged: bool | None = None,
        answered: bool | None = None,
        body: str | None = None,
        text: str | None = None,
        has_attachment: bool | None = None,
    ) -> EmailMetadataPageResponse:
        total, email_dicts = await self.incoming_client.get_emails_metadata(
            page,
            page_size,
            before,
            since,
            subject,
            from_address,
            to_address,
            order,
            mailbox,
            seen,
            flagged,
            answered,
            body,
            text,
            has_attachment,
            allowed_senders=get_settings().allowed_senders,
        )
        emails = [EmailMetadata.from_email(d) for d in email_dicts]
        return EmailMetadataPageResponse(
            page=page,
            page_size=page_size,
            before=before,
            since=since,
            subject=subject,
            emails=emails,
            total=total,
        )

    async def get_emails_content(
        self,
        email_ids: list[str],
        mailbox: str = "INBOX",
        mark_as_read: bool = False,
        body_offset: int = 0,
        max_body_length: int = MAX_BODY_LENGTH,
    ) -> EmailContentBatchResponse:
        """Batch retrieve email body content, honoring the sender allowlist.

        The allowlist is enforced in the read path: get_email_body_by_id checks the From header
        before fetching the body, so a blocked message is never read or marked and returns None —
        indistinguishable from a missing/inaccessible one (both land in failed_ids).
        """
        allowed_senders = get_settings().allowed_senders
        emails = []
        failed_ids = []

        for email_id in email_ids:
            try:
                email_data = await self.incoming_client.get_email_body_by_id(
                    email_id,
                    mailbox,
                    mark_as_read,
                    allowed_senders=allowed_senders,
                    body_offset=body_offset,
                    max_body_length=max_body_length,
                )
                if not email_data:
                    failed_ids.append(email_id)
                    continue
                emails.append(
                    EmailBodyResponse(
                        email_id=email_data["email_id"],
                        message_id=email_data.get("message_id"),
                        subject=email_data["subject"],
                        sender=email_data["from"],
                        recipients=email_data["to"],
                        date=email_data["date"],
                        body=email_data["body"],
                        attachments=email_data["attachments"],
                    )
                )
            except Exception as e:
                logger.error(f"Failed to retrieve email {email_id}: {e}")
                failed_ids.append(email_id)

        return EmailContentBatchResponse(
            emails=emails,
            requested_count=len(email_ids),
            retrieved_count=len(emails),
            failed_ids=failed_ids,
        )

    async def send_email(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        html: bool = False,
        attachments: list[str] | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
        reply_to: str | None = None,
        inline_attachments: list[InlineAttachment] | None = None,
    ) -> None:
        if self.outgoing_client is None:
            raise RuntimeError(f"SMTP is not configured for account '{self.email_settings.account_name}'")

        # Kwargs (not positional) since inline_attachments was appended to the
        # EmailClient.send_email signature.
        msg = await self.outgoing_client.send_email(
            recipients=recipients,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            html=html,
            attachments=attachments,
            in_reply_to=in_reply_to,
            references=references,
            reply_to=reply_to,
            inline_attachments=inline_attachments,
        )

        # Save to Sent folder if enabled
        if self.save_to_sent and msg:
            # Add BCC header to the saved copy so users can see who was BCC'd.
            # This MUST happen after smtp.send_message() — that ordering is
            # load-bearing for security (BCC must not appear in sent headers).
            if bcc and msg["Bcc"] is None:
                msg["Bcc"] = ", ".join(bcc)
            try:
                await self.outgoing_client.append_to_sent(
                    msg,
                    self.email_settings.incoming,
                    self.sent_folder_name,
                )
            except Exception as e:
                logger.error(f"Failed to save email to Sent folder: {e}", exc_info=True)

    async def save_to_mailbox(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        mailbox: str = "Drafts",
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        html: bool = False,
        attachments: list[str] | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
        flags: list[str] | None = None,
        inline_attachments: list[InlineAttachment] | None = None,
    ) -> str:
        """Compose and save an email to the specified IMAP mailbox.

        BCC headers are preserved in the saved message so mail clients can
        display BCC recipients (unlike ``send_email``, where BCC is handled
        via the SMTP envelope only).

        Returns:
            A string in the format ``<message-id>|uid:<uid>``.

        Raises:
            ValueError: If any flag in *flags* is invalid per RFC 3501, or if
                any inline attachment fails validation.
            RuntimeError: If the IMAP APPEND operation fails.
        """
        if self.outgoing_client is None:
            raise RuntimeError(f"SMTP is not configured for account '{self.email_settings.account_name}'")

        # Kwargs (not positional) since inline_attachments was appended to the
        # compose_message signature.
        msg = self.outgoing_client.compose_message(
            recipients=recipients,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            html=html,
            attachments=attachments,
            in_reply_to=in_reply_to,
            references=references,
            include_bcc_header=True,
            inline_attachments=inline_attachments,
        )

        flags_str = r"(\Draft \Seen)" if flags is None else _validate_flags(flags)

        uid = await self.outgoing_client.append_to_mailbox(msg, self.email_settings.incoming, mailbox, flags_str)

        if uid is None:
            raise RuntimeError(f"Failed to save email to mailbox '{mailbox}'")

        message_id = msg["Message-Id"] or "saved"
        return f"{message_id}|uid:{uid}"

    async def delete_emails(self, email_ids: list[str], mailbox: str = "INBOX") -> tuple[list[str], list[str]]:
        """Delete emails by their UIDs. Returns (deleted_ids, failed_ids)."""
        settings = get_settings()
        return await self.incoming_client.delete_emails(
            email_ids,
            mailbox,
            allowed_senders=settings.allowed_senders,
            report_blocked_mutations=settings.report_blocked_mutations,
        )

    async def mark_emails_as_read(self, email_ids: list[str], mailbox: str = "INBOX") -> tuple[list[str], list[str]]:
        """Mark emails as read by their UIDs. Returns (marked_ids, failed_ids)."""
        settings = get_settings()
        return await self.incoming_client.mark_emails_as_read(
            email_ids,
            mailbox,
            allowed_senders=settings.allowed_senders,
            report_blocked_mutations=settings.report_blocked_mutations,
        )

    async def move_emails(
        self, email_ids: list[str], source_mailbox: str, destination_mailbox: str
    ) -> tuple[list[str], list[str]]:
        """Move emails between mailboxes. Returns (moved_ids, failed_ids)."""
        settings = get_settings()
        return await self.incoming_client.move_emails(
            email_ids,
            source_mailbox,
            destination_mailbox,
            allowed_senders=settings.allowed_senders,
            report_blocked_mutations=settings.report_blocked_mutations,
        )

    async def _find_archive_folder(self) -> str | None:
        """Locate the Archive folder via the RFC 6154 ``\\Archive`` flag, then common names."""
        mailboxes = await self.incoming_client.list_mailboxes()
        for mailbox_info in mailboxes:
            if any(flag.lstrip("\\").lower() == "archive" for flag in mailbox_info.flags):
                return mailbox_info.name

        names_by_lowercase = {mailbox_info.name.lower(): mailbox_info.name for mailbox_info in mailboxes}
        for candidate in _ARCHIVE_FOLDER_CANDIDATES:
            archive_folder = names_by_lowercase.get(candidate.lower())
            if archive_folder is not None:
                return archive_folder
        return None

    async def archive_emails(self, email_ids: list[str], mailbox: str = "INBOX") -> tuple[list[str], list[str], str]:
        """Move emails to the auto-detected Archive folder. Returns (moved_ids, failed_ids, archive_folder)."""
        archive_folder = await self._find_archive_folder()
        if archive_folder is None:
            raise ValueError(
                "No Archive folder found (looked for the RFC 6154 \\Archive flag and common names: "
                f"{', '.join(_ARCHIVE_FOLDER_CANDIDATES)}). Use move_emails with an explicit folder instead."
            )
        settings = get_settings()
        moved_ids, failed_ids = await self.incoming_client.move_emails(
            email_ids,
            mailbox,
            archive_folder,
            allowed_senders=settings.allowed_senders,
            report_blocked_mutations=settings.report_blocked_mutations,
        )
        return moved_ids, failed_ids, archive_folder

    async def list_mailboxes(self, pattern: str = "*", reference: str = "") -> list[MailboxInfo]:
        """List available mailboxes with flags and delimiter."""
        return await self.incoming_client.list_mailboxes(pattern, reference)

    async def download_attachment(
        self,
        email_id: str,
        attachment_name: str,
        save_path: str,
        mailbox: str = "INBOX",
    ) -> AttachmentDownloadResponse:
        """Download an email attachment and save it to the specified path.

        Args:
            email_id: The UID of the email containing the attachment.
            attachment_name: The filename of the attachment to download.
            save_path: The local path where the attachment will be saved.
            mailbox: The mailbox to search in (default: "INBOX").

        Returns:
            AttachmentDownloadResponse with download result information.
        """
        allowed_senders = get_settings().allowed_senders
        result = await self.incoming_client.download_attachment(
            email_id, attachment_name, save_path, mailbox, allowed_senders=allowed_senders
        )
        return AttachmentDownloadResponse(
            email_id=result["email_id"],
            attachment_name=result["attachment_name"],
            mime_type=result["mime_type"],
            size=result["size"],
            saved_path=result["saved_path"],
        )

    async def download_attachment_inline(
        self,
        email_id: str,
        attachment_name: str,
        mailbox: str = "INBOX",
        *,
        max_bytes: int,
    ) -> AttachmentDownloadResponse:
        """Download an email attachment as inline base64 bytes (no disk write).

        Symmetric companion to :py:meth:`download_attachment` for remote MCP
        clients that cannot read the server's filesystem. The sender allowlist
        is enforced the same way in both modes.
        """
        allowed_senders = get_settings().allowed_senders
        result = await self.incoming_client.fetch_attachment_inline(
            email_id,
            attachment_name,
            mailbox,
            allowed_senders=allowed_senders,
            max_bytes=max_bytes,
        )
        return AttachmentDownloadResponse(
            email_id=result["email_id"],
            attachment_name=result["attachment_name"],
            mime_type=result["mime_type"],
            size=result["size"],
            content_base64=result["content_base64"],
        )
