"""One reader for what the VLM loop reports — commands fired, and its reasoning.

WHY THIS MODULE EXISTS.  ``run_local_agentic_loop`` accumulates a record of
every iteration into ``extracted_responses`` and returns it with an
``exit_reason``, ON EVERY EXIT — done, max_iterations, timeout, stopped,
action_error.  Its own comment states the contract:

    status mirrors exit_reason: only 'done' is a real success.  Callers
    (LangChain router, autogen) can inspect exit_reason to craft an honest
    response instead of confidently lying when the loop timed out.

Four consumers read that structure.  ONE of them (``hart_intelligence_entry.
_handle_computer_action_tool``) honours the contract.  The other three were
written against message types the producer has never emitted:

    producer emits          'action'      'completion'   'error'
    three consumers parse   'analysis'    'next_action'

A filter that matches nothing fails SILENTLY — it yields an empty list, the
caller falls through to a default, and a plausible string comes back.  So the
defect survived every review and every test.

WHAT IT COST, measured 2026-09-11 against the recipes on disk:

    vlm_agent files banked                                    : 106
    recipe is EXACTLY the instruction restated (the fallback)  : 106
    recipe carrying a real observed step                       :   0

Every VLM-authored recipe step in the store is the instruction echoed back,
because ``create_recipe``'s extraction matched nothing and fell through to
``recipe_steps.append({"steps": instructions})``.  On the reuse side the same
fiction meant the tool handed the model its own request under the heading of
an observation.

The shape is the producer's, so the reader lives beside the producer.  A
consumer that renders it differently is free to; a consumer that PARSES it
again is a second source of truth for "what did the tool see", and those
drift — that is exactly how this happened.

Guarded by tests/unit/test_vlm_response_is_read_as_produced.py, whose drift
check reads the producer's own ``"type": "..."`` literals and fails if any
consumer compares against a type the producer cannot emit.
"""

from core.constants import TOOL_OBSERVATION_MAX_CHARS

# The producer's vocabulary, in the producer's words.  local_loop appends
# exactly these three and nothing else.  The drift guard re-derives this list
# from local_loop.py at test time rather than trusting this tuple, so a new
# producer type fails the suite instead of being silently dropped here.
VLM_MESSAGE_TYPES = ('action', 'completion', 'error')

# What the loop calls the tool when it records a step, so a banked recipe step
# names the tool that can actually replay it.
_REPLAY_TOOL = 'execute_windows_or_android_command'

# Lines starting with these are grounding mechanics, not observations: they
# describe WHERE the model clicked, not WHAT it found.  Kept verbatim from the
# three inline copies this module replaces so the recipe text does not change
# shape for anything already reading it.
_NOISE_PREFIXES = ('Next Action:', 'Box ID:', 'box_centroid_coordinate:',
                   'value:')


def _clean(text):
    """Drop grounding mechanics, keep what was observed."""
    keep = []
    for line in str(text or '').split('\n'):
        if not line.strip().startswith(_NOISE_PREFIXES):
            keep.append(line)
    return '\n'.join(keep)


def observed_records(response):
    """The loop's own account of the run, oldest first.

    Returns ``[{kind, text, ok, iteration}]`` — one entry per recorded
    iteration.  ``kind`` is the producer's type verbatim.  This is the ONLY
    parse of ``extracted_responses``; every other function here renders what
    it returns.

    Never raises.  It runs on tool-return and error paths, where an exception
    would cost a result that was really produced.
    """
    out = []
    try:
        for msg in (response or {}).get('extracted_responses') or []:
            if not isinstance(msg, dict):
                continue
            kind = msg.get('type', '')
            content = msg.get('content', '')
            iteration = msg.get('iteration')

            if kind == 'action' and isinstance(content, dict):
                # The command fired, why, and what it produced.  `result` is
                # the action's real stdout for deterministic actions (shell,
                # read_file_and_understand) and is empty for a click.
                bits = []
                act = str(content.get('action') or '').strip()
                reasoning = _clean(content.get('reasoning')).strip()
                result = str(content.get('result') or '').strip()
                if act:
                    bits.append(act)
                if reasoning:
                    bits.append(reasoning)
                text = ' - '.join(bits)
                if result:
                    text = '%s\n    output: %s' % (text, result) if text \
                        else result
                out.append({'kind': kind, 'text': text,
                            'ok': bool(content.get('ok', True)),
                            'iteration': iteration})
            elif kind in ('completion', 'error'):
                out.append({'kind': kind, 'text': _clean(content).strip(),
                            'ok': kind != 'error', 'iteration': iteration})
            elif isinstance(content, dict):
                # An unknown type carrying structured content: keep it rather
                # than drop it.  Dropping is what produced this whole defect.
                out.append({'kind': kind or 'unknown',
                            'text': _clean(content.get('reasoning')
                                           or content.get('result') or '')
                            .strip(),
                            'ok': True, 'iteration': iteration})
            else:
                out.append({'kind': kind or 'unknown',
                            'text': _clean(content).strip(),
                            'ok': True, 'iteration': iteration})
    except Exception:
        return out
    return [r for r in out if r['text']]


def observation_text(response, max_chars=TOOL_OBSERVATION_MAX_CHARS):
    """What the machine showed, as text a model can read.  '' when nothing.

    Bounded because an unbounded screen dump would spend the very slot it is
    trying to inform (n_ctx 12288, ~6144/slot — #539/#734).  Empty in, empty
    out, so a caller can keep its plain sentence rather than append a blank
    section.  Never raises.
    """
    try:
        lines = ['- %s' % r['text'] for r in observed_records(response)]
        if not lines:
            return ''
        return '\n'.join(lines)[:max_chars]
    except Exception:
        return ''


def outcome_summary(response):
    """An HONEST one-liner about how the loop ended, for any exit_reason.

    ``exit_reason`` is published by the producer precisely so a caller need
    not pretend success.  Mirrors the wording
    ``hart_intelligence_entry._handle_computer_action_tool`` already uses, so
    the two legs describe the same run the same way.
    """
    try:
        resp = response or {}
        reason = resp.get('exit_reason') or (
            'done' if resp.get('status') == 'success' else 'incomplete')
        n = len(observed_records(resp))
        secs = resp.get('execution_time_seconds') or 0
        return {
            'done': 'Completed in %.0fs after %d step(s).' % (secs, n),
            'timeout': 'Ran out of time after %.0fs (%d step(s)) before '
                       'finishing.' % (secs, n),
            'max_iterations': 'Tried %d step(s) without reaching a clear '
                              'completion.' % n,
            'action_error': 'Hit errors on 3 consecutive actions after %d '
                            'step(s) and stopped.' % n,
            'stopped': 'Stopped at your request after %d step(s).' % n,
            'grounding_failed': 'Could not reliably locate the UI element '
                                'after %d attempt(s).' % n,
        }.get(reason, "Loop exited with reason '%s' after %d step(s)."
              % (reason, n))
    except Exception:
        return ''


def recipe_steps(response, fallback_instruction, tool_name=_REPLAY_TOOL):
    """The banked recipe's steps — what the run DID, not what it was asked.

    Falls back to the instruction only when the loop recorded nothing at all.
    Before this module that fallback fired 106 times out of 106, because the
    extraction above it matched no producer type.
    """
    steps = []
    try:
        for r in observed_records(response):
            steps.append({
                'steps': r['text'],
                'tool_name': tool_name,
                'agent_to_perform_this_action': 'Helper',
            })
    except Exception:
        pass
    if not steps:
        steps.append({
            'steps': fallback_instruction,
            'tool_name': tool_name,
            'agent_to_perform_this_action': 'Helper',
        })
    return steps
