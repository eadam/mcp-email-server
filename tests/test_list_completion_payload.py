"""Tests for IMAP LIST response parsing.

Regression cover for the phantom mailbox: `list_mailboxes` reported a 22nd
entry `{name: "completed", delimiter: "LIST"}` on iCloud, because the provider
returns its tagged completion text as a data item and the parser read it as a
mailbox line. The payloads below are verbatim captures from
`imap.mail.me.com` taken while diagnosing it.
"""

from mcp_email_server.emails.classic import _parse_list_response

# Verbatim capture: `imap.list('""', '"*"')` against imap.mail.me.com.
# 22 items, of which the last is not a mailbox.
ICLOUD_LIST_CAPTURE = [
    b'() "/" "Archive"',
    b'() "/" "Claude Duet"',
    b'() "/" "Mexico House"',
    b'() "/" "Tempe Sister Cities/Completed Requests"',
    b'() "/" "Tempe Sister Cities/Bug Reports and Alerts"',
    b'() "/" "Bills Paid"',
    b'() "/" "House"',
    b'(\\Sent) "/" "Sent Messages"',
    b'() "/" "Taxes"',
    b'() "/" "Claude Duet/Archive"',
    b'() "/" "Tempe Sister Cities/DMARC"',
    b'() "/" "Junk"',
    b'(\\Noinferiors) "/" "INBOX"',
    b'() "/" "Jobs"',
    b'() "/" "Claude Duet/Action"',
    b'() "/" "Notes"',
    b'() "/" "Claude Duet/Johnny ASU"',
    b'(\\Trash) "/" "Deleted Messages"',
    b'() "/" "Claude Duet/Delete"',
    b'() "/" "Drafts"',
    b'() "/" "Tempe Sister Cities"',
    b"LIST completed (took 2 ms)",
]


class TestParseListResponseCompletionLine:
    """The completion line must not become a mailbox."""

    def test_icloud_capture_yields_only_real_mailboxes(self):
        """The real 22-item response produces 21 mailboxes, not 22."""
        parsed = [m for m in (_parse_list_response(i) for i in ICLOUD_LIST_CAPTURE) if m]
        assert len(parsed) == 21
        assert "completed" not in {m.name for m in parsed}

    def test_completion_line_alone_is_none(self):
        """The verbatim payload is rejected."""
        assert _parse_list_response(b"LIST completed (took 2 ms)") is None

    def test_completion_line_without_timing_is_none(self):
        """Not all providers append the timing suffix."""
        assert _parse_list_response(b"LIST completed") is None

    def test_completion_line_is_case_insensitive(self):
        """Casing is not guaranteed by the protocol."""
        assert _parse_list_response(b"list Completed (took 11 ms)") is None

    def test_lsub_and_xlist_completion_lines_are_none(self):
        """The same shape appears for LSUB and the legacy XLIST."""
        assert _parse_list_response(b"LSUB completed (took 1 ms)") is None
        assert _parse_list_response(b"XLIST completed (took 1 ms)") is None

    def test_unknown_command_completion_is_rejected_by_the_delimiter_check(self):
        """The backstop catches wording the regex does not know about.

        `FOO completed in 3 ms` is not matched by the completion-line regex,
        but "completed" is not a legal one-character hierarchy delimiter, so
        it is still refused rather than becoming a mailbox named "in".
        """
        assert _parse_list_response(b"FOO completed in 3 ms") is None


class TestParseListResponseStillParsesMailboxes:
    """The fix must not drop anything real."""

    def test_plain_mailbox(self):
        mailbox = _parse_list_response(b'() "/" "Archive"')
        assert mailbox is not None
        assert mailbox.name == "Archive"
        assert mailbox.delimiter == "/"
        assert mailbox.flags == []

    def test_mailbox_with_flags(self):
        mailbox = _parse_list_response(b'(\\Sent) "/" "Sent Messages"')
        assert mailbox is not None
        assert mailbox.name == "Sent Messages"
        assert mailbox.flags == ["\\Sent"]

    def test_hierarchical_mailbox_name(self):
        mailbox = _parse_list_response(b'() "/" "Claude Duet/Johnny ASU"')
        assert mailbox is not None
        assert mailbox.name == "Claude Duet/Johnny ASU"

    def test_a_mailbox_actually_named_completed_still_parses(self):
        """The guard keys on the line shape, not on the word "completed".

        A user is entitled to a folder called "completed", and filtering by
        name rather than by shape would silently hide it.
        """
        mailbox = _parse_list_response(b'() "/" "completed"')
        assert mailbox is not None
        assert mailbox.name == "completed"
        assert mailbox.delimiter == "/"

    def test_mailbox_named_list_completed_still_parses(self):
        """Even the exact completion wording is fine inside a quoted name."""
        mailbox = _parse_list_response(b'() "/" "LIST completed"')
        assert mailbox is not None
        assert mailbox.name == "LIST completed"

    def test_nil_delimiter_becomes_empty_string(self):
        """A flat namespace reports NIL, which must not trip the length check."""
        mailbox = _parse_list_response(b'() NIL "INBOX"')
        assert mailbox is not None
        assert mailbox.name == "INBOX"
        assert mailbox.delimiter == ""

    def test_str_input_is_accepted(self):
        """Callers may pass str as well as bytes."""
        mailbox = _parse_list_response('() "/" "Taxes"')
        assert mailbox is not None
        assert mailbox.name == "Taxes"

    def test_empty_and_whitespace_are_none(self):
        assert _parse_list_response(b"") is None
        assert _parse_list_response(b"   ") is None
        assert _parse_list_response("") is None
