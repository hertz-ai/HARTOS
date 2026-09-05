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

    /// This colour moved `t` of the way toward `other` (0.0 unchanged, 1.0 `other`), with
    /// ALPHA untouched: the mix answers "what hue", never "how opaque", so a blend can
    /// never quietly change whether the thing behind shows through.
    ///
    /// The one channel-blend primitive the scene has. `lift` is this toward white, and the
    /// card art's two stops are this toward ink, which is the same arithmetic the shell's
    /// `HartBrandArt.blend` runs; a second implementation of it is how the desktop icon
    /// layer and the home cards drifted apart in the first place (hartBrandArt.js header).
    pub fn mix(self, other: Color, t: f32) -> Color {
        let k = t.clamp(0.0, 1.0);
        Color::rgba(
            self.r + (other.r - self.r) * k,
            self.g + (other.g - self.g) * k,
            self.b + (other.b - self.b) * k,
            self.a,
        )
    }

    /// This colour lifted toward white by `amount` (0.0 unchanged, 1.0 white), with alpha
    /// untouched so a hover never changes how opaque a card is. Lifting toward white
    /// rather than scaling the channels is what keeps a near-black card visibly reactive:
    /// a multiply would leave a dark card almost unchanged.
    pub fn lift(self, amount: f32) -> Color {
        self.mix(Color::rgba(1.0, 1.0, 1.0, self.a), amount)
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
    /// The leaderboard rank numeral's outline. Faint on purpose: it sits BEHIND the card's
    /// own content and must read as a watermark, not compete with it.
    pub rank_ink: Color,
    /// The brand SPECTRUM, in the shell's own order (teal, cyan, blue, violet, magenta,
    /// amber). A row names one of these and its note and its cards' progress bars take
    /// that hue, which is what stops a stack of rows reading flat. The names stay wire
    /// strings resolved HERE, the same rule `mood` follows: the palette lives in the
    /// theme, never as a second table inside the layout.
    pub spectrum: [Color; 6],
    pub taskbar_bg: Color,
    /// `shell.topbar_height`, the shell's `--hart-topbar-height`. NOT a constant, because
    /// four of the ten shipped themes move it (36, 38, 40, 44) and the SHELL publishes
    /// the panel reservation from this same number: a native bar that drew a fixed 40
    /// would overdraw a 36px reservation, which is the 2026-08-29 "taskbar unreachable"
    /// report arriving through the new renderer.
    ///
    /// The taskbar's height is deliberately NOT here: the theme has no key for it, it is
    /// a Python constant beside a CSS literal, and test_panel_reservation.py pins those
    /// two together. Inventing a theme key for it here would be a third source.
    pub top_bar_h: f32,
    /// `shell.icon_size`, the shell's `--hart-icon-size`: the tray glyph size. Moves with
    /// the theme too (18, 20, 22).
    pub icon_px: f32,
    /// `shell.border_radius`, the shell's `--hart-radius`: the card corner. The shipped
    /// DEFAULT theme sets 22, and the themes span 4 to 22, so a fixed 16 was already the
    /// wrong shape on aura before any of them was chosen.
    pub card_radius: f32,
    /// `colors.glass_border`, the shell's `--hart-glass-border`: the 1px rule the top bar
    /// draws along its bottom and the taskbar along its top. It is the line that separates
    /// chrome from desktop, and the native strips had none, so their edge was wherever the
    /// translucency happened to stop. It varies REAL amounts by theme (aura white at .10,
    /// arctic blue at .15, cyberpunk pink at .2), so it is not a constant.
    pub chrome_border: Color,
    /// How thick that rule is. 1px normally; `html.a11y-contrast .glass` doubles it, which
    /// is the whole reason this is not the `CHROME_RULE` constant it started as.
    pub chrome_rule_px: f32,
}

/// The spectrum names, positionally matched to `Theme::spectrum`.
const SPECTRUM_NAMES: [&str; 6] = ["teal", "cyan", "blue", "violet", "magenta", "amber"];

/// A palette literal, degrading to `fallback` instead of panicking.
///
/// The strings below are compile-time constants copied from the shell's CSS, so the
/// fallback is unreachable in practice. It exists because this runs inside the process
/// that owns scanout: a mistyped hex should cost one wrong colour, not the desktop. That
/// is the same posture the text path already takes with an empty font database, and the
/// reason `from_hex` returns Option rather than panicking in the first place.
fn palette(hex: &str, fallback: Color) -> Color {
    match Color::from_hex(hex) {
        Some(c) => c,
        None => fallback,
    }
}

impl Theme {
    /// Resolve a row's accent NAME to its hue, or None for a name outside the spectrum.
    /// Callers fall back by ROW INDEX rather than to a fixed colour, which is what the
    /// shell does and the reason consecutive rows do not all read teal.
    pub fn spectrum_named(&self, name: &str) -> Option<Color> {
        SPECTRUM_NAMES
            .iter()
            .position(|n| *n == name)
            .map(|i| self.spectrum[i])
    }

    /// The hue a row at `index` carries: its own accent when it names a real one, else
    /// the spectrum rotated by position.
    pub fn row_accent(&self, name: Option<&str>, index: usize) -> Color {
        name.and_then(|n| self.spectrum_named(n))
            .unwrap_or(self.spectrum[index % self.spectrum.len()])
    }

    /// A card's art-tile gradient: `(from, to, angle_deg)`, the port of the shell's
    /// `HartBrandArt.gradient(baseHex, seed)` with `seed` = the card's index in its row.
    ///
    /// This is the surface EVERY card has. The shell paints it unconditionally
    /// (`art.style.background = gradientArt(...)`) and only then fades a photo in over it,
    /// with the comment "no empty flash". So a card without a photo is not a flat tile: it
    /// is its row's hue darkened toward ink across two stops. The native scene drew the
    /// flat tile, and drew a ranked card as NOTHING at all, because a ranked card's own
    /// background is transparent by design and the art it is made of was never lowered.
    ///
    /// Angles and blend factors are the shell's literals, not a re-derivation:
    /// `[135,150,165][seed % 3]`, `blend(base, INK, 0.46)` for the darker stop,
    /// `blend(second, INK, 0.20)` for the lighter one.
    pub fn art_stops(
        &self,
        accent: Option<&str>,
        row_index: usize,
        card_index: usize,
    ) -> (Color, Color, f32) {
        let n = self.spectrum.len();
        // The shell resolves the row's accent to a NAME first (`row.accent || spec[idx]`)
        // and only then asks SPECTRUM_HEX for it, so the two branches below are its two,
        // not an interpretation of them.
        let (base, second) = match accent {
            //   A name outside the spectrum: `spectrumHex[accent]` is undefined, which
            //   drops `gradient()` into its no-hex branch and folds in a NEIGHBOUR hue for
            //   iridescence, seeded by the CARD index. The wire sanitizer coerces the
            //   accent into the spectrum, so this is the hand-written-payload path.
            Some(name) => match self.spectrum_named(name) {
                Some(hue) => (hue, hue),
                None => (
                    self.spectrum[card_index % n],
                    self.spectrum[(card_index + 2) % n],
                ),
            },
            //   No accent at all: the row's positional hue, and an explicit hex from there
            //   on, so both stops are that one hue.
            None => {
                let hue = self.spectrum[row_index % n];
                (hue, hue)
            }
        };
        (
            second.mix(ART_INK, 0.20),
            base.mix(ART_INK, 0.46),
            ART_ANGLES[card_index % ART_ANGLES.len()],
        )
    }
}

/// The deep ink every art tile darkens toward: hartBrandArt.js `INK = [14,14,17]`, with
/// its own note that it is neutral near-black and NOT navy, so blending toward it does not
/// blue-shift the hue away from the row's accent.
const ART_INK: Color = Color::rgba(14.0 / 255.0, 14.0 / 255.0, 17.0 / 255.0, 1.0);

/// The three gradient angles a row cycles through, so neighbouring cards do not read as
/// one repeated tile. The shell's own list, in its order.
const ART_ANGLES: [f32; 3] = [135.0, 150.0, 165.0];

impl Theme {
    /// The checklist b-section default anchors: teal accent (#00E6C3, the orb default),
    /// a near-black cosmic bar, neutral ink. A safe fallback when no `mood` was pushed;
    /// a real compose overrides via the palette owner upstream.
    pub fn cosmic_default() -> Theme {
        // A visible neutral as the fallback, never TRANSPARENT: a colour that vanishes
        // hides the mistake, and a grey square does not.
        let teal = palette("#00E6C3", Color::rgba(0.5, 0.5, 0.5, 1.0));
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
            accent2: palette("#9B5CFF", Color::rgba(0.5, 0.5, 0.5, 1.0)),
            // The shell's own card-badge ink and --hart-amb-4 default, so a native chip
            // reads as the same component rather than a lookalike.
            on_accent_ink: palette("#04140F", Color::rgba(0.0, 0.0, 0.0, 1.0)),
            live_dot: palette("#FF2E9A", Color::rgba(0.5, 0.5, 0.5, 1.0)),
            chip_bg: Color::rgba(0.031, 0.047, 0.078, 0.72),
            // rgba(255,255,255,0.30), the shell's own -webkit-text-stroke colour.
            rank_ink: Color::rgba(1.0, 1.0, 1.0, 0.30),
            // hartBrandArt's SPECTRUM_HEX, in its order.
            spectrum: [
                teal,
                palette("#29C5FF", teal),
                palette("#3B82F6", teal),
                palette("#9B5CFF", teal),
                palette("#FF2E9A", teal),
                palette("#FFC83D", teal),
            ],
            taskbar_bg: Color::rgba(0.043, 0.047, 0.063, 0.85),
            // The theme_service fallbacks, which are what the shell renders with when a
            // theme omits the key. Overridden per theme by `with_shell_metrics`.
            top_bar_h: TOP_BAR_H,
            // The named constants, not bare literals: these ARE the fallbacks, and the
            // cross-language guard pins the constants. Duplicating their values here left
            // the constants orphaned and the guard pinning something the layout no longer
            // read, which is a guard that cannot fail for the reason it exists.
            icon_px: TRAY_PX,
            card_radius: CARD_RADIUS,
            // The built-in css_vars fallback (liquid_ui_service l.1764), which is what
            // the shell renders with when ThemeService cannot be consulted. An unreadable
            // theme file here is the same situation, so it takes the same value.
            chrome_border: Color::rgba(0.0, 230.0 / 255.0, 195.0 / 255.0, 0.18),
            chrome_rule_px: CHROME_RULE,
        }
    }

    /// This theme forced into HIGH CONTRAST, exactly as `html.a11y-contrast` does.
    ///
    /// The shell's rule is four token overrides plus a doubled glass border:
    /// `--hart-muted:#e8eef2; --hart-glass-bg:#0a0a12; --hart-glass-border:#ffffff;
    /// --hart-text:#ffffff` and `.glass{background:#0a0a12;border-width:2px}`. The native
    /// scene read its colours from the theme file and knew nothing about the class, so a
    /// high-contrast desktop would have gone native at ordinary contrast.
    ///
    /// This is the ONE place a palette gets to set OPACITY, and deliberately: `#0a0a12`
    /// has no alpha, so the chrome goes solid. Translucency is precisely the thing high
    /// contrast exists to remove, so the "alpha belongs to the surface treatment" rule
    /// that governs `with_theme_colors` is the wrong rule here and is overridden on
    /// purpose rather than by oversight.
    ///
    /// The literals are the shell's own, not a re-derivation.
    pub fn with_high_contrast(mut self) -> Theme {
        let solid = |hex: &str| palette(hex, Color::rgba(1.0, 1.0, 1.0, 1.0));
        let glass = solid("#0A0A12");
        // `.glass` is the top bar and the taskbar here; the home cards and the omnibox
        // pill carry their own backgrounds in the shell and are not `.glass`.
        self.bar_bg = glass;
        self.taskbar_bg = glass;
        self.chrome_border = solid("#FFFFFF");
        let text = solid("#FFFFFF");
        self.bar_ink = text;
        self.hero_title = text;
        self.card_ink = text;
        let muted = solid("#E8EEF2");
        self.omnibox_ink = muted;
        self.hero_copy = muted;
        self.chrome_rule_px = CHROME_RULE * 2.0;
        self
    }

    /// This theme with the active theme file's SHELL METRICS folded in.
    ///
    /// Separate from `with_theme_colors` because these are not colours and their failure
    /// mode is different: a wrong colour is ugly, a wrong bar height is a band of desktop
    /// windows can be placed under. Each is `None`-tolerant and keeps the shipped value,
    /// so an unreadable theme is byte-identical to before this existed.
    ///
    /// Clamped, because these come from a file: a zero or negative bar would invert the
    /// content band's arithmetic, and an enormous one would leave no desktop at all. The
    /// bounds are generous enough that every shipped theme passes untouched.
    pub fn with_shell_metrics(
        mut self,
        top_bar_h: Option<f32>,
        icon_px: Option<f32>,
        card_radius: Option<f32>,
        chrome_border: Option<Color>,
    ) -> Theme {
        if let Some(c) = chrome_border {
            self.chrome_border = c;
        }
        if let Some(h) = top_bar_h {
            self.top_bar_h = h.clamp(16.0, 128.0);
        }
        if let Some(i) = icon_px {
            self.icon_px = i.clamp(8.0, 64.0);
        }
        if let Some(r) = card_radius {
            self.card_radius = r.clamp(0.0, 64.0);
        }
        self
    }

    /// This theme with the ACTIVE theme file's colours folded in, each one optional.
    ///
    /// The compositor already reads `conky-themes/<id>.json` for the desktop backdrop
    /// (bloom.rs, whose own header calls it "one palette source for both renderers, Gate
    /// 4: no parallel theme table"). The scene's colours were a hardcoded copy of what
    /// that same file carries, which made the compositor its OWN counter-example: change
    /// the theme and the backdrop restyled under a desktop that did not move.
    ///
    /// Every argument is `None`-tolerant and falls back to the value already in `self`,
    /// so a missing file, a missing key or a malformed hex costs exactly one colour and
    /// an absent theme file is byte-identical to before this existed. That is the same
    /// posture the backdrop takes, for the same reason: this runs in the process that
    /// owns scanout.
    ///
    /// Alpha comes from `self`, never from the file. The theme names HUES; how opaque a
    /// bar or a card is belongs to the surface treatment, and letting a palette change it
    /// would let a theme make the top bar transparent or the cards solid.
    pub fn with_theme_colors(
        mut self,
        background: Option<Color>,
        accent: Option<Color>,
        secondary: Option<Color>,
        text: Option<Color>,
        muted: Option<Color>,
        surface: Option<Color>,
    ) -> Theme {
        let keep_alpha = |base: Color, hue: Option<Color>| match hue {
            Some(c) => Color::rgba(c.r, c.g, c.b, base.a),
            None => base,
        };
        // `background` is the ground both strips sit on, at their own opacities.
        //
        // `chip_bg` deliberately does NOT follow it. The shell's `.hh-card-live` is a
        // fixed `rgba(8,12,20,0.72)` scrim, not a theme colour, because it exists to keep
        // a live tag readable over whatever art is behind it. A pale theme background
        // would turn that guarantee into pale-on-pale.
        self.bar_bg = keep_alpha(self.bar_bg, background);
        self.taskbar_bg = keep_alpha(self.taskbar_bg, background);
        // `accent` is the functional signifier: the orb hue, the active tab, a badge.
        // It also leads the spectrum, whose first entry IS teal in the shipped theme.
        if let Some(a) = accent {
            self.accent = a;
            self.spectrum[0] = a;
        }
        if let Some(s) = secondary {
            self.accent2 = s;
        }
        self.bar_ink = keep_alpha(self.bar_ink, text);
        self.hero_title = keep_alpha(self.hero_title, text);
        self.card_ink = keep_alpha(self.card_ink, text);
        self.omnibox_ink = keep_alpha(self.omnibox_ink, muted);
        self.hero_copy = keep_alpha(self.hero_copy, muted);
        // `surface` is the raised material: cards and the omnibox pill.
        self.card_bg = keep_alpha(self.card_bg, surface);
        self.omnibox_bg = keep_alpha(self.omnibox_bg, surface);
        self
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
                    ranked: false,
                    accent: Some("cyan".to_string()),
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
                            photo: None,
                        },
                        Card {
                            title: "Inbox triage".to_string(),
                            meta: Some("12 unread".to_string()),
                            progress: Some(0.25),
                            icon: Some("inbox".to_string()),
                            badge: None,
                            live: Some("running".to_string()),
                            photo: None,
                        },
                        Card {
                            title: "Storage report".to_string(),
                            meta: Some("92% used".to_string()),
                            progress: Some(0.92),
                            icon: Some("storage".to_string()),
                            badge: None,
                            live: None,
                            photo: None,
                        },
                    ],
                },
                Row {
                    title: "For you".to_string(),
                    ranked: true,
                    accent: Some("amber".to_string()),
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
                            photo: None,
                        },
                        Card {
                            title: "Agent recipes".to_string(),
                            meta: Some("3 ready to run".to_string()),
                            progress: None,
                            icon: Some("auto_awesome".to_string()),
                            badge: None,
                            live: None,
                            photo: None,
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
    /// The hive leaderboard treatment (`row.ranked`): cards drop their tile and carry a
    /// big outlined rank numeral instead.
    pub ranked: bool,
    /// The row's spectrum accent NAME (`row.accent`), kept as the wire string and
    /// resolved by the theme, the same way `mood` is. Absent means rotate by position.
    pub accent: Option<String>,
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
    /// The card's PHOTO, if the payload named one. Not the card's art: every card has art
    /// (the brand gradient, see `Theme::art_stops`), and a photo is what fades in over it.
    ///
    /// Decoded from `card.image` OR `card.image_url`, in that priority, because that is the
    /// shell's own `imgSrc` and, more to the point, its `hasImage`. Reading only `image`
    /// meant a news or app card, which carries `image_url` (liquid_ui_service stamps art
    /// there), decoded as photo-less and so drew the icon glyph the shell suppresses.
    ///
    /// Still not LOWERED: the photo layer is the M3 remainder. What changed is that its
    /// absence is no longer the difference between a card and a hole.
    pub photo: Option<String>,
}

/// Which named surface of the desktop a point belongs to, for LATENCY ATTRIBUTION.
///
/// docs/architecture/latency_budgets.json carries a per-component budget table with 23
/// entries, and not one of them has ever been consulted: the instrument reports
/// `component=shell` for every sample, so every measurement is checked against the
/// `_defaults` and a slow orb is indistinguishable from a slow marketplace. latency.rs
/// says why in its own header, and says the blocker has MOVED: "the scene graph now
/// EXISTS and hit-tests ... what is missing is carrying a node identity from the input
/// that produced a sample through to the frame that presented it."
///
/// This is that identity. It carries NO names of its own: `surface()` maps each variant
/// into `latency::Surface`, whose `label()` is the single source for the budget file's
/// keys. An earlier cut had a `key()` here saying the same words, which is two spellings
/// of one vocabulary and exactly the drift the guards exist to stop; the guard now reads
/// the mapping and follows it to the label.
///
/// Only the surfaces the NATIVE scene actually owns appear here. The rest of the budget
/// table (start-menu, panel, chat-input, marketplace, onboarding) belongs to the WebView
/// shell, which the same instrument measures as `shell`; that is deliberate, because the
/// harness wants the web baseline measured by the same instrument, so "native is faster"
/// is a demonstrated delta rather than a claim.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Component {
    Orb,
    TopBar,
    Omnibox,
    Taskbar,
    HomeCard,
    /// A card rail, carrying WHICH row it is.
    ///
    /// The index is not decoration: a wheel event has to reach the row under the pointer,
    /// and the tree is the only thing that knows where the rows are. Deriving it from
    /// geometry outside the layout would be a second copy of everything the layout
    /// decides, which is the mistake that left `EDGE_PAD` at a value belonging to neither
    /// of its two jobs. It does not affect the budget key: every row is `home-row`.
    HomeRow(usize),
}

impl Component {
    /// The latency instrument's own surface for this component.
    ///
    /// Two enums rather than one because latency.rs is deliberately free of every other
    /// module (no Smithay, no scene, no clock), which is what lets its state machine run
    /// under `cargo test` on the default no-feature build where `scene` is not even
    /// compiled. This is the one bridge, and the key strings on both sides are pinned to
    /// latency_budgets.json by the same guard.
    pub fn surface(self) -> crate::latency::Surface {
        match self {
            Component::Orb => crate::latency::Surface::Orb,
            Component::TopBar => crate::latency::Surface::TopBar,
            Component::Omnibox => crate::latency::Surface::Omnibox,
            Component::Taskbar => crate::latency::Surface::Taskbar,
            Component::HomeCard => crate::latency::Surface::HomeCard,
            Component::HomeRow(_) => crate::latency::Surface::HomeRow,
        }
    }

    /// The row index, for a component that is a row. `None` for everything else, which is
    /// what makes "scroll the thing under the pointer" a total function rather than a
    /// guess: a wheel over the top bar scrolls no row at all.
    pub fn row_index(self) -> Option<usize> {
        match self {
            Component::HomeRow(i) => Some(i),
            _ => None,
        }
    }
}

/// The scene tree the compositor renders. Wayland-FREE and GL-FREE: `comp_core`
/// lowers each variant to a `HartRenderElement` (Rect -> SolidColorBuffer or a cached
/// rounded tile, Text -> glyph-atlas Memory texture, Art -> a cached gradient tile,
/// OrbSlot -> the existing M2 orb element). A `Container` only groups and positions; it
/// paints nothing itself.
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
        /// Which named surface this group IS, for latency attribution, or None for a
        /// group that is only structure (the root). Set where the group is BUILT, so
        /// the layout that decides what a thing is also names it, rather than a second
        /// table elsewhere re-deriving it from geometry.
        component: Option<Component>,
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
        /// Outline width in px; 0.0 fills the glyph, which is every run but one.
        ///
        /// The shell draws the hive leaderboard's rank numeral as OUTLINED text
        /// (`color: transparent` plus a 3px `-webkit-text-stroke`), and coverage
        /// compositing cannot express that: a filled glyph at low alpha is a different
        /// thing, not a cheaper one. So the scene says what it wants and the rasterizer
        /// renders it, rather than the layout approximating.
        ///
        /// This field replaced `align`, which was written at every construction and read
        /// nowhere: neither the lowering nor the rasterizer ever looked at it, because
        /// layout aligns by computing x now that it can measure. A field written twenty
        /// times and read zero is not a contract, it is weight.
        stroke: f32,
    },
    /// A card's art tile: a two-stop linear gradient, plus the photo that belongs over it.
    ///
    /// This replaced `Image { rect, source, radius }`, which was constructed at exactly one
    /// site, lowered nowhere, and emitted ONLY when the payload named a picture. That is
    /// the wrong shape for what the shell draws: `.hh-card-art` is always present, its
    /// background is always the brand gradient, and the photo is an `<img>` that fades in
    /// on top. Modelling the picture as the tile meant a ranked card, whose own background
    /// is `transparent` because the art IS the card, rendered as nothing at all.
    Art {
        rect: Rect,
        /// The gradient's first stop, at the angle's start edge (the shell's `light`).
        from: Color,
        /// Its last stop (the shell's `dark`).
        to: Color,
        /// CSS gradient angle in degrees: 0 points UP the tile, increasing clockwise.
        angle_deg: f32,
        /// Corner radius in logical px, matching the tile the art fills.
        radius: f32,
        /// The photo drawn over the gradient once image lowering lands (M3 remainder).
        /// Carried here, not dropped, so the contract stays visible in the tree.
        photo: Option<String>,
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
            | SceneNode::Art { rect, .. }
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

    /// Which named component a point belongs to, for latency attribution, or None over
    /// bare desktop.
    ///
    /// The rule is DEEPEST WINS, matching `hit_test` and `hover_leaf`, so a card inside
    /// the content band names the card and the pill inside the top bar names the omnibox
    /// rather than the bar it sits in. The orb is the one component that is a LEAF rather
    /// than a group: `OrbSlot` already is the orb, so tagging a container around it would
    /// be a second way of saying the same thing.
    ///
    /// Pure, allocation-free and off the frame path: the caller runs it once per INPUT
    /// event against the retained tree, which is how a sample learns what it touched
    /// without the render loop paying for anything.
    pub fn component_at(&self, px: f32, py: f32) -> Option<Component> {
        let mut found = None;
        self.component_walk(px, py, &mut found);
        found
    }

    fn component_walk(&self, px: f32, py: f32, found: &mut Option<Component>) {
        match self {
            SceneNode::Container {
                rect,
                component,
                children,
                ..
            } => {
                if !rect.contains(px, py) {
                    return;
                }
                if let Some(c) = component {
                    *found = Some(*c);
                }
                for child in children {
                    child.component_walk(px, py, found);
                }
            }
            SceneNode::OrbSlot { rect, .. } if rect.contains(px, py) => {
                *found = Some(Component::Orb);
            }
            // A leaf inside a tagged group is that group; a leaf outside every group is
            // bare desktop, which has no budget row and must not borrow one.
            _ => {}
        }
    }

    /// Which card row a point is over, if any. One walk, the same deepest-wins rule as
    /// `component_at`, and no geometry duplicated outside the layout that placed it.
    pub fn row_at(&self, px: f32, py: f32) -> Option<usize> {
        self.component_at(px, py).and_then(Component::row_index)
    }

    /// The index, in `flatten` paint order, of the leaf that must paint its HOVER state
    /// this frame, or None when the pointer is absent or over nothing interactive. That
    /// leaf is the SURFACE of the top-most interactive `Container` under the cursor: its
    /// first child that actually paints a ground, a `Rect` or an `Art`. The highlight is a
    /// lift of something the scene already draws, never an extra node, so hover changes no
    /// geometry and no element count.
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

    /// Paint-order walk behind `hover_leaf`. `next` counts the leaves already passed, so at
    /// the moment a child is reached `next` IS the index that child's first leaf will take.
    /// Later hits overwrite, which is exactly top-most (and deepest) wins, matching
    /// `hit_test`'s rule with one walk and no allocation.
    fn hover_leaf_walk(&self, px: f32, py: f32, next: &mut usize, found: &mut Option<usize>) {
        match self {
            SceneNode::Container {
                rect,
                interactive,
                children,
                ..
            } => {
                // Claim the group's SURFACE: the first child that paints a ground. For an
                // ordinary card that is child zero, its background Rect. For a RANKED card
                // it is the Art tile, which is not first, because the shell appends the
                // rank numeral before the art so the art paints OVER it. Searching rather
                // than demanding child zero is what lets the numeral keep that order and
                // still leaves the ranked card with a hover, which it never had: its first
                // child used to be a fully transparent placeholder rect, added only to
                // satisfy this test, and lifting a transparent colour shows nothing.
                let mut claiming = *interactive && rect.contains(px, py);
                for child in children {
                    if claiming && matches!(child, SceneNode::Rect { .. } | SceneNode::Art { .. })
                    {
                        *found = Some(*next);
                        claiming = false;
                    }
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
/// `.top-bar { padding: 0 12px }`: the BAR's own inset, which is not the content gutter
/// and never was. One `EDGE_PAD` did both jobs at a value belonging to neither, which is
/// how the bar ended up indented twice as far as the shell's and the content half as far.
const BAR_PAD_X: f32 = 12.0;
/// `.top-bar-omni { max-width: 360px }`: how wide the pill gets when there is room.
const OMNIBOX_W: f32 = 360.0;
/// `.top-bar-orb { width: 30px; height: 30px }`: the brand orb docked in the bar.
const ORB_SM: f32 = 30.0;
/// `.top-bar .start-btn { font-size: 13px }`. The shell sets the wordmark in the BAR's
/// own scale, not the hero's, which is the whole reason it has its own constant.
const WORDMARK_PX: f32 = 13.0;
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
/// `.tray-btn { width: 32px; height: 32px }`, and `.top-bar-right { gap: 8px }`.
const TRAY_BTN: f32 = 32.0;
/// `.top-bar-right .tray-btn .mi { font-size: var(--hart-icon-size) }`, which the theme
/// service and the built-in css_vars both default to 20px.
const TRAY_PX: f32 = 20.0;
const TRAY_GAP: f32 = 8.0;
/// `.top-bar-avatar { width: 30px; height: 30px; font-size: 13px }`.
const AVATAR_D: f32 = 30.0;
const AVATAR_PX: f32 = 13.0;
/// The shell hardcodes this letter in its own markup, so it is the same letter rather
/// than a guess at whose account it is.
const AVATAR_INITIAL: &str = "H";
const OMNIBOX_GLYPH: &str = "search";
/// `.top-bar-omni { font-size: 13px }`, which the prompt run inside it also takes.
const OMNIBOX_PX: f32 = 13.0;
/// The shortcut hint at the far end of the pill (the shell's `.tbo-kbd`).
const OMNIBOX_KBD: &str = "Super K";
/// `.tbo-kbd { font-size: 11px }`.
const KBD_PX: f32 = 11.0;
/// The shell's five primary destinations (`.top-bar-nav .tb-tab`), in its order.
const NAV_TABS: [&str; 5] = ["Home", "Agents", "Apps", "Hive", "Earn"];
/// Which tab reads as current. The native scene only lays out the HOME canvas, so home
/// IS the active destination; this becomes state the moment a tab can navigate.
const ACTIVE_TAB: usize = 0;
/// `.tb-tab { font-size: 13px }`, and `.top-bar-nav { gap: 2px }` between them.
const TAB_PX: f32 = 13.0;
const TAB_GAP: f32 = 2.0;
/// TWO measurements under one name, which is the `EDGE_PAD` shape again and is NOT fixed
/// here because unpicking it is a visual call the box has to settle.
///
/// It is (a) the home orb's slot, which the shell sizes at `.hart-hero-orbwrap`'s 300px,
/// and (b) the vertical budget the hero takes before the rows start, which the shell does
/// not have as a number at all: `.hh-hero` is `flex: 0 0 auto` so its height is whatever
/// its content comes to, `.hh-rows` is `flex: 1 1 auto` taking the rest, and the ORB is
/// not in that flow (it floats above the home at z 1450). Here the orb slot is subtracted
/// from the hero's text width AND the same number is what the rows start below, so the
/// orb pays for itself twice.
///
/// Setting this to the shell's 300 in place would therefore cost 100px of row budget that
/// the shell never spends, and a row that does not fit is dropped silently. The correct
/// shape is a separate `HERO_ORB_D = 300` that floats, with the band's height coming from
/// where the hero's own content actually ends, and that changes what overlaps what.
const HERO_H: f32 = 200.0;
/// `.hh-eyebrow`.
const HERO_EYEBROW_PX: f32 = 16.0;
/// Both producers default the unit to this, so a payload that omits it still reads right.
const HERO_UNIT_FALLBACK: &str = "Spark";
/// `.hh-hero-meta`.
const HERO_META_PX: f32 = 15.0;
/// `.hh-btn { font-size: 18px; padding: 15px 26px; border-radius: 14px }`. The height is
/// the two paddings around one line of that type, which is what the box actually is.
const HERO_BTN_PX: f32 = 18.0;
const HERO_BTN_H: f32 = 15.0 * 2.0 + HERO_BTN_PX * 1.3;
const HERO_BTN_PAD_X: f32 = 26.0;
/// `.hh-row-title`, and the head's own `gap: 14px` / `margin-bottom: 12px`.
const ROW_LABEL_PX: f32 = 23.0;
const ROW_LABEL_H: f32 = ROW_LABEL_PX * 1.3 + 12.0;
/// `.hh-row-note` and `.hh-see-all`, both 15px: secondary to the label, a step smaller.
const ROW_NOTE_PX: f32 = 15.0;
const ROW_HEAD_GAP: f32 = 14.0;
/// The shell's own wording (hartHome.js `see.textContent`), not a paraphrase.
const SEE_ALL: &str = "See all";
/// `.hh-rows { gap: 18px }` between rows, `.hh-cards { gap: 18px }` between cards.
const ROW_GAP: f32 = 18.0;
/// `.hh-card { width: 258px }`. The height is responsive, so it lives in `HomeMetrics`.
const CARD_W: f32 = 258.0;
const CARD_GAP: f32 = 18.0;
/// `.hh-card-title` / `.hh-card-meta`, and the line box each needs.
const CARD_TITLE_PX: f32 = 18.0;
const CARD_META_PX: f32 = 13.0;
const CARD_TITLE_H: f32 = CARD_TITLE_PX * 1.3;
const CARD_META_H: f32 = CARD_META_PX * 1.3;
/// How far the title's baseline block sits above the card's bottom edge, leaving room for
/// the meta line and the progress bar beneath it.
const CARD_BODY_BOTTOM: f32 = 34.0;
/// `.hh-card-body`'s own left inset, and `.hh-card-ic`'s.
const CARD_PAD_X: f32 = 12.0;
const CARD_ICON_INSET_X: f32 = 14.0;
/// The small VERTICAL breathing room between the fixed strips and the content, which is
/// not the gutter: `.hh-hero` pads `6px` on top, `.hh-rows` `18px` top and `4px` bottom.
/// One value for both edges, because the native band has no scroller to absorb a
/// mismatch, and it stays small so a short screen keeps its last row.
const CONTENT_PAD_Y: f32 = 12.0;
/// 5px, the shell's own `.hh-card-prog { height: 5px }`.
const CARD_PROG_H: f32 = 5.0;
/// `.hh-card-badge` / `.hh-card-live`, both 12px on an 8px horizontal pad, and the live
/// tag's own `gap: 6px` before an 8px `.hh-dot`.
const CARD_CHIP_PX: f32 = 12.0;
const CARD_CHIP_H: f32 = 20.0;
const CARD_CHIP_PAD_X: f32 = 8.0;
/// The shell insets both the badge and the live tag 12px from the card's top right.
const CARD_CHIP_INSET: f32 = 12.0;
const CARD_LIVE_DOT: f32 = 8.0;
const CARD_LIVE_GAP: f32 = 6.0;
/// The shell's `.hh-card-ic`: a 34px rounded tile holding a 20px glyph.
const CARD_ICON_BOX: f32 = 34.0;
/// `.hh-card-ic .mi { font-size: 20px }`.
const CARD_ICON_PX: f32 = 20.0;
/// `.hh-rank-num`: a 116px numeral with a 3px stroke, overhanging its card.
const RANK_PX: f32 = 116.0;
/// `.hh-rank-num { -webkit-text-stroke: 3px }`.
const RANK_STROKE: f32 = 3.0;
/// `.hh-card.hh-ranked .hh-rank-inner`: the art box of a leaderboard card is a fixed
/// 174px wide, pinned to the card's right edge and full height, leaving the numeral the
/// gutter to its left. Everything the shell appends to a card goes INSIDE this box.
const RANK_INNER_W: f32 = 174.0;
/// The 1px rule the chrome strips draw along the edge that faces the desktop.
const CHROME_RULE: f32 = 1.0;

/// The most rows the home ever lays out (a2: "2-3 rows"), and so the most scroll
/// offsets there can be.
pub const MAX_ROWS: usize = 3;

/// How far each row is scrolled sideways, in logical px, 0 = showing its first card.
///
/// The checklist is explicit that this exists: a1 forbids the canvas page-scrolling and
/// a2 says in the same breath that "Netflix rows scroll HORIZONTALLY (sideways = native
/// / console-like)". The shell does it with `.hh-cards { overflow-x: auto }`. The native
/// scene did not, so a row simply CLIPPED: the sanitizer allows twelve cards a row, about
/// seven fit a 1920 screen, and the rest were unreachable rather than off-screen.
///
/// A fixed array rather than a map: there are at most three rows by construction, and
/// this is read on the layout path where an allocation would be the wrong shape.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct RowScroll {
    px: [f32; MAX_ROWS],
}

impl RowScroll {
    /// The offset for `row`, or 0 for a row index this cannot hold.
    pub fn get(&self, row: usize) -> f32 {
        self.px.get(row).copied().unwrap_or(0.0)
    }

    /// Scroll `row` by `delta` px, clamped to its own content.
    ///
    /// `content_w` is the total width the row's cards occupy and `view_w` what is visible.
    /// A row that fits has NOTHING to scroll and is pinned at 0, which is what keeps a
    /// short row from drifting sideways under a stray wheel event; a row that overflows
    /// stops exactly at its last card rather than scrolling into empty space.
    pub fn scroll(&mut self, row: usize, delta: f32, content_w: f32, view_w: f32) {
        if row >= MAX_ROWS || !delta.is_finite() {
            return;
        }
        let max = (content_w - view_w).max(0.0);
        self.px[row] = (self.px[row] + delta).clamp(0.0, max);
    }

    /// Re-clamp every row against fresh extents, for when the OUTPUT or the FEED changes
    /// under a scrolled row. Without this a row scrolled to its end and then given fewer
    /// cards would keep an offset past its own content and render as empty.
    pub fn reclamp(&mut self, extents: &[(f32, f32)]) {
        for (i, (content_w, view_w)) in extents.iter().take(MAX_ROWS).enumerate() {
            let max = (content_w - view_w).max(0.0);
            self.px[i] = self.px[i].clamp(0.0, max);
        }
        for i in extents.len()..MAX_ROWS {
            self.px[i] = 0.0;
        }
    }

    /// The width a row's cards occupy, including the gaps between them but not a trailing
    /// one, which is what `content_w` means everywhere above.
    pub fn content_width(cards: usize) -> f32 {
        if cards == 0 {
            return 0.0;
        }
        cards as f32 * CARD_W + (cards - 1) as f32 * CARD_GAP
    }
}
/// The card corner. `.hh-card` uses `var(--hart-radius, 16px)` and `.hh-rank-inner` a flat
/// 16px, so 16 is the shape both draw when no theme preset overrides the variable.
const CARD_RADIUS: f32 = 16.0;

/// The four numbers hartHome.css makes RESPONSIVE, resolved for one output size.
///
/// Everything else on this desktop is a fixed literal, but the shell shrinks the hero
/// figure and the cards on a small screen so the rows still fit without scrolling, and it
/// pulls the gutter in on a narrow one. A compositor that ignored those rules would lay
/// out a 1366x768 panel at desktop scale and push the last row off the bottom, which is
/// the same "fits one screen" promise this layout is built on.
struct HomeMetrics {
    /// `--hh-gutter`: the CONTENT inset, left of the hero and the rows.
    gutter: f32,
    /// `.hh-amount` and `.hh-amount-unit`.
    amount_px: f32,
    unit_px: f32,
    /// `.hh-card` height.
    card_h: f32,
    /// `.tb-tab` horizontal padding.
    tab_pad_x: f32,
    /// `.top-bar-omni` min-width: how far the pill may be squeezed before the tabs stop
    /// getting the room instead.
    omnibox_min_w: f32,
    /// Whether the pill still shows `.tbo-kbd`, its shortcut hint.
    show_kbd: bool,
    /// How many of `NAV_TABS` are shown at all. The shell hides the last two on a narrow
    /// bar, which is a different thing from the width check that drops a tab that would
    /// collide with the pill: this one hides them even when they WOULD fit.
    nav_tabs: usize,
}

impl HomeMetrics {
    /// Resolve for an output, applying the shell's four media queries IN ITS ORDER, so a
    /// screen matching several takes the later block's value exactly as the cascade does.
    fn for_output(output_w: f32, output_h: f32) -> HomeMetrics {
        let mut m = HomeMetrics {
            gutter: 60.0,
            amount_px: 88.0,
            unit_px: 26.0,
            card_h: 150.0,
            tab_pad_x: 13.0,
            omnibox_min_w: 220.0,
            show_kbd: true,
            nav_tabs: NAV_TABS.len(),
        };
        // @media (max-width: 1100px): the pill gives up its hint and most of its floor,
        // and the tabs tighten, so the bar keeps all five destinations for longer.
        if output_w <= 1100.0 {
            m.omnibox_min_w = 120.0;
            m.show_kbd = false;
            m.tab_pad_x = 9.0;
        }
        // @media (max-width: 880px): Hive and Earn go, in that order from the right.
        if output_w <= 880.0 {
            m.nav_tabs = NAV_TABS.len() - 2;
        }
        // @media (max-width: 1400px)
        if output_w <= 1400.0 {
            m.amount_px = 70.0;
            m.unit_px = 22.0;
            m.gutter = 40.0;
        }
        // @media (max-height: 820px)
        if output_h <= 820.0 {
            m.amount_px = 58.0;
            m.card_h = 132.0;
        }
        m
    }
}

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
    scroll: &RowScroll,
    measure: &mut dyn TextMeasure,
) -> SceneNode {
    let mut root: Vec<SceneNode> = Vec::new();
    // Asked ONCE for the whole layout rather than per glyph: it walks the font database,
    // and the answer cannot change within a single layout pass. Every icon on this
    // desktop is a ligature name, so this one bool decides whether ANY of them draw.
    let icons_available = measure.has_icon_face();
    // The shell's two media queries, resolved ONCE for this output (see `HomeMetrics`).
    let m = HomeMetrics::for_output(output_w, output_h);

    // ── Top bar (fixed, 40px): background, centre omnibox pill, right orb-sm. ──
    let bar = Rect::new(0.0, 0.0, output_w, theme.top_bar_h);
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
    // `.top-bar-omni { min-width: 220px; max-width: 360px }` in a flex row: it takes the
    // max where there is room and is squeezed no further than the min. Centring it on the
    // output is what the flex centre column resolves to on a bar this simple.
    let pill_w = OMNIBOX_W.min(output_w).max(m.omnibox_min_w.min(output_w));
    let pill = Rect::new((output_w - pill_w) * 0.5, 6.0, pill_w, theme.top_bar_h - 12.0);
    let mark_h = WORDMARK_PX * 1.3;
    let mark_y = (theme.top_bar_h - mark_h) * 0.5;
    let hart_w = measure.text_width("HART", WORDMARK_PX);
    let gap_w = measure.text_width(" ", WORDMARK_PX);
    let os_w = measure.text_width("OS", WORDMARK_PX);
    // A shaped run needs its whole advance to fit the buffer it is composed into, so the
    // box is the measured width rounded up with a pixel of slack rather than trusting an
    // exact float to survive the f32 -> i32 the lowering does.
    bar_children.push(SceneNode::Text {
        rect: Rect::new(BAR_PAD_X, mark_y, hart_w.ceil() + 2.0, mark_h),
        text: "HART".to_string(),
        size_px: WORDMARK_PX,
        color: theme.accent,
        stroke: 0.0,
    });
    bar_children.push(SceneNode::Text {
        rect: Rect::new(
            BAR_PAD_X + hart_w + gap_w,
            mark_y,
            os_w.ceil() + 2.0,
            mark_h,
        ),
        text: "OS".to_string(),
        size_px: WORDMARK_PX,
        color: theme.accent2,
        stroke: 0.0,
    });

    // ── Nav tabs (P5): the shell's five primary destinations, each sized to its own
    //    label, which is the second thing the measure buys. A tab is emitted only while
    //    it fits BEFORE the omnibox pill, the same discipline the card rows use for the
    //    taskbar, so a narrow output drops tabs from the right instead of drawing them
    //    under the pill. They are deliberately NOT hover targets: nothing routes a tab
    //    activation yet, and an affordance that reacts but does nothing is a lie. Wrap
    //    them as interactive groups when a tab actually navigates.
    let tab_h = TAB_PX * 2.0;
    let tab_y = (theme.top_bar_h - tab_h) * 0.5;
    let tab_ink_h = TAB_PX * 1.3;
    let tab_ink_y = (theme.top_bar_h - tab_ink_h) * 0.5;
    let mut tab_x = BAR_PAD_X + hart_w + gap_w + os_w + BAR_PAD_X;
    for (i, label) in NAV_TABS.iter().take(m.nav_tabs).enumerate() {
        let ink_w = measure.text_width(label, TAB_PX);
        let slot = ink_w.ceil() + 2.0 * m.tab_pad_x;
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
            rect: Rect::new(tab_x + m.tab_pad_x, tab_ink_y, ink_w.ceil() + 2.0, tab_ink_h),
            text: label.to_string(),
            size_px: TAB_PX,
            color: if i == ACTIVE_TAB {
                theme.bar_ink
            } else {
                theme.omnibox_ink
            },
            stroke: 0.0,
        });
        tab_x += slot + TAB_GAP;
    }

    // The pill and the three runs inside it are ONE surface (`.top-bar-omni`), so they
    // are one group. They used to be four siblings of the bar's other children, which is
    // why the omnibox could not be named even though the latency budget table has a row
    // for it; collecting them here costs one node and makes the pill addressable.
    let mut pill_children = vec![SceneNode::Rect {
        rect: pill,
        color: theme.omnibox_bg,
        radius: (theme.top_bar_h - 12.0) * 0.5,
    }];
    // Inside the pill, the shell's own three parts: a search glyph, the prompt, and the
    // shortcut hint pushed to the far end. The hint is right-anchored, which is the
    // measure again; before it there was nowhere to put it.
    let pill_ink_y = (theme.top_bar_h - OMNIBOX_PX * 1.3) * 0.5;
    let mut pill_x = pill.x + 12.0;
    if icons_available {
        let gw = measure.text_width(OMNIBOX_GLYPH, OMNIBOX_PX);
        pill_children.push(SceneNode::Text {
            rect: Rect::new(pill_x, pill_ink_y, gw.ceil() + 2.0, OMNIBOX_PX * 1.3),
            text: OMNIBOX_GLYPH.to_string(),
            size_px: OMNIBOX_PX,
            color: theme.omnibox_ink,
            stroke: 0.0,
        });
        pill_x += gw + 8.0;
    }
    pill_children.push(SceneNode::Text {
        rect: Rect::new(
            pill_x,
            pill_ink_y,
            (pill.right() - 12.0 - pill_x).max(0.0),
            OMNIBOX_PX * 1.3,
        ),
        text: "Ask or search anything".to_string(),
        size_px: OMNIBOX_PX,
        color: theme.omnibox_ink,
        stroke: 0.0,
    });
    let kbd_w = measure.text_width(OMNIBOX_KBD, KBD_PX);
    let kbd_x = pill.right() - 12.0 - kbd_w;
    if m.show_kbd && kbd_x > pill_x {
        pill_children.push(SceneNode::Text {
            rect: Rect::new(
                kbd_x,
                (theme.top_bar_h - KBD_PX * 1.3) * 0.5,
                kbd_w.ceil() + 2.0,
                KBD_PX * 1.3,
            ),
            text: OMNIBOX_KBD.to_string(),
            size_px: KBD_PX,
            color: theme.omnibox_ink,
            stroke: 0.0,
        });
    }
    // `.top-bar { border-bottom: 1px solid var(--hart-glass-border) }`, and nothing else:
    // the rule explicitly sets `border-top: 0` and `border-radius: 0`, so the bar has ONE
    // edge. Without it the strip simply ends wherever its translucency stops, which is
    // the difference between chrome that sits on the desktop and chrome that dissolves
    // into it.
    bar_children.push(SceneNode::Rect {
        rect: Rect::new(
            0.0,
            theme.top_bar_h - theme.chrome_rule_px,
            output_w,
            theme.chrome_rule_px,
        ),
        color: theme.chrome_border,
        radius: 0.0,
    });
    bar_children.push(SceneNode::Container {
        rect: pill,
        // Not a hover target: nothing routes an omnibox activation yet, and an
        // affordance that reacts but does nothing is a lie (the same rule the nav tabs
        // follow). It is a named SURFACE either way, which is what attribution needs.
        interactive: false,
        component: Some(Component::Omnibox),
        children: pill_children,
    });

    // ── The bar's right cluster, laid out from the RIGHT EDGE inward so it stays put as
    //    the output widens: tray glyphs, then the avatar, then the orb-sm, which is the
    //    shell's order read right to left. The clock sits outermost in the shell and is
    //    absent here: it needs a time source the scene has no input for, and reserving a
    //    slot for something that never draws would leave a hole in the cluster.
    let mut right_x = output_w - BAR_PAD_X;
    if icons_available {
        for glyph in TRAY_GLYPHS.iter().rev() {
            right_x -= TRAY_BTN;
            let b = centered_box(
                measure.text_width(glyph, theme.icon_px),
                right_x,
                TRAY_BTN,
                (theme.top_bar_h - theme.icon_px * 1.3) * 0.5,
                theme.icon_px * 1.3,
            );
            bar_children.push(SceneNode::Text {
                rect: b,
                text: (*glyph).to_string(),
                size_px: theme.icon_px,
                color: theme.omnibox_ink,
                stroke: 0.0,
            });
            right_x -= TRAY_GAP;
        }
    }
    // Avatar: a filled disc with the account initial. The shell hardcodes the letter in
    // its markup, so this is the same letter, not a guess at a user's name.
    right_x -= AVATAR_D;
    let av = Rect::new(right_x, (theme.top_bar_h - AVATAR_D) * 0.5, AVATAR_D, AVATAR_D);
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
            (theme.top_bar_h - AVATAR_PX * 1.3) * 0.5,
            AVATAR_PX * 1.3,
        ),
        text: AVATAR_INITIAL.to_string(),
        size_px: AVATAR_PX,
        color: theme.bar_ink,
        stroke: 0.0,
    });
    right_x -= TRAY_GAP;

    right_x -= ORB_SM;
    let orb_sm_rect = Rect::new(right_x, (theme.top_bar_h - ORB_SM) * 0.5, ORB_SM, ORB_SM);
    bar_children.push(SceneNode::OrbSlot {
        rect: orb_sm_rect,
        compact: true,
    });
    root.push(SceneNode::Container {
        rect: bar,
        interactive: false,
        component: Some(Component::TopBar),
        children: bar_children,
    });

    // ── Content band, between the two fixed strips. ──
    // `--hh-gutter` is a LEFT inset on both `.hh-hero` and `.hh-rows`; the rows carry
    // `padding-right: 0` and their cards bleed to the viewport edge under an overflow
    // scroller, so the band's right edge is the output's. Insetting both sides by the
    // gutter would drop a card the shell shows.
    let content = Rect::new(
        m.gutter,
        theme.top_bar_h + CONTENT_PAD_Y,
        (output_w - m.gutter).max(0.0),
        (output_h - theme.top_bar_h - TASKBAR_H - 2.0 * CONTENT_PAD_Y).max(0.0),
    );

    // ── Hero (P4, the EARNINGS hero): eyebrow, the big Spark figure with its unit, an
    //    honest meta strip, then the two calls to action. The large orb floats right (c7).
    //    Laid out top down from the content band, each part skipped when the payload has
    //    nothing for it, so a hero without an amount is short rather than gappy.
    let orb_home = HERO_H.min(content.h).max(0.0);
    let hero_text_w = (content.w - orb_home - m.gutter).max(0.0);
    let mut hero_y = content.y;
    if !home.hero.eyebrow.is_empty() {
        root.push(SceneNode::Text {
            rect: Rect::new(content.x, hero_y, hero_text_w, HERO_EYEBROW_PX * 1.4),
            text: home.hero.eyebrow.clone(),
            size_px: HERO_EYEBROW_PX,
            color: theme.hero_copy,
            stroke: 0.0,
        });
        hero_y += HERO_EYEBROW_PX * 1.6;
    }
    if let Some(amount) = home.hero.amount {
        // The number and its unit are two runs on one line, the unit sitting right after
        // the figure ends. That is the measure again: the figure's width is not known
        // until it is shaped, and it changes with the balance.
        let figure = amount.to_string();
        let fw = measure.text_width(&figure, m.amount_px);
        root.push(SceneNode::Text {
            rect: Rect::new(content.x, hero_y, fw.ceil() + 2.0, m.amount_px * 1.3),
            text: figure,
            size_px: m.amount_px,
            color: theme.hero_title,
            stroke: 0.0,
        });
        let unit = if home.hero.amount_unit.is_empty() {
            HERO_UNIT_FALLBACK
        } else {
            &home.hero.amount_unit
        };
        let uw = measure.text_width(unit, m.unit_px);
        root.push(SceneNode::Text {
            rect: Rect::new(
                content.x + fw + 8.0,
                // Sat on the figure's baseline rather than its box top, so the unit reads
                // as part of the number instead of floating above it.
                hero_y + (m.amount_px - m.unit_px) * 0.9,
                uw.ceil() + 2.0,
                m.unit_px * 1.3,
            ),
            text: unit.to_string(),
            size_px: m.unit_px,
            color: theme.accent,
            stroke: 0.0,
        });
        hero_y += m.amount_px * 1.35;
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
            stroke: 0.0,
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
            stroke: 0.0,
        });
        cta_x += bw + 10.0;
    }
    root.push(SceneNode::OrbSlot {
        rect: Rect::new(content.right() - orb_home, content.y, orb_home, orb_home),
        compact: false,
    });

    // ── Rows: cap at 3 (a2 "2-3 rows"), each a label + a strip of cards, emitted only
    //    while they fit inside the content band so the desktop never scrolls. ──
    let mut cursor_y = content.y + HERO_H + CONTENT_PAD_Y;
    for (row_index, row) in home.rows.iter().take(3).enumerate() {
        let row_block_h = ROW_LABEL_H + m.card_h;
        if cursor_y + row_block_h > content.bottom() {
            break;
        }
        // ── Row header: label, an optional note beside it, and an optional right-anchored
        //    "See all". The label keeps a measured box now rather than the whole row width,
        //    so the note can sit AFTER it; right-anchoring the See-all is the third thing
        //    the measure buys, and it is the reason TextAlign::Right was never needed:
        //    knowing the width lets layout place a left-aligned run exactly.
        // The row's hue. It tints exactly what the shell tints with it: this row's note
        // and its cards' progress fills (.hh-row-note and .hh-card-prog both read
        // --hh-acc). Without it every row was teal and a stack of them read flat.
        let row_accent = theme.row_accent(row.accent.as_deref(), row_index);
        // `.hh-row` is a real element in the shell and the budget table has a `home-row`
        // entry for it; the native scene had neither, so every part of a row was a
        // root-level leaf and a wheel event had nothing to land on. Collecting them is
        // what lets a scroll find its row without re-deriving the layout's geometry.
        let mut row_children: Vec<SceneNode> = Vec::new();
        let label_w = measure.text_width(&row.title, ROW_LABEL_PX);
        row_children.push(SceneNode::Text {
            rect: Rect::new(content.x, cursor_y, label_w.ceil() + 2.0, ROW_LABEL_H),
            text: row.title.clone(),
            size_px: ROW_LABEL_PX,
            color: theme.card_ink,
            stroke: 0.0,
        });
        if let Some(note) = &row.note {
            let note_w = measure.text_width(note, ROW_NOTE_PX);
            let note_x = content.x + label_w + ROW_HEAD_GAP;
            if note_x + note_w <= content.right() {
                row_children.push(SceneNode::Text {
                    rect: Rect::new(note_x, cursor_y, note_w.ceil() + 2.0, ROW_LABEL_H),
                    text: note.clone(),
                    size_px: ROW_NOTE_PX,
                    color: row_accent,
                    stroke: 0.0,
                });
            }
        }
        if row.see_all.is_some() {
            let see_w = measure.text_width(SEE_ALL, ROW_NOTE_PX);
            let see_x = content.right() - see_w;
            // Only if it clears the label (and any note): a cramped row drops the
            // affordance rather than overlapping the text it belongs to.
            if see_x > content.x + label_w + ROW_HEAD_GAP {
                row_children.push(SceneNode::Text {
                    rect: Rect::new(see_x, cursor_y, see_w.ceil() + 2.0, ROW_LABEL_H),
                    text: SEE_ALL.to_string(),
                    size_px: ROW_NOTE_PX,
                    color: theme.accent,
                    stroke: 0.0,
                });
            }
        }
        let cards_y = cursor_y + ROW_LABEL_H;
        // The row's strip is scrolled sideways by its own offset (a2: "Netflix rows
        // scroll HORIZONTALLY"). Cards are laid out from their true positions and the
        // ones that fall outside the band are skipped, so a scrolled row costs the same
        // as an unscrolled one: the offset moves the WINDOW, it does not move a list.
        let scroll_x = scroll.get(row_index);
        let mut card_x = content.x - scroll_x;
        for (card_index, card) in row.cards.iter().enumerate() {
            // Past the right edge: everything after this is too, so stop.
            if card_x >= content.right() {
                break;
            }
            // Scrolled off the left: advance without emitting. Not `continue` before the
            // advance, or every later card would pile up at the same x.
            if card_x + CARD_W <= content.x {
                card_x += CARD_W + CARD_GAP;
                continue;
            }
            let cr = Rect::new(card_x, cards_y, CARD_W, m.card_h);
            // A ranked card has NO tile: `.hh-card.hh-ranked` is background:transparent,
            // border:none, so the numeral and the art ARE the whole card. It gets no
            // background node at all now that its art tile can anchor the hover.
            let mut card_children: Vec<SceneNode> = Vec::new();
            if !row.ranked {
                card_children.push(SceneNode::Rect {
                    rect: cr,
                    color: theme.card_bg,
                    radius: theme.card_radius,
                });
            }
            if row.ranked {
                // The rank numeral, overhanging the card's bottom-left exactly as the
                // shell places it, drawn as an OUTLINE because that is what the shell
                // draws: transparent fill, 3px stroke. Its own box, not the card's, so a
                // two-digit rank is not clipped at ten.
                let label = (card_index + 1).to_string();
                let nw = measure.text_width(&label, RANK_PX);
                card_children.push(SceneNode::Text {
                    rect: Rect::new(
                        cr.x - 6.0,
                        cr.bottom() + 14.0 - RANK_PX * 1.3,
                        nw.ceil() + 2.0,
                        RANK_PX * 1.3,
                    ),
                    text: label,
                    size_px: RANK_PX,
                    color: theme.rank_ink,
                    stroke: RANK_STROKE,
                });
            }
            // ── The art tile, which EVERY card has, and which is also the box every other
            //    thing on the card is positioned in: the shell appends the glyph, the
            //    live/badge chip, the title+meta body and the progress bar to `artWrap`,
            //    NOT to the card. On an ordinary card `.hh-card-art` is `inset: 0`, so the
            //    two boxes coincide. On a ranked one `artWrap` is `.hh-rank-inner`, a fixed
            //    174px box pinned to the card's right edge, full height, with the numeral
            //    overhanging the gutter to its left. Positioning the content against the
            //    CARD instead put all of it 84px left of where the shell puts it, and sized
            //    the title and the progress bar to the wrong width.
            let ab = if row.ranked {
                let w = RANK_INNER_W.min(cr.w);
                Rect::new(cr.right() - w, cr.y, w, cr.h)
            } else {
                cr
            };
            let (art_from, art_to, art_angle) =
                theme.art_stops(row.accent.as_deref(), row_index, card_index);
            card_children.push(SceneNode::Art {
                rect: ab,
                from: art_from,
                to: art_to,
                angle_deg: art_angle,
                radius: theme.card_radius,
                photo: card.photo.clone(),
            });
            // ── Icon glyph, top left, and ONLY when the card has no photo: the shell draws
            //    it as `card.icon && !hasImage`, because the glyph is the stand-in FOR the
            //    missing picture, not a decoration beside one. It is a ligature name in a
            //    Material face, so it goes down the ordinary text path; `icons_available`
            //    is what stops it rendering as the literal word when the face is absent.
            if let (Some(name), None, true) = (&card.icon, &card.photo, icons_available) {
                card_children.push(SceneNode::Rect {
                    rect: Rect::new(
                        ab.x + CARD_ICON_INSET_X,
                        ab.y + CARD_CHIP_INSET,
                        CARD_ICON_BOX,
                        CARD_ICON_BOX,
                    ),
                    color: theme.chip_bg,
                    radius: 10.0,
                });
                card_children.push(SceneNode::Text {
                    rect: centered_box(
                        measure.text_width(name, CARD_ICON_PX),
                        ab.x + CARD_ICON_INSET_X,
                        CARD_ICON_BOX,
                        ab.y + CARD_CHIP_INSET + (CARD_ICON_BOX - CARD_ICON_PX * 1.3) * 0.5,
                        CARD_ICON_PX * 1.3,
                    ),
                    text: name.clone(),
                    size_px: CARD_ICON_PX,
                    color: theme.card_ink,
                    stroke: 0.0,
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
                let chip_x = ab.right() - CARD_CHIP_INSET - chip_w;
                let chip_y = ab.y + CARD_CHIP_INSET;
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
                    stroke: 0.0,
                });
            }

            // Title, then the meta line under it, then the progress bar pinned to the art
            // box's bottom edge: the shell's own body order (hh-card-title, hh-card-meta,
            // hh-card-prog). The title sits a line higher when there is a meta to carry,
            // so the pair stays inside the box rather than the meta hanging off it.
            let has_meta = card.meta.is_some();
            let title_y = if has_meta {
                ab.bottom() - CARD_BODY_BOTTOM - CARD_META_H
            } else {
                ab.bottom() - CARD_BODY_BOTTOM
            };
            card_children.push(SceneNode::Text {
                rect: Rect::new(
                    ab.x + CARD_PAD_X,
                    title_y,
                    ab.w - 2.0 * CARD_PAD_X,
                    CARD_TITLE_H,
                ),
                text: card.title.clone(),
                size_px: CARD_TITLE_PX,
                color: theme.card_ink,
                stroke: 0.0,
            });
            if let Some(meta) = &card.meta {
                card_children.push(SceneNode::Text {
                    rect: Rect::new(
                        ab.x + CARD_PAD_X,
                        title_y + CARD_TITLE_H,
                        ab.w - 2.0 * CARD_PAD_X,
                        CARD_META_H,
                    ),
                    text: meta.clone(),
                    size_px: CARD_META_PX,
                    color: theme.hero_copy,
                    stroke: 0.0,
                });
            }
            if let Some(p) = card.progress {
                // A completion bar, so ZERO must read as an empty track rather than as no
                // bar at all: a card at 0% and a card with no progress are different
                // states, and collapsing them would silently lose one.
                card_children.push(SceneNode::Rect {
                    rect: Rect::new(ab.x, ab.bottom() - CARD_PROG_H, ab.w, CARD_PROG_H),
                    color: theme.omnibox_bg,
                    radius: 0.0,
                });
                let filled = ab.w * p.clamp(0.0, 1.0);
                if filled >= 1.0 {
                    card_children.push(SceneNode::Rect {
                        rect: Rect::new(ab.x, ab.bottom() - CARD_PROG_H, filled, CARD_PROG_H),
                        color: row_accent,
                        radius: 0.0,
                    });
                }
            }
            row_children.push(SceneNode::Container {
                rect: cr,
                // A card is the one thing on this desktop the cursor reacts to, and the
                // background rect pushed FIRST above is what the hover lifts.
                interactive: true,
                component: Some(Component::HomeCard),
                children: card_children,
            });
            card_x += CARD_W + CARD_GAP;
        }
        // The band the wheel lands on: the row's own strip, header included, spanning the
        // content width. Not interactive, because a row is not a hover target in the
        // shell either; its CARDS are, and they are its children.
        root.push(SceneNode::Container {
            rect: Rect::new(content.x, cursor_y, content.w, row_block_h),
            interactive: false,
            component: Some(Component::HomeRow(row_index)),
            children: row_children,
        });
        cursor_y += row_block_h + ROW_GAP;
    }

    // ── Taskbar (fixed, 44px, bottom). ──
    let taskbar = Rect::new(0.0, output_h - TASKBAR_H, output_w, TASKBAR_H);
    root.push(SceneNode::Container {
        rect: taskbar,
        interactive: false,
        component: Some(Component::Taskbar),
        children: vec![
            SceneNode::Rect {
                rect: taskbar,
                color: theme.taskbar_bg,
                radius: 0.0,
            },
            // `.taskbar { border-top: 1px solid var(--hart-glass-border) }`: the mirror of
            // the top bar's rule, on the edge that faces the desktop.
            SceneNode::Rect {
                rect: Rect::new(taskbar.x, taskbar.y, taskbar.w, theme.chrome_rule_px),
                color: theme.chrome_border,
                radius: 0.0,
            },
        ],
    });

    SceneNode::Container {
        rect: Rect::new(0.0, 0.0, output_w, output_h),
        interactive: false,
        component: None,
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
    key_scroll: RowScroll,
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
        scroll: &RowScroll,
        measure: &mut dyn TextMeasure,
    ) -> &SceneNode {
        // The scroll offset IS a layout key, unlike the pointer: hovering changes which
        // leaf lights up and rebuilds nothing, but scrolling moves where the cards are.
        // A rebuild per wheel event is the honest cost of that and is what the retained
        // tree is for the rest of the time.
        let stale = self.tree.is_none()
            || self.key_w != w
            || self.key_h != h
            || self.key_theme != Some(*theme)
            || self.key_scroll != *scroll
            || self.key_home != *home;
        if stale {
            self.tree = Some(layout_home(w, h, home, theme, scroll, measure));
            self.key_w = w;
            self.key_h = h;
            self.key_theme = Some(*theme);
            self.key_scroll = *scroll;
            self.key_home = home.clone();
            self.rebuilds += 1;
        }
        self.tree
            .as_ref()
            .expect("the tree was just built when it was stale")
    }

    /// The retained tree, if one has been built. Read-only and borrow-free of the
    /// caches, so the INPUT path can ask what a point is over without touching the
    /// buffers the render path owns. None before the first frame, which is the honest
    /// answer: nothing has been laid out, so nothing can be named.
    pub fn tree(&self) -> Option<&SceneNode> {
        self.tree.as_ref()
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
                        // `image` then `image_url`, the shell's own `imgSrc` priority. Both,
                        // because the second is what a news or app card actually carries
                        // and reading only the first made every one of them look photo-less
                        // (and so drew the icon glyph the shell suppresses). Empty strings
                        // are filtered like every other optional text key here: a present
                        // but blank source is not a picture.
                        photo: c
                            .get("image")
                            .and_then(Value::as_str)
                            .filter(|t| !t.is_empty())
                            .or_else(|| {
                                c.get("image_url")
                                    .and_then(Value::as_str)
                                    .filter(|t| !t.is_empty())
                            })
                            .map(str::to_string),
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
                ranked: r.get("ranked").and_then(Value::as_bool).unwrap_or(false),
                accent: r
                    .get("accent")
                    .and_then(Value::as_str)
                    .filter(|t| !t.is_empty())
                    .map(str::to_string),
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
                    ranked: false,
                    accent: Some("magenta".into()),
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
                            photo: None,
                        },
                        Card {
                            title: "Recipe B".into(),
                            meta: None,
                            progress: None,
                            icon: None,
                            badge: None,
                            live: None,
                            photo: Some("b.png".into()),
                        },
                    ],
                },
                Row {
                    title: "For you".into(),
                    ranked: true,
                    accent: None,
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
        let root = layout_home(1600.0, 900.0, &sample(), &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
        let bar = root.hit_test(800.0, 5.0).expect("a node at the top strip");
        // The topmost hit in the bar band is a bar child, and the bar rect is 40px.
        assert!(bar.rect().y < TOP_BAR_H);
    }

    #[test]
    fn taskbar_is_the_fixed_44px_strip_at_the_bottom() {
        let (w, h) = (1600.0, 900.0);
        let root = layout_home(w, h, &sample(), &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
        let hit = root.hit_test(w * 0.5, h - 2.0).expect("a node at the bottom strip");
        assert!((hit.rect().h - TASKBAR_H).abs() < 0.01);
        assert!((hit.rect().y - (h - TASKBAR_H)).abs() < 0.01);
    }

    #[test]
    fn home_orb_floats_to_the_right_of_the_hero() {
        let (w, h) = (1600.0, 900.0);
        let root = layout_home(w, h, &sample(), &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
        // The large orb (compact=false) sits in the right portion of the content band.
        let mut orb_x = None;
        {
            let mut all: Vec<&SceneNode> = Vec::new();
            walk_groups(&root, &mut all);
            for c in all {
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
        let root = layout_home(1600.0, 320.0, &sample(), &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
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
        {
            let mut all: Vec<&SceneNode> = Vec::new();
            walk_groups(&root, &mut all);
            for c in all {
                if c.rect().y >= TOP_BAR_H {
                    assert_within(c, bottom);
                }
            }
        }
    }

    #[test]
    fn flatten_yields_leaves_in_paint_order_no_containers() {
        let root = layout_home(1600.0, 900.0, &sample(), &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
        let mut leaves = Vec::new();
        root.flatten(&mut leaves);
        // No Container survives the flatten.
        assert!(leaves.iter().all(|n| !matches!(n, SceneNode::Container { .. })));
        // First painted leaf is the top-bar background rect (back of the paint order).
        assert!(matches!(leaves.first(), Some(SceneNode::Rect { rect, .. }) if rect.y == 0.0));
        // Last painted leaf is the taskbar's 1px rule, which is drawn ON TOP of the strip
        // itself: `.taskbar { border-top: 1px solid var(--hart-glass-border) }` is the
        // edge that faces the desktop, so it is the front-most thing in the whole scene.
        assert!(matches!(leaves.last(), Some(SceneNode::Rect { rect, .. })
                         if (rect.h - CHROME_RULE).abs() < 0.01));
        // And the strip itself is right behind it.
        let strip = leaves[leaves.len() - 2];
        assert!(matches!(strip, SceneNode::Rect { rect, .. }
                         if (rect.h - TASKBAR_H).abs() < 0.01));
        assert!(leaves.len() >= 6);
    }

    #[test]
    fn the_leaf_walk_and_the_hover_index_share_one_index_space() {
        // The card highlight works by comparing `hover_leaf`'s index against the index the
        // lowering's walk hands it. Those are two different traversals, so if they ever
        // disagreed the wrong node would light up, and nothing else would catch it. This
        // pins them together.
        let root = layout_home(1600.0, 900.0, &sample(), &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);

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
        {
            let mut all: Vec<&SceneNode> = Vec::new();
            walk_groups(&root, &mut all);
            for c in all {
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
    fn a_mistyped_palette_literal_costs_a_colour_not_the_desktop() {
        // Theme construction runs inside the process that owns scanout, so a bad hex must
        // degrade rather than unwind. The fallback is deliberately a VISIBLE neutral and
        // never transparent: a colour that vanishes hides the mistake.
        let fallback = Color::rgba(0.5, 0.5, 0.5, 1.0);
        assert_eq!(palette("#00E6C3", fallback), Color::from_hex("#00E6C3").unwrap());
        assert_eq!(palette("not-a-colour", fallback), fallback);
        assert_eq!(palette("", fallback), fallback);
        assert!(palette("#zzz", fallback).a > 0.0, "the fallback must be visible");
        // And the shipped theme really is the shell's palette, not a wall of fallbacks.
        let t = Theme::cosmic_default();
        assert_ne!(t.accent, fallback);
        assert_ne!(t.accent2, fallback);
        assert!(t.spectrum.iter().all(|c| *c != fallback), "every spectrum hue parsed");
        // The six are distinct, which is the whole point of rotating through them.
        for i in 0..t.spectrum.len() {
            for j in (i + 1)..t.spectrum.len() {
                assert_ne!(t.spectrum[i], t.spectrum[j], "spectrum {i} and {j} collide");
            }
        }
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
        let root = layout_home(1600.0, 900.0, &sample(), &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
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
    fn a_rows_accent_tints_its_note_and_its_cards_progress() {
        let theme = Theme::cosmic_default();
        let root = layout_home(1600.0, 900.0, &sample(), &theme, &RowScroll::default(), &mut MonoMeasure);
        // The sample's first row names magenta; its note must carry that hue, not the
        // functional teal every row used to get.
        let magenta = theme.spectrum_named("magenta").expect("magenta is in the spectrum");
        let mut note_color = None;
        root.for_each_leaf(&mut |_, leaf| {
            if let SceneNode::Text { text, color, .. } = leaf {
                if text == "3 in progress" {
                    note_color = Some(*color);
                }
            }
        });
        assert_eq!(note_color, Some(magenta), "the note takes the ROW's accent");

        // And the progress FILL in that row, which is the other thing --hh-acc paints.
        let mut groups: Vec<&SceneNode> = Vec::new();
        walk_groups(&root, &mut groups);
        let fills: Vec<Color> = {
            groups
                .iter()
                .copied()
                .filter_map(|c| match c {
                    SceneNode::Container {
                        interactive: true,
                        children,
                        ..
                    } => children.iter().find_map(|n| match n {
                        SceneNode::Rect { rect, color, .. }
                            if (rect.h - CARD_PROG_H).abs() < 0.01 && *color != theme.omnibox_bg =>
                        {
                            Some(*color)
                        }
                        _ => None,
                    }),
                    _ => None,
                })
                .collect()
        };
        assert!(!fills.is_empty(), "the sample has a progress fill");
        assert!(fills.iter().all(|c| *c == magenta), "fills take the row accent");
    }

    #[test]
    fn a_row_without_an_accent_rotates_by_position_rather_than_repeating() {
        // The shell falls back to spec[idx % len], which is what stops a stack of rows
        // all reading teal. A fixed default would lose exactly that.
        let theme = Theme::cosmic_default();
        assert_eq!(theme.row_accent(None, 0), theme.spectrum[0]);
        assert_eq!(theme.row_accent(None, 1), theme.spectrum[1]);
        assert_ne!(theme.row_accent(None, 0), theme.row_accent(None, 1));
        // Past the end it wraps rather than panicking on a fourth row.
        assert_eq!(theme.row_accent(None, 7), theme.spectrum[1]);
        // A named accent wins over position, and an unknown name falls back to position
        // rather than to a hardcoded hue.
        assert_eq!(theme.row_accent(Some("amber"), 0), theme.spectrum[5]);
        assert_eq!(theme.row_accent(Some("chartreuse"), 2), theme.spectrum[2]);
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
        let root = layout_home(1600.0, 900.0, &hc, &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
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
        assert_eq!(hc.rows[0].cards[0].photo.as_deref(), Some("a.png"));
        assert_eq!(hc.mood.as_deref(), Some("cosmic"));
    }

    #[test]
    fn the_wordmark_butts_its_two_runs_together_using_the_measure() {
        let root = layout_home(1600.0, 900.0, &sample(), &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
        let theme = Theme::cosmic_default();
        // Both runs live in the top bar, in reading order.
        let mut hart = None;
        let mut os = None;
        {
            let mut all: Vec<&SceneNode> = Vec::new();
            walk_groups(&root, &mut all);
            for c in all {
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
        {
            let mut all: Vec<&SceneNode> = Vec::new();
            walk_groups(&root, &mut all);
            for c in all {
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
                if let SceneNode::Container { rect, .. } = c {
                    if rect.y != 0.0 || rect.h != TOP_BAR_H {
                        continue;
                    }
                    // The bar's runs are no longer all direct children: the omnibox is
                    // its own group now, so walk the bar's LEAVES rather than its
                    // children. Depth is a layout decision, and a test that pins it
                    // fails on every regrouping without anything being wrong.
                    let mut leaves: Vec<&SceneNode> = Vec::new();
                    c.flatten(&mut leaves);
                    for n in leaves {
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
        let root = layout_home(1600.0, 900.0, &sample(), &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
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
            &RowScroll::default(),
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
        let root = layout_home(w, h, &sample(), &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
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
        // See-all is anchored to the right edge of the content band, which IS the
        // output's: `.hh-rows` pads only on the left, by the gutter.
        let content_right = w;
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
        let root = layout_home(1600.0, 900.0, &hc, &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
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
        let root = layout_home(1600.0, 900.0, &sample(), &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
        // The first card in the sample has both; the second has neither.
        let mut cards: Vec<Vec<SceneNode>> = Vec::new();
        {
            let mut all: Vec<&SceneNode> = Vec::new();
            walk_groups(&root, &mut all);
            for c in all {
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


    /// Every Container in the tree, so a test can find a card group without knowing how
    /// deep the layout nested it. Cards used to be root's direct children and now sit
    /// inside their row's group, which is what `.hh-row` is in the shell.
    fn walk_groups<'a>(node: &'a SceneNode, out: &mut Vec<&'a SceneNode>) {
        if let SceneNode::Container { children, .. } = node {
            for c in children {
                out.push(c);
                walk_groups(c, out);
            }
        }
    }

    /// The leaves of the first card laid out from `hc`.
    fn first_card(hc: &HomeCompose) -> Vec<SceneNode> {
        let root = layout_home(1600.0, 900.0, hc, &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
        first_interactive(&root).expect("no card laid out")
    }

    /// The children of the first INTERACTIVE group, wherever it sits in the tree.
    ///
    /// Depth-agnostic on purpose: cards used to be root's direct children and are now
    /// inside their row's group, which is what `.hh-row` is in the shell. A helper that
    /// pins depth fails on every regrouping without anything being wrong.
    fn first_interactive(node: &SceneNode) -> Option<Vec<SceneNode>> {
        if let SceneNode::Container {
            interactive,
            children,
            ..
        } = node
        {
            if *interactive {
                return Some(children.clone());
            }
            for c in children {
                if let Some(found) = first_interactive(c) {
                    return Some(found);
                }
            }
        }
        None
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
        let root = layout_home(w, h, &sample(), &Theme::cosmic_default(), &RowScroll::default(), &mut IconMeasure);
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
            shield.right() <= w - BAR_PAD_X + 0.01,
            "the cluster stays inside the bar's own pad"
        );

        // The orb-sm is inboard of the avatar now, which is where the shell puts it.
        let mut orb_sm_x = None;
        {
            let mut all: Vec<&SceneNode> = Vec::new();
            walk_groups(&root, &mut all);
            for c in all {
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
        let plain = layout_home(w, h, &sample(), &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
        let pruns = bar_runs(&plain);
        assert!(!pruns.iter().any(|(s, _)| s == "shield"));
        assert!(pruns.iter().any(|(s, _)| s == AVATAR_INITIAL), "the avatar survives");
    }

    #[test]
    fn the_omnibox_pill_carries_its_glyph_prompt_and_shortcut_hint() {
        let root = layout_home(1600.0, 900.0, &sample(), &Theme::cosmic_default(), &RowScroll::default(), &mut IconMeasure);
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
        let root = layout_home(1600.0, 900.0, &hc, &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
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
        let root = layout_home(1600.0, 900.0, &hc, &Theme::cosmic_default(), &RowScroll::default(), &mut IconMeasure);
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
        arted.rows[0].cards[0].photo = Some("/shell/static/app_art/a.png".into());
        let root = layout_home(1600.0, 900.0, &arted, &Theme::cosmic_default(), &RowScroll::default(), &mut IconMeasure);
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
    fn a_ranked_row_drops_the_tile_and_numbers_its_cards() {
        let theme = Theme::cosmic_default();
        let root = layout_home(1600.0, 900.0, &sample(), &theme, &RowScroll::default(), &mut MonoMeasure);
        // The sample's SECOND row is the ranked one.
        let mut rows: Vec<Vec<SceneNode>> = Vec::new();
        {
            let mut all: Vec<&SceneNode> = Vec::new();
            walk_groups(&root, &mut all);
            for c in all {
                if let SceneNode::Container {
                    interactive: true,
                    children,
                    ..
                } = c
                {
                    rows.push(children.clone());
                }
            }
        }
        // Ranked cards carry a numeral; unranked ones do not.
        let numerals: Vec<(String, Rect, f32)> = rows
            .iter()
            .flatten()
            .filter_map(|n| match n {
                SceneNode::Text {
                    rect, text, stroke, ..
                } if *stroke > 0.0 => Some((text.clone(), *rect, *stroke)),
                _ => None,
            })
            .collect();
        assert_eq!(numerals.len(), 1, "one ranked card in the sample, so one numeral");
        assert_eq!(numerals[0].0, "1", "ranks are 1-based");
        assert_eq!(numerals[0].2, RANK_STROKE, "drawn as an outline, not a fill");

        // The ranked card has NO background tile at all: `.hh-card.hh-ranked` is
        // background:transparent / border:none, so the numeral and the ART are the card.
        // It leads with the numeral, matching the shell's own append order (num, then the
        // art box, so the art paints over the numeral's overhang).
        let ranked = rows
            .iter()
            .find(|leaves| {
                leaves
                    .iter()
                    .any(|n| matches!(n, SceneNode::Text { stroke, .. } if *stroke > 0.0))
            })
            .expect("the ranked card");
        assert!(
            matches!(ranked.first(), Some(SceneNode::Text { stroke, .. }) if *stroke > 0.0),
            "a ranked card leads with its numeral, got {:?}",
            ranked.first()
        );
        assert!(
            !ranked
                .iter()
                .any(|n| matches!(n, SceneNode::Rect { color, .. } if *color == theme.card_bg)),
            "a ranked card must not draw the ordinary card tile"
        );
        // Its art box is `.hh-rank-inner`: 174px wide, pinned to the card's RIGHT edge,
        // full height. This is also the box its title and chip live in.
        let art = ranked
            .iter()
            .find_map(|n| match n {
                SceneNode::Art { rect, .. } => Some(*rect),
                _ => None,
            })
            .expect("a ranked card is made of its art");
        assert_eq!(art.w, RANK_INNER_W, "the rank art box is a fixed width");
        let m = HomeMetrics::for_output(1600.0, 900.0);
        assert_eq!(art.h, m.card_h, "and full card height");
        // An unranked card keeps its tile, and its art fills it.
        let plain = rows
            .iter()
            .find(|leaves| {
                !leaves
                    .iter()
                    .any(|n| matches!(n, SceneNode::Text { stroke, .. } if *stroke > 0.0))
            })
            .expect("an unranked card");
        match plain.first() {
            Some(SceneNode::Rect { color, .. }) => assert_eq!(*color, theme.card_bg),
            other => panic!("expected a tile, got {other:?}"),
        }
        let plain_art = plain
            .iter()
            .find_map(|n| match n {
                SceneNode::Art { rect, .. } => Some(*rect),
                _ => None,
            })
            .expect("every card has art");
        assert_eq!(plain_art.w, CARD_W, "an ordinary card's art is inset:0");
    }

    // == THE WIRE CONTRACT =====================================================
    // Every native-scene bug found so far has been one bug: the decoder was written
    // against an IMAGINED payload. `Hero` read `title` and `copy`, `Row` read `label`,
    // `Card` read `subtitle`, and not one of those four keys is emitted by any producer,
    // so a live compose rendered a blank hero and unlabelled rows while every test here
    // passed. Then `card.image_url` turned out to be the key news and app cards actually
    // carry, so those decoded as art-less and drew a glyph the shell suppresses.
    //
    // No test above can catch that class, because they build their fixtures from the same
    // misunderstanding the decoder has. Only a fixture the REAL producer wrote can.
    // `fixtures/home_compose_sanitized.json` is the verbatim output of
    // liquid_ui_service._sanitize_home_payload, the single authority on what reaches a
    // client, and tests/unit/test_panel_reservation.py regenerates it and fails if the
    // producer's shape has moved. Python pins what is SENT; these pin that it is DRAWN.

    fn wire_fixture() -> HomeCompose {
        let v: serde_json::Value =
            serde_json::from_str(crate::wire_fixture::HOME_COMPOSE_SANITIZED)
                .expect("the fixture is the sanitizer's own output");
        decode_home_compose(&v)
    }

    fn drawn_texts(root: &SceneNode) -> Vec<String> {
        let mut leaves: Vec<&SceneNode> = Vec::new();
        root.flatten(&mut leaves);
        leaves
            .iter()
            .filter_map(|n| match n {
                SceneNode::Text { text, .. } => Some(text.clone()),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn every_field_the_shell_actually_sends_reaches_the_native_desktop() {
        let home = wire_fixture();
        assert_eq!(home.hero.amount, Some(1284), "the hero figure");
        assert_eq!(home.hero.amount_unit, "Spark");
        assert_eq!(home.hero.eyebrow, "Earned on the hive");
        assert_eq!((home.hero.agents, home.hero.tasks), (3, 7));
        assert_eq!(home.rows.len(), 2, "both rows survived the decode");

        let cont = &home.rows[0];
        assert_eq!(cont.title, "Continue");
        assert_eq!(cont.accent.as_deref(), Some("teal"));
        assert_eq!(cont.see_all.as_deref(), Some("recipes"));
        assert!(!cont.ranked);
        assert_eq!(cont.cards[0].progress, Some(0.62));
        assert_eq!(cont.cards[0].icon.as_deref(), Some("code"));
        assert_eq!(cont.cards[0].meta.as_deref(), Some("3 files changed"));
        assert_eq!(cont.cards[1].live.as_deref(), Some("RUNNING"));
        // The one that was silently wrong: a news card carries `image_url`, not `image`,
        // and reading only the latter made every one of them look art-less.
        assert_eq!(
            cont.cards[1].photo.as_deref(),
            Some("https://example.invalid/a.jpg"),
            "image_url is a photo, exactly as the shell's own imgSrc treats it"
        );
        let top = &home.rows[1];
        assert!(top.ranked, "the leaderboard row is ranked");
        assert_eq!(top.accent.as_deref(), Some("magenta"));
        assert_eq!(top.cards[0].badge.as_deref(), Some("NEW"));
        assert_eq!(
            top.cards[1].photo.as_deref(),
            Some("/shell/static/app_art/a.svg"),
            "and a same-origin `image` is the same photo slot"
        );

        // Every one of those must actually be DRAWN, not merely decoded.
        let root =
            layout_home(1920.0, 1080.0, &home, &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
        let drawn = drawn_texts(&root);
        for want in [
            "Earned on the hive",
            "1284",
            "Spark",
            "Continue",
            "Refactor the parser",
            "3 files changed",
            "Morning briefing",
            "12 sources",
            "RUNNING",
            "Top agents",
            "Scout",
            "412 tasks",
            "Archivist",
            "388 tasks",
            "NEW",
            SEE_ALL,
            "Resume",
            "Ask anything",
        ] {
            assert!(
                drawn.iter().any(|t| t == want),
                "the shell sent {want:?} and the native desktop never drew it"
            );
        }
    }

    #[test]
    fn the_ranked_row_from_a_real_payload_is_drawn_as_cards_not_holes() {
        // The leaderboard is the shape that rendered as numerals floating on the desktop
        // ground: a ranked card's background is transparent by design and the art it is
        // made of was never lowered. Assert it from the REAL payload, because a
        // hand-built fixture is exactly what missed this the first time.
        let home = wire_fixture();
        let root =
            layout_home(1920.0, 1080.0, &home, &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
        let mut leaves: Vec<&SceneNode> = Vec::new();
        root.flatten(&mut leaves);

        let arts: Vec<&SceneNode> = leaves
            .iter()
            .copied()
            .filter(|n| matches!(n, SceneNode::Art { .. }))
            .collect();
        let cards: usize = home.rows.iter().map(|r| r.cards.len()).sum();
        assert_eq!(arts.len(), cards, "every card has an art tile, ranked or not");
        for art in &arts {
            if let SceneNode::Art { from, to, rect, .. } = art {
                assert!(from.a > 0.9 && to.a > 0.9, "art is a ground, never a hole");
                assert_ne!(from, to, "two stops, so the brand gradient and not a fill");
                assert!(rect.w > 1.0 && rect.h > 1.0, "and it has a real box");
            }
        }
        // The two rows name different accents, so they must not paint the same colour: a
        // desktop where every row read teal is what the accent decode was added to fix.
        let hue = |n: &SceneNode| match n {
            SceneNode::Art { to, .. } => (to.r, to.g, to.b),
            _ => unreachable!("filtered to Art above"),
        };
        assert_ne!(
            hue(arts[0]),
            hue(arts[home.rows[0].cards.len()]),
            "the teal row and the magenta row must not paint the same colour"
        );

        // The rank numerals are OUTLINES, one per ranked card and none elsewhere.
        let strokes: Vec<&String> = leaves
            .iter()
            .filter_map(|n| match n {
                SceneNode::Text { text, stroke, .. } if *stroke > 0.0 => Some(text),
                _ => None,
            })
            .collect();
        assert_eq!(strokes.len(), home.rows[1].cards.len(), "one numeral per ranked card");
        assert_eq!(strokes[0], "1", "ranks are 1-based");
    }

    #[test]
    fn a_payload_the_sanitizer_would_never_emit_still_cannot_break_the_desktop() {
        // The decoder is deliberately tolerant, and this is what that has to mean: a
        // section degrades rather than the desktop blanking or panicking. Worth asserting
        // beside the happy path, because that tolerance is why a producer change shows up
        // as a missing label rather than a crash, which is how the imagined-key bugs
        // stayed invisible for so long.
        for bad in [
            serde_json::json!({}),
            serde_json::json!({"hero": 5, "rows": "no"}),
            serde_json::json!({"rows": [{"title": "x", "cards": [{}]}]}),
            serde_json::json!({"hero": {"amount": "lots"}, "rows": []}),
        ] {
            let home = decode_home_compose(&bad);
            let root =
                layout_home(1920.0, 1080.0, &home, &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
            let mut leaves: Vec<&SceneNode> = Vec::new();
            root.flatten(&mut leaves);
            // The fixed chrome is unconditional: whatever the payload, there is a bar and
            // a taskbar, so the desktop is never a void the user cannot get out of.
            assert!(
                leaves.len() > 4,
                "a malformed payload must leave the chrome standing: {bad}"
            );
        }
    }

    #[test]
    fn every_surface_the_native_shell_owns_names_itself_for_latency_attribution() {
        // latency_budgets.json carries 23 per-component budgets and the instrument has
        // never consulted one of them: every sample is reported as `component=shell`, so
        // a slow orb and a slow marketplace are the same number. latency.rs says the
        // blocker moved once the scene graph could hit-test; this is the identity it
        // named as missing.
        let theme = Theme::cosmic_default();
        let root = layout_home(1600.0, 900.0, &sample(), &theme, &RowScroll::default(), &mut MonoMeasure);

        // The top bar, and the omnibox INSIDE it, because deepest wins: a pill that
        // reported `top-bar` would hide the omnibox's own budget behind the bar's.
        assert_eq!(root.component_at(4.0, 4.0), Some(Component::TopBar));
        let pill_x = 1600.0 * 0.5;
        assert_eq!(root.component_at(pill_x, TOP_BAR_H * 0.5), Some(Component::Omnibox));

        // The taskbar strip.
        assert_eq!(
            root.component_at(800.0, 900.0 - TASKBAR_H * 0.5),
            Some(Component::Taskbar)
        );

        // A card, found through the tree rather than by guessing at its geometry.
        let mut groups: Vec<&SceneNode> = Vec::new();
        walk_groups(&root, &mut groups);
        let card = groups
            .iter()
            .find(|c| matches!(c, SceneNode::Container { interactive: true, .. }))
            .map(|c| c.rect())
            .expect("a card was laid out");
        assert_eq!(
            root.component_at(card.x + card.w * 0.5, card.y + card.h * 0.5),
            Some(Component::HomeCard),
            "deepest wins: a card inside a row names the CARD"
        );
        // And the row band around it names the row, carrying its index so a wheel event
        // can reach it. A point on the row's header is over the row and over no card.
        let row = groups
            .iter()
            .find_map(|c| match c {
                SceneNode::Container {
                    component: Some(Component::HomeRow(i)),
                    rect,
                    ..
                } => Some((*i, *rect)),
                _ => None,
            })
            .expect("a row band was laid out");
        // EVERY row carries its OWN index. A band that always said 0 would send every
        // wheel event to the first row, which is the failure a single-row check misses.
        let indices: Vec<usize> = groups
            .iter()
            .filter_map(|c| match c {
                SceneNode::Container {
                    component: Some(Component::HomeRow(i)),
                    ..
                } => Some(*i),
                _ => None,
            })
            .collect();
        assert!(indices.len() >= 2, "the sample lays out more than one row");
        assert_eq!(
            indices,
            (0..indices.len()).collect::<Vec<_>>(),
            "row bands carry 0, 1, 2 in order"
        );
        assert_eq!(row.0, 0, "the first row is index 0");
        assert_eq!(
            root.component_at(row.1.right() - 2.0, row.1.y + 2.0),
            Some(Component::HomeRow(0)),
            "the row's own band names the row"
        );
        assert_eq!(root.row_at(card.x + card.w * 0.5, card.y + card.h * 0.5), None,
                   "a point on a CARD is not a scroll target for the row");

        // The orb is a LEAF, not a group: OrbSlot already is the orb, so it names itself
        // without a container wrapped around it saying the same thing twice.
        let mut leaves: Vec<&SceneNode> = Vec::new();
        root.flatten(&mut leaves);
        let orb = leaves
            .iter()
            .find_map(|n| match n {
                SceneNode::OrbSlot { rect, compact } if !*compact => Some(*rect),
                _ => None,
            })
            .expect("the home orb has a slot");
        assert_eq!(
            root.component_at(orb.x + orb.w * 0.5, orb.y + orb.h * 0.5),
            Some(Component::Orb)
        );
        // The COMPACT orb docked in the bar is still the orb, not the bar it sits in.
        let orb_sm = leaves
            .iter()
            .find_map(|n| match n {
                SceneNode::OrbSlot { rect, compact } if *compact => Some(*rect),
                _ => None,
            })
            .expect("the bar has an orb-sm");
        assert_eq!(
            root.component_at(orb_sm.x + orb_sm.w * 0.5, orb_sm.y + orb_sm.h * 0.5),
            Some(Component::Orb)
        );

        // Bare desktop has no budget row and must not borrow one. A sample attributed to
        // a component it did not touch is worse than an unattributed sample.
        assert_eq!(root.component_at(-5.0, -5.0), None, "outside the output entirely");
    }

    #[test]
    fn the_shipped_fallbacks_are_the_constants_the_guards_pin() {
        // The cross-language guard pins TRAY_PX and CARD_RADIUS against the CSS. That
        // only means anything if the value the scene actually falls back to IS those
        // constants. When their values were duplicated as bare literals here the
        // constants went dead, the compiler said so, and the guard carried on passing
        // while pinning something the layout no longer read: a guard that cannot fail
        // for the reason it exists.
        let t = Theme::cosmic_default();
        assert_eq!(t.icon_px, TRAY_PX, "the tray fallback IS the pinned constant");
        assert_eq!(t.card_radius, CARD_RADIUS, "and so is the corner");
        assert_eq!(t.top_bar_h, TOP_BAR_H, "as the bar already was");
        assert_eq!(t.chrome_rule_px, CHROME_RULE, "and the rule's own width");
    }

    #[test]
    fn every_component_maps_to_its_own_instrument_surface() {
        // The names live ONCE, on `latency::Surface`. This half asserts the mapping into
        // it: total, and injective, so two components cannot quietly share a budget row.
        // The names themselves are pinned to latency_budgets.json in Python, which is
        // where both files are readable (the budget file is outside the crate and crane's
        // source filter ships `*.rs` only).
        let all = [
            Component::Orb,
            Component::TopBar,
            Component::Omnibox,
            Component::Taskbar,
            Component::HomeCard,
        ];
        let labels: Vec<&str> = all.iter().map(|c| c.surface().label()).collect();
        assert_eq!(labels, ["orb", "top-bar", "omnibox", "taskbar", "home-card"]);
        for (i, a) in labels.iter().enumerate() {
            for b in labels.iter().skip(i + 1) {
                assert_ne!(a, b, "two components share a budget row");
            }
            assert!(!a.is_empty() && !a.contains(' '), "a key must be a bare slug");
        }
        assert_ne!(
            all[0].surface(),
            crate::latency::Surface::Shell,
            "a named component must never map to the unattributed surface"
        );
    }

    #[test]
    fn both_chrome_strips_draw_their_rule_on_the_edge_that_faces_the_desktop() {
        // `.top-bar { border-bottom: 1px solid var(--hart-glass-border); border-top: 0 }`
        // and `.taskbar { border-top: 1px ... }`. One edge each, facing the desktop, and
        // the native strips had neither: their edge was wherever the translucency
        // happened to stop, which is chrome dissolving into the desktop rather than
        // sitting on it.
        let mut theme = Theme::cosmic_default();
        // A colour nothing else in the scene uses, so finding it IS finding the rule.
        theme.chrome_border = Color::rgba(1.0, 0.0, 0.5, 0.2);
        let (w, h) = (1600.0, 900.0);
        let root = layout_home(w, h, &sample(), &theme, &RowScroll::default(), &mut MonoMeasure);
        let mut leaves: Vec<&SceneNode> = Vec::new();
        root.flatten(&mut leaves);
        let rules: Vec<Rect> = leaves
            .iter()
            .filter_map(|n| match n {
                SceneNode::Rect { rect, color, .. } if *color == theme.chrome_border => {
                    Some(*rect)
                }
                _ => None,
            })
            .collect();
        assert_eq!(rules.len(), 2, "one rule per strip, no more: {rules:?}");
        for r in &rules {
            assert_eq!(r.h, CHROME_RULE, "a rule is one pixel tall");
            assert_eq!(r.w, w, "and spans the output");
        }
        // The top bar's sits on its BOTTOM edge, the taskbar's on its TOP edge.
        let top = rules.iter().find(|r| r.y < h * 0.5).expect("the top bar's rule");
        let bottom = rules.iter().find(|r| r.y > h * 0.5).expect("the taskbar's rule");
        assert!(
            (top.bottom() - theme.top_bar_h).abs() < 0.01,
            "the top bar's rule ends exactly at the bar's edge: {top:?}"
        );
        assert!(
            (bottom.y - (h - TASKBAR_H)).abs() < 0.01,
            "the taskbar's rule starts exactly at the strip's edge: {bottom:?}"
        );
    }

    #[test]
    fn a_row_that_overflows_can_be_scrolled_to_its_last_card() {
        // a2, verbatim: "Netflix rows scroll HORIZONTALLY (sideways = native /
        // console-like), the canvas itself never page-scrolls." The shell does it with
        // `.hh-cards { overflow-x: auto }`. The native scene CLIPPED: the sanitizer
        // allows twelve cards a row, about seven fit a 1920 screen, and the rest were
        // unreachable rather than merely off-screen.
        let mut hc = sample();
        let proto = hc.rows[0].cards[0].clone();
        hc.rows[0].cards = (0..12)
            .map(|i| {
                let mut c = proto.clone();
                c.title = format!("card{i}");
                c
            })
            .collect();
        let theme = Theme::cosmic_default();
        let (w, h) = (1920.0, 1080.0);
        let titles = |scroll: &RowScroll| -> Vec<String> {
            let root = layout_home(w, h, &hc, &theme, scroll, &mut MonoMeasure);
            let mut leaves: Vec<&SceneNode> = Vec::new();
            root.flatten(&mut leaves);
            leaves
                .iter()
                .filter_map(|n| match n {
                    SceneNode::Text { text, size_px, .. }
                        if *size_px == CARD_TITLE_PX && text.starts_with("card") =>
                    {
                        Some(text.clone())
                    }
                    _ => None,
                })
                .collect()
        };

        let unscrolled = titles(&RowScroll::default());
        assert!(!unscrolled.is_empty(), "some cards are visible at rest");
        assert!(
            unscrolled.len() < 12,
            "twelve cards must NOT all fit, or this proves nothing: {}",
            unscrolled.len()
        );
        assert_eq!(unscrolled[0], "card0", "at rest a row starts at its first card");
        assert!(!unscrolled.contains(&"card11".to_string()), "the last is off-screen");

        // Scrolled to its end, the LAST card is reachable. That is the whole point: the
        // cards past the edge were not merely hidden, they could not be got to.
        let m = HomeMetrics::for_output(w, h);
        let view_w = w - m.gutter;
        let content_w = RowScroll::content_width(12);
        let mut end = RowScroll::default();
        end.scroll(0, content_w, content_w, view_w);
        let scrolled = titles(&end);
        assert!(
            scrolled.contains(&"card11".to_string()),
            "the last card must be reachable: {scrolled:?}"
        );
        assert!(!scrolled.contains(&"card0".to_string()), "and the first has gone by");
        assert_eq!(
            scrolled.len(),
            unscrolled.len(),
            "the WINDOW moves; the number of cards on screen does not"
        );
    }

    #[test]
    fn a_row_that_fits_cannot_drift_and_a_row_that_does_not_stops_at_its_end() {
        // The clamp is the whole model. A short row pinned at 0 is what stops a stray
        // wheel event sliding a two-card row off its own gutter, and stopping exactly at
        // the last card is what stops a long row scrolling into empty space.
        let mut sc = RowScroll::default();
        let (content_w, view_w) = (RowScroll::content_width(2), 1860.0);
        assert!(content_w < view_w, "two cards fit a wide screen");
        sc.scroll(0, 500.0, content_w, view_w);
        assert_eq!(sc.get(0), 0.0, "a row with nothing to scroll does not move");

        let long = RowScroll::content_width(12);
        let max = long - view_w;
        assert!(max > 0.0, "twelve cards overflow");
        sc.scroll(0, 10_000.0, long, view_w);
        assert_eq!(sc.get(0), max, "it stops at its last card, not past it");
        sc.scroll(0, -10_000.0, long, view_w);
        assert_eq!(sc.get(0), 0.0, "and back to its first, not before it");

        // Rows are independent, and an index past the end is a no-op rather than a panic:
        // this is fed by an input event, so it must never index out of bounds.
        sc.scroll(1, 200.0, long, view_w);
        assert_eq!(sc.get(0), 0.0, "row 0 did not move");
        assert_eq!(sc.get(1), 200.0, "row 1 did");
        sc.scroll(MAX_ROWS + 5, 100.0, long, view_w);
        assert_eq!(sc.get(MAX_ROWS + 5), 0.0, "an unknown row reads as unscrolled");
        // A non-finite delta cannot poison the offset.
        sc.scroll(1, f32::NAN, long, view_w);
        assert_eq!(sc.get(1), 200.0, "NaN is not a scroll");
    }

    #[test]
    fn a_shrinking_feed_pulls_a_scrolled_row_back_into_its_content() {
        // A row scrolled to its end and then given fewer cards would keep an offset past
        // its own content and render EMPTY: the cards would all be off the left edge. The
        // re-clamp is what makes a live feed safe to scroll.
        let view_w = 1860.0;
        let long = RowScroll::content_width(12);
        let mut sc = RowScroll::default();
        sc.scroll(0, 10_000.0, long, view_w);
        assert!(sc.get(0) > 0.0);

        // The feed shrinks to two cards, which fit.
        sc.reclamp(&[(RowScroll::content_width(2), view_w)]);
        assert_eq!(sc.get(0), 0.0, "a row that now fits is pinned back to its start");

        // And a row that vanishes entirely takes its offset with it.
        sc.scroll(1, 500.0, long, view_w);
        sc.reclamp(&[(long, view_w)]);
        assert_eq!(sc.get(1), 0.0, "a row the feed no longer has is not left scrolled");

        // content_width is the cards plus the gaps BETWEEN them, never a trailing one.
        assert_eq!(RowScroll::content_width(0), 0.0);
        assert_eq!(RowScroll::content_width(1), CARD_W);
        assert_eq!(RowScroll::content_width(2), CARD_W * 2.0 + CARD_GAP);
    }

    #[test]
    fn the_shells_two_breakpoints_still_fit_every_row_on_a_real_panel() {
        // The scale correction is only safe if the desktop still keeps its promise: rows
        // are dropped, silently, the moment one does not fit the band, so laying out at
        // the shell's real 88px figure and 150px cards could have cost a row on a small
        // screen and nothing would have said so. Check the panel sizes that matter.
        let theme = Theme::cosmic_default();
        let hc = sample();
        let rows_at = |w: f32, h: f32| {
            let root = layout_home(w, h, &hc, &theme, &RowScroll::default(), &mut MonoMeasure);
            let mut leaves: Vec<&SceneNode> = Vec::new();
            root.flatten(&mut leaves);
            hc.rows
                .iter()
                .filter(|r| {
                    leaves.iter().any(
                        |n| matches!(n, SceneNode::Text { text, size_px, .. }
                                     if *text == r.title && *size_px == ROW_LABEL_PX),
                    )
                })
                .count()
        };
        let want = hc.rows.len().min(3);
        for (w, h) in [(1920.0, 1080.0), (1600.0, 900.0), (1366.0, 768.0), (1280.0, 800.0)] {
            assert_eq!(rows_at(w, h), want, "a {w}x{h} panel lost a row");
        }
        // And the cards themselves must still fit ACROSS: a row that shows one card is
        // not a row. 258px cards plus an 18px gap inside a 60px gutter is four on 1280.
        let root = layout_home(1280.0, 800.0, &hc, &theme, &RowScroll::default(), &mut MonoMeasure);
        let mut leaves: Vec<&SceneNode> = Vec::new();
        root.flatten(&mut leaves);
        let arts = leaves
            .iter()
            .filter(|n| matches!(n, SceneNode::Art { .. }))
            .count();
        assert!(arts >= 3, "a narrow panel still shows a strip of cards, got {arts}");
    }

    #[test]
    fn the_metrics_apply_the_shells_media_queries_in_its_own_order() {
        // hartHome.css declares max-width:1400 first and max-height:820 second, so on a
        // screen matching BOTH the later block wins the figure size. Getting the order
        // backwards is invisible except on exactly those screens, which is most laptops.
        let big = HomeMetrics::for_output(1920.0, 1080.0);
        assert_eq!((big.gutter, big.amount_px, big.unit_px, big.card_h), (60.0, 88.0, 26.0, 150.0));
        let narrow = HomeMetrics::for_output(1366.0, 1000.0);
        assert_eq!((narrow.gutter, narrow.amount_px, narrow.unit_px), (40.0, 70.0, 22.0));
        assert_eq!(narrow.card_h, 150.0, "width alone does not shrink a card");
        let short = HomeMetrics::for_output(1920.0, 800.0);
        assert_eq!((short.gutter, short.amount_px, short.card_h), (60.0, 58.0, 132.0));
        // Both: the max-height block is later in the cascade, so 58 not 70.
        let both = HomeMetrics::for_output(1366.0, 768.0);
        assert_eq!(both.amount_px, 58.0, "the later media block wins the figure");
        assert_eq!((both.gutter, both.unit_px, both.card_h), (40.0, 22.0, 132.0));
        // The breakpoints are inclusive, exactly as `max-width` / `max-height` are.
        assert_eq!(HomeMetrics::for_output(1400.0, 1080.0).gutter, 40.0);
        assert_eq!(HomeMetrics::for_output(1401.0, 1080.0).gutter, 60.0);
        assert_eq!(HomeMetrics::for_output(1920.0, 820.0).card_h, 132.0);
        assert_eq!(HomeMetrics::for_output(1920.0, 821.0).card_h, 150.0);
        // The BAR's own two, which have nothing to do with the content scale: the pill
        // drops its hint and its floor at 1100, and the last two destinations go at 880.
        assert!(big.show_kbd && big.nav_tabs == NAV_TABS.len());
        assert_eq!((big.tab_pad_x, big.omnibox_min_w), (13.0, 220.0));
        let tight = HomeMetrics::for_output(1100.0, 1080.0);
        assert!(!tight.show_kbd, "the shortcut hint goes first");
        assert_eq!((tight.tab_pad_x, tight.omnibox_min_w), (9.0, 120.0));
        assert_eq!(tight.nav_tabs, NAV_TABS.len(), "all five still fit at 1100");
        assert_eq!(HomeMetrics::for_output(1101.0, 1080.0).tab_pad_x, 13.0);
        let narrowest = HomeMetrics::for_output(880.0, 1080.0);
        assert_eq!(narrowest.nav_tabs, 3, "Hive and Earn go at 880");
        assert_eq!(HomeMetrics::for_output(881.0, 1080.0).nav_tabs, NAV_TABS.len());
    }

    #[test]
    fn a_narrow_bar_hides_the_last_two_tabs_and_the_shortcut_hint() {
        // Not the same as the collision check the tab loop already does: at 880 the shell
        // hides Hive and Earn even where they would fit, and at 1100 it hides the hint
        // even where it clears the prompt. Drawing either anyway is a bar that does not
        // match the shell's on exactly the panels most likely to be plugged in.
        let theme = Theme::cosmic_default();
        let hc = sample();
        let texts = |w: f32| {
            let root = layout_home(w, 1080.0, &hc, &theme, &RowScroll::default(), &mut MonoMeasure);
            let mut leaves: Vec<&SceneNode> = Vec::new();
            root.flatten(&mut leaves);
            leaves
                .iter()
                .filter_map(|n| match n {
                    SceneNode::Text { text, .. } => Some(text.clone()),
                    _ => None,
                })
                .collect::<Vec<_>>()
        };
        let wide = texts(1920.0);
        assert!(wide.contains(&"Hive".to_string()) && wide.contains(&"Earn".to_string()));
        assert!(wide.contains(&OMNIBOX_KBD.to_string()), "the hint shows on a wide bar");
        let tight = texts(1100.0);
        assert!(!tight.contains(&OMNIBOX_KBD.to_string()), "the hint is hidden at 1100");
        let narrow = texts(880.0);
        assert!(!narrow.contains(&"Hive".to_string()), "Hive is hidden at 880");
        assert!(!narrow.contains(&"Earn".to_string()), "Earn is hidden at 880");
        assert!(narrow.contains(&"Home".to_string()), "Home always stays");
    }

    #[test]
    fn every_card_carries_art_whether_or_not_the_feed_named_a_picture() {
        // The shell paints `art.style.background = gradientArt(...)` unconditionally and
        // only then fades a photo in over it, with its own note "no empty flash". The
        // scene emitted an art node ONLY when the payload named a picture, so a card
        // without one drew a flat tile and a RANKED card, whose background is transparent
        // by design, drew nothing at all.
        let mut hc = sample();
        hc.rows[0].cards[0].photo = None;
        hc.rows[0].cards[1].photo = Some("/shell/static/app_art/a.svg".into());
        let theme = Theme::cosmic_default();
        let tree = layout_home(1280.0, 800.0, &hc, &theme, &RowScroll::default(), &mut MonoMeasure);
        let mut leaves: Vec<&SceneNode> = Vec::new();
        tree.flatten(&mut leaves);
        let arts: Vec<(Option<String>, Color, Color, f32)> = leaves
            .into_iter()
            .filter_map(|n| match n {
                SceneNode::Art {
                    photo,
                    from,
                    to,
                    angle_deg,
                    ..
                } => Some((photo.clone(), *from, *to, *angle_deg)),
                _ => None,
            })
            .collect();
        let cards: usize = hc.rows.iter().map(|r| r.cards.len()).sum();
        assert_eq!(arts.len(), cards, "one art tile per card, photo or not");
        assert!(arts[0].0.is_none(), "the first card named no picture");
        assert!(arts[1].0.is_some(), "the second carries its photo for later");
        // Both stops of every tile must be REAL colour, never transparent: a transparent
        // art tile is the hole this fixed.
        for (_, from, to, _) in &arts {
            assert!(from.a > 0.9 && to.a > 0.9, "art is an opaque ground");
            assert_ne!(from, to, "two stops, not a flat fill");
        }
        // The angle cycles per card so a row does not read as one repeated tile.
        assert_ne!(arts[0].3, arts[1].3, "neighbouring cards differ in angle");
    }

    #[test]
    fn art_stops_are_the_shells_own_arithmetic() {
        // Ported from hartBrandArt.gradient: blend(base, INK, 0.46) is the darker stop,
        // blend(second, INK, 0.20) the lighter, and the angle is [135,150,165][seed % 3].
        // Pinned against the hue directly, so a drift in either factor fails here rather
        // than showing up as a desktop that reads darker or flatter than the shell's.
        let theme = Theme::cosmic_default();
        let magenta = theme.spectrum_named("magenta").expect("in the spectrum");
        let (from, to, angle) = theme.art_stops(Some("magenta"), 0, 0);
        assert_eq!(angle, 135.0, "seed 0 takes the first angle");
        assert_eq!(from, magenta.mix(ART_INK, 0.20), "the light stop");
        assert_eq!(to, magenta.mix(ART_INK, 0.46), "the dark stop");
        assert!(to.r < from.r, "the second stop is the darker one");
        // A row with no accent takes its POSITIONAL hue, both stops, exactly as the shell
        // does with `row.accent || spec[idx]` resolving to a real name before the lookup.
        let (f1, t1, _) = theme.art_stops(None, 3, 0);
        let hue3 = theme.spectrum[3];
        assert_eq!(f1, hue3.mix(ART_INK, 0.20));
        assert_eq!(t1, hue3.mix(ART_INK, 0.46));
        // A name OUTSIDE the spectrum is the shell's no-hex branch: `spectrumHex[name]` is
        // undefined, so gradient() folds in a neighbour hue two places along, seeded by
        // the CARD index. Two different hues, not one.
        let (f2, t2, angle2) = theme.art_stops(Some("chartreuse"), 0, 1);
        assert_eq!(angle2, 150.0, "seed 1 takes the second angle");
        assert_eq!(f2, theme.spectrum[3].mix(ART_INK, 0.20), "the neighbour hue");
        assert_eq!(t2, theme.spectrum[1].mix(ART_INK, 0.46), "the card's own hue");
        // The angle list wraps rather than running off its end.
        assert_eq!(theme.art_stops(None, 0, 3).2, 135.0, "seed 3 wraps to the first");
    }

    #[test]
    fn a_ranked_cards_content_sits_in_its_art_box_not_its_card_box() {
        // Everything the shell builds for a card is appended to `artWrap`: the glyph, the
        // live/badge chip, the title+meta body, the progress bar. On a ranked card that is
        // `.hh-rank-inner`, the 174px box on the card's right, NOT the card. Positioning
        // against the card put every one of them 84px left of where the shell puts them.
        let mut hc = sample();
        hc.rows[0].ranked = true;
        hc.rows[0].cards[0].photo = None;
        hc.rows[0].cards[0].badge = Some("NEW".into());
        let leaves = first_card(&hc);
        let art = leaves
            .iter()
            .find_map(|n| match n {
                SceneNode::Art { rect, .. } => Some(*rect),
                _ => None,
            })
            .expect("the art box");
        let title = leaves
            .iter()
            .find_map(|n| match n {
                SceneNode::Text { rect, text, .. } if *text == hc.rows[0].cards[0].title => {
                    Some(*rect)
                }
                _ => None,
            })
            .expect("the title");
        assert!(
            title.x >= art.x && title.right() <= art.right() + 0.01,
            "the title belongs inside the art box: title {title:?} art {art:?}"
        );
        // The badge is inset from the ART box's right edge, which for a ranked card is
        // also the card's, so check the left edge: it must clear the numeral's gutter.
        let badge = leaves
            .iter()
            .find_map(|n| match n {
                SceneNode::Text { rect, text, .. } if text == "NEW" => Some(*rect),
                _ => None,
            })
            .expect("the badge");
        assert!(badge.x > art.x, "the chip sits inside the art box");
    }

    #[test]
    fn a_ranked_card_can_finally_show_a_hover() {
        // Its background used to be a fully transparent rect, kept only so `hover_leaf`
        // would find a first-child Rect, and lifting a transparent colour shows nothing.
        // The art tile is the ranked card's real surface, so it is what the hover finds.
        let mut hc = sample();
        hc.rows[0].ranked = true;
        let tree = layout_home(1600.0, 900.0, &hc, &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
        let mut leaves: Vec<&SceneNode> = Vec::new();
        tree.flatten(&mut leaves);
        let art_idx = leaves
            .iter()
            .position(|n| matches!(n, SceneNode::Art { .. }))
            .expect("the first card's art");
        let art_rect = leaves[art_idx].rect();
        let inside = (art_rect.x + art_rect.w * 0.5, art_rect.y + art_rect.h * 0.5);
        assert_eq!(
            tree.hover_leaf(Some(inside)),
            Some(art_idx),
            "the pointer over a ranked card must light its art tile"
        );
        assert_eq!(tree.hover_leaf(None), None, "no pointer, no hover");
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
        // Matched on SIZE as well as colour. The live dot is --hart-amb-4, which is also
        // magenta in the row spectrum, so a row that names magenta paints its progress
        // fills the same hue: colour alone would count those too.
        let dots = |ls: &Vec<SceneNode>| {
            ls.iter()
                .filter(|n| {
                    matches!(n, SceneNode::Rect { rect, color, .. }
                        if *color == theme.live_dot
                            && (rect.w - CARD_LIVE_DOT).abs() < 0.01
                            && (rect.h - CARD_LIVE_DOT).abs() < 0.01)
                })
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
        let root = layout_home(1600.0, 900.0, &hc, &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
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
            let root = layout_home(w, h, &sample(), &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
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
        let root = layout_home(w, h, &sample(), &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
        // Find a card: the interactive group layout_home emits once per card.
        let mut card = None;
        {
            let mut all: Vec<&SceneNode> = Vec::new();
            walk_groups(&root, &mut all);
            for c in all {
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
            root.hover_leaf(Some((
                HomeMetrics::for_output(w, h).gutter + 4.0,
                TOP_BAR_H + CONTENT_PAD_Y + 4.0,
            ))),
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
        let root = layout_home(w, h, &sample(), &Theme::cosmic_default(), &RowScroll::default(), &mut MonoMeasure);
        // The large home orb's centre must energise the orb.
        let mut orb_centre = None;
        {
            let mut all: Vec<&SceneNode> = Vec::new();
            walk_groups(&root, &mut all);
            for c in all {
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
        let hero_pt = (
            HomeMetrics::for_output(w, h).gutter + 4.0,
            TOP_BAR_H + CONTENT_PAD_Y + 4.0,
        );
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

        let _ = cache.tree_for(1600.0, 900.0, &home, &theme, &RowScroll::default(), &mut MonoMeasure);
        assert_eq!(cache.rebuilds(), 1, "the first frame builds the tree");

        // A steady desktop: same size, same payload, same theme. However many frames run,
        // the tree must NOT be rebuilt — this is the zero-per-frame-alloc NFR.
        for _ in 0..10 {
            let _ = cache.tree_for(1600.0, 900.0, &home, &theme, &RowScroll::default(), &mut MonoMeasure);
        }
        assert_eq!(cache.rebuilds(), 1, "a steady desktop must not rebuild per frame");

        // A resize changes layout, so it must rebuild.
        let _ = cache.tree_for(1280.0, 800.0, &home, &theme, &RowScroll::default(), &mut MonoMeasure);
        assert_eq!(cache.rebuilds(), 2, "a resize must rebuild");

        // A new compose changes layout, so it must rebuild.
        let mut recomposed = home.clone();
        recomposed.hero.eyebrow = "Shipped a release".into();
        let _ = cache.tree_for(1280.0, 800.0, &recomposed, &theme, &RowScroll::default(), &mut MonoMeasure);
        assert_eq!(cache.rebuilds(), 3, "a new compose must rebuild");

        // And the retained tree is a REAL tree, not an empty placeholder: the cached nodes
        // are what hover hit-tests against (the pointer is deliberately not part of the key).
        let node_count = cache
            .tree_for(1280.0, 800.0, &recomposed, &theme, &RowScroll::default(), &mut MonoMeasure)
            .node_count();
        assert!(node_count > 1, "the retained tree must hold real nodes");
        assert_eq!(cache.rebuilds(), 3, "re-reading the cached tree must not rebuild");
    }
}
