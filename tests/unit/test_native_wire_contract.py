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
# The bar content (`shell.chrome`), the sibling contract: see THE CHROME CONTRACT below.
FIXTURE_CHROME = "SHELL_CHROME_COMPOSED"


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
    _write_fixture(_composed_chrome(), FIXTURE_CHROME)
    _write_art_fixture()


# -- THE CARD ART CONTRACT ------------------------------------------------------
#
# The compositor now rasterises the bundled card SVGs itself (NATIVE_OS_PROGRAM 3.2,
# decision (a)). The files live outside the crate, and both the deepbox loop and the
# crane source filter ship `compositor/` alone, so a Rust test "over the directory"
# cannot see the directory. The same answer as the wire payload: pin every asset,
# verbatim, into the generated module, and let the Rust side decode every one of them.
# This half pins that the module IS the directory; the other half pins that each
# decodes to pixels. An asset added or edited without regenerating fails here; one the
# rasteriser cannot render fails there.
ART_DIR = os.path.join(REPO, "integrations", "agent_engine", "static", "app_art")
ART_CONST = "pub const BUNDLED_CARD_ART: &[(&str, &str)] = &["
ART_CLOSE = "\n];"

# The SVG vocabulary the compositor's rasteriser is asked to cover, and the elements
# it is deliberately NOT asked to: usvg renders filters, masks and patterns too, but
# each is a per-pixel pass the compose-once budget never priced, and `<image>` would
# be a raster decoder the build leaves out. An asset that reaches for one fails here,
# in review, rather than as a card that quietly rendered without it.
ART_ALLOWED_TAGS = {
    "svg", "defs", "linearGradient", "radialGradient", "stop", "rect", "circle",
    "ellipse", "g", "path", "line", "polyline", "polygon", "text", "title", "desc",
}


def _bundled_art():
    """(relative path, source) for every bundled SVG, sorted, slashes forward."""
    out = []
    for root, _dirs, files in os.walk(ART_DIR):
        for name in files:
            if not name.endswith(".svg"):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, ART_DIR).replace(os.sep, "/")
            with open(full, encoding="utf-8") as f:
                out.append((rel, f.read()))
    return sorted(out)


def _art_fixture_body():
    lines = []
    for rel, src in _bundled_art():
        assert '"##' not in src and "\r" not in src, rel
        lines.append('    (%s, r##"%s"##),' % (json.dumps(rel), src.rstrip("\n")))
    return "\n".join(lines)


def _write_art_fixture():
    src = open(FIXTURE, encoding="utf-8").read()
    i = src.index(ART_CONST) + len(ART_CONST)
    j = src.index(ART_CLOSE, i)
    with open(FIXTURE, "w", encoding="utf-8", newline="\n") as f:
        f.write(src[:i] + "\n" + _art_fixture_body() + src[j:])


def test_the_art_fixture_is_every_bundled_card_svg_verbatim():
    src = open(FIXTURE, encoding="utf-8").read()
    i = src.index(ART_CONST) + len(ART_CONST)
    j = src.index(ART_CLOSE, i)
    assert src[i + 1:j] == _art_fixture_body(), (
        "compositor/src/wire_fixture.rs is stale for the card art: an asset under "
        "static/app_art was added, removed or edited. Regenerate with the same "
        "_write_all_fixtures command, then read the diff.")
    names = [rel for rel, _ in _bundled_art()]
    assert len(names) == 51, "the program counts 51 bundled assets: %d" % len(names)
    assert "apps/com.brave.Browser.svg" in names and "app-files.svg" in names


def test_every_bundled_card_svg_stays_inside_the_vocabulary_the_compositor_renders():
    import xml.etree.ElementTree as ET
    seen_text = 0
    for rel, src in _bundled_art():
        root = ET.fromstring(src)
        for el in root.iter():
            tag = el.tag.split("}", 1)[-1]
            assert tag in ART_ALLOWED_TAGS, (
                "%s uses <%s>, which the native art path does not render" % (rel, tag))
            for attr in el.attrib:
                assert attr.split("}", 1)[-1] not in ("filter", "mask", "clip-path",
                                                       "style"), (rel, tag, attr)
            if tag == "text":
                seen_text += 1
    # The parity program recorded "ZERO uses of <text>". That was wrong for the 39
    # app icons, which is why the build carries usvg's text feature; pin the fact so
    # the next inventory does not repeat the miss.
    assert seen_text == 39, "expected the 39 app initials as <text>, found %d" % seen_text

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
                    "home-row", "toast", "context-menu"}, (
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


# -- THE CHROME CONTRACT -------------------------------------------------------
#
# `shell.chrome` (IPC_PROTOCOL.md 4.13) is the sibling of the home feed: the bar
# content the home never carried (clock, tray glyphs, badge, agent cluster, task
# chips, start state, a toast, a context menu). `compose_shell_chrome` is its single
# producer, the way `_sanitize_home_payload` is the home's, and this pins what it
# SENDS into compositor/src/wire_fixture.rs for scene.rs to decode and draw. Same
# failure class, same cure: a bar decoder written against an imagined payload would
# pass every Rust test while drawing the wrong glyph names on the box.

import datetime as _dt

# 14:05 so the 12-hour clock has to carry its PM and its leading zero; a Wednesday
# so the date's weekday and month are both long words.
CHROME_NOW = _dt.datetime(2026, 9, 23, 14, 5, 0)
# A connectivity summary in the exact shape _ConnectivityCache.summary() returns,
# chosen so every glyph resolver takes a NON-default branch: a joined wifi at a
# middling signal, a powered adapter with a device connected, a battery on its way
# down, a quiet but unmuted volume.
CHROME_CONNECTIVITY = {
    'wifi': {'available': True, 'enabled': True, 'connected': True,
             'ssid': 'hive', 'signal': 62, 'blocked': None},
    'bluetooth': {'available': True, 'powered': True, 'connected_count': 1},
    'battery': {'available': True, 'percent': 64, 'plugged_in': False,
                'state': 'discharging'},
    'volume': {'available': True, 'volume': 35, 'muted': False},
}
# Dashboard agent rows: one idle (filtered), one with only a goal_type (the JS's
# fallback name), one with a name past the 16-character clip, and more running
# than the four chips the bar shows.
CHROME_AGENTS = [
    {'name': 'Scout', 'status': 'running'},
    {'name': 'Napping', 'status': 'idle'},
    {'goal_type': 'summarise_inbox_for_me', 'status': 'running'},
    {'name': 'Archivist', 'status': 'running'},
    {'name': 'Cartographer', 'status': 'running'},
    {'name': 'Fifth wheel', 'status': 'running'},
]
CHROME_TASKS = [
    {'id': 'files', 'title': 'Files', 'icon': 'folder', 'active': True},
    {'id': 'terminal', 'title': 'Terminal', 'icon': 'terminal'},
    # An instance id, markup in the title, and an icon that is not a ligature name.
    {'id': 'web#2', 'title': 'Hevolve <b>docs</b>', 'icon': 'Not A Ligature'},
]
CHROME_TOAST = {'title': 'Bluetooth', 'message': 'Not available',
                'severity': 'warning'}
CHROME_MENU = {'x': 412, 'y': 300, 'items': [
    {'label': 'Open', 'icon': 'open_in_new'},
    {'sep': True},
    {'label': 'Delete', 'danger': True},
    {'label': 'Rename', 'disabled': True},
]}


def _composed_chrome():
    return L.compose_shell_chrome(
        CHROME_NOW, CHROME_CONNECTIVITY, CHROME_AGENTS, 2, tasks=CHROME_TASKS,
        start_open=False, toast=CHROME_TOAST, menu=CHROME_MENU)


def test_the_chrome_fixture_is_still_what_the_producer_actually_emits():
    """THE CONTRACT, bar half. Regenerate with the same command as the home:

        python -c "import sys; sys.path.insert(0,'.'); \\
          from tests.unit.test_native_wire_contract import _write_all_fixtures; \\
          _write_all_fixtures()"

    then teach scene.rs to decode and DRAW whatever is new, and read the diff
    before trusting it.
    """
    assert _fixture_json(FIXTURE_CHROME) == _composed_chrome(), (
        "compositor/src/wire_fixture.rs is stale for the chrome fixture: the bar "
        "producer now emits a different shape than the native bars are tested against")


def test_the_chrome_fixture_carries_the_shapes_the_bars_show():
    """Guard the guard: a fixture that lost its interesting cases would still pass
    the comparison above. Each of these is a datum the WebView bar shows today."""
    c = _composed_chrome()
    assert c['clock'] == {'time': '02:05 PM', 'date': 'Wednesday, September 23'}, (
        "the clock is the 12-hour, zero-padded, long-weekday form tickClock renders")
    tray = c['tray']
    assert tray['wifi'] == 'network_wifi_3_bar', "a 62 signal is three bars"
    assert tray['bluetooth'] == 'bluetooth_connected'
    assert tray['battery'] == 'battery_4_bar' and tray['battery_pct'] == '64%'
    assert tray['volume'] == 'volume_down' and tray['live'] is True
    assert c['notifications'] == {'unread': 2}, "a badge with something behind it"
    assert c['agents'] == ['Scout', 'summarise_inbox_', 'Archivist', 'Cartographer'], (
        "running only, the goal_type fallback, the 16-character clip, four at most")
    assert c['tasks'][0] == {'id': 'files', 'title': 'Files', 'icon': 'folder',
                             'active': True}, "an active chip with its icon"
    assert c['tasks'][2] == {'id': 'web#2', 'title': 'Hevolve bdocs/b', 'active': False}, (
        "markup stripped, a non-ligature icon dropped, an instance id kept")
    assert c['start'] == {'open': False}
    assert c['toast'] == CHROME_TOAST
    assert c['menu']['items'][1] == {'sep': True} and c['menu']['items'][2]['danger']
    assert c['menu']['items'][3]['disabled'] and c['menu']['x'] == 412


def test_the_chrome_producer_leaves_absent_what_it_was_not_given():
    """The claim rule reads ABSENCE. A datum the shell could not compose must be
    missing from the wire, never an empty stand-in, or the compositor would claim
    a band that is not fully drawn and the shell would stop painting its own."""
    c = L.compose_shell_chrome(None, None, None, None)
    for key in ('clock', 'tray', 'notifications', 'agents', 'tasks', 'toast', 'menu'):
        assert key not in c, "%s was invented from nothing" % key
    assert c == {'start': {'open': False}}, "only the start state has a default"
    # An empty agent list and an empty panel list are COMPOSED, not absent.
    c = L.compose_shell_chrome(None, None, [], 0, tasks=[])
    assert c['agents'] == [] and c['tasks'] == [] and c['notifications'] == {'unread': 0}
    # A count must be a count.
    assert 'notifications' not in L.compose_shell_chrome(None, None, None, True)
    assert 'notifications' not in L.compose_shell_chrome(None, None, None, -1)
    # A toast with no words and a menu with no rows are not on screen.
    c = L.compose_shell_chrome(None, None, None, None, toast={'severity': 'error'},
                               menu={'x': 1, 'y': 1, 'items': [{'sep': True}]})
    assert 'toast' not in c
    assert 'menu' in c, "a divider-only menu still has geometry"
    assert 'menu' not in L.compose_shell_chrome(None, None, None, None,
                                                menu={'x': 1, 'items': []})


def test_the_chrome_producer_drops_what_it_promises_to_drop():
    """The native bars draw what they are given, so this is the only thing between
    a hostile panel title or agent name and the top of the screen."""
    c = L.compose_shell_chrome(
        None, None,
        [{'name': '<img onerror=x>', 'status': 'running'}], None,
        toast={'title': '<b>x</b>', 'message': 'y', 'severity': 'shout'},
        menu={'x': 1, 'y': 2, 'items': [{'label': '<i>Open</i>', 'icon': 'Not A Name'}]})
    assert c['agents'] == ['img onerror=x'], "angle brackets never reach the bar"
    assert c['toast']['severity'] == 'info', "an unknown severity falls back, as showToast does"
    assert c['toast']['title'] == 'bx/b'
    assert c['menu']['items'][0] == {'label': 'iOpen/i'}, "a non-ligature icon is dropped"
    # An unavailable domain reads as its neutral glyph, never as a guess.
    c = L.compose_shell_chrome(None, {'wifi': {}, 'bluetooth': None,
                                      'battery': {'available': True, 'percent': 'lots'},
                                      'volume': {'available': True, 'volume': 0}}, None, None)
    assert c['tray'] == {'wifi': 'wifi_off', 'bluetooth': 'bluetooth_disabled',
                         'battery': 'battery_unknown', 'battery_pct': '',
                         'volume': 'volume_off', 'live': True}


CONNECTIVITY_JS = os.path.join(REPO, "integrations", "agent_engine", "static",
                               "hartConnectivity.js")


def _js_resolver(src, name):
    m = re.search(r"function %s\([^)]*\) \{(.*?)\n  \}" % name, src, re.S)
    assert m, "hartConnectivity.js no longer has %s" % name
    return m.group(1)


def test_the_tray_glyphs_are_hartconnectivity_own_resolvers():
    """The tray is rendered by hartConnectivity.js and the native tray must show the
    SAME glyph for the same summary. The Python mirror cannot import the JS, so this
    pins both halves: every glyph name and threshold the mirror uses is read out of
    the JS resolvers, and the resolvers' branches are exercised as a table."""
    js = open(CONNECTIVITY_JS, encoding="utf-8").read()
    wifi = _js_resolver(js, "wifiGlyph")
    for floor, glyph in L._TRAY_WIFI_BARS:
        assert "return '%s'" % glyph in wifi, "wifiGlyph no longer returns %s" % glyph
        if floor:
            assert "sgl >= %d" % floor in wifi, "wifi threshold %d moved" % floor
    assert "return 'wifi_off'" in wifi and "return 'wifi_find'" in wifi
    bt = _js_resolver(js, "btGlyph")
    for glyph in ("bluetooth_disabled", "bluetooth_connected", "bluetooth"):
        assert "return '%s'" % glyph in bt
    bat = _js_resolver(js, "batGlyph")
    for floor, glyph in L._TRAY_BATTERY_BARS:
        assert "return '%s'" % glyph in bat, "batGlyph no longer returns %s" % glyph
        if floor >= 0:
            assert "p > %d" % floor in bat, "battery threshold %d moved" % floor
    assert "return 'battery_charging_full'" in bat and "return 'battery_unknown'" in bat
    vol = _js_resolver(js, "volGlyph")
    assert "v.volume < %d" % L._TRAY_VOLUME_DOWN_BELOW in vol
    for glyph in ("volume_up", "volume_off", "volume_down"):
        assert "return '%s'" % glyph in vol

    # The branches, as a table read off the JS above.
    W = L._tray_wifi_glyph
    assert W(None) == 'wifi_off'
    assert W({'available': True, 'enabled': False}) == 'wifi_off'
    assert W({'available': True, 'enabled': True, 'connected': False}) == 'wifi_find'
    for sig, want in ((100, 'wifi'), (75, 'wifi'), (74, 'network_wifi_3_bar'),
                      (50, 'network_wifi_3_bar'), (49, 'network_wifi_2_bar'),
                      (25, 'network_wifi_2_bar'), (24, 'network_wifi_1_bar'),
                      (0, 'network_wifi_1_bar'), (None, 'wifi')):
        assert W({'available': True, 'enabled': True, 'connected': True,
                  'signal': sig}) == want, (sig, want)
    B = L._tray_bluetooth_glyph
    assert B({'available': True, 'powered': False}) == 'bluetooth_disabled'
    assert B({'available': True, 'powered': True, 'connected_count': 0}) == 'bluetooth'
    T = L._tray_battery_glyph
    assert T({'available': True, 'percent': 10, 'state': 'charging'}) == 'battery_charging_full'
    assert T({'available': True, 'percent': 10, 'plugged_in': True}) == 'battery_charging_full'
    for pct, want in ((100, 'battery_full'), (91, 'battery_full'), (90, 'battery_6_bar'),
                      (71, 'battery_6_bar'), (70, 'battery_4_bar'), (51, 'battery_4_bar'),
                      (50, 'battery_3_bar'), (31, 'battery_3_bar'), (30, 'battery_2_bar'),
                      (16, 'battery_2_bar'), (15, 'battery_alert'), (0, 'battery_alert')):
        assert T({'available': True, 'percent': pct, 'state': 'discharging'}) == want, (pct, want)
    V = L._tray_volume_glyph
    assert V({'available': True, 'volume': 50, 'muted': True}) == 'volume_off'
    assert V({'available': True, 'volume': 39}) == 'volume_down'
    assert V({'available': True, 'volume': 40}) == 'volume_up'


def test_the_agent_cluster_mirrors_refreshagentstatus():
    """`refreshAgentStatus` in the served shell filters to running, shows four chips
    and clips each name at 16. The producer's constants are read against that JS."""
    src = open(os.path.join(REPO, "integrations", "agent_engine",
                            "liquid_ui_service.py"), encoding="utf-8").read()
    # The filter, the clip and the chip cap may live in refreshAgentStatus itself or in
    # the paintAgentStatus it hands names to (the SSE push shares that painter), so
    # the window covers both.
    i = src.index("function refreshAgentStatus()")
    j = src.rfind("function paintAgentStatus", 0, i)
    body = src[(j if j >= 0 else i):i + 900]
    assert "a.status==='running'" in body
    assert "slice(0,%d)" % L.CHROME_AGENTS_MAX in body, "the chip count moved"
    assert "substring(0,%d)" % L.CHROME_AGENT_NAME_MAX in body, "the name clip moved"
    assert "a.name||a.goal_type||'agent'" in body, "the fallback name order moved"
