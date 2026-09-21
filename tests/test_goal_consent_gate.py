"""require_consent enforcement lives INSIDE GuardrailEnforcer.before_dispatch.

Owner directive 2026-08-25 (#698): daemons work autonomously WITH human
consent.  goal_seeding has carried 'require_consent': True with zero
enforcement sites.  First attempt (4bbb1758) added a standalone gate in
dispatch.py — owner called the parallel path: before_dispatch step 3 is
the designed goal-specific policy point (constitutional, ethos) and the
consent check belongs there.  This rework folds it in: ONE policy gate,
N policies.  dispatch_goal now passes goal.to_dict() + user_id the same
way agent_daemon.py:1268 always has.

The denied case was proven RED before any enforcement existed.
"""
import contextlib
import types
from unittest.mock import MagicMock

import pytest


def _patch_consent(monkeypatch, granted):
    from integrations.social import models as social_models
    from integrations.social.consent_service import ConsentService

    db = MagicMock()

    @contextlib.contextmanager
    def db_session(commit=False):
        yield db

    monkeypatch.setattr(social_models, 'db_session', db_session)
    monkeypatch.setattr(ConsentService, 'check_consent',
                        staticmethod(lambda *a, **k: granted))
    req_spy = MagicMock(return_value=types.SimpleNamespace(granted=False))
    monkeypatch.setattr(ConsentService, 'request_consent',
                        staticmethod(req_spy))
    return req_spy


def _quiet_other_policies(monkeypatch):
    """Isolate the consent policy from its guardrail siblings."""
    from security import hive_guardrails as hg
    monkeypatch.setattr(hg.ConstitutionalFilter, 'check_prompt',
                        staticmethod(lambda p: (True, 'ok')))
    monkeypatch.setattr(hg.ConstitutionalFilter, 'check_goal',
                        staticmethod(lambda g: (True, 'ok')))
    monkeypatch.setattr(hg.HiveEthos, 'check_goal_ethos',
                        staticmethod(lambda g: (True, 'ok')))
    monkeypatch.setattr(hg.HiveEthos, 'rewrite_prompt_for_togetherness',
                        staticmethod(lambda p: p))


FLAGGED = {'config_json': {'require_consent': True}}


def test_flagged_goal_without_consent_blocks_and_files_request(monkeypatch):
    from security.hive_guardrails import GuardrailEnforcer
    _quiet_other_policies(monkeypatch)
    req_spy = _patch_consent(monkeypatch, granted=False)

    allowed, reason, _ = GuardrailEnforcer.before_dispatch(
        'p', goal_dict=FLAGGED, user_id='user-1')

    assert allowed is False
    assert 'consent' in reason.lower()
    assert req_spy.call_count == 1, (
        "gate must file the pending request the UserConsent UI surfaces")


def test_flagged_goal_with_consent_passes(monkeypatch):
    from security.hive_guardrails import GuardrailEnforcer
    _quiet_other_policies(monkeypatch)
    req_spy = _patch_consent(monkeypatch, granted=True)

    allowed, reason, _ = GuardrailEnforcer.before_dispatch(
        'p', goal_dict=FLAGGED, user_id='user-1')

    assert allowed is True
    assert req_spy.call_count == 0


def test_unflagged_goal_touches_no_consent_machinery(monkeypatch):
    from security.hive_guardrails import GuardrailEnforcer
    _quiet_other_policies(monkeypatch)
    req_spy = _patch_consent(monkeypatch, granted=False)

    allowed, _, _ = GuardrailEnforcer.before_dispatch(
        'p', goal_dict={'config_json': {}}, user_id='user-1')

    assert allowed is True
    assert req_spy.call_count == 0


def test_flagged_goal_without_user_context_blocks(monkeypatch):
    from security.hive_guardrails import GuardrailEnforcer
    _quiet_other_policies(monkeypatch)
    _patch_consent(monkeypatch, granted=True)
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)

    allowed, reason, _ = GuardrailEnforcer.before_dispatch(
        'p', goal_dict=FLAGGED, user_id=None)

    assert allowed is False
    assert 'user context' in reason


def test_daemon_goal_falls_back_to_boot_owner_and_asks(monkeypatch):
    """A user-less goal on an owned desktop asks the OWNER, not a wall.

    Before this fallback the daemon's require_consent goals looped
    blocked forever because request_consent needs a user to file the
    pending record against; HEVOLVE_OWNER_USER_ID (boot-set, same trust
    as crossbar publish auth) names that user.
    """
    from security.hive_guardrails import GuardrailEnforcer
    _quiet_other_policies(monkeypatch)
    req_spy = _patch_consent(monkeypatch, granted=False)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-9')

    allowed, reason, _ = GuardrailEnforcer.before_dispatch(
        'p', goal_dict=FLAGGED, user_id=None)

    assert allowed is False
    assert 'pending request filed' in reason
    assert req_spy.call_count == 1
    assert req_spy.call_args[0][1] == 'owner-9'


def test_daemon_goal_with_boot_owner_and_granted_consent_dispatches(monkeypatch):
    from security.hive_guardrails import GuardrailEnforcer
    _quiet_other_policies(monkeypatch)
    req_spy = _patch_consent(monkeypatch, granted=True)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-9')

    allowed, _, _ = GuardrailEnforcer.before_dispatch(
        'p', goal_dict=FLAGGED, user_id=None)

    assert allowed is True
    assert req_spy.call_count == 0


def test_dispatch_goal_feeds_goal_dict_and_user_to_the_one_gate(monkeypatch):
    """dispatch_goal must load the goal row and pass it to before_dispatch —
    prompt-only left the goal-specific policies dormant on this path."""
    from integrations.agent_engine import dispatch
    from integrations.social import models as social_models
    from security import hive_guardrails as hg

    goal = types.SimpleNamespace(
        id='g-1', to_dict=lambda: dict(FLAGGED))
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = goal

    @contextlib.contextmanager
    def db_session(commit=False):
        yield db

    monkeypatch.setattr(social_models, 'db_session', db_session)

    seen = {}

    def spy_before_dispatch(prompt, goal_dict=None, node_id=None,
                            user_id=None):
        seen['goal_dict'] = goal_dict
        seen['user_id'] = user_id
        return False, 'stop-here', prompt

    monkeypatch.setattr(hg.GuardrailEnforcer, 'before_dispatch',
                        staticmethod(spy_before_dispatch))
    from integrations.agent_engine import budget_gate as bg
    monkeypatch.setattr(bg, 'pre_dispatch_budget_gate',
                        lambda *a, **k: (True, 'ok'))

    result = dispatch.dispatch_goal('p', 'user-1', 'g-1', 'marketing')

    assert result is None
    assert seen['goal_dict'] == FLAGGED
    assert seen['user_id'] == 'user-1'


# ── #96: ONE spelling, `require_consent` ────────────────────────────────
# goal_seeding used to write BOTH require_consent (:1745,1863,1892,1948,2009)
# and requires_consent (:118,514,580) while this gate, the only enforcement
# site, read the singular alone — so every plural-seeded goal dispatched
# UNGATED.  The interim fix read both spellings, which made the typo a second
# working vocabulary.  The plural had no readers anywhere, so the fold goes
# toward the singular (smaller blast radius, gate semantics untouched): the
# three producers were corrected and migrations v56 re-keys already-seeded
# rows.  The two tests below are what keep the gate: the producers can't drift
# back, and an existing row can't lose its gate in the hand-off.
FLAGGED_PLURAL = {'config_json': {'requires_consent': True}}


def test_no_seed_writes_the_plural_spelling():
    """The producer-side guard.

    A plural key in a seed template is silently unenforced — the exact shape of
    #96 — and a copy-paste from an old template is how it comes back.  Pinned on
    the seed source, because that is where the typo is authored.
    """
    import inspect
    from integrations.agent_engine import goal_seeding
    src = inspect.getsource(goal_seeding)
    assert "'requires_consent'" not in src and '"requires_consent"' not in src, (
        "a seed writes requires_consent, which no reader gates on — the goal "
        "would dispatch with no consent check (#96)")


def test_the_gate_reads_exactly_one_spelling():
    """The reader-side guard: no second vocabulary, in either direction.

    Reading both is what this canonicalisation removed; reading only the plural
    would silently ungate the five goals that use the canonical key.
    """
    import inspect
    from security.hive_guardrails import GuardrailEnforcer
    src = inspect.getsource(GuardrailEnforcer.before_dispatch)
    code = '\n'.join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith('#'))
    assert "cfg.get('require_consent')" in code, \
        "the canonical consent trigger is no longer read — goals are ungated"
    assert "cfg.get('requires_consent')" not in code, (
        "the gate reads the legacy spelling again: two vocabularies for one "
        "trigger is the parallel path this fold removed")


def test_gate_files_no_request_when_it_cannot_name_a_human(monkeypatch):
    """The dead end this gate has when nobody can be asked.

    With no user_id AND no HEVOLVE_OWNER_USER_ID (central/regional set it
    NOWHERE — only desktop does, from guest_identity), the gate refuses with
    'without user context' and files NOTHING: blocked, nobody asked, nothing to
    grant. Pinned so it is a KNOWN dead end, not a surprise — it is why callers
    must pass the requester they already hold.
    """
    from security.hive_guardrails import GuardrailEnforcer
    _quiet_other_policies(monkeypatch)
    req_spy = _patch_consent(monkeypatch, granted=False)
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)

    allowed, reason, _ = GuardrailEnforcer.before_dispatch(
        'p', goal_dict=FLAGGED, user_id=None)

    assert allowed is False
    assert 'user context' in reason
    assert req_spy.call_count == 0, (
        "no human to name, so no request can be filed — this is the dead end")


def test_daemon_path_passes_the_goals_owner_as_requester():
    """agent_daemon must not drop the requester it already holds.

    The daemon loop has the goal in hand and AgentGoal.owner_id is a real
    column, but it used to call before_dispatch(prompt, goal.to_dict()) with no
    user_id — so on any topology without HEVOLVE_OWNER_USER_ID every
    consent-flagged goal hit the dead end above. dispatch_goal already passes
    its user_id (dispatch.py); this pins that the daemon path does too, since a
    silent regression here stops goals with no way to grant.
    """
    import inspect
    from integrations.agent_engine import agent_daemon
    src = inspect.getsource(agent_daemon)
    assert 'user_id=goal.owner_id' in src, (
        "daemon before_dispatch must pass the goal's owner as the requester, or "
        "a consent-flagged goal is blocked with nobody to ask")
    # and no guardrail call may pass the goal WITHOUT naming the requester
    for seg in src.split('GuardrailEnforcer.before_dispatch(')[1:]:
        call = seg[:160]
        if 'goal.to_dict()' in call:
            assert 'user_id=' in call, (
                f"requester-less guardrail call is the dead end: {call!r}")


# ── v56: the hand-off, where a gate could be lost ───────────────────────
# Correcting the producers only fixes goals seeded from here on.  The three
# plural goals (seo, paper-explainer, demo-video) are ALREADY in every node's
# DB, and the moment the gate stops reading the plural those rows go ungated
# unless the data moves with the code.  That is the whole risk of this fold, so
# it is tested against the real migration and the real gate, not a stub.

def _goals_db(tmp_path, cfg_json):
    """A stamped-at-55 DB holding one goal with the given raw config_json."""
    import json
    from sqlalchemy import create_engine, text
    from integrations.social import migrations as mig

    engine = create_engine(f"sqlite:///{tmp_path / 'social.db'}")
    mig.Base.metadata.create_all(engine)
    mig.set_schema_version(engine, 55)
    with engine.connect() as conn:
        conn.execute(
            text("INSERT INTO agent_goals (id, goal_type, title, status, "
                 "config_json) VALUES ('g-96', 'marketing', 'seo', 'active', "
                 ":c)"),
            {'c': json.dumps(cfg_json)})
        conn.commit()
    return engine


def _read_cfg(engine, goal_id='g-96'):
    import json
    from sqlalchemy import text
    with engine.connect() as conn:
        raw = conn.execute(
            text("SELECT config_json FROM agent_goals WHERE id = :i"),
            {'i': goal_id}).fetchone()[0]
    return json.loads(raw) if isinstance(raw, (str, bytes)) else raw


def test_v56_rekeys_a_legacy_row_so_it_keeps_its_gate(tmp_path, monkeypatch):
    from integrations.social import migrations as mig
    from security.hive_guardrails import GuardrailEnforcer

    engine = _goals_db(tmp_path, {'bootstrap_slug': 'seo',
                                  'requires_consent': True,
                                  'enabled': True})
    monkeypatch.setattr(mig, 'get_engine', lambda: engine)
    mig.run_migrations()

    cfg = _read_cfg(engine)
    assert cfg.get('require_consent') is True, "the row lost its consent gate"
    assert 'requires_consent' not in cfg, "the legacy key survived the migration"
    assert cfg['bootstrap_slug'] == 'seo' and cfg['enabled'] is True, \
        "the migration disturbed unrelated config keys"
    assert mig.get_schema_version(engine) == 56

    # and the migrated row is actually gated by the real gate
    _quiet_other_policies(monkeypatch)
    req_spy = _patch_consent(monkeypatch, granted=False)
    allowed, reason, _ = GuardrailEnforcer.before_dispatch(
        'p', goal_dict={'config_json': cfg}, user_id='user-1')
    assert allowed is False and 'consent' in reason.lower()
    assert req_spy.call_count == 1


def test_v56_preserves_a_deliberate_false_and_never_invents_a_gate(
        tmp_path, monkeypatch):
    """Renaming a key must not change what it says.

    requires_consent=False is an explicit "no gate"; turning it into True during
    a rename would block a goal the owner left open, which is the mirror-image
    failure of #96 and just as wrong.
    """
    from integrations.social import migrations as mig

    engine = _goals_db(tmp_path, {'requires_consent': False})
    monkeypatch.setattr(mig, 'get_engine', lambda: engine)
    mig.run_migrations()

    cfg = _read_cfg(engine)
    assert cfg.get('require_consent') is False
    assert 'requires_consent' not in cfg


def test_v56_leaves_an_untouched_row_alone(tmp_path, monkeypatch):
    """Idempotence + no collateral: a canonical row is not rewritten."""
    from integrations.social import migrations as mig

    engine = _goals_db(tmp_path, {'require_consent': True, 'other': 'keep'})
    monkeypatch.setattr(mig, 'get_engine', lambda: engine)
    mig.run_migrations()
    mig.run_migrations()   # second pass must be a no-op

    assert _read_cfg(engine) == {'require_consent': True, 'other': 'keep'}
