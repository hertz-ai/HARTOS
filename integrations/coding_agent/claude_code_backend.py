"""Single source for invoking the resident Claude Code (`claude -p`).

Both consumers call THIS, never `claude -p` directly, so exactly one place
knows the binary, the node's creds, the model, the timeout, kill-on-timeout,
and error shaping:

  * scripts/hart_copilot_daemon.run_claude  -> mode='agentic'   (branch coding)
  * the autogen EXPERT shim (claude_code_endpoint) -> mode='inference'
                                                     (frontier completion)

Extracted from scripts/hart_copilot_daemon.py, where run_claude was inline and
daemon-only. Without this shared primitive, wiring Claude Code as HARTOS's
frontier inference tier would create a SECOND, parallel `claude -p` invocation
beside the copilot's — the parallel-path trap. One backend, two consumers, the
same pattern as one _tool_impls behind several MCP transports.

Pure stdlib (subprocess/os) so both a bare daemon script and the backend can
import it without dragging in heavy deps.
"""
import os
import subprocess

CLAUDE_BIN = os.environ.get('HART_CLAUDE_BIN', 'claude')

# Branch coding work is a full agent session that may run long; a frontier
# completion is one answer and must not hold a request open for half an hour.
DEFAULT_AGENTIC_TIMEOUT_S = 1800
DEFAULT_INFERENCE_TIMEOUT_S = 180


def invoke_claude(prompt, *, mode='agentic', cwd=None, timeout_s=None,
                  model=None, system=None, extra_args=None):
    """One bounded, headless Claude Code run.

    Returns, on a run that COMPLETED (regardless of exit code):
        {'ok': bool, 'returncode': int, 'stdout': str, 'stderr': str}
    On a failure to run at all (binary missing, timeout, spawn error):
        {'ok': False, 'error': str, 'category': 'notfound'|'timeout'|'other'}

    mode:
      'agentic'   — the copilot's coding runs: full tools, long timeout,
                    cwd = the work repo. Exactly the old run_claude behavior.
      'inference' — a completion for the autogen EXPERT tier: constrained to
                    answer (no tool use), text output, short timeout. The
                    frontier tier wants an ANSWER, not an agent that acts.
    """
    if timeout_s is None:
        timeout_s = (DEFAULT_INFERENCE_TIMEOUT_S if mode == 'inference'
                     else DEFAULT_AGENTIC_TIMEOUT_S)

    cmd = [CLAUDE_BIN, '-p', prompt]
    if mode == 'inference':
        # Pure completion: text out, and no tools so it responds rather than
        # acting on the host. (CLI tool-gating semantics are verified on the
        # box; the system preamble is the belt to --allowedTools' braces.)
        cmd += ['--output-format', 'text', '--allowedTools', '']
        system = system or (
            'You are an inference engine. Answer the user\'s message directly '
            'and only. Do not use tools, do not act on the system.')
    if system:
        cmd += ['--append-system-prompt', system]
    if model:
        cmd += ['--model', model]
    if extra_args:
        cmd += list(extra_args)

    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout_s)
        return {'ok': proc.returncode == 0, 'returncode': proc.returncode,
                'stdout': (proc.stdout or ''), 'stderr': (proc.stderr or '')}
    except FileNotFoundError:
        return {'ok': False, 'error': 'claude not on PATH', 'category': 'notfound'}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'timed out after %ss' % timeout_s,
                'category': 'timeout'}
    except Exception as e:  # never let one bad run take down the caller
        return {'ok': False, 'error': str(e), 'category': 'other'}


def classify_failure(result):
    """Category of a FAILED invoke_claude result, so the shim can map it to an
    HTTP status that rides dispatch.py's existing fallback ladder:

        'overload' (Anthropic 529 / 'overloaded')  -> 503 (transient, breaker)
        'auth'     (login/unauthorized/expired)     -> 503 (degrade to local)
        'timeout'                                   -> 504
        'notfound' | 'other'                        -> 502

    Returns None if the result is actually a success (ok and rc == 0).
    """
    if result.get('ok'):
        return None
    cat = result.get('category')
    if cat in ('timeout', 'notfound'):
        return cat
    blob = ((result.get('stderr') or '') + ' '
            + (result.get('error') or '')).lower()
    if '529' in blob or 'overload' in blob:
        return 'overload'
    if any(w in blob for w in (
            'unauthorized', 'authentication', 'not logged in', 'invalid api key',
            'expired', 'please run /login', '401')):
        return 'auth'
    return 'other'


def claude_code_available():
    """True if this node can actually run Claude Code: the binary resolves AND
    an authorized credential store exists. The EXPERT-tier registration gates
    on this so a logged-out node simply LACKS a local-frontier model (and falls
    back to hive experts / local), rather than registering a backend that 503s
    on every call.
    """
    import shutil
    if not shutil.which(CLAUDE_BIN):
        return False
    home = os.environ.get('HOME', '')
    if not home:
        return False
    # claude-code stores its oauth/creds under ~/.claude. Presence of that dir
    # with a credentials file is the cheapest honest "is it logged in" check.
    cdir = os.path.join(home, '.claude')
    if not os.path.isdir(cdir):
        return False
    for name in ('.credentials.json', 'credentials.json'):
        if os.path.exists(os.path.join(cdir, name)):
            return True
    # Some builds keep creds in .claude.json at HOME root.
    return os.path.exists(os.path.join(home, '.claude.json'))
