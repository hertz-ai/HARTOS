"""#125: the LLM-WATCHDOG adopts an externally-spawned llama-server.

The main engine is launched by trueflow / Nunba's ``LlamaConfig`` OUTSIDE
``RuntimeToolManager``, so RTM never fires ``_on_tool_started('llm')`` and
``_refresh_memory_state`` leaves the ``'llm'`` model state stuck at
``device=UNLOADED`` (and ``'llm'`` is deliberately NOT in ``TOOL_CONFIGS`` —
verified — so the idle-sweep never touches it either).

Before the fix, ``_check_llm_health`` probed the adopted server over HTTP,
found it alive, but never healed the stale ``device`` field.  So every tick:

  * line ~1222 re-logged a false ``state.device=UNLOADED`` warning
    (the "492x UNLOADED in the window" of #125), and
  * ``_update_priorities`` skipped the engine entirely
    (``if state.device == UNLOADED: continue``).

The model was never actually unloading — the bookkeeping was stale.  The fix
adds ``_record_llm_alive()`` (single source, reused by BOTH the stateless-probe
branch and the adopted-server branch) which heals ``UNLOADED -> GPU/ACTIVE``
when the HTTP probe confirms the server is alive.

Behavioral: real ``_check_llm_health`` + ``_record_llm_alive``, with a fake
``llama.llama_config.LlamaConfig`` injected at the import boundary.  Assert the
stale state heals and NO restart is queued when alive; a genuinely-dead adopted
server still queues a restart with NO false heal.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import patch  # noqa: E402


def _fake_llama_modules(*, server_process, alive):
    """Build fake ``llama`` + ``llama.llama_config`` modules whose
    ``LlamaConfig()`` reports the given adopted-server liveness."""
    pkg = types.ModuleType('llama')
    mod = types.ModuleType('llama.llama_config')

    class _FakeCfg:
        def __init__(self):
            self.server_process = server_process
            self.config = {'server_port': 8080}

        def check_server_running(self):
            return alive

    mod.LlamaConfig = _FakeCfg
    pkg.llama_config = mod
    return {'llama': pkg, 'llama.llama_config': mod}


def _mgr_with_llm(device):
    from integrations.service_tools.model_lifecycle import (
        ModelLifecycleManager, ModelState, ModelDevice, ModelPriority)  # noqa: F401
    mgr = ModelLifecycleManager()
    mgr._models['llm'] = ModelState(
        name='llm', device=device, priority=ModelPriority.IDLE,
        pressure_evict_only=True)
    return mgr


def test_adopted_alive_heals_stale_unloaded():
    """Adopted server alive over HTTP -> stale UNLOADED heals to GPU/ACTIVE,
    no restart queued.  This is what stops the 492x false-UNLOADED log storm."""
    from integrations.service_tools.model_lifecycle import (
        ModelDevice, ModelPriority)
    mgr = _mgr_with_llm(ModelDevice.UNLOADED)
    with patch.dict(sys.modules,
                    _fake_llama_modules(server_process=None, alive=True)):
        dead = []
        mgr._check_llm_health(dead)
    assert dead == [], f"alive server must not queue a restart: {dead}"
    assert mgr._models['llm'].device == ModelDevice.GPU
    assert mgr._models['llm'].priority == ModelPriority.ACTIVE


def test_adopted_dead_still_queues_restart_no_false_heal():
    """A genuinely-dead adopted server still queues a restart AND must NOT be
    falsely healed — the heal is gated on a real alive probe."""
    from integrations.service_tools.model_lifecycle import ModelDevice
    mgr = _mgr_with_llm(ModelDevice.UNLOADED)
    with patch.dict(sys.modules,
                    _fake_llama_modules(server_process=None, alive=False)):
        dead = []
        mgr._check_llm_health(dead)
    assert ('llm', None, 'llm_server') in dead, \
        f"dead adopted server must queue a restart: {dead}"
    assert mgr._models['llm'].device == ModelDevice.UNLOADED


def test_stateless_probe_alive_registers_active():
    """No 'llm' state + alive probe -> registers an ACTIVE/GPU state (refactor
    preserves the stateless-probe-alive behavior via shared _record_llm_alive)."""
    from integrations.service_tools.model_lifecycle import (
        ModelLifecycleManager, ModelDevice, ModelPriority)
    mgr = ModelLifecycleManager()
    assert 'llm' not in mgr._models
    with patch.dict(sys.modules,
                    _fake_llama_modules(server_process=None, alive=True)):
        dead = []
        mgr._check_llm_health(dead)
    assert dead == []
    assert mgr._models['llm'].device == ModelDevice.GPU
    assert mgr._models['llm'].priority == ModelPriority.ACTIVE


def test_already_loaded_adopted_is_idempotent():
    """An adopted server already marked GPU/ACTIVE stays put — no churn."""
    from integrations.service_tools.model_lifecycle import (
        ModelDevice, ModelPriority)
    mgr = _mgr_with_llm(ModelDevice.GPU)
    mgr._models['llm'].priority = ModelPriority.ACTIVE
    with patch.dict(sys.modules,
                    _fake_llama_modules(server_process=None, alive=True)):
        dead = []
        mgr._check_llm_health(dead)
    assert dead == []
    assert mgr._models['llm'].device == ModelDevice.GPU
    assert mgr._models['llm'].priority == ModelPriority.ACTIVE
