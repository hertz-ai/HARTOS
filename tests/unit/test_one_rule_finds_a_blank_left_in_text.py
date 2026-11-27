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
    code = (' If this is code, put it inside <code>.'
            if part == 'body' and agent_tools.bracket_looks_like_code(blank) else '')
    return {'success': False, 'via': 'verification',
            'error': f"the email's {part} still holds the template blank {blank}",
            'fix': (f'Fill in or remove {blank} in the {part}, then send it '
                    f'again. An email with a blank in it never leaves.{code}')}

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
#: still leave.  ("<p>Hi [First Name],</p>" and "<p>Hi %FIRSTNAME%,</p>"
#: were here: the owner's rule of 2026-11-23 makes each a blank,
#: OWNERS_BLANKS_REFUSED.)
SENT_BEFORE_AND_NOW = [
    ('hello', '<p style="color: red">hi</p>'),
    ('hello', '<style>p {color: red}</style><p>hi</p>'),
    ('hello', '<style>p { margin: 0; }</style><p>hi</p>'),
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
    assert no_blank(crm.verify_outgoing_email(TO, subject, body))


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
    assert no_blank(crm.verify_outgoing_email(TO, subject, body))


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
                  '<!--[if a', '<![if a', '<style ', '<style>', '<code>a</code>[',
                  '<div><code>x</div>', '<div><svg>x</div>', '<p>x</p>'):
        # a block an element each: 20,000 (a scan per element would take minutes)
        body = start * (20_000 if start.endswith(('</div>', '</p>')) else 40_000)
        began = time.perf_counter()
        try:
            assert not agent_tools.unfilled_text_placeholder(body, braces=True)
        except agent_tools.UnreadableEmail as unreadable:
            # "<pre>" * 40,000 nests deeper than libxml2 reads: refused
            assert 'nests' in str(unreadable), start
        # a tree walk is about a second for these; a scan per element, minutes
        assert time.perf_counter() - began < 5.0, start
    # a code block of open '[' (not glued to a word) with no capitals in
    # them: its blanks in capitals are looked for once, not once per '['
    began = time.perf_counter()
    assert not agent_tools.unfilled_text_placeholder('<pre>' + '[a ' * 40_000 + '</pre>',
                                                     braces=True)
    assert time.perf_counter() - began < 5.0


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
    # read as libxml2 decodes it, the first blank the reader meets is named
    ('hello', '<p>Hi &lt;first_name&gt; from [COMPANY_NAME]</p>', '<first_name>'),
    # ("x&#65;[COMPANY_NAME]" shows as xA[COMPANY_NAME], glued as
    # my_list[item_index] is: READ_AS_SHOWN_LEAVES)
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
    # (code in prose, "std::vector&lt;my_type&gt;", is held to the rule now:
    # CODE_IN_PROSE_REFUSED)
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
    # (glued to a reference, "x&#65;[PROMO_CODE]" shows as xA[PROMO_CODE],
    # code's own shape: READ_AS_SHOWN_LEAVES)
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
    assert no_blank(crm.verify_outgoing_email(TO, subject, body))


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
    # as before: a name no element has is no markup (an end tag alone is
    # markup, never a blank: READ_AS_SHOWN_LEAVES)
    ('Hi', '<a}--!>', '<a}--!>'),
]

#: (subject, body): an element that holds markup, words, or code leaves.
NO_GAP_LEAVES = [
    ('Hi', '<p><img src="https://x.example/b.png" alt=""></p>'),
    ('Hi', '<p><br></p><p>hi</p>'),
    ('Hi', '<table><tr><td><o:p></o:p></td></tr></table>'),
    ('Hi', '<p><mj-raw></mj-raw></p>'),
    # ("Dear <name>, hi" reads "Dear , hi": RAW_IN_A_SENTENCE_REFUSED)
    ('Hi', '<pre><K_V></pre>'),
    ('Hi', '<td><img src="x"><span>shop</span></td>'),
    ('Hi', '<p><img src="x" hidden></p>'),
]


@pytest.mark.parametrize('subject, body', MARKUP_WITH_A_BARE_ATTRIBUTE_LEAVES + NO_GAP_LEAVES)
def test_markup_and_full_elements_still_leave(subject, body):
    """No blank in them: each leaves, or is refused only for having nothing
    to read (NO_TEXT_REFUSED)."""
    assert no_blank(crm.verify_outgoing_email(TO, subject, body))


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
    a '>', not one '<' at a time: 200,000 of them are read at once.  The
    best of three runs, and a busy machine is allowed what a step at a time
    never has, ten times the text in under thirty times the time (a step at
    a time is a hundred; review rv_52c3: one run took over 1 s at 94% load)."""
    import time

    def best(text):
        runs = []
        for _ in range(3):
            began = time.perf_counter()
            agent_tools.unfilled_text_placeholder(text, braces=True)
            agent_tools.unfilled_text_placeholder(text)
            runs.append(time.perf_counter() - began)
        return min(runs)
    for unit, count in (('<', 200_000), ('<a', 100_000)):
        whole = best(unit * count + '>')
        assert whole < 1.0 or whole < 30 * best(unit * (count // 10) + '>'), unit
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
    # an unknown tag holds what follows it, as an element would; libxml2
    # nests the next in it, the inner one named first at one place
    ('Hi', '<pre-header><your message here>', '<your message here>'),
    ('Hi', '<p><your intro> <your offer></p>', '<your intro>'),
]


@pytest.mark.parametrize('subject, body', REAL_MARKUP_LEAVES)
def test_real_markup_is_no_blank(subject, body):
    assert no_blank(crm.verify_outgoing_email(TO, subject, body))


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
    # brackets in text that are no blank ("Map&lt;String, Integer&gt;" is
    # code in prose now: CODE_IN_PROSE_REFUSED)
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


# -- one reading by libxml2 (rv_1a81; the lxml design, hartos-e1) ---------
# The body is read from the tree libxml2 builds, the parser the reviews
# judge with, not by hand-written tag rules; one rule tells a blank's shape
# in the body and the subject (_placeholder_shaped).

def _unreadable(reason):
    return {'success': False, 'via': 'verification',
            'error': f'this node cannot check the email for blanks: {reason}'}


def test_a_node_without_lxml_refuses_every_email_and_sends_none(channel, monkeypatch):
    """lxml is imported in the check: without it the email is refused,
    named, and nothing reaches the channel (hartos-e1 #1)."""
    import sys
    monkeypatch.setitem(sys.modules, 'lxml', None)

    refused = crm.verify_outgoing_email(TO, 'Hi', '<p>Hi Ana, thanks</p>')
    out = crm._send_email(TO, 'Hi', '<p>Hi Ana, thanks</p>')

    assert {k: refused[k] for k in ('success', 'via', 'error')} == _unreadable(
        'lxml is not installed')
    assert 'Install lxml' in refused['fix']
    assert out == refused
    assert channel == []


def test_a_parse_that_raises_refuses_the_email(monkeypatch):
    from lxml import etree

    def broken(*_a, **_k):
        raise etree.ParserError('boom')
    monkeypatch.setattr(etree, 'fromstring', broken)

    refused = crm.verify_outgoing_email(TO, 'Hi', '<p>Hi Ana, thanks</p>')

    assert refused['error'] == ('this node cannot check the email for blanks: '
                                'libxml2 could not parse its HTML (ParserError)')


@pytest.mark.parametrize('depth', [300, 1000])
def test_html_deeper_than_libxml2_reads_is_refused_fast(depth):
    """libxml2 stops at 256 elements deep and cuts the rest: a cut tree is
    never judged, so a blank at the bottom never leaves (hartos-e1 #2)."""
    import time
    body = '<div>' * depth + '<p><your message here></p>' + '</div>' * depth
    began = time.perf_counter()
    refused = crm.verify_outgoing_email(TO, 'Hi', body)
    assert time.perf_counter() - began < 1.0
    assert refused['error'] == ('this node cannot check the email for blanks: its '
                                'HTML nests more elements than libxml2 reads (256)')


#: (subject, body, blank): inputs libxml2 refuses or cuts short as given.
AWKWARD_INPUT_REFUSED = [
    # an XML declaration in a str: read as UTF-8 bytes
    ('Hi', '<?xml version="1.0" encoding="utf-8"?><p>Hi Ana,</p><p>&lt;your message here&gt;</p>',
     '<your message here>'),
    # a NUL in a tag stopped libxml2 reading the rest
    ('Hi', '<p>Hi</p><p><your\x00 message here></p><p>Thanks</p>', '<your message here>'),
    ('Hi', '<p>Hi\x00 there</p><p>&lt;your message here&gt;</p>', '<your message here>'),
    ('Hi', '<p>Hi\x01\x0b there</p><p>&lt;your message here&gt;</p>', '<your message here>'),
    ('Hi', '<p>Hi \ud83d there</p><p>&lt;your message here&gt;</p>', '<your message here>'),
    ('Hi', '<meta charset="iso-8859-1"><p>Café</p><p>&lt;first name&gt;</p>', '<first name>'),
    # a start tag over several lines (libxml2 gives the line it ends on)
    ('Hi', '<p>Hi</p>\n<td\n style="x"\n width="1"\n><your-note></td>', '<your-note>'),
    ('Hi', '<p>Hi</p>\n<p><first\nname></p>', '<first\nname>'),
    # a '<' that opens no tag is text, as HTML5 reads it (libxml2 2.11
    # dropped one that starts the email: "<2 lines>" read "2 lines>")
    ('Hi', '<2 lines>', '<2 lines>'),
    ('Hi', '< Your Name >', '< Your Name >'),
    ('Hi', '<p>&lt;&lt;first name&gt; thanks</p>', '<first name>'),
    # ... in what a conditional comment holds too
    ('Hi', '<p>Hi</p><!--[if mso]><2 lines><![endif]-->', '<2 lines>'),
    # an HTML tag in an svg ends it for all that follows, as HTML5 does
    ('Hi', '<p>Hi</p><svg viewBox="0 0 1 1"><img src="x"><your-note>', '<your-note>'),
    # ... and one libxml2 drops, where it is written
    ('Hi', '<p>Hi</p><math></p><path d="M0 0"/><why you ask><br>', '<why you ask>'),
    ('Hi', '<svg viewBox="0 0 1 1"><body><your-message-here>', '<your-message-here>'),
    # an svg ends at its own end tag, as written (libxml2 kept it open past
    # it with a <td> open inside); a nested one is counted
    ('Hi', '<p>Hi</p><svg viewBox="0 0 1 1"><td nowrap>x</svg><why you ask>', '<why you ask>'),
    ('Hi', '<p>Hi</p><svg><svg/><td nowrap>x</svg><why you ask>', '<why you ask>'),
    # an email of one [if mso] comment: Outlook shows what it holds
    ('Hi', '<!--[if mso]><p>Hi</p><p><2 lines></p>', '<2 lines>'),
    # "</p>" ends a line where none is open, and "</br>" is a "<br>", as
    # HTML5 reads them (libxml2 drops them and glued the blank to "Hi")
    ('Hi', '<div>&lt;first name&gt;</p>Hi</div>', '<first name>'),
    ('Hi', '<p>Hi</p><insert photo></p>Hi', '<insert photo>'),
    ('Hi', '<p>Hi &lt;x&gt;</br>there</p>', '<x>'),
    # a blank on its own line is not glued to the line before it
    ('Hi', '<p>Hi<br>[A_B]</p>', '[A_B]'),
    ('Hi', '<p>Hi<br>[first_name], welcome</p>', '[first_name]'),
    ('Hi', '<div>Hi</div>[COMPANY_NAME]', '[COMPANY_NAME]'),
    # all its line shows, beside a tag no reader sees
    ('Hi', '<p>Hi</p><p>&lt; first name &gt;<path d="M0 0"/></p>', '< first name >'),
    # a code block ends at its own end tag, as written (libxml2 kept it
    # open past it with a <div> inside)
    ('Hi', '<p>Hi</p><code><div>x</code><your message here>', '<your message here>'),
    # the ask hint's blank written as a tag in a code block
    ('Hi', '<p>Hi</p><pre><why you ask></pre>', '<why you ask>'),
    # a tag is named as written where its element is: not a commented-out
    # one before it, nor a later one for a <p> the parser added
    ('Hi', '<!-- <your note> -->\n<p>Hi</p><p><your offer></p>', '<your offer>'),
    ('Hi', 'Hi\n<p>x</p>\n<p your note></p>', '<p your note>'),
    ('Hi', 'Hi\n<p your note></p>', '<p your note>'),
    # a tag alone in its line by the memory keys' rule ("!"), and two in an
    # email that shows nothing
    ('Hi', '<p>Hi</p><p><your note!></p>', '<your note!>'),
    ('Hi', '<p><your note!><your offer!></p>', '<your note!>'),
    # an [if mso] before any element of the email
    ('Hi', '<!--[if mso]><p>[COMPANY_NAME]</p><![endif]--><p>Hi</p>', '[COMPANY_NAME]'),
    # a body that is one tag written for a value
    ('Hi', '<your message here>', '<your message here>'),
    ('Hi', '  <first-name>  ', '<first-name>'),
]


@pytest.mark.parametrize('subject, body, blank', AWKWARD_INPUT_REFUSED)
def test_input_libxml2_takes_badly_is_still_read_whole(subject, body, blank):
    assert crm.verify_outgoing_email(TO, subject, body) == refusal(subject, blank)


def test_an_empty_body_and_awkward_clean_input_leave_with_their_words():
    for body in ('', '   \n ', '<!-- only a note -->'):
        assert crm.verify_outgoing_email(TO, 'Hi', body) == NO_TEXT
        assert agent_tools.email_reader_text(body) == ''
    assert agent_tools.email_reader_text(
        '<?xml version="1.0" encoding="utf-8"?><p>Hi Ana,</p>') == 'Hi Ana,'
    assert agent_tools.email_reader_text('<p>Hi</p><p>a\x00b</p><p>Thanks</p>') == 'HiabThanks'
    assert agent_tools.email_reader_text('<p>Hi \ud83d</p>') == 'Hi �'


#: (subject, body, blank): a name of the standard shown in brackets is a
#: blank; only formatting tags shown are markup (hartos-e1 #4).
SHOWN_NAMES_REFUSED = [
    ('Hi', '<p>Meet at &lt;address&gt; on &lt;date&gt; at &lt;time&gt;</p>', '<address>'),
    ('Hi', '<p>&lt;caption&gt;</p>', '<caption>'),
    ('Hi', '<p>Your &lt;label&gt; is ready</p>', '<label>'),
]


@pytest.mark.parametrize('subject, body, blank', SHOWN_NAMES_REFUSED)
def test_a_name_of_the_standard_shown_in_brackets_never_leaves(subject, body, blank):
    assert crm.verify_outgoing_email(TO, subject, body) == refusal(subject, blank)


#: (subject, body, blank): code is no exception in a subject or in a body's
#: prose; its place is inside <code> (hartos-e1 #5).
CODE_IN_PROSE_REFUSED = [
    ('Dear<FirstName>,', '<p>hi</p>', '<FirstName>'),
    ('Hi', '<p>Dear&lt;FirstName&gt;,</p>', '<FirstName>'),
    ('Hi', '<p>Hi&lt;first_name&gt; there</p>', '<first_name>'),
    ('Hi', '<p>Use Map&lt;String, Integer&gt; here</p>', '<String, Integer>'),
    ('hello', '<p>std::vector&lt;my_type&gt; is a type</p>', '<my_type>'),
    ('Hi', '<p>Use Box&lt;dyn Error&gt; in main.</p>', '<dyn Error>'),
    ('Hi', '<p>Write &lt;T extends Base&gt; for the bound.</p>', '<T extends Base>'),
    ('Hi', '<p>Hi <first_name>, welcome</p>', '<first_name>'),
]


@pytest.mark.parametrize('subject, body, blank', CODE_IN_PROSE_REFUSED)
def test_code_in_prose_or_a_subject_is_held_to_the_rule(subject, body, blank):
    assert crm.verify_outgoing_email(TO, subject, body) == refusal(subject, blank)


#: (blank, whether its refusal says code's place is <code>): only a blank
#: that reads as code (review rv_52c3: "<first name>" was told so too).
CODE_HINT = [('<String, Integer>', True), ('<T>', True), ('<FirstName>', True),
             ('<std::string>', True), ('<first name>', False), ('<first_name>', False),
             ('<dyn Error>', False), ('<your message here>', False)]


@pytest.mark.parametrize('blank, hinted', CODE_HINT)
def test_the_code_hint_is_given_for_code_only(blank, hinted):
    refused = crm.verify_outgoing_email(TO, 'Hi', f'<p>Use {html.escape(blank)} here</p>')
    assert blank in refused['error']
    assert refused['fix'].endswith('If this is code, put it inside <code>.') == hinted


#: (subject, body): code inside <code> or <pre> keeps its brackets.
CODE_IN_CODE_LEAVES = [
    ('Hi', '<p>Use <code>Box&lt;dyn Error&gt;</code> in main.</p>'),
    ('Hi', '<pre>Map&lt;String, Integer&gt; m;</pre>'),
]


@pytest.mark.parametrize('subject, body', CODE_IN_CODE_LEAVES)
def test_code_inside_code_leaves(subject, body):
    assert crm.verify_outgoing_email(TO, subject, body) is None


#: (subject, body, blank): the ask hint's blanks in a line, written as a
#: tag, in the subject and as the whole body (hartos-e1 #6).
HINT_BLANKS_REFUSED = [
    ('Hi', '<p>Q: &lt;your question?&gt; thanks</p>', '<your question?>'),
    ('Hi', '<p>Q: <your question?> thanks</p>', '<your question?>'),
    ('Quick question: <your question?>', '<p>hi</p>', '<your question?>'),
    ('Hi', '&lt;your question?&gt;', '<your question?>'),
    ('Hi', '<your question?>', '<your question?>'),
    ('Hi', '<p>Because &lt;why you ask&gt; we ask</p>', '<why you ask>'),
    ('Hi', '<p>Because <why you ask> we ask</p>', '<why you ask>'),
    ('About <why you ask>', '<p>hi</p>', '<why you ask>'),
    ('Hi', '&lt;why you ask&gt;', '<why you ask>'),
    ('Hi', '<why you ask>', '<why you ask>'),
]


@pytest.mark.parametrize('subject, body, blank', HINT_BLANKS_REFUSED)
def test_the_ask_hints_blanks_never_leave_anywhere(subject, body, blank):
    assert crm.verify_outgoing_email(TO, subject, body) == refusal(subject, blank)


#: (subject, body, blank): a tag written in a sentence for a value, which
#: the reader sees as a gap ("Hi , thanks"; hartos-e1 #7).
RAW_IN_A_SENTENCE_REFUSED = [
    ('Hi', '<p>Hi <first name>, thanks for your time.</p>', '<first name>'),
    ('Hi', '<p>Dear <name>, hi</p>', '<name>'),
    ('Hi', '<p>We help <company> grow.</p>', '<company>'),
    ('Hi', '<p>Our <product/service> saves time.</p>', '<product/service>'),
    ('Hi', '<p>Meet at <address> on <date></p>', '<address>'),
    ('Hi', '<p>Meet at <address> today</p>', '<address>'),
    ('Hi', '<p>Your <time> slot is held</p>', '<time>'),
]


@pytest.mark.parametrize('subject, body, blank', RAW_IN_A_SENTENCE_REFUSED)
def test_a_tag_written_for_a_value_in_a_sentence_never_leaves(subject, body, blank):
    assert crm.verify_outgoing_email(TO, subject, body) == refusal(subject, blank)


#: (subject, body): ordinary prose with brackets (review rv_1a81's corpus:
#: refused by 1a81ce6ad, sent by both parents), a bracketed www. link, and
#: real elements a template writes too.
PROSE_BRACKETS_LEAVE = [
    ('Hi', '<p>Home &lt;- Settings -&gt; Profile is the path back.</p>'),
    ('Hi', '<p>If cost &lt;= budget =&gt; approve.</p>'),
    ('Hi', '<p>Use x &lt;= 10 -&gt; y for the cap.</p>'),
    ('Hi', '<p>x&lt;y and y&gt;z, so x&lt;z.</p>'),
    ('Hi', '<p>since 2&lt;3 and 5&gt;4 the order holds.</p>'),
    ('Hi', '<p>If a&lt;b then b&gt;a.</p>'),
    ('Hi', '<p>if p&lt;0.05 and q&gt;0.9 we ship.</p>'),
    ('Hi', '<p>if (a&lt;b &amp;&amp; c&gt;d) return;</p>'),
    ('Hi', '<p>Call fetch&lt;User[]&gt;(url).</p>'),
    ('Hi', '<p>Java: Comparable&lt;? super T&gt;</p>'),
    ('Hi', '<p>Close every &lt;p&gt; with &lt;/p&gt;.</p>'),
    ('Hi', '<p>The &lt;!DOCTYPE html&gt; line comes first.</p>'),
    ('Hi', '<p>Comments look like &lt;!-- note --&gt;.</p>'),
    ('Hi', '<p>Visit &lt;www.example.com&gt; for details.</p>'),
    ('Re: <Dr. Smith>', '<p>hi</p>'),
    ('Hi', '<p>Meet at <address>12 Main St</address> today</p>'),
    ('Hi', '<p>Keep the file size &lt;2MB and the width &gt;600px.</p>'),
    ('Hi', '<p>Growth was &lt;10% this year, &gt; 5% next.</p>'),
    ('Hi', '<p>Check &lt;x == y&gt; first.</p>'),
]


@pytest.mark.parametrize('subject, body', PROSE_BRACKETS_LEAVE)
def test_ordinary_prose_with_brackets_leaves(subject, body):
    assert crm.verify_outgoing_email(TO, subject, body) is None


#: (subject, body, blank): the parser folds a written </body> or </html>
#: into the open document, so what follows it is still read.
AFTER_AN_EARLY_END_REFUSED = [
    ('Hi', '<p>Hi</p></body></html><p>&lt;your message here&gt;</p>', '<your message here>'),
    ('Hi', '<div><p>Hi</p></body><p><your message here></p></div>', '<your message here>'),
]


@pytest.mark.parametrize('subject, body, blank', AFTER_AN_EARLY_END_REFUSED)
def test_a_blank_after_an_early_end_tag_never_leaves(subject, body, blank):
    assert crm.verify_outgoing_email(TO, subject, body) == refusal(subject, blank)


#: (subject, body): read as the reader sees it.  "x&#65;[PROMO_CODE]"
#: shows as xA[PROMO_CODE], glued to a word: code in a code block
#: ("os.environ[API_KEY]"), and lowercase is code in prose too
#: ("my_list[item_index]").
READ_AS_SHOWN_LEAVES = [
    ('Offer', '<p>Use <code>x&#65;[PROMO_CODE]</code></p>'),
    ('Hi', '<p>my_list[item_index] in code</p>'),
    # a tag of no element with a name=value is markup alone in its line
    # (review rv_f32b's must-send shapes)
    ('Hi', '<p>Hi</p><p><path d="M0 0"/></p>'),
    # a code tag left open with code after it, glued to it as the reader sees
    ('Hi', '<div><code>x = 1</div><div>Thanks</div>'),
    # inside an svg, still, after a nested svg's end
    ('Hi', '<p>Hi</p><svg><svg></svg><your note></svg>'),
]


@pytest.mark.parametrize('subject, body', READ_AS_SHOWN_LEAVES)
def test_what_the_tree_shows_as_no_blank_leaves(subject, body):
    assert crm.verify_outgoing_email(TO, subject, body) is None


# -- review rv_52c3 of 52c3be2b3 ------------------------------------------
# HTML5's named references; a blank in capitals glued to a word in prose;
# an email with nothing to read; the owner's rule for one-word blanks
# (2026-11-23, "Block them"; review rv_52c3 of 02d098595).

#: The refusal of an email with nothing to read.
NO_TEXT = {'success': False, 'via': 'verification', 'error': 'the email has no text',
           'fix': ('Write what the email says to its reader, then send it again. An '
                   'email with nothing to read never leaves.')}


def no_blank(refused) -> bool:
    """No blank named: the email leaves, or, holding only markup, is
    refused for having nothing to read (review rv_52c3)."""
    return refused is None or refused == NO_TEXT

#: (subject, body, blank): names HTML5 decodes and libxml2 2.11 does not
#: know; every reader shows the blank (each was sent on the desktop).
HTML5_NAMED_REFUSED = [
    ('Hi', '<p>Dear &lbrack;COMPANY_NAME&rbrack; team</p>', '[COMPANY_NAME]'),
    ('Hi', '<p>Dear &lsqb;COMPANY_NAME&rsqb; team</p>', '[COMPANY_NAME]'),
    ('Hi', '<p>Dear &lcub;first_name&rcub;,</p>', '{first_name}'),
    ('Hi', '<p>Dear &lbrace;first_name&rbrace;,</p>', '{first_name}'),
    ('Hi', '<p>Hi &LT;first_name&GT;,</p>', '<first_name>'),
    ('Hi', '<p>Hi &LT;first name&GT;, thanks</p>', '<first name>'),
    ('Hi', '<p>Use code &lcub;SAVE10&rcub; today</p>', '{SAVE10}'),
    # with no ';', the longest name HTML5 knows bare ("&lt")
    ('Hi', '<p>Hi &ltfirst_name&gt;,</p>', '<first_name>'),
]


@pytest.mark.parametrize('subject, body, blank', HTML5_NAMED_REFUSED)
def test_an_html5_named_reference_is_read_as_every_reader_reads_it(subject, body, blank):
    assert crm.verify_outgoing_email(TO, subject, body) == refusal(subject, blank)


def test_a_reference_written_out_as_text_stays_text():
    """Read left to right, "&amp;lbrack;" is "&" and the text "lbrack;"."""
    body = '<p>Write &amp;lbrack;COMPANY_NAME&amp;rbrack; to show a bracket</p>'
    assert crm.verify_outgoing_email(TO, 'Hi', body) is None
    assert agent_tools.email_reader_text(body) == (
        'Write &lbrack;COMPANY_NAME&rbrack; to show a bracket')


#: (subject, body, blank): a blank in capitals glued to a word is code only
#: in a code block; in prose every reader sees the blank.
GLUED_CAPS_IN_PROSE_REFUSED = [
    ('hello', '<p>x&#65;[COMPANY_NAME]</p>', '[COMPANY_NAME]'),
    ('Hi', '<p>Hi</p><svg><desc>d</desc><text>[FIRST_NAME]</text></svg>', '[FIRST_NAME]'),
    ('Hi', '<p>Hi Ana,</p><p>Dear[FIRST_NAME],</p>', '[FIRST_NAME]'),
    ('Hi', '<p>Thanks, Sam from[COMPANY_NAME]</p>', '[COMPANY_NAME]'),
    ('Hi', '<p>Hi Ana,</p><p><b>Re</b>[COMPANY_NAME] proposal</p>', '[COMPANY_NAME]'),
    ('Hi', '<p>Hi Ana (from[COMPANY_NAME])</p>', '[COMPANY_NAME]'),
    ('Hi', '<p>Hi Ana, Order 12[ORDER_ID]</p>', '[ORDER_ID]'),
]


@pytest.mark.parametrize('subject, body, blank', GLUED_CAPS_IN_PROSE_REFUSED)
def test_a_glued_blank_in_capitals_in_prose_never_leaves(subject, body, blank):
    assert crm.verify_outgoing_email(TO, subject, body) == refusal(subject, blank)


#: Bodies with nothing to read (review rv_52c3: each was sent).
NO_TEXT_BODIES = ['</pre>', '', '   ', '<p></p>', '<br>', '<img src="x.png">',
                  '<p>&nbsp;</p>', '<!-- only a note -->',
                  '<table><tr><td><o:p></o:p></td></tr></table>',
                  # tags with a name=value only: markup, not a blank left for a value
                  '<path d="M0 0"/><mspace width="1em"/>']


@pytest.mark.parametrize('body', NO_TEXT_BODIES)
def test_an_email_with_nothing_to_read_never_leaves(body):
    assert crm.verify_outgoing_email(TO, 'Hi', body) == NO_TEXT


def test_an_email_with_nothing_to_read_is_not_sent(channel):
    assert crm._send_email(TO, 'Hi', '<p></p>') == NO_TEXT
    assert channel == []


#: (subject, body, blank): the owner's rule, 2026-11-23 ("Block them"):
#: a word in capitals in square brackets, a merge tag and a common field
#: name are blanks, whoever writes them, in the body and in the subject,
#: as the reader sees them (character references decoded).  These are the
#: 29 emails review rv_52c3 saw sent by 02d098595, as it wrote them: the
#: first is its ordinary email with "[PDF]", and three bodies come twice
#: (once from its blank set, once from its owner's-rule set).
OWNERS_BLANKS_REFUSED = [
    ('Hello', '<div style="font-family:Arial">Hi Ana,</div><div>The [PDF] brochure is '
              'attached.</div><div>-- Sam</div>', '[PDF]'),
    ('Hello', '<p>Dear &#91;NAME&#93;,</p>', '[NAME]'),
    ('Hello', '<p>Name:[NAME]</p>', '[NAME]'),
    ('Hello', '<p>[NAME], thanks for the call.</p>', '[NAME]'),
    ('Hello', '<p>Hi [First Name], thanks.</p>', '[First Name]'),
    ('Hello', '<p>Hi [NAME], thanks.</p>', '[NAME]'),
    ('Hello', '<p>Hi %FIRSTNAME%, thanks.</p>', '%FIRSTNAME%'),
    ('Hello', '<p>Hi Ana,</p><ul><li>Price: [PRICE]</li></ul>', '[PRICE]'),
    # "&lt" and "&gt" with no ';' and a space: the reader sees "< first_name>"
    ('Hello', '<p>Hi &lt first_name&gt ,</p>', '< first_name>'),
    ('Hello', '<p>Hi [NAME], thanks.</p>', '[NAME]'),
    ('Hello', '<p>Price: [PRICE]</p>', '[PRICE]'),
    ('Hello', '<p>Hi %FIRSTNAME%, thanks.</p>', '%FIRSTNAME%'),
    ('Hello', '<p>Hi %first_name%, thanks.</p>', '%first_name%'),
    ('Hello', '<p>Hi [First Name], thanks.</p>', '[First Name]'),
    ('Hello', '<p>Hi [Name], thanks.</p>', '[Name]'),
    ('Hello', '<p>Dear [Company Name] team,</p>', '[Company Name]'),
    ('Hello', '<p>Dear [Company] team,</p>', '[Company]'),
    ('Hello', '<p>Regards,<br>[Your Name]</p>', '[Your Name]'),
    ('Hello', '<p>[UPDATE] Our pricing changed.</p>', '[UPDATE]'),
    ('Hello', '<p>Hi Ana, [COMPANY] is ready.</p>', '[COMPANY]'),
    ('Hello', '<p>Hi Ana, reply by [DATE].</p>', '[DATE]'),
    ('Hello', '<p>Hi Ana, call [PHONE].</p>', '[PHONE]'),
    ('Hello', '<p>Hi Ana, [LINK]</p>', '[LINK]'),
    ('Hello', '<p>Hi [FIRSTNAME],</p>', '[FIRSTNAME]'),
    ('Hello', '<p>Hi *|FNAME|*,</p>', '*|FNAME|*'),
    ('Hi [NAME]', '<p>Hi Ana, thanks for the call.</p>', '[NAME]'),
    ('[UPDATE] pricing', '<p>Hi Ana, thanks for the call.</p>', '[UPDATE]'),
    ('Hi %FIRSTNAME%', '<p>Hi Ana, thanks for the call.</p>', '%FIRSTNAME%'),
    ('For [First Name]', '<p>Hi Ana, thanks for the call.</p>', '[First Name]'),
]

#: (subject, body, blank): the same rule in the other places and spellings a
#: template leaves it: the spaced "&lt" form in a subject, character
#: references, a field name's other spellings, an image's alt text, a
#: link, a code block (its other brackets stay code).
OWNERS_OTHER_BLANKS_REFUSED = [
    ('Hi &lt first_name&gt', '<p>Hi Ana, thanks</p>', '< first_name>'),
    ('&#91;UPDATE&#93; pricing', '<p>Hi Ana</p>', '[UPDATE]'),
    ('For [Your Name]', '<p>Hi Ana</p>', '[Your Name]'),
    ('Hi *|FNAME|*', '<p>Hi Ana</p>', '*|FNAME|*'),
    ('Hi', '<p>Hi Ana, &lsqb;UPDATE&rsqb; pricing</p>', '[UPDATE]'),
    ('Hi', '<p>Hi &#37;FIRSTNAME&#37;,</p>', '%FIRSTNAME%'),
    ('Hi', '<p>Hi [name], thanks</p>', '[name]'),
    ('Hi', '<p>Hi [FirstName],</p>', '[FirstName]'),
    ('Hi', '<p>Dear[First Name],</p>', '[First Name]'),
    ('Hi', '<p>Hi [ first_name ],</p>', '[ first_name ]'),
    ('Hi', '<p>Your order [ order_id ] ships today</p>', '[ order_id ]'),
    ('Hi', '<p>Hi &lt; first name &gt;, thanks</p>', '< first name >'),
    ('Hi', '<p>Dear [Your Company] team</p>', '[Your Company]'),
    ('Hi', '<p>Dear [Clinic Name] team</p>', '[Clinic Name]'),
    ('Hi', "<p>Dear [the client's name],</p>", "[the client's name]"),
    ('Hi', '<p>Book here: [Booking Link]</p>', '[Booking Link]'),
    ('Hi', '<p>Hi Ana, [insert testimonial here]</p>', '[insert testimonial here]'),
    ('Hi', '<p>Hi *|FNAME|*, see *|MC:SUBJECT|*</p>', '*|FNAME|*'),
    ('Hi', '<p>Hi Ana, <a href="https://x.example/?n=%FIRSTNAME%">book</a></p>',
     '%FIRSTNAME%'),
    ('Hi', '<p>Hi Ana,</p><img src="https://x.example/a.png" alt="[COMPANY] logo">',
     '[COMPANY]'),
    ('Hi', '<p>Use <code>[NAME]</code> at the top</p>', '[NAME]'),
]


@pytest.mark.parametrize('subject, body, blank',
                         OWNERS_BLANKS_REFUSED + OWNERS_OTHER_BLANKS_REFUSED)
def test_the_owners_blanks_never_leave(subject, body, blank):
    assert crm.verify_outgoing_email(TO, subject, body) == refusal(subject, blank)


#: (subject, body): brackets that are no blank, as the owner's rule keeps
#: them ("still allowing [1], [sic] and similar"): citations, years, one
#: letter, an editor's words, a word in brackets that is neither capitals
#: nor a field name, a percent sign, a link's percent-encoding, code's own
#: "%PATH%" and brackets, a style sheet's selectors.
OWNERS_RULE_LEAVES = [
    ('Hello', '<p>Hi Ana, as noted [1], growth is real.</p>'),
    ('Hello', '<p>Hi Ana, "their [sic] clinic" was the quote.</p>'),
    ('Hello', '<p>Hi Ana, sources [2][3] agree.</p>'),
    ('Hello', '<p>Hi Ana, our [Webinar] recording is up.</p>'),
    ('Hello', '<p>Hi Ana, the [2023] report and the [2024-25] plan.</p>'),
    ('Hello', '<p>Hi Ana, see appendix [A] and table [B].</p>'),
    ('Hello', '<p>Hi Ana, Q3 [Q3] closed; option [x] is ticked, [ ] is not.</p>'),
    ('Hello', '<p>Hi Ana, "we loved [the company] and its team" [emphasis added].</p>'),
    ('Hello', '<p>Hi Ana, he said [...] it works [laughs].</p>'),
    ('Hello', '<p>Hi Ana, 50% off and 20% more this week; 100%sure.</p>'),
    ('Hello', '<p>Hi Ana, the page https://x.example/caf%C3%A9s%C3%A0 and '
              '<a href="https://x.example/r%C3%A9sum%C3%A9?q=a%20b">this one</a>.</p>'),
    ('Hello', '<p>Hi Ana, set it with <code>echo %PATH%</code> on Windows.</p>'),
    ('Hello', '<p>Hi Ana, the form posts user[name] and row[date] as code does.</p>'),
    ('Hello', '<pre>rows = arr[N]\nkeys = [1, 2]</pre><p>Hi Ana</p>'),
    ('Hello', '<p>Hi Ana,</p><style>[data-ogsc] a { color: #fff } [name] { x: 1 }</style>'),
    ('Study [1] results', '<p>Hi Ana, thanks for the call.</p>'),
    ('Re: our call', '<p>Hi Ana, thanks for the call.</p>'),
    ('Q&A on Friday', '<p>Hi Ana, thanks for the call.</p>'),
    ('Fish & chips', '<p>Hi Ana, thanks for the call.</p>'),
    ('[Hevolve] your demo, 50% off', '<p>Hi Ana, thanks for the call.</p>'),
]


@pytest.mark.parametrize('subject, body', OWNERS_RULE_LEAVES)
def test_brackets_the_owner_allows_still_leave(subject, body):
    assert crm.verify_outgoing_email(TO, subject, body) is None


def _wrapped(inner, kind):
    """rv_52c3's three ordinary email frames around one sentence."""
    if kind == 0:
        return f'<html><body><p>Hi Ana,</p><p>{inner}</p><p>Thanks,<br>Sam</p></body></html>'
    if kind == 1:
        return (f'<table role="presentation" width="100%"><tr><td style="padding:12px">'
                f'Hi Ana,<br><br>{inner}<br><br>Best,<br>Sam</td></tr></table>')
    return f'<div style="font-family:Arial">Hi Ana,</div><div>{inner}</div><div>-- Sam</div>'


#: rv_52c3's own ordinary sentences, each in one of its frames (its 40th,
#: "The [PDF] brochure is attached.", is a blank by the owner's rule and
#: is OWNERS_BLANKS_REFUSED's first row).
_ORDINARY_SENTENCES = [
    'Navigate Home &lt;- Settings -&gt; Profile to change it.',
    'Flow: lead -&gt; demo -&gt; trial -&gt; paid.',
    'Our cost is &lt; 2% of revenue and churn is &gt; 5% lower.',
    'Response time went from 900ms to &lt;200ms.',
    'If x &lt; y and y &gt; z then x &lt; z, as you said.',
    'We love it &lt;3 and so does the team.',
    'Rated &gt;= 4.5 by 1,200 users; refunds &lt;= 1%.',
    'Margins: A&gt;B, B&gt;C.',
    'The formula is 2&lt;3 and 5&gt;4, nothing more.',
    'Pricing =&gt; $49/month, billed yearly.',
    'Reply to &lt;sam@clinic.example&gt; if easier.',
    'See &lt;https://hevolve.ai/pricing&gt; for plans.',
    'Our newsletter &lt;&lt;Weekly Wins&gt;&gt; is out.',
    'He wrote: «we are ready» and “let us go”.',
    'Revenue up 40% YoY 🚀 and NPS 72 😀.',
    'நன்றி! உங்கள் கிளினிக்கிற்கு வாழ்த்துக்கள்.',
    'धन्यवाद, हम सोमवार को मिलेंगे।',
    '谢谢您的回复，我们下周见。',
    'Merci beaucoup \u2014 à bientôt, l’équipe.',
    'See note [1] and appendix [A].',
    None,           # 'The [PDF] brochure is attached.' (a blank now)
    'Our [Webinar] on Tuesday covers onboarding.',
    'Use <code>Map&lt;String, Integer&gt;</code> for the cache.',
    'Run <code>npm install --save</code> first.',
    'The API returns <code>{"status": "ok"}</code>.',
    'Q3 results: revenue 1.2M, costs 0.8M, margin 33%.',
    'Call me at +44 20 7946 0958 or 9:30–11:00.',
    'Our address: 221B Baker St, London NW1 6XE.',
    'The ratio a:b is 3:2, i.e. 60/40.',
    'Steps 1 -&gt; 2 -&gt; 3 take about 5 minutes.',
    'Use Ctrl+C / Ctrl+V to copy.',
    'A &amp; B Partners signed last week.',
    'Price: $5 &lt; $10 &lt; $15 tiers.',
    'Speech therapy sessions (45 min) \u2014 Mon/Wed/Fri.',
    'Home &lt;-&gt; Office sync is instant.',
    'Old flow: form -&gt; email -&gt; call &lt;- slow.',
    'In C++, write std::vector for lists.',
    'Use <b>bold</b>, <i>italics</i> and <u>underline</u> sparingly.',
    'Visit <a href="https://hevolve.ai">our site</a> for details.',
    'The 2-3 day turnaround still holds.',
]
#: (subject, body): rv_52c3's 62 other ordinary emails (with "[PDF]" its
#: 63; none newly refused) and its 20 short and image emails.
ORDINARY_EMAILS_LEAVE = [
    ('Quick question about your clinic', _wrapped(sentence, at % 3))
    for at, sentence in enumerate(_ORDINARY_SENTENCES) if sentence
] + [
    ('Q3 update: revenue -> up 40%', _wrapped('Details below.', 0)),
    ('A/B test results <3', _wrapped('Details below.', 0)),
    ('Re: x < y?', _wrapped('Yes, x is smaller.', 0)),
    ('Welcome to Hevolve!', '<!--[if mso]><table><tr><td width="600"><![endif]-->'
     '<div style="max-width:600px"><p>Hi Ana,</p><p>Welcome aboard.</p></div>'
     '<!--[if mso]></td></tr></table><![endif]-->'),
    ('Your demo', '<!--[if mso]><v:rect xmlns:v="urn:schemas-microsoft-com:vml" '
     'fill="true" style="width:600px"><v:fill type="tile" color="#fff"/><v:textbox '
     'inset="0,0,0,0"><![endif]--><div><p>Hi Ana, your demo is Tuesday.</p></div>'
     '<!--[if mso]></v:textbox></v:rect><![endif]-->'),
    ('Hello', '<html><head><title>Newsletter</title><style>.x{color:red}'
     ' p{margin:0}</style></head><body><p>Hi Ana, here is our monthly note.</p></body></html>'),
    ('Hello', '<p>Hi Ana,</p><svg width="10" height="10"><rect width="10" '
     'height="10"/></svg><p>Logo above.</p>'),
    ('Hello', '<p>Hi Ana,</p><img src="https://x.example/l.png" alt="Hevolve logo">'
     '<p>Best</p>'),
    ('Hello', 'Hi Ana,\n\nThanks for the call. Home <- Settings -> Profile is '
     'where it lives.\n\nSam'),
    ('Hello', 'Hi Ana,\n\nCosts are < 2% and growth > 30%.\n\nSam'),
    ('Hello', '<p>Hi Ana,</p><p>Our rule: if score &lt; 50 -&gt; review.</p>'),
    ('Hello', '<p>Hi Ana,</p><blockquote>&gt; On Mon, Sam wrote:<br>&gt; '
     'sounds good</blockquote>'),
    ('Hello', '<p>Hi Ana \u2014 the &lt;5 min setup is real.</p>'),
    ('Hello', '<p>Hi Ana, the <span style="color:#c00">red</span> line is '
     'the target.</p><hr><p>Unsubscribe <a href="https://x.example/u?id=42">here</a>.</p>'),
    ('Hello', '<p>Hi Ana,</p><ul><li>Fast</li><li>Private</li><li>'
     'Offline &gt; online</li></ul>'),
    ('Hello', '<p>Hi Ana, 1 &lt; 2 &lt; 3 is our tier ladder.</p>'),
    ('Hello', '<p>Hi Ana, our motto: &lt;&lt; less is more &gt;&gt;.</p>'),
    ('Hello', '<p>Hi Ana, temperatures &lt;-5°C are fine.</p>'),
    ('Hello', '<p>Hi Ana, write to us at &lt;support@hevolve.ai&gt;.</p>'),
    ('Hello', '<p>Hi Ana, as noted [1], growth is real.</p>'),
    ('Hello', '<p>Hi Ana, "their [sic] clinic" was the quote.</p>'),
    ('Hello', '<p>Hi Ana, sources [2][3] agree.</p>'),
    ('Hello', '<p>Hi Ana, our [Webinar] recording is up.</p>'),
] + [('Hello', body) for body in (
    '<img src="https://x.example/banner.png" alt="Hevolve"><p>Hi Ana, see you Tuesday.</p>',
    '<p>Thanks!</p>', '<p>OK</p>', '<p>Hi Ana 👋</p>', 'Thanks, Ana.',
    '<p><img src="https://x.example/l.png" alt="logo" width="80"></p><p>Quick note: the '
    'demo is live.</p>',
    '<table><tr><td><img src="https://x.example/a.png" alt=""></td><td>Hi Ana, 10 min '
    'call?</td></tr></table>',
    '<p>Hi Ana,<br>Yes.<br>Sam</p>', '<div>Got it 👍</div>',
    # an image alone whose alt text is its words (review rv_52c3: refused as
    # an email with nothing to read)
    '<p><a href="https://cal.example/sam"><img src="https://x.example/btn.png" '
    'alt="Book a call"></a></p>',
    '<p>Hi Ana \u2014 3 slots: Mon, Tue, Wed?</p>', '<p>நன்றி</p>', '<p>धन्यवाद</p>',
    '<p>Hi Ana, the PDF is attached.</p>', '<h2>Hevolve</h2><p>Hi Ana, welcome.</p>',
    '<p>Hi Ana,</p><p><img src="https://x.example/chart.png" alt="Q3 chart"></p><p>Q3 '
    'up 40%.</p>',
    '<p>Hi Ana, 5 &gt; 3.</p>', '<p>Re: pricing \u2014 $49.</p>',
    '<center><img src="https://x.example/l.png"><br>Hi Ana, thanks!</center>',
    '<p>Hi Ana, <i>see attached</i>.</p>')]


@pytest.mark.parametrize('subject, body', ORDINARY_EMAILS_LEAVE)
def test_ordinary_emails_still_leave(subject, body):
    assert crm.verify_outgoing_email(TO, subject, body) is None


def test_the_rule_reads_the_email_alone_on_any_thread(channel):
    """No agent's files are read for its blanks (review rv_52c3: a pooled
    thread's stale agent replaced the sender's, a worker thread had none,
    and a flow past the tenth was never read, so each sent the blank)."""
    import threading
    from hartos.threadlocal import thread_local_data
    out = []

    def send():
        thread_local_data.set_prompt_id('9002')       # a pooled thread's last agent
        try:
            out.append(crm._send_email(TO, 'Hi', '<p>Hi Ana, it costs [PRICE] a month</p>'))
        finally:
            thread_local_data.set_prompt_id(None)
    worker = threading.Thread(target=send)
    worker.start()
    worker.join(60)
    out.append(crm._send_email(TO, '[UPDATE] pricing', '<p>Hi Ana</p>'))

    assert out == [refusal('Hi', '[PRICE]'), refusal('[UPDATE] pricing', '[UPDATE]')]
    assert channel == []


def test_names_libxml2_knows_are_left_as_written():
    """The HTML4 names libxml2 2.11 reads itself are not rewritten (review
    rv_52c3: rewriting every one made a long email about 6x slower to
    read); HTML5's names and other cases still are."""
    assert agent_tools._parseable('a&amp;b&nbsp;c&mdash;d&lbrack;e&LT;f&lt g') == (
        'a&amp;b&nbsp;c&mdash;d&#91;e&#60;f&#60; g')
