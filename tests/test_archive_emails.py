"""Tests for archive_emails — homelab convenience wrapper over move_emails."""

from unittest.mock import AsyncMock, patch

import pytest

from mcp_email_server.app import archive_emails


class TestArchiveEmails:
    @pytest.mark.asyncio
    async def test_archive_default_destination(self):
        """Default destination is 'Archive' folder."""
        mock_handler = AsyncMock()
        mock_handler.move_emails.return_value = (["1", "2"], [])

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await archive_emails(account_name="test", email_ids=["1", "2"])

        assert result == "Successfully archived 2 email(s) to Archive"
        mock_handler.move_emails.assert_called_once_with(["1", "2"], "INBOX", "Archive")

    @pytest.mark.asyncio
    async def test_archive_custom_source_and_destination(self):
        """Both source and archive folder can be overridden."""
        mock_handler = AsyncMock()
        mock_handler.move_emails.return_value = (["42"], [])

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await archive_emails(
                account_name="test",
                email_ids=["42"],
                source_mailbox="Sent",
                archive_mailbox="[Gmail]/All Mail",
            )

        assert result == "Successfully archived 1 email(s) to [Gmail]/All Mail"
        mock_handler.move_emails.assert_called_once_with(["42"], "Sent", "[Gmail]/All Mail")

    @pytest.mark.asyncio
    async def test_archive_partial_failure_reported(self):
        """Failed IDs surface in the response string."""
        mock_handler = AsyncMock()
        mock_handler.move_emails.return_value = (["1"], ["2", "3"])

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            result = await archive_emails(account_name="test", email_ids=["1", "2", "3"])

        assert "Successfully archived 1 email(s) to Archive" in result
        assert "failed to archive 2 email(s): 2, 3" in result

    @pytest.mark.asyncio
    async def test_archive_delegates_no_extra_imap_logic(self):
        """archive_emails must be a thin wrapper — never call anything other than move_emails."""
        mock_handler = AsyncMock()
        mock_handler.move_emails.return_value = ([], [])

        with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
            await archive_emails(account_name="test", email_ids=[])

        mock_handler.move_emails.assert_called_once()
        # Anything else on the handler being called would mean the wrapper grew accidental logic.
        # Walk all method invocations and assert only move_emails was touched.
        called_methods = {
            name for name, attr in mock_handler._mock_children.items() if attr.called and name != "move_emails"
        }
        assert called_methods == set(), f"Wrapper unexpectedly called: {called_methods}"
