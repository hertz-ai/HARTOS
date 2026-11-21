"""One rule finds a fill-in mark left in text a person reads (audit H9).

The outreach email check (outreach_crm_tools.verify_outgoing_email) kept its
own pattern for a word in braces ({POSTAL ADDRESS}, {{first_name}}), beside
the rule every message and ask goes through
(core.agent_tools.unfilled_text_placeholder), and the two disagreed: an
email holding "<first_name>" or "[COMPANY_NAME]" left, which a chat message
never does.  Now the email check asks the one rule, with the brace shape as
the email's own mode:
  * every email the old pattern refused is refused with the same words and
    names the same blank (the table below was written against the old
    pattern and passes on it);
  * every email the old pattern let through that holds no mark of the one
    rule still leaves;
  * an email holding a mark of the one rule is now refused;
  * a message or an ask is read exactly as before: braces are never a blank
    there.

The real verify_outgoing_email and unfilled_text_placeholder; only the
node's mailbox list is stubbed (no channel registry in this test).
"""
import pytest

from core import agent_tools
from integrations.agent_engine import outreach_crm_tools as crm

TO = 'ana@clinic.example'

#: (subject, body, the blank the email check names) as the pattern the email
#: check had refused them.
REFUSED_BEFORE = [
    ('hello', '<p>hello</p><p>{POSTAL ADDRESS}</p>', '{POSTAL ADDRESS}'),
    ('hello', '<p>Hi {{first_name}},</p>', '{{first_name}}'),
    ('hello', '<p>about {product_keyword}</p>', '{product_keyword}'),
    ('Use {SAVE10} today', '<p>a code</p>', '{SAVE10}'),
    ('hello', '<p>Hi { name },</p>', '{ name }'),
    ('hello', '<p>Hi {{ name }</p>', '{{ name }'),
    ('{company} news', '<p>{first_name}</p>', '{company}'),
    ('hello', '<p>{_x}</p>', '{_x}'),
    # The brace blank is the one named even when another mark comes first.
    ('hello', '<p>Hi <first_name>, your code is {CODE}</p>', '{CODE}'),
]

#: Emails the old pattern let through, with no mark of the one rule: they
#: still leave.
SENT_BEFORE_AND_NOW = [
    ('hello', '<p style="color: red">hi</p>'),
    ('hello', '<style>p {color: red}</style><p>hi</p>'),
    ('hello', '<style>p { margin: 0; }</style><p>hi</p>'),
    ('hello', '<p>Hi [First Name],</p>'),
    ('hello', '<p>Hi %FIRSTNAME%,</p>'),
    ('hello', '<p>the set {1, 2} and {}</p>'),
    ('hello', '<p>3 &lt; 5</p><br><a href="https://x.example/a_b">here</a>'),
    ('hello', '<p>see [the_page](https://x.example)</p>'),
    ('hello', '<div><table><tr><td>plain</td></tr></table></div>'),
    ('', '<p>a body and no subject</p>'),
    (None, None),
]

#: Emails the old pattern let through that hold a mark of the one rule.
REFUSED_NOW = [
    ('hello', '<p>Hi <first_name>,</p>', '<first_name>'),
    ('hello', '<p>Dear [COMPANY_NAME] team</p>', '[COMPANY_NAME]'),
    ('About [REVENUE_AMOUNT]', '<p>hi</p>', '[REVENUE_AMOUNT]'),
]


@pytest.fixture(autouse=True)
def no_mailboxes(monkeypatch):
    monkeypatch.setattr(crm, '_goal_mailboxes', lambda: {})


@pytest.mark.parametrize('subject, body, blank', REFUSED_BEFORE)
def test_an_email_with_a_brace_blank_is_refused_in_the_same_words(subject, body, blank):
    out = crm.verify_outgoing_email(TO, subject, body)

    assert out == {
        'success': False, 'via': 'verification',
        'error': f'the email still holds the template blank {blank}',
        'fix': (f'Fill in or remove {blank} in the subject or body, then '
                f'send it again. An email with a blank in it never leaves.')}


@pytest.mark.parametrize('subject, body', SENT_BEFORE_AND_NOW)
def test_an_email_with_no_blank_still_leaves(subject, body):
    assert crm.verify_outgoing_email(TO, subject, body) is None


@pytest.mark.parametrize('subject, body, blank', REFUSED_NOW)
def test_an_email_with_a_mark_a_message_would_be_refused_for_never_leaves(
        subject, body, blank):
    out = crm.verify_outgoing_email(TO, subject, body)

    assert out is not None, 'the email left with a fill-in mark in it'
    assert out['error'] == f'the email still holds the template blank {blank}'
    assert out['via'] == 'verification' and blank in out['fix']


def test_the_email_check_asks_the_one_rule(monkeypatch):
    asked = []
    real = agent_tools.unfilled_text_placeholder

    def recording(text, *a, **k):
        asked.append(text)
        return real(text, *a, **k)
    monkeypatch.setattr(agent_tools, 'unfilled_text_placeholder', recording)

    crm.verify_outgoing_email(TO, 'a subject', '<p>a body</p>')

    assert asked == ['a subject\n<p>a body</p>']
    assert not hasattr(crm, '_TEMPLATE_BLANK'), 'the email check keeps a pattern of its own'


@pytest.mark.parametrize('text', [
    'In an f-string write {name} where the value goes.',
    'The set {x} has one member.',
    'Use the code {SAVE10} at checkout.',
    'Hi {{first_name}}, this is how a template looks.',
    '{POSTAL ADDRESS}',
])
def test_braces_in_a_message_or_an_ask_are_not_a_blank(text):
    assert agent_tools.unfilled_text_placeholder(text) is None


@pytest.mark.parametrize('text, mark', [
    ('Thank you, <parent_name>!', '<parent_name>'),
    ('Revenue was [REVENUE_AMOUNT].', '[REVENUE_AMOUNT]'),
    ('<why you ask>', '<why you ask>'),
    ('3 < 5 and 7 > 2', None),
    ('see [click_here](https://x.example)', None),
    ('my_list[item_index] is code', None),
])
def test_a_message_is_read_as_before(text, mark):
    assert agent_tools.unfilled_text_placeholder(text) == mark
