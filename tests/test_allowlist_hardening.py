"""Homelab-only hardening tests for the recipient/sender allowlists.

Upstream already normalizes display-name and packed multi-address recipient
forms (see test_mcp_tools.py) — those cases are intentionally not duplicated
here. This module covers the fork-only behaviors:

- Fail-closed required mode via MCP_EMAIL_SERVER_ALLOWLIST_REQUIRED: when True
  and the relevant allowlist is empty, send_email (recipients) and
  list_emails_metadata / get_emails_content / download_attachment (senders)
  refuse to operate instead of allowing everything.
- Structured ``allowlist_block`` logger.warning emission on blocks, greppable
  from container logs.
- The drafts exemption: save_to_mailbox never consults the recipient
  allowlist, in any mode (deliberate divergence from upstream).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server.app import (
    download_attachment,
    get_emails_content,
    list_emails_metadata,
    save_to_mailbox,
    send_email,
)


def _required_mode_settings(**overrides):
    """MagicMock settings with real (non-Mock) values for the fields the guards read."""
    settings = MagicMock()
    settings.allowlist_required = True
    settings.allowed_recipients = []
    settings.allowed_senders = []
    settings.enable_attachment_download = True
    settings.max_inline_download_bytes = 20 * 1024 * 1024
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


class TestRequiredMode:
    @pytest.mark.asyncio
    async def test_send_email_blocks_when_required_and_recipients_unset(self):
        with patch("mcp_email_server.app.get_settings", return_value=_required_mode_settings()):
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
    async def test_send_email_proceeds_when_required_and_recipient_listed(self):
        mock_settings = _required_mode_settings(allowed_recipients=["alice@example.com"])
        mock_handler = AsyncMock()
        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await send_email(
                    account_name="test",
                    recipients=["alice@example.com"],
                    subject="hi",
                    body="ok",
                )
        assert "sent successfully" in result
        mock_handler.send_email.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_emails_metadata_blocks_when_required_and_senders_unset(self):
        with patch("mcp_email_server.app.get_settings", return_value=_required_mode_settings()):
            with pytest.raises(ValueError) as exc_info:
                await list_emails_metadata(account_name="test")

        assert "required" in str(exc_info.value).lower()
        assert "allowed_senders" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_emails_content_blocks_when_required_and_senders_unset(self):
        with patch("mcp_email_server.app.get_settings", return_value=_required_mode_settings()):
            with pytest.raises(ValueError) as exc_info:
                await get_emails_content(account_name="test", email_ids=["1"])

        assert "required" in str(exc_info.value).lower()
        assert "allowed_senders" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_download_attachment_blocks_when_required_and_senders_unset(self):
        """The fail-closed pre-check runs before any IMAP work, in both modes."""
        with patch("mcp_email_server.app.get_settings", return_value=_required_mode_settings()):
            with patch("mcp_email_server.app.dispatch_handler") as mock_dispatch:
                with pytest.raises(ValueError, match="Sender allowlist is required"):
                    await download_attachment(
                        account_name="test",
                        email_id="1",
                        attachment_name="doc.pdf",
                        inline=True,
                    )
                mock_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_magicmock_settings_do_not_trip_required_mode(self):
        """The guard requires a literal True — an unconfigured MagicMock attribute
        (truthy, but not True) must not flip the server into fail-closed mode."""
        mock_settings = MagicMock()
        mock_settings.allowed_recipients = []
        # NOTE: allowlist_required deliberately left as a bare MagicMock attribute.
        mock_handler = AsyncMock()
        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await send_email(
                    account_name="test",
                    recipients=["anyone@example.com"],
                    subject="S",
                    body="B",
                )
        assert "sent successfully" in result


class TestStructuredLogging:
    @pytest.mark.asyncio
    async def test_logger_warning_on_send_block(self):
        """Each blocked recipient produces an allowlist_block warning with the addr field."""
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

        block_calls = [c for c in mock_warn.call_args_list if "allowlist_block" in str(c)]
        assert block_calls, f"expected an allowlist_block warning, got {mock_warn.call_args_list}"
        assert any("bob@example.com" in str(c) for c in block_calls)

    @pytest.mark.asyncio
    async def test_logger_warning_on_required_mode_block(self):
        with patch("mcp_email_server.app.get_settings", return_value=_required_mode_settings()):
            with patch("mcp_email_server.app.logger.warning") as mock_warn:
                with pytest.raises(ValueError):
                    await list_emails_metadata(account_name="test")

        block_calls = [c for c in mock_warn.call_args_list if "allowlist_block" in str(c)]
        assert block_calls
        assert any("required-mode-empty-allowlist" in str(c) for c in block_calls)

    @pytest.mark.asyncio
    async def test_logger_warning_on_attachment_download_sender_block(self, email_server):
        """A blocked attachment download emits allowlist_block kind=attachment_download.

        Logged at the enforcement point in the email client (not the tool layer):
        upstream's design makes a blocked UID indistinguishable from a missing one
        by the time the error reaches the tool, so the client layer is the only
        place the block is definitively known.
        """
        import asyncio

        from mcp_email_server.emails.classic import EmailClient

        email_client = EmailClient(email_server)
        mock_imap = AsyncMock()
        mock_imap._client_task = asyncio.Future()
        mock_imap._client_task.set_result(None)
        mock_imap.wait_hello_from_server = AsyncMock()
        mock_imap.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
        mock_imap.select = AsyncMock(return_value=("OK", [b"1"]))
        mock_imap.logout = AsyncMock()

        with (
            patch.object(email_client, "_batch_fetch_senders", AsyncMock(return_value={"1": "evil@blocked.com"})),
            patch.object(email_client, "imap_class", return_value=mock_imap),
            patch("mcp_email_server.emails.classic.logger.warning") as mock_warn,
        ):
            with pytest.raises(ValueError):
                await email_client.fetch_attachment_inline(
                    email_id="1",
                    attachment_name="doc.pdf",
                    allowed_senders=["*@allowed.com"],
                    max_bytes=1024,
                )

        block_calls = [c for c in mock_warn.call_args_list if "allowlist_block" in str(c)]
        assert block_calls
        assert any("kind=attachment_download" in str(c) for c in block_calls)


class TestSaveToMailboxNotAllowlisted:
    """Regression guard: save_to_mailbox MUST NOT check the recipient allowlist.

    Drafts are the human-review gate: saving a draft transmits nothing, and
    gating drafts would only block the compose-review-send workflow for new
    contacts. This is a deliberate divergence from upstream (which enforces
    the allowlist on save_to_mailbox). If this test starts failing because
    save_to_mailbox now consults the allowlist, that is a behavioral
    regression — not a fix. Revert the offending change or update the
    documented decision (with explicit human approval).
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
        mock_handler = AsyncMock()
        mock_handler.save_to_mailbox.return_value = "<msgid>|uid:99"

        with patch("mcp_email_server.app.get_settings", return_value=_required_mode_settings()):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await save_to_mailbox(
                    account_name="test",
                    recipients=["anyone@anywhere.example"],
                    subject="draft",
                    body="hello",
                )

        assert "saved to 'Drafts' successfully" in result
        mock_handler.save_to_mailbox.assert_called_once()
