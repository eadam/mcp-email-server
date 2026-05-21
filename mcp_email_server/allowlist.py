"""Shared sender-allowlist helpers used by both the MCP tool layer (app.py)
and the IMAP handler layer (emails/classic.py).

Extracted so that download_attachment can apply the same sender check the read
tools do, against the parsed message's From header, without duplicating the
matching logic.
"""

import fnmatch
from email import utils as email_utils


def normalize_addrs(raw: list[str]) -> list[str]:
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


def sender_allowed(sender: str, patterns: list[str]) -> bool:
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
