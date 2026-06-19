"""Embodied model bootstrap — register a VLA/world-model policy (Qwen RobotSuite)
in the universal ModelCatalog, exactly like an LLM/TTS/VLM model.

Behavioural: build a catalog on a throwaway path, run the embodied populator,
assert the entry + that its declared action_verbs are REAL RobotAction factories
(catalog ↔ action-model can't drift). The policy itself runs in HevolveAI; this
is the discoverable metadata record.

    python -m pytest tests/unit/test_embodied_model_bootstrap.py --noconftest -q
"""
import os
import tempfile

from integrations.service_tools.model_catalog import ModelCatalog, ModelType
from integrations.robotics.action_model import RobotAction


def _fresh_catalog():
    fd, path = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    os.remove(path)  # non-existent → empty catalog
    cat = ModelCatalog(catalog_path=path)
    cat._entries.clear()  # deterministic: exercise the embodied populator alone
    return cat


def test_embodied_model_type_is_first_class():
    assert ModelType.EMBODIED == 'embodied'          # str-enum equality
    assert ModelType.EMBODIED.value == 'embodied'
    assert ModelType.EMBODIED.label == 'Embodied VLA / World Model'
    assert ModelType('embodied') is ModelType.EMBODIED


def test_populate_embodied_registers_three_robotsuite_models():
    cat = _fresh_catalog()
    n = cat._populate_embodied_models()
    assert n == 3  # RobotManip + RobotWorld + RobotNav — three independent models
    manip = cat._entries.get('embodied-qwen-robotmanip')
    world = cat._entries.get('embodied-qwen-robotworld')
    nav = cat._entries.get('embodied-qwen-robotnav')
    assert manip and world and nav
    assert all(e.model_type == ModelType.EMBODIED for e in (manip, world, nav))
    assert all(e.backend == 'in_process' for e in (manip, world, nav))
    # RobotManip — canonical 80-D masked state-action, language-conditioned VLA
    assert manip.capabilities['action_dims'] == 80
    assert manip.capabilities['per_arm_dims'] == 29
    assert manip.capabilities['language_conditioned'] is True
    # RobotWorld — language-conditioned video world model
    assert world.capabilities['world_model'] is True
    assert world.capabilities['output'] == 'predicted_video'
    # RobotNav — 8 (x, y, theta) waypoints
    assert nav.capabilities['num_waypoints'] == 8
    # all three share the WorldModelBridge endpoints
    assert manip.capabilities['action_endpoint'] == '/v1/actions'


def test_declared_action_verbs_are_real_robotaction_factories():
    """No drift: every verb ANY embodied entry advertises is a buildable action."""
    cat = _fresh_catalog()
    cat._populate_embodied_models()
    embodied = [e for e in cat._entries.values()
                if e.model_type == ModelType.EMBODIED]
    verbs = {v for e in embodied for v in e.capabilities.get('action_verbs', [])}
    assert verbs, 'embodied entries must advertise action verbs'
    for verb in verbs:
        assert hasattr(RobotAction, verb), f"{verb} has no RobotAction factory"


def test_populate_embodied_is_idempotent():
    cat = _fresh_catalog()
    assert cat._populate_embodied_models() == 3
    assert cat._populate_embodied_models() == 0  # already present, not duplicated


def test_embodied_in_populate_all_orchestration():
    """The embodied populator is wired into the catalog's populate-all path."""
    import inspect
    src = inspect.getsource(ModelCatalog)
    assert 'self._populate_embodied_models()' in src
