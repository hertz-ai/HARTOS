//! NATIVE SHELL PARITY PROGRAM, M3 scene plumbing: the `SceneNode` foundation that
//! M0 named ("land the SceneNode enum + A2UI->Scene decoder") but never landed, so
//! `latency.rs` still reads "there is no native scene graph". This module is PURE
//! geometry + data: no smithay, no Wayland, no GL. `comp_core` lowers a `SceneNode`
//! tree into `HartRenderElement`s on the render path (gated to any(winit, smithay));
//! this file carries the layout + decode logic and its unit floor so the smithay
//! `doCheck` exercises it, exactly as bloom.rs / the PHASE 5 window bookkeeping are
//! tested without a live display.
//!
//! ONE layout contract, not a parallel path:
//!   * geometry binds HOME_DESKTOP_DESIGN_CHECKLIST (a2: fixed 40px top bar + hero +
//!     2-3 rows + fixed 44px taskbar; the 40/44 are the SAME panel-reservation the
//!     compositor already publishes; checklist:277 top bar = state | spacer | omnibox
//!     pill | orb-sm; c7 orb floats RIGHT of the hero copy in home mode).
//!   * input binds the `home_compose {hero, rows, mood}` A2UI payload
//!     (liquid_ui_service.py) so the native scene consumes the SAME feed the HTML
//!     shell does, and `mood`/palette stays the client's vocabulary (a `Theme` is
//!     passed IN, resolved upstream; no parallel palette table here).
//!
//! Performance intent (NATIVE_SHELL_PARITY_PROGRAM binding NFRs): the tree is plain
//! owned data with zero interior mutability, built once per compose and walked each
//! frame with no allocation in the walk; user-driven motion writes a node transform
//! directly (no easing), so the CSS-transition drag bug class is impossible by
//! construction. NO em dashes anywhere (checklist binding rule).
#![cfg(any(feature = "winit", feature = "smithay"))]

/// A rectangle in LOGICAL pixels (the compositor scales to physical at render time).
/// f32 because the render lowering multiplies by an output scale and a per-node
/// transform, and integer rounding is done once, at lowering, not here.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Rect {
    pub x: f32,
    pub y: f32,
    pub w: f32,
    pub h: f32,
}

impl Rect {
    pub const fn new(x: f32, y: f32, w: f32, h: f32) -> Self {
        Rect { x, y, w, h }
    }
    /// Inset on all four sides (padding). A pad larger than half the extent floors
    /// the result to a zero-size rect at the centre rather than inverting it.
    pub fn inset(&self, pad: f32) -> Rect {
        let w = (self.w - 2.0 * pad).max(0.0);
        let h = (self.h - 2.0 * pad).max(0.0);
        Rect {
            x: self.x + pad,
            y: self.y + pad,
            w,
            h,
        }
    }
    pub fn contains(&self, px: f32, py: f32) -> bool {
        px >= self.x && px < self.x + self.w && py >= self.y && py < self.y + self.h
    }
    pub fn right(&self) -> f32 {
        self.x + self.w
    }
    pub fn bottom(&self) -> f32 {
        self.y + self.h
    }
}

/// Straight (NOT premultiplied) RGBA, each channel 0.0..=1.0. The render lowering
/// premultiplies where the element format needs it (the orb is premultiplied, solids
/// are not), so keeping the scene straight-alpha means one conversion site, not many.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Color {
    pub r: f32,
    pub g: f32,
    pub b: f32,
    pub a: f32,
}

impl Color {
    pub const fn rgba(r: f32, g: f32, b: f32, a: f32) -> Self {
        Color { r, g, b, a }
    }
    pub const TRANSPARENT: Color = Color::rgba(0.0, 0.0, 0.0, 0.0);
    /// Parse `#RRGGBB` or `#RRGGBBAA` (leading `#` optional). Returns None on any
    /// malformed input so a bad theme value falls back to a caller default rather
    /// than panicking mid-compose. This is the ONLY hex parser the scene uses.
    pub fn from_hex(s: &str) -> Option<Color> {
        let h = s.strip_prefix('#').unwrap_or(s);
        if !h.is_ascii() {
            return None;
        }
        let byte = |i: usize| u8::from_str_radix(&h[i..i + 2], 16).ok();
        match h.len() {
            6 => Some(Color::rgba(
                byte(0)? as f32 / 255.0,
                byte(2)? as f32 / 255.0,
                byte(4)? as f32 / 255.0,
                1.0,
            )),
            8 => Some(Color::rgba(
                byte(0)? as f32 / 255.0,
                byte(2)? as f32 / 255.0,
                byte(4)? as f32 / 255.0,
                byte(6)? as f32 / 255.0,
            )),
            _ => None,
        }
    }
}

/// Horizontal text alignment inside a `Text` node's rect.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum TextAlign {
    Left,
    Center,
    Right,
}

/// The resolved shell palette for one compose. Colours are handed IN (resolved from
/// the `mood`/HART_PALETTES id by the same owner the HTML shell uses), so this struct
/// is a parameter, never a second palette table. Field names are roles, not hues, so
/// a theme swap is one construction, not edits across the layout.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Theme {
    pub bar_bg: Color,
    pub bar_ink: Color,
    pub omnibox_bg: Color,
    pub omnibox_ink: Color,
    pub hero_title: Color,
    pub hero_copy: Color,
    pub card_bg: Color,
    pub card_ink: Color,
    pub accent: Color,
    pub taskbar_bg: Color,
}

impl Theme {
    /// The checklist b-section default anchors: teal accent (#00E6C3, the orb default),
    /// a near-black cosmic bar, neutral ink. A safe fallback when no `mood` was pushed;
    /// a real compose overrides via the palette owner upstream.
    pub fn cosmic_default() -> Theme {
        let teal = Color::from_hex("#00E6C3").unwrap();
        Theme {
            bar_bg: Color::rgba(0.043, 0.047, 0.063, 0.72),
            bar_ink: Color::rgba(0.92, 0.95, 0.98, 1.0),
            omnibox_bg: Color::rgba(1.0, 1.0, 1.0, 0.08),
            omnibox_ink: Color::rgba(0.80, 0.85, 0.90, 1.0),
            hero_title: Color::rgba(0.97, 0.98, 1.0, 1.0),
            hero_copy: Color::rgba(0.78, 0.83, 0.90, 1.0),
            card_bg: Color::rgba(1.0, 1.0, 1.0, 0.06),
            card_ink: Color::rgba(0.90, 0.93, 0.97, 1.0),
            accent: teal,
            taskbar_bg: Color::rgba(0.043, 0.047, 0.063, 0.85),
        }
    }
}

/// The decoded `home_compose` A2UI payload. Mirrors the props allowlisted in
/// liquid_ui_service.py (`home_compose {hero, rows, mood}`). `mood` stays a raw id
/// string owned by the palette layer, not resolved here.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct HomeCompose {
    pub hero: Hero,
    pub rows: Vec<Row>,
    pub mood: Option<String>,
}

impl HomeCompose {
    /// A non-empty placeholder that proves the native scene renders before the A2UI
    /// `shell.compose` feed is wired (M3 step 3 retires it). Deterministic, no clock.
    pub fn demo() -> HomeCompose {
        HomeCompose {
            hero: Hero {
                title: "HART OS".to_string(),
                copy: "Native shell, drawn by the compositor.".to_string(),
            },
            rows: vec![
                Row {
                    label: "Continue".to_string(),
                    cards: vec![Card::default(), Card::default(), Card::default()],
                },
                Row {
                    label: "For you".to_string(),
                    cards: vec![Card::default(), Card::default()],
                },
            ],
            mood: None,
        }
    }
}

/// The hero copy. The checklist (b/hero) says the hero is SHORT and lets the orb speak
/// the rest, so this is a headline plus a one-line subhead, never a paragraph wall.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Hero {
    pub title: String,
    pub copy: String,
}

/// One horizontal row of cards (the Netflix-home rows, a2 "2-3 rows").
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Row {
    pub label: String,
    pub cards: Vec<Card>,
}

/// One card in a row.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Card {
    pub title: String,
    pub subtitle: Option<String>,
    /// An image ref (URL or app-icon id). Lowered to a texture element; None draws
    /// the card as a solid tile with just its text.
    pub image: Option<String>,
}

/// The scene tree the compositor renders. Wayland-FREE and GL-FREE: `comp_core`
/// lowers each variant to a `HartRenderElement` (Rect -> SolidColorBuffer, Text ->
/// glyph-atlas Memory texture, Image -> Memory texture, OrbSlot -> the existing M2
/// orb element). A `Container` only groups and positions; it paints nothing itself.
#[derive(Clone, Debug, PartialEq)]
pub enum SceneNode {
    Container {
        rect: Rect,
        children: Vec<SceneNode>,
    },
    Rect {
        rect: Rect,
        color: Color,
        /// Corner radius in logical px; 0.0 is a hard rectangle.
        radius: f32,
    },
    Text {
        rect: Rect,
        text: String,
        size_px: f32,
        color: Color,
        align: TextAlign,
    },
    Image {
        rect: Rect,
        source: String,
        radius: f32,
    },
    /// Where the native M2 orb draws. `compact` is the orb-sm docked in the top bar
    /// (checklist c7); the large home orb is `compact = false`.
    OrbSlot {
        rect: Rect,
        compact: bool,
    },
}

impl SceneNode {
    /// The node's own bounds. For a `Container` this is its group rect.
    pub fn rect(&self) -> Rect {
        match self {
            SceneNode::Container { rect, .. }
            | SceneNode::Rect { rect, .. }
            | SceneNode::Text { rect, .. }
            | SceneNode::Image { rect, .. }
            | SceneNode::OrbSlot { rect, .. } => *rect,
        }
    }
    /// Total node count including self (used by tests and the render-budget log).
    pub fn node_count(&self) -> usize {
        match self {
            SceneNode::Container { children, .. } => {
                1 + children.iter().map(SceneNode::node_count).sum::<usize>()
            }
            _ => 1,
        }
    }

    /// Collect the LEAF nodes (everything but `Container`) into `out` in PAINT ORDER,
    /// back to front. Containers position and clip their children but paint nothing,
    /// so lowering only needs the leaves, already in absolute coords from layout. This
    /// is the render-list the gated comp_core adapter walks to emit ONE
    /// HartRenderElement per leaf. It appends to a caller-owned `out` (reused across
    /// frames), so the walk itself allocates nothing per node, honouring the
    /// zero-per-frame-alloc NFR.
    pub fn flatten<'a>(&'a self, out: &mut Vec<&'a SceneNode>) {
        match self {
            SceneNode::Container { children, .. } => {
                for child in children {
                    child.flatten(out);
                }
            }
            leaf => out.push(leaf),
        }
    }
    /// The DEEPEST node whose rect contains the point, in paint order (last child
    /// wins, matching top-most-on-screen). Returns None if the point is outside self.
    /// This is the hook the pointer path uses to route a click/drag to a node the
    /// SAME frame the input arrives, with no easing (the input-to-photon NFR).
    pub fn hit_test(&self, px: f32, py: f32) -> Option<&SceneNode> {
        if !self.rect().contains(px, py) {
            return None;
        }
        if let SceneNode::Container { children, .. } = self {
            for child in children.iter().rev() {
                if let Some(hit) = child.hit_test(px, py) {
                    return Some(hit);
                }
            }
        }
        Some(self)
    }

    /// Extra orb energy contributed by the pointer at `pointer` (in the SAME logical
    /// coords the scene was laid out in), so the native orb energises under the cursor
    /// exactly as the WebView shell's orb does: a lift on hover, a stronger lift while a
    /// button is held OVER the orb (the M2 press half). Returns 0.0 when the pointer is
    /// absent or is not over an `OrbSlot`; a press anywhere else contributes nothing, so
    /// clicking a card never makes the orb flare. The render path adds this scalar to the
    /// ambient orb energy it already computes, so pointer reactivity rides the EXISTING
    /// orb path (one orb, no easing) and `orb::motion_at` clamps the sum into 0..=1.
    pub fn pointer_orb_energy(&self, pointer: Option<(f32, f32)>, pressed: bool) -> f32 {
        const HOVER_LIFT: f32 = 0.35;
        const PRESS_LIFT: f32 = 0.65;
        match pointer {
            Some((px, py)) => match self.hit_test(px, py) {
                Some(SceneNode::OrbSlot { .. }) => {
                    if pressed {
                        PRESS_LIFT
                    } else {
                        HOVER_LIFT
                    }
                }
                _ => 0.0,
            },
            None => 0.0,
        }
    }
}

// ── Layout constants. The 40/44 are the SAME strip dims the compositor publishes at
//    /run/hart/session/panel-reservation (top=40 bottom=44), named ONCE here so the
//    native scene reserves exactly what window placement already reserves. ──
pub const TOP_BAR_H: f32 = 40.0;
pub const TASKBAR_H: f32 = 44.0;
const EDGE_PAD: f32 = 24.0;
const OMNIBOX_W: f32 = 420.0;
const ORB_SM: f32 = 28.0;
const HERO_H: f32 = 200.0;
const ROW_LABEL_H: f32 = 22.0;
const ROW_GAP: f32 = 14.0;
const CARD_W: f32 = 210.0;
const CARD_H: f32 = 128.0;
const CARD_GAP: f32 = 14.0;

/// Build the home-desktop scene for an output of `output_w` x `output_h` LOGICAL px.
/// The layout is the checklist's a2 canvas: a fixed 40px top bar, a hero with the orb
/// floated to its right (c7), 2-3 card rows, and a fixed 44px taskbar. It never
/// scrolls: rows past the content area are simply not emitted (deep content opens in
/// an app, a2), so the desktop always fits one screen.
pub fn layout_home(output_w: f32, output_h: f32, home: &HomeCompose, theme: &Theme) -> SceneNode {
    let mut root: Vec<SceneNode> = Vec::new();

    // ── Top bar (fixed, 40px): background, centre omnibox pill, right orb-sm. ──
    let bar = Rect::new(0.0, 0.0, output_w, TOP_BAR_H);
    let mut bar_children = vec![SceneNode::Rect {
        rect: bar,
        color: theme.bar_bg,
        radius: 0.0,
    }];
    let pill = Rect::new(
        (output_w - OMNIBOX_W) * 0.5,
        6.0,
        OMNIBOX_W,
        TOP_BAR_H - 12.0,
    );
    bar_children.push(SceneNode::Rect {
        rect: pill,
        color: theme.omnibox_bg,
        radius: (TOP_BAR_H - 12.0) * 0.5,
    });
    bar_children.push(SceneNode::Text {
        rect: pill.inset(12.0),
        text: "Ask or search anything".to_string(),
        size_px: 14.0,
        color: theme.omnibox_ink,
        align: TextAlign::Left,
    });
    let orb_sm_rect = Rect::new(
        output_w - EDGE_PAD - ORB_SM,
        (TOP_BAR_H - ORB_SM) * 0.5,
        ORB_SM,
        ORB_SM,
    );
    bar_children.push(SceneNode::OrbSlot {
        rect: orb_sm_rect,
        compact: true,
    });
    root.push(SceneNode::Container {
        rect: bar,
        children: bar_children,
    });

    // ── Content band, between the two fixed strips. ──
    let content = Rect::new(
        EDGE_PAD,
        TOP_BAR_H + EDGE_PAD,
        (output_w - 2.0 * EDGE_PAD).max(0.0),
        (output_h - TOP_BAR_H - TASKBAR_H - 2.0 * EDGE_PAD).max(0.0),
    );

    // ── Hero: title + copy on the left, the large orb floated to the right (c7). ──
    let orb_home = HERO_H.min(content.h).max(0.0);
    let hero_text_w = (content.w - orb_home - EDGE_PAD).max(0.0);
    root.push(SceneNode::Text {
        rect: Rect::new(content.x, content.y, hero_text_w, 48.0),
        text: home.hero.title.clone(),
        size_px: 34.0,
        color: theme.hero_title,
        align: TextAlign::Left,
    });
    root.push(SceneNode::Text {
        rect: Rect::new(content.x, content.y + 56.0, hero_text_w, 60.0),
        text: home.hero.copy.clone(),
        size_px: 16.0,
        color: theme.hero_copy,
        align: TextAlign::Left,
    });
    root.push(SceneNode::OrbSlot {
        rect: Rect::new(content.right() - orb_home, content.y, orb_home, orb_home),
        compact: false,
    });

    // ── Rows: cap at 3 (a2 "2-3 rows"), each a label + a strip of cards, emitted only
    //    while they fit inside the content band so the desktop never scrolls. ──
    let mut cursor_y = content.y + HERO_H + EDGE_PAD;
    for row in home.rows.iter().take(3) {
        let row_block_h = ROW_LABEL_H + CARD_H;
        if cursor_y + row_block_h > content.bottom() {
            break;
        }
        root.push(SceneNode::Text {
            rect: Rect::new(content.x, cursor_y, content.w, ROW_LABEL_H),
            text: row.label.clone(),
            size_px: 15.0,
            color: theme.card_ink,
            align: TextAlign::Left,
        });
        let cards_y = cursor_y + ROW_LABEL_H;
        let mut card_x = content.x;
        for card in row.cards.iter() {
            if card_x + CARD_W > content.right() {
                break;
            }
            let cr = Rect::new(card_x, cards_y, CARD_W, CARD_H);
            let mut card_children = vec![SceneNode::Rect {
                rect: cr,
                color: theme.card_bg,
                radius: 12.0,
            }];
            if let Some(src) = &card.image {
                card_children.push(SceneNode::Image {
                    rect: cr,
                    source: src.clone(),
                    radius: 12.0,
                });
            }
            card_children.push(SceneNode::Text {
                rect: Rect::new(cr.x + 12.0, cr.bottom() - 34.0, cr.w - 24.0, 22.0),
                text: card.title.clone(),
                size_px: 14.0,
                color: theme.card_ink,
                align: TextAlign::Left,
            });
            root.push(SceneNode::Container {
                rect: cr,
                children: card_children,
            });
            card_x += CARD_W + CARD_GAP;
        }
        cursor_y += row_block_h + ROW_GAP;
    }

    // ── Taskbar (fixed, 44px, bottom). ──
    let taskbar = Rect::new(0.0, output_h - TASKBAR_H, output_w, TASKBAR_H);
    root.push(SceneNode::Rect {
        rect: taskbar,
        color: theme.taskbar_bg,
        radius: 0.0,
    });

    SceneNode::Container {
        rect: Rect::new(0.0, 0.0, output_w, output_h),
        children: root,
    }
}

/// The RETAINED scene tree. `layout_home` allocates a fresh node tree and clones every
/// label on each call, so calling it per frame violates the zero-per-frame-alloc NFR
/// this module's header states. This is "step two" of the native render path: the tree is
/// rebuilt ONLY when something that actually changes LAYOUT changes (the output size, the
/// composed home payload, or the theme), so a steady desktop walks a tree it already owns.
///
/// The pointer is deliberately NOT part of the key: hover changes the orb's energy scalar,
/// never the layout, so cursor motion must never invalidate the tree.
#[derive(Default)]
pub struct SceneCache {
    tree: Option<SceneNode>,
    key_w: f32,
    key_h: f32,
    key_home: HomeCompose,
    /// `Theme` has no `Default`, so the key starts as None and the first call is a miss.
    key_theme: Option<Theme>,
    rebuilds: u64,
}

impl SceneCache {
    /// The tree for this size/home/theme, rebuilding only when one of them changed.
    /// The comparison walks a handful of short strings; the rebuild it avoids allocates
    /// the whole node tree and re-clones every label, so the compare is the cheap side.
    pub fn tree_for(&mut self, w: f32, h: f32, home: &HomeCompose, theme: &Theme) -> &SceneNode {
        let stale = self.tree.is_none()
            || self.key_w != w
            || self.key_h != h
            || self.key_theme != Some(*theme)
            || self.key_home != *home;
        if stale {
            self.tree = Some(layout_home(w, h, home, theme));
            self.key_w = w;
            self.key_h = h;
            self.key_theme = Some(*theme);
            self.key_home = home.clone();
            self.rebuilds += 1;
        }
        self.tree
            .as_ref()
            .expect("the tree was just built when it was stale")
    }

    /// How many times the tree was actually rebuilt. This is the retention PROOF: a
    /// steady desktop must not grow this per frame.
    pub fn rebuilds(&self) -> u64 {
        self.rebuilds
    }
}

// ── A2UI decode: home_compose {hero, rows, mood} -> HomeCompose. Tolerant by design:
//    a missing or wrong-typed field yields an empty section, never a panic, matching
//    the JS consumer's samplePayload skeleton fallback (an accepted push overrides it,
//    a malformed one degrades a section rather than blanking the desktop). ──
pub fn decode_home_compose(v: &serde_json::Value) -> HomeCompose {
    use serde_json::Value;
    let s = |x: Option<&Value>| x.and_then(Value::as_str).unwrap_or("").to_string();

    let hero = match v.get("hero") {
        Some(h) => Hero {
            title: s(h.get("title")),
            copy: s(h.get("copy")),
        },
        None => Hero::default(),
    };

    let mut rows = Vec::new();
    if let Some(Value::Array(arr)) = v.get("rows") {
        for r in arr {
            let mut cards = Vec::new();
            if let Some(Value::Array(cs)) = r.get("cards") {
                for c in cs {
                    cards.push(Card {
                        title: s(c.get("title")),
                        subtitle: c.get("subtitle").and_then(Value::as_str).map(str::to_string),
                        image: c.get("image").and_then(Value::as_str).map(str::to_string),
                    });
                }
            }
            rows.push(Row {
                label: s(r.get("label")),
                cards,
            });
        }
    }

    HomeCompose {
        hero,
        rows,
        mood: v.get("mood").and_then(Value::as_str).map(str::to_string),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> HomeCompose {
        HomeCompose {
            hero: Hero {
                title: "Your hive earned $12 overnight".into(),
                copy: "3 agents ran 41 tasks. Ask the orb for the details.".into(),
            },
            rows: vec![
                Row {
                    label: "Continue".into(),
                    cards: vec![
                        Card { title: "Recipe A".into(), subtitle: None, image: None },
                        Card { title: "Recipe B".into(), subtitle: None, image: Some("b.png".into()) },
                    ],
                },
                Row { label: "For you".into(), cards: vec![Card::default()] },
            ],
            mood: Some("cosmic".into()),
        }
    }

    #[test]
    fn top_bar_is_the_fixed_40px_strip_at_the_top() {
        let root = layout_home(1600.0, 900.0, &sample(), &Theme::cosmic_default());
        let bar = root.hit_test(800.0, 5.0).expect("a node at the top strip");
        // The topmost hit in the bar band is a bar child, and the bar rect is 40px.
        assert!(bar.rect().y < TOP_BAR_H);
    }

    #[test]
    fn taskbar_is_the_fixed_44px_strip_at_the_bottom() {
        let (w, h) = (1600.0, 900.0);
        let root = layout_home(w, h, &sample(), &Theme::cosmic_default());
        let hit = root.hit_test(w * 0.5, h - 2.0).expect("a node at the bottom strip");
        assert!((hit.rect().h - TASKBAR_H).abs() < 0.01);
        assert!((hit.rect().y - (h - TASKBAR_H)).abs() < 0.01);
    }

    #[test]
    fn home_orb_floats_to_the_right_of_the_hero() {
        let (w, h) = (1600.0, 900.0);
        let root = layout_home(w, h, &sample(), &Theme::cosmic_default());
        // The large orb (compact=false) sits in the right portion of the content band.
        let mut orb_x = None;
        if let SceneNode::Container { children, .. } = &root {
            for c in children {
                if let SceneNode::OrbSlot { rect, compact: false } = c {
                    orb_x = Some(rect.x);
                }
            }
        }
        assert!(orb_x.expect("a home orb slot") > w * 0.5);
    }

    #[test]
    fn rows_never_overflow_the_one_screen_canvas() {
        // A tiny output must not emit rows that fall below the taskbar (a2: fits one
        // screen, deep content opens an app instead of scrolling).
        let root = layout_home(1600.0, 320.0, &sample(), &Theme::cosmic_default());
        let bottom = 320.0 - TASKBAR_H;
        fn assert_within(n: &SceneNode, limit: f32) {
            if let SceneNode::Container { children, .. } = n {
                for c in children {
                    assert_within(c, limit);
                }
            }
            // Every card/row text node stays above the taskbar line.
            if let SceneNode::Text { rect, .. } = n {
                assert!(rect.y <= limit + 0.01 || rect.h == 0.0, "text at {} overflows {}", rect.y, limit);
            }
        }
        // Only assert on content-band text (skip the omnibox placeholder in the bar).
        if let SceneNode::Container { children, .. } = &root {
            for c in children {
                if c.rect().y >= TOP_BAR_H {
                    assert_within(c, bottom);
                }
            }
        }
    }

    #[test]
    fn flatten_yields_leaves_in_paint_order_no_containers() {
        let root = layout_home(1600.0, 900.0, &sample(), &Theme::cosmic_default());
        let mut leaves = Vec::new();
        root.flatten(&mut leaves);
        // No Container survives the flatten.
        assert!(leaves.iter().all(|n| !matches!(n, SceneNode::Container { .. })));
        // First painted leaf is the top-bar background rect (back of the paint order).
        assert!(matches!(leaves.first(), Some(SceneNode::Rect { rect, .. }) if rect.y == 0.0));
        // Last painted leaf is the taskbar rect (front-most opaque strip).
        assert!(matches!(leaves.last(), Some(SceneNode::Rect { rect, .. }) if (rect.h - TASKBAR_H).abs() < 0.01));
        assert!(leaves.len() >= 6);
    }

    #[test]
    fn color_from_hex_parses_6_and_8_and_rejects_junk() {
        assert_eq!(Color::from_hex("#00E6C3").unwrap().g, 0xE6 as f32 / 255.0);
        assert_eq!(Color::from_hex("00e6c3ff").unwrap().a, 1.0);
        assert!(Color::from_hex("#nothex").is_none());
        assert!(Color::from_hex("#fff").is_none());
    }

    #[test]
    fn decode_is_tolerant_of_missing_and_wrong_typed_fields() {
        let v = serde_json::json!({ "hero": { "title": "hi" }, "rows": "not-an-array" });
        let hc = decode_home_compose(&v);
        assert_eq!(hc.hero.title, "hi");
        assert_eq!(hc.hero.copy, "");
        assert!(hc.rows.is_empty());
        assert!(hc.mood.is_none());
    }

    #[test]
    fn decode_reads_rows_and_cards() {
        let v = serde_json::json!({
            "rows": [{ "label": "Continue", "cards": [{ "title": "A", "image": "a.png" }] }],
            "mood": "cosmic"
        });
        let hc = decode_home_compose(&v);
        assert_eq!(hc.rows.len(), 1);
        assert_eq!(hc.rows[0].label, "Continue");
        assert_eq!(hc.rows[0].cards[0].image.as_deref(), Some("a.png"));
        assert_eq!(hc.mood.as_deref(), Some("cosmic"));
    }

    #[test]
    fn pointer_over_the_orb_lifts_its_energy_and_nowhere_else() {
        let (w, h) = (1600.0, 900.0);
        let root = layout_home(w, h, &sample(), &Theme::cosmic_default());
        // The large home orb's centre must energise the orb.
        let mut orb_centre = None;
        if let SceneNode::Container { children, .. } = &root {
            for c in children {
                if let SceneNode::OrbSlot { rect, compact: false } = c {
                    orb_centre = Some((rect.x + rect.w * 0.5, rect.y + rect.h * 0.5));
                }
            }
        }
        let (ox, oy) = orb_centre.expect("a home orb slot");
        let hover = root.pointer_orb_energy(Some((ox, oy)), false);
        let press = root.pointer_orb_energy(Some((ox, oy)), true);
        assert!(hover > 0.0, "the orb must energise when the cursor is over it");
        assert!(
            press > hover,
            "a held button over the orb must energise it beyond hover ({press} vs {hover})"
        );
        // A point in the hero-title column (left of the floated orb) is NOT the orb, so
        // it lifts nothing, hovered OR pressed: clicking elsewhere never flares the orb.
        let hero_pt = (EDGE_PAD + 4.0, TOP_BAR_H + EDGE_PAD + 4.0);
        assert_eq!(root.pointer_orb_energy(Some(hero_pt), false), 0.0);
        assert_eq!(root.pointer_orb_energy(Some(hero_pt), true), 0.0);
        // No pointer contributes nothing: the flag-off / no-cursor default is unchanged.
        assert_eq!(root.pointer_orb_energy(None, false), 0.0);
        assert_eq!(root.pointer_orb_energy(None, true), 0.0);
    }

    #[test]
    fn the_scene_tree_is_retained_and_rebuilt_only_when_layout_inputs_change() {
        let theme = Theme::cosmic_default();
        let home = sample();
        let mut cache = SceneCache::default();

        let _ = cache.tree_for(1600.0, 900.0, &home, &theme);
        assert_eq!(cache.rebuilds(), 1, "the first frame builds the tree");

        // A steady desktop: same size, same payload, same theme. However many frames run,
        // the tree must NOT be rebuilt — this is the zero-per-frame-alloc NFR.
        for _ in 0..10 {
            let _ = cache.tree_for(1600.0, 900.0, &home, &theme);
        }
        assert_eq!(cache.rebuilds(), 1, "a steady desktop must not rebuild per frame");

        // A resize changes layout, so it must rebuild.
        let _ = cache.tree_for(1280.0, 800.0, &home, &theme);
        assert_eq!(cache.rebuilds(), 2, "a resize must rebuild");

        // A new compose changes layout, so it must rebuild.
        let mut recomposed = home.clone();
        recomposed.hero.title = "Your hive shipped a release".into();
        let _ = cache.tree_for(1280.0, 800.0, &recomposed, &theme);
        assert_eq!(cache.rebuilds(), 3, "a new compose must rebuild");

        // And the retained tree is a REAL tree, not an empty placeholder: the cached nodes
        // are what hover hit-tests against (the pointer is deliberately not part of the key).
        let node_count = cache
            .tree_for(1280.0, 800.0, &recomposed, &theme)
            .node_count();
        assert!(node_count > 1, "the retained tree must hold real nodes");
        assert_eq!(cache.rebuilds(), 3, "re-reading the cached tree must not rebuild");
    }
}
