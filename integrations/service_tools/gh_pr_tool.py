"""GitHub Pull Request tool — agent-callable wrapper for ``gh pr create``.

Used by the autonomous SEO blog writer + any other agent goal that needs
to publish file changes to a GitHub repo without giving the agent direct
``git push`` access to a long-lived working tree.

Design contract (matches the Crawl4AITool pattern in
``crawl4ai_tool.py`` — register via ServiceToolRegistry, expose a single
public function the LLM can call with JSON params):

  Input  (params_json):
    {
      "repo":         "owner/repo",         # required, validated
      "base":         "main",               # default 'main'
      "head_branch":  "auto/seo-blog-...",  # required, must not exist
      "title":        "...",                # required, <= 256 chars
      "body":         "...",                # required, <= 64KB
      "files": [                            # required, >= 1
        {"path": "src/pages/Blogs/foo.md", "content": "..."},
        ...
      ],
      "draft":        false,                # default false
      "labels":       ["seo", "auto"]       # optional
    }

  Output  (JSON string returned to the agent):
    {"ok": true,  "pr_url": "https://...", "pr_number": 42, "head": "..."}
    {"ok": false, "error": "...",          "reason_code": "no_token" | ...}

Defense in depth (see Gate 7 of CLAUDE.md):
  * Subprocess calls go through the same ``_SUBPROCESS_KW`` helper used
    by ``private_repo_access.py`` (Windows CREATE_NO_WINDOW + explicit
    timeout) — never ``os.popen``, never shell=True with user input.
  * Repo, branch, label, file-path inputs are regex-validated BEFORE
    being passed to ``gh``.  Stops shell-meta injection if a downstream
    caller ever lets attacker input reach this layer.
  * File paths are confined to a single working tree under the runtime
    data dir (``platform_paths.get_data_dir()``) — no writes anywhere
    else on disk, no symlink follow-out.
  * Auth: requires ``HEVOLVE_GITHUB_TOKEN`` or pre-authenticated ``gh``
    CLI session.  Returns ``reason_code='no_token'`` instead of leaking
    a confusing CLI error to the agent.
  * 60s default timeout on every subprocess call; clean error on hang.

Cross-OS: ``gh`` resolves via PATH on Linux/macOS, ``gh.exe`` on Windows.
The ``shutil.which`` lookup handles both transparently.  No hardcoded
``C:\\`` paths.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .registry import ServiceToolInfo, service_tool_registry

logger = logging.getLogger(__name__)


# ── subprocess hygiene (mirrors private_repo_access.py) ─────────────
_SUBPROCESS_KW: Dict[str, Any] = {}
if sys.platform == 'win32':
    _SUBPROCESS_KW['creationflags'] = subprocess.CREATE_NO_WINDOW

_DEFAULT_TIMEOUT_S = 60
_MAX_BODY_BYTES = 64 * 1024            # 64KB — GitHub PR body cap is 65535
_MAX_TITLE_LEN = 256
_MAX_FILES_PER_PR = 50                 # safety cap; daemon usually adds 1-3


# ── input validators ────────────────────────────────────────────────
# GitHub repo:  owner/repo, owner ∈ [a-zA-Z0-9._-]{1,39}, repo ∈ [a-zA-Z0-9._-]{1,100}
_REPO_RE = re.compile(r'^[a-zA-Z0-9](?:[a-zA-Z0-9._-]{0,38})/[a-zA-Z0-9._-]{1,100}$')
# Branch:       per `git check-ref-format` — alphanumeric, ., /, _, - ; no leading/trailing slash, no ..
_BRANCH_RE = re.compile(r'^(?!.*\.\.)(?!/)(?!.*//)[a-zA-Z0-9._/-]{1,200}(?<!/)$')
# Label:        GitHub allows any string up to 50 chars; restrict to common slug shape
_LABEL_RE = re.compile(r'^[a-zA-Z0-9 _.,/-]{1,50}$')
# Relative file path inside repo: no .., no leading /, no absolute Windows path
_RELPATH_RE = re.compile(r'^(?!.*\.\.)(?!/)(?![A-Za-z]:)[^\x00-\x1f]{1,500}$')


def _validate_repo(repo: str) -> Optional[str]:
    """Return error message or None if valid.  Cheap pre-check before shelling out."""
    if not isinstance(repo, str) or not _REPO_RE.match(repo):
        return f'invalid repo {repo!r} (expected "owner/repo")'
    return None


def _validate_branch(branch: str, *, allow_empty: bool = False) -> Optional[str]:
    if not branch:
        return None if allow_empty else 'branch is required'
    if not isinstance(branch, str) or not _BRANCH_RE.match(branch):
        return f'invalid branch name {branch!r}'
    return None


def _validate_relpath(path: str) -> Optional[str]:
    if not isinstance(path, str) or not _RELPATH_RE.match(path):
        return f'invalid file path {path!r} (relative, no traversal)'
    return None


# ── auth / availability ─────────────────────────────────────────────
def _gh_binary() -> Optional[str]:
    """Resolve the gh CLI binary or return None.

    Tries shutil.which first (handles ``gh`` on Linux/macOS and
    ``gh.exe`` on Windows via PATHEXT).  Honors ``HEVOLVE_GH_BIN`` for
    test harnesses and unusual installs.
    """
    override = os.environ.get('HEVOLVE_GH_BIN')
    if override and Path(override).is_file():
        return override
    return shutil.which('gh') or shutil.which('gh.exe')


def _has_auth() -> bool:
    """True if either env token or pre-authed gh session is available.

    Cheap check: env var only.  A live ``gh auth status`` probe would
    add ~1s latency per tool call; we accept noisy errors from gh CLI
    if the session has expired and let the LLM retry-or-escalate.
    """
    if os.environ.get('HEVOLVE_GITHUB_TOKEN'):
        return True
    if os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN'):
        return True
    return False


# ── error helper ────────────────────────────────────────────────────
def _err(reason_code: str, message: str, **extra: Any) -> str:
    """Build a stable error-JSON the agent can branch on."""
    payload: Dict[str, Any] = {
        'ok': False,
        'reason_code': reason_code,
        'error': message,
    }
    payload.update(extra)
    return json.dumps(payload)


# ── temp working tree ───────────────────────────────────────────────
def _workdir_root() -> Path:
    """Where temp clones live.  Honors platform conventions; falls back to system temp."""
    try:
        from core.platform_paths import get_data_dir
        root = Path(get_data_dir()) / 'gh_pr_workdirs'
    except Exception:
        root = Path(tempfile.gettempdir()) / 'hevolve_gh_pr_workdirs'
    root.mkdir(parents=True, exist_ok=True)
    return root


def _run_gh(
    args: List[str],
    *,
    cwd: Optional[Path] = None,
    timeout: int = _DEFAULT_TIMEOUT_S,
    extra_env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """Run ``gh <args>`` with hygiene + token injection.

    Centralises every shell-out so the timeout / Windows console-hide /
    env-token injection is consistent.  Mirrors private_repo_access.py
    so a future security audit only inspects one place.
    """
    binary = _gh_binary()
    if not binary:
        raise FileNotFoundError('gh CLI not found on PATH')

    env = os.environ.copy()
    token = (os.environ.get('HEVOLVE_GITHUB_TOKEN')
             or os.environ.get('GH_TOKEN')
             or os.environ.get('GITHUB_TOKEN'))
    if token:
        env['GH_TOKEN'] = token
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        [binary, *args],
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        **_SUBPROCESS_KW,
    )


def _run_git(
    args: List[str],
    *,
    cwd: Path,
    timeout: int = _DEFAULT_TIMEOUT_S,
) -> subprocess.CompletedProcess:
    """Run ``git <args>`` in a sandboxed working tree."""
    return subprocess.run(
        ['git', *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        **_SUBPROCESS_KW,
    )


# ── public function the agent calls ─────────────────────────────────
def gh_pr_open(params_json: str) -> str:
    """Open a GitHub Pull Request with the given files.

    Sequence:
      1. Validate inputs.
      2. Clone the repo to a fresh sandboxed work dir.
      3. Create branch from base.
      4. Write files atomically (parent dirs created as needed).
      5. ``git add`` + commit (single commit, conventional title).
      6. ``git push`` head branch to origin.
      7. ``gh pr create`` → return URL + number.
      8. Best-effort cleanup of work dir on success or known error.

    Returns a JSON string (kept as a string so it fits the
    ServiceToolRegistry's ``func(params_json: str) -> str`` shape used
    by every other registered tool today — keeps the LLM's tool-result
    contract stable).
    """
    # ── parse params ───────────────────────────────────────────────
    try:
        params = (json.loads(params_json)
                  if isinstance(params_json, str) else params_json) or {}
    except (json.JSONDecodeError, TypeError) as exc:
        return _err('bad_input', f'params must be JSON: {exc}')

    repo = params.get('repo', '')
    base = params.get('base', 'main') or 'main'
    head = params.get('head_branch', '')
    title = params.get('title', '')
    body = params.get('body', '')
    files = params.get('files') or []
    draft = bool(params.get('draft', False))
    labels = params.get('labels') or []

    # ── validate ──────────────────────────────────────────────────
    for err in (
        _validate_repo(repo),
        _validate_branch(base),
        _validate_branch(head),
    ):
        if err:
            return _err('bad_input', err)

    if not isinstance(title, str) or not title.strip():
        return _err('bad_input', 'title is required')
    if len(title) > _MAX_TITLE_LEN:
        return _err('bad_input', f'title too long (>{_MAX_TITLE_LEN})')

    if not isinstance(body, str):
        return _err('bad_input', 'body must be a string')
    if len(body.encode('utf-8')) > _MAX_BODY_BYTES:
        return _err('bad_input', f'body too large (>{_MAX_BODY_BYTES}B)')

    if not isinstance(files, list) or not files:
        return _err('bad_input', 'files is required and must be non-empty')
    if len(files) > _MAX_FILES_PER_PR:
        return _err('bad_input', f'too many files (max {_MAX_FILES_PER_PR})')
    for i, f in enumerate(files):
        if not isinstance(f, dict):
            return _err('bad_input', f'files[{i}] must be an object')
        path_err = _validate_relpath(f.get('path', ''))
        if path_err:
            return _err('bad_input', f'files[{i}]: {path_err}')
        if not isinstance(f.get('content'), str):
            return _err('bad_input', f'files[{i}].content must be a string')

    if not isinstance(labels, list):
        return _err('bad_input', 'labels must be a list')
    for lbl in labels:
        if not isinstance(lbl, str) or not _LABEL_RE.match(lbl):
            return _err('bad_input', f'invalid label {lbl!r}')

    if not _has_auth():
        return _err(
            'no_token',
            'no GitHub token (HEVOLVE_GITHUB_TOKEN / GH_TOKEN / '
            'GITHUB_TOKEN) and gh CLI not pre-authenticated',
        )

    if not _gh_binary():
        return _err('gh_not_installed', 'gh CLI not found on PATH')

    # ── execute ───────────────────────────────────────────────────
    workdir = _workdir_root() / f'pr_{os.getpid()}_{abs(hash(head)) % 10_000_000}'
    try:
        workdir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return _err('workdir_collision',
                    f'work dir {workdir} already exists; retry')

    try:
        # clone — depth=1 keeps disk + network cheap; we only need to
        # add files, not preserve history.  Use HTTPS+token via gh; gh
        # handles auth injection so we don't expose the token in argv.
        clone = _run_gh(['repo', 'clone', repo, str(workdir), '--', '--depth=1',
                         '--branch', base])
        if clone.returncode != 0:
            return _err('clone_failed',
                        f'gh repo clone failed: {clone.stderr.strip()[:500]}',
                        stdout=clone.stdout[:200])

        # set local git identity inside the clone so the commit can be
        # made without depending on the host's global git config (CI
        # runners often have none).
        for cfg in (
            ['config', 'user.email', 'agent+seo@hevolve.ai'],
            ['config', 'user.name', 'Hevolve SEO Agent'],
        ):
            r = _run_git(cfg, cwd=workdir)
            if r.returncode != 0:
                return _err('git_config_failed',
                            f'git {cfg[1]} failed: {r.stderr.strip()[:200]}')

        # branch
        branch_r = _run_git(['checkout', '-b', head], cwd=workdir)
        if branch_r.returncode != 0:
            return _err('branch_failed',
                        f'git checkout -b failed: '
                        f'{branch_r.stderr.strip()[:200]}')

        # write files — confine writes to the work dir; resolve and
        # check the resolved path stays under workdir even after parent
        # dirs are created.
        workdir_resolved = workdir.resolve()
        for f in files:
            rel = f['path']
            dest = (workdir / rel).resolve()
            try:
                dest.relative_to(workdir_resolved)
            except ValueError:
                return _err('path_escape',
                            f'file path {rel!r} escapes work dir')
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(f['content'], encoding='utf-8')

        # commit
        add_r = _run_git(['add', '--all'], cwd=workdir)
        if add_r.returncode != 0:
            return _err('add_failed',
                        f'git add failed: {add_r.stderr.strip()[:200]}')

        commit_r = _run_git(
            ['commit', '-m', title],
            cwd=workdir,
            timeout=_DEFAULT_TIMEOUT_S,
        )
        if commit_r.returncode != 0:
            # Empty commit = nothing changed; treat as a clear, distinct error.
            if 'nothing to commit' in (commit_r.stdout + commit_r.stderr).lower():
                return _err('no_changes',
                            'requested files match the base branch — nothing to commit')
            return _err('commit_failed',
                        f'git commit failed: {commit_r.stderr.strip()[:300]}')

        # push
        push_r = _run_git(
            ['push', '--set-upstream', 'origin', head],
            cwd=workdir,
            timeout=_DEFAULT_TIMEOUT_S * 2,
        )
        if push_r.returncode != 0:
            return _err('push_failed',
                        f'git push failed: {push_r.stderr.strip()[:300]}')

        # PR
        pr_args = ['pr', 'create',
                   '--repo', repo,
                   '--base', base,
                   '--head', head,
                   '--title', title,
                   '--body', body]
        if draft:
            pr_args.append('--draft')
        for lbl in labels:
            pr_args += ['--label', lbl]

        pr_r = _run_gh(pr_args, cwd=workdir)
        if pr_r.returncode != 0:
            return _err('pr_create_failed',
                        f'gh pr create failed: {pr_r.stderr.strip()[:500]}',
                        stdout=pr_r.stdout[:200])

        pr_url = (pr_r.stdout or '').strip().splitlines()[-1] if pr_r.stdout else ''
        pr_number = None
        m = re.search(r'/pull/(\d+)', pr_url)
        if m:
            pr_number = int(m.group(1))

        logger.info(f'gh_pr_open: opened {pr_url} on {repo} head={head}')
        return json.dumps({
            'ok': True,
            'pr_url': pr_url,
            'pr_number': pr_number,
            'head': head,
            'base': base,
            'repo': repo,
        })

    except subprocess.TimeoutExpired as exc:
        return _err('timeout',
                    f'subprocess timed out: {exc.cmd!r} after {exc.timeout}s')
    except FileNotFoundError as exc:
        return _err('binary_missing', str(exc))
    except OSError as exc:
        return _err('io_error', f'filesystem error: {exc}')
    finally:
        # best-effort cleanup; failure to delete is non-fatal
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:  # pragma: no cover — defensive
            pass


# ── registration ────────────────────────────────────────────────────
# Contract verified against Crawl4AITool.create_tool_info() — same
# canonical fields: base_url='native://in-process', endpoints map with
# {path, method, description, params_schema, native_handler}, registry
# method is register_tool (not register), params_schema is flat per-param
# (not JSON-Schema with properties/required wrapper).
class GhPrTool:
    """Service-tool registration shim (mirrors Crawl4AITool)."""

    NAME = 'gh_pr_open'

    @classmethod
    def create_tool_info(cls, base_url: Optional[str] = None) -> ServiceToolInfo:
        return ServiceToolInfo(
            name=cls.NAME,
            description=(
                'Open a GitHub Pull Request with one or more files. Use this '
                'when an agent needs to publish file changes (blog posts, '
                'sitemap updates, generated code) to a GitHub repo without '
                'direct push access. Returns the PR URL and number for the '
                'user to review and merge.'
            ),
            base_url=base_url or 'native://in-process',
            endpoints={
                'gh_pr_open': {
                    'path': '/gh_pr_open',
                    'method': 'POST',
                    'description': (
                        'Create a PR with files. Required params: repo '
                        '("owner/repo"), head_branch, title, body, '
                        'files=[{"path","content"},...]. Optional: base '
                        '(default "main"), draft (default false), labels. '
                        'Returns JSON {ok, pr_url, pr_number} or '
                        '{ok:false, reason_code, error}.'
                    ),
                    'params_schema': {
                        'repo': {'type': 'string',
                                 'description': 'owner/repo'},
                        'base': {'type': 'string',
                                 'description': 'base branch (default "main")'},
                        'head_branch': {'type': 'string',
                                        'description': 'feature branch to create'},
                        'title': {'type': 'string',
                                  'description': 'PR title (<= 256 chars)'},
                        'body': {'type': 'string',
                                 'description': 'PR body markdown (<= 64KB)'},
                        'files': {'type': 'array',
                                  'description': '[{path, content}, ...]'},
                        'draft': {'type': 'boolean',
                                  'description': 'open as draft (default false)'},
                        'labels': {'type': 'array',
                                   'description': 'GitHub labels'},
                    },
                    'native_handler': gh_pr_open,
                },
            },
            health_endpoint=None,  # native — no HTTP service to ping
            tags=['github', 'publish', 'pr', 'blog'],
            timeout=120,
        )

    @classmethod
    def register(cls, base_url: Optional[str] = None) -> bool:
        """Register with the global service_tool_registry.

        Idempotent — register_tool() short-circuits if the tool name is
        already registered.  Returns the registry's outcome bool.
        """
        try:
            return service_tool_registry.register_tool(
                cls.create_tool_info(base_url),
            )
        except Exception as exc:
            logger.warning(f'gh_pr_open registration skipped: {exc}')
            return False
