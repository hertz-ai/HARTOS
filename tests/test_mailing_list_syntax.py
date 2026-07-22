"""Regression tests for address validation and campaign link tagging.

Written after a defect that cost a whole consolidation run. The syntax
pattern was a negated character class:

    ^[^@\\s,;<>()\\[\\]\\\\]+@[A-Za-z0-9][A-Za-z0-9.-]*\\.[A-Za-z]{2,}$

which reads as "one or more characters that are not @ or whitespace or
punctuation, then @, then a domain". It is not what Python compiled. The
trailing ``\\\\]`` did not close the set, so the class ran on and swallowed
the ``+``, the ``@`` and the following ``[A-Za-z0-9]`` group, closing only at
THAT bracket. The pattern silently became "one non-alphanumeric character
followed by dot-something", which can never match an ``@`` because ``@`` sits
inside the negated set.

Consequences: classify() returned 'bad_syntax' for every real address, and
the matching extraction pattern harvested fragments like '.e.ajaj' out of
'n.e.ajaj@hotmail.com' instead of the address. Nothing raised. The counts
looked plausible, which is why it survived review.

The lesson these tests encode: a validator must be shown to ACCEPT good
input, not merely to reject bad input. A reject-everything validator passes
every test that only feeds it garbage.
"""
import re

import pytest

from integrations.channels.mailing_list import _SYNTAX, classify, normalise

VALID = [
    "n.e.ajaj@hotmail.com",
    "teslim2010@hotmail.com",
    "mr-j@hotmail.com.tw",
    "sathish@hertzai.com",
    "bsathish.in@gmail.com",
    "a.b+news@gmail.com",
    "abedelaziz_gw@hotmail.com",
    "user%test@example.co.uk",
]

INVALID = [
    "notanemail",
    "a@b",                  # no dot in domain
    "x y@z.com",            # space
    "@nolocal.com",
    "two@@at.com",
    "trailing@dot.",
    "",
]


@pytest.mark.parametrize("addr", VALID)
def test_syntax_accepts_real_addresses(addr):
    """The test that would have caught it. The old pattern failed all of these."""
    assert _SYNTAX.match(addr), "%s wrongly rejected" % addr


@pytest.mark.parametrize("addr", INVALID)
def test_syntax_rejects_malformed(addr):
    assert not _SYNTAX.match(addr), "%s wrongly accepted" % addr


def test_syntax_pattern_can_actually_match_an_at_sign():
    """Guards the specific failure mode rather than its symptom.

    The broken pattern could not emit an '@' under any input, because '@' was
    trapped inside a negated class that never closed. Assert the property
    directly so a future rewrite that reintroduces it fails here.
    """
    m = _SYNTAX.match("someone@example.com")
    assert m is not None
    assert "@" in m.group(0)
    assert m.group(0) == "someone@example.com"


def test_no_stray_group_capture():
    """findall() returns groups when a pattern has them, which silently turns
    a list of addresses into a list of fragments."""
    assert _SYNTAX.groups == 0


def test_classify_passes_a_deliverable_address():
    ok, reason = classify("sathish@hertzai.com", check_mx=False)
    assert ok and reason == "ok"


def test_classify_still_catches_the_categories_it_should():
    assert classify("info@hertzai.com", check_mx=False) == (False, "role_account")
    assert classify("x@mailinator.com", check_mx=False) == (False, "disposable_domain")
    assert classify("x@gmial.com", check_mx=False) == (False, "typo_domain")
    assert classify("garbage", check_mx=False) == (False, "bad_syntax")


def test_normalise_collapses_gmail_aliases():
    assert normalise("A.B+news@Gmail.com") == "ab@gmail.com"
    assert normalise("a.b@googlemail.com") == "ab@gmail.com"
    # Dots are significant everywhere else.
    assert normalise("a.b@hertzai.com") == "a.b@hertzai.com"


def test_extraction_pattern_anchors_local_part():
    """The archive files use runs of '-' as separators. A pattern that allows
    a leading '-' glues the separator onto the address:
    '-------rolyclulow@gmail.com'."""
    EMAIL = re.compile(
        r"[A-Za-z0-9_][A-Za-z0-9._%+\-]*@[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,}")
    text = "-------rolyclulow@gmail.com\nn.e.ajaj@hotmail.com\nmr-j@hotmail.com.tw"
    assert EMAIL.findall(text) == [
        "rolyclulow@gmail.com", "n.e.ajaj@hotmail.com", "mr-j@hotmail.com.tw"]


def test_tracking_does_not_duplicate_query_params():
    """The copy hand-writes '/download?ref=email'. Appending blindly produced
    '?ref=email&ref=email&c=...', and a query parser then reads whichever
    duplicate it likes."""
    from integrations.channels.email_campaign import add_tracking
    once = add_tracking("https://hevolve.ai/download?ref=email", "c1", "a@b.com")
    twice = add_tracking(once, "c1", "a@b.com")
    assert once == twice, "add_tracking must be idempotent"
    assert once.count("ref=") == 1
    assert once.count("c=") == 1


def test_tracking_leaves_third_party_links_alone():
    from integrations.channels.email_campaign import add_tracking
    url = "https://github.com/hertz-ai/Nunba"
    assert add_tracking(url, "c1", "a@b.com") == url


def test_warmup_ramps_then_plateaus():
    from integrations.channels.email_campaign import warmup_cap, WARMUP_SCHEDULE
    assert warmup_cap(0) == 500
    assert warmup_cap(1) == 1000
    # Past the end of the schedule it holds at the plateau rather than
    # falling back to unlimited, which is the failure that would matter.
    assert warmup_cap(99) == WARMUP_SCHEDULE[-1]
    assert warmup_cap(-1) == 0
    assert WARMUP_SCHEDULE == sorted(WARMUP_SCHEDULE), "ramp must be monotonic"


def test_sent_log_reads_both_formats(tmp_path, monkeypatch):
    """Rows written before timestamps existed must still count as sent, or a
    resumed campaign re-mails everyone contacted in the first batch."""
    import integrations.channels.email_campaign as ec
    monkeypatch.setattr(ec, "_STATE_DIR", str(tmp_path))
    p = ec._state_path("c", "sent")
    with open(p, "w", encoding="utf-8") as f:
        f.write("old@example.com\n")               # legacy, undated
        f.write("2026-07-22\tnew@example.com\n")   # dated
    assert ec.load_sent("c") == {"old@example.com", "new@example.com"}
    assert ec.sent_on("c", "2026-07-22") == 1
    assert ec.campaign_days("c") == 1


def test_daily_cap_trims_the_run(tmp_path, monkeypatch):
    import integrations.channels.email_campaign as ec
    monkeypatch.setattr(ec, "_STATE_DIR", str(tmp_path))
    r = ec.send_campaign(["a%d@example.com" % i for i in range(50)],
                         "s", "<p>h</p>", "t", campaign="capped",
                         dry_run=True, daily_cap=10)
    assert r["candidates"] == 10, r
    assert r["daily_cap"] == 10
