//! NATIVE SHELL PARITY PROGRAM, M3 scene plumbing: the `SceneNode` foundation that
//! M0 named ("land the SceneNode enum + A2UI->Scene decoder") but never landed, which
//! is why `latency.rs` used to read "there is no native scene graph". This module is PURE
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

    /// This colour lifted toward white by `amount` (0.0 unchanged, 1.0 white), with alpha
    /// untouched so a hover never changes how opaque a card is. Lifting toward white
    /// rather than scaling the channels is what keeps a near-black card visibly reactive:
    /// a multiply would leave a dark card almost unchanged.
    pub fn lift(self, amount: f32) -> Color {
        let k = amount.clamp(0.0, 1.0);
        Color::rgba(
            self.r + (1.0 - self.r) * k,
            self.g + (1.0 - self.g) * k,
            self.b + (1.0 - self.b) * k,
            self.a,
        )
    }
}

/// How wide a run of text will be once the renderer shapes it.
///
/// `layout_home` is pure geometry and cannot shape, which is why every text node it
/// emitted before this was either left-aligned in a known box or a fixed slot: nothing
/// could be right-anchored, centred, or placed immediately after another run. That single
/// missing capability, not five missing features, is what kept the P5 top bar at an
/// omnibox and an orb (no wordmark, no nav tabs, no clock) and left `TextAlign` with no
/// consumer. The renderer already shapes in order to rasterize, so it can answer this
/// without a second text stack anywhere.
///
/// `&mut self` because shaping mutates the font system's caches. That is affordable
/// precisely because the tree is RETAINED: a measure runs on a real layout rebuild, never
/// per frame.
pub trait TextMeasure {
    /// The advance width in LOGICAL px of `text` at `size_px`, on ONE unwrapped line.
    fn text_width(&mut self, text: &str, size_px: f32) -> f32;

    /// Whether the Material ligature face the shell's icons rely on is loaded.
    ///
    /// A card icon is not an image: it is the icon's NAME ("storage") shaped by a font
    /// that resolves the name as a ligature. Which means that without the face, the name
    /// renders as the literal WORD, and that is not hypothetical: it is exactly what a
    /// fresh offline ISO did before the shell bundled its fonts, showing "lock" and
    /// "notifications" as text across the tray.
    ///
    /// Defaults to FALSE so the failure is a missing icon rather than a stray word, and
    /// so a measure that knows nothing about fonts (the unit tests') never claims one.
    fn has_icon_face(&self) -> bool {
        false
    }
}

/// A font-free measure: every character is a fixed fraction of the size. Unit tests use
/// it so scene layout stays testable with no font stack at all, and the renderer falls
/// back to it when its font database is empty, which keeps a font-less box laying out
/// sanely instead of collapsing every run to zero width.
pub struct MonoMeasure;

impl TextMeasure for MonoMeasure {
    fn text_width(&mut self, text: &str, size_px: f32) -> f32 {
        // 0.52 em is close to the average advance of a UI sans at these sizes. It only
        // has to be plausible: nothing measured this way is claimed to be exact.
        text.chars().count() as f32 * size_px * 0.52
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
    /// The SECOND brand hue (the shell's `--hart-a2`). The wordmark is two-tone, so the
    /// palette needs both; every other native surface still uses `accent` alone.
    pub accent2: Color,
    /// Ink for text sitting ON `accent` (the shell's card badge, dark on teal). A role,
    /// not a hue: any accent bright enough to need dark text uses this.
    pub on_accent_ink: Color,
    /// The live indicator dot (the shell's `--hart-amb-4`), which rides the ambient mood
    /// rather than the functional accent, so it stays distinct from a badge.
    pub live_dot: Color,
    /// The translucent ground a live tag sits on, dark enough to read over card art.
    pub chip_bg: Color,
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
            // #9B5CFF, the shell's --hart-a2, so the native wordmark reads exactly as the
            // HTML one does rather than inventing a second brand purple.
            accent2: Color::from_hex("#9B5CFF").unwrap(),
            // The shell's own card-badge ink and --hart-amb-4 default, so a native chip
            // reads as the same component rather than a lookalike.
            on_accent_ink: Color::from_hex("#04140F").unwrap(),
            live_dot: Color::from_hex("#FF2E9A").unwrap(),
            chip_bg: Color::rgba(0.031, 0.047, 0.078, 0.72),
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
                eyebrow: "Earned on the hive".to_string(),
                amount: Some(1284),
                amount_unit: "Spark".to_string(),
                agents: 3,
                tasks: 41,
                local: true,
                payout_pending: true,
                primary: Some("Resume".to_string()),
                secondary: Some("Ask anything".to_string()),
            },
            // Every card carries real text. `Card::default()` has an EMPTY title, and the
            // lowering skips an empty run, so a default-card demo drew blank tiles: the
            // worst possible thing for the one payload whose whole job is to prove the
            // scene renders, and the first thing the box would show at the M6 flip before
            // any compose arrives. These also exercise the full card vocabulary (icon,
            // badge, live, meta, progress) so the render tests that lower this payload
            // actually walk those paths.
            rows: vec![
                Row {
                    title: "Continue".to_string(),
                    note: Some("picked up where you left off".to_string()),
                    see_all: Some("panel:continue".to_string()),
                    cards: vec![
                        Card {
                            title: "Morning briefing".to_string(),
                            meta: Some("4 min left".to_string()),
                            progress: Some(0.62),
                            icon: Some("summarize".to_string()),
                            badge: None,
                            live: None,
                            image: None,
                        },
                        Card {
                            title: "Inbox triage".to_string(),
                            meta: Some("12 unread".to_string()),
                            progress: Some(0.25),
                            icon: Some("inbox".to_string()),
                            badge: None,
                            live: Some("running".to_string()),
                            image: None,
                        },
                        Card {
                            title: "Storage report".to_string(),
                            meta: Some("92% used".to_string()),
                            progress: Some(0.92),
                            icon: Some("storage".to_string()),
                            badge: None,
                            live: None,
                            image: None,
                        },
                    ],
                },
                Row {
                    title: "For you".to_string(),
                    note: None,
                    see_all: None,
                    cards: vec![
                        Card {
                            title: "Hive earnings".to_string(),
                            meta: Some("last 24h".to_string()),
                            progress: None,
                            icon: Some("hive".to_string()),
                            badge: Some("NEW".to_string()),
                            live: None,
                            image: None,
                        },
                        Card {
                            title: "Agent recipes".to_string(),
                            meta: Some("3 ready to run".to_string()),
                            progress: None,
                            icon: Some("auto_awesome".to_string()),
                            badge: None,
                            live: None,
                            image: None,
                        },
                    ],
                },
            ],
            mood: None,
        }
    }
}

/// The EARNINGS hero (P4), which is what both producers of this payload actually emit:
/// an eyebrow, a big Spark number, an honest meta strip and two calls to action.
///
/// This used to be `{ title, copy }`. Neither key exists in either producer
/// (`_home_sanitize_hero` and the backbone builder both emit the shape below), so the
/// native hero decoded two fields that were always empty and rendered NOTHING on a live
/// compose. It looked correct only because the demo payload filled the imagined names.
/// The checklist's "hero is SHORT, let the orb speak" still holds: this is a number and a
/// strip, never a paragraph.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Hero {
    /// Small line above the number. Producers default it to "Earned on the hive".
    pub eyebrow: String,
    /// The Spark figure. None means there was no positive balance to lead with, and the
    /// producers omit the whole hero in that case rather than showing a zero.
    pub amount: Option<i64>,
    /// Unit beside the number, defaulted to "Spark" by both producers.
    pub amount_unit: String,
    pub agents: i64,
    pub tasks: i64,
    /// Whether the work was done locally, which the shell says in the stat line.
    pub local: bool,
    pub payout_pending: bool,
    /// Labels only. The actions behind them are not wired natively yet, and a button
    /// that looks live but does nothing is the same lie as a hoverable dead tab.
    pub primary: Option<String>,
    pub secondary: Option<String>,
}

/// One horizontal row of cards (the Netflix-home rows, a2 "2-3 rows").
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Row {
    /// The row heading. Named `title` for the key both producers emit: it was `label`,
    /// which neither sends, so every row on a live compose drew an empty heading.
    pub title: String,
    /// A short qualifier the shell draws beside the label (`row.note`).
    pub note: Option<String>,
    /// The panel a row's "See all" opens (`row.see_all`). Present means the row HAS a
    /// See-all affordance; the shell only draws one when the payload carries a target,
    /// so an absent value must draw nothing rather than a dead control.
    pub see_all: Option<String>,
    pub cards: Vec<Card>,
}

/// One card in a row.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Card {
    pub title: String,
    /// The qualifier under the title (`card.meta`, the shell's `hh-card-meta`). Named for
    /// the key the feed actually carries: this field used to be `subtitle`, which the
    /// home-card sanitizer has never emitted, so it decoded to None on every card ever
    /// composed while the real text was dropped.
    pub meta: Option<String>,
    /// Completion 0..=1 (`card.progress`, the shell's `hh-card-prog` bar). Clamped at
    /// decode, matching the sanitizer, so a malformed value cannot draw past the card.
    pub progress: Option<f32>,
    /// A Material Symbols LIGATURE NAME (`card.icon`, e.g. "storage"), drawn only when
    /// the card has no art, exactly as the shell does. It is text, not an image.
    pub icon: Option<String>,
    /// A short label in the card's top-right corner (`card.badge`).
    pub badge: Option<String>,
    /// A running-agent tag (`card.live`), which SUPERSEDES `badge`: the shell draws one
    /// or the other, never both, so the pair is decoded separately and resolved at layout.
    pub live: Option<String>,
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
        /// Whether this group is a HOVER TARGET: a pointer resting inside it lifts the
        /// background it already draws (see `hover_leaf`). Cards are interactive; the top
        /// bar and the root are structural groups that must never react. This one bool is
        /// the whole "interactive node" notion the native shell needs today, carried by
        /// the group that already exists rather than a new node kind, because reacting to
        /// a cursor is a property of a group, not a thing that paints.
        interactive: bool,
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
        self.for_each_leaf(&mut |_, leaf| out.push(leaf));
    }

    /// Hand every LEAF to `f` in paint order, with its paint-order index. This is the ONE
    /// traversal the render path uses; `flatten` is a thin collector over it for tests.
    ///
    /// A callback rather than a returned Vec because a `Vec<&SceneNode>` borrows the tree
    /// the cache owns, so it cannot be retained across frames the way the tree and the
    /// buffer pools are. Collecting one per lowering was the last per-frame heap traffic
    /// the zero-per-frame-alloc NFR named, after the retained tree and the solid pool.
    pub fn for_each_leaf<'a>(&'a self, f: &mut impl FnMut(usize, &'a SceneNode)) {
        let mut next = 0usize;
        self.walk_leaves(&mut next, f);
    }

    /// `dyn` deliberately: a recursive generic `impl FnMut` can infer the inner callback
    /// as `&mut F` at each level and monomorphize without end. One indirect call per leaf
    /// is far cheaper than the allocation this walk exists to avoid.
    fn walk_leaves<'a>(&'a self, next: &mut usize, f: &mut dyn FnMut(usize, &'a SceneNode)) {
        match self {
            SceneNode::Container { children, .. } => {
                for child in children {
                    child.walk_leaves(next, f);
                }
            }
            leaf => {
                f(*next, leaf);
                *next += 1;
            }
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

    /// The index, in `flatten` paint order, of the leaf that must paint its HOVER state
    /// this frame, or None when the pointer is absent or over nothing interactive. That
    /// leaf is the background `Rect` of the top-most interactive `Container` under the
    /// cursor, which is why a group only counts as a target when its FIRST child is a
    /// Rect: the highlight is a lift of a background the scene already draws, never an
    /// extra node, so hover changes no geometry and no element count.
    ///
    /// An INDEX is what the lowering wants (not a rect, not a borrowed node): it walks the
    /// same flattened leaves in the same order, so a counter comparison is exact, needs no
    /// float compare, and takes no second borrow of the tree.
    pub fn hover_leaf(&self, pointer: Option<(f32, f32)>) -> Option<usize> {
        let (px, py) = pointer?;
        let mut next = 0usize;
        let mut found = None;
        self.hover_leaf_walk(px, py, &mut next, &mut found);
        found
    }

    /// Paint-order walk behind `hover_leaf`. `next` counts the leaves already passed, so
    /// at the moment an interactive container is entered `next` IS the index its first
    /// leaf will take. Later hits overwrite, which is exactly top-most (and deepest) wins,
    /// matching `hit_test`'s rule with one walk and no allocation.
    fn hover_leaf_walk(&self, px: f32, py: f32, next: &mut usize, found: &mut Option<usize>) {
        match self {
            SceneNode::Container {
                rect,
                interactive,
                children,
            } => {
                if *interactive
                    && rect.contains(px, py)
                    && matches!(children.first(), Some(SceneNode::Rect { .. }))
                {
                    *found = Some(*next);
                }
                for child in children {
                    child.hover_leaf_walk(px, py, next, found);
                }
            }
            _ => *next += 1,
        }
    }
}

/// How far a hovered card lifts its background toward white. Small on purpose: the
/// shell's card hover is a nudge that says "this one", not a flash.
pub const CARD_HOVER_LIFT: f32 = 0.08;

// ── Layout constants. The 40/44 are the SAME strip dims the compositor publishes at
//    /run/hart/session/panel-reservation (top=40 bottom=44), named ONCE here so the
//    native scene reserves exactly what window placement already reserves. ──
pub const TOP_BAR_H: f32 = 40.0;
pub const TASKBAR_H: f32 = 44.0;
const EDGE_PAD: f32 = 24.0;
const OMNIBOX_W: f32 = 420.0;
const ORB_SM: f32 = 28.0;
/// Wordmark type size. The shell sets it in the bar's own scale, not the hero's.
const WORDMARK_PX: f32 = 15.0;
/// A text box of `ink_w` centred inside a slot at `slot_x` of width `slot_w`, CLAMPED so
/// it can never exceed the slot.
///
/// The clamp is the point. Icons are ligature names, so a measure that lacks the face
/// reports the width of the WORD ("notifications") rather than of the one glyph it
/// becomes, which is several times the 32px button it has to sit in. Sizing a slot's
/// contents from an untrusted measurement is how a tray glyph ends up drawn over the
/// clock, so the slot wins and the run is clipped instead.
fn centered_box(ink_w: f32, slot_x: f32, slot_w: f32, y: f32, h: f32) -> Rect {
    let w = (ink_w.ceil() + 2.0).min(slot_w).max(0.0);
    Rect::new(slot_x + (slot_w - w) * 0.5, y, w, h)
}

/// The tray glyphs (`.top-bar-right .tray-btn`), in the shell's left-to-right order.
/// Ligature names, so they ride the ordinary text path like every other icon.
const TRAY_GLYPHS: [&str; 3] = ["notifications", "palette", "shield"];
const TRAY_BTN: f32 = 32.0;
const TRAY_PX: f32 = 18.0;
const TRAY_GAP: f32 = 8.0;
const AVATAR_D: f32 = 28.0;
const AVATAR_PX: f32 = 13.0;
/// The shell hardcodes this letter in its own markup, so it is the same letter rather
/// than a guess at whose account it is.
const AVATAR_INITIAL: &str = "H";
const OMNIBOX_GLYPH: &str = "search";
const OMNIBOX_PX: f32 = 14.0;
/// The shortcut hint at the far end of the pill (the shell's `.tbo-kbd`).
const OMNIBOX_KBD: &str = "Super K";
const KBD_PX: f32 = 11.0;
/// The shell's five primary destinations (`.top-bar-nav .tb-tab`), in its order.
const NAV_TABS: [&str; 5] = ["Home", "Agents", "Apps", "Hive", "Earn"];
/// Which tab reads as current. The native scene only lays out the HOME canvas, so home
/// IS the active destination; this becomes state the moment a tab can navigate.
const ACTIVE_TAB: usize = 0;
const TAB_PX: f32 = 13.0;
const TAB_PAD_X: f32 = 10.0;
const TAB_GAP: f32 = 2.0;
const HERO_H: f32 = 200.0;
const HERO_EYEBROW_PX: f32 = 13.0;
const HERO_AMOUNT_PX: f32 = 40.0;
const HERO_UNIT_PX: f32 = 16.0;
/// Both producers default the unit to this, so a payload that omits it still reads right.
const HERO_UNIT_FALLBACK: &str = "Spark";
const HERO_META_PX: f32 = 13.0;
const HERO_BTN_PX: f32 = 13.0;
const HERO_BTN_H: f32 = 30.0;
const HERO_BTN_PAD_X: f32 = 14.0;
const ROW_LABEL_H: f32 = 22.0;
const ROW_LABEL_PX: f32 = 15.0;
/// The note and the See-all are secondary to the label, so they sit a step smaller.
const ROW_NOTE_PX: f32 = 13.0;
const ROW_HEAD_GAP: f32 = 10.0;
/// The shell's own wording (hartHome.js `see.textContent`), not a paraphrase.
const SEE_ALL: &str = "See all";
const ROW_GAP: f32 = 14.0;
const CARD_W: f32 = 210.0;
const CARD_H: f32 = 128.0;
const CARD_GAP: f32 = 14.0;
const CARD_META_H: f32 = 16.0;
/// 5px, the shell's own `.hh-card-prog { height: 5px }`.
const CARD_PROG_H: f32 = 5.0;
const CARD_CHIP_PX: f32 = 12.0;
const CARD_CHIP_H: f32 = 20.0;
const CARD_CHIP_PAD_X: f32 = 8.0;
/// The shell insets both the badge and the live tag 12px from the card's top right.
const CARD_CHIP_INSET: f32 = 12.0;
const CARD_LIVE_DOT: f32 = 8.0;
const CARD_LIVE_GAP: f32 = 6.0;
/// The shell's `.hh-card-ic`: a 34px rounded tile holding a 20px glyph.
const CARD_ICON_BOX: f32 = 34.0;
const CARD_ICON_PX: f32 = 20.0;

/// Build the home-desktop scene for an output of `output_w` x `output_h` LOGICAL px.
/// The layout is the checklist's a2 canvas: a fixed 40px top bar, a hero with the orb
/// floated to its right (c7), 2-3 card rows, and a fixed 44px taskbar. It never
/// scrolls: rows past the content area are simply not emitted (deep content opens in
/// an app, a2), so the desktop always fits one screen.
pub fn layout_home(
    output_w: f32,
    output_h: f32,
    home: &HomeCompose,
    theme: &Theme,
    measure: &mut dyn TextMeasure,
) -> SceneNode {
    let mut root: Vec<SceneNode> = Vec::new();
    // Asked ONCE for the whole layout rather than per glyph: it walks the font database,
    // and the answer cannot change within a single layout pass. Every icon on this
    // desktop is a ligature name, so this one bool decides whether ANY of them draw.
    let icons_available = measure.has_icon_face();

    // ── Top bar (fixed, 40px): background, centre omnibox pill, right orb-sm. ──
    let bar = Rect::new(0.0, 0.0, output_w, TOP_BAR_H);
    let mut bar_children = vec![SceneNode::Rect {
        rect: bar,
        color: theme.bar_bg,
        radius: 0.0,
    }];
    // ── Brand wordmark (P5, the shell's start-btn treatment): "HART" in the accent then
    //    "OS" in the second brand hue. Two runs, so the second must begin exactly where
    //    the first ends. This is the layout that was impossible before `TextMeasure`:
    //    without a width there is no way to butt one run against another. The logo IMAGE
    //    beside it in the HTML shell waits on Image lowering (the image-source contract).
    // The omnibox pill is computed HERE, before the wordmark and tabs are placed, because
    // the tabs need to know where the pill starts in order to stop short of it. It is
    // still PUSHED in paint order below.
    let pill = Rect::new(
        (output_w - OMNIBOX_W) * 0.5,
        6.0,
        OMNIBOX_W,
        TOP_BAR_H - 12.0,
    );
    let mark_h = WORDMARK_PX * 1.3;
    let mark_y = (TOP_BAR_H - mark_h) * 0.5;
    let hart_w = measure.text_width("HART", WORDMARK_PX);
    let gap_w = measure.text_width(" ", WORDMARK_PX);
    let os_w = measure.text_width("OS", WORDMARK_PX);
    // A shaped run needs its whole advance to fit the buffer it is composed into, so the
    // box is the measured width rounded up with a pixel of slack rather than trusting an
    // exact float to survive the f32 -> i32 the lowering does.
    bar_children.push(SceneNode::Text {
        rect: Rect::new(EDGE_PAD, mark_y, hart_w.ceil() + 2.0, mark_h),
        text: "HART".to_string(),
        size_px: WORDMARK_PX,
        color: theme.accent,
        align: TextAlign::Left,
    });
    bar_children.push(SceneNode::Text {
        rect: Rect::new(
            EDGE_PAD + hart_w + gap_w,
            mark_y,
            os_w.ceil() + 2.0,
            mark_h,
        ),
        text: "OS".to_string(),
        size_px: WORDMARK_PX,
        color: theme.accent2,
        align: TextAlign::Left,
    });

    // ── Nav tabs (P5): the shell's five primary destinations, each sized to its own
    //    label, which is the second thing the measure buys. A tab is emitted only while
    //    it fits BEFORE the omnibox pill, the same discipline the card rows use for the
    //    taskbar, so a narrow output drops tabs from the right instead of drawing them
    //    under the pill. They are deliberately NOT hover targets: nothing routes a tab
    //    activation yet, and an affordance that reacts but does nothing is a lie. Wrap
    //    them as interactive groups when a tab actually navigates.
    let tab_h = TAB_PX * 2.0;
    let tab_y = (TOP_BAR_H - tab_h) * 0.5;
    let tab_ink_h = TAB_PX * 1.3;
    let tab_ink_y = (TOP_BAR_H - tab_ink_h) * 0.5;
    let mut tab_x = EDGE_PAD + hart_w + gap_w + os_w + EDGE_PAD;
    for (i, label) in NAV_TABS.iter().enumerate() {
        let ink_w = measure.text_width(label, TAB_PX);
        let slot = ink_w.ceil() + 2.0 * TAB_PAD_X;
        if tab_x + slot > pill.x - TAB_GAP {
            break;
        }
        // The active tab carries the same faint surface the omnibox pill does, so the
        // bar reads as one material rather than two.
        if i == ACTIVE_TAB {
            bar_children.push(SceneNode::Rect {
                rect: Rect::new(tab_x, tab_y, slot, tab_h),
                color: theme.omnibox_bg,
                radius: tab_h * 0.5,
            });
        }
        bar_children.push(SceneNode::Text {
            rect: Rect::new(tab_x + TAB_PAD_X, tab_ink_y, ink_w.ceil() + 2.0, tab_ink_h),
            text: label.to_string(),
            size_px: TAB_PX,
            color: if i == ACTIVE_TAB {
                theme.bar_ink
            } else {
                theme.omnibox_ink
            },
            align: TextAlign::Left,
        });
        tab_x += slot + TAB_GAP;
    }

    bar_children.push(SceneNode::Rect {
        rect: pill,
        color: theme.omnibox_bg,
        radius: (TOP_BAR_H - 12.0) * 0.5,
    });
    // Inside the pill, the shell's own three parts: a search glyph, the prompt, and the
    // shortcut hint pushed to the far end. The hint is right-anchored, which is the
    // measure again; before it there was nowhere to put it.
    let pill_ink_y = (TOP_BAR_H - OMNIBOX_PX * 1.3) * 0.5;
    let mut pill_x = pill.x + 12.0;
    if icons_available {
        let gw = measure.text_width(OMNIBOX_GLYPH, OMNIBOX_PX);
        bar_children.push(SceneNode::Text {
            rect: Rect::new(pill_x, pill_ink_y, gw.ceil() + 2.0, OMNIBOX_PX * 1.3),
            text: OMNIBOX_GLYPH.to_string(),
            size_px: OMNIBOX_PX,
            color: theme.omnibox_ink,
            align: TextAlign::Left,
        });
        pill_x += gw + 8.0;
    }
    bar_children.push(SceneNode::Text {
        rect: Rect::new(
            pill_x,
            pill_ink_y,
            (pill.right() - 12.0 - pill_x).max(0.0),
            OMNIBOX_PX * 1.3,
        ),
        text: "Ask or search anything".to_string(),
        size_px: 14.0,
        color: theme.omnibox_ink,
        align: TextAlign::Left,
    });
    let kbd_w = measure.text_width(OMNIBOX_KBD, KBD_PX);
    let kbd_x = pill.right() - 12.0 - kbd_w;
    if kbd_x > pill_x {
        bar_children.push(SceneNode::Text {
            rect: Rect::new(
                kbd_x,
                (TOP_BAR_H - KBD_PX * 1.3) * 0.5,
                kbd_w.ceil() + 2.0,
                KBD_PX * 1.3,
            ),
            text: OMNIBOX_KBD.to_string(),
            size_px: KBD_PX,
            color: theme.omnibox_ink,
            align: TextAlign::Left,
        });
    }

    // ── The bar's right cluster, laid out from the RIGHT EDGE inward so it stays put as
    //    the output widens: tray glyphs, then the avatar, then the orb-sm, which is the
    //    shell's order read right to left. The clock sits outermost in the shell and is
    //    absent here: it needs a time source the scene has no input for, and reserving a
    //    slot for something that never draws would leave a hole in the cluster.
    let mut right_x = output_w - EDGE_PAD;
    if icons_available {
        for glyph in TRAY_GLYPHS.iter().rev() {
            right_x -= TRAY_BTN;
            let b = centered_box(
                measure.text_width(glyph, TRAY_PX),
                right_x,
                TRAY_BTN,
                (TOP_BAR_H - TRAY_PX * 1.3) * 0.5,
                TRAY_PX * 1.3,
            );
            bar_children.push(SceneNode::Text {
                rect: b,
                text: (*glyph).to_string(),
                size_px: TRAY_PX,
                color: theme.omnibox_ink,
                align: TextAlign::Left,
            });
            right_x -= TRAY_GAP;
        }
    }
    // Avatar: a filled disc with the account initial. The shell hardcodes the letter in
    // its markup, so this is the same letter, not a guess at a user's name.
    right_x -= AVATAR_D;
    let av = Rect::new(right_x, (TOP_BAR_H - AVATAR_D) * 0.5, AVATAR_D, AVATAR_D);
    bar_children.push(SceneNode::Rect {
        rect: av,
        color: theme.omnibox_bg,
        radius: AVATAR_D * 0.5,
    });
    bar_children.push(SceneNode::Text {
        rect: centered_box(
            measure.text_width(AVATAR_INITIAL, AVATAR_PX),
            av.x,
            AVATAR_D,
            (TOP_BAR_H - AVATAR_PX * 1.3) * 0.5,
            AVATAR_PX * 1.3,
        ),
        text: AVATAR_INITIAL.to_string(),
        size_px: AVATAR_PX,
        color: theme.bar_ink,
        align: TextAlign::Left,
    });
    right_x -= TRAY_GAP;

    right_x -= ORB_SM;
    let orb_sm_rect = Rect::new(right_x, (TOP_BAR_H - ORB_SM) * 0.5, ORB_SM, ORB_SM);
    bar_children.push(SceneNode::OrbSlot {
        rect: orb_sm_rect,
        compact: true,
    });
    root.push(SceneNode::Container {
        rect: bar,
        interactive: false,
        children: bar_children,
    });

    // ── Content band, between the two fixed strips. ──
    let content = Rect::new(
        EDGE_PAD,
        TOP_BAR_H + EDGE_PAD,
        (output_w - 2.0 * EDGE_PAD).max(0.0),
        (output_h - TOP_BAR_H - TASKBAR_H - 2.0 * EDGE_PAD).max(0.0),
    );

    // ── Hero (P4, the EARNINGS hero): eyebrow, the big Spark figure with its unit, an
    //    honest meta strip, then the two calls to action. The large orb floats right (c7).
    //    Laid out top down from the content band, each part skipped when the payload has
    //    nothing for it, so a hero without an amount is short rather than gappy.
    let orb_home = HERO_H.min(content.h).max(0.0);
    let hero_text_w = (content.w - orb_home - EDGE_PAD).max(0.0);
    let mut hero_y = content.y;
    if !home.hero.eyebrow.is_empty() {
        root.push(SceneNode::Text {
            rect: Rect::new(content.x, hero_y, hero_text_w, HERO_EYEBROW_PX * 1.4),
            text: home.hero.eyebrow.clone(),
            size_px: HERO_EYEBROW_PX,
            color: theme.hero_copy,
            align: TextAlign::Left,
        });
        hero_y += HERO_EYEBROW_PX * 1.6;
    }
    if let Some(amount) = home.hero.amount {
        // The number and its unit are two runs on one line, the unit sitting right after
        // the figure ends. That is the measure again: the figure's width is not known
        // until it is shaped, and it changes with the balance.
        let figure = amount.to_string();
        let fw = measure.text_width(&figure, HERO_AMOUNT_PX);
        root.push(SceneNode::Text {
            rect: Rect::new(content.x, hero_y, fw.ceil() + 2.0, HERO_AMOUNT_PX * 1.3),
            text: figure,
            size_px: HERO_AMOUNT_PX,
            color: theme.hero_title,
            align: TextAlign::Left,
        });
        let unit = if home.hero.amount_unit.is_empty() {
            HERO_UNIT_FALLBACK
        } else {
            &home.hero.amount_unit
        };
        let uw = measure.text_width(unit, HERO_UNIT_PX);
        root.push(SceneNode::Text {
            rect: Rect::new(
                content.x + fw + 8.0,
                // Sat on the figure's baseline rather than its box top, so the unit reads
                // as part of the number instead of floating above it.
                hero_y + (HERO_AMOUNT_PX - HERO_UNIT_PX) * 0.9,
                uw.ceil() + 2.0,
                HERO_UNIT_PX * 1.3,
            ),
            text: unit.to_string(),
            size_px: HERO_UNIT_PX,
            color: theme.accent,
            align: TextAlign::Left,
        });
        hero_y += HERO_AMOUNT_PX * 1.35;
    }
    // The meta strip: a payout pill, then the agents/tasks stat. Built as ONE run rather
    // than several, because the shell writes it as one sentence with separators and
    // splitting it would need per-fragment spacing the scene has no reason to own.
    let mut strip: Vec<String> = Vec::new();
    if home.hero.payout_pending {
        strip.push("Payout pending".to_string());
    }
    if home.hero.agents != 0 || home.hero.tasks != 0 {
        fn plural<'a>(n: i64, one: &'a str, many: &'a str) -> &'a str {
            if n == 1 {
                one
            } else {
                many
            }
        }
        strip.push(format!(
            "{} {} · {} {}",
            home.hero.agents,
            plural(home.hero.agents, "agent", "agents"),
            home.hero.tasks,
            plural(home.hero.tasks, "task", "tasks"),
        ));
        if home.hero.local {
            strip.push("fully local".to_string());
        }
    }
    if !strip.is_empty() {
        root.push(SceneNode::Text {
            rect: Rect::new(content.x, hero_y, hero_text_w, HERO_META_PX * 1.4),
            text: strip.join(" · "),
            size_px: HERO_META_PX,
            color: theme.hero_copy,
            align: TextAlign::Left,
        });
        hero_y += HERO_META_PX * 1.9;
    }
    // Calls to action. Labels only: nothing routes a hero action natively yet, so these
    // draw as the shell's two buttons but are not hover targets, the same call made for
    // the nav tabs.
    let mut cta_x = content.x;
    for (label, primary) in [(&home.hero.primary, true), (&home.hero.secondary, false)] {
        let Some(label) = label else { continue };
        let lw = measure.text_width(label, HERO_BTN_PX);
        let bw = lw.ceil() + 2.0 * HERO_BTN_PAD_X;
        if cta_x + bw > content.x + hero_text_w {
            break;
        }
        root.push(SceneNode::Rect {
            rect: Rect::new(cta_x, hero_y, bw, HERO_BTN_H),
            color: if primary { theme.accent } else { theme.omnibox_bg },
            radius: HERO_BTN_H * 0.5,
        });
        root.push(SceneNode::Text {
            rect: Rect::new(
                cta_x + HERO_BTN_PAD_X,
                hero_y + (HERO_BTN_H - HERO_BTN_PX * 1.3) * 0.5,
                lw.ceil() + 2.0,
                HERO_BTN_PX * 1.3,
            ),
            text: label.clone(),
            size_px: HERO_BTN_PX,
            color: if primary {
                theme.on_accent_ink
            } else {
                theme.card_ink
            },
            align: TextAlign::Left,
        });
        cta_x += bw + 10.0;
    }
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
        // ── Row header: label, an optional note beside it, and an optional right-anchored
        //    "See all". The label keeps a measured box now rather than the whole row width,
        //    so the note can sit AFTER it; right-anchoring the See-all is the third thing
        //    the measure buys, and it is the reason TextAlign::Right was never needed:
        //    knowing the width lets layout place a left-aligned run exactly.
        let label_w = measure.text_width(&row.title, ROW_LABEL_PX);
        root.push(SceneNode::Text {
            rect: Rect::new(content.x, cursor_y, label_w.ceil() + 2.0, ROW_LABEL_H),
            text: row.title.clone(),
            size_px: ROW_LABEL_PX,
            color: theme.card_ink,
            align: TextAlign::Left,
        });
        if let Some(note) = &row.note {
            let note_w = measure.text_width(note, ROW_NOTE_PX);
            let note_x = content.x + label_w + ROW_HEAD_GAP;
            if note_x + note_w <= content.right() {
                root.push(SceneNode::Text {
                    rect: Rect::new(note_x, cursor_y, note_w.ceil() + 2.0, ROW_LABEL_H),
                    text: note.clone(),
                    size_px: ROW_NOTE_PX,
                    color: theme.hero_copy,
                    align: TextAlign::Left,
                });
            }
        }
        if row.see_all.is_some() {
            let see_w = measure.text_width(SEE_ALL, ROW_NOTE_PX);
            let see_x = content.right() - see_w;
            // Only if it clears the label (and any note): a cramped row drops the
            // affordance rather than overlapping the text it belongs to.
            if see_x > content.x + label_w + ROW_HEAD_GAP {
                root.push(SceneNode::Text {
                    rect: Rect::new(see_x, cursor_y, see_w.ceil() + 2.0, ROW_LABEL_H),
                    text: SEE_ALL.to_string(),
                    size_px: ROW_NOTE_PX,
                    color: theme.accent,
                    align: TextAlign::Left,
                });
            }
        }
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
            // ── Icon glyph, top left, and ONLY when the card has no art: the shell draws
            //    it as `card.icon && !hasImage`, because the glyph is the stand-in FOR the
            //    missing picture, not a decoration beside one. It is a ligature name in a
            //    Material face, so it goes down the ordinary text path; `icons_available`
            //    is what stops it rendering as the literal word when the face is absent.
            if let (Some(name), None, true) = (&card.icon, &card.image, icons_available) {
                card_children.push(SceneNode::Rect {
                    rect: Rect::new(cr.x + 14.0, cr.y + 12.0, CARD_ICON_BOX, CARD_ICON_BOX),
                    color: theme.chip_bg,
                    radius: 10.0,
                });
                card_children.push(SceneNode::Text {
                    rect: centered_box(
                        measure.text_width(name, CARD_ICON_PX),
                        cr.x + 14.0,
                        CARD_ICON_BOX,
                        cr.y + 12.0 + (CARD_ICON_BOX - CARD_ICON_PX * 1.3) * 0.5,
                        CARD_ICON_PX * 1.3,
                    ),
                    text: name.clone(),
                    size_px: CARD_ICON_PX,
                    color: theme.card_ink,
                    align: TextAlign::Left,
                });
            }

            // ── Badge / live tag, top right. The shell draws LIVE **or** badge, never
            //    both (hartHome.js: `if (card.live) ... else if (card.badge)`), because a
            //    running agent supersedes whatever the card was otherwise labelled. Same
            //    inset for either, so a card never shifts its chip when it goes live.
            let chip = card
                .live
                .as_ref()
                .map(|t| (t, true))
                .or_else(|| card.badge.as_ref().map(|t| (t, false)));
            if let Some((label, is_live)) = chip {
                let ink_w = measure.text_width(label, CARD_CHIP_PX);
                let dot_w = if is_live {
                    CARD_LIVE_DOT + CARD_LIVE_GAP
                } else {
                    0.0
                };
                let chip_w = ink_w.ceil() + 2.0 * CARD_CHIP_PAD_X + dot_w;
                let chip_x = cr.right() - CARD_CHIP_INSET - chip_w;
                let chip_y = cr.y + CARD_CHIP_INSET;
                // A live tag is a pill on a dark ground; a badge is a filled accent
                // block with dark ink on it.
                card_children.push(SceneNode::Rect {
                    rect: Rect::new(chip_x, chip_y, chip_w, CARD_CHIP_H),
                    color: if is_live { theme.chip_bg } else { theme.accent },
                    radius: if is_live { CARD_CHIP_H * 0.5 } else { 8.0 },
                });
                let mut ink_x = chip_x + CARD_CHIP_PAD_X;
                if is_live {
                    card_children.push(SceneNode::Rect {
                        rect: Rect::new(
                            ink_x,
                            chip_y + (CARD_CHIP_H - CARD_LIVE_DOT) * 0.5,
                            CARD_LIVE_DOT,
                            CARD_LIVE_DOT,
                        ),
                        color: theme.live_dot,
                        radius: CARD_LIVE_DOT * 0.5,
                    });
                    ink_x += CARD_LIVE_DOT + CARD_LIVE_GAP;
                }
                card_children.push(SceneNode::Text {
                    rect: Rect::new(
                        ink_x,
                        chip_y + (CARD_CHIP_H - CARD_CHIP_PX * 1.3) * 0.5,
                        ink_w.ceil() + 2.0,
                        CARD_CHIP_PX * 1.3,
                    ),
                    text: label.clone(),
                    size_px: CARD_CHIP_PX,
                    color: if is_live {
                        theme.card_ink
                    } else {
                        theme.on_accent_ink
                    },
                    align: TextAlign::Left,
                });
            }

            // Title, then the meta line under it, then the progress bar pinned to the
            // card's bottom edge: the shell's own body order (hh-card-title, hh-card-meta,
            // hh-card-prog). The title sits a line higher when there is a meta to carry,
            // so the pair stays inside the card rather than the meta hanging off it.
            let has_meta = card.meta.is_some();
            let title_y = if has_meta {
                cr.bottom() - 34.0 - CARD_META_H
            } else {
                cr.bottom() - 34.0
            };
            card_children.push(SceneNode::Text {
                rect: Rect::new(cr.x + 12.0, title_y, cr.w - 24.0, 22.0),
                text: card.title.clone(),
                size_px: 14.0,
                color: theme.card_ink,
                align: TextAlign::Left,
            });
            if let Some(meta) = &card.meta {
                card_children.push(SceneNode::Text {
                    rect: Rect::new(cr.x + 12.0, title_y + 22.0, cr.w - 24.0, CARD_META_H),
                    text: meta.clone(),
                    size_px: 12.0,
                    color: theme.hero_copy,
                    align: TextAlign::Left,
                });
            }
            if let Some(p) = card.progress {
                // A completion bar, so ZERO must read as an empty track rather than as no
                // bar at all: a card at 0% and a card with no progress are different
                // states, and collapsing them would silently lose one.
                card_children.push(SceneNode::Rect {
                    rect: Rect::new(cr.x, cr.bottom() - CARD_PROG_H, cr.w, CARD_PROG_H),
                    color: theme.omnibox_bg,
                    radius: 0.0,
                });
                let filled = cr.w * p.clamp(0.0, 1.0);
                if filled >= 1.0 {
                    card_children.push(SceneNode::Rect {
                        rect: Rect::new(cr.x, cr.bottom() - CARD_PROG_H, filled, CARD_PROG_H),
                        color: theme.accent,
                        radius: 0.0,
                    });
                }
            }
            root.push(SceneNode::Container {
                rect: cr,
                // A card is the one thing on this desktop the cursor reacts to, and the
                // background rect pushed FIRST above is what the hover lifts.
                interactive: true,
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
        interactive: false,
        children: root,
    }
}

/// The demo home as a process-wide singleton, so the render path can fall back to it
/// by REFERENCE instead of building (or cloning) one per frame. `HomeCompose::demo` is
/// deterministic and never mutated, which is exactly what makes a `OnceLock` sound here.
/// This is what lets `render_native_scene` hand `lower_scene` a borrowed home in the
/// no-compose case without an allocation.
pub fn demo_ref() -> &'static HomeCompose {
    static DEMO: std::sync::OnceLock<HomeCompose> = std::sync::OnceLock::new();
    DEMO.get_or_init(HomeCompose::demo)
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
    /// `measure` is only ever consulted on a REBUILD, which is the point of retaining the
    /// tree: shaping the bar's runs is not something a steady desktop should pay for.
    pub fn tree_for(
        &mut self,
        w: f32,
        h: f32,
        home: &HomeCompose,
        theme: &Theme,
        measure: &mut dyn TextMeasure,
    ) -> &SceneNode {
        let stale = self.tree.is_none()
            || self.key_w != w
            || self.key_h != h
            || self.key_theme != Some(*theme)
            || self.key_home != *home;
        if stale {
            self.tree = Some(layout_home(w, h, home, theme, measure));
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

    // The keys BOTH producers emit (`_home_sanitize_hero` and the backbone builder), not
    // the `{title, copy}` this used to read, which neither has ever sent.
    let hero = match v.get("hero") {
        Some(h) => Hero {
            eyebrow: s(h.get("eyebrow")),
            amount: h.get("amount").and_then(Value::as_i64),
            amount_unit: s(h.get("amount_unit")),
            agents: h.get("agents").and_then(Value::as_i64).unwrap_or(0),
            tasks: h.get("tasks").and_then(Value::as_i64).unwrap_or(0),
            local: h.get("local").and_then(Value::as_bool).unwrap_or(false),
            payout_pending: h
                .get("payout_pending")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            // Only the LABEL: the action and target beside it in the payload drive
            // behaviour the native scene does not route yet.
            primary: h
                .get("primary")
                .and_then(|p| p.get("label"))
                .and_then(Value::as_str)
                .filter(|t| !t.is_empty())
                .map(str::to_string),
            secondary: h
                .get("secondary")
                .and_then(|p| p.get("label"))
                .and_then(Value::as_str)
                .filter(|t| !t.is_empty())
                .map(str::to_string),
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
                        meta: c
                            .get("meta")
                            .and_then(Value::as_str)
                            .filter(|t| !t.is_empty())
                            .map(str::to_string),
                        // Out-of-range is DROPPED, not clamped: the sanitizer only ever
                        // emits 0..=1, so a value outside it means the payload did not
                        // come from there and guessing at intent would draw a confident
                        // wrong bar.
                        progress: c
                            .get("progress")
                            .and_then(Value::as_f64)
                            .filter(|p| (0.0..=1.0).contains(p))
                            .map(|p| p as f32),
                        icon: c
                            .get("icon")
                            .and_then(Value::as_str)
                            .filter(|t| !t.is_empty())
                            .map(str::to_string),
                        badge: c
                            .get("badge")
                            .and_then(Value::as_str)
                            .filter(|t| !t.is_empty())
                            .map(str::to_string),
                        live: c
                            .get("live")
                            .and_then(Value::as_str)
                            .filter(|t| !t.is_empty())
                            .map(str::to_string),
                        image: c.get("image").and_then(Value::as_str).map(str::to_string),
                    });
                }
            }
            rows.push(Row {
                // `title`, the key both producers emit; `label` was never sent.
                title: s(r.get("title")),
                // Both are already in the payload the HTML shell reads (hartHome.js
                // row.note / row.see_all); the native scene was simply dropping them.
                // An empty string is treated as absent, so a blank field cannot produce
                // a See-all that opens nothing.
                note: r
                    .get("note")
                    .and_then(Value::as_str)
                    .filter(|t| !t.is_empty())
                    .map(str::to_string),
                see_all: r
                    .get("see_all")
                    .and_then(Value::as_str)
                    .filter(|t| !t.is_empty())
                    .map(str::to_string),
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
                eyebrow: "Earned on the hive".into(),
                amount: Some(12),
                amount_unit: "Spark".into(),
                agents: 3,
                tasks: 41,
                local: true,
                payout_pending: true,
                primary: Some("Resume".into()),
                secondary: Some("Ask anything".into()),
            },
            rows: vec![
                Row {
                    title: "Continue".into(),
                    note: Some("3 in progress".into()),
                    see_all: Some("panel:continue".into()),
                    cards: vec![
                        Card {
                            title: "Recipe A".into(),
                            meta: Some("2 min left".into()),
                            progress: Some(0.6),
                            icon: Some("storage".into()),
                            badge: Some("NEW".into()),
                            live: None,
                            image: None,
                        },
                        Card {
                            title: "Recipe B".into(),
                            meta: None,
                            progress: None,
                            icon: None,
                            badge: None,
                            live: None,
                            image: Some("b.png".into()),
                        },
                    ],
                },
                Row {
                    title: "For you".into(),
                    note: None,
                    see_all: None,
                    cards: vec![Card::default()],
                },
            ],
            mood: Some("cosmic".into()),
        }
    }

    #[test]
    fn top_bar_is_the_fixed_40px_strip_at_the_top() {
        let root = layout_home(1600.0, 900.0, &sample(), &Theme::cosmic_default(), &mut MonoMeasure);
        let bar = root.hit_test(800.0, 5.0).expect("a node at the top strip");
        // The topmost hit in the bar band is a bar child, and the bar rect is 40px.
        assert!(bar.rect().y < TOP_BAR_H);
    }

    #[test]
    fn taskbar_is_the_fixed_44px_strip_at_the_bottom() {
        let (w, h) = (1600.0, 900.0);
        let root = layout_home(w, h, &sample(), &Theme::cosmic_default(), &mut MonoMeasure);
        let hit = root.hit_test(w * 0.5, h - 2.0).expect("a node at the bottom strip");
        assert!((hit.rect().h - TASKBAR_H).abs() < 0.01);
        assert!((hit.rect().y - (h - TASKBAR_H)).abs() < 0.01);
    }

    #[test]
    fn home_orb_floats_to_the_right_of_the_hero() {
        let (w, h) = (1600.0, 900.0);
        let root = layout_home(w, h, &sample(), &Theme::cosmic_default(), &mut MonoMeasure);
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
        let root = layout_home(1600.0, 320.0, &sample(), &Theme::cosmic_default(), &mut MonoMeasure);
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
        let root = layout_home(1600.0, 900.0, &sample(), &Theme::cosmic_default(), &mut MonoMeasure);
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
    fn the_leaf_walk_and_the_hover_index_share_one_index_space() {
        // The card highlight works by comparing `hover_leaf`'s index against the index the
        // lowering's walk hands it. Those are two different traversals, so if they ever
        // disagreed the wrong node would light up, and nothing else would catch it. This
        // pins them together.
        let root = layout_home(1600.0, 900.0, &sample(), &Theme::cosmic_default(), &mut MonoMeasure);

        let mut walked: Vec<(usize, Rect)> = Vec::new();
        root.for_each_leaf(&mut |i, leaf| walked.push((i, leaf.rect())));
        // Indices are contiguous from zero, in paint order.
        for (n, (i, _)) in walked.iter().enumerate() {
            assert_eq!(*i, n);
        }
        // The same leaves, in the same order, as the collector built on top of it.
        let mut flat = Vec::new();
        root.flatten(&mut flat);
        assert_eq!(flat.len(), walked.len());
        for (n, leaf) in flat.iter().enumerate() {
            assert_eq!(leaf.rect(), walked[n].1);
        }
        // And a card's hover index addresses that card's own background in THAT space.
        let mut card = None;
        if let SceneNode::Container { children, .. } = &root {
            for c in children {
                if let SceneNode::Container {
                    rect,
                    interactive: true,
                    ..
                } = c
                {
                    card = Some(*rect);
                    break;
                }
            }
        }
        let cr = card.expect("a card group");
        let idx = root
            .hover_leaf(Some((cr.x + cr.w * 0.5, cr.y + cr.h * 0.5)))
            .expect("a card is a hover target");
        assert_eq!(walked[idx].1, cr);
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
        let v = serde_json::json!({ "hero": { "eyebrow": "hi" }, "rows": "not-an-array" });
        let hc = decode_home_compose(&v);
        assert_eq!(hc.hero.eyebrow, "hi");
        // Absent keys are absent, never invented: no amount means the producers had no
        // positive balance to lead with, and a zero would be a different claim.
        assert_eq!(hc.hero.amount, None);
        assert_eq!(hc.hero.amount_unit, "");
        assert_eq!(hc.hero.agents, 0);
        assert!(!hc.hero.payout_pending);
        assert_eq!(hc.hero.primary, None);
        assert!(hc.rows.is_empty());
        assert!(hc.mood.is_none());
    }

    #[test]
    fn decode_reads_the_hero_shape_both_producers_actually_emit() {
        // Copied from _home_sanitize_hero's own output, so this fails if the native
        // decoder drifts from the payload again rather than only when someone notices
        // the hero is blank on the box.
        let v = serde_json::json!({
            "hero": {
                "eyebrow": "Earned on the hive",
                "amount": 1284, "amount_unit": "Spark",
                "local": true, "payout_pending": true,
                "primary": {"label": "Resume", "action": "resume", "target": "recipes"},
                "secondary": {"label": "Ask anything", "action": "ask"},
                "agents": 3, "tasks": 41
            },
            "rows": [{"title": "Continue", "cards": [{"title": "A"}]}]
        });
        let hc = decode_home_compose(&v);
        assert_eq!(hc.hero.eyebrow, "Earned on the hive");
        assert_eq!(hc.hero.amount, Some(1284));
        assert_eq!(hc.hero.amount_unit, "Spark");
        assert_eq!((hc.hero.agents, hc.hero.tasks), (3, 41));
        assert!(hc.hero.local && hc.hero.payout_pending);
        // The LABEL out of the action object, not the object itself.
        assert_eq!(hc.hero.primary.as_deref(), Some("Resume"));
        assert_eq!(hc.hero.secondary.as_deref(), Some("Ask anything"));
        assert_eq!(hc.rows[0].title, "Continue");
    }

    #[test]
    fn the_hero_lays_out_its_number_unit_stat_and_actions() {
        let root = layout_home(1600.0, 900.0, &sample(), &Theme::cosmic_default(), &mut MonoMeasure);
        let mut runs: Vec<(String, Rect)> = Vec::new();
        root.for_each_leaf(&mut |_, leaf| {
            if let SceneNode::Text { rect, text, .. } = leaf {
                runs.push((text.clone(), *rect));
            }
        });
        let find = |t: &str| runs.iter().find(|(s, _)| s == t).map(|(_, r)| *r);

        let eyebrow = find("Earned on the hive").expect("the eyebrow");
        let amount = find("12").expect("the amount figure");
        let unit = find("Spark").expect("the unit beside it");
        assert!(amount.y > eyebrow.y, "the figure sits under the eyebrow");
        assert!(unit.x > amount.right() - 1.0, "the unit follows the figure");
        assert!(
            unit.y > amount.y,
            "the unit sits on the figure's baseline, not its box top"
        );

        // The stat line is ONE run reading as a sentence, with the pill folded in.
        let stat = runs
            .iter()
            .find(|(s, _)| s.contains("3 agents") && s.contains("41 tasks"))
            .expect("the agents/tasks stat");
        assert!(stat.0.contains("Payout pending"), "the payout pill leads the strip");
        assert!(stat.0.contains("fully local"), "and the local claim closes it");
        assert!(stat.1.y > amount.y, "the strip is under the number");

        // Both actions draw, primary first.
        let p = find("Resume").expect("the primary action");
        let s = find("Ask anything").expect("the secondary action");
        assert!(s.x > p.x, "primary leads");
        assert!(p.y > stat.1.y, "the actions close the hero");
    }

    #[test]
    fn a_hero_with_no_amount_is_short_rather_than_gappy() {
        // The producers omit the hero entirely when there is no positive balance, but a
        // partial payload must still lay out: each part is skipped, not left as a hole.
        let mut hc = sample();
        hc.rows[0].cards.clear();
        hc.hero = Hero {
            eyebrow: "Nothing yet".into(),
            ..Hero::default()
        };
        let root = layout_home(1600.0, 900.0, &hc, &Theme::cosmic_default(), &mut MonoMeasure);
        let mut texts = Vec::new();
        root.for_each_leaf(&mut |_, leaf| {
            if let SceneNode::Text { text, .. } = leaf {
                texts.push(text.clone());
            }
        });
        assert!(texts.contains(&"Nothing yet".to_string()));
        assert!(!texts.iter().any(|t| t.contains("agents")), "no stat without agents");
        assert!(!texts.contains(&"Spark".to_string()), "no unit without a figure");
        assert!(!texts.contains(&"Resume".to_string()), "no action without a label");
    }

    #[test]
    fn decode_reads_rows_and_cards() {
        let v = serde_json::json!({
            "rows": [{ "title": "Continue", "cards": [{ "title": "A", "image": "a.png" }] }],
            "mood": "cosmic"
        });
        let hc = decode_home_compose(&v);
        assert_eq!(hc.rows.len(), 1);
        assert_eq!(hc.rows[0].title, "Continue");
        assert_eq!(hc.rows[0].cards[0].image.as_deref(), Some("a.png"));
        assert_eq!(hc.mood.as_deref(), Some("cosmic"));
    }

    #[test]
    fn the_wordmark_butts_its_two_runs_together_using_the_measure() {
        let root = layout_home(1600.0, 900.0, &sample(), &Theme::cosmic_default(), &mut MonoMeasure);
        let theme = Theme::cosmic_default();
        // Both runs live in the top bar, in reading order.
        let mut hart = None;
        let mut os = None;
        if let SceneNode::Container { children, .. } = &root {
            for c in children {
                if let SceneNode::Container { rect, children, .. } = c {
                    if rect.y != 0.0 {
                        continue;
                    }
                    for n in children {
                        if let SceneNode::Text { rect, text, .. } = n {
                            if text == "HART" {
                                hart = Some(*rect);
                            }
                            if text == "OS" {
                                os = Some(*rect);
                            }
                        }
                    }
                }
            }
        }
        let (h, o) = (hart.expect("a HART run"), os.expect("an OS run"));
        // OS begins after HART ends, by about one space, which is the whole point of
        // having a measure: without one the second run has nowhere to start.
        let space = MonoMeasure.text_width(" ", WORDMARK_PX);
        let gap = o.x - (h.x + MonoMeasure.text_width("HART", WORDMARK_PX));
        assert!(
            (gap - space).abs() < 0.51,
            "OS should sit one space past HART, gap was {gap} against a {space} space"
        );
        // Each box is wide enough to hold the run it will be asked to shape into.
        assert!(h.w >= MonoMeasure.text_width("HART", WORDMARK_PX));
        assert!(o.w >= MonoMeasure.text_width("OS", WORDMARK_PX));
        // The two-tone treatment: the runs carry the two BRAND hues, not the bar ink.
        let mut colors = Vec::new();
        if let SceneNode::Container { children, .. } = &root {
            for c in children {
                if let SceneNode::Container { rect, children, .. } = c {
                    if rect.y != 0.0 {
                        continue;
                    }
                    for n in children {
                        if let SceneNode::Text { text, color, .. } = n {
                            if text == "HART" || text == "OS" {
                                colors.push(*color);
                            }
                        }
                    }
                }
            }
        }
        assert_eq!(colors, vec![theme.accent, theme.accent2]);
        // Both sit inside the fixed 40px strip.
        assert!(h.y >= 0.0 && h.bottom() <= TOP_BAR_H + 0.01);
    }

    /// Every Text run inside the fixed top bar, in paint order.
    fn bar_runs(root: &SceneNode) -> Vec<(String, Rect)> {
        let mut out = Vec::new();
        if let SceneNode::Container { children, .. } = root {
            for c in children {
                if let SceneNode::Container { rect, children, .. } = c {
                    if rect.y != 0.0 || rect.h != TOP_BAR_H {
                        continue;
                    }
                    for n in children {
                        if let SceneNode::Text { rect, text, .. } = n {
                            out.push((text.clone(), *rect));
                        }
                    }
                }
            }
        }
        out
    }

    #[test]
    fn the_nav_tabs_are_laid_out_left_to_right_each_sized_to_its_own_label() {
        let root = layout_home(1600.0, 900.0, &sample(), &Theme::cosmic_default(), &mut MonoMeasure);
        let runs = bar_runs(&root);
        let tabs: Vec<_> = runs
            .iter()
            .filter(|(t, _)| NAV_TABS.contains(&t.as_str()))
            .collect();
        assert_eq!(tabs.len(), NAV_TABS.len(), "a wide bar fits every tab");
        // In the shell's order, strictly left to right, never overlapping.
        for w in tabs.windows(2) {
            assert!(
                w[1].1.x > w[0].1.right(),
                "{} must start after {} ends",
                w[1].0,
                w[0].0
            );
        }
        for (i, (label, _)) in tabs.iter().enumerate() {
            assert_eq!(label.as_str(), NAV_TABS[i]);
        }
        // Sized to the LABEL, not a uniform slot: Agents is wider than Apps.
        let width_of = |name: &str| tabs.iter().find(|(t, _)| t == name).unwrap().1.w;
        assert!(width_of("Agents") > width_of("Apps"));
        // They stop short of the omnibox pill and stay inside the strip.
        let pill_x = (1600.0 - OMNIBOX_W) * 0.5;
        for (label, r) in &tabs {
            assert!(r.right() <= pill_x, "{label} runs under the omnibox");
            assert!(r.y >= 0.0 && r.bottom() <= TOP_BAR_H + 0.01);
        }
        // They begin after the wordmark rather than on top of it.
        let os = runs.iter().find(|(t, _)| t == "OS").expect("an OS run");
        assert!(tabs[0].1.x > os.1.x);
    }

    #[test]
    fn a_narrow_bar_drops_tabs_instead_of_drawing_them_under_the_omnibox() {
        // The same discipline the card rows use: emit only what fits. A bar barely wider
        // than its own pill has no room for tabs at all, and must show none rather than
        // overlap.
        let root = layout_home(
            OMNIBOX_W + 120.0,
            900.0,
            &sample(),
            &Theme::cosmic_default(),
            &mut MonoMeasure,
        );
        let runs = bar_runs(&root);
        let pill_x = ((OMNIBOX_W + 120.0) - OMNIBOX_W) * 0.5;
        for (label, r) in &runs {
            if NAV_TABS.contains(&label.as_str()) {
                assert!(r.right() <= pill_x, "{label} overlapped the omnibox");
            }
        }
        let shown = runs
            .iter()
            .filter(|(t, _)| NAV_TABS.contains(&t.as_str()))
            .count();
        assert!(shown < NAV_TABS.len(), "a narrow bar must drop tabs");
    }

    #[test]
    fn a_row_header_carries_its_note_and_a_right_anchored_see_all() {
        let (w, h) = (1600.0, 900.0);
        let root = layout_home(w, h, &sample(), &Theme::cosmic_default(), &mut MonoMeasure);
        let mut runs: Vec<(String, Rect)> = Vec::new();
        root.for_each_leaf(&mut |_, leaf| {
            if let SceneNode::Text { rect, text, .. } = leaf {
                runs.push((text.clone(), *rect));
            }
        });
        let find = |t: &str| runs.iter().find(|(s, _)| s == t).map(|(_, r)| *r);

        let label = find("Continue").expect("the row label");
        let note = find("3 in progress").expect("the row note");
        let see = find(SEE_ALL).expect("a see-all on the row that carries a target");
        // The note sits AFTER the label on the same header line, not on top of it. That
        // is only expressible because the label's box is measured rather than the full
        // row width, which is what it used to be.
        assert!(note.x > label.right(), "the note must follow the label");
        assert_eq!(note.y, label.y, "note and label share the header line");
        // See-all is anchored to the right edge of the content band.
        let content_right = w - EDGE_PAD;
        assert!(
            (see.right() - content_right).abs() < 3.0,
            "see-all should sit at the right edge, ended at {} against {}",
            see.right(),
            content_right
        );
        assert!(see.x > note.right(), "see-all must clear the header text");
        assert_eq!(see.y, label.y);

        // Exactly one See-all: the second row carries no target, so it must draw none.
        assert_eq!(runs.iter().filter(|(s, _)| s == SEE_ALL).count(), 1);
        assert!(find("For you").is_some(), "the second row still has its label");
    }

    #[test]
    fn a_row_with_no_see_all_target_draws_no_affordance() {
        // A dead control is worse than none: the shell only draws See-all when the
        // payload names something for it to open.
        let mut hc = sample();
        for r in hc.rows.iter_mut() {
            r.see_all = None;
            r.note = None;
        }
        let root = layout_home(1600.0, 900.0, &hc, &Theme::cosmic_default(), &mut MonoMeasure);
        let mut seen = 0;
        root.for_each_leaf(&mut |_, leaf| {
            if let SceneNode::Text { text, .. } = leaf {
                if text == SEE_ALL {
                    seen += 1;
                }
            }
        });
        assert_eq!(seen, 0);
    }

    #[test]
    fn a_card_carries_its_meta_line_and_its_progress_bar() {
        let root = layout_home(1600.0, 900.0, &sample(), &Theme::cosmic_default(), &mut MonoMeasure);
        // The first card in the sample has both; the second has neither.
        let mut cards: Vec<Vec<SceneNode>> = Vec::new();
        if let SceneNode::Container { children, .. } = &root {
            for c in children {
                if let SceneNode::Container {
                    interactive: true,
                    children,
                    ..
                } = c
                {
                    cards.push(children.clone());
                }
            }
        }
        assert!(cards.len() >= 2, "the sample lays out at least two cards");

        let texts = |ch: &Vec<SceneNode>| -> Vec<String> {
            ch.iter()
                .filter_map(|n| match n {
                    SceneNode::Text { text, .. } => Some(text.clone()),
                    _ => None,
                })
                .collect()
        };
        assert!(texts(&cards[0]).contains(&"2 min left".to_string()), "the meta line draws");
        assert!(texts(&cards[1]).is_empty() || !texts(&cards[1]).contains(&"2 min left".to_string()));

        // Title above meta, both inside the card.
        let card_rect = cards[0]
            .first()
            .map(|n| n.rect())
            .expect("the card background");
        let mut title_y = None;
        let mut meta_y = None;
        for n in &cards[0] {
            if let SceneNode::Text { rect, text, .. } = n {
                if text == "Recipe A" {
                    title_y = Some(rect.y);
                }
                if text == "2 min left" {
                    meta_y = Some(rect.bottom());
                }
            }
        }
        let (ty, mb) = (title_y.expect("a title"), meta_y.expect("a meta"));
        assert!(mb > ty, "the meta sits under the title");
        assert!(
            mb <= card_rect.bottom() + 0.01,
            "the meta must stay inside the card, ended at {mb} against {}",
            card_rect.bottom()
        );

        // The progress bar is a track plus a fill, pinned to the bottom edge, and the
        // fill is a fraction of the card rather than the whole width.
        let bars: Vec<Rect> = cards[0]
            .iter()
            .filter_map(|n| match n {
                SceneNode::Rect { rect, .. } if (rect.h - CARD_PROG_H).abs() < 0.01 => Some(*rect),
                _ => None,
            })
            .collect();
        assert_eq!(bars.len(), 2, "a 0.6 progress draws a track and a fill");
        assert!((bars[0].w - card_rect.w).abs() < 0.01, "the track spans the card");
        assert!(bars[1].w < bars[0].w && bars[1].w > 0.0, "the fill is a fraction of it");
        assert!((bars[0].bottom() - card_rect.bottom()).abs() < 0.01, "pinned to the bottom");

        // The card without progress draws no bar at all.
        let bars2 = cards[1]
            .iter()
            .filter(|n| matches!(n, SceneNode::Rect { rect, .. } if (rect.h - CARD_PROG_H).abs() < 0.01))
            .count();
        assert_eq!(bars2, 0, "no progress in the payload means no bar");
    }

    /// The leaves of the first card laid out from `hc`.
    fn first_card(hc: &HomeCompose) -> Vec<SceneNode> {
        let root = layout_home(1600.0, 900.0, hc, &Theme::cosmic_default(), &mut MonoMeasure);
        if let SceneNode::Container { children, .. } = &root {
            for c in children {
                if let SceneNode::Container {
                    interactive: true,
                    children,
                    ..
                } = c
                {
                    return children.clone();
                }
            }
        }
        panic!("no card laid out")
    }

    /// A measure that claims the Material face, so the icon path can be exercised
    /// without a font stack. Widths come from `MonoMeasure`, which is what the rest of
    /// these tests already lay out with.
    struct IconMeasure;
    impl TextMeasure for IconMeasure {
        fn text_width(&mut self, text: &str, size_px: f32) -> f32 {
            MonoMeasure.text_width(text, size_px)
        }
        fn has_icon_face(&self) -> bool {
            true
        }
    }

    #[test]
    fn the_bars_right_cluster_reads_in_the_shells_order_from_the_right_edge() {
        let (w, h) = (1600.0, 900.0);
        let root = layout_home(w, h, &sample(), &Theme::cosmic_default(), &mut IconMeasure);
        let runs = bar_runs(&root);
        let at = |t: &str| runs.iter().find(|(s, _)| s == t).map(|(_, r)| *r);

        // Right to left: shield, palette, notifications, then the avatar, then the orb-sm.
        let shield = at("shield").expect("a shield glyph");
        let palette = at("palette").expect("a palette glyph");
        let notif = at("notifications").expect("a notifications glyph");
        let avatar = at(AVATAR_INITIAL).expect("the avatar initial");
        assert!(shield.x > palette.x, "shield is outermost");
        assert!(palette.x > notif.x, "then palette, then notifications");
        assert!(notif.x > avatar.x, "the avatar sits inside the tray");
        assert!(
            shield.right() <= w - EDGE_PAD + 0.01,
            "the cluster stays inside the edge pad"
        );

        // The orb-sm is inboard of the avatar now, which is where the shell puts it.
        let mut orb_sm_x = None;
        if let SceneNode::Container { children, .. } = &root {
            for c in children {
                if let SceneNode::Container { children, .. } = c {
                    for n in children {
                        if let SceneNode::OrbSlot { rect, compact: true } = n {
                            orb_sm_x = Some(rect.x);
                        }
                    }
                }
            }
        }
        let ox = orb_sm_x.expect("a compact orb slot in the bar");
        assert!(ox < avatar.x, "the orb-sm is inboard of the avatar");

        // Everything in the cluster stays on the bar's line.
        for (label, r) in &runs {
            assert!(
                r.y >= 0.0 && r.bottom() <= TOP_BAR_H + 0.01,
                "{label} escapes the 40px strip"
            );
        }

        // With no icon face the glyphs vanish but the avatar and the orb do not: they are
        // not ligatures, so a missing font must not take them with it.
        let plain = layout_home(w, h, &sample(), &Theme::cosmic_default(), &mut MonoMeasure);
        let pruns = bar_runs(&plain);
        assert!(!pruns.iter().any(|(s, _)| s == "shield"));
        assert!(pruns.iter().any(|(s, _)| s == AVATAR_INITIAL), "the avatar survives");
    }

    #[test]
    fn the_omnibox_pill_carries_its_glyph_prompt_and_shortcut_hint() {
        let root = layout_home(1600.0, 900.0, &sample(), &Theme::cosmic_default(), &mut IconMeasure);
        let runs = bar_runs(&root);
        let at = |t: &str| runs.iter().find(|(s, _)| s == t).map(|(_, r)| *r);
        let glyph = at(OMNIBOX_GLYPH).expect("the search glyph");
        let prompt = at("Ask or search anything").expect("the prompt");
        let kbd = at(OMNIBOX_KBD).expect("the shortcut hint");
        assert!(prompt.x > glyph.x, "the prompt follows the glyph");
        assert!(kbd.x > prompt.x, "the hint is pushed to the far end");

        let pill_right = (1600.0 - OMNIBOX_W) * 0.5 + OMNIBOX_W;
        assert!(
            kbd.right() <= pill_right - 6.0,
            "the hint stays inside the pill, ended at {} against {pill_right}",
            kbd.right()
        );
    }

    #[test]
    fn a_card_icon_needs_the_face_and_draws_only_when_there_is_no_art() {
        // The icon is a LIGATURE NAME, so without the face it renders as the word
        // "storage" across the card. That is not hypothetical: literal "lock" and
        // "notifications" across the tray is what a fresh offline ISO did before the
        // shell bundled its fonts. So no face means no icon.
        let hc = sample();
        let root = layout_home(1600.0, 900.0, &hc, &Theme::cosmic_default(), &mut MonoMeasure);
        let mut seen = 0;
        root.for_each_leaf(&mut |_, leaf| {
            if let SceneNode::Text { text, .. } = leaf {
                if text == "storage" {
                    seen += 1;
                }
            }
        });
        assert_eq!(seen, 0, "no Material face means the icon must NOT be drawn");

        // With the face, it draws, in a tile at the card's top left.
        let root = layout_home(1600.0, 900.0, &hc, &Theme::cosmic_default(), &mut IconMeasure);
        let mut icon = None;
        root.for_each_leaf(&mut |_, leaf| {
            if let SceneNode::Text { rect, text, .. } = leaf {
                if text == "storage" {
                    icon = Some(*rect);
                }
            }
        });
        let ir = icon.expect("the icon glyph draws when the face is there");
        let card = first_card(&hc)[0].rect();
        assert!(ir.x < card.x + card.w * 0.5, "the icon hugs the LEFT edge");
        assert!(ir.y < card.y + card.h * 0.5, "and the top");

        // A card WITH art draws no icon: the glyph stands in FOR the missing picture,
        // it is not a decoration beside one (the shell's `card.icon && !hasImage`).
        let mut arted = sample();
        arted.rows[0].cards[0].image = Some("/shell/static/app_art/a.png".into());
        let root = layout_home(1600.0, 900.0, &arted, &Theme::cosmic_default(), &mut IconMeasure);
        let mut seen = 0;
        root.for_each_leaf(&mut |_, leaf| {
            if let SceneNode::Text { text, .. } = leaf {
                if text == "storage" {
                    seen += 1;
                }
            }
        });
        assert_eq!(seen, 0, "art supersedes the icon");
    }

    #[test]
    fn a_live_tag_supersedes_a_badge_and_never_draws_beside_it() {
        // hartHome.js is `if (card.live) ... else if (card.badge)`: a running agent
        // supersedes whatever the card was otherwise labelled, and drawing both would put
        // two chips in the same corner.
        let mut hc = sample();
        hc.rows[0].cards[0].badge = Some("NEW".into());
        hc.rows[0].cards[0].live = Some("RUNNING".into());
        let leaves = first_card(&hc);
        let texts: Vec<String> = leaves
            .iter()
            .filter_map(|n| match n {
                SceneNode::Text { text, .. } => Some(text.clone()),
                _ => None,
            })
            .collect();
        assert!(texts.contains(&"RUNNING".to_string()), "the live tag draws");
        assert!(!texts.contains(&"NEW".to_string()), "the badge must NOT draw beside it");

        // A live tag carries its dot; a badge does not.
        let theme = Theme::cosmic_default();
        let dots = |ls: &Vec<SceneNode>| {
            ls.iter()
                .filter(|n| matches!(n, SceneNode::Rect { color, .. } if *color == theme.live_dot))
                .count()
        };
        assert_eq!(dots(&leaves), 1, "the live tag has exactly one indicator dot");

        hc.rows[0].cards[0].live = None;
        let badged = first_card(&hc);
        assert_eq!(dots(&badged), 0, "a badge has no dot");
    }

    #[test]
    fn the_card_chip_sits_inside_the_card_at_its_top_right() {
        let leaves = first_card(&sample());
        let card_rect = leaves[0].rect();
        let chip = leaves
            .iter()
            .find_map(|n| match n {
                SceneNode::Text { rect, text, .. } if text == "NEW" => Some(*rect),
                _ => None,
            })
            .expect("the badge label");
        assert!(chip.right() <= card_rect.right(), "the chip stays inside the card");
        assert!(chip.x > card_rect.x + card_rect.w * 0.5, "it hugs the RIGHT edge");
        assert!(chip.y >= card_rect.y, "and the top");
        assert!(chip.bottom() < card_rect.bottom(), "well clear of the title");
    }

    #[test]
    fn zero_progress_still_draws_an_empty_track() {
        // A card at 0% and a card with no progress at all are different states. Drawing
        // nothing for the first would silently collapse them into the second.
        let mut hc = sample();
        hc.rows[0].cards[0].progress = Some(0.0);
        let root = layout_home(1600.0, 900.0, &hc, &Theme::cosmic_default(), &mut MonoMeasure);
        let mut bars = 0;
        root.for_each_leaf(&mut |_, leaf| {
            if let SceneNode::Rect { rect, .. } = leaf {
                if (rect.h - CARD_PROG_H).abs() < 0.01 {
                    bars += 1;
                }
            }
        });
        assert_eq!(bars, 1, "zero draws the track and no fill");
    }

    #[test]
    fn decode_reads_the_card_meta_and_clamps_progress() {
        let v = serde_json::json!({
            "rows": [{ "label": "R", "cards": [
                { "title": "A", "meta": "2 min left", "progress": 0.42 },
                { "title": "B" },
                { "title": "C", "meta": "", "progress": 4.0 },
                { "title": "D", "progress": -1.0 }
            ] }]
        });
        let cards = &decode_home_compose(&v).rows[0].cards;
        assert_eq!(cards[0].meta.as_deref(), Some("2 min left"));
        assert_eq!(cards[0].progress, Some(0.42));
        assert_eq!(cards[1].meta, None);
        assert_eq!(cards[1].progress, None);
        // Empty meta is absent, and an out-of-range progress is DROPPED rather than
        // clamped: it cannot have come from the sanitizer, so drawing a confident bar
        // from it would be inventing a number.
        assert_eq!(cards[2].meta, None);
        assert_eq!(cards[2].progress, None);
        assert_eq!(cards[3].progress, None);
    }

    #[test]
    fn decode_reads_the_row_note_and_see_all_target() {
        let v = serde_json::json!({
            "rows": [
                { "label": "Continue", "note": "3 in progress", "see_all": "panel:continue" },
                { "label": "Bare" },
                { "label": "Blank", "see_all": "", "note": "" }
            ]
        });
        let hc = decode_home_compose(&v);
        assert_eq!(hc.rows[0].note.as_deref(), Some("3 in progress"));
        assert_eq!(hc.rows[0].see_all.as_deref(), Some("panel:continue"));
        assert_eq!(hc.rows[1].note, None);
        assert_eq!(hc.rows[1].see_all, None);
        // An empty string is absent, not a target that opens nothing.
        assert_eq!(hc.rows[2].see_all, None);
        assert_eq!(hc.rows[2].note, None);
    }

    #[test]
    fn a_degenerate_output_lays_out_without_panicking_or_inverting_a_rect() {
        // The never-fail floor, applied to layout. This runs inside the compositor's own
        // render path, so a panic here is not a wrong-looking desktop, it is NO desktop:
        // the process that owns scanout dies. A zero or 1px output is reachable in
        // practice (a mode not yet set, a CRTC coming back from DPMS, a hotplug race),
        // and every arm of this layout subtracts fixed chrome from the output size, which
        // is exactly the arithmetic that goes negative first.
        for (w, h) in [
            (0.0, 0.0),
            (1.0, 1.0),
            (0.0, 900.0),
            (1600.0, 0.0),
            (320.0, 40.0),
            (2.0, 84.0), // exactly the two strips, so the content band is empty
        ] {
            let root = layout_home(w, h, &sample(), &Theme::cosmic_default(), &mut MonoMeasure);
            let mut checked = 0;
            root.for_each_leaf(&mut |_, leaf| {
                let r = leaf.rect();
                assert!(
                    r.w >= 0.0 && r.h >= 0.0,
                    "{w}x{h} produced an INVERTED rect {r:?}, which reaches the lowering \
                     as a negative buffer size"
                );
                assert!(
                    r.w.is_finite() && r.h.is_finite() && r.x.is_finite() && r.y.is_finite(),
                    "{w}x{h} produced a non-finite rect {r:?}"
                );
                checked += 1;
            });
            // A hit test at the origin and off the canvas must also be total.
            let _ = root.hit_test(0.0, 0.0);
            let _ = root.hover_leaf(Some((-5.0, -5.0)));
            let _ = root.pointer_orb_energy(Some((w * 2.0, h * 2.0)), true);
            assert!(checked > 0, "{w}x{h} emitted no leaves at all");
        }
    }

    #[test]
    fn the_font_free_measure_scales_with_length_and_size() {
        let mut m = MonoMeasure;
        assert_eq!(m.text_width("", 15.0), 0.0);
        assert!(m.text_width("HART OS", 15.0) > m.text_width("HART", 15.0));
        assert!(m.text_width("HART", 30.0) > m.text_width("HART", 15.0));
    }

    #[test]
    fn hovering_a_card_marks_its_own_background_leaf_and_nothing_else_does() {
        let (w, h) = (1600.0, 900.0);
        let root = layout_home(w, h, &sample(), &Theme::cosmic_default(), &mut MonoMeasure);
        // Find a card: the interactive group layout_home emits once per card.
        let mut card = None;
        if let SceneNode::Container { children, .. } = &root {
            for c in children {
                if let SceneNode::Container {
                    rect,
                    interactive: true,
                    ..
                } = c
                {
                    card = Some(*rect);
                    break;
                }
            }
        }
        let cr = card.expect("a card group");
        let centre = (cr.x + cr.w * 0.5, cr.y + cr.h * 0.5);

        let idx = root
            .hover_leaf(Some(centre))
            .expect("a card must be a hover target");
        let mut leaves = Vec::new();
        root.flatten(&mut leaves);
        // The marked leaf is THAT card's own background rect: not a neighbour's, not its
        // title text. The highlight lifts a background the scene already draws.
        match leaves[idx] {
            SceneNode::Rect { rect, .. } => assert_eq!(*rect, cr),
            other => panic!("hover marked a {other:?}, not the card background"),
        }

        // The top bar and the bare hero column are structural, not hover targets, so the
        // cursor resting on them lifts nothing.
        assert_eq!(root.hover_leaf(Some((w * 0.5, 4.0))), None);
        assert_eq!(
            root.hover_leaf(Some((EDGE_PAD + 4.0, TOP_BAR_H + EDGE_PAD + 4.0))),
            None
        );
        // No pointer, no highlight (the default every frame before the cursor moves).
        assert_eq!(root.hover_leaf(None), None);
    }

    #[test]
    fn the_hover_lift_brightens_and_leaves_alpha_alone() {
        let base = Color::rgba(0.1, 0.2, 0.3, 0.5);
        let lit = base.lift(CARD_HOVER_LIFT);
        assert!(lit.r > base.r && lit.g > base.g && lit.b > base.b);
        assert_eq!(lit.a, base.a, "hover must not change how opaque a card is");
        // No lift is identity, and the amount is clamped at both ends.
        assert_eq!(base.lift(0.0), base);
        assert_eq!(base.lift(-1.0), base);
        let white = base.lift(5.0);
        assert!((white.r - 1.0).abs() < 1e-6 && (white.b - 1.0).abs() < 1e-6);
        assert_eq!(white.a, base.a);
    }

    #[test]
    fn pointer_over_the_orb_lifts_its_energy_and_nowhere_else() {
        let (w, h) = (1600.0, 900.0);
        let root = layout_home(w, h, &sample(), &Theme::cosmic_default(), &mut MonoMeasure);
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

        let _ = cache.tree_for(1600.0, 900.0, &home, &theme, &mut MonoMeasure);
        assert_eq!(cache.rebuilds(), 1, "the first frame builds the tree");

        // A steady desktop: same size, same payload, same theme. However many frames run,
        // the tree must NOT be rebuilt — this is the zero-per-frame-alloc NFR.
        for _ in 0..10 {
            let _ = cache.tree_for(1600.0, 900.0, &home, &theme, &mut MonoMeasure);
        }
        assert_eq!(cache.rebuilds(), 1, "a steady desktop must not rebuild per frame");

        // A resize changes layout, so it must rebuild.
        let _ = cache.tree_for(1280.0, 800.0, &home, &theme, &mut MonoMeasure);
        assert_eq!(cache.rebuilds(), 2, "a resize must rebuild");

        // A new compose changes layout, so it must rebuild.
        let mut recomposed = home.clone();
        recomposed.hero.eyebrow = "Shipped a release".into();
        let _ = cache.tree_for(1280.0, 800.0, &recomposed, &theme, &mut MonoMeasure);
        assert_eq!(cache.rebuilds(), 3, "a new compose must rebuild");

        // And the retained tree is a REAL tree, not an empty placeholder: the cached nodes
        // are what hover hit-tests against (the pointer is deliberately not part of the key).
        let node_count = cache
            .tree_for(1280.0, 800.0, &recomposed, &theme, &mut MonoMeasure)
            .node_count();
        assert!(node_count > 1, "the retained tree must hold real nodes");
        assert_eq!(cache.rebuilds(), 3, "re-reading the cached tree must not rebuild");
    }
}
