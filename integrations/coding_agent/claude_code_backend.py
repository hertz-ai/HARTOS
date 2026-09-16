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

Pure stdlib (subprocess/os) at import so both a bare daemon script and the
backend can import it without dragging in heavy deps.  core.subprocess_safe is
the one import-time exception and costs nothing.  The egress accounting below
(core.llm_outbound_logger, core.circuit_breaker, security.dlp_engine) is
imported inside invoke_claude, on the call, and each of those modules is
stdlib-only at import too.

Every call is INSPECTED at this one site, because it is the one place the
text leaves the device: scrubbed by the same DLP engine the message bus
applies to its outbound legs, written to the same llm_outbound.jsonl the
llama calls are written to (source 'claude-code'), and fed to the same
provider breaker under CLAUDE_CODE_PROVIDER_KEY.  Measured 2026-09-16 on the
installed desktop before this: 759 outbound records for the day, all
model='local', none for the expert tier -- the httpx hook captures only the
llama ports, the shim's POST fell to its breaker-only passthrough, and a
subprocess is not HTTP -- so the expert prompt (system text, conversation,
tool results) left for Anthropic with no local record, no redaction, and a
lapsed login invisible to the breaker.
"""
import logging
import os
import shutil
import subprocess
import sys
import time

from core.subprocess_safe import no_window_kwargs

logger = logging.getLogger('hartos_copilot')

CLAUDE_BIN = os.environ.get('HART_CLAUDE_BIN', 'claude')

# Branch coding work is a full agent session that may run long; a frontier
# completion is one answer and must not hold a request open for half an hour.
DEFAULT_AGENTIC_TIMEOUT_S = 1800
DEFAULT_INFERENCE_TIMEOUT_S = 180

#: The whole system prompt of an inference-mode run when the caller sends
#: none: an inference endpoint answers, it does not act.
INFERENCE_SYSTEM_PROMPT = (
    "You are an inference engine. Answer the user's message directly "
    "and only. Do not use tools, do not act on the system.")

#: The key this copilot's standing is tracked under in the ONE provider
#: breaker (core.circuit_breaker.llm_provider_breaker).  A key, not a host:
#: the httpx feed keys hosted providers by hostname, and the shim's loopback
#: hostname is the local llama-server's too, so a lapsed Claude login must
#: not open the breaker for the local model.
CLAUDE_CODE_PROVIDER_KEY = 'claude-code'

_no_redactor_warned = False


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
    if mode == 'inference':
        system = system or INFERENCE_SYSTEM_PROMPT

    # The owner's switch, FIRST, at the one place a `claude -p` is started, so
    # off is off for every consumer (the expert shim's turns and the copilot
    # daemon's branch work) and no path routes around it.  The switch used to
    # gate only claude_code_available(), which the boot-time registration and
    # GET /models consulted and this function did not.  Measured 2026-09-16
    # on the bundled desktop: the owner switched the copilot off in Admin at
    # 15:53:36, and in the six minutes after it 16 expert-tier sessions were
    # written and two `claude.EXE -p` children of Nunba.exe were live, because
    # the backend registered at boot was never re-evaluated and every request
    # that reached the shim spawned.  'off' rides the same ladder as a lapsed
    # subscription: the shim answers 503 and the caller degrades to local.
    if not copilot_enabled():
        result = {'ok': False, 'category': 'off',
                  'error': 'the Claude Code copilot is switched off on this node'}
        _account_egress(mode, system, prompt, result, 0.0)
        return result

    # Scrub BEFORE the text leaves: the same DLP engine the message bus applies
    # to its PeerLink/Crossbar legs, here on the only path to Anthropic.  No
    # redactor means no egress -- the OS does not send raw text off-device
    # because a module failed to import (the bus's leg did exactly that, #73).
    try:
        prompt = _scrub_for_egress(prompt)
        system = _scrub_for_egress(system)
    except ImportError as e:
        _warn_no_redactor(e)
        result = {'ok': False, 'category': 'other',
                  'error': 'DLP redactor unavailable; refusing to send text off-device'}
        _account_egress(mode, system, prompt, result, 0.0, status='no-redactor')
        return result

    start = time.monotonic()
    result = _spawn(prompt, mode=mode, cwd=cwd, timeout_s=timeout_s,
                    model=model, system=system, extra_args=extra_args)
    _account_egress(mode, system, prompt, result,
                    (time.monotonic() - start) * 1000.0)
    return result


def _scrub_for_egress(text):
    """``text`` with PII replaced, by the ONE DLP engine; '' / None unchanged.
    Raises ImportError when the engine is not importable -- the caller
    refuses the egress rather than sending raw text."""
    if not text:
        return text
    from security.dlp_engine import get_dlp_engine
    return get_dlp_engine().redact(text)


def _warn_no_redactor(exc):
    global _no_redactor_warned
    if not _no_redactor_warned:
        _no_redactor_warned = True
        logger.warning("copilot egress REFUSED: the DLP redactor "
                       "(security.dlp_engine) is not importable, so no prompt "
                       "leaves this device until it is: %s", exc)
    else:
        logger.debug("copilot egress refused again: no DLP redactor (%s)", exc)


def _account_egress(mode, system, prompt, result, latency_ms, status=None):
    """One outbound record + the provider breaker's verdict for one call.

    The record is the same llm_outbound.jsonl line the llama calls write,
    through the logger's public hook for non-httpx callers, with an
    OpenAI-shaped body so the record's body policy applies uniformly and
    source 'claude-code' so it is one grep away from the rest.  Status is the
    exit code of a run that completed, else the failure category ('off',
    'notfound', 'timeout', ...), or the explicit ``status`` of a refusal that
    never spawned.

    Only a run that COMPLETED speaks for the provider: a login refusal in its
    stderr feeds the breaker as a failure, a clean exit as a success.  A
    refusal here (switch, redactor) or a spawn failure (binary, timeout) says
    nothing about the account and is not counted.  Never raises; a failure
    to account is logged, not swallowed."""
    try:
        from core.llm_outbound_logger import log_outbound
        from core.circuit_breaker import llm_provider_breaker
        cat = classify_failure(result)
        if status is None:
            status = result['returncode'] if 'returncode' in result else (cat or 'other')
        messages = []
        if system:
            messages.append({'role': 'system', 'content': system})
        messages.append({'role': 'user', 'content': prompt})
        log_outbound({'model': CLAUDE_CODE_PROVIDER_KEY, 'mode': mode,
                      'messages': messages},
                     response_status=status, latency_ms=round(latency_ms, 1),
                     source=CLAUDE_CODE_PROVIDER_KEY)
        if 'returncode' in result:
            if cat is None:
                llm_provider_breaker.record_success(CLAUDE_CODE_PROVIDER_KEY)
            elif cat == 'auth':
                llm_provider_breaker.record_failure(CLAUDE_CODE_PROVIDER_KEY)
    except Exception as e:
        logger.warning("copilot egress not accounted (status=%s): %s", status, e)


def _spawn(prompt, *, mode, cwd, timeout_s, model, system, extra_args):
    """The bounded `claude -p` run itself; text already scrubbed."""
    # Resolved, not the bare name: a service unit or frozen app whose PATH
    # lacks the install dir would otherwise detect the CLI and then fail to
    # spawn it, reporting 'notfound' for a binary this node can see.
    cmd = [_resolve_claude_bin() or CLAUDE_BIN, '-p', prompt]
    if mode == 'inference':
        # Pure completion: text out and NO tools, so it answers rather than
        # acting on the host.
        #
        # --tools "" removes the built-in tools from the model; --allowedTools
        # only withheld PRE-APPROVAL, which is not the same thing.  Measured
        # 2026-09-16 on this desktop, 300 recent expert-tier sessions run with
        # --allowedTools "": 199 contained tool_use blocks, and the model
        # EXECUTED Edit 326x, Grep 374x, Read 267x, Bash 109x, Write 6x --
        # nearly all against Claude Code's own auto-memory directory, whose
        # writes need no approval.  Each "completion" was a small agentic
        # session that read a 25 KB memory index, grepped a 280 KB state file
        # and appended an addendum; and the model re-read its own record of
        # earlier refusals every turn, which is why one goal's refusal held
        # for "the nineteenth consecutive slot".
        #
        # --strict-mcp-config: ignore the mcpServers in settings files, so a
        # completion HARTOS asked for cannot dial back into HARTOS over MCP.
        #
        # --system-prompt REPLACES the harness prompt (memory instructions,
        # CLAUDE.md, tool guidance) instead of appending to it: the caller's
        # system text is the whole system prompt, as an inference endpoint
        # expects, and none of that context is billed into every turn.
        cmd += ['--output-format', 'text', '--tools', '', '--strict-mcp-config']
        cmd += ['--system-prompt', system or INFERENCE_SYSTEM_PROMPT]
    elif system:
        cmd += ['--append-system-prompt', system]
    if model:
        cmd += ['--model', model]
    if extra_args:
        cmd += list(extra_args)

    try:
        # claude is a console-subsystem binary. Nunba.exe is GUI-subsystem and
        # owns no console, so spawning it bare makes Windows allocate a fresh
        # VISIBLE console for the run's lifetime — a cmd window flashing on the
        # user's desktop. no_window_kwargs() returns {} off win32.
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout_s, **no_window_kwargs())
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
        'off'      (the owner's switch)             -> 503 (degrade to local)
        'timeout'                                   -> 504
        'notfound' | 'other'                        -> 502

    Returns None if the result is actually a success (ok and rc == 0).
    """
    if result.get('ok'):
        return None
    cat = result.get('category')
    if cat in ('timeout', 'notfound', 'off'):
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


def _resolve_claude_bin():
    """Absolute path to the Claude Code CLI, or '' if this node has none.

    shutil.which alone is not enough, because PATH differs by DEPLOYMENT SHAPE,
    not just by OS. A frozen Nunba desktop app inherits the launcher's PATH; a
    docker service and a HART OS systemd unit start with a minimal one; and the
    installer drops the binary in a per-user directory none of them necessarily
    carry. So: explicit override, then PATH, then the documented install
    locations per platform. Resolving here also means invoke_claude spawns an
    ABSOLUTE path, so detection and invocation can never disagree.
    """
    override = os.environ.get('HART_CLAUDE_BIN', '').strip()
    if override:
        # Verify even an absolute override exists, or a stale HART_CLAUDE_BIN
        # makes claude_code_available() answer True for a binary that is not
        # there and every call fails at spawn time instead of degrading.
        if os.path.isabs(override):
            return override if os.path.isfile(override) else ''
        return shutil.which(override) or ''

    found = shutil.which(CLAUDE_BIN)
    if found:
        return found

    home = os.path.expanduser('~')
    names = ('claude.exe', 'claude.cmd', 'claude') if os.name == 'nt' else ('claude',)
    roots = [
        os.path.join(home, '.local', 'bin'),              # native installer, all OSes
        os.path.join(home, '.claude', 'local'),           # claude-code local install
        os.path.join(home, 'AppData', 'Roaming', 'npm'),  # npm global, Windows
        '/usr/local/bin', '/usr/bin',                     # linux, docker, HART OS
        '/opt/homebrew/bin',                              # macOS apple silicon
    ]
    for root in roots:
        for name in names:
            cand = os.path.join(root, name)
            if os.path.isfile(cand):
                return cand
    return ''


def _claude_config_dir():
    """Where Claude Code keeps its state, honouring the CLI's own override."""
    d = os.environ.get('CLAUDE_CONFIG_DIR', '').strip()
    return d if d else os.path.join(os.path.expanduser('~'), '.claude')


#: The consent whose grant/revoke acts on the copilot switch (consent_service
#: flips set_copilot_enabled on it).  One flag, two writers: the admin page
#: and the owner's answer to an agent's ask.
COPILOT_CONSENT_TYPE = 'copilot_access'


def _copilot_switch_path():
    """The operator's off-switch marker, in Claude's own config dir: present
    means OFF, absent means on, so an install that predates the switch is
    unchanged."""
    return os.path.join(_claude_config_dir(), 'hartos-copilot.off')


def copilot_enabled():
    """MAY this node use the resident Claude Code copilot (as its expert tier,
    and as an MCP client)?  Distinct from claude_code_available(), which is
    CAN it.  HARTOS_COPILOT_ENABLED=0/1 pins it for headless installs; else
    the marker decides."""
    env = os.environ.get('HARTOS_COPILOT_ENABLED', '').strip().lower()
    if env:
        return env in ('1', 'true', 'yes', 'on')
    return not os.path.exists(_copilot_switch_path())


def set_copilot_enabled(enabled):
    """Flip the switch.  Returns the state IN FORCE, which the env pin can
    make differ from the request.  The MCP token is untouched: turning the
    copilot back on needs no client reconfiguration, unlike a token rotation.

    The flip also brings the EXPERT registry in line (ensure_claude_code_
    registered is a two-way sync), so a switched-off copilot is not OFFERED
    to the selectors either: without this the backend registered at boot
    stayed registered and every expert turn still dialled the shim.  invoke_
    claude's own check is the guarantee; this keeps the offer honest.  A
    function-local import: this module stays light for the bare daemon
    script, which never flips the switch."""
    path = _copilot_switch_path()
    try:
        if enabled:
            if os.path.exists(path):
                os.remove(path)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, 'w').close()
    except OSError as e:
        logger.error("set_copilot_enabled(%s): %s", enabled, e)
    now = copilot_enabled()
    logger.info("copilot switched %s%s", 'on' if now else 'off',
                '' if now == enabled else ' (pinned by HARTOS_COPILOT_ENABLED)')
    try:
        from integrations.agent_engine.model_registry import ensure_claude_code_registered
        ensure_claude_code_registered()
    except Exception as e:
        logger.warning("copilot switch: expert registry not synced (the switch "
                       "still holds at invoke_claude): %s", e)
    return now


def claude_code_available():
    """True if this node can actually run Claude Code: the binary resolves AND
    an authorized credential store exists. The EXPERT-tier registration gates
    on this so a logged-out node simply LACKS a local-frontier model (and falls
    back to hive experts / local), rather than registering a backend that 503s
    on every call.

    Deliberately agnostic of OS and of deployment shape, because HARTOS ships
    bundled inside Nunba, as a standalone docker image, and as HART OS itself,
    and those authenticate differently:

      env token     a container or HART OS unit is configured this way and has
                    no home-directory state at all
      config dir    the desktop case; CLAUDE_CONFIG_DIR overrides the location
      keychain      macOS keeps the OAuth token in the login Keychain, so a
                    logged-in mac has a config dir and NO credentials file.
                    Requiring the file made every mac look logged out.

    The previous version read os.environ['HOME'] directly. HOME is a POSIX
    variable that Windows does not set, so this returned False on every Windows
    node no matter what, two lines before it would have found the credentials
    sitting right there. Measured on a Windows node with a working, logged-in
    CLI: which() resolved claude.EXE, ~/.claude/.credentials.json existed, and
    this still answered False.
    """
    if not copilot_enabled() or not _resolve_claude_bin():
        return False

    # A key or OAuth token is a first-class auth path for the CLI, and is how a
    # container or a HART OS service unit is normally configured.
    for var in ('ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'):
        if os.environ.get(var, '').strip():
            return True

    cdir = _claude_config_dir()
    for name in ('.credentials.json', 'credentials.json'):
        if os.path.exists(os.path.join(cdir, name)):
            return True

    if os.path.exists(os.path.join(os.path.expanduser('~'), '.claude.json')):
        return True

    # macOS: the token lives in the Keychain, not on disk. A config dir plus a
    # resolvable binary is the honest signal there; a stale one degrades to a
    # 401 at call time, which classify_failure already maps cleanly.
    return sys.platform == 'darwin' and os.path.isdir(cdir)
