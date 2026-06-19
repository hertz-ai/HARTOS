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


def test_populate_embodied_registers_robotsuite():
    cat = _fresh_catalog()
    n = cat._populate_embodied_models()
    assert n == 1
    e = cat._entries.get('embodied-qwen-robotsuite')
    assert e is not None
    assert e.model_type == ModelType.EMBODIED
    assert e.backend == 'in_process'             # served by HevolveAI via the bridge
    caps = e.capabilities
    assert caps['language_conditioned'] is True  # VLA
    assert caps['world_model'] is True
    assert caps['action_endpoint'] == '/v1/actions'
    assert 'camera' in caps['sensor_modalities']


def test_declared_action_verbs_are_real_robotaction_factories():
    """No drift: every verb the catalog advertises must be a buildable action."""
    cat = _fresh_catalog()
    cat._populate_embodied_models()
    verbs = cat._entries['embodied-qwen-robotsuite'].capabilities['action_verbs']
    assert verbs, 'embodied entry must advertise action verbs'
    for verb in verbs:
        assert hasattr(RobotAction, verb), f"{verb} has no RobotAction factory"
        # and the factory actually stamps that action_type
        built = getattr(RobotAction, verb)('x') if verb == 'vla_instruct' \
            else getattr(RobotAction, verb)([]) if verb in ('action_chunk', 'world_model_rollout') \
            else getattr(RobotAction, verb)()
        assert built.action_type == verb


def test_populate_embodied_is_idempotent():
    cat = _fresh_catalog()
    assert cat._populate_embodied_models() == 1
    assert cat._populate_embodied_models() == 0  # already present, not duplicated


def test_embodied_in_populate_all_orchestration():
    """The embodied populator is wired into the catalog's populate-all path."""
    import inspect
    src = inspect.getsource(ModelCatalog)
    assert 'self._populate_embodied_models()' in src
