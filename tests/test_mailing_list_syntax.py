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
    monkeypatch.setattr(ec, "_today", lambda: "2026-07-23")
    p = ec._state_path("c", "sent")
    with open(p, "w", encoding="utf-8") as f:
        f.write("old@example.com\n")               # legacy, undated
        f.write("2026-07-22\tnew@example.com\n")   # dated
    assert ec.load_sent("c") == {"old@example.com", "new@example.com"}
    assert ec.sent_on("c", "2026-07-22") == 1
    # One prior day (the 22nd); today is the 23rd and has sent nothing yet.
    assert ec.campaign_days("c") == 1
    assert ec.sent_on("c") == 0


def test_daily_cap_trims_the_run(tmp_path, monkeypatch):
    import integrations.channels.email_campaign as ec
    monkeypatch.setattr(ec, "_STATE_DIR", str(tmp_path))
    r = ec.send_campaign(["a%d@example.com" % i for i in range(50)],
                         "s", "<p>h</p>", "t", campaign="capped",
                         dry_run=True, daily_cap=10)
    assert r["candidates"] == 10, r
    assert r["daily_cap"] == 10


def test_recipient_filtering_is_not_quadratic(tmp_path, monkeypatch):
    """Membership was tested against a set rebuilt from `todo` on every
    recipient. Correct, and quadratic. At 77k recipients the call never
    reached the send loop and looked like a hang rather than a bug."""
    import time
    import integrations.channels.email_campaign as ec
    monkeypatch.setattr(ec, "_STATE_DIR", str(tmp_path))
    addrs = ["u%d@example.com" % i for i in range(60000)]
    t0 = time.time()
    r = ec.send_campaign(addrs, "s", "<p>h</p>", "t",
                         campaign="perf", dry_run=True, daily_cap=10)
    assert time.time() - t0 < 10, "filtering 60k recipients should be near-instant"
    assert r["candidates"] == 10


def test_duplicate_recipients_still_collapse(tmp_path, monkeypatch):
    """The speedup must not lose the dedupe it replaced."""
    import integrations.channels.email_campaign as ec
    monkeypatch.setattr(ec, "_STATE_DIR", str(tmp_path))
    r = ec.send_campaign(["a@x.com", "A@X.com", " a@x.com ", "b@x.com"],
                         "s", "<p>h</p>", "t", campaign="dupes", dry_run=True)
    assert r["candidates"] == 2, r


def test_campaign_days_excludes_today(tmp_path, monkeypatch):
    """A campaign that already sent this morning is still on the same day.
    Counting today as elapsed steps the ramp up early on every resume."""
    import integrations.channels.email_campaign as ec
    monkeypatch.setattr(ec, "_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(ec, "_today", lambda: "2026-07-22")
    p = ec._state_path("ramp", "sent")
    with open(p, "w", encoding="utf-8") as f:
        f.write("2026-07-20\ta@x.com\n")
        f.write("2026-07-21\tb@x.com\n")
        f.write("2026-07-22\tc@x.com\n")   # today, in progress
    assert ec.campaign_days("ramp") == 2          # not 3
    assert ec.warmup_cap(ec.campaign_days("ramp")) == 2500
    # A campaign whose only activity is today is still on day one.
    p2 = ec._state_path("fresh", "sent")
    with open(p2, "w", encoding="utf-8") as f:
        f.write("2026-07-22\tc@x.com\n")
    assert ec.campaign_days("fresh") == 0
    assert ec.warmup_cap(ec.campaign_days("fresh")) == 500


def _dsn(recipient, status="5.1.1"):
    """A Postfix-shaped bounce: the delivery-status part is NOT text, its
    payload is a list of header-only Message objects."""
    import email
    return email.message_from_string(
        "From: MAILER-DAEMON@mail.hertzai.com\n"
        "Subject: Undelivered Mail Returned to Sender\n"
        "Content-Type: multipart/report; report-type=delivery-status;"
        ' boundary="B"\n\n'
        "--B\nContent-Type: text/plain\n\nThis is the mail system.\n\n"
        "--B\nContent-Type: message/delivery-status\n\n"
        "Reporting-MTA: dns; mail.hertzai.com\n\n"
        "Final-Recipient: rfc822; %s\nAction: failed\nStatus: %s\n\n"
        "--B--\n" % (recipient, status))


def test_parses_postfix_delivery_status():
    """get_payload(decode=True) returns None for message/delivery-status and
    str() of its payload gives a Python repr, so a naive walk files every
    real bounce as unreadable. 262 of 275 were missed exactly this way."""
    from integrations.channels.bounce_handler import classify_message
    kind, addr, code, _ = classify_message(_dsn("dead@example.com"))
    assert (kind, addr, code) == ("hard", "dead@example.com", "5.1.1")


def test_soft_bounce_is_not_suppressed():
    """A full mailbox or a greylist is not a reason to drop someone forever."""
    from integrations.channels.bounce_handler import classify_message
    kind, addr, code, _ = classify_message(_dsn("busy@example.com", "4.2.2"))
    assert kind == "soft" and code == "4.2.2"


def test_ordinary_mail_is_not_treated_as_a_bounce():
    import email
    from integrations.channels.bounce_handler import classify_message
    m = email.message_from_string(
        "From: a-real-person@example.com\nSubject: thanks for this\n"
        "Content-Type: text/plain\n\nLooks great, will try it.\n")
    assert classify_message(m)[0] == "other"


def test_unsubscribe_reply_is_recognised():
    import email
    from integrations.channels.bounce_handler import classify_message
    m = email.message_from_string(
        "From: Someone <someone@example.com>\nSubject: Re: your email\n"
        "Content-Type: text/plain\n\nunsubscribe\n")
    kind, addr, _, _ = classify_message(m)
    assert kind == "unsubscribe" and addr == "someone@example.com"


def test_bounced_addresses_are_excluded_from_sends(tmp_path, monkeypatch):
    import integrations.channels.email_campaign as ec
    monkeypatch.setattr(ec, "_STATE_DIR", str(tmp_path))
    ec.record_bounce("dead@example.com", "5.1.1", "user unknown")
    assert "dead@example.com" in ec.load_bounced()
    r = ec.send_campaign(["dead@example.com", "live@example.com"],
                         "s", "<p>h</p>", "t", campaign="supp", dry_run=True)
    assert r["candidates"] == 1, r
    assert r["suppressed_bounced"] == 1


def test_accept_all_providers_return_none_not_true():
    """The finding this encodes, measured against mailboxes those providers
    had ALREADY hard-bounced:

        gmail.com    550 5.1.1 does not exist   -> truthful
        hotmail.com  250 Recipient OK           -> accepts all
        yahoo.com    250 recipient ok           -> accepts all
        aol.com      250 recipient ok           -> accepts all

    Reporting Microsoft/Yahoo/AOL's 250 as 'exists' would mark every dead
    address on 52% of the list as good. That is worse than not checking,
    so those providers must return None (unknown), never True.
    """
    from integrations.channels.mailing_list import (
        mailbox_exists, VERIFIABLE_PROVIDERS)
    for dom in ("hotmail.com", "yahoo.com", "aol.com", "outlook.com"):
        exists, why = mailbox_exists("someone@" + dom)
        assert exists is None, "%s must be unknown, got %r" % (dom, exists)
        assert "not verifiable" in why
        assert dom not in VERIFIABLE_PROVIDERS


def test_gmail_is_the_verifiable_one():
    from integrations.channels.mailing_list import VERIFIABLE_PROVIDERS
    assert "gmail.com" in VERIFIABLE_PROVIDERS
    assert "googlemail.com" in VERIFIABLE_PROVIDERS


def test_probe_maps_smtp_codes_correctly():
    """4xx is a deferral, not an answer. Treating it as 'dead' would drop
    real people because a server was busy."""
    from integrations.channels import mailing_list as ml

    class FakeSMTP:
        def __init__(self, code):
            self.code = code
        def rcpt(self, addr):
            return self.code, b"synthetic"

    assert ml.mailbox_exists("a@gmail.com", smtp=FakeSMTP(250))[0] is True
    assert ml.mailbox_exists("a@gmail.com", smtp=FakeSMTP(550))[0] is False
    assert ml.mailbox_exists("a@gmail.com", smtp=FakeSMTP(451))[0] is None


def test_bounce_breaker_halts_a_bad_list(tmp_path, monkeypatch):
    """MAX_CONSECUTIVE_FAILURES cannot catch this. Hotmail, Yahoo and AOL
    accept every recipient at RCPT and reject asynchronously, so for ~52% of
    a consumer list every send 'succeeds' and the consecutive counter never
    moves however dead the addresses are."""
    import integrations.channels.email_campaign as ec
    monkeypatch.setattr(ec, "_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(ec, "_today", lambda: "2026-07-22")
    with open(ec._state_path("c", "sent"), "w", encoding="utf-8") as f:
        for i in range(200):
            f.write("2026-07-22\tu%d@example.com\n" % i)
    for i in range(60):                       # 30% bounce
        ec.record_bounce("u%d@example.com" % i, "5.1.1", "user unknown")

    b = ec.recent_bounce_rate("c")
    assert b["sent"] == 200 and b["bounced"] == 60
    assert abs(b["rate"] - 0.30) < 0.001

    r = ec.send_campaign(["new%d@example.com" % i for i in range(10)],
                         "s", "<p>h</p>", "t", campaign="c", dry_run=True)
    assert "HALTED" in r.get("error", ""), r
    assert r["candidates"] == 0


def test_bounce_breaker_allows_a_healthy_list(tmp_path, monkeypatch):
    import integrations.channels.email_campaign as ec
    monkeypatch.setattr(ec, "_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(ec, "_today", lambda: "2026-07-22")
    with open(ec._state_path("h", "sent"), "w", encoding="utf-8") as f:
        for i in range(200):
            f.write("2026-07-22\tu%d@example.com\n" % i)
    for i in range(4):                        # 2%, under the 5% ceiling
        ec.record_bounce("u%d@example.com" % i, "5.1.1", "user unknown")
    r = ec.send_campaign(["new%d@example.com" % i for i in range(10)],
                         "s", "<p>h</p>", "t", campaign="h", dry_run=True)
    assert "error" not in r, r
    assert r["candidates"] == 10


def test_breaker_needs_a_sample_before_it_fires(tmp_path, monkeypatch):
    """One bounce out of three is 33% and means nothing. Firing on tiny
    samples would halt every campaign on its first bad address."""
    import integrations.channels.email_campaign as ec
    monkeypatch.setattr(ec, "_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(ec, "_today", lambda: "2026-07-22")
    with open(ec._state_path("t", "sent"), "w", encoding="utf-8") as f:
        for i in range(3):
            f.write("2026-07-22\tu%d@example.com\n" % i)
    ec.record_bounce("u0@example.com", "5.1.1", "x")
    r = ec.send_campaign(["new@example.com"], "s", "<p>h</p>", "t",
                         campaign="t", dry_run=True)
    assert "error" not in r, r


def test_old_sends_age_out_of_the_window(tmp_path, monkeypatch):
    """The rate must reflect the current list, not a bad batch from a month
    ago that was already dealt with."""
    import integrations.channels.email_campaign as ec
    monkeypatch.setattr(ec, "_STATE_DIR", str(tmp_path))
    with open(ec._state_path("a", "sent"), "w", encoding="utf-8") as f:
        f.write("2020-01-01\tancient@example.com\n")
    ec.record_bounce("ancient@example.com", "5.1.1", "x")
    b = ec.recent_bounce_rate("a", days=3)
    assert b["sent"] == 0 and b["rate"] == 0.0
