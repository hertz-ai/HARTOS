"""Every secret-looking environment read is declared, so the vault knows which
names it may deliver to the environment (hartos.ai_key_vault.reads_from_env)
and which it must never set.

Review of Nunba 86c65e76 (M3): reads_from_env was two hand-kept tables and
missed names the process reads (TWITCH_CLIENT_SECRET, TELEGRAM_API_HASH,
EMAIL_PASSWORD, TWILIO_AUTH_TOKEN, LINE_CHANNEL_ACCESS_TOKEN, the webhook
secrets built from the channel name, GITHUB_TOKEN, STRIPE_API_KEY,
LIVEKIT_*).  A value the owner stored for one of those never reached the
adapter that reads it.

Now each module that reads a credential from the environment declares it:
  ENV_SECRETS         names an owner may enter; a vault value is delivered
                      to os.environ for them (collected at runtime: channel
                      adapters through FlaskChannelIntegration.env_names,
                      other modules through ai_key_vault._ENV_SECRET_MODULES)
  ENV_NOT_FROM_VAULT  names the process reads as its own configuration or
                      key material; a vault/card value must never set them
The source guard below fails on an os.environ / os.getenv read of a
secret-looking name that neither declares nor SECRET_KEYS covers, so the
list cannot drift again.
"""
import ast
import importlib
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

SCAN = ('core', 'hartos', 'integrations', 'security', 'hart_intelligence_entry.py')

#: Files this guard does not ask to declare, each with the reason.
EXEMPT = {
    os.path.join('security', 'hive_guardrails.py'):
        'structurally immutable guardrail module (CLAUDE.md): never edited here',
    os.path.join('security', 'hsm_provider.py'):
        'master-key provider: AI exclusion zone, never read or edited',
    os.path.join('security', 'master_key.py'):
        'trust anchor (CLAUDE.md): never edited here',
    os.path.join('security', 'key_delegation.py'):
        'master-key certificate chain: AI exclusion zone, never edited here',
}

SECRETY = re.compile(r'(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|APIKEY|_HASH|AUTH|'
                     r'PRIVATE_KEY|ACCESS_KEY|CREDENTIAL|_KEY$)')

_READERS = {'os.getenv', 'os.environ.get', '_os.getenv', '_os.environ.get'}
_MAPPINGS = {'os.environ', '_os.environ'}


def _files():
    for entry in SCAN:
        path = os.path.join(ROOT, entry)
        if os.path.isfile(path):
            yield entry
            continue
        for dp, dns, fns in os.walk(path):
            dns[:] = [d for d in dns if d != '__pycache__']
            for fn in fns:
                if fn.endswith('.py'):
                    yield os.path.relpath(os.path.join(dp, fn), ROOT)


def _tree(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8', errors='replace') as fh:
        return ast.parse(fh.read())


def _env_reads(tree):
    """(lineno, literal name or None, JoinedStr or None) per env read."""
    for node in ast.walk(tree):
        arg = None
        if isinstance(node, ast.Call) and node.args and \
                ast.unparse(node.func) in _READERS:
            arg = node.args[0]
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) \
                and ast.unparse(node.value) in _MAPPINGS:
            arg = node.slice
        if arg is None:
            continue
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            yield node.lineno, arg.value, None
        elif isinstance(arg, ast.JoinedStr):
            yield node.lineno, None, arg


def _declared(tree, const):
    """The strings a module-level ``const = (...)`` names (AST, no import)."""
    out = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == const for t in targets):
                for sub in ast.walk(node.value):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        out.add(sub.value)
    return out


def _fstring_pattern(joined):
    parts = []
    for v in joined.values:
        if isinstance(v, ast.Constant):
            parts.append(re.escape(v.value))
        else:
            parts.append('[A-Z0-9_]+')
    return re.compile(''.join(parts))


def _module_name(rel):
    return rel[:-3].replace(os.sep, '.').replace('/', '.')


# ── Behaviour: the names the review found are delivered ────────────────

@pytest.mark.parametrize('name', [
    'TWITCH_CLIENT_SECRET', 'TELEGRAM_API_HASH', 'EMAIL_PASSWORD',
    'TWILIO_AUTH_TOKEN', 'LINE_CHANNEL_ACCESS_TOKEN', 'LINE_CHANNEL_SECRET',
    'ZALO_WEBHOOK_SECRET', 'MESSENGER_VERIFY_TOKEN', 'GITHUB_TOKEN',
    'STRIPE_API_KEY', 'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET',
    'TELEGRAM_BOT_TOKEN', 'NEWS_API_KEY',
])
def test_a_credential_the_process_reads_is_delivered(name):
    from hartos.ai_key_vault import reads_from_env
    assert reads_from_env(name) is True


@pytest.mark.parametrize('name', [
    'HEVOLVE_MASTER_KEY', 'HARTOS_MCP_DISABLE_AUTH', 'HARTOS_API_TOKEN',
    'HART_SHELL_TOKEN', 'NUNBA_CI', 'PATH', 'HTTPS_PROXY', 'SITE_PASSWORD',
])
def test_configuration_and_unknown_names_are_never_delivered(name):
    from hartos.ai_key_vault import reads_from_env
    assert reads_from_env(name) is False


def test_every_channel_gets_its_webhook_secret_names():
    from integrations.channels.flask_integration import FlaskChannelIntegration
    names = FlaskChannelIntegration.env_names()
    for channel in FlaskChannelIntegration._ADAPTER_FACTORIES:
        up = channel.upper()
        for suffix in ('_APP_SECRET', '_CHANNEL_SECRET', '_WEBHOOK_SECRET',
                       '_VERIFY_TOKEN'):
            assert up + suffix in names, (channel, suffix)


# ── Source guards: the declarations cannot drift ────────────────────────

def test_source_guard_every_secret_env_read_is_declared():
    """A node secret is declared by security.secrets_manager.NODE_SECRET_KEYS
    (only the node's own vault preload sets it)."""
    from hartos.ai_key_vault import is_node_secret, reads_from_env
    missing = []
    for rel in _files():
        if rel in EXEMPT:
            continue
        tree = _tree(rel)
        reads = list(_env_reads(tree))
        if not reads:
            continue
        not_from_vault = _declared(tree, 'ENV_NOT_FROM_VAULT')
        declared = _declared(tree, 'ENV_SECRETS')
        for lineno, name, joined in reads:
            if name is not None:
                if not SECRETY.search(name.upper()):
                    continue
                if reads_from_env(name) or is_node_secret(name)                         or name in not_from_vault:
                    continue
                missing.append(f'{rel}:{lineno} {name}')
            else:
                pat = _fstring_pattern(joined)
                if not SECRETY.search(ast.unparse(joined).upper()):
                    continue
                known = declared | set(_runtime_declared(rel))
                if any(pat.fullmatch(n) for n in known):
                    continue
                missing.append(f'{rel}:{lineno} {ast.unparse(joined)}')
    assert missing == [], (
        'secret-looking environment reads that no ENV_SECRETS / '
        'ENV_NOT_FROM_VAULT declares:\n  ' + '\n  '.join(missing))


def _runtime_declared(rel):
    """Names a module declares at import (an f-string read's module builds
    them from its own table), or flask_integration's env_names()."""
    if rel == os.path.join('integrations', 'channels', 'flask_integration.py'):
        from integrations.channels.flask_integration import FlaskChannelIntegration
        return FlaskChannelIntegration.env_names()
    mod = importlib.import_module(_module_name(rel))
    return getattr(mod, 'ENV_SECRETS', ())


def test_source_guard_every_env_secrets_module_is_collected():
    """A module that declares ENV_SECRETS is read by reads_from_env: a
    channel adapter through _ADAPTER_FACTORIES, anything else through
    ai_key_vault._ENV_SECRET_MODULES.  A declaration nothing collects is a
    list that silently does nothing."""
    from hartos.ai_key_vault import _ENV_SECRET_MODULES, reads_from_env
    from integrations.channels.flask_integration import FlaskChannelIntegration
    adapters = {importlib.import_module(m, 'integrations.channels').__name__
                for m, _ in FlaskChannelIntegration._ADAPTER_FACTORIES.values()}
    uncollected, undelivered = [], []
    for rel in _files():
        tree = _tree(rel)
        if not _declared(tree, 'ENV_SECRETS') and not any(
                isinstance(n, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == 'ENV_SECRETS' for t in n.targets)
                for n in tree.body):
            continue
        mod = _module_name(rel)
        if mod not in adapters and mod not in _ENV_SECRET_MODULES:
            uncollected.append(mod)
            continue
        for name in importlib.import_module(mod).ENV_SECRETS:
            if not reads_from_env(name):
                undelivered.append(f'{mod} {name}')
    assert uncollected == [] and undelivered == [], (uncollected, undelivered)


def test_source_guard_no_name_is_both_delivered_and_not_from_vault():
    from hartos.ai_key_vault import reads_from_env
    both = []
    for rel in _files():
        for name in _declared(_tree(rel), 'ENV_NOT_FROM_VAULT'):
            if reads_from_env(name):
                both.append(f'{rel} {name}')
    assert both == []


# ── Node secrets: the node's own vault may set them, a card never ──────

NODE = ('SOCIAL_SECRET_KEY', 'SOCIAL_DB_KEY', 'DATABASE_URL', 'REDIS_URL')


@pytest.mark.parametrize('name', NODE)
def test_a_node_secret_is_never_an_owner_entered_credential(name):
    """Review follow-up: SOCIAL_SECRET_KEY signs every JWT, SOCIAL_DB_KEY
    and DATABASE_URL point the node at its data.  A value from the consent
    card or an agent must never set them, held or not."""
    from hartos.ai_key_vault import is_node_secret, reads_from_env
    assert reads_from_env(name) is False
    assert is_node_secret(name) is True


def test_an_owner_credential_is_not_a_node_secret():
    from hartos.ai_key_vault import is_node_secret
    assert is_node_secret('NEWS_API_KEY') is False
    assert is_node_secret('SITE_PASSWORD') is False


# ── The deliverable names come from a manifest, not from importing ─────

def test_the_manifest_is_current():
    """reads_from_env reads hartos.env_secrets_manifest.DELIVERABLE, so the
    first call on Nunba's boot path imports no adapter.  The manifest is
    generated from the declarations; this fails when it is stale.
    Regenerate: python -m hartos.env_secrets_manifest"""
    from hartos import env_secrets_manifest as m
    assert m.NODE_SECRETS == m.collect_node(), 'stale: run python -m hartos.env_secrets_manifest'
    assert m.DELIVERABLE == m.collect(), (
        'stale: run python -m hartos.env_secrets_manifest; '
        f'missing {sorted(m.collect() - m.DELIVERABLE)}, '
        f'extra {sorted(m.DELIVERABLE - m.collect())}')


def test_reads_from_env_imports_no_adapter():
    """Measured before: the first reads_from_env imported 30 adapters and 13
    modules, about 6 s on the boot path."""
    import subprocess
    code = ("import sys; sys.path.insert(0, '.')\n"
            "from hartos.ai_key_vault import reads_from_env\n"
            "reads_from_env('NEWS_API_KEY'); reads_from_env('TWITCH_CLIENT_SECRET')\n"
            "from hartos.ai_key_vault import is_node_secret; is_node_secret('SOCIAL_DB_KEY')\n"
            "bad = [m for m in sys.modules if m.startswith('integrations.channels') or m == 'security'"
            " or m == 'integrations.service_tools.gh_pr_tool']\n"
            "print(','.join(sorted(bad)))\n")
    out = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True,
                         text=True, timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == '', out.stdout
