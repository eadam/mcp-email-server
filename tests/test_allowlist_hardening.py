"""Homelab-only hardening tests for the recipient/sender allowlists.

Companion to upstream PR #144 (zalez/combined-allowlists). These tests cover the
hardening commit that lives only on the `homelab` branch:

- Display-name normalization (closes the "Eve <bad@evil>" bypass on send).
- Fail-closed mode via MCP_EMAIL_SERVER_ALLOWLIST_REQUIRED.
- Structured logger.warning emission on every block.
- Regression guard: save_to_mailbox is intentionally exempt from the allowlist
  in every mode (drafts are the human-review gate; see MAINTENANCE-HANDBOOK §2.1).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.app import (
    _normalize_addrs,
    get_emails_content,
    list_emails_metadata,
    save_to_mailbox,
    send_email,
)
from mcp_email_server.emails.models import (
    EmailBodyResponse,
    EmailContentBatchResponse,
    EmailMetadata,
    EmailMetadataPageResponse,
)


class TestNormalizeAddrs:
    def test_strips_display_name(self):
        assert _normalize_addrs(["Foo <bad@evil.example>"]) == ["bad@evil.example"]

    def test_lowercases(self):
        assert _normalize_addrs(["Alice@EXAMPLE.COM"]) == ["alice@example.com"]

    def test_handles_multiple(self):
        assert _normalize_addrs(["Alice <a@x.com>", "b@y.com"]) == ["a@x.com", "b@y.com"]

    def test_drops_empty(self):
        assert _normalize_addrs([""]) == []


class TestSendEmailDisplayNameBypass:
    @pytest.mark.asyncio
    async def test_blocks_unlisted_addr_inside_display_name(self):
        """`'Eve <bad@evil.example>'` must be checked as `bad@evil.example`, not the raw string."""
        mock_settings = MagicMock()
        mock_settings.allowed_recipients = ["alice@example.com"]
        mock_settings.allowlist_required = False

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with pytest.raises(ValueError) as exc_info:
                await send_email(
                    account_name="test",
                    recipients=["Eve <eve@evil.example>"],
                    subject="hi",
                    body="payload",
                )

        assert "eve@evil.example" in str(exc_info.value)
        assert "not in allowlist" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_allows_listed_addr_inside_display_name(self):
        """`'Alice <alice@example.com>'` must succeed when alice@example.com is allowlisted."""
        mock_settings = MagicMock()
        mock_settings.allowed_recipients = ["alice@example.com"]
        mock_settings.allowlist_required = False
        mock_handler = AsyncMock()

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await send_email(
                    account_name="test",
                    recipients=["Alice <alice@example.com>"],
                    subject="hi",
                    body="ok",
                )

        assert "sent successfully" in result
        mock_handler.send_email.assert_called_once()


class TestRequiredMode:
    @pytest.mark.asyncio
    async def test_send_email_blocks_when_required_and_recipients_unset(self):
        mock_settings = MagicMock()
        mock_settings.allowed_recipients = []
        mock_settings.allowlist_required = True

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with pytest.raises(ValueError) as exc_info:
                await send_email(
                    account_name="test",
                    recipients=["alice@example.com"],
                    subject="hi",
                    body="x",
                )

        assert "required" in str(exc_info.value).lower()
        assert "allowed_recipients" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_list_emails_metadata_blocks_when_required_and_senders_unset(self):
        mock_settings = MagicMock()
        mock_settings.allowed_senders = []
        mock_settings.allowlist_required = True

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with pytest.raises(ValueError) as exc_info:
                await list_emails_metadata(account_name="test")

        assert "required" in str(exc_info.value).lower()
        assert "allowed_senders" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_emails_content_blocks_when_required_and_senders_unset(self):
        mock_settings = MagicMock()
        mock_settings.allowed_senders = []
        mock_settings.allowlist_required = True

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with pytest.raises(ValueError) as exc_info:
                await get_emails_content(account_name="test", email_ids=["1"])

        assert "required" in str(exc_info.value).lower()


class TestStructuredLogging:
    @pytest.mark.asyncio
    async def test_logger_warning_on_send_block(self):
        """Each blocked recipient produces a warning with the addr field."""
        mock_settings = MagicMock()
        mock_settings.allowed_recipients = ["alice@example.com"]
        mock_settings.allowlist_required = False

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.logger.warning") as mock_warn:
                with pytest.raises(ValueError):
                    await send_email(
                        account_name="test",
                        recipients=["bob@example.com"],
                        subject="hi",
                        body="x",
                    )

        # Should have at least one block-warning call carrying the offending address
        block_calls = [c for c in mock_warn.call_args_list if "allowlist_block" in str(c)]
        assert block_calls, f"expected an allowlist_block warning, got {mock_warn.call_args_list}"
        assert any("bob@example.com" in str(c) for c in block_calls)

    @pytest.mark.asyncio
    async def test_logger_warning_on_read_filter(self):
        """Each filtered email in list_emails_metadata produces a warning."""
        now = datetime.now(timezone.utc)
        mock_settings = MagicMock()
        mock_settings.allowed_senders = ["*@trusted.example"]
        mock_settings.allowlist_required = False
        page = EmailMetadataPageResponse(
            page=1,
            page_size=10,
            before=None,
            since=None,
            subject=None,
            emails=[
                EmailMetadata(
                    email_id="1",
                    subject="Spam",
                    sender="spam@evil.example",
                    recipients=[],
                    date=now,
                    attachments=[],
                ),
            ],
            total=1,
        )
        mock_handler = AsyncMock()
        mock_handler.get_emails_metadata.return_value = page

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                with patch("mcp_email_server.app.logger.warning") as mock_warn:
                    result = await list_emails_metadata(account_name="test")

        assert len(result.emails) == 0
        block_calls = [c for c in mock_warn.call_args_list if "allowlist_block" in str(c)]
        assert block_calls
        assert any("spam@evil.example" in str(c) for c in block_calls)


class TestSaveToMailboxNotAllowlisted:
    """Regression guard: save_to_mailbox MUST NOT check the allowlist.

    Drafts are the human-review gate. See MAINTENANCE-HANDBOOK.md §2.1.
    If this test starts failing because save_to_mailbox now consults the allowlist,
    that is a behavioral regression — not a fix. Revert the offending change or
    update the handbook decision (with explicit human approval).
    """

    @pytest.mark.asyncio
    async def test_save_to_mailbox_succeeds_with_unlisted_recipient(self):
        mock_settings = MagicMock()
        mock_settings.allowed_recipients = ["alice@example.com"]
        mock_settings.allowlist_required = False
        mock_handler = AsyncMock()
        mock_handler.save_to_mailbox.return_value = "<msgid>|uid:42"

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await save_to_mailbox(
                    account_name="test",
                    recipients=["new-contact@example.org"],
                    subject="draft",
                    body="hello",
                )

        assert "saved to 'Drafts' successfully" in result
        mock_handler.save_to_mailbox.assert_called_once()

    @pytest.mark.asyncio
    async def test_save_to_mailbox_succeeds_in_required_mode(self):
        """Even with allowlist_required=True and an empty allowlist, drafts succeed."""
        mock_settings = MagicMock()
        mock_settings.allowed_recipients = []
        mock_settings.allowlist_required = True
        mock_handler = AsyncMock()
        mock_handler.save_to_mailbox.return_value = "<msgid>|uid:99"

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await save_to_mailbox(
                    account_name="test",
                    recipients=["anyone@anywhere.example"],
                    subject="draft",
                    body="hello",
                )

        assert "saved to 'Drafts' successfully" in result


# Silence ruff F401 for fixtures imported for side-effect.
_ = (EmailBodyResponse, EmailContentBatchResponse)
