"""What the shell SENDS is what the native desktop DRAWS.

WHY THIS EXISTS. Every bug found in the native scene's decoder has been the same
bug wearing a different key. It was written against an IMAGINED payload: `Hero`
read `title` and `copy`, `Row` read `label`, `Card` read `subtitle`. Not one of
those four keys is emitted by any producer, so a live compose would have rendered
a blank hero and unlabelled rows, and every Rust unit test passed the whole time.
Then `card.image_url` turned out to be the key news and app cards actually carry,
so those decoded as art-less and drew an icon glyph the shell suppresses.

The reason unit tests on either side cannot catch this is that each side builds
its own fixtures, so a decoder written against the wrong shape is tested against
the wrong shape. The only fixture that can catch it is one the REAL producer
wrote.

So: `_sanitize_home_payload` is the single authority on what reaches a client (the
LLM composes freely, that function is the only thing between its output and the
wire). This test runs it on a realistic payload and pins the result byte for byte
into `compositor/fixtures/home_compose_sanitized.json`, which scene.rs's own tests
`include_str!` and assert every field of.

Together the two halves are the contract: this pins what is SENT, and the Rust
side pins that all of it is DRAWN. Change the sanitizer's shape and this fails
with the regeneration command; ignore the new shape in the decoder and the Rust
side fails.

Run:
  pytest tests/unit/test_native_wire_contract.py -v
"""

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from integrations.agent_engine import liquid_ui_service as L  # noqa: E402

# A `.rs` file rather than the `.json` it plainly is, for one build reason: the
# crane source filter hart-comp.nix uses keeps Cargo.toml/Cargo.lock and `*.rs`
# ONLY, so a `.json` beside the crate would be filtered out of the build sandbox
# and `include_str!` would fail in CI while passing on a dev box.
FIXTURE = os.path.join(REPO, "compositor", "src", "wire_fixture.rs")
_RAW_OPEN = 'r#"'
_RAW_CLOSE = '"#;'


def _fixture_json():
    """The JSON embedded in the generated Rust module."""
    src = open(FIXTURE, encoding="utf-8").read()
    i = src.index(_RAW_OPEN) + len(_RAW_OPEN)
    j = src.index(_RAW_CLOSE, i)
    return json.loads(src[i:j])


def _write_fixture(payload):
    """Rewrite the module's literal, leaving everything around it alone."""
    src = open(FIXTURE, encoding="utf-8").read()
    i = src.index(_RAW_OPEN) + len(_RAW_OPEN)
    j = src.index(_RAW_CLOSE, i)
    body = json.dumps(payload, indent=2, sort_keys=True)
    assert '"#' not in body, "the payload would terminate the raw string literal"
    with open(FIXTURE, "w", encoding="utf-8", newline="\n") as f:
        f.write(src[:i] + body + src[j:])

# A realistic LLM-authored home, chosen to carry every shape that has ever
# decoded wrong or that the native scene treats specially:
#   - a card with `image_url` only (the news/app case that read as art-less)
#   - a card with same-origin `image` (the other photo slot)
#   - a RANKED row (whose cards have no background of their own)
#   - two DIFFERENT accents (a desktop where every row read teal was the bug)
#   - progress, a live tag, a badge, an icon, and a See-all target
SAMPLE = {
    "mood": "aurora",
    "hero": {
        "eyebrow": "Earned on the hive",
        "amount": 1284,
        "amount_unit": "Spark",
        "agents": 3,
        "tasks": 7,
    },
    "rows": [
        {
            "title": "Continue",
            "accent": "teal",
            "see_all": "recipes",
            "cards": [
                {"title": "Refactor the parser", "meta": "3 files changed",
                 "progress": 0.62, "icon": "code", "action": "resume",
                 "target": "recipes"},
                {"title": "Morning briefing", "meta": "12 sources",
                 "live": "RUNNING", "action": "open",
                 "image_url": "https://example.invalid/a.jpg"},
            ],
        },
        {
            "title": "Top agents",
            "accent": "magenta",
            "ranked": True,
            "see_all": "agents_browse",
            "cards": [
                {"title": "Scout", "meta": "412 tasks", "badge": "NEW",
                 "icon": "explore"},
                {"title": "Archivist", "meta": "388 tasks",
                 "image": "/shell/static/app_art/a.svg"},
            ],
        },
    ],
}


def _sanitized():
    clean = L._sanitize_home_payload(SAMPLE)
    assert clean, "the sanitizer rejected a payload it should accept"
    return clean


def test_the_native_fixture_is_still_what_the_sanitizer_actually_emits():
    """THE CONTRACT. Regenerate and compare.

    A failure here means the wire shape moved. That is allowed, but it has to be
    deliberate: regenerate the fixture with

        python -c "import sys; sys.path.insert(0,'.'); \\
          from tests.unit.test_native_wire_contract import _sanitized, \\
          _write_fixture; _write_fixture(_sanitized())"

    and then make scene.rs decode whatever is new, because the Rust half of this
    contract asserts every field of that file reaches the screen. Regenerating
    WITHOUT reading the diff is the whole failure mode this exists to prevent:
    that is how four keys nobody sends ended up in the decoder.
    """
    assert os.path.exists(FIXTURE), (
        "the native wire fixture is missing: %s" % FIXTURE)
    assert _fixture_json() == _sanitized(), (
        "compositor/src/wire_fixture.rs is stale: the sanitizer now emits a "
        "different shape, so the native decoder is being tested against a "
        "payload no producer sends any more")


def test_the_fixture_carries_the_shapes_that_have_bitten_before():
    """Guard the GUARD: a fixture that lost its interesting cases still passes
    the comparison above while proving nothing. Each of these is a real bug that
    reached the tree, so a fixture without them is not doing its job."""
    clean = _sanitized()
    cards = [c for r in clean["rows"] for c in r["cards"]]
    assert any("image_url" in c for c in cards), (
        "no card carries image_url, the key that decoded as art-less")
    assert any("image" in c for c in cards), (
        "no card carries a same-origin image")
    assert any(r.get("ranked") for r in clean["rows"]), (
        "no ranked row, the shape that rendered as holes")
    accents = {r["accent"] for r in clean["rows"]}
    assert len(accents) > 1, (
        "every row shares an accent, so the per-row hue proves nothing")
    assert any("progress" in c for c in cards), "no progress bar"
    assert any("live" in c for c in cards), "no live tag"
    assert any("badge" in c for c in cards), "no badge"
    assert any("see_all" in r for r in clean["rows"]), "no See-all target"


def test_the_sanitizer_still_drops_what_it_promises_to_drop():
    """The other half of the wire contract: the native decoder is tolerant by
    design, so the sanitizer is the ONLY thing keeping a hostile or hallucinated
    string off the surface. Assert the drops it exists for, since the native
    renderer now paints what it passes."""
    dirty = {
        "hero": {"amount": 5},
        "rows": [{
            "title": "x",
            "accent": "chartreuse",
            "see_all": "/etc/shadow",
            "cards": [{
                "title": "t",
                "icon": "not a ligature name",
                "image": "javascript:alert(1)",
                "image_url": "data:text/html,<script>",
                "progress": 42,
                "action": "rm -rf",
            }],
        }],
    }
    clean = L._sanitize_home_payload(dirty)
    assert clean, "a mostly-bad payload should degrade, not vanish"
    row = clean["rows"][0]
    card = row["cards"][0]
    assert row["accent"] == "teal", "an unknown accent is coerced, never passed"
    assert "see_all" not in row, "a see_all outside the panel targets is dropped"
    assert "icon" not in card, "an icon that is not a ligature name is dropped"
    assert "image" not in card, "a non-prefixed image is dropped"
    assert "image_url" not in card, "a data: URL is dropped"
    assert "progress" not in card, "an out-of-range progress is dropped"
    assert card["action"] == "open", "an unknown action falls back, never passes"
