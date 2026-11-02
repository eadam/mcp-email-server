"""Tests for UID SEARCH payload normalization.

Regression cover for the iCloud zero-match crash: when a search matched
nothing, `list_emails_metadata` failed with
`invalid literal for int() with base 10: 'SEARCH'` instead of returning an
empty result. The payloads below are verbatim captures from
`imap.mail.me.com` taken while diagnosing it.
"""

import pytest

from mcp_email_server.emails.classic import _normalize_search_uids


class TestNormalizeSearchUids:
    """Tests for _normalize_search_uids function."""

    def test_returns_uids_for_a_normal_payload(self):
        """A plain UID set is returned verbatim as bytes tokens."""
        assert _normalize_search_uids([b"17655 17662 17663 17679 17684"]) == [
            b"17655",
            b"17662",
            b"17663",
            b"17679",
            b"17684",
        ]

    def test_returns_single_uid(self):
        """A one-result search still parses."""
        assert _normalize_search_uids([b"17684"]) == [b"17684"]

    def test_treats_icloud_completion_only_payload_as_empty(self):
        """iCloud omits the untagged `* SEARCH` line when nothing matches."""
        assert _normalize_search_uids([b"SEARCH completed (took 56 ms)"]) == []

    def test_completion_only_payload_is_timing_independent(self):
        """The reported duration varies between responses."""
        assert _normalize_search_uids([b"SEARCH completed (took 22 ms)"]) == []
        assert _normalize_search_uids([b"SEARCH completed (took 1 ms)"]) == []

    def test_completion_only_payload_is_case_insensitive(self):
        """Providers are not required to match upstream's casing."""
        assert _normalize_search_uids([b"search completed (took 3 ms)"]) == []

    def test_treats_rfc_compliant_empty_payload_as_empty(self):
        """A compliant server sends an empty payload instead."""
        assert _normalize_search_uids([b""]) == []

    def test_handles_missing_response(self):
        """A None or empty response list is an empty result, not an error."""
        assert _normalize_search_uids(None) == []
        assert _normalize_search_uids([]) == []

    def test_accepts_str_payloads(self):
        """Some adapters hand back str rather than bytes."""
        assert _normalize_search_uids(["17684 17679"]) == [b"17684", b"17679"]

    def test_rejects_an_unrecognized_non_uid_payload(self):
        """An unexpected payload is an error, not a silent empty result."""
        with pytest.raises(ValueError, match="Unexpected UID SEARCH payload"):
            _normalize_search_uids([b"BOGUS nonsense here"])

    def test_rejects_a_payload_with_one_bad_token(self):
        """A partially numeric payload must not be half-parsed."""
        with pytest.raises(ValueError, match="Unexpected UID SEARCH payload"):
            _normalize_search_uids([b"17684 SEARCH 17679"])

    def test_rejects_zero_and_negative_uids(self):
        """IMAP UIDs are positive integers."""
        with pytest.raises(ValueError, match="Unexpected UID SEARCH payload"):
            _normalize_search_uids([b"0"])
        with pytest.raises(ValueError, match="Unexpected UID SEARCH payload"):
            _normalize_search_uids([b"-1"])

    def test_ignores_trailing_completion_line_elements(self):
        """Only the first response element is the UID set."""
        assert _normalize_search_uids([b"17684 17679", b"UID SEARCH completed (took 4 ms)"]) == [b"17684", b"17679"]
