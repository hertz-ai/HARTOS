"""Guards core.wamp_url — the WAMP_URL two-vocabulary fix (#612).

Producers (scripts/run{,_with_tracing}.{sh,bat}) export a ws:// ROUTER url.
Consumers want an http:// PUBLISH-BRIDGE url.  Before this module both
consumers POSTed straight at the router socket whenever a run script was
used, and the unset-env default masked it in local dev.
"""
import ast
from pathlib import Path

import pytest

from core.wamp_url import DEFAULT_PUBLISH_URL, resolve_publish_url

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("raw,expected", [
    # the exact literal all four run scripts export
    ("ws://azurekong.hertzai.com:8088/ws",
     "http://azurekong.hertzai.com:8088/publish"),
    ("wss://azurekong.hertzai.com:8088/ws",
     "https://azurekong.hertzai.com:8088/publish"),
    ("ws://localhost:8088/ws", "http://localhost:8088/publish"),
    # already-correct publish urls must pass through untouched
    ("http://localhost:8088/publish", "http://localhost:8088/publish"),
    ("https://central.hevolve.ai/publish", "https://central.hevolve.ai/publish"),
])
def test_normalises_router_url_to_publish_bridge(raw, expected):
    assert resolve_publish_url({"WAMP_URL": raw}) == expected


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_unset_or_blank_falls_back(raw):
    env = {} if raw is None else {"WAMP_URL": raw}
    assert resolve_publish_url(env) == DEFAULT_PUBLISH_URL


def test_unrecognised_value_is_returned_untouched():
    """Fail loudly downstream beats silently redirecting somewhere else."""
    assert resolve_publish_url({"WAMP_URL": "not a url"}) == "not a url"
    # scheme we don't map, with a netloc — still not ours to rewrite
    assert resolve_publish_url({"WAMP_URL": "amqp://host:5672/x"}) == \
        "amqp://host:5672/x"


# ── drift guard: neither consumer may read WAMP_URL directly again ────

@pytest.mark.parametrize("relpath", [
    "hart_intelligence_entry.py",
    "integrations/social/realtime.py",
])
def test_consumers_go_through_the_normalizer(relpath):
    """A raw os.environ.get('WAMP_URL') here reintroduces the split.

    The ImportError fallback in hart_intelligence_entry.py is allowed to
    mention it (cx_Freeze defence), so require the normalizer is imported
    rather than banning the string outright.
    """
    src = (REPO / relpath).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imports_normalizer = any(
        isinstance(n, ast.ImportFrom)
        and (n.module or "").endswith("wamp_url")
        and any(a.name == "resolve_publish_url" for a in n.names)
        for n in ast.walk(tree)
    )
    assert imports_normalizer, (
        f"{relpath} must resolve WAMP_URL through core.wamp_url — a direct "
        "os.environ read makes it POST at a ws:// router socket again"
    )


# ── container-aware default ───────────────────────────────────────────────
# localhost:8088 is right off a container and wrong inside one: crossbar runs
# as a sibling container (nothing in this repo starts one in the app image), so
# the container's own loopback has nothing listening. Measured on central
# 2026-08-19 from inside the langchain container: 127.0.0.1:8088 refused,
# default-route gateway returned HTTP 200.

def test_gateway_parses_little_endian_hex(monkeypatch):
    """/proc/net/route stores the gateway low byte first: 010011AC = 172.17.0.1."""
    import io

    from core import wamp_url

    route = (
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "eth0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\n"   # on-link, skipped
        "eth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\n"   # default route
    )
    monkeypatch.setattr(
        wamp_url, "open", lambda *a, **k: io.StringIO(route), raising=False)
    assert wamp_url._default_route_gateway() == "172.17.0.1"


def test_off_container_default_is_unchanged(monkeypatch):
    """The no-env behaviour off a container must stay byte-identical."""
    from core import wamp_url

    monkeypatch.setattr(wamp_url, "_in_container", lambda env=None: False)
    assert resolve_publish_url({}) == DEFAULT_PUBLISH_URL


def test_in_container_uses_derived_gateway(monkeypatch):
    """Unset WAMP_URL inside a container resolves to the host, not localhost."""
    from core import wamp_url

    monkeypatch.setattr(wamp_url, "_in_container", lambda env=None: True)
    monkeypatch.setattr(wamp_url, "_default_route_gateway", lambda: "10.42.0.1")
    url = resolve_publish_url({})
    assert url == "http://10.42.0.1:8088/publish"
    assert "localhost" not in url


def test_in_container_falls_back_when_gateway_unreadable(monkeypatch):
    """No default route (or /proc absent) must not raise — keep the old default."""
    from core import wamp_url

    monkeypatch.setattr(wamp_url, "_in_container", lambda env=None: True)
    monkeypatch.setattr(wamp_url, "_default_route_gateway", lambda: None)
    assert resolve_publish_url({}) == DEFAULT_PUBLISH_URL


def test_explicit_wamp_url_still_wins_inside_a_container(monkeypatch):
    """An operator-set value must never be overridden by the derivation."""
    from core import wamp_url

    monkeypatch.setattr(wamp_url, "_in_container", lambda env=None: True)
    monkeypatch.setattr(wamp_url, "_default_route_gateway", lambda: "10.42.0.1")
    assert resolve_publish_url({"WAMP_URL": "ws://router.example:8088/ws"}) == \
        "http://router.example:8088/publish"


def test_container_signals_are_the_documented_three(monkeypatch):
    from core import wamp_url

    monkeypatch.setattr(wamp_url.os.path, "exists", lambda p: False)
    assert wamp_url._in_container({}) is False
    assert wamp_url._in_container({"DOCKER_CONTAINER": "true"}) is True
    assert wamp_url._in_container({"HEVOLVE_CLOUD_MODE": "true"}) is True


def test_no_hardcoded_bridge_ip():
    """The host address must be derived, never a baked-in literal.

    172.17.0.1 is only Docker's DEFAULT bridge gateway — wrong on a
    user-defined network or a daemon with a custom --bip.

    Checked over the AST, not the text: the prose in this module explains why
    the literal is wrong, and a plain string match counts that explanation as a
    violation. Documentation must not be able to fail a structural assertion.
    """
    tree = ast.parse((REPO / "core" / "wamp_url.py").read_text(encoding="utf-8"))

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)

    offenders = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and "172.17.0.1" in n.value
        and n.value not in docstrings
    ]
    assert not offenders, (
        f"hardcoded docker bridge gateway in executable code: {offenders} — "
        "derive it from /proc/net/route instead")
