from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class InlineAttachment(BaseModel):
    """An attachment shipped to send_email / save_to_mailbox as inline bytes.

    Used by remote MCP clients that cannot put a file on the server's
    filesystem. The receiving server decodes ``content_base64`` and assembles
    a MIME part using ``filename`` and ``mime_type``. Filename is sanitized
    server-side; ``mime_type`` is auto-detected from the filename extension
    if omitted.
    """

    filename: str = Field(description="Display filename, e.g. 'report.pdf'. Sanitized server-side.")
    content_base64: str = Field(description="Standard base64-encoded file bytes. No whitespace, no 'data:' URI prefix.")
    mime_type: str | None = Field(
        default=None,
        description=(
            "Override MIME type as 'type/subtype'. If omitted, auto-detected from filename. "
            "Must be a valid RFC 2045 token (e.g. 'image/png'); parameters are not supported."
        ),
    )


class EmailMetadata(BaseModel):
    """Email metadata"""

    email_id: str
    message_id: str | None = None  # RFC 5322 Message-ID header for reply threading
    subject: str
    sender: str
    recipients: list[str]  # Recipient list
    date: datetime
    attachments: list[str]

    @classmethod
    def from_email(cls, email: dict[str, Any]):
        return cls(
            email_id=email["email_id"],
            message_id=email.get("message_id"),
            subject=email["subject"],
            sender=email["from"],
            recipients=email.get("to", []),
            date=email["date"],
            attachments=email["attachments"],
        )


class EmailMetadataPageResponse(BaseModel):
    """Paged email metadata response"""

    page: int
    page_size: int
    before: datetime | None
    since: datetime | None
    subject: str | None
    emails: list[EmailMetadata]
    total: int


class EmailBodyResponse(EmailMetadata):
    """Single email body response - extends EmailMetadata with body content"""

    body: str


class EmailContentBatchResponse(BaseModel):
    """Batch email content response for multiple emails"""

    emails: list[EmailBodyResponse]
    requested_count: int
    retrieved_count: int
    failed_ids: list[str]


class MailboxInfo(BaseModel):
    """IMAP mailbox/folder information"""

    name: str
    delimiter: str
    flags: list[str]


class EmailMarkResponse(BaseModel):
    """Response for mark_emails (read/unread) operations"""

    success: bool
    marked_ids: list[str]
    failed_ids: list[str]
    mailbox: str
    marked_as: str  # "read" or "unread"


class AttachmentDownloadResponse(BaseModel):
    """Attachment download response"""

    email_id: str
    attachment_name: str
    mime_type: str
    size: int
    saved_path: str
