"""SOURCE GUARD (labelled per feedback_no_grep_tests) — a RATCHET on /chat callers.

WHY A SOURCE GUARD IS LEGITIMATE HERE
─────────────────────────────────────
This is the DRY-across-many-files case the rule carves out: the defect is "N
independent files each hand-build a /chat body and forget request_id", and no
behavioural test at a single call site can catch the eleventh file being added
tomorrow. It ACCOMPANIES real behavioural tests — tests/unit/test_chat_client_canonical.py
drives the real functions with 13 tests — it does not replace them.

WHAT IT RATCHETS
────────────────
Measured 2026-08-09: 11 files POST to /chat and 9 never mentioned request_id, so
dispatch.is_genuine_user_request classified them all as BACKGROUND — D-Bus users,
CLI users, the desktop intent bar, and every Discord/Telegram/WhatsApp user
queuing behind the flywheel on the closable client.

Rather than block on migrating all eleven at once, KNOWN_UNMIGRATED is an
allowlist that must only ever SHRINK. A NEW /chat call site outside the canonical
client fails immediately; an existing one is tracked debt with a name. The list
reaching empty is the completion condition for task #46.

PRECISION NOTES (both learned by getting it wrong first)
  * ``/v1/chat/completions`` is the LLM endpoint, NOT HARTOS's /chat — excluded,
    or every model client looks like an offender.
  * A bare ``'/chat'`` in a path CONSTANT (security/middleware.py's
    NETWORK_PROTECTED_PATHS) is a declaration, not a call — so a match only counts
    when a post() call actually precedes it.
"""
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Files that POST to /chat and have NOT yet moved to core.chat_client.
#: SHRINK ONLY. Adding to this list is a regression, not a fix.
#:
#: NOTE: "unmigrated" is a SUPERSET of "broken". Membership here means the file
#: does not route through the canonical client; it does NOT mean the file omits
#: request_id. The second group below already sends one, so migrating those is a
#: DRY fix, not a correctness one. Do not quote this list's length as a count of
#: broken callers. "Already sends one" must hold on EVERY path of the file:
#: dispatch.py was listed here while its two HTTP fallbacks sent no id, so on
#: native HARTOS the hive worker's and the instruction queue's turns reached the
#: create pipeline untagged and ran in INTERACTIVE mode, asking a user who was
#: not there (#97, central 2026-09-13).
KNOWN_UNMIGRATED = {
    # --- omit request_id entirely: a real person is classified as background ---
    'hartos_bootstrap.py',
    'hart_cli.py',
    'hart_dbus_service.py',
    'intelligence_api.py',
    'shell_openclaw_apis.py',
    'hart_skill_server.py',
    'registry.py',                 # every Discord/Telegram/WhatsApp/Matrix user
    'morphable_agent.py',

    # --- already reference request_id; migrating is DRY, not a bug fix ---
    'crossbar_server.py',
    'api_tracker.py',
    'model_bus_service.py',
    'speculative_dispatcher.py',
}

_SKIP_DIRS = ('tests', 'scripts', 'examples', 'docs', '.git', '__pycache__',
              'venv', '.venv', 'node_modules', 'claw_native', 'build', 'tools')

#: A /chat URL literal that is NOT the LLM's /v1/chat/completions.
_CHAT_URL = re.compile(r"""["'][^"'\n]*?/chat["']""")
#: ...and a post call close enough in front of it to make this a CALL, not a
#: constant. 220 chars covers a multi-line `pooled_post(\n  f'...{port}/chat',`.
_POST_NEAR = re.compile(r"""\.?\b(?:pooled_post|post|_api_post|request)\s*\(""")
#: What "migrated" means: the file IMPORTS the canonical client (either import
#: form, at any indentation: the metered endpoints import it inside the
#: function) or CALLS it.  A bare mention of the module's name is not
#: migration: this guard used to read the substring 'chat_client' as done.
#: Measured on 890f160e2..faf7c21a5^ (the review of f0861746d ran the old
#: guard on that tree): commercial_api.py named core.chat_client in a COMMENT
#: beside its bare pooled_post to /chat (the call did carry a request_id);
#: the file was still listed in KNOWN_UNMIGRATED, so the offender test
#: skipped it, but the allowlist check read the comment as migration and
#: demanded "delete this line", which would have hidden the bare call
#: (task #117).  Known limit: a ``post_chat(`` inside prose still counts, so
#: the second half is a call shape, not a parse; a parser-based check would
#: close that.
_MIGRATED = re.compile(
    r"""^\s*(?:from\s+core\.chat_client\s+import\b|import\s+core\.chat_client\b)"""
    r"""|\b(?:post_chat|normalize_chat_body)\s*\(""", re.M)


def _is_migrated(src: str) -> bool:
    """True when ``src`` imports or calls core.chat_client, not when it merely
    mentions it."""
    return bool(_MIGRATED.search(src))


def _is_real_chat_call(src: str) -> bool:
    for m in _CHAT_URL.finditer(src):
        lit = m.group(0)
        if '/v1/chat' in lit or 'completions' in lit:
            continue
        window = src[max(0, m.start() - 220):m.start()]
        if _POST_NEAR.search(window):
            return True
    return False


def _sources():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith('.')]
        for fn in files:
            if fn.endswith('.py'):
                yield os.path.join(root, fn)


class ChatCallersGoThroughTheCanonicalClient(unittest.TestCase):

    def test_no_NEW_chat_call_site_bypasses_core_chat_client(self):
        offenders = []
        for path in _sources():
            rel = os.path.relpath(path, REPO)
            if os.path.basename(rel) in KNOWN_UNMIGRATED:
                continue
            try:
                src = open(path, encoding='utf-8', errors='replace').read()
            except OSError:
                continue
            if _is_migrated(src):
                continue                                  # migrated
            if re.search(r"@app\.route\(\s*['\"]/chat", src):
                continue                                  # the server DEFINING /chat
            if _is_real_chat_call(src):
                offenders.append(rel)

        self.assertFalse(
            offenders,
            "these files POST to /chat without core.chat_client, so their turns "
            "carry no request_id and HARTOS classifies a real person as background "
            "daemon work (never preempted for, runs on the closable client, "
            "abortable mid-flight):\n  " + "\n  ".join(sorted(offenders)) +
            "\nUse core.chat_client.post_chat / normalize_chat_body.")

    def test_the_allowlist_only_shrinks(self):
        """A stale entry makes the ratchet lie about how much debt is left."""
        by_name = {}
        for path in _sources():
            by_name.setdefault(os.path.basename(path), []).append(path)
        stale = []
        for entry in sorted(KNOWN_UNMIGRATED):
            paths = by_name.get(entry)
            if not paths:
                stale.append('%s  (no such file)' % entry)
                continue
            if all(_is_migrated(open(p, encoding='utf-8', errors='replace').read())
                   for p in paths):
                stale.append('%s  (already migrated — delete this line)' % entry)
        self.assertFalse(stale, "KNOWN_UNMIGRATED is out of date:\n  " +
                                "\n  ".join(stale))


class TheDesktopIntentBarIsMigrated(unittest.TestCase):
    """The A2UI INTENT -> DECOMPOSE -> COMPOSE loop is why this work exists: while
    the shell posted without a request_id its turn was background, so it queued
    behind the flywheel and its 30s budget always expired."""

    def _src(self):
        p = os.path.join(REPO, 'integrations', 'agent_engine', 'liquid_ui_service.py')
        return open(p, encoding='utf-8', errors='replace').read()

    def test_it_routes_through_the_canonical_client(self):
        # assertTrue, not assertIn: assertIn prints the whole 8k-line file on failure.
        self.assertTrue(
            _is_migrated(self._src()),
            "liquid_ui_service no longer routes the desktop intent bar through the "
            "canonical client — the A2UI compose loop is background-classified again")

    def test_the_backend_chat_call_uses_post_chat(self):
        src = self._src()
        i = src.find('backend_port}/chat')
        self.assertNotEqual(-1, i, "the desktop intent bar's /chat call site is gone")
        window = src[max(0, i - 400):i]
        self.assertTrue(
            'post_chat(' in window,
            "the /chat call at the desktop intent bar is not post_chat() — a bare "
            "requests.post there sends no request_id and re-opens the defect")


class TheDetectorReadsCodeNotProse(unittest.TestCase):
    """The guard's own two predicates, driven on sources shaped like the real
    cases.  Task #117: the old check was ``'chat_client' in src``, so a
    comment that named the canonical client made the allowlist check call
    commercial_api.py migrated (890f160e2 to faf7c21a5^) while its call to
    /chat was a bare pooled_post."""

    PROSE_ONLY_OFFENDER = (
        '"""Routes through core.chat_client (it does not)."""\n'
        'from core.http_pool import pooled_post\n\n'
        'def ask(port, body):\n'
        "    return pooled_post(f'http://localhost:{port}/chat', json=body)\n")
    COMMENT_ONLY_OFFENDER = (
        'from core.http_pool import pooled_post\n\n'
        'def ask(port, body):\n'
        '    # see core.chat_client: a missing id reads as background\n'
        "    return pooled_post(f'http://localhost:{port}/chat', json=body)\n")
    FROM_IMPORT = (
        'from core.chat_client import post_chat\n\n'
        'def ask(port, body):\n'
        "    return post_chat(f'http://localhost:{port}/chat', body)\n")
    IMPORT_IN_FUNCTION = (
        'def ask(port, body):\n'
        '    from core.chat_client import post_chat\n'
        "    return post_chat(f'http://localhost:{port}/chat', body)\n")
    MODULE_IMPORT = (
        'import core.chat_client as cc\n\n'
        'def ask(port, body):\n'
        "    return cc.post_chat(f'http://localhost:{port}/chat', body)\n")
    # Pins each half of the predicate on its own: an import with the call
    # under another name, and a call with an import the first half does not
    # recognise (``from core import chat_client``).
    IMPORT_ONLY = (
        'from core.chat_client import post_chat as _pc\n\n'
        'def ask(port, body):\n'
        "    return _pc(f'http://localhost:{port}/chat', body)\n")
    CALL_ONLY = (
        'from core import chat_client\n\n'
        'def ask(port, body):\n'
        "    return chat_client.post_chat(f'http://localhost:{port}/chat', body)\n")
    LLM_CLIENT_NOT_HARTOS = (
        'import requests\n\n'
        'def complete(base, body):\n'
        "    return requests.post(f'{base}/v1/chat/completions', json=body)\n")

    def test_a_prose_mention_is_not_migration(self):
        for src in (self.PROSE_ONLY_OFFENDER, self.COMMENT_ONLY_OFFENDER):
            self.assertFalse(_is_migrated(src), src)
            self.assertTrue(_is_real_chat_call(src), src)

    def test_either_import_form_or_a_call_is_migration(self):
        for src in (self.FROM_IMPORT, self.IMPORT_IN_FUNCTION, self.MODULE_IMPORT):
            self.assertTrue(_is_migrated(src), src)

    def test_each_half_of_the_predicate_stands_on_its_own(self):
        self.assertTrue(_is_migrated(self.IMPORT_ONLY))     # import half alone
        self.assertTrue(_is_migrated(self.CALL_ONLY))       # call half alone

    def test_the_llm_endpoint_is_not_a_chat_call(self):
        self.assertFalse(_is_real_chat_call(self.LLM_CLIENT_NOT_HARTOS))
        self.assertFalse(_is_migrated(self.LLM_CLIENT_NOT_HARTOS))


if __name__ == '__main__':
    unittest.main()
