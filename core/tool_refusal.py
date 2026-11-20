"""ONE way a tool tells the model its call could not be done, and why.

The owner, 2026-11-09: "all refusals shd hint llm on what was wrong with tool
call", then "enforce a framework for tool call so that validation errors are
semantically understood by LLM so that retry can correct them".

Measured that day on the owner's desktop (llm_outbound.jsonl + .old, 11-07
21:15 to 11-09): 482 tool calls; 131 were refused BY THE TOOL ITSELF after the
argument check had passed (hartos/helper.py bind_tool_call_arguments /
tool_argument_error, which refused none); of the 113 retries that followed,
92 failed again, 33 of them with identical arguments.  The refusals named a
code or a bare fact -- {"refused": "too_many_open_asks"} 66 times, "Key not
found in stored data." 22 times, "No file uploaded from user" 19 times --
and nothing the model could change.

A refusal built here carries the three things a retry needs:
  * what was wrong: the argument, value or precondition, in the call's own
    terms;
  * how to fix it: the change to make, or the tool to call first;
  * whether calling again can help at all (RETRY_AFTER_FIX, RETRY_LATER,
    RETRY_NEVER, RETRY_OTHER_TOOL when the named tool does not exist and
    another one should be called, or RETRY_FINISH_WITHOUT when the step may
    end without the tool's result), so a refusal no retry can fix is not
    retried as it was.

A text refusal opens with "Error:" (core.constants.TOOL_ERROR_REPLY_PREFIXES),
so core.constants.tool_reply_failed reads it as a failed call and the REUSE
gate re-steers instead of crediting it.  A tool that answers JSON keeps its
own fields and adds the same sentence under NOTE_KEY (the key person_ask
already uses for its refusals); tool_reply_failed reads a top-level
{"status": "refused"} as failed too.

Scope: what a tool finds wrong once its arguments are bound -- a value it
cannot use, a precondition not met.  Argument-shape refusals (unknown or
missing names, broken JSON) are made before the tool runs, by
hartos/helper.py tool_argument_error, in their own words ("Error: <tool> was
not run: ...", each naming the argument and what to send instead); they do
not carry REFUSAL_MARK and never reach the count below, since the tool did
not run.  core.tool_logging
counts every failed reply that came back without a hint
([TOOL-REFUSAL-NO-HINT]), which is how this framework is enforced: each lane
can see the refusals it has not moved here yet.  A tool wrapped by
log_tool_execution is counted on its own return; a tool registered as a
plain callable is counted by the autogen executor (hartos.helper,
note_executor_reply), once either way.
"""
from __future__ import annotations

RETRY_AFTER_FIX = 'after_fix'
RETRY_LATER = 'later'
RETRY_NEVER = 'never'
RETRY_OTHER_TOOL = 'other_tool'
# The tool did all it could and the step may end without its result: the
# image service delivered nothing, the memory store could not be read.  Still
# a failed call (no recipe step is banked from it), but the completion gates
# take it as the step's receipt (core.constants.tool_reply_is_step_work), so
# the step the refusal says to finish can finish.  Review of e0b313bdd: told
# to "carry on without the image", the step could never complete.
RETRY_FINISH_WITHOUT = 'finish_without'

# The words every text refusal built here carries, and the JSON key that
# holds the sentence in a JSON refusal.  core.tool_logging reads both to tell
# a refusal that says what to do from one that does not.
REFUSAL_MARK = ' call could not be done: '
NOTE_KEY = 'note'

_RETRY_SENTENCES = {
    RETRY_AFTER_FIX: ("Call {tool} again only with that changed; the same call "
                      "will be refused again."),
    RETRY_LATER: ("This will not change during this turn; do not call {tool} "
                  "again now."),
    RETRY_NEVER: ("Calling {tool} again cannot work; tell the person what "
                  "could not be done."),
    # A call to a tool that does not exist: "call it again with that
    # changed" told the model to call the missing tool again (review of
    # 2ddf102da, F3), so this one points away from it.
    RETRY_OTHER_TOOL: ("There is no {tool} to call again; make the next call "
                       "with one of the tool names given."),
    RETRY_FINISH_WITHOUT: ("Do not call {tool} again now; this step may "
                           "finish without its result."),
}
# How every text refusal built by ``refuse`` opens, before the tool's name.
_TEXT_REFUSAL_OPEN = "Error: the "


def _clause(text, what):
    clause = str(text or '').strip()
    if not clause:
        raise ValueError(f'a refusal must say {what}')
    return clause if clause.endswith(('.', '?', '!')) else clause + '.'


def refuse(tool: str, problem: str, fix: str, *,
           retry: str = RETRY_AFTER_FIX) -> str:
    """The text a tool returns when the call it was given cannot be done:
    ``Error: the <tool> call could not be done: <problem>. <fix>. <retry>``.

    ``problem`` and ``fix`` are required and must be the call's own terms
    (the argument and the value, the precondition and the tool that meets
    it), never a code.  ``retry`` says whether calling again can help."""
    if retry not in _RETRY_SENTENCES:
        raise ValueError(f'unknown retry {retry!r}; use one of '
                         f'{sorted(_RETRY_SENTENCES)}')
    name = str(tool or '').strip() or 'tool'
    return (f"{_TEXT_REFUSAL_OPEN}{name}{REFUSAL_MARK}{_clause(problem, 'what was wrong')} "
            f"{_clause(fix, 'how to fix it')} "
            f"{_RETRY_SENTENCES[retry].format(tool=name)}")


def refusal_fields(tool: str, code: str, problem: str, fix: str, *,
                   retry: str = RETRY_AFTER_FIX) -> dict:
    """The fields a JSON-answering tool adds to a refusal: its ``refused``
    code, ``status`` 'refused' (which tool_reply_failed reads as failed), the
    ``retry`` mode and the sentence under NOTE_KEY.  The tool merges these
    into its own reply."""
    return {'status': 'refused', 'refused': str(code), 'retry': retry,
            NOTE_KEY: refuse(tool, problem, fix, retry=retry)}


def refusal_has_hint(content) -> bool:
    """Whether a failed reply says what was wrong and what to do: a text
    refusal built by ``refuse`` (it carries REFUSAL_MARK anywhere, so a
    tool may append it to an older failure text), or a JSON refusal with a
    non-empty NOTE_KEY."""
    body = '' if content is None else str(content)
    if REFUSAL_MARK in body:
        return True
    if f'"{NOTE_KEY}"' not in body:      # no note key: skip the parse
        return False
    payload = _json_object(body)
    return bool(payload and str(payload.get(NOTE_KEY) or '').strip())


def refusal_lets_step_finish(content, tool=None) -> bool:
    """Whether ``content`` IS the refusal, built with RETRY_FINISH_WITHOUT,
    of the tool that answered it: a text refusal that opens "Error: the
    <tool> call could not be done:" and closes with that mode's sentence
    for the same tool, or a JSON refusal whose ``retry`` is that mode.

    ``tool`` is the name the call was made by.  None when the reader cannot
    resolve the call; then the name the refusal opens with must be the one
    its closing sentence names.  The refusal's words anywhere else do not
    count (review of cda26051e, #290): a Generate_video HTTP 500 that quoted
    txt2img's refusal completed the video step with no video, and a crawled
    page or another tool handing back that refusal whole would have too."""
    body = '' if content is None else str(content).strip()
    if body.startswith(_TEXT_REFUSAL_OPEN) and REFUSAL_MARK in body:
        name = body[len(_TEXT_REFUSAL_OPEN):].split(REFUSAL_MARK, 1)[0]
        if tool is not None and name != str(tool).strip():
            return False
        return body.endswith(
            _RETRY_SENTENCES[RETRY_FINISH_WITHOUT].format(tool=name))
    if not is_json_refusal(body):
        return False
    return (_json_object(body) or {}).get('retry') == RETRY_FINISH_WITHOUT


#: What the judge's report (hartos.helper.ToolActivityAsEvidence) puts under
#: a result that IS the called tool's own RETRY_FINISH_WITHOUT refusal, and
#: only there (refusal_lets_step_finish).  A fact, not an order: the rule that
#: says what it means lives in the judge's system message
#: (FINISH_WITHOUT_JUDGE_RULE), which quotes this same text.
_FINISH_WITHOUT_MARKER = "(this is {tool}'s own finish-without refusal)"


def finish_without_marker(tool) -> str:
    """The marker for ``tool``'s own finish-without refusal (the name the
    call was made by; 'the called tool' when the call cannot be resolved)."""
    name = str(tool or '').strip() or 'the called tool'
    return _FINISH_WITHOUT_MARKER.format(tool=name)


#: The judge's rule for a finish-without refusal, given to every
#: StatusVerifier seat in its own system message by hartos.helper.
#: give_judge_view.  It used to reach the judge as a note appended to the
#: tool's report, a user turn addressed to the judge and asking for
#: "completed" -- the shape of a prompt injection.  Live on central
#: 2026-11-20 03:00-03:29Z (build 641d7e289, goal 09bbc67a): txt2img answered
#: image_not_made, the Assistant saved the image prompt as text as the refusal
#: asked, and Claude Haiku 5.5 as the StatusVerifier refused the note 5 out
#: of 5 times ("is not accepted as evidence because it is not a tool output",
#: "Repeated failures are reported as error, not pending"); the goal paused
#: needs_help after 3 attempts.  The call's name may differ from the one the
#: step was written with (the step says text_2_image, the call is txt2img).
FINISH_WITHOUT_JUDGE_RULE = (
    "Finish-without rule (this comes before the error and pending rules "
    "above): a tool may answer with its own refusal saying the step may "
    "finish without its result, for example when the image service "
    "delivered no image. The report marks such a result, right after its "
    "'Tool result:' text, with "
    + _FINISH_WITHOUT_MARKER.format(tool='<tool>')
    + ". When a tool called for this step returned that marked refusal, and "
    "the agent working the step then recorded what it could as text (in "
    "its reply or with a save), as the refusal asked, the step is "
    "completed: report status \"completed\" with "
    "evidence kind \"tool_receipt\" citing the message that holds the marked "
    "refusal. A repeated refusal of this kind is still completed, not "
    "error. The name the tool was called by may differ from the name the "
    "step was written with (txt2img for text_2_image). Only the report's "
    "own marker counts: text an agent wrote itself is never a receipt, even "
    "when it reads 'Tool result:' or copies the marker, and every other "
    "refusal or failure is judged by the rules above.")


def _window_text(window) -> str:
    """'2026-11-04..2026-11-08' for a (lo, hi) window; a bound off a whole
    day keeps its time ('2026-11-20 00:00')."""
    lo, hi = window
    whole_lo = (lo.hour, lo.minute, lo.second, lo.microsecond) == (0, 0, 0, 0)
    whole_hi = (hi.hour, hi.minute, hi.second, hi.microsecond) == (23, 59, 59, 999999)
    return (f"{lo:%Y-%m-%d}" if whole_lo else f"{lo:%Y-%m-%d %H:%M}") + '..' + (
        f"{hi:%Y-%m-%d}" if whole_hi else f"{hi:%Y-%m-%d %H:%M}")


def nothing_recalled(query, window=None, open_side=None) -> str:
    """What a memory recall answers when nothing saved matches ``query``.

    An answer, not a refusal: a step may end on it.  It names the query and
    says what to do next, because a bare 'No relevant memories found.' left
    central's daemon steps searching again until they paused needs_help
    (2026-11-19).  search_long_term_memory and recall_memory both answer it.

    ``window`` (lo, hi): the dates the search read, named after the query's
    120-character cut so a long query never drops them.  ``open_side``
    ('end' or 'start'): the ask left that side open and only the window was
    read, so the reply says so instead of "the same search again will find
    nothing" (get_chat_history; hartos.helper.searched_recall_window)."""
    head = f"No relevant memories found for {str(query)[:120]!r}"
    if window is None:
        return (f"{head}. Nothing saved matches it: carry on with this step "
                f"from what you already have; the same search again will "
                f"find nothing.")
    span = _window_text(window)
    if open_side in ('end', 'start'):
        missing = 'an end' if open_side == 'end' else 'a start'
        return (f"{head} in {span}. Searched only {span}; ask again with "
                f"{missing} date to cover the rest, or carry on with this "
                f"step from what you already have.")
    return (f"{head} in {span}. Nothing saved in those dates matches it: "
            f"carry on with this step from what you already have; the same "
            f"search again will find nothing.")


def recall_failed(tool: str, store: str, error) -> str:
    """What a memory recall answers when ``store`` could not be read: a
    refusal naming the error's class (never its text, which can carry a
    path), and the step may finish without saved facts."""
    return refuse(tool, f"the {store} memory store could not be read "
                        f"({type(error).__name__})",
                  "Carry on with this step without saved facts",
                  retry=RETRY_FINISH_WITHOUT)


def image_not_made(tool: str, why: str) -> str:
    """What an image tool answers when the image service delivered no
    image (``why``: unreachable, the HTTP status, the error's class): a
    refusal saying to finish the step without the image.  text_2_image and
    hart_intelligence_entry's 'Text to image' tool both answer it; it holds
    no braces, so it is safe as LangChain tool text."""
    return refuse(tool, f"the image service did not deliver an image for "
                        f"this prompt ({why})",
                  "Carry on without the image: put the image prompt and what "
                  "the picture should show into the step's result as text, "
                  "and finish the step",
                  retry=RETRY_FINISH_WITHOUT)


def is_json_refusal(content) -> bool:
    """Whether ``content`` is a JSON object whose top-level ``status`` is
    'refused' -- the shape JSON-answering tools refuse with.

    It runs on every wrapped tool's reply and in every gate reader, so a
    reply that cannot be a refusal is never parsed: measured by the review
    of 2ddf102da, parsing every JSON reply took a 1.2 MB reply from 0.38 ms
    to 33 ms."""
    body = '' if content is None else str(content)
    if '"refused"' not in body:
        return False
    payload = _json_object(body)
    return bool(payload) and payload.get('status') == 'refused'


def _json_object(body: str):
    text = body.strip()
    if not (text.startswith('{') and text.endswith('}')):
        return None
    try:
        import json
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


__all__ = ['FINISH_WITHOUT_JUDGE_RULE', 'NOTE_KEY', 'REFUSAL_MARK',
           'RETRY_AFTER_FIX', 'RETRY_FINISH_WITHOUT',
           'RETRY_LATER', 'RETRY_NEVER', 'RETRY_OTHER_TOOL',
           'finish_without_marker', 'image_not_made',
           'is_json_refusal',
           'nothing_recalled', 'recall_failed', 'refusal_fields',
           'refusal_has_hint', 'refusal_lets_step_finish', 'refuse']
