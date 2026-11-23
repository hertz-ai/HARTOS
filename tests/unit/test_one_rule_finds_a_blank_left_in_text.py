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
import html

import pytest

from core import agent_tools
from integrations.agent_engine import outreach_crm_tools as crm

TO = 'ana@clinic.example'


def refusal(subject, blank):
    """The email check's refusal of ``blank``, naming the part that
    holds it (review rv_fc0c): the subject when it is there, else the body."""
    part = ('subject' if blank in (subject or '')
            or blank in html.unescape(subject or '') else 'body')
    return {'success': False, 'via': 'verification',
            'error': f"the email's {part} still holds the template blank {blank}",
            'fix': (f'Fill in or remove {blank} in the {part}, then send it '
                    f'again. An email with a blank in it never leaves.')}

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

    assert out == refusal(subject, blank)


@pytest.mark.parametrize('subject, body', SENT_BEFORE_AND_NOW)
def test_an_email_with_no_blank_still_leaves(subject, body):
    assert crm.verify_outgoing_email(TO, subject, body) is None


@pytest.mark.parametrize('subject, body, blank', REFUSED_NOW)
def test_an_email_with_a_mark_a_message_would_be_refused_for_never_leaves(
        subject, body, blank):
    out = crm.verify_outgoing_email(TO, subject, body)

    assert out is not None, 'the email left with a fill-in mark in it'
    assert out['error'] == refusal(subject, blank)['error']
    assert out['via'] == 'verification' and blank in out['fix']


def test_the_email_check_asks_the_one_rule(monkeypatch):
    asked = []
    real = agent_tools.unfilled_text_placeholder

    def recording(text, *a, **k):
        asked.append((text, k.get('as_html', True)))
        return real(text, *a, **k)
    monkeypatch.setattr(agent_tools, 'unfilled_text_placeholder', recording)

    crm.verify_outgoing_email(TO, 'a subject', '<p>a body</p>')

    # the subject as the plain text it is, the body as HTML (rv_c0f5)
    assert asked == [('a subject', False), ('<p>a body</p>', True)]

    # What the one rule answers is all the email check decides on blanks
    # (review rv_c092 #3: a check of a name alone passes a pattern kept
    # under another name).  With the rule finding none, no email of the
    # tables above is refused, whatever its blank; with the rule naming
    # one, that one is refused and named.
    monkeypatch.setattr(agent_tools, 'unfilled_text_placeholder',
                        lambda text, *a, **k: None)
    for subject, body, blank in REFUSED_BEFORE + REFUSED_NOW + STILL_REFUSED:
        assert crm.verify_outgoing_email(TO, subject, body) is None, (
            f'the email check refused {blank} on a rule of its own')
    monkeypatch.setattr(agent_tools, 'unfilled_text_placeholder',
                        lambda text, *a, **k: '<the_rules_blank>')
    out = crm.verify_outgoing_email(TO, 'a subject', '<p>a body</p>')
    assert out['error'] == ("the email's subject still holds the template "
                            "blank <the_rules_blank>")
    assert '<the_rules_blank>' in out['fix']


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
    assert out['error'] == refusal(subject, blank)['error']


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
    for start in ('<!-- ', '<pre ', '<pre>', '<code x="',
                  '<!--[if a', '<![if a', '<style ', '<style>', '<code>a</code>['):
        body = start * 40_000
        began = time.perf_counter()
        assert agent_tools.unfilled_text_placeholder(body, braces=True) is None
        assert time.perf_counter() - began < 2.0, start


# -- what a mail reader shows (review rv_fqa of 310d4a93e, rv_c092 #2) ----
# A comment ends where the reader's HTML parser ends it, so the Outlook
# idiom's '<!--[if !mso]><!-->' is no comment's start and the words after it
# are shown by every reader but Outlook.  And the reader decodes character
# references: "&lt;first_name&gt;" is shown as <first_name>.

#: (subject, body, blank): a reader shows the blank, so the email never
#: leaves.
SHOWN_REFUSED = [
    # the Outlook idiom: every reader but Outlook shows what it holds
    ('hello', '<!--[if !mso]><!--><p>Hello [COMPANY_NAME]</p><!--<![endif]-->',
     '[COMPANY_NAME]'),
    ('hello', '<!--[if !mso]><!---><p>Hi <first_name></p><!--<![endif]-->',
     '<first_name>'),
    ('hello', '<!--[if !mso]><!----><p>Hello [COMPANY_NAME]</p><!--<![endif]-->',
     '[COMPANY_NAME]'),
    ('hello', '<!--[if !mso]><!-- --><p>Hi <first_name></p><!--<![endif]-->',
     '<first_name>'),
    # '<!-->' is a whole comment, and '--!>' ends one
    ('hello', '<p>hi</p><!--><p>Dear [COMPANY_NAME]</p><!-- end -->',
     '[COMPANY_NAME]'),
    ('hello', '<!-- note --!><p>Dear [COMPANY_NAME]</p>', '[COMPANY_NAME]'),
    # character references, decoded as the reader decodes them
    ('hello', '<p>Hi &lt;first_name&gt;,</p>', '<first_name>'),
    ('hello', '<p>Hi&nbsp;&lt;first_name&gt;,</p>', '<first_name>'),
    ('hello', '<p>Hi &#x3C;first_name&#x3E;,</p>', '<first_name>'),
    ('hello', '<p>Dear &#91;COMPANY_NAME&#93; team</p>', '[COMPANY_NAME]'),
    ('About &lt;company_name&gt;', '<p>hi</p>', '<company_name>'),
    ('hello', '<p>Use &#123;SAVE10&#125; today</p>', '{SAVE10}'),
    # a reference never opens a comment: the reader shows "<!-- ... -->"
    ('hello', '<p>&lt;!-- &lt;first_name&gt; --&gt;</p>', '<first_name>'),
    # the text as written is read first: what was found is named the same
    ('hello', '<p>Hi &lt;first_name&gt; from [COMPANY_NAME]</p>', '[COMPANY_NAME]'),
    ('hello', '<p>x&#65;[COMPANY_NAME]</p>', '[COMPANY_NAME]'),
    # and as shown, a brace blank before the other shape, as when written
    ('hello', '<p>Hi &lt;first_name&gt;, code &#123;CODE&#125;</p>', '{CODE}'),
]

#: (subject, body): no reader shows a blank, so the email leaves.
HIDDEN_SENT = [
    # a real comment inside the idiom is still a comment
    ('Hi', '<!--[if !mso]><!--><p>hi</p><!-- TODO [COMPANY_NAME] --><!--<![endif]-->'),
    ('Hi', '<p>hi</p><!-- hidden [COMPANY_NAME] and &lt;first_name&gt; -->'),
    ('Hi', '<!-- &#123;first_name&#125; --><p>hi</p>'),
    # references in a code block are code
    ('Dev tip', '<pre>List&lt;my_type&gt; and &lt;item_id&gt;</pre>'),
    ('Dev tip', '<p>write <code>&#91;user_id&#93;</code></p>'),
    # references that show no blank
    ('hello', '<p>Fish &amp; chips, 3 &lt; 5, &copy; 2026</p>'),
    ('hello', '<p>std::vector&lt;my_type&gt; is a type</p>'),
    # decoded once, as the reader does: this shows "&lt;first_name&gt;"
    ('hello', '<p>Write &amp;lt;first_name&amp;gt; to show a tag</p>'),
]


@pytest.mark.parametrize('subject, body, blank', SHOWN_REFUSED)
def test_a_blank_a_mail_reader_shows_never_leaves(subject, body, blank):
    out = crm.verify_outgoing_email(TO, subject, body)

    assert out is not None, 'the email left with a blank its reader sees'
    assert out == refusal(subject, blank)


@pytest.mark.parametrize('subject, body', HIDDEN_SENT)
def test_what_no_mail_reader_shows_is_not_a_blank(subject, body):
    assert crm.verify_outgoing_email(TO, subject, body) is None


def test_a_real_outlook_email_with_both_idioms_still_leaves():
    body = ('<!--[if mso]><table><tr><td><![endif]-->'
            '<!--[if !mso]><!--><div class="wrap"><!--<![endif]-->'
            '<p>Hi Ana, a short note from Sam.</p>'
            '<!--[if !mso]><!--></div><!--<![endif]-->'
            '<!--[if mso]></td></tr></table><![endif]-->')
    assert crm.verify_outgoing_email(TO, 'Hello', body) is None


@pytest.mark.parametrize('text, mark', [
    ('Hi &lt;first_name&gt;', None),
    ('<!--[if !mso]><!-->hi<!--<![endif]-->', None),
    ('Use &#123;SAVE10&#125;', None),
])
def test_a_message_is_not_read_as_an_email(text, mark):
    """Chat and asks are read as before: the reader's decoding and the
    comment rule are the email mode's alone."""
    assert agent_tools.unfilled_text_placeholder(text) == mark


# -- a template field's name holds dots and hyphens (rv_c092 #2) ----------
# A local model writing a template writes Jinja's and Liquid's fields:
# {{ user.first_name }}, {{first-name}}.  The brace rule took only letters,
# digits, '_' and spaces in a name, so these were sent.

#: (subject, body, blank): refused in the brace rule's own words.
DOTTED_BRACE_REFUSED = [
    ('hello', '<p>Hi {{ user.first_name }},</p>', '{{ user.first_name }}'),
    ('hello', '<p>Hi {{first-name}},</p>', '{{first-name}}'),
    ('{{ company.name }} news', '<p>hi</p>', '{{ company.name }}'),
    ('hello', '<p>Your order {order-id} has shipped</p>', '{order-id}'),
    ('hello', '<p>Dear {{ contact.first-name }} at {{ contact.company }}</p>',
     '{{ contact.first-name }}'),
    ('hello', '<p><a href="https://x.example/u/{{ user.id }}">your page</a></p>',
     '{{ user.id }}'),
    ('hello', '<p>Hi {first.name},</p>', '{first.name}'),
    # found everywhere, as every brace blank is
    ('hello', '<!-- {{ user.first_name }} --><p>hi</p>', '{{ user.first_name }}'),
    ('hello', '<p>Hi &#123;&#123; user.first_name &#125;&#125;</p>',
     '{{ user.first_name }}'),
    # the first brace blank in the text is the one named
    ('hello', '<p>Hi {{ user.first_name }}, your code is {CODE}</p>',
     '{{ user.first_name }}'),
]

#: (subject, body): braces holding dots or hyphens that are no field: CSS,
#: numbers, a format spec, JSON, an ellipsis, a list.  They still leave.
DOTTED_BRACES_THAT_LEAVE = [
    ('Styles', '<style>.btn-primary {background-color: #f60; border-radius: 4px}'
               '</style><p>hi</p>'),
    ('Styles', '<style>@media (max-width: 600px) { .col-6 { width: 100% !important; } }'
               '</style><p>hi</p>'),
    ('Styles', '<style>p {margin: 0.5em 0; line-height: 1.5}</style><p>hi</p>'),
    ('Styles', '<style>body {font-family: -apple-system, Segoe UI}</style><p>hi</p>'),
    ('Sizes', '<p>Pick a size from {0.5, 1.5, 2.5} litres.</p>'),
    ('Weather', '<p>Expect {-5 to 10} degrees this week.</p>'),
    ('Dev tip', '<p>Format a price with {price:.2f} in Python.</p>'),
    ('Dev tip', '<p>Send {"user.id": 7, "plan": "pro-annual"} as the body.</p>'),
    ('Quote', '<p>The quote was trimmed {...} for length.</p>'),
    ('Versions', '<p>Works with {v2.1, v2.2} and later.</p>'),
]


@pytest.mark.parametrize('subject, body, blank', DOTTED_BRACE_REFUSED)
def test_a_template_field_with_dots_or_hyphens_never_leaves(subject, body, blank):
    out = crm.verify_outgoing_email(TO, subject, body)

    assert out is not None, 'the email left with a template field in it'
    assert out == refusal(subject, blank)


@pytest.mark.parametrize('subject, body', DOTTED_BRACES_THAT_LEAVE)
def test_braces_with_dots_or_hyphens_that_are_no_field_still_leave(subject, body):
    assert crm.verify_outgoing_email(TO, subject, body) is None


@pytest.mark.parametrize('text', [
    'Hi {{ user.first_name }}, this is how a template looks.',
    'In Jinja write {{first-name}} where the value goes.',
])
def test_a_dotted_field_in_a_message_or_an_ask_is_not_a_blank(text):
    """Braces are the email mode's alone, dotted or not."""
    assert agent_tools.unfilled_text_placeholder(text) is None


# -- a blank in capitals is a blank in a code block too (review rv_fqa) ---
# A code block is shown to the reader as it is.  Its brackets are code
# ("[user_id]", "<K_V>"), but the underscore shape's [] form in capitals is
# the blank the rule refuses everywhere else: "<code>[DISCOUNT_CODE]</code>"
# was sent.

#: (subject, body, blank)
CODE_CAPS_REFUSED = [
    ('Offer', '<p>Use code <code>[DISCOUNT_CODE]</code> at checkout</p>', '[DISCOUNT_CODE]'),
    ('hello', '<p>Hi <code>[FIRST_NAME]</code>,</p>', '[FIRST_NAME]'),
    ('hello', '<pre>Dear [FIRST_NAME],\nyour id is [user_id]</pre>', '[FIRST_NAME]'),
    ('hello', '<pre>ids = [user_id]\npromo = [PROMO_CODE]</pre>', '[PROMO_CODE]'),
    ('hello', '<pre><code class="x">[DISCOUNT_CODE]</code></pre>', '[DISCOUNT_CODE]'),
    # a code block left open runs to the end, and is still read for them
    ('hello', 'See <code>pip install x<p>Hello [FIRST_NAME], thanks</p>', '[FIRST_NAME]'),
    # written as character references, as the reader sees them
    ('Offer', '<p>Use <code>&#91;DISCOUNT_CODE&#93;</code></p>', '[DISCOUNT_CODE]'),
    # ... and first as written, as the words are ("x&#65;[COMPANY_NAME]")
    ('Offer', '<p>Use <code>x&#65;[PROMO_CODE]</code></p>', '[PROMO_CODE]'),
    # the first blank in the text is named
    ('hello', '<code>[PROMO_CODE]</code> Dear [FIRST_NAME]', '[PROMO_CODE]'),
]

#: (subject, body): code that holds no blank in capitals still leaves.
CODE_WITHOUT_CAPS_BLANK_LEAVES = [
    ('Dev tip', '<pre>token = os.environ[API_KEY]</pre>'),
    ('Dev tip', '<pre>[SEE_DOCS](https://x.example/docs)</pre>'),
    ('Dev tip', '<pre>fn id< T >(x: T) and <K_V> and <MAX_SIZE></pre>'),
    ('Dev tip', '<pre>rows = [row_count]\nreturn [user_id]</pre>'),
    ('Hi', '<p>hi</p><!-- <code>[DISCOUNT_CODE]</code> -->'),
]


@pytest.mark.parametrize('subject, body, blank', CODE_CAPS_REFUSED)
def test_a_blank_in_capitals_in_a_code_block_never_leaves(subject, body, blank):
    out = crm.verify_outgoing_email(TO, subject, body)

    assert out is not None, 'the email left with a blank in a code block'
    assert out == refusal(subject, blank)


@pytest.mark.parametrize('subject, body', CODE_WITHOUT_CAPS_BLANK_LEAVES)
def test_code_that_holds_no_blank_in_capitals_still_leaves(subject, body):
    assert crm.verify_outgoing_email(TO, subject, body) is None


# -- the subject is plain text; one reading of what the reader sees -------
# Review rv_c0f5 of 310d4a93e: the subject went in with the body and was read
# as HTML; the ask hint's blanks and the whole-text blank were dropped for
# the body, though its reader is left with "Q:" or an empty email; and the
# plain-text part a sent email carries had a reading of its own, which
# showed "b [COMPANY_NAME] -->" from a comment the check had cut.

#: (subject, body, blank): the subject read as the plain text it is.
SUBJECT_REFUSED = [
    ('Quick question: <your question>', '<p>hi</p>', '<your question>'),
    ('<why you ask>', '<p>hi</p>', '<why you ask>'),
    ('<your question?> about your clinic', '<p>hi</p>', '<your question?>'),
    # a subject line shows a comment or a code tag as it is written
    ('<!-- [COMPANY_NAME] --> news', '<p>hi</p>', '[COMPANY_NAME]'),
    ('Re: <code>[user_id]</code>', '<p>hi</p>', '[user_id]'),
]

#: (subject, body, blank): a tag the reader drops is still a blank left in.
BODY_HINT_OR_WHOLE_REFUSED = [
    ('hello', '<p><why you ask> and more</p>', '<why you ask>'),
    ('Hi', '<p>Q: <your question?></p>', '<your question?>'),
    ('', '<your message here>', '<your message here>'),
    ('Hi', '<!-- note --><your message here>', '<your message here>'),
    ('Hi', '<p>Q: &lt;your question?&gt;</p>', '<your question?>'),
    ('Hi', '&lt;your message here&gt;', '<your message here>'),
    # the reader's words run on across a tag the reader drops
    ('hello', '<p>Dear [COMPANY<b></b>_NAME] team</p>', '[COMPANY_NAME]'),
    ('hello', '<p>Hi {first<i></i>_name},</p>', '{first_name}'),
    # <pre-header> is no <pre>: what it holds is read
    ('hello', '<pre-header>Hi <first_name>,</pre-header><p>hi</p>', '<first_name>'),
    # a code block ends at its own end tag: what follows is read
    ('hello', '<pre>ids = [user_id]</pre><p>Hi <first_name>,</p>', '<first_name>'),
]

#: (subject, body): one tag that reads as an element, and the hint's words
#: in a comment, still leave.
ONE_ELEMENT_LEAVES = [
    ('', '<br/>'),
    ('', '<hr class="rule">'),
    ('', '<img src=https://x.example/a.png alt="">'),
    ('', '<mj-raw>'),
    ('', '<v:rect xmlns:v="urn:schemas-microsoft-com:vml" fill="true">'),
    ('Hi', '<!-- preheader: your question block --><p>hi</p>'),
]


@pytest.mark.parametrize('subject, body, blank', SUBJECT_REFUSED + BODY_HINT_OR_WHOLE_REFUSED)
def test_a_blank_in_the_subject_or_a_dropped_tag_never_leaves(subject, body, blank):
    out = crm.verify_outgoing_email(TO, subject, body)

    assert out is not None, 'the email left with a blank in it'
    assert out == refusal(subject, blank)


@pytest.mark.parametrize('subject, body', ONE_ELEMENT_LEAVES)
def test_one_element_or_a_hint_in_a_comment_still_leaves(subject, body):
    assert crm.verify_outgoing_email(TO, subject, body) is None


READER_TEXT = [
    ('<p>Hello Ana</p>', 'Hello Ana'),
    ('<!-- a > b [COMPANY_NAME] --><p>hi</p>', 'hi'),
    ('<style>p{margin:0} .a{color:red}</style><p>hi</p>', 'hi'),
    ('<head><title>News</title></head><p>hi</p>', 'hi'),
    ('<p>3 &lt; 5 &amp; 7 &gt; 2</p>', '3 < 5 & 7 > 2'),
    ('<p>3 < 5 and 7 > 2</p>', '3 < 5 and 7 > 2'),
    ('<!--[if mso]><p>x</p><![endif]--><p>hi</p>', 'xhi'),
    ('<!--[if !mso]><!--><p>Hello</p><!--<![endif]-->', 'Hello'),
    ('<pre>ids = [user_id]</pre>', 'ids = [user_id]'),
    ('<p>hi</p><!-- open', 'hi'),
]


@pytest.fixture
def channel(monkeypatch):
    """The node's email channel, faked at the adapter: what the real
    _send_email hands it is recorded, nothing leaves."""
    import asyncio
    import sys
    import threading
    import types
    sent = []

    class Result:
        success, error = True, None

    class Adapter:
        async def send_email(self, to, subject, body, html_body=None, **_):
            sent.append((subject, body, html_body))
            return Result()

    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    integration = types.SimpleNamespace(
        registry=types.SimpleNamespace(get=lambda name: Adapter()),
        running_loop=lambda: loop)
    monkeypatch.setitem(sys.modules, 'integrations.channels.flask_integration',
                        types.SimpleNamespace(get_channel_integration=lambda: integration))
    monkeypatch.setattr(crm, '_goalless_email_is_held', lambda: False)
    yield sent
    loop.call_soon_threadsafe(loop.stop)
    runner.join(5)
    loop.close()


@pytest.mark.parametrize('body, seen', READER_TEXT)
def test_the_plain_part_is_what_the_reader_sees(channel, body, seen):
    out = crm._send_email(TO, 'Hi', body)

    assert out['success'] is True
    assert channel[-1][1] == seen


def test_the_plain_part_shows_only_what_the_check_read(channel):
    out = crm._send_email(TO, 'Hi', '<!-- a > b [COMPANY_NAME] --><p>hi</p>'
                                    '<style>p{margin:0}</style>')

    assert out['success'] is True
    assert channel == [('Hi', 'hi', '<!-- a > b [COMPANY_NAME] --><p>hi</p>'
                                    '<style>p{margin:0}</style>')]


def test_the_check_and_the_plain_part_are_one_reading(channel, monkeypatch):
    """No second reading of what the reader sees: the plain part is
    email_reader_text's, and the check and that text read the email once
    each, by _read_email."""
    read = []
    real = getattr(agent_tools, '_read_email', None)

    def reading(text):
        read.append(text)
        return real(text)
    monkeypatch.setattr(agent_tools, '_read_email', reading, raising=False)

    crm._send_email(TO, 'Hi', '<p>hi</p>')

    assert read == ['<p>hi</p>', '<p>hi</p>']   # the plain part's, the check's

    monkeypatch.setattr(agent_tools, 'email_reader_text',
                        lambda html_text: 'THE READER TEXT', raising=False)
    crm._send_email(TO, 'Hi', '<p>hi</p>')

    assert channel[-1] == ('Hi', 'THE READER TEXT', '<p>hi</p>')


# -- review rv_fc0c of fc0cca9a3 ------------------------------------------
# A real element's tag may carry the standard's boolean attributes; a
# subject line is never code, so a blank glued to a word is one there; a
# '<...>' that is all its element holds is a gap in the email; the refusal
# names the part.

#: (subject, body): one tag of markup with a boolean attribute still leaves.
MARKUP_WITH_A_BARE_ATTRIBUTE_LEAVES = [
    ('Hi', '<img src="https://x.example/b.png" alt="" hidden>'),
    ('Hi', '<td nowrap>'),
    ('Hi', '<p hidden>'),
    ('Hi', '<VIDEO SRC="https://x.example/a.mp4" CONTROLS>'),
]

#: (subject, body, blank): refused.
GAPS_AND_GLUED_REFUSED = [
    # bare words no element takes, or no element at all
    ('Hi', '<a short intro>', '<a short intro>'),
    ('Hi', '<link here>', '<link here>'),
    ('Hi', '<name>', '<name>'),
    # a subject is never code
    ('Hi<first_name>', '<p>hi</p>', '<first_name>'),
    ('x[A_B]', '<p>hi</p>', '[A_B]'),
    ('Re: [COMPANY_NAME](https://x.example)', '<p>hi</p>', '[COMPANY_NAME]'),
    # a '<...>' that is all its element holds
    ('Hi', '<p>Hi Ana,</p><p><your message here></p><p>Sam</p>', '<your message here>'),
    ('Hi', '<table><tr><td> <insert photo> </td></tr></table>', '<insert photo>'),
    ('Hi', '<ul><li><first benefit></li></ul>', '<first benefit>'),
    ('Hi', '<div>&nbsp;<!-- x --><add your offer>&nbsp;</div>', '<add your offer>'),
    ('Hi', '<p>Hi</p><p><a short intro></p>', '<a short intro>'),
    ('Hi', '<p>&lt;your message here&gt;</p>', '<your message here>'),
    ('Hi', '<html><body><your message here></body></html>', '<your message here>'),
    ('Hi', '<p>Hi Ana,</p><p><your message here>', '<your message here>'),
    ('Hi', '<p><b><your note></p>', '<your note>'),
    ('Hi', '<td><img src="x"><your caption></td>', '<your caption>'),
    # as before: an end tag alone, or a name no element has, is no markup
    ('Hi', '</pre>', '</pre>'),
    ('Hi', '<a}--!>', '<a}--!>'),
]

#: (subject, body): an element that holds markup, words, or code leaves.
NO_GAP_LEAVES = [
    ('Hi', '<p><img src="https://x.example/b.png" alt=""></p>'),
    ('Hi', '<p><br></p><p>hi</p>'),
    ('Hi', '<table><tr><td><o:p></o:p></td></tr></table>'),
    ('Hi', '<p><mj-raw></mj-raw></p>'),
    ('Hi', '<p>Dear <name>, hi</p>'),
    ('Hi', '<pre><K_V></pre>'),
    ('Hi', '<td><img src="x"><span>shop</span></td>'),
    ('Hi', '<p><img src="x" hidden></p>'),
]


@pytest.mark.parametrize('subject, body', MARKUP_WITH_A_BARE_ATTRIBUTE_LEAVES + NO_GAP_LEAVES)
def test_markup_and_full_elements_still_leave(subject, body):
    assert crm.verify_outgoing_email(TO, subject, body) is None


@pytest.mark.parametrize('subject, body, blank', GAPS_AND_GLUED_REFUSED)
def test_a_gap_or_a_glued_subject_blank_never_leaves(subject, body, blank):
    assert crm.verify_outgoing_email(TO, subject, body) == refusal(subject, blank)


def test_the_refusal_names_the_part_that_holds_the_blank():
    in_subject = crm.verify_outgoing_email(TO, 'About [COMPANY_NAME]', '<p>hi</p>')
    in_body = crm.verify_outgoing_email(TO, 'Hello', '<p>Dear [COMPANY_NAME]</p>')

    assert in_subject['error'] == "the email's subject still holds the template blank [COMPANY_NAME]"
    assert in_subject['fix'].startswith('Fill in or remove [COMPANY_NAME] in the subject,')
    assert in_body['error'] == "the email's body still holds the template blank [COMPANY_NAME]"
    assert in_body['fix'].startswith('Fill in or remove [COMPANY_NAME] in the body,')


@pytest.mark.parametrize('text', ['Hi<first_name>, welcome', 'my_list[item_index]',
                                  'see [click_here](https://x.example)', '<a short intro>'])
def test_a_message_keeps_its_code_and_link_rules(text):
    """Chat and asks are read as before: the subject's rules are its own."""
    expected = '<a short intro>' if text == '<a short intro>' else None
    assert agent_tools.unfilled_text_placeholder(text) == expected


def test_a_run_of_open_brackets_is_read_at_once():
    """The memory keys' rule (whole-text blank) steps to the last '<' before
    a '>', not one '<' at a time: 200,000 of them are read at once."""
    import time
    for text in ('<' * 200_000 + '>', '<a' * 100_000 + '>'):
        began = time.perf_counter()
        agent_tools.unfilled_text_placeholder(text, braces=True)
        agent_tools.unfilled_text_placeholder(text)
        assert time.perf_counter() - began < 1.0, text[:4]
    assert agent_tools.unfilled_key_placeholder('<' * 1000 + 'a>') == '<a>'
    assert agent_tools.unfilled_key_placeholder('x<<y>z') == '<y>'


# -- review rv_e4f5 of e4f597137 ------------------------------------------
# Real markup was refused (inline SVG, data-* and download attributes, an
# unquoted value, Outlook's empty <xml>); blanks the reader sees still left
# (a subject's "<first name>", an escaped "<first-name>", a blank on a line
# of its own, an implicitly closed <p>, a made-up hyphenated tag).

#: (subject, body): real markup leaves again.
REAL_MARKUP_LEAVES = [
    ('Hi', '<span style="display:inline-block"><svg viewBox="0 0 24 24">'
           '<path d="M9 16l-4-4"/></svg></span> Booked'),
    ('Hi', '<p>Hi Ana,</p><p><svg width="16" height="16"><path d="M0 0h16v16H0z"/></svg></p>'),
    ('Hi', '<p><svg><circle cx="4" cy="4" r="4"></circle></svg> Live now</p>'),
    ('Hi', '<p><math><mi>x</mi></math></p>'),
    ('Hi', '<p><math><mspace width="1em"/></math></p>'),
    ('Hi', '<table><tr><td><div data-empty></div></td></tr></table>'),
    ('Hi', '<table><tr><td><span aria-hidden></span></td></tr></table>'),
    ('Hi', '<p><a href="https://x.example/g.pdf" download></a></p>'),
    # ... and where no end tag of their own follows
    ('Hi', '<img src="https://x.example/a.png" alt="" data-lazy>'),
    ('Hi', '<a href="https://x.example/g.pdf" download>'),
    ('Hi', '<p><img src="https://x.example/a.png" aria-hidden><br>Caption</p>'),
    ('Hi', '<p><img src=https://x.example/i.png?w=100></p>'),
    ('Hi', '<table><tr><td><!--[if mso]><xml></xml><![endif]-->Hi</td></tr></table>'),
    ('Hi', '<table><tr><td><!--[if gte mso 9]><xml><o:AllowPNG/></xml><![endif]--></td></tr></table>'),
    ('Hi', '<table><tr><td><tbodyx></tbodyx></td></tr></table>'),
    ('Hi', '<table><tr><td><amp-img src="x" width="1" height="1"></amp-img></td></tr></table>'),
    ('Hi', '<p><v:roundrect arcsize="10%"><w:anchorlock/></v:roundrect></p>'),
    ('Reply to <sam@x.example>', '<p>hi</p>'),
    ('Fees < 5 > all', '<p>hi</p>'),
]

#: (subject, body, blank): a blank the reader sees never leaves.
VISIBLE_BLANKS_REFUSED = [
    ('Hi <first name>', '<p>hi</p>', '<first name>'),
    ('Your <Company> plan', '<p>hi</p>', '<Company>'),
    ('Hi &lt;first name&gt;', '<p>hi</p>', '<first name>'),
    ('Hi', '<p>&lt;first-name&gt;</p>', '<first-name>'),
    ('Hi', 'Hi Ana,<br><br><your message here><br><br>Best,<br>Sam', '<your message here>'),
    ('Hi', '<p>Hi Ana,<br><your message here></p>', '<your message here>'),
    ('Hi', '<p>Hi Ana,</p><p><your message here><br></p><p>Sam</p>', '<your message here>'),
    ('Hi', '<p>Thanks,<br><sender:name></p>', '<sender:name>'),
    ('Hi', '<p>Hi Ana,<p><your message here><p>Thanks', '<your message here>'),
    ('Hi', '<ul><li>One<li><your benefit><li>Three</ul>', '<your benefit>'),
    ('Hi', '<p>Hi Ana,</p><p><your-message-here></p><p>Sam</p>', '<your-message-here>'),
    ('Hi', '<your-message-here>', '<your-message-here>'),
    # an unknown tag holds what follows it, as an element would
    ('Hi', '<pre-header><your message here>', '<your message here>'),
    ('Hi', '<p><your intro> <your offer></p>', '<your offer>'),
]


@pytest.mark.parametrize('subject, body', REAL_MARKUP_LEAVES)
def test_real_markup_is_no_blank(subject, body):
    assert crm.verify_outgoing_email(TO, subject, body) is None


@pytest.mark.parametrize('subject, body, blank', VISIBLE_BLANKS_REFUSED)
def test_a_blank_the_reader_sees_never_leaves(subject, body, blank):
    assert crm.verify_outgoing_email(TO, subject, body) == refusal(subject, blank)


@pytest.mark.parametrize('text', ['Hi <first name>, welcome', 'Hi <first-name>'])
def test_a_message_keeps_single_brackets_inside_its_words(text):
    """Chat and asks are read as before: words in brackets inside a longer
    text are no blank there."""
    assert agent_tools.unfilled_text_placeholder(text) is None


# -- review rv_f32b of f32b9eb71 ------------------------------------------
# Shown blanks that open with a digit, a symbol or a space were sent again;
# an <svg> or <math> that a self-closing tag, a stray word or a missing end
# tag left open hid every blank after it; and the own-end-tag rule took
# '<a call-to-action label></a>' for an empty element.

#: (subject, body, blank): refused, as before f32b9eb71.
SHOWN_AND_HIDDEN_BLANKS_REFUSED = [
    ('Hi', '<p>Hi Ana,</p><p>&lt;2-3 sentences about the product&gt;</p><p>Thanks</p>',
     '<2-3 sentences about the product>'),
    ('Hi', '<p>Hi Ana,</p><p><2-3 sentences about the product></p>',
     '<2-3 sentences about the product>'),
    ('Hi', '<p>&lt; Your Name &gt;</p>', '< Your Name >'),
    ('Hi', '<table><tr><td>Price</td><td>&lt;$price&gt;</td></tr></table>', '<$price>'),
    ('Hi', '<p>&lt;10% discount code&gt;</p>', '<10% discount code>'),
    ('Hi', '<p>&lt;1-line summary&gt;</p>', '<1-line summary>'),
    ('Hi', '<p>&lt;#order&gt;</p>', '<#order>'),
    # as before f32b9eb71; whether it is a blank is the owner's call
    ('Hi', '<p>&lt; 5 mins &gt;</p>', '< 5 mins >'),
    # an svg or math that holds nothing, or that an HTML tag ends
    ('Hi', '<p><svg width="16" height="16"/> Booked</p><p><your message here></p>'
           '<p>Thanks,<br>Sam</p>', '<your message here>'),
    ('Hi', '<p><svg width="16" height="16"/> Booked</p><p>&lt;customer name&gt;</p>',
     '<customer name>'),
    ('Hi', '<p><math/></p><p><your message here></p>', '<your message here>'),
    ('Hi', '<p>Hi</p><table><tr><td><svg width="16"/><your note></td></tr></table>',
     '<your note>'),
    ('Hi', '<p>Join our <math> club!</p><p><your message here></p>', '<your message here>'),
    ('Hi', '<p><svg width="16"><path d="M0 0"/></p><p><your message here></p>',
     '<your message here>'),
    ('Hi', '<svg><p><your message here></p></svg>', '<your message here>'),
    ('Hi', '<p><math></p><font><your note></font>', '<your note>'),
    # an svg's own element holds shown text, as before f32b9eb71
    ('Hi', '<svg width="16">Hi<summary>&lt; first name &gt;', '< first name >'),
    # its own end tag after bare words: still a blank
    ('Hi', '<p>Hi Ana,</p><a href="https://x.example/book"><a call-to-action label></a>',
     '<a call-to-action label>'),
    ('Hi', '<details><summary><summary of call></summary></details>', '<summary of call>'),
    # a line break of either kind, and the next of its kind for td and option
    ('Hi', '<p>Hi</p><hr><your message here><hr><p>Sam</p>', '<your message here>'),
    ('Hi', '<table><tr><td><your note><td>Price</td></tr></table>', '<your note>'),
    ('Hi', '<select><option><your choice><option>Two</select>', '<your choice>'),
    # bracketed words the reader sees in a longer run of text
    ('Hi', '<p>Prices from &lt;$price&gt; a month</p>', '<$price>'),
    ('Hi', '<p>We send &lt;2-3 lines&gt; each week</p>', '<2-3 lines>'),
    ('Hi', '<p>Hi&lt;first-name&gt;, welcome</p>', '<first-name>'),
    ('Hi', '<div>x</div>&lt;first-name&gt;', '<first-name>'),
    ('Hi', '<div>Hi</div>&lt;Company&gt;', '<Company>'),
    ('Hi', '&lt;<b>Hi</b>&gt; there', '<Hi>'),
    # an email that shows nothing but holds an unknown tag
    ('Hi', '<your-note><a data-point>', '<your-note>'),
    ('Hi', '<table><tr><td><your caption><img src="https://x.example/a.png"></td></tr></table>',
     '<your caption>'),
    ('Hi', '<a call-to-action label></a>', '<a call-to-action label>'),
    # a stray end tag ends no element, and <body> folds into the open ones
    ('Hi', '<p>Hi</p><div></td><your note></div>', '<your note>'),
    ('Hi', '<p>Hi</p><p><your note><body></p><p>Sam</p>', '<your note>'),
]

#: (subject, body): still leaves.
STILL_LEAVES = [
    ('Hi', '<p><svg width="16" height="16"/> Booked</p><p>Thanks</p>'),
    ('<<Newsletter>> November', '<p>hi</p>'),
    ('Your link: <www.example.com>', '<p>hi</p>'),
    ('x<y and y>z', '<p>hi</p>'),
    # brackets in text that are no blank
    ('Hi', '<p>Use Map&lt;String, Integer&gt; here</p>'),
    ('Hi', '<p>On Mon, Sam &lt;sam@x.example&gt; wrote:</p>'),
    ('Hi', '<p>Read &lt;https://x.example/a&gt; first</p>'),
    ('Hi', '<p>Type &lt;b&gt; to bold</p>'),
    # space-padded in a sentence, as before f32b9eb71 (the owner's call)
    ('Hi', '<p>Wait &lt; 5 mins &gt; please</p>'),
]


@pytest.mark.parametrize('subject, body, blank', SHOWN_AND_HIDDEN_BLANKS_REFUSED)
def test_a_shown_or_left_open_blank_never_leaves(subject, body, blank):
    assert crm.verify_outgoing_email(TO, subject, body) == refusal(subject, blank)


@pytest.mark.parametrize('subject, body', STILL_LEAVES)
def test_an_icon_and_bracketed_subject_words_that_are_no_blank_leave(subject, body):
    assert crm.verify_outgoing_email(TO, subject, body) is None
