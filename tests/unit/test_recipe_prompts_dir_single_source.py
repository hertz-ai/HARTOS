"""Recipe SAVE / REUSE / CHECK must resolve the SAME prompts dir in EVERY
deployment mode (bundled desktop, Docker standalone, local dev).

Root cause (2026-06-03, found via the llm_outbound.jsonl draft flood): the recipe
dir was resolved THREE different ways — helper.PROMPTS_DIR (gated on sys.frozen
only), cache_loaders (prefer-local-if-exists), and the daemon reuse-CHECK
(get_prompts_dir, always the user data dir).  They agreed only in the
frozen/bundled build; in Docker (not frozen, WORKDIR /app) the SAVE went to
/app/prompts while the CHECK looked in ~/.config/nunba — so REUSE never matched
and every autonomous goal re-CREATEd forever, flooding the 4B draft model and
starving the user's foreground chat.

Fix: ONE deployment-aware resolver, ``core.platform_paths.get_recipe_prompts_dir``,
shared by all three.  Bundled → writable user data dir; Docker & dev →
code-relative ``prompts/`` (= /app/prompts in the container, <repo>/prompts in
dev).  No extra env; Docker keeps working as is.

Behavioural — calls the real resolver under each simulated mode.  No grep tests.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _recipe_dir():
    from core.platform_paths import get_recipe_prompts_dir
    return get_recipe_prompts_dir()


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def test_bundled_uses_writable_user_data_dir():
    """Bundled desktop (read-only install tree) → the user data dir
    (get_prompts_dir), via either sys.frozen or NUNBA_BUNDLED."""
    from core.platform_paths import get_prompts_dir
    with patch.object(sys, 'frozen', True, create=True):
        assert _recipe_dir() == get_prompts_dir()
    with patch.object(sys, 'frozen', False, create=True), \
         patch.dict(os.environ, {'NUNBA_BUNDLED': '1'}):
        assert _recipe_dir() == get_prompts_dir()


def test_docker_and_dev_share_the_code_relative_prompts_dir():
    """Docker (DOCKER_CONTAINER, NOT frozen) AND local dev resolve to the SAME
    code-relative prompts/ (= /app/prompts in the container, <repo>/prompts in
    dev) — no env needed, and Docker is unchanged from how it already worked."""
    import core.platform_paths as pp
    expected = os.path.join(os.path.dirname(os.path.dirname(pp.__file__)), 'prompts')
    # dev: not frozen, not bundled
    with patch.object(sys, 'frozen', False, create=True), \
         patch.dict(os.environ, {}, clear=False):
        os.environ.pop('NUNBA_BUNDLED', None)
        dev = _recipe_dir()
    # docker: DOCKER_CONTAINER set but still not bundled → SAME dir as dev
    with patch.object(sys, 'frozen', False, create=True), \
         patch.dict(os.environ, {'DOCKER_CONTAINER': 'true'}):
        os.environ.pop('NUNBA_BUNDLED', None)
        docker = _recipe_dir()
    assert _norm(dev) == _norm(expected)
    assert _norm(docker) == _norm(expected), "Docker must keep using /app/prompts"


def test_save_reuse_check_resolvers_all_agree():
    """The SAVE dir (helper.PROMPTS_DIR), the REUSE read dir
    (cache_loaders.PROMPTS_DIR), and the daemon CHECK's resolver
    (get_recipe_prompts_dir) must resolve identically — so a recipe written by
    CREATE is found by REUSE and routed correctly by the daemon."""
    from hartos import helper
    import core.cache_loaders as cl
    from core.platform_paths import get_recipe_prompts_dir
    assert _norm(helper.PROMPTS_DIR) == _norm(cl.PROMPTS_DIR) == _norm(get_recipe_prompts_dir())
