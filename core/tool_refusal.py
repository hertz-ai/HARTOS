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
    RETRY_NEVER), so a refusal no retry can fix is not retried as it was.

A text refusal opens with "Error:" (core.constants.TOOL_ERROR_REPLY_PREFIXES),
so core.constants.tool_reply_failed reads it as a failed call and the REUSE
gate re-steers instead of crediting it.  A tool that answers JSON keeps its
own fields and adds the same sentence under NOTE_KEY (the key person_ask
already uses for its refusals); tool_reply_failed reads a top-level
{"status": "refused"} as failed too.

Scope: what a tool finds wrong once its arguments are bound -- a value it
cannot use, a precondition not met.  Argument-shape refusals (unknown or
missing names, broken JSON) are made before the tool runs, by
hartos/helper.py tool_argument_error, in the same voice.  core.tool_logging
counts every failed reply that came back without a hint
([TOOL-REFUSAL-NO-HINT]), which is how this framework is enforced: each lane
can see the refusals it has not moved here yet.
"""
from __future__ import annotations

RETRY_AFTER_FIX = 'after_fix'
RETRY_LATER = 'later'
RETRY_NEVER = 'never'

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
}


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
    return (f"Error: the {name}{REFUSAL_MARK}{_clause(problem, 'what was wrong')} "
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
    payload = _json_object(body)
    return bool(payload and str(payload.get(NOTE_KEY) or '').strip())


def is_json_refusal(content) -> bool:
    """Whether ``content`` is a JSON object whose top-level ``status`` is
    'refused' -- the shape JSON-answering tools refuse with."""
    payload = _json_object('' if content is None else str(content))
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


__all__ = ['NOTE_KEY', 'REFUSAL_MARK', 'RETRY_AFTER_FIX', 'RETRY_LATER',
           'RETRY_NEVER', 'is_json_refusal', 'refusal_fields',
           'refusal_has_hint', 'refuse']
