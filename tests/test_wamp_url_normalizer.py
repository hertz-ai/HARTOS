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
