import asyncio
import base64
import email.utils
import mimetypes
import re
import ssl
import time
import unicodedata
from collections.abc import AsyncGenerator
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

from mcp_email_server.allowlist import sender_allowed
from mcp_email_server.config import EmailServer, EmailSettings
from mcp_email_server.emails import EmailHandler
from mcp_email_server.emails.models import (
    AttachmentDownloadResponse,
    EmailBodyResponse,
    EmailContentBatchResponse,
    EmailMarkResponse,
    EmailMetadata,
    EmailMetadataPageResponse,
    InlineAttachment,
)
from mcp_email_server.log import logger

# Maximum body length before truncation (characters)
MAX_BODY_LENGTH = 20000


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


def _quote_mailbox(mailbox: str) -> str:
    """Quote mailbox name for IMAP compatibility.

    Some IMAP servers (notably Proton Mail Bridge) require mailbox names
    to be quoted. This is valid per RFC 3501 and works with all IMAP servers.

    Per RFC 3501 Section 9 (Formal Syntax), quoted strings must escape
    backslashes and double-quote characters with a preceding backslash.

    See: https://github.com/ai-zerolab/mcp-email-server/issues/87
    See: https://www.rfc-editor.org/rfc/rfc3501#section-9
    """
    # Per RFC 3501, literal double-quote characters in a quoted string must
    # be escaped with a backslash. Backslashes themselves must also be escaped.
    escaped = mailbox.replace("\\", "\\\\").replace('"', r"\"")
    return f'"{escaped}"'


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
            await imap.protocol.execute(
                aioimaplib.Command(
                    "ID",
                    imap.protocol.new_tag(),
                    '("name" "mcp-email-server" "version" "1.0.0")',
                )
            )
    except Exception as e:
        logger.warning(f"IMAP ID command failed: {e!s}")


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


# Backwards-compatible alias
_create_smtp_ssl_context = _create_ssl_context


# RFC 2045 token chars (no parameters, no control chars). Conservative.
_MIME_TYPE_TOKEN = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
# Filename-disallowed characters under our conservative LLM-transport policy.
# Strict because filenames are a display string only — users with weird names
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


def _resolve_inline_attachment(item: InlineAttachment, max_per_item: int) -> "_ResolvedAttachment":
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
    except (ValueError, Exception) as e:
        # binascii.Error subclasses ValueError on Python 3.10+, but keep the
        # broader except for older environments. Message stays content-free.
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


class _ResolvedAttachment(NamedTuple):
    """An attachment resolved to MIME-assembly-ready bytes.

    Inputs to the send pipeline (file paths today; inline base64 in a
    forthcoming commit) get normalized into this shape before being added
    to a MIMEMultipart. Keeping ``maintype`` / ``subtype`` separate avoids
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
    attachment name — so the inline-download response shows the same
    filename a mail client would.
    """

    data: bytes
    mime_type: str
    filename: str


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

    def _parse_email_data(self, raw_email: bytes, email_id: str | None = None) -> dict[str, Any]:  # noqa: C901
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

        def _strip_html(html: str) -> str:
            """Simple HTML to text conversion."""
            import re

            # Remove script and style elements
            text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
            # Convert common block elements to newlines
            text = re.sub(r"<(br|p|div|tr|li)[^>]*/?>", "\n", text, flags=re.IGNORECASE)
            # Remove all remaining HTML tags
            text = re.sub(r"<[^>]+>", "", text)
            # Decode common HTML entities
            text = text.replace("&nbsp;", " ").replace("&amp;", "&")
            text = text.replace("&lt;", "<").replace("&gt;", ">")
            text = text.replace("&quot;", '"').replace("&#39;", "'")
            # Collapse multiple newlines and whitespace
            text = re.sub(r"\n\s*\n", "\n\n", text)
            text = re.sub(r" +", " ", text)
            return text.strip()

        if email_message.is_multipart():
            for part in email_message.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))

                # Handle attachments
                if "attachment" in content_disposition:
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
                body = _strip_html(html_body)
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

                body = _strip_html(text) if content_type == "text/html" else text
        # TODO: Allow retrieving full email body
        if body and len(body) > MAX_BODY_LENGTH:
            body = body[:MAX_BODY_LENGTH] + "...[TRUNCATED]"
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
    ) -> list[str]:
        search_criteria = []
        if before:
            search_criteria.extend(["BEFORE", before.strftime("%d-%b-%Y").upper()])
        if since:
            search_criteria.extend(["SINCE", since.strftime("%d-%b-%Y").upper()])
        if subject:
            search_criteria.extend(["SUBJECT", EmailClient._sanitize_imap_value(subject)])
        if body:
            search_criteria.extend(["BODY", EmailClient._sanitize_imap_value(body)])
        if text:
            search_criteria.extend(["TEXT", EmailClient._sanitize_imap_value(text)])
        if from_address:
            search_criteria.extend(["FROM", EmailClient._sanitize_imap_value(from_address)])
        if to_address:
            search_criteria.extend(["TO", EmailClient._sanitize_imap_value(to_address)])

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
        """Parse raw email headers into metadata dictionary."""
        try:
            parser = BytesParser(policy=default)
            email_message = parser.parsebytes(raw_headers)

            subject = email_message.get("Subject", "")
            sender = email_message.get("From", "")
            date_str = email_message.get("Date", "")

            to_addresses = self._parse_recipients(email_message)
            date = self._parse_date(date_str)

            return {
                "email_id": email_id,
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

    async def get_email_count(
        self,
        before: datetime | None = None,
        since: datetime | None = None,
        subject: str | None = None,
        from_address: str | None = None,
        to_address: str | None = None,
        mailbox: str = "INBOX",
        seen: bool | None = None,
        flagged: bool | None = None,
        answered: bool | None = None,
    ) -> int:
        imap = self._imap_connect()
        try:
            # Wait for the connection to be established
            await imap._client_task
            await imap.wait_hello_from_server()

            # Login and select inbox
            await imap.login(self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            await imap.select(_quote_mailbox(mailbox))
            search_criteria = self._build_search_criteria(
                before,
                since,
                subject,
                from_address=from_address,
                to_address=to_address,
                seen=seen,
                flagged=flagged,
                answered=answered,
            )
            logger.info(f"Count: Search criteria: {search_criteria}")
            # Search for messages and count them - use UID SEARCH for consistency
            _, messages = await imap.uid_search(*search_criteria)
            return len(messages[0].split())
        finally:
            # Ensure we logout properly
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

    async def get_emails_metadata_stream(
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
    ) -> AsyncGenerator[dict[str, Any], None]:
        imap = self._imap_connect()
        try:
            # Wait for the connection to be established
            await imap._client_task
            await imap.wait_hello_from_server()

            # Login and select mailbox
            await imap.login(self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            await imap.select(_quote_mailbox(mailbox))

            search_criteria = self._build_search_criteria(
                before,
                since,
                subject,
                from_address=from_address,
                to_address=to_address,
                seen=seen,
                flagged=flagged,
                answered=answered,
            )
            logger.info(f"Get metadata: Search criteria: {search_criteria}")

            # Search for messages - use UID SEARCH for better compatibility
            _, messages = await imap.uid_search(*search_criteria)

            # Handle empty or None responses
            if not messages or not messages[0]:
                logger.warning("No messages returned from search")
                return

            email_ids = messages[0].split()
            logger.info(f"Found {len(email_ids)} email IDs")

            # Phase 1: Batch fetch INTERNALDATE for sorting (sequential chunks)
            fetch_dates_start = time.perf_counter()
            uid_dates = await self._batch_fetch_dates(imap, email_ids)
            fetch_dates_elapsed = time.perf_counter() - fetch_dates_start

            # Sort by INTERNALDATE
            sorted_uids = sorted(uid_dates.items(), key=lambda x: x[1], reverse=(order == "desc"))

            # Paginate
            start = (page - 1) * page_size
            page_uids = [uid for uid, _ in sorted_uids[start : start + page_size]]

            if not page_uids:
                logger.info(f"Phase 1 (dates): {len(uid_dates)} UIDs in {fetch_dates_elapsed:.2f}s, page {page} empty")
                return

            # Phase 2: Batch fetch headers for requested page only
            fetch_headers_start = time.perf_counter()
            metadata_by_uid = await self._batch_fetch_headers(imap, page_uids)
            fetch_headers_elapsed = time.perf_counter() - fetch_headers_start

            logger.info(
                f"Fetched page {page}: {fetch_dates_elapsed:.2f}s dates ({len(uid_dates)} UIDs), "
                f"{fetch_headers_elapsed:.2f}s headers ({len(page_uids)} UIDs)"
            )

            # Yield in sorted order
            for uid in page_uids:
                if uid in metadata_by_uid:
                    yield metadata_by_uid[uid]
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
        """Try different fetch formats to get email data."""
        fetch_formats = ["RFC822", "BODY[]", "BODY.PEEK[]", "(BODY.PEEK[])"]

        for fetch_format in fetch_formats:
            try:
                _, data = await imap.uid("fetch", email_id, fetch_format)

                if data and len(data) > 0 and self._check_email_content(data):
                    return data

            except Exception as e:
                logger.debug(f"Fetch format {fetch_format} failed: {e}")

        return None

    async def get_email_body_by_id(self, email_id: str, mailbox: str = "INBOX") -> dict[str, Any] | None:
        imap = self._imap_connect()
        try:
            # Wait for the connection to be established
            await imap._client_task
            await imap.wait_hello_from_server()

            # Login and select inbox
            await imap.login(self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            await imap.select(_quote_mailbox(mailbox))

            # Fetch the specific email by UID
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
                return self._parse_email_data(raw_email, email_id)
            except Exception as e:
                logger.error(f"Error parsing email: {e!s}")
                return None

        finally:
            # Ensure we logout properly
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

    @staticmethod
    def _find_attachment_part(email_message, attachment_name: str) -> _FetchedAttachment | None:
        """Walk a parsed message and return the named attachment, or None.

        Looks at every part with a ``Content-Disposition`` header that mentions
        ``attachment`` and returns the first match by filename.
        """
        if not email_message.is_multipart():
            return None
        for part in email_message.walk():
            content_disposition = str(part.get("Content-Disposition", ""))
            if "attachment" not in content_disposition:
                continue
            filename = part.get_filename()
            if filename != attachment_name:
                continue
            return _FetchedAttachment(
                data=part.get_payload(decode=True),
                mime_type=part.get_content_type(),
                filename=filename,
            )
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

        Returns the raw bytes, MIME type, and the filename as the parsed MIME
        part reports it (which may differ from ``attachment_name`` if encoded
        headers normalize differently).

        One IMAP round-trip total — the same fetch covers both the sender
        check (against ``From``) and the attachment extraction.

        Args:
            email_id: The UID of the email containing the attachment.
            attachment_name: The filename of the attachment to download.
            mailbox: The mailbox to search in (default: "INBOX").
            allowed_senders: Optional per-message allowlist; if non-empty,
                the email's ``From`` header must match before extraction.
                Fail-closed required-mode lives in the MCP tool layer.

        Raises:
            ValueError: If the sender is not in ``allowed_senders`` (stable
                message prefix ``"Attachment download blocked:"``), or the
                requested attachment cannot be located.
        """
        imap = self._imap_connect()
        try:
            await imap._client_task
            await imap.wait_hello_from_server()

            await imap.login(self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            await imap.select(_quote_mailbox(mailbox))

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

            # Sender allowlist enforcement — happens after parse so the
            # already-fetched From header is reused; no extra IMAP round-trip.
            if allowed_senders:
                from_header = str(email_message.get("From", ""))
                if not sender_allowed(from_header, allowed_senders):
                    raise ValueError(
                        f"Attachment download blocked: sender {from_header!r} is not in the configured allowlist."
                    )

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
        *,
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
            allowed_senders: Optional per-message sender allowlist.

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
        logger.info(f"Attachment '{fetched.filename}' saved to {save_path}")

        return {
            "email_id": email_id,
            "attachment_name": attachment_name,
            "mime_type": fetched.mime_type,
            "size": len(fetched.data),
            "saved_path": str(save_file.resolve()),
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
        inline_attachments: list[InlineAttachment] | None,
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

        # Deferred settings import keeps unit tests that exercise compose_message
        # directly (without monkey-patching settings) on sane defaults. Production
        # paths still see env-overridden values via get_settings().
        from mcp_email_server.config import get_settings

        settings = get_settings()
        per_item = settings.max_inline_attachment_bytes_per_item
        aggregate = settings.max_inline_attachment_bytes

        running_total = 0
        for idx, item in enumerate(inline_attachments):
            try:
                rendered = _resolve_inline_attachment(item, per_item)
            except ValueError as e:
                raise ValueError(f"inline_attachments[{idx}]: {e}") from None
            running_total += len(rendered.data)
            if running_total > aggregate:
                raise ValueError(
                    f"inline_attachments aggregate size {running_total} bytes exceeds cap {aggregate}. "
                    f"Raise MCP_EMAIL_SERVER_MAX_INLINE_ATTACHMENT_BYTES or remove attachments."
                )
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
        inline_attachments: list[InlineAttachment] | None = None,
    ) -> MIMEText | MIMEMultipart:
        """Compose an email message without sending it.

        Builds MIME structure, sets headers (Subject, From, To, Cc, Date,
        Message-Id, threading headers). Synchronous — no I/O.

        Two attachment inputs that are concatenated server-side:

        - ``attachments`` is a list of absolute paths on the server's
          filesystem. Resolved via :py:meth:`_resolve_path_attachment`.
        - ``inline_attachments`` is a list of
          :py:class:`~mcp_email_server.emails.models.InlineAttachment` objects
          carrying base64-encoded bytes. Resolved via
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

        # Handle sender name with special characters
        if any(ord(c) > 127 for c in self.sender):
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

        # Set Date and Message-Id headers
        msg["Date"] = email.utils.formatdate(localtime=True)
        sender_domain = self.sender.rsplit("@", 1)[-1].rstrip(">")
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
        inline_attachments: list[InlineAttachment] | None = None,
    ) -> MIMEText | MIMEMultipart:
        # Switched from positional to kwargs while threading inline_attachments
        # through compose_message — see the inline-attachments plan, section
        # "Positional-API compatibility".
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
                folder_str = folder.decode("utf-8") if isinstance(folder, bytes) else str(folder)
                # IMAP LIST response format: (flags) "delimiter" "name"
                # Example: (\Sent \HasNoChildren) "/" "Gesendete Objekte"
                if r"\Sent" in folder_str or "\\Sent" in folder_str:
                    # Extract folder name from the response
                    # Split by quotes and get the last quoted part
                    parts = folder_str.split('"')
                    if len(parts) >= 3:
                        folder_name = parts[-2]  # The folder name is the second-to-last quoted part
                        logger.info(f"Found Sent folder by \\Sent flag: '{folder_name}'")
                        return folder_name
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
        if incoming_server.use_ssl:
            imap_ssl_context = _create_ssl_context(incoming_server.verify_ssl)
            imap = aioimaplib.IMAP4_SSL(incoming_server.host, incoming_server.port, ssl_context=imap_ssl_context)
        else:
            imap = aioimaplib.IMAP4(incoming_server.host, incoming_server.port)

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
            await imap._client_task
            await imap.wait_hello_from_server()
            await imap.login(incoming_server.user_name, incoming_server.password.get_secret_value())
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
        if incoming_server.use_ssl:
            imap_ssl_context = _create_ssl_context(incoming_server.verify_ssl)
            imap = aioimaplib.IMAP4_SSL(incoming_server.host, incoming_server.port, ssl_context=imap_ssl_context)
        else:
            imap = aioimaplib.IMAP4(incoming_server.host, incoming_server.port)

        try:
            await imap._client_task
            await imap.wait_hello_from_server()
            await imap.login(incoming_server.user_name, incoming_server.password.get_secret_value())
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

        except Exception as e:
            logger.error(f"Error saving to mailbox '{mailbox}': {e}")
            return None
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.debug(f"Error during logout: {e}")

    async def delete_emails(self, email_ids: list[str], mailbox: str = "INBOX") -> tuple[list[str], list[str]]:
        """Delete emails by their UIDs. Returns (deleted_ids, failed_ids)."""
        imap = self._imap_connect()
        deleted_ids = []
        failed_ids = []

        try:
            await imap._client_task
            await imap.wait_hello_from_server()
            await imap.login(self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            await imap.select(_quote_mailbox(mailbox))

            for email_id in email_ids:
                try:
                    await imap.uid("store", email_id, "+FLAGS", r"(\Deleted)")
                    deleted_ids.append(email_id)
                except Exception as e:
                    logger.error(f"Failed to delete email {email_id}: {e}")
                    failed_ids.append(email_id)

            await imap.expunge()
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

        return deleted_ids, failed_ids

    async def move_emails(
        self, email_ids: list[str], source_mailbox: str, destination_mailbox: str
    ) -> tuple[list[str], list[str]]:
        """Move emails to a different mailbox. Uses IMAP MOVE (RFC 6851) with COPY+DELETE fallback."""
        imap = self._imap_connect()
        moved_ids = []
        failed_ids = []

        try:
            await imap._client_task
            await imap.wait_hello_from_server()
            await imap.login(self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            await imap.select(_quote_mailbox(source_mailbox))

            has_move = hasattr(imap, "move") and "MOVE" in getattr(imap, "capabilities", ())

            for email_id in email_ids:
                try:
                    if has_move:
                        await imap.uid("move", email_id, _quote_mailbox(destination_mailbox))
                    else:
                        await imap.uid("copy", email_id, _quote_mailbox(destination_mailbox))
                        await imap.uid("store", email_id, "+FLAGS", r"(\Deleted)")
                    moved_ids.append(email_id)
                except Exception as e:
                    logger.error(f"Failed to move email {email_id}: {e}")
                    failed_ids.append(email_id)

            if not has_move and moved_ids:
                await imap.expunge()
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

        return moved_ids, failed_ids

    async def list_mailboxes(self, pattern: str = "*", reference: str = "") -> list[dict]:
        """List available IMAP mailboxes with flags and delimiter."""
        from mcp_email_server.emails.models import MailboxInfo

        imap = self._imap_connect()
        mailboxes = []

        try:
            await imap._client_task
            await imap.wait_hello_from_server()
            await imap.login(self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)

            quoted_ref = f'"{reference}"' if reference else '""'
            _, data = await imap.list(quoted_ref, pattern)

            for item in data:
                if item == b"":
                    continue
                item_str = item.decode("utf-8") if isinstance(item, bytes) else str(item)
                # IMAP LIST response format: (\Flag1 \Flag2) "delimiter" "name"
                # Parse flags from parentheses
                flags = []
                if "(" in item_str and ")" in item_str:
                    flags_str = item_str[item_str.index("(") + 1 : item_str.index(")")]
                    flags = [f.strip() for f in flags_str.split() if f.strip()]

                # Parse delimiter and name from quoted parts
                parts = item_str.split('"')
                if len(parts) >= 3:
                    delimiter = parts[1]  # First quoted string is delimiter
                    folder_name = parts[-2]  # Last quoted string is name
                    mailboxes.append(MailboxInfo(name=folder_name, delimiter=delimiter, flags=flags))
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

        return mailboxes

    async def mark_emails(
        self, email_ids: list[str], mark_as: str, mailbox: str = "INBOX"
    ) -> tuple[list[str], list[str]]:
        """Mark emails as read or unread. Returns (marked_ids, failed_ids)."""
        if mark_as == "read":
            flag_op = "+FLAGS"
        elif mark_as == "unread":
            flag_op = "-FLAGS"
        else:
            raise ValueError(f"Invalid mark_as value: {mark_as}. Must be 'read' or 'unread'.")

        imap = self._imap_connect()
        marked_ids: list[str] = []
        failed_ids: list[str] = []

        try:
            await imap._client_task
            await imap.wait_hello_from_server()
            await imap.login(self.email_server.user_name, self.email_server.password.get_secret_value())
            await _send_imap_id(imap)
            await imap.select(_quote_mailbox(mailbox))

            for email_id in email_ids:
                try:
                    await imap.uid("store", email_id, flag_op, r"(\Seen)")
                    marked_ids.append(email_id)
                except Exception as e:
                    logger.error(f"Failed to mark email {email_id} as {mark_as}: {e}")
                    failed_ids.append(email_id)
        finally:
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

        return marked_ids, failed_ids


class ClassicEmailHandler(EmailHandler):
    def __init__(self, email_settings: EmailSettings):
        self.email_settings = email_settings
        self.incoming_client = EmailClient(email_settings.incoming)
        self.outgoing_client = EmailClient(
            email_settings.outgoing,
            sender=f"{email_settings.full_name} <{email_settings.email_address}>",
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
    ) -> EmailMetadataPageResponse:
        emails = []
        async for email_data in self.incoming_client.get_emails_metadata_stream(
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
        ):
            emails.append(EmailMetadata.from_email(email_data))
        total = await self.incoming_client.get_email_count(
            before,
            since,
            subject,
            from_address=from_address,
            to_address=to_address,
            mailbox=mailbox,
            seen=seen,
            flagged=flagged,
            answered=answered,
        )
        return EmailMetadataPageResponse(
            page=page,
            page_size=page_size,
            before=before,
            since=since,
            subject=subject,
            emails=emails,
            total=total,
        )

    async def get_emails_content(self, email_ids: list[str], mailbox: str = "INBOX") -> EmailContentBatchResponse:
        """Batch retrieve email body content"""
        emails = []
        failed_ids = []

        for email_id in email_ids:
            try:
                email_data = await self.incoming_client.get_email_body_by_id(email_id, mailbox)
                if email_data:
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
                else:
                    failed_ids.append(email_id)
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
        inline_attachments: list[InlineAttachment] | None = None,
    ) -> None:
        # Switched to kwargs at the same time inline_attachments was appended
        # to the EmailClient.send_email signature — see plan §"Positional-API
        # compatibility".
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
        # Switched to kwargs while threading inline_attachments through.
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
        return await self.incoming_client.delete_emails(email_ids, mailbox)

    async def move_emails(
        self, email_ids: list[str], source_mailbox: str, destination_mailbox: str
    ) -> tuple[list[str], list[str]]:
        """Move emails between mailboxes. Returns (moved_ids, failed_ids)."""
        return await self.incoming_client.move_emails(email_ids, source_mailbox, destination_mailbox)

    async def list_mailboxes(self, pattern: str = "*", reference: str = "") -> list:
        """List available mailboxes with flags and delimiter."""
        return await self.incoming_client.list_mailboxes(pattern, reference)

    async def mark_emails(
        self,
        email_ids: list[str],
        mark_as: str,
        mailbox: str = "INBOX",
    ) -> EmailMarkResponse:
        """Mark emails as read or unread."""
        marked_ids, failed_ids = await self.incoming_client.mark_emails(email_ids, mark_as, mailbox)
        return EmailMarkResponse(
            success=len(failed_ids) == 0,
            marked_ids=marked_ids,
            failed_ids=failed_ids,
            mailbox=mailbox,
            marked_as=mark_as,
        )

    async def download_attachment(
        self,
        email_id: str,
        attachment_name: str,
        save_path: str,
        mailbox: str = "INBOX",
        *,
        allowed_senders: list[str] | None = None,
    ) -> AttachmentDownloadResponse:
        """Download an email attachment and save it to the specified path.

        Args:
            email_id: The UID of the email containing the attachment.
            attachment_name: The filename of the attachment to download.
            save_path: The local path where the attachment will be saved.
            mailbox: The mailbox to search in (default: "INBOX").
            allowed_senders: Optional sender allowlist enforced before the
                attachment is extracted. See ``EmailClient.download_attachment``.

        Returns:
            AttachmentDownloadResponse with download result information.
        """
        result = await self.incoming_client.download_attachment(
            email_id,
            attachment_name,
            save_path,
            mailbox,
            allowed_senders=allowed_senders,
        )
        return AttachmentDownloadResponse(
            email_id=result["email_id"],
            attachment_name=result["attachment_name"],
            mime_type=result["mime_type"],
            size=result["size"],
            saved_path=result["saved_path"],
        )
