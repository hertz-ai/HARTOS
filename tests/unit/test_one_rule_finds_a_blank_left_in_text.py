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


# -- an email is read as HTML (review rv_c092 of c092acb4b) ---------------
# The email check fell through to the rule's chat shapes, and an email is
# HTML: emails that left before c092acb4b were refused after it.

#: (subject, body): left before c092acb4b, refused by it, leave again.
SENT_AGAIN = [
    # the whole text read as one blank: a body that is one tag
    ('', '<img src="https://x.example/banner.png">'),
    (None, '<p>'),
    ('', '  <hr>  '),
    ('', '<a href="https://x.example/a">read it here</a>'),
    # the ask hint's words, in a comment nobody reads
    ('Hi', '<!-- your question block --><p>hi</p>'),
    ('Hi', '<!-- why you ask --><p>hi</p>'),
    ('Hi', '<p>hi</p><!--\n<your question?>\n-->'),
    # ... and in the words: a chat shape, not an email's
    ('hello', '<p><why you ask> and more</p>'),
    ('hello', '<p>Q: <your question?></p>'),
    # code brackets in a code block
    ('XML', '<pre><order><item_id>7</item_id></order></pre>'),
    ('Dev tip', '<pre>ids = [user_id]\nreturn [row_count]</pre>'),
    ('Dev tip', '<pre>fn id< T >(x: T) and <K_V></pre>'),
    ('Dev tip', '<p>write <code>[user_id]</code> and <CODE class="x">[row_id]</CODE></p>'),
    ('Dev tip', '<PRE\nclass="py">rows = [row_count]</PRE >'),
    # a comment holding a mark
    ('Hi', '<p>hi</p><!-- TODO [COMPANY_NAME] and <first_name> --><p>bye</p>'),
    # left open: the rest is the comment, or the code
    ('Hi', '<p>hi</p><!-- [COMPANY_NAME]'),
    ('Hi', '<p>hi</p><pre>[user_id]'),
]

#: (subject, body, blank): a mark in the words the reader sees is still
#: found, beside a comment or a code block and after one.
STILL_REFUSED = [
    ('hello', '<!-- greeting --><p>Hi <first_name>,</p>', '<first_name>'),
    ('hello', '<pre>[user_id]</pre><p>Dear [COMPANY_NAME] team</p>', '[COMPANY_NAME]'),
    ('hello', '<p>Dear [COMPANY_NAME] team</p><pre>[user_id]</pre>', '[COMPANY_NAME]'),
    ('hello', '<code>a</code> [FIRST_NAME] <code>b</code>', '[FIRST_NAME]'),
    ('hello', '<!-- a --><!-- b -->[FIRST_NAME]<!-- c', '[FIRST_NAME]'),
    ('About <company_name>', '<!-- x --><p>hi</p>', '<company_name>'),
    ('hello', '<p><a href="[UNSUBSCRIBE_URL]">x</a></p>', '[UNSUBSCRIBE_URL]'),
    ('', '<first_name>', '<first_name>'),
    ('', '[COMPANY_NAME]', '[COMPANY_NAME]'),
    # a code block of another kind does not end this one
    ('hello', '<pre>a</code>[user_id]</pre> [FIRST_NAME]', '[FIRST_NAME]'),
    # an end tag written with a space ends the block, as a mail reader ends it
    ('hello', '<pre>[user_id]</pre > Dear [FIRST_NAME]', '[FIRST_NAME]'),
    ('hello', '<code>[user_id]</CODE\n> Dear [FIRST_NAME]', '[FIRST_NAME]'),
    # <preview> is not a code block
    ('hello', '<preview>[FIRST_NAME]</preview>', '[FIRST_NAME]'),
    # Outlook shows what a conditional comment holds
    ('hello', '<!--[if mso]><p>Dear [COMPANY_NAME]</p><![endif]--><p>hi</p>',
     '[COMPANY_NAME]'),
    # a brace blank is found everywhere, as the old pattern found it
    ('hello', '<!-- {POSTAL ADDRESS} --><p>hi</p>', '{POSTAL ADDRESS}'),
    ('hello', '<pre>print(f"Hello {name}")</pre>', '{name}'),
    ('', '{first_name}', '{first_name}'),
]


@pytest.mark.parametrize('subject, body', SENT_AGAIN)
def test_an_email_is_read_as_html_not_as_a_chat_text(subject, body):
    assert crm.verify_outgoing_email(TO, subject, body) is None


@pytest.mark.parametrize('subject, body, blank', STILL_REFUSED)
def test_a_mark_in_the_words_an_email_reader_sees_is_still_found(subject, body, blank):
    out = crm.verify_outgoing_email(TO, subject, body)

    assert out is not None, 'the email left with a fill-in mark in it'
    assert out['error'] == f'the email still holds the template blank {blank}'


def test_a_real_outlook_email_still_leaves():
    body = ('<!--[if mso]><table><tr><td><![endif]--><p>hi</p>'
            '<!--[if mso]></td></tr></table><![endif]-->')
    assert crm.verify_outgoing_email(TO, 'Hello', body) is None


@pytest.mark.parametrize('text, mark', [
    ('<p>', '<p>'), ('<hr>', '<hr>'), ('  <x>  ', '<x>'),
    ('<!-- your question block --><p>hi</p>', '<!-- your question block -->'),
    ('<p><why you ask> and more</p>', '<why you ask>'),
    ('Q: <your question?>', '<your question?>'),
    ('<pre>ids = [user_id]</pre>', '[user_id]'),
    ('<code>[row_id]</code>', '[row_id]'),
    ('<!-- [COMPANY_NAME] -->', '<!-- [COMPANY_NAME] -->'),
    ('note <!-- [COMPANY_NAME] -->', '[COMPANY_NAME]'),
    ('<pre><order><item_id>7</item_id></order></pre>', '<item_id>'),
])
def test_a_message_keeps_every_shape_an_email_no_longer_has(text, mark):
    """Chat and asks are read as before: the email reading is the email
    mode's alone."""
    assert agent_tools.unfilled_text_placeholder(text) == mark
    assert agent_tools.unfilled_text_placeholder(text, braces=False) == mark


def test_many_unclosed_comments_and_code_blocks_are_read_in_one_pass():
    """Each '<!--' and '<pre' with no end is tried once, not once per
    start: 40,000 of them in an email are read at once."""
    import time
    for start in ('<!-- ', '<pre ', '<pre>', '<code x="'):
        body = start * 40_000
        began = time.perf_counter()
        assert agent_tools.unfilled_text_placeholder(body, braces=True) is None
        assert time.perf_counter() - began < 2.0, start
