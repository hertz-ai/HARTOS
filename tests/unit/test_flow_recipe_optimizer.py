"""Behavioural tests for core.flow_recipe_optimizer — the ACT half of the
flywheel's Gate 4 (measure -> improve).

The optimizer retires an underperforming flow recipe by RENAME (never delete) +
a reward sidecar, so a re-CREATE that turns out WORSE can be rolled back to the
proven-better original:

    archive (recipe -> .optbak + meta) -> daemon re-CREATEs -> accept | rollback

0% covered before this file. These drive the real functions against a tmp
prompts dir and assert the filesystem state machine + the rollback SAFETY
(the restored recipe is byte-identical to the retired one) + the anti-churn
guard (#85: never stack archives). Real I/O, no source-substring checks.

    python -m pytest tests/unit/test_flow_recipe_optimizer.py -q --noconftest
"""
from __future__ import annotations

import json
import os

import pytest

from core import flow_recipe_optimizer as opt

PID, FLOW = "42", 3
_ORIGINAL = '{"actions": ["a", "b"], "note": "the proven-better recipe"}'


@pytest.fixture(autouse=True)
def _prompts_dir(monkeypatch, tmp_path):
    """Point the module's single-source dir resolver at a throwaway dir so the
    archive/restore renames happen in isolation."""
    monkeypatch.setattr(opt, "_resolve_prompts_dir", lambda pd=None: str(tmp_path))
    return tmp_path


def _write_recipe(tmp_path, content=_ORIGINAL):
    p = os.path.join(str(tmp_path), f"{PID}_{FLOW}_recipe.json")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(content)
    return p


# ── archive ─────────────────────────────────────────────────────────────────
class TestArchive:
    def test_archive_retires_recipe_and_writes_sidecar(self, _prompts_dir):
        recipe = _write_recipe(_prompts_dir)
        acted = opt.archive_recipe_for_reoptimization(PID, FLOW, reward=0.3)
        assert acted is True
        assert not os.path.exists(recipe), "live recipe must be renamed away"
        assert os.path.exists(recipe + ".optbak"), "retired recipe must be kept"
        assert opt.has_pending_optimization(PID, FLOW) is True

    def test_archive_preserves_original_bytes_in_optbak(self, _prompts_dir):
        _write_recipe(_prompts_dir)
        opt.archive_recipe_for_reoptimization(PID, FLOW, reward=0.3)
        with open(os.path.join(str(_prompts_dir),
                               f"{PID}_{FLOW}_recipe.json.optbak"),
                  encoding="utf-8") as fh:
            assert fh.read() == _ORIGINAL

    def test_sidecar_records_reward_and_ids_with_correct_types(self, _prompts_dir):
        _write_recipe(_prompts_dir)
        opt.archive_recipe_for_reoptimization(PID, FLOW, reward=0.375)
        meta = os.path.join(str(_prompts_dir),
                            f"{PID}_{FLOW}_recipe.json.optbak.meta.json")
        with open(meta, encoding="utf-8") as fh:
            d = json.load(fh)
        assert d["archived_reward"] == 0.375
        assert d["prompt_id"] == "42" and d["flow_id"] == 3

    def test_archive_is_noop_when_recipe_missing(self, _prompts_dir):
        assert opt.archive_recipe_for_reoptimization(PID, FLOW, reward=0.1) is False
        assert opt.has_pending_optimization(PID, FLOW) is False

    def test_archive_is_noop_when_already_optimizing_antichurn(self, _prompts_dir):
        _write_recipe(_prompts_dir)
        assert opt.archive_recipe_for_reoptimization(PID, FLOW, reward=0.3) is True
        # A daemon re-CREATE lands a fresh recipe while the .optbak still sits.
        _write_recipe(_prompts_dir, content='{"fresh": true}')
        # Second archive must NOT stack (would re-enter the #85 re-CREATE churn).
        assert opt.archive_recipe_for_reoptimization(PID, FLOW, reward=0.2) is False
        # The ORIGINAL retired recipe is untouched (reward still the first one).
        assert opt.archived_reward(PID, FLOW) == 0.3


# ── archived_reward ─────────────────────────────────────────────────────────
class TestArchivedReward:
    def test_returns_reward_when_pending(self, _prompts_dir):
        _write_recipe(_prompts_dir)
        opt.archive_recipe_for_reoptimization(PID, FLOW, reward=0.42)
        assert opt.archived_reward(PID, FLOW) == 0.42

    def test_none_when_nothing_archived(self, _prompts_dir):
        assert opt.archived_reward(PID, FLOW) is None

    def test_none_on_malformed_sidecar(self, _prompts_dir):
        meta = os.path.join(str(_prompts_dir),
                            f"{PID}_{FLOW}_recipe.json.optbak.meta.json")
        with open(meta, "w", encoding="utf-8") as fh:
            fh.write("not json{")
        assert opt.archived_reward(PID, FLOW) is None


# ── accept ──────────────────────────────────────────────────────────────────
class TestAccept:
    def test_accept_drops_backup_and_sidecar(self, _prompts_dir):
        _write_recipe(_prompts_dir)
        opt.archive_recipe_for_reoptimization(PID, FLOW, reward=0.3)
        assert opt.accept_reoptimization(PID, FLOW) is True
        assert opt.has_pending_optimization(PID, FLOW) is False
        assert opt.archived_reward(PID, FLOW) is None

    def test_accept_is_noop_when_nothing_pending(self, _prompts_dir):
        assert opt.accept_reoptimization(PID, FLOW) is False


# ── rollback ────────────────────────────────────────────────────────────────
class TestRollback:
    def test_rollback_restores_the_exact_original_recipe(self, _prompts_dir):
        recipe = _write_recipe(_prompts_dir)
        opt.archive_recipe_for_reoptimization(PID, FLOW, reward=0.9)
        # daemon re-CREATE produced a WORSE recipe:
        _write_recipe(_prompts_dir, content='{"worse": true}')
        assert opt.rollback_recipe(PID, FLOW) is True
        # the proven-better original is live again, byte-identical...
        with open(recipe, encoding="utf-8") as fh:
            assert fh.read() == _ORIGINAL
        # ...and the pending state is cleared.
        assert opt.has_pending_optimization(PID, FLOW) is False
        assert opt.archived_reward(PID, FLOW) is None

    def test_rollback_is_noop_when_nothing_pending(self, _prompts_dir):
        assert opt.rollback_recipe(PID, FLOW) is False


# ── recipe_exists + full lifecycle ──────────────────────────────────────────
class TestLifecycle:
    def test_recipe_exists_tracks_presence(self, _prompts_dir):
        assert opt.recipe_exists(PID, FLOW) is False
        _write_recipe(_prompts_dir)
        assert opt.recipe_exists(PID, FLOW) is True

    def test_archive_then_accept_leaves_no_backup(self, _prompts_dir):
        _write_recipe(_prompts_dir)
        opt.archive_recipe_for_reoptimization(PID, FLOW, reward=0.3)
        _write_recipe(_prompts_dir, content='{"better": true}')  # re-CREATE
        opt.accept_reoptimization(PID, FLOW)
        assert opt.recipe_exists(PID, FLOW) is True
        assert opt.has_pending_optimization(PID, FLOW) is False

    def test_archive_then_rollback_round_trips_to_original(self, _prompts_dir):
        _write_recipe(_prompts_dir)
        opt.archive_recipe_for_reoptimization(PID, FLOW, reward=0.7)
        opt.rollback_recipe(PID, FLOW)
        assert opt.recipe_exists(PID, FLOW) is True
        assert opt.has_pending_optimization(PID, FLOW) is False
