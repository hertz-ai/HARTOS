"""
integrations.vlm.safety — guards for the VLM action pipeline.

Phase 6 of memory/vlm_best_of_all_worlds_plan.md §5.  Three layers
of protection between the VLM's decisions and the user's screen:

  1. SessionGuard       per-session action cap + per-second throttle
                        (avoid runaway loops spamming clicks)
  2. WindowBlocklist    refuse to click in sensitive apps
                        (lsass / password managers / banking-titled
                         windows) and an admin-overridable allowlist
  3. AuditLogger        JSONL trail at ~/.nunba/audit/vlm_actions_*.jsonl
                        with timestamp / window / coords / hash / exit
                        code so post-incident review can reconstruct
                        what the VLM did

All three are OPT-IN via ``execute_action(..., safety=True)`` so
existing call sites stay unchanged unless they explicitly opt in.
The plan §5 calls these out as production-readiness, not always-
on hard limits.

computer_control_block is different: it is the owner's permission for an
agent to act on this computer at all, and it is not opt-in.
run_local_agentic_loop and hart_intelligence_entry._handle_shell_command_tool
call it before anything runs.

Configuration via ``SafetyConfig`` dataclass; module-level singletons
returned by ``get_session_guard()`` / ``get_audit_logger()``.  The
session guard is reset via ``reset_session_guard()``, which
``run_local_agentic_loop`` calls at the start of every goal so each goal
gets its own action budget.
"""

import collections
import hashlib
import json
import logging
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

logger = logging.getLogger('hevolve.vlm.safety')


# Computer-use may receive prose in any language, but the operating-system
# operations it can ultimately invoke have a finite, stable vocabulary.  This
# is deliberately an always-on deny policy: consent to control a computer is
# never consent to power it off, reset it, or erase it.
_DESTRUCTIVE_COMMAND_RE = re.compile(
    r'(?imx)(?:'
    r'(?:^|[;&|]\s*)(?:cmd(?:\.exe)?\s+/c\s+|powershell(?:\.exe)?\s+[^\n]*?\s+)?'
    r'(?:shutdown(?:\.exe)?\b|restart-computer\b|stop-computer\b|'
    r'reboot\b|poweroff\b|halt\b|systemctl\s+(?:reboot|poweroff|halt)\b|'
    r'init\s+[06]\b|adb\s+reboot\b|diskpart\b|mkfs(?:\.[\w-]+)?\b|'
    r'format(?:\.com)?\s+(?:[a-z]:|/|disk\b|volume\b)|'
    r'rm\s+-[^\n]*r[^\n]*f\s+(?:[/~]|[a-z]:[\\/]))|'
    r'\b(?:factory[\s_-]*reset|reset[\s_-]*this[\s_-]*pc)\b)'
)

# Early refusal for plain-language requests.  The final command/action gate
# above is authoritative and language-independent; these terms only avoid
# handing an obviously destructive request to a GUI planner first.
_DESTRUCTIVE_REQUEST_RE = re.compile(
    r'(?ix)(?:'
    r'\b(?:shutdown|shut[\s-]*down|restart|reboot|power[\s-]*off|'
    r'hibernate|sleep)\b(?:\s+(?:the|this|my))?\s+'
    r'(?:computer|pc|machine|device|system|windows|phone|tablet)\b|'
    r'\b(?:computer|pc|machine|device|system|windows|phone|tablet)\b'
    r'(?:\s+(?:should|must|can|please|now|to))*\s+'
    r'(?:shutdown|shut[\s-]*down|restart|reboot|power[\s-]*off|hibernate|sleep)\b|'
    r'\bfactory[\s-]*reset\b|'
    r'\u91cd\u542f|\u5173\u673a|\u6062\u590d\u51fa\u5382\u8bbe\u7f6e|'
    r'\u092a\u0941\u0928\u0930\u094d\u092d\u0942\u0924|\u0930\u0940\u0938\u094d\u091f\u093e\u0930\u094d\u091f|'
    r'\u092c\u0902\u0926\s*\u0915\u0930|'
    r'\u0440\u0435\u0436\u0438\u043c\s+\u043f\u0435\u0440\u0435\u0437\u0430\u0433\u0440\u0443\u0437\u043a\u0438)'
)


# Stopping a process: every verb that ends one, in any shell.  Matched on the
# de-obfuscated text (_deobfuscate), never compiled from the command itself.
_KILL_VERB_RE = re.compile(
    r'(?:taskkill|stop-process|\bspps\b|\bp?kill(?:all)?\b|'
    r'\.(?:kill|terminate)\s*\(|stop-service|\bsc(?:\.exe)?\s+stop\b|'
    r'\bnet\s+stop\b|systemctl\s+(?:stop|kill)\b|'
    r'\bwmic\b[^\n]*\b(?:delete|terminate)\b)')
# The target arrives from elsewhere (a pipe, a variable, xargs), so the
# command text does not say which process it stops.
_UNRESOLVED_TARGET_RE = re.compile(
    r'\|\s*(?:stop-process|spps|xargs)\b|\$')
_NUMBER_RE = re.compile(r'\b\d+\b')
# cmd caret escapes, PowerShell backtick escapes, and %VAR% expansions hide a
# verb from a plain search ("task^kill", "Stop`-Process", "%comspec% /c ...").
_OBFUSCATION_RE = re.compile(r'[\^`]|%[^%\s]*%')


def _own_process_identity():
    """(pids, names) of the assistant's own processes.  Raises when they
    cannot be read; the caller then refuses any kill."""
    import psutil
    from core.resource_governor import get_governor
    pids = get_governor().own_process_pids(include_parent=True)
    names = set()
    for pid in pids:
        try:
            name = psutil.Process(pid).name().casefold()
        except Exception:  # noqa: BLE001 -- exited mid-walk (M1)
            continue
        names.add(name[:-4] if name.endswith('.exe') else name)
    return pids, names


def _deobfuscate(text: str) -> str:
    return _OBFUSCATION_RE.sub('', text)


def _own_process_kill(text: str) -> Optional[str]:
    """Refusal when ``text`` stops one of the assistant's own processes, or
    stops a process it does not name (#877: an agent ran
    ``Get-Process | Where-Object {$_.Name -like '*Nunba*'} | Stop-Process``
    and Nunba exited under its owner)."""
    text = _deobfuscate(text)
    if not _KILL_VERB_RE.search(text):
        return None
    try:
        pids, names = _own_process_identity()
    except Exception as e:  # noqa: BLE001 -- unknown "own" is a no
        return ('own_process_kill: the assistant\'s own processes could not '
                f'be identified ({e}), so no process may be stopped')
    if any(int(n) in pids for n in _NUMBER_RE.findall(text)):
        return 'own_process_kill: the command stops one of the assistant\'s own processes'
    if any(name and name in text for name in names):
        return 'own_process_kill: the command stops one of the assistant\'s own processes'
    if _UNRESOLVED_TARGET_RE.search(text):
        return ('own_process_kill: the command stops processes it does not '
                'name, so it could stop the assistant itself')
    return None


def destructive_computer_operation(value) -> Optional[str]:
    """Return a refusal reason for a power/reset/erase operation.

    Accepts either a tool instruction or a concrete action dict.  Every
    computer-use dispatcher calls this same function before it transfers
    control, and ``execute_action`` calls it again immediately before the OS
    action so a remote or VLM-generated action cannot bypass the policy.
    """
    if isinstance(value, dict):
        parts = (value.get(key) for key in
                 ('command', 'text', 'value', 'path', 'reasoning', 'Reasoning'))
        text = '\n'.join(str(part) for part in parts if part)
        # What would actually run; the model's reasoning is prose about it.
        runs = '\n'.join(str(value.get(key)) for key in
                         ('command', 'text', 'value') if value.get(key))
    else:
        text = runs = '' if value is None else str(value)
    text = unicodedata.normalize('NFKC', text).casefold()
    if _DESTRUCTIVE_COMMAND_RE.search(text):
        return 'destructive_computer_operation: power, reset, erase, or format commands are never agent-executable'
    if _DESTRUCTIVE_REQUEST_RE.search(text):
        return 'destructive_computer_operation: power, reset, erase, or format requests require a human to act directly'
    return _own_process_kill(unicodedata.normalize('NFKC', runs).casefold())


def computer_operation_refusal(value) -> Optional[str]:
    """Shared synchronous hard-deny policy for every execution hand-off.

    Semantic policy review belongs to the existing CREATE/REUSE StatusVerifier
    conversation, where it can be attributed and attached to the action
    ledger.  A dispatcher must never make a separate best-effort model call or
    infer an allow because that review is unavailable.  Its synchronous job is
    the deterministic final deny check below.
    """
    return destructive_computer_operation(value)


# ─── Defaults ─────────────────────────────────────────────────────────

#: Process names that must never receive VLM clicks.  Lowercased.
#: Includes Windows credential broker (lsass), session manager
#: (winlogon), known password managers, and the Windows logon UI
#: (LogonUI.exe).  Admins may extend at runtime via
#: ``SafetyConfig(blocked_processes=...)``.
DEFAULT_BLOCKED_PROCESSES: Tuple[str, ...] = (
    'lsass.exe', 'winlogon.exe', 'logonui.exe', 'consent.exe',
    'bitwarden.exe', '1password.exe', 'keepass.exe', 'keepassxc.exe',
    'lastpass.exe', 'dashlane.exe', 'enpass.exe',
)

#: Window-title regex patterns that suggest sensitive content.
#: Case-insensitive.  Designed to be conservative — false positives
#: are recoverable (user can override per-window), false negatives
#: are not.
DEFAULT_BLOCKED_TITLE_PATTERNS: Tuple[str, ...] = (
    r'\b(?:online[\s-]?)?bank(?:ing)?\b',
    r'\bcredit[\s-]?card\b',
    r'\b(?:enter|change|reset)[\s-]+password\b',
    r'\b(?:UAC|elevation|administrator)\s*prompt\b',
    r'\b(?:pin|cvv|security[\s-]?code)\b',
)


# ─── Configuration ────────────────────────────────────────────────────

@dataclass
class SafetyConfig:
    """Tuneable knobs.  All env-overridable so per-host policies
    don't require code changes."""

    max_actions_per_session: int = int(
        os.environ.get('HEVOLVE_VLM_MAX_ACTIONS_PER_SESSION', '100'))
    max_actions_per_second: float = float(
        os.environ.get('HEVOLVE_VLM_MAX_ACTIONS_PER_SECOND', '5.0'))
    blocked_processes: Tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_BLOCKED_PROCESSES)
    blocked_title_patterns: Tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_BLOCKED_TITLE_PATTERNS)
    audit_enabled: bool = (
        os.environ.get('HEVOLVE_VLM_AUDIT_ENABLED', '1') not in ('0', 'false', 'no'))
    # Override with HEVOLVE_VLM_AUDIT_DIR; empty default → ~/.nunba/audit
    # via _default_dir().
    audit_dir: str = field(
        default_factory=lambda: os.environ.get('HEVOLVE_VLM_AUDIT_DIR', ''))


# ─── Session guard (count + throttle) ─────────────────────────────────

class SessionGuard:
    """Tracks per-session action count + per-second rate.

    Returns a non-None block reason string from :meth:`check` when the
    limit has been reached; the caller MUST treat this as a refusal
    to act.  :meth:`record` is called after a successful action to
    increment counters.

    Thread-safe: a single lock protects counter updates so concurrent
    VLM calls (e.g. the agentic loop dispatching from a worker pool)
    don't double-count.
    """

    def __init__(self, config: Optional[SafetyConfig] = None):
        self.config = config or SafetyConfig()
        self.action_count: int = 0
        # Bounded deque so memory doesn't grow unbounded over a long
        # session; capacity covers ~1 second of max-rate actions.
        self.recent_action_times: collections.deque = collections.deque(
            maxlen=max(64, int(self.config.max_actions_per_second * 4)))
        self._lock = threading.Lock()

    def check(self) -> Optional[str]:
        """Return None when OK; otherwise a reason string."""
        with self._lock:
            if self.action_count >= self.config.max_actions_per_session:
                return (f'session-cap reached '
                        f'({self.config.max_actions_per_session} actions)')
            now = time.time()
            recent = sum(
                1 for t in self.recent_action_times if now - t < 1.0)
            if recent >= self.config.max_actions_per_second:
                return (f'throttle exceeded '
                        f'(>{self.config.max_actions_per_second}/s)')
        return None

    def record(self) -> None:
        with self._lock:
            self.action_count += 1
            self.recent_action_times.append(time.time())

    def reset(self) -> None:
        with self._lock:
            self.action_count = 0
            self.recent_action_times.clear()


# ─── Window blocklist ─────────────────────────────────────────────────

def is_window_blocked(window_meta: Optional[dict],
                      config: Optional[SafetyConfig] = None
                      ) -> Optional[str]:
    """Return a block-reason string when the window is sensitive,
    None otherwise.  Safe to call with ``window_meta=None`` (returns
    None — no info to block on).

    ``window_meta`` is the dict shape :func:`integrations.remote_desktop.
    window_capture.list_windows` returns: ``{title, process_name, ...}``.
    """
    if not window_meta:
        return None
    config = config or SafetyConfig()
    pname = (window_meta.get('process_name') or '').lower().strip()
    if pname:
        for blocked in config.blocked_processes:
            blocked_l = blocked.lower()
            if pname == blocked_l or pname.endswith('\\' + blocked_l) \
                    or pname.endswith('/' + blocked_l):
                return f'process_blocked: {pname}'
    title = window_meta.get('title') or ''
    for pat in config.blocked_title_patterns:
        if re.search(pat, title, re.IGNORECASE):
            return f'title_pattern_blocked: "{title[:60]}" matches /{pat}/'
    return None


# ─── Fabricated-credential guard ──────────────────────────────────────

# RFC 2606 / RFC 6761 reserve these for documentation and testing, so an
# address inside one can never be a real account.  Typing it into a live
# login form is therefore always a fabrication, never a user credential.
RESERVED_CREDENTIAL_DOMAINS: Tuple[str, ...] = (
    'example.com', 'example.org', 'example.net', 'example.edu',
)
RESERVED_CREDENTIAL_TLDS: Tuple[str, ...] = (
    '.example', '.invalid', '.test', '.localhost',
)

# Local-parts a model writes when standing in for a value it does not have.
# Deliberately limited to the unambiguous "your*" family plus an explicit
# placeholder marker.  'user', 'admin', 'test' and 'email' are NOT here on
# purpose: user@realcompany.com and admin@realcompany.com are perfectly real
# addresses, and blocking them would break legitimate typing (the observed
# user@example.com is already caught by its reserved DOMAIN, which is the
# unambiguous half of the signal).
PLACEHOLDER_LOCAL_PARTS: Tuple[str, ...] = (
    'youremail', 'your_email', 'your-email', 'yourname', 'your_name',
    'your-name', 'yourusername', 'your_username', 'placeholder',
)

# A bare credential-shaped token: no whitespace, exactly one '@', dotted
# domain.  Anchored on purpose -- prose that merely mentions example.com
# contains spaces and so cannot match, which keeps ordinary typing working.
_CREDENTIAL_TOKEN = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


def is_placeholder_credential(action: Optional[dict]) -> Optional[str]:
    """Return a block-reason when a 'type' action would enter a FABRICATED
    credential, None otherwise.  Same contract as :func:`is_window_blocked`.

    Measured live 2026-09-10: the VLM loop reached x.com's login form with no
    credential, invented one, typed it, and reported ok:True --
    'Typed: user@example.com...' then 'Typed: your_email@example.com...'.
    Repeated bad logins against a real account trip rate-limiting and
    security locks, so the refusal has to happen before pyautogui runs.

    Scope is intentionally narrow (see PLACEHOLDER_LOCAL_PARTS): only a
    'type' action whose ENTIRE trimmed text is one credential-shaped token.
    """
    if not action or action.get('action') != 'type':
        return None
    text = (action.get('text') or action.get('value') or '').strip()
    if not text or not _CREDENTIAL_TOKEN.match(text):
        return None

    local, _, domain = text.rpartition('@')
    domain_l = domain.lower()
    if domain_l in RESERVED_CREDENTIAL_DOMAINS:
        return (f'placeholder_credential: "{text}" uses reserved '
                f'documentation domain "{domain_l}" (RFC 2606)')
    for tld in RESERVED_CREDENTIAL_TLDS:
        if domain_l.endswith(tld):
            return (f'placeholder_credential: "{text}" uses reserved '
                    f'TLD "{tld}" (RFC 6761)')
    if local.lower() in PLACEHOLDER_LOCAL_PARTS:
        return (f'placeholder_credential: "{text}" has placeholder '
                f'local-part "{local}"')
    return None


# ─── Permission to control this computer ──────────────────────────────

#: What a computer_control grant lets an agent do.  The ask carries this
#: text, so every client shows the same list.
COMPUTER_CONTROL_COVERS = ('run shell commands, write files, move the mouse, '
                           'type on the keyboard and open apps on this '
                           'computer')

#: How long a turn someone is watching waits for the owner's answer, and how
#: often it looks.  A background (daemon) run does not wait.
COMPUTER_CONTROL_WAIT_SECONDS = 90.0
COMPUTER_CONTROL_POLL_SECONDS = 3.0


def _known_agent(agent_id) -> Optional[str]:
    """The asking agent's id, or None when no agent is known.

    Callers pass the prompt id they hold: None or '' when there is none, and
    hart_intelligence_entry._handle_computer_action_tool sends
    str(prompt_id or 0), so '0' too.  An unknown agent is never guessed.
    """
    text = '' if agent_id is None else str(agent_id).strip()
    return None if text in ('', '0', 'None') else text


def _computer_control_answer(owner: str, agent: Optional[str],
                             reason: str) -> Optional[bool]:
    """One look at the owner's answer: True allowed, False said no ("Don't
    allow" on the ask), None not answered yet, in which case the ask is filed
    or sent again (request_consent dedupes it to one card)."""
    from integrations.social.models import db_session
    from integrations.social.consent_service import ConsentService
    with db_session(commit=True) as db:
        if ConsentService.check_or_request(
                db, owner, 'computer_control', agent_id=agent, reason=reason):
            return True
        if ConsentService.declined(db, owner, 'computer_control',
                                   agent_id=agent):
            return False
        return None


def computer_control_block(agent_id, *, sleep=time.sleep) -> Optional[str]:
    """Return a refusal when the owner has not allowed agents to control this
    computer, None when they have.  Same contract as is_window_blocked.

    Live 2026-09-14 the VLM loop wrote C:\\Users\\Public\\search_llm_config.py
    and ran it for agent 88659566083, and nothing asked the person at the
    desk.  run_local_agentic_loop and
    hart_intelligence_entry._handle_shell_command_tool call this before
    anything runs.

    The owner is whose machine this is: HEVOLVE_OWNER_USER_ID, which Nunba
    exports at boot (the signed-in user, else this desktop's guest), read on
    every call.  With no owner nobody can be asked, so the answer is no.

    Without a grant the owner is asked through ConsentService, so the ask
    reaches their devices and the privacy page lists and revokes the grant.
    A turn someone is watching waits up to COMPUTER_CONTROL_WAIT_SECONDS for
    the answer; a daemon run does not wait.  When the owner says no ("Don't
    allow" on the ask) the run is refused at once, and that agent's later
    runs are refused without asking until the owner allows agents again: a
    no stands (hartos-3e ruling (a)).  A grant covers every agent until
    user_consents can hold per-agent grants: its UNIQUE constraint rejects a
    per-agent grant once a per-agent ask exists.  A check that fails is a no.
    """
    owner = os.environ.get('HEVOLVE_OWNER_USER_ID')
    if not owner:
        logger.warning('computer control refused: no owner identity '
                       '(HEVOLVE_OWNER_USER_ID is not set)')
        return ('Not run: nobody is signed in on this computer who could '
                'allow an agent to control it.')
    agent = _known_agent(agent_id)
    reason = (f'Agent {agent} asks to {COMPUTER_CONTROL_COVERS}.' if agent
              else 'An agent that could not be identified asks to '
                   f'{COMPUTER_CONTROL_COVERS}.')
    try:
        from integrations.agent_engine.dispatch import (
            is_current_request_autonomous)
        background = is_current_request_autonomous()
    except Exception:  # noqa: BLE001 -- unknown is a watched turn: it waits
        background = False
    started = time.monotonic()
    deadline = started + (0.0 if background
                          else COMPUTER_CONTROL_WAIT_SECONDS)
    # One line when the wait starts and one for how it ends, not one per look.
    waited = False
    while True:
        try:
            answer = _computer_control_answer(owner, agent, reason)
        except Exception as e:  # noqa: BLE001 -- a failed check is a no
            logger.warning(f'computer control refused for agent {agent}: '
                           f'the permission could not be checked: {e}')
            return ('Not run: the permission to control this computer '
                    f'could not be checked ({e}).')
        if answer:
            if waited:
                logger.info(f'computer control allowed for agent {agent} '
                            f'after {time.monotonic() - started:.0f}s')
            return None
        if answer is False:
            logger.warning(f'computer control refused for agent {agent}: '
                           f'owner {owner} said no')
            who = (f'agent {agent}' if agent
                   else 'an agent that could not be identified')
            return (f'Not run: the owner of this computer said no to {who} '
                    'controlling it. They can allow agents again on the '
                    'privacy page.')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if not waited:
            waited = True
            logger.info(f'computer control: asked owner {owner} for agent '
                        f'{agent}, waiting up to '
                        f'{COMPUTER_CONTROL_WAIT_SECONDS:.0f}s')
        sleep(min(COMPUTER_CONTROL_POLL_SECONDS, remaining))
    why = 'daemon run, not waited for' if background else 'no answer in time'
    logger.warning(f'computer control refused for agent {agent}: owner '
                   f'{owner} has not allowed it ({why})')
    if background:
        return ('Not run: the owner has not allowed agents to control this '
                'computer. They have been asked; this can run once they '
                'allow it.')
    return ('Not run: the owner did not allow agents to control this '
            f'computer within {COMPUTER_CONTROL_WAIT_SECONDS:.0f}s. They can '
            'allow it and ask again.')


# ─── Audit logger ─────────────────────────────────────────────────────

#: Longest command the audit keeps: enough to read a script path or a
#: one-liner, short enough that a heredoc does not copy the script in.
AUDIT_COMMAND_MAX_CHARS = 500


def _redacted(text, limit: int) -> str:
    """``text`` with secrets replaced by the canonical redactor, then cut.

    A command or typed text can carry a password.  If the redactor cannot
    run, the text is withheld rather than written raw.
    """
    if not text:
        return ''
    try:
        from security.secret_redactor import redact_secrets
        return redact_secrets(str(text))[0][:limit]
    except Exception as e:  # noqa: BLE001 -- the record must still be written
        logger.warning(f'audit redaction unavailable, text withheld: {e}')
        return '[withheld: redactor unavailable]'


class AuditLogger:
    """Append-only JSONL audit trail of every VLM action."""

    def __init__(self, config: Optional[SafetyConfig] = None):
        self.config = config or SafetyConfig()
        self.path: Optional[str] = None
        self._lock = threading.Lock()
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        target = self.config.audit_dir or self._default_dir()
        try:
            os.makedirs(target, exist_ok=True)
            self.path = target
        except Exception as e:
            logger.warning(f'audit dir create failed for {target}: {e}')
            self.path = None  # disables logging

    def _default_dir(self) -> str:
        """Audit log location.

        Plan §5 spec: ``~/.nunba/audit/vlm_actions_{date}.jsonl``.
        Reviewer flagged the prior implementation deferred to
        ``platform_paths.get_data_dir()`` which gave platform-correct
        paths but didn't match the plan literally.  Resolution: use
        the plan-literal ``~/.nunba/audit`` as the default; admins
        who want platform-default paths set
        ``HEVOLVE_VLM_AUDIT_DIR=$(python -c "from core.platform_paths
        import get_data_dir; import os; print(os.path.join(
        get_data_dir(), 'audit'))")`` once at install time.

        Override with ``HEVOLVE_VLM_AUDIT_DIR=...`` env var (read in
        SafetyConfig).  Empty string honored (audit logger inits but
        never writes).
        """
        return os.path.expanduser('~/.nunba/audit')

    def log(self, action: dict, result: dict, *,
            window_meta: Optional[dict] = None,
            screenshot_b64: Optional[str] = None,
            block_reason: Optional[str] = None) -> None:
        """Append one JSONL record.  No-op when audit_enabled is False
        or the dir couldn't be created."""
        if not self.config.audit_enabled or not self.path:
            return
        # Which command ran and which file was touched.  Live 2026-09-14 the
        # loop wrote and ran C:\Users\Public\search_llm_config.py and no
        # record named it: the command rides in 'command' and the target in
        # 'path', and neither was kept, while the script's first 80 chars
        # were kept raw as 'text'.  Text is redacted; content is hashed.
        act = action.get('action')
        text = action.get('text') or ''
        content = action.get('content')
        if act == 'write_file':
            if content is None:
                content = text
            text = ''
        record = {
            'ts': time.time(),
            'iso': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'action': act,
            'coordinate': action.get('coordinate'),
            'text': _redacted(text, 80),
            'command': _redacted(action.get('command'),
                                 AUDIT_COMMAND_MAX_CHARS) or None,
            'path': action.get('path'),
            'source_path': action.get('source_path'),
            'destination_path': action.get('destination_path'),
            'content_sha256': (
                hashlib.sha256(str(content).encode('utf-8', 'surrogatepass'))
                .hexdigest()[:16] if content else None),
            'translated_from': action.get('_translated_from'),
            'translated_to': action.get('_translated_to'),
            'window': {
                'hwnd': (window_meta or {}).get('hwnd'),
                'title': ((window_meta or {}).get('title') or '')[:80],
                'process_name': (window_meta or {}).get('process_name'),
                'pid': (window_meta or {}).get('pid'),
            } if window_meta else None,
            'screenshot_sha256': (
                hashlib.sha256(screenshot_b64.encode('ascii')).hexdigest()[:16]
                if screenshot_b64 else None),
            'status': result.get('status'),
            'error': result.get('error'),
            'block_reason': block_reason,
            'verify_diff': result.get('verify_diff'),
            'verify_retried': result.get('verify_retried'),
            # Correlation fields are set by local_loop before it executes an
            # action. A durable ledger event can reference this redacted
            # evidence without treating the audit file as an interaction store.
            'prompt_id': action.get('_prompt_id'),
            'agent_id': action.get('_agent_id'),
            'user_id': action.get('_user_id'),
            'activity_id': action.get('_activity_id'),
        }
        date = time.strftime('%Y%m%d')
        log_path = os.path.join(self.path, f'vlm_actions_{date}.jsonl')
        line = json.dumps(record, default=str)
        try:
            with self._lock:
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write(line + '\n')
        except Exception as e:
            logger.debug(f'audit write failed: {e}')


# ─── Module-level singletons ──────────────────────────────────────────

_session_guard: Optional[SessionGuard] = None
_audit_logger: Optional[AuditLogger] = None
_singleton_lock = threading.Lock()


def get_session_guard() -> SessionGuard:
    global _session_guard
    if _session_guard is None:
        with _singleton_lock:
            if _session_guard is None:
                _session_guard = SessionGuard()
    return _session_guard


def get_audit_logger() -> AuditLogger:
    global _audit_logger
    if _audit_logger is None:
        with _singleton_lock:
            if _audit_logger is None:
                _audit_logger = AuditLogger()
    return _audit_logger


def reset_session_guard() -> None:
    """Start a fresh action budget.  Called by run_local_agentic_loop at
    the start of every goal; without it the process-wide count reached the
    cap once and refused every later action (2,506 on 2026-09-13)."""
    guard = get_session_guard()
    guard.reset()
