"""Recipe-banked window layouts — the Recipe Pattern extended to window mgmt.

THE MOAT, BANKED (compositor/IPC_PROTOCOL.md §8). A CREATE-mode action can bank
its ``window.*`` steps — ``summon A + B``, ``tile grid``, ``switch workspace`` —
into a small per-(prompt_id, flow_id) artifact so a later REUSE replays the EXACT
desktop layout WITHOUT any LLM call, exactly like a normal recipe replays actions.

Invariants this module HONORS (IPC §8, §9 + reference_task_ledger_identifiers):
  - Does NOT alter the core recipe on-disk format, the
    prompt_id/flow_id/action_id/session_id identifier semantics, or dashboard
    grouping. Window-layout steps live in a SEPARATE file
    (``{prompt_id}_{flow_id}_window_layout.json``) keyed by the SAME
    (prompt_id, flow_id) coordinate the recipe uses — never inside the recipe.
  - Replays through the SAME gate as a live agent verb: every step goes through
    ``HartWmClient.dispatch_verb`` (Phase 6), so a banked ``window.close`` of a
    focused window still hits the fail-closed constitutional gate at REPLAY time,
    and a destructive op the constitution refuses is refused on replay too.
  - HONEST FAILURE on replay: a ``window.summon`` that cannot confirm a real
    toplevel map returns ``unsupported`` and the replay surfaces it — never a
    phantom-success no-op (§8, §1.4).
  - Feature-detected: when no compositor is present (cage Tier-3) the WM client
    reports unavailable and replay is a logged no-op; banking is still allowed
    (the layout is recorded for a future tier) but never claims it ran.

Fire-and-forget: bank_* never raises into the agent's execution flow. replay
returns a structured per-step report for the caller to log/surface.

prompt_id is str|int (human DB id) OR a UUID string (autonomous agents) — we
coerce to str for the filename, matching ``create_ledger_from_actions``.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

try:
    from hartos.helper import PROMPTS_DIR
except ImportError:  # pragma: no cover - standalone
    # Same resolved canonical dir, without helper's weight.  The old
    # module-relative fallback pointed at site-packages/prompts in the
    # frozen layout — a folder that does not exist (#692).
    from core.cache_loaders import PROMPTS_DIR

logger = logging.getLogger('hevolve.window_layout_recipe')

_lock = threading.RLock()

# The ONLY verbs a layout recipe may bank/replay. A allowlist (not the full
# dispatch_verb surface) so a banked layout can never carry, e.g., an arbitrary
# future verb — it is a window-ARRANGEMENT recipe, nothing else. ``window.close``
# is intentionally INCLUDED (a layout may close a scratch window) but stays
# fail-closed gated at replay by dispatch_verb.
_BANKABLE_VERBS = frozenset({
    'window.summon',
    'window.tile',
    'window.place',
    'window.focus',
    'window.move_to_workspace',
    'workspace.switch',
    'window.close',
})


def _layout_path(prompt_id: Any, flow_id: int) -> str:
    return os.path.join(PROMPTS_DIR,
                        f'{str(prompt_id)}_{int(flow_id)}_window_layout.json')


def bank_layout(prompt_id: Any, flow_id: int,
                steps: List[Dict[str, Any]]) -> bool:
    """Persist a window-layout recipe for (prompt_id, flow_id).

    ``steps`` is an ordered list of ``{"verb": "window.tile", "args": {...}}``.
    Unknown/forbidden verbs are dropped (logged), so a caller cannot smuggle a
    non-window verb into a layout recipe. Returns True iff a non-empty recipe was
    written. Fire-and-forget — never raises.
    """
    try:
        clean: List[Dict[str, Any]] = []
        for s in (steps or []):
            verb = (s or {}).get('verb')
            if verb not in _BANKABLE_VERBS:
                logger.debug("layout bank: dropping non-bankable verb %r", verb)
                continue
            clean.append({'verb': verb, 'args': dict((s.get('args') or {}))})
        if not clean:
            return False
        payload = {
            'v': 1,
            'prompt_id': str(prompt_id),
            'flow_id': int(flow_id),
            'steps': clean,
        }
        path = _layout_path(prompt_id, flow_id)
        with _lock:
            os.makedirs(PROMPTS_DIR, exist_ok=True)
            tmp = path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, path)
        logger.info("banked %d window-layout step(s) -> %s", len(clean), path)
        return True
    except Exception as e:  # never propagate into execution
        logger.debug("layout bank failed (non-fatal): %s", e)
        return False


def has_layout(prompt_id: Any, flow_id: int) -> bool:
    """True iff a banked window-layout recipe exists for this coordinate."""
    return os.path.exists(_layout_path(prompt_id, flow_id))


def load_layout(prompt_id: Any, flow_id: int) -> Optional[Dict[str, Any]]:
    """Load a banked layout recipe, or None if absent/corrupt."""
    path = _layout_path(prompt_id, flow_id)
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get('steps'), list):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


def replay_layout(prompt_id: Any, flow_id: int, agent_id: str = 'recipe',
                  wm_client: Any = None) -> Dict[str, Any]:
    """Replay a banked window-layout recipe WITHOUT any LLM call.

    Each step is dispatched through ``HartWmClient.dispatch_verb`` (the SAME gate
    a live agent verb passes), so the constitution still vets destructive ops at
    replay time. Returns a structured report:

        {"ok": bool, "replayed": int, "total": int, "available": bool,
         "results": [{"verb": ..., "ok": ..., ...}, ...]}

    ``ok`` is True only if a recipe existed AND every step the gate allowed
    succeeded. A missing recipe → ok=False, total=0. No compositor present →
    available=False and a logged no-op (the layout is preserved for a future
    tier). Honest-failure preserved: a step's ``unsupported``/``refused`` is
    reported, never masked.
    """
    data = load_layout(prompt_id, flow_id)
    if not data:
        return {'ok': False, 'replayed': 0, 'total': 0,
                'available': True, 'results': [], 'reason': 'no-layout'}

    if wm_client is None:
        try:
            from integrations.agent_engine.hart_wm_client import get_wm_client
            wm_client = get_wm_client()
        except Exception as e:
            return {'ok': False, 'replayed': 0,
                    'total': len(data.get('steps', [])),
                    'available': False, 'results': [],
                    'reason': f'wm-client-unavailable: {e}'}

    if not getattr(wm_client, 'available', False):
        # No native-window management (cage Tier-3). Do NOT pretend it ran.
        logger.info("layout replay: no compositor (Tier-3) — no-op, layout kept")
        return {'ok': False, 'replayed': 0,
                'total': len(data.get('steps', [])),
                'available': False, 'results': [], 'reason': 'no-compositor'}

    results: List[Dict[str, Any]] = []
    replayed = 0
    for step in data.get('steps', []):
        verb = step.get('verb')
        args = step.get('args') or {}
        if verb not in _BANKABLE_VERBS:
            results.append({'verb': verb, 'ok': False, 'error': 'not-bankable'})
            continue
        try:
            r = wm_client.dispatch_verb(verb, args, agent_id)
        except Exception as e:
            r = {'ok': False, 'error': f'dispatch raised: {e}'}
        entry = {'verb': verb, **(r if isinstance(r, dict) else {'ok': False})}
        results.append(entry)
        if entry.get('ok'):
            replayed += 1

    return {
        'ok': replayed == len(results) and replayed > 0,
        'replayed': replayed,
        'total': len(results),
        'available': True,
        'results': results,
    }


class LayoutRecorder:
    """CREATE-mode companion to ``replay_layout`` — banks ``window.*`` steps as an
    agent performs them, so a later REUSE replays the EXACT layout (IPC §8).

    THE no-parallel-path point: a recorded step is dispatched through the SAME
    ``HartWmClient.dispatch_verb`` gate a normal agent verb uses — CREATE never
    forks a second dispatch path. Only a verb the gate ACTUALLY ALLOWED (``ok``
    truthy) and that is bankable is recorded, so the banked recipe can never
    replay a step that never really happened — a ``window.close`` the
    constitution refused, or a ``window.summon`` that returned the honest
    ``unsupported`` (no real toplevel mapped, §1.4/§4.6), is dropped from the
    bank, not stored as a phantom success.

    Usage (CREATE mode)::

        rec = LayoutRecorder(agent_id='goal_abc')
        rec.dispatch('window.summon', {'manifest_id': 'files'})
        rec.dispatch('window.tile', {'layout': 'splith'})
        rec.dispatch('workspace.switch', {'workspace': 2})
        rec.flush(prompt_id, flow_id)   # persists via bank_layout

    Fire-and-forget like the rest of this module: ``dispatch``/``flush`` never
    raise into the agent's execution flow.
    """

    def __init__(self, agent_id: str = 'recipe', wm_client: Any = None):
        self.agent_id = agent_id
        self._steps: List[Dict[str, Any]] = []
        if wm_client is None:
            try:
                from integrations.agent_engine.hart_wm_client import get_wm_client
                wm_client = get_wm_client()
            except Exception as e:  # pragma: no cover - non-shell node
                logger.debug("LayoutRecorder: wm-client unavailable: %s", e)
                wm_client = None
        self._wm = wm_client

    @property
    def steps(self) -> List[Dict[str, Any]]:
        """The bankable steps recorded so far (a copy; never the live list)."""
        return [dict(s) for s in self._steps]

    def dispatch(self, verb: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Dispatch ONE ``window.*`` verb through the canonical WM gate and, iff it
        was a bankable verb the gate allowed, record it for the layout recipe.

        Returns the gate's own result dict (so the caller still sees a refused /
        unsupported step honestly). Never raises.
        """
        args = dict(args or {})
        if self._wm is None or not getattr(self._wm, 'available', False):
            # No native-window management (cage Tier-3): do NOT fabricate a
            # success and do NOT bank — there was no real window op to replay.
            return {'ok': False, 'available': False, 'verb': verb,
                    'reason': 'no-compositor'}
        try:
            r = self._wm.dispatch_verb(verb, args, self.agent_id)
        except Exception as e:  # never propagate into execution
            logger.debug("LayoutRecorder dispatch %r raised (non-fatal): %s",
                         verb, e)
            return {'ok': False, 'verb': verb, 'error': f'dispatch raised: {e}'}
        if not isinstance(r, dict):
            r = {'ok': False}
        # Bank ONLY a real, allowed, bankable op — never a refused/unsupported one.
        if r.get('ok') and verb in _BANKABLE_VERBS:
            self._steps.append({'verb': verb, 'args': args})
        return {'verb': verb, **r}

    def flush(self, prompt_id: Any, flow_id: int) -> bool:
        """Persist the recorded steps as the (prompt_id, flow_id) layout recipe.

        Returns True iff a non-empty recipe was written (delegates to the SINGLE
        ``bank_layout`` writer — one source of truth for the on-disk format).
        """
        return bank_layout(prompt_id, flow_id, self._steps)
