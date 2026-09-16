"""A non-flow entry in config['flows'] must not 500 the whole /chat request.

MEASURED LIVE 2026-09-09, agent 28160128202. Five consecutive CREATE turns
(19:46:24, 19:57:20, 20:10:54, 20:13:05, 20:15:25) each died ~2ms after
``[SCAN] Scanning for existing progress`` with

    File "hartos/create_recipe.py", line 5902, in detect_and_resume_progress
      flow_actions = config['flows'][flow_idx]['actions']
    KeyError: 'actions'

so ``recipe()`` never reached the authoring loop at all -- it died inside
``initialize_with_resume``. The turn then fell through to
``LangChain returned error or empty: {'_tier': 'direct'}``, i.e. a plain
chat model answered with no create pipeline behind it, which is why that
agent's turn 2 produced a *fabricated* execution log.

WHAT IS ACTUALLY WRONG IS THE DATA, NOT THE FLOW LOGIC. The plan template in
gather_agentdetails.py:212 puts ``extra_information`` / ``review_required`` as
SIBLINGS of ``flows``; hart_intelligence_entry's own salvage stub writes them
top-level too. Measured over all 720 on-disk configs (1151 flow entries), only
two carry a non-flow entry inside ``flows``, and both are malformed-model-JSON
artifacts that a lenient repair let through:

    28160128202  flows[1] = {'extra_information', 'review_required'}   (misplaced)
    79991757345  flows[1] = '}'                                        (a bare str)

718/720 configs are unaffected, so the guard below cannot change their
behaviour -- for them the predicate never fires.

    python -m pytest tests/unit/test_prompt_config_flows_are_flows.py --noconftest -q
"""
import pytest

cr = pytest.importorskip("hartos.create_recipe")


def _drop():
    fn = getattr(cr, "_drop_non_flow_entries", None)
    assert fn is not None, (
        "hartos.create_recipe._drop_non_flow_entries is gone -- re-point this "
        "test rather than deleting it; it guards a live /chat 500")
    return fn


class TestTheTwoShapesMeasuredOnDisk:
    """Both real defects, verbatim from the on-disk configs."""

    def test_misplaced_metadata_dict_is_not_a_flow(self):
        """28160128202: the model put two TOP-LEVEL keys inside flows[]."""
        cfg = {'flows': [
            {'actions': ['a1'], 'flow_name': 'main', 'persona': 'Executor'},
            {'extra_information': 'x', 'review_required': True},
        ]}
        out = _drop()(cfg, '28160128202')
        assert len(out['flows']) == 1, (
            "the metadata dict is not a flow -- keeping it makes total_flows "
            "count a phantom that can never complete")
        assert out['flows'][0]['actions'] == ['a1']

    def test_bare_string_from_json_repair_is_not_a_flow(self):
        """79991757345: a stray '}' survived a lenient JSON repair."""
        cfg = {'flows': [
            {'actions': ['a1'], 'flow_name': 'main', 'persona': 'Executor'},
            '}',
        ]}
        out = _drop()(cfg, '79991757345')
        assert len(out['flows']) == 1
        assert all(isinstance(f, dict) for f in out['flows'])

    def test_every_surviving_entry_can_be_indexed_for_actions(self):
        """The exact expression that crashed at create_recipe.py:5902."""
        cfg = {'flows': [
            {'actions': ['a1'], 'flow_name': 'main', 'persona': 'Executor'},
            {'extra_information': 'x', 'review_required': True},
            '}',
        ]}
        out = _drop()(cfg, 'both')
        for i in range(len(out['flows'])):
            out['flows'][i]['actions']  # must not raise KeyError/TypeError


class TestItCannotDisturbThe718GoodConfigs:
    """A guard that changes a working config is a regression, not a fix."""

    def test_wellformed_config_is_returned_unchanged(self):
        cfg = {'goal': 'g', 'flows': [
            {'actions': ['a1', 'a2'], 'flow_name': 'main', 'persona': 'Executor'},
            {'actions': ['b1'], 'flow_name': 'second', 'persona': 'Reviewer'},
        ]}
        import copy
        before = copy.deepcopy(cfg)
        out = _drop()(cfg, 'good')
        assert out == before, (
            "718 of 720 on-disk configs have no phantom entry; this function "
            "must be a no-op for every one of them")

    def test_flow_indices_do_not_shift_when_nothing_is_dropped(self):
        """Action files are named <id>_<flow>_<action>.json -- indices matter."""
        cfg = {'flows': [
            {'actions': ['a'], 'flow_name': 'f0', 'persona': 'p'},
            {'actions': ['b'], 'flow_name': 'f1', 'persona': 'p'},
            {'actions': ['c'], 'flow_name': 'f2', 'persona': 'p'},
        ]}
        out = _drop()(cfg, 'idx')
        assert [f['flow_name'] for f in out['flows']] == ['f0', 'f1', 'f2']

    def test_a_flow_with_an_empty_action_list_is_still_a_flow(self):
        """Empty actions is legal data; only a MISSING key is the defect."""
        cfg = {'flows': [{'actions': [], 'flow_name': 'main', 'persona': 'p'}]}
        out = _drop()(cfg, 'empty')
        assert len(out['flows']) == 1


class TestDegenerateInputs:

    def test_missing_flows_key_is_left_alone(self):
        """create_recipe.py already treats absent flows as zero flows."""
        assert _drop()({'goal': 'g'}, 'none') == {'goal': 'g'}

    def test_flows_not_a_list_is_left_alone(self):
        cfg = {'flows': 'not-a-list'}
        assert _drop()(cfg, 'weird') == cfg

    def test_none_config_does_not_raise(self):
        assert _drop()(None, 'null') is None
