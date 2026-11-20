"""The CREATE judge is told a receipt in the form its view shows it.

WHAT WENT WRONG (central, 20 Nov 01:12Z, build 70ece55bd)
  Daemon goal 74416785, action 2 (search_long_term_memory).  The judge's view
  ([JUDGE-VIEW] StatusVerifier: 5 tool call(s) and 5 result(s) shown as a
  report) held the four recall results, and the verdict was 'pending': "The
  three search results were pasted into the user's message as text, not
  returned as role="tool" receipts, so they cannot serve as tool_receipt
  evidence."

  hartos.helper.ToolActivityAsEvidence shows the judge every tool call and
  result as a report line from the seat that made it ("Assistant called
  search_long_term_memory(...)" / "Tool result: ..."), on purpose: given the
  raw role="tool" messages the verifier continued the Assistant's call instead
  of judging it.  The StatusVerifier's prompt (create_recipe.instantiate_
  status_verifier_agent) still said kind="tool_receipt" may point only to a
  role="tool" result, a message its view never holds, so the judge could
  refuse every receipt it was shown.  The completion gate checks the cited
  receipt against the raw group log (lifecycle_hooks), unchanged.

    python -m pytest tests/unit/test_the_judge_is_told_the_receipt_form_it_sees.py -q --noconftest
"""
import re
from types import SimpleNamespace
from unittest import mock

RESULT = 'HertzAI commission agent trade model: 2% of FOB, minimum US$300.'


def _judge_view():
    """What the StatusVerifier receives for one recall call and its result."""
    import flask
    from hartos.helper import ToolActivityAsEvidence
    seat = SimpleNamespace(name='StatusVerifier', _function_map={},
                           _code_execution_config=False, llm_config={})
    msgs = [
        {'role': 'user', 'name': 'ChatInstructor',
         'content': 'Execute Action 2: search_long_term_memory: recall saved facts'},
        {'role': 'assistant', 'name': 'Assistant', 'content': '',
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': 'search_long_term_memory',
                                      'arguments': '{"query": "HertzAI"}'}}]},
        {'role': 'tool', 'name': 'Assistant', 'content': RESULT,
         'tool_responses': [{'tool_call_id': 'c1', 'role': 'tool',
                             'content': RESULT}]},
    ]
    with flask.Flask('judge-view').app_context():
        return ToolActivityAsEvidence(seat).apply_transform(msgs)


def _verifier_prompt():
    """The system message instantiate_status_verifier_agent gives the judge."""
    import hartos.create_recipe as cr
    built = {}

    def _agent(**kwargs):
        built.update(kwargs)
        return SimpleNamespace(**kwargs)
    with mock.patch.object(cr, 'autogen', SimpleNamespace(AssistantAgent=_agent)), \
            mock.patch.object(cr, 'get_llm_config', return_value={}), \
            mock.patch.object(cr, 'user_tasks', {
                'system_daemon_74416785': SimpleNamespace(actions=['recall'])}):
        cr.instantiate_status_verifier_agent('system_daemon_74416785',
                                             autonomous=True)
    return built['system_message']


def _receipt_rule(prompt):
    """The sentence of the prompt that says what a tool_receipt may cite."""
    return next(s for s in re.split(r'(?<=\.)\s+', prompt)
                if 'kind="tool_receipt"' in s)


def test_the_receipt_rule_names_no_role_the_judge_never_sees():
    view = _judge_view()
    holding = [m for m in view if RESULT in str(m.get('content') or '')]
    assert holding, view
    seen_roles = {m.get('role') for m in holding}
    rule = _receipt_rule(_verifier_prompt())
    demanded = set(re.findall(r'role="(\w+)"', rule))
    assert demanded <= seen_roles, (rule, seen_roles)


def test_the_receipt_rule_names_the_form_the_view_shows():
    view = _judge_view()
    line = next(ln for m in view for ln in str(m.get('content') or '').splitlines()
                if RESULT in ln)
    marker = line.split(RESULT, 1)[0].strip()      # what the report puts first
    assert marker, line
    assert marker in _receipt_rule(_verifier_prompt())
