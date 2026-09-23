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
import re
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
# `r##"..."##`, not `r#"..."#`: a resolved palette is JSON hex, and `"#00E6C3"` opens
# with the exact two bytes that would end a single-hash raw string.
_RAW_OPEN = 'r##"'
_RAW_CLOSE = '"##;'

# The named literals the module carries, each the sanitizer's output for one
# SAMPLE below. More than one because the program's own "Add" row for the home
# asks for it: a contract pinned on ONE payload proves the decoder against one
# shape, and the mood set is a different shape from the aurora default.
FIXTURE_DEFAULT = "HOME_COMPOSE_SANITIZED"
FIXTURE_CLASSIC_MOOD = "HOME_COMPOSE_SANITIZED_CLASSIC_MOOD"


def _literal_span(src, name):
    """(start, end) of the raw string body of `pub const <name>: &str = r#"..."#;`."""
    decl = "pub const %s: &str = " % name
    d = src.index(decl)
    i = src.index(_RAW_OPEN, d) + len(_RAW_OPEN)
    j = src.index(_RAW_CLOSE, i)
    return i, j


def _fixture_json(name=FIXTURE_DEFAULT):
    """The JSON embedded in the generated Rust module, for one named literal."""
    src = open(FIXTURE, encoding="utf-8").read()
    i, j = _literal_span(src, name)
    return json.loads(src[i:j])


def _write_fixture(payload, name=FIXTURE_DEFAULT):
    """Rewrite ONE named literal, leaving everything around it alone."""
    src = open(FIXTURE, encoding="utf-8").read()
    i, j = _literal_span(src, name)
    body = json.dumps(payload, indent=2, sort_keys=True)
    assert '"##' not in body, "the payload would terminate the raw string literal"
    with open(FIXTURE, "w", encoding="utf-8", newline="\n") as f:
        f.write(src[:i] + body + src[j:])


def _write_all_fixtures():
    """Regenerate every named literal from its SAMPLE. The one command to run."""
    _write_fixture(_sanitized(), FIXTURE_DEFAULT)
    _write_fixture(_sanitized_classic_mood(), FIXTURE_CLASSIC_MOOD)

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


# The SECOND fixture: a CLASSIC mood (one of the ten palettes that set the
# functional accent from their lead hue, where the six Aura moods pin it teal),
# and a card whose photo is one of the 51 BUNDLED SVGs by its real served path.
# Two things the default fixture cannot prove: that a mood moves the accent when
# it is meant to, and that the photo slot names a file the compositor can find.
SAMPLE_CLASSIC_MOOD = {
    "mood": "sunset",
    "hero": {
        "eyebrow": "Earned on the hive",
        "amount": 96,
        "amount_unit": "Spark",
        "agents": 1,
        "tasks": 2,
    },
    "rows": [
        {
            "title": "Apps",
            "accent": "amber",
            "see_all": "app_store",
            "cards": [
                {"title": "Brave", "meta": "Browser", "action": "open",
                 "image": "/shell/static/app_art/apps/com.brave.Browser.svg"},
                {"title": "Files", "meta": "Everything you own", "action": "open",
                 "image": "/shell/static/app_art/app-files.svg"},
            ],
        },
    ],
}


def _sanitized_classic_mood():
    clean = L._sanitize_home_payload(SAMPLE_CLASSIC_MOOD)
    assert clean, "the sanitizer rejected a payload it should accept"
    return clean


def test_the_native_fixture_is_still_what_the_sanitizer_actually_emits():
    """THE CONTRACT. Regenerate and compare.

    A failure here means the wire shape moved. That is allowed, but it has to be
    deliberate: regenerate the fixtures with

        python -c "import sys; sys.path.insert(0,'.'); \\
          from tests.unit.test_native_wire_contract import _write_all_fixtures; \\
          _write_all_fixtures()"

    and then make scene.rs decode whatever is new, because the Rust half of this
    contract asserts every field of that file reaches the screen. Regenerating
    WITHOUT reading the diff is the whole failure mode this exists to prevent:
    that is how four keys nobody sends ended up in the decoder.
    """
    assert os.path.exists(FIXTURE), (
        "the native wire fixture is missing: %s" % FIXTURE)
    assert _fixture_json(FIXTURE_DEFAULT) == _sanitized(), (
        "compositor/src/wire_fixture.rs is stale: the sanitizer now emits a "
        "different shape, so the native decoder is being tested against a "
        "payload no producer sends any more")
    assert _fixture_json(FIXTURE_CLASSIC_MOOD) == _sanitized_classic_mood(), (
        "compositor/src/wire_fixture.rs is stale for the classic-mood fixture")


def test_the_sanitizer_resolves_a_mood_to_the_colours_the_browser_paints():
    """`mood` reaches the pixels. The compositor keeps no palette table (Gate 4),
    so the sanitizer hands it the resolution of the id beside the id itself, and
    the resolution follows paintPalette's own rule: the functional accent is
    `p.accent || p.a`. Aura moods pin `accent` teal, so teal survives an Aura mood
    on every functional signifier while the quad drives the ambient field; the
    classic palettes set the accent from their lead hue."""
    aurora = _sanitized()["palette"]
    assert aurora["accent"] == "#00E6C3", "an Aura mood keeps the functional accent teal"
    assert aurora["ambient_1"] == "#B182FF", "while its quad leads violet"
    assert aurora["ambient_4"] == "#FFB330"
    assert aurora["background"] == "#04050B"
    assert aurora["secondary"] == aurora["ambient_2"] == "#00DDF9"

    sunset = _sanitized_classic_mood()["palette"]
    assert sunset["accent"] == sunset["ambient_1"] == "#FF8A4C", (
        "a classic palette sets the accent from its lead hue")
    assert sunset["secondary"] == sunset["ambient_2"] == "#FF2E9A"
    assert "ambient_3" not in sunset and "ambient_4" not in sunset, (
        "a palette that omits a3/a4 leaves the theme's ambient in place, on both "
        "renderers, rather than inventing a hue")

    # Every id the prompt may emit resolves, so no mood is decoded and dropped.
    for pid in L.HART_MOOD_PALETTE_IDS:
        pal = L._home_resolve_mood(pid)
        assert pal and "accent" in pal and "ambient_1" in pal, (
            "mood %r has no resolution" % pid)
        for v in pal.values():
            assert re.match(r"^#[0-9A-F]{6}$", v), "a resolved colour is #RRGGBB"
    # And an id outside the table resolves to nothing, exactly as byId does.
    assert L._home_resolve_mood("chartreuse") is None
    clean = L._sanitize_home_payload(dict(SAMPLE, mood="Not A Mood!"))
    assert clean["mood"] == "notamood" and "palette" not in clean


def test_the_resolution_is_read_from_the_one_palette_table_not_a_copy():
    """The table lives in hartPersonalize.js and nowhere else. Read the SAME
    array the browser executes and check every entry resolves to what
    paintPalette would set, so a colour edited in the JS moves the native desktop
    with it and a second copy cannot drift."""
    js = os.path.join(REPO, "integrations", "agent_engine", "static",
                      "hartPersonalize.js")
    src = open(js, encoding="utf-8").read()
    m = re.search(r"var PALETTES\s*=\s*window\.HART_PALETTES\s*=\s*\[(.*?)\];",
                  src, re.S)
    assert m, "PALETTES array not found in hartPersonalize.js"
    entries = [dict(re.findall(r"(\w+):\s*'([^']*)'", e))
               for e in re.findall(r"\{([^{}]*)\}", m.group(1))]
    assert len(entries) == 16
    for p in entries:
        pal = L._home_resolve_mood(p["id"])
        assert pal, "mood %r did not resolve" % p["id"]
        # paintPalette: `var acc = p.accent || p.a`
        assert pal["accent"] == (p.get("accent") or p["a"]).upper(), p["id"]
        assert pal["background"] == p["b"].upper()
        for i, key in enumerate(("a", "a2", "a3", "a4"), 1):
            if key in p:
                assert pal["ambient_%d" % i] == p[key].upper(), (p["id"], key)
            else:
                assert "ambient_%d" % i not in pal, (p["id"], key)


def test_compose_home_sends_the_native_scene_the_same_resolution():
    """The route hands compose_home a RAW body, so the palette is derived from the
    mood there and never accepted from the caller; and what it derives is what
    the sanitizer emits, because both call the one resolver."""
    from unittest.mock import MagicMock, patch
    svc = L.LiquidUIService.__new__(L.LiquidUIService)
    client = MagicMock()
    client.shell_compose.return_value = {"ok": True}
    with patch.object(L.LiquidUIService, "agent_ui_update", return_value=True), \
            patch.object(L.LiquidUIService, "_ensure_native_input_relay",
                         return_value=True), \
            patch("integrations.agent_engine.hart_wm_client.get_wm_client",
                  return_value=client):
        assert svc.compose_home(hero=SAMPLE["hero"], rows=SAMPLE["rows"],
                                mood="aurora") is True
        kw = client.shell_compose.call_args.kwargs
        assert kw["palette"] == _sanitized()["palette"]
        client.shell_compose.reset_mock()
        assert svc.compose_home(hero=SAMPLE["hero"], rows=SAMPLE["rows"],
                                mood="no-such-mood") is True
        assert client.shell_compose.call_args.kwargs.get("palette") is None


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
    assert "palette" in clean, (
        "no resolved palette, the shape that left `mood` decoded and dropped")
    # The classic-mood fixture carries the two things this one cannot.
    classic = _sanitized_classic_mood()
    assert classic["palette"]["accent"] != clean["palette"]["accent"], (
        "the two fixtures must differ in accent, or the mood proves nothing")
    images = [c["image"] for r in classic["rows"] for c in r["cards"] if "image" in c]
    for img in images:
        rel = img[len("/shell/static/"):]
        assert os.path.exists(os.path.join(
            REPO, "integrations", "agent_engine", "static", rel)), (
            "the photo fixture names a bundled file that does not exist: %s" % img)


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

# -- LATENCY ATTRIBUTION ------------------------------------------------------

SCENE_SRC = os.path.join(REPO, "compositor", "src", "scene.rs")
BUDGETS = os.path.join(REPO, "docs", "architecture", "latency_budgets.json")
def _component_keys():
    """Every budget key scene.rs can attribute a latency sample to.

    Read by FOLLOWING the mapping rather than from a second list. An earlier cut
    had a `Component::key()` naming these directly, which is two spellings of one
    vocabulary and exactly the drift these guards exist to stop: the runtime used
    `Surface::label` and NOTHING used `key`, so a change to one would have left
    the other pinning a name the instrument never emits. `surface()` maps each
    component into latency::Surface, and that label is the single source.
    """
    scene = open(SCENE_SRC, encoding="utf-8").read()
    block = re.search(
        r"pub fn surface\(self\) -> crate::latency::Surface \{(.*?)\n    \}",
        scene, re.S)
    assert block, "scene.rs no longer maps Component into latency::Surface"
    # `(?:\([^)]*\))?` because a variant may carry a payload, in whatever pattern
    # the arm binds it with: `Component::HomeRow(_)` is a row plus its index, and
    # `Component::HomeCard(..)` is a card plus its row and card indices, matched
    # with the rest pattern. Without it the arm reads as absent, which is how this
    # guard first reported the row surface as unmapped rather than unreadable, and
    # then reported the card surface the same way once bdaf0d3 gave it a payload
    # `\w+` could not read.
    variants = re.findall(
        r"Component::\w+(?:\([^)]*\))? => crate::latency::Surface::(\w+)",
        block.group(1))
    assert variants, "the Component-to-Surface mapping names nothing"

    latency = open(os.path.join(REPO, "compositor", "src", "latency.rs"),
                   encoding="utf-8").read()
    lab = re.search(r"impl Surface \{(.*?)\n    const ALL", latency, re.S)
    assert lab, "latency.rs no longer has a Surface::label to read"
    labels = dict(re.findall(r'Surface::(\w+) => "([^"]+)"', lab.group(1)))
    keys = []
    for v in variants:
        assert v in labels, (
            "Component maps to Surface::%s, which latency.rs does not label" % v)
        keys.append(labels[v])
    return keys


def test_every_component_the_scene_names_has_a_real_latency_budget():
    """The attribution contract, across the same language boundary as the rest.

    docs/architecture/latency_budgets.json carries 23 per-component budgets and
    the instrument has never consulted one: latency.rs reports `component=shell`
    for every sample, so a slow orb and a slow marketplace are the same number.
    Its own header says the blocker moved once the scene graph could hit-test.

    scene.rs now names the surfaces the native shell owns, and those names are
    the budget file's OWN keys rather than a parallel vocabulary. Rust cannot
    check that: the budget file is outside the crate, and the crane source filter
    ships `*.rs` only. So the pin lives here, where both files are readable.
    """
    budgets = json.load(open(BUDGETS, encoding="utf-8"))
    known = budgets.get("components", {})
    for key in _component_keys():
        assert key in known, (
            "scene.rs attributes latency to %r, which latency_budgets.json has "
            "no row for, so its samples would be checked against the _defaults "
            "and that component's budget would stay dead" % key)
        rows = [k for k in known[key] if not k.startswith("_")]
        assert rows, "%r is in the budget file with no actual budgets" % key


def test_the_attributable_components_are_the_ones_the_native_shell_draws():
    """Guard the guard, again.

    A Component enum that quietly shrank to one entry would still pass the pin
    above while attributing almost nothing. These five are exactly the surfaces
    the native scene paints today. The rest of the budget table (start-menu,
    panel, chat-input, marketplace, onboarding) belongs to the WebView shell and
    is measured as `shell` on purpose, so that "native is faster" stays a
    demonstrated delta rather than a claim.
    """
    keys = set(_component_keys())
    assert keys == {"orb", "top-bar", "omnibox", "taskbar", "home-card",
                    "home-row"}, (
        "the set of attributable native surfaces changed: %s. That is allowed, "
        "but a new one must be a surface the native scene actually DRAWS and "
        "must have a row in latency_budgets.json." % sorted(keys))

def test_no_component_overrides_the_default_budget_for_its_kind():
    """WHY THE RUST MIRROR CAN STAY PER-KIND.

    latency.rs mirrors latency_budgets.json's `_defaults` as consts, because the
    file lives outside the crate and crane's source filter would drop it anyway.
    The per-component table looks like it needs mirroring too, and today it does
    not: every value in it equals the default for that kind. The components map
    declares WHICH interactions each surface is expected to support, at the
    standard budget, rather than different numbers.

    That is a load-bearing fact, so it is asserted rather than assumed. The
    moment someone gives a component a genuinely different budget, the instrument
    would silently keep checking it against the default and the override would do
    nothing. This test fails first and says where to put it.
    """
    budgets = json.load(open(BUDGETS, encoding="utf-8"))
    defaults = budgets["_defaults"]
    for name, rows in budgets.get("components", {}).items():
        for kind, value in rows.items():
            if kind.startswith("_"):
                continue
            assert kind in defaults, (
                "component %r budgets the kind %r, which has no default; "
                "latency.rs::Kind has no bucket for it either" % (name, kind))
            assert value == defaults[kind], (
                "component %r sets %s=%s against a default of %s. That is a real "
                "override, and latency.rs still looks its budget up per KIND only, "
                "so the override would do nothing. Teach Kind::budget_ms about the "
                "surface before landing it." % (name, kind, value, defaults[kind]))


# Surfaces the INSTRUMENT names that the SCENE cannot point at, and why. Every
# other Surface must be a scene Component, or a sample would be attributed to
# something no hit-test can reach.
NOT_SCENE_SURFACES = {
    # Bare desktop, WebView chrome, and everything measured while the native scene
    # is not on screen. Deliberate: the harness wants the web shell measured by the
    # same instrument so "native is faster" is a delta, not a claim.
    "shell",
    # Not a thing ON the desktop, the desktop CHANGING. A workspace transition has
    # no hit box, so it is named by the action that starts it rather than by a
    # point, and latency_budgets.json gives it a row with `animate-start` alone.
    "workspace-switch",
}


def test_the_instrument_and_the_scene_agree_on_the_surface_names():
    """The bridge between the two enums.

    latency.rs deliberately knows nothing about scene.rs (no Smithay, no scene,
    no clock) so its state machine runs under `cargo test` on the default
    no-feature build where `scene` is not even compiled. That means the surface
    names exist TWICE, and a drift between them would attribute samples to a
    component whose budget row is spelled differently.
    """
    latency = open(os.path.join(REPO, "compositor", "src", "latency.rs"),
                   encoding="utf-8").read()
    block = re.search(r"impl Surface \{(.*?)\n    const ALL", latency, re.S)
    assert block, "latency.rs no longer has a Surface::label to read"
    labels = set(re.findall(r'Surface::\w+ => "([^"]+)"', block.group(1)))
    assert "shell" in labels, (
        "the unattributed surface must stay `shell`, or every historical number "
        "changes format")
    assert labels - NOT_SCENE_SURFACES == set(_component_keys()), (
        "latency.rs and scene.rs disagree on the attributable surfaces: %s "
        "against %s. A surface the instrument names but the scene cannot point "
        "at needs an entry in NOT_SCENE_SURFACES saying why."
        % (sorted(labels - NOT_SCENE_SURFACES), sorted(_component_keys())))
    for name in NOT_SCENE_SURFACES:
        assert name in labels, (
            "%r is exempted from the scene bridge but latency.rs no longer names "
            "it; drop the exemption" % name)
