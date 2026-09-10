//! NATIVE SHELL PARITY PROGRAM, M3 text: real shaped, anti-aliased text for the
//! native scene, so the compositor's own chrome reads like a desktop instead of the
//! WebView's. This is the M3 spike's decided path (NATIVE_SHELL_PARITY_PROGRAM):
//! cosmic-text does shaping + rasterization; each run is drawn ONCE into a cached
//! premultiplied-ARGB `MemoryRenderBuffer` and lowered through the SAME
//! `MemoryRenderBufferRenderElement` path bloom.rs / orb.rs already use, so there is
//! no parallel render path. NOT a glyph atlas: a shell has a handful of runs, so
//! per-run buffers are simplest and reuse existing code; the atlas is a later
//! optimization if run count grows.
//!
//! Cache (compose-once NFR): a run is keyed by (text, size, box, color) and rendered
//! only when one changes — mirrors OrbCache/BloomCache, so steady state is zero text
//! rasterization per frame. Fonts come from the system `FontSystem` (the same fonts
//! the shell already uses); no parallel font table. NO em dashes (checklist rule).
#![cfg(any(feature = "winit", feature = "smithay"))]

use std::collections::HashMap;

use cosmic_text::{
    Attrs, Buffer, Color as CtColor, Family, FontSystem, Metrics, Shaping, SwashCache, Weight,
};
use smithay::backend::allocator::Fourcc;
use smithay::backend::renderer::element::memory::MemoryRenderBuffer;
use smithay::utils::Transform;

use crate::scene::TextMeasure;

/// How a run is shaped: the CSS weight and letter-spacing the scene asked for.
///
/// One function because measure and paint MUST agree. They were two bare `Attrs::new()`
/// calls, which agreed only because neither asked for anything; the moment one of them
/// carried a weight and the other did not, every measured width would have been the width
/// of a different face than the one painted.
///
/// `cosmic_text::Weight` is a newtype over the same CSS number the shell's rules are
/// written in, so this is a wrap, not a mapping table.
/// The icon face, named exactly as the shell names it first.
///
/// `.mi { font-family: 'Material Symbols Rounded', 'Material Icons Round',
/// 'Material Icons', 'Material Symbols Outlined' }`, and the shell @font-faces a bundled
/// MaterialSymbolsRounded.woff2 under that name. The box has it: `fc-list` reports
/// `Material Symbols Rounded` among eight Material families, installed by
/// hart-subsystems.nix so the shell renders offline.
///
/// ONE name rather than the shell's four-deep stack, because a CSS stack falls through on
/// a MISSING FAMILY while cosmic-text's fallback is per CODEPOINT: an icon name is ASCII,
/// every sans face covers it, so a fallback chain would never fire on the one thing that
/// makes icons different. Which is exactly how they came to render as words.
pub const ICON_FAMILY: &str = "Material Symbols Rounded";

fn attrs_for(weight: u16, letter_spacing_px: f32, size_px: f32, icon: bool) -> Attrs<'static> {
    let base = Attrs::new();
    // ASK FOR THE ICON FACE, or the name is just a word.
    //
    // A card icon is a Material LIGATURE NAME ("storage", "sd_card_alert"): the face
    // substitutes the whole string for one glyph. Nothing here used to select a family, so
    // every run shaped in cosmic-text's default (`Family::SansSerif`). The names are pure
    // ASCII, so sans covers every codepoint, the missing-glyph fallback never fired, and
    // the Material face sat in fontdb unused while the tray painted the literal words
    // "notifications", "palette", "shield".
    let base = if icon { base.family(Family::Name(ICON_FAMILY)) } else { base };
    base
        .weight(Weight(weight))
        // TRACKING IS EM HERE, PX EVERYWHERE ELSE. cosmic-text says so itself
        // ("Set letter spacing (tracking) in EM", attrs.rs), and shape.rs adds the
        // value straight onto the em-normalised advance
        // (`pos.x_advance / font_scale + spacing`), which layout then multiplies by
        // the font size.
        //
        // The scene speaks CSS px, because the rules it mirrors are written that way:
        // `.hh-eyebrow` is `letter-spacing: 3px`, and scene.rs passes 3.0 under that
        // citation. Handing that number over unconverted asked for THREE EM, which at
        // a 16px eyebrow is 48px between every pair of letters. "EARNED ON THE HIVE"
        // laid out around five times its real width, off the end of its box.
        //
        // Converted in the one place measure and paint both go through, so they cannot
        // drift apart. It is also what makes the two TextMeasure implementations agree:
        // MonoMeasure adds tracking as raw px (scene.rs), which is right for the CSS
        // meaning, so before this the two differed by a factor of the font size.
        .letter_spacing(letter_spacing_px.max(0.0) / size_px.max(1.0))
}

/// The identity of one rasterized run. `size_bits`/`color` are the bit patterns of
/// the f32 inputs so the key is `Eq + Hash` (f32 is neither). The box (w,h) is part
/// of the key because layout width changes the wrap/clip.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct RunKey {
    text: String,
    size_bits: u32,
    w: u32,
    h: u32,
    color: u32,
    /// Outline width in px, as bits. Part of the identity because a stroked run and a
    /// filled one are different pictures of the same string.
    stroke_bits: u32,
    /// The CSS weight. Part of the identity for the same reason: the same string at the
    /// same size in bold is a different picture, and without this the first run to be
    /// cached would answer for every weight after it.
    weight: u16,
    /// Letter spacing in px, as bits, for the same reason again.
    tracking_bits: u32,
    /// Whether the run was shaped in the ICON face. Part of the identity because the
    /// same string is a different picture in each: "storage" is one glyph in Material
    /// Symbols and seven letters in sans, so without this the first of the two to be
    /// cached would answer for the other.
    icon: bool,
}

/// The most runs kept alive at once.
///
/// Every distinct (text, size, box, colour) holds a full RGBA buffer, and a hero line at
/// 34px across a wide box is a quarter of a megabyte. The cache key includes the STRING,
/// and the strings arrive from the A2UI feed: they are agent-written and change every time
/// the home recomposes. So an unbounded map is not a cache, it is a log of everything the
/// agent has ever said, held in the compositor for the life of the session, on a box that
/// already runs close to its memory limit.
///
/// A live desktop shows a few dozen runs, so this is a wide margin around normal use.
const MAX_CACHED_RUNS: usize = 256;

/// Grow an alpha mask by `r` px, as a separable max filter: horizontal then vertical.
///
/// Separable because a square max is the composition of two line maxes, which turns the
/// cost from r squared per pixel into 2r. The mask is small (one run's box) and this runs
/// only on a cache miss, but a 116px numeral with a 3px stroke is still a quarter of a
/// million comparisons the naive form would do four times over.
fn dilate(mask: &[u8], w: u32, h: u32, r: u32) -> Vec<u8> {
    let (wi, hi) = (w as usize, h as usize);
    let r = r as usize;
    let mut horizontal = vec![0u8; wi * hi];
    for y in 0..hi {
        for x in 0..wi {
            let lo = x.saturating_sub(r);
            let hi_x = (x + r).min(wi.saturating_sub(1));
            let mut m = 0u8;
            for k in lo..=hi_x {
                m = m.max(mask[y * wi + k]);
            }
            horizontal[y * wi + x] = m;
        }
    }
    let mut out = vec![0u8; wi * hi];
    for y in 0..hi {
        let lo = y.saturating_sub(r);
        let hi_y = (y + r).min(hi.saturating_sub(1));
        for x in 0..wi {
            let mut m = 0u8;
            for k in lo..=hi_y {
                m = m.max(horizontal[k * wi + x]);
            }
            out[y * wi + x] = m;
        }
    }
    out
}

fn pack_color(c: [f32; 4]) -> u32 {
    let b = |x: f32| (x.clamp(0.0, 1.0) * 255.0).round() as u32;
    (b(c[0]) << 24) | (b(c[1]) << 16) | (b(c[2]) << 8) | b(c[3])
}

/// Owns the cosmic-text engine and the per-run buffer cache. Constructed ONCE and
/// held on the compositor State (accessed via `CompState::text_rasterizer_mut`), so
/// `FontSystem::new()` (which enumerates system fonts) runs a single time, not per
/// frame.
pub struct TextRasterizer {
    font_system: FontSystem,
    swash_cache: SwashCache,
    cache: HashMap<RunKey, MemoryRenderBuffer>,
    /// How many runs were ever actually shaped and drawn. The compose-once PROOF: a
    /// steady desktop must not grow this per frame. Without it, a key that accidentally
    /// carried something unstable would re-shape every run every frame and nothing would
    /// notice, which is the expensive failure this cache exists to prevent.
    composes: u64,
}

impl Default for TextRasterizer {
    fn default() -> Self {
        Self::new()
    }
}

impl TextRasterizer {
    pub fn new() -> Self {
        TextRasterizer {
            font_system: FontSystem::new(),
            swash_cache: SwashCache::new(),
            cache: HashMap::new(),
            composes: 0,
        }
    }

    /// Total runs ever composed (test hook for the compose-once proof).
    pub fn composes(&self) -> u64 {
        self.composes
    }

    /// How many runs are cached right now (test hook for the bounded-cache proof).
    pub fn cached_runs(&self) -> usize {
        self.cache.len()
    }

    /// Shape `text` at `size_px` on ONE unwrapped line and report its advance width.
    /// This is the same shaping `compose` does, minus the rasterization, so the layout and
    /// the glyphs it later draws agree by construction rather than by a fudge factor.
    /// Not cached: it runs on a scene-tree rebuild, which the retained tree already makes
    /// rare, so a cache here would hold strings that are never asked for twice.
    fn measure(&mut self, text: &str, size_px: f32, weight: u16, letter_spacing: f32, icon: bool) -> f32 {
        if text.is_empty() {
            return 0.0;
        }
        // The same empty-font-DB guard compose() carries: cosmic-text's shaper panics with
        // no face to fall back to, so degrade to the font-free estimate rather than die.
        if self.font_system.db().is_empty() {
            return crate::scene::MonoMeasure.text_width(text, size_px, weight, letter_spacing);
        }
        let metrics = Metrics::new(size_px, size_px * 1.3);
        let mut buffer = Buffer::new(&mut self.font_system, metrics);
        // No width bound: a measure must never wrap, or a long run would report the width
        // of its wrapped box instead of its own advance.
        buffer.set_size(&mut self.font_system, None, None);
        buffer.set_text(&mut self.font_system, text, &attrs_for(weight, letter_spacing, size_px, icon), Shaping::Advanced);
        buffer.shape_until_scroll(&mut self.font_system, false);
        buffer
            .layout_runs()
            .map(|run| run.line_w)
            .fold(0.0_f32, f32::max)
    }

    /// Rasterize `text` at `size_px` into a `w x h` premultiplied-ARGB buffer at
    /// `color` (straight RGBA, 0..1), cached. Returns the cached buffer, ready for
    /// `MemoryRenderBufferRenderElement::from_buffer`. A zero/negative box floors to
    /// 1px so the buffer is always valid. The run is composed only on a cache miss.
    pub fn rasterize(
        &mut self,
        text: &str,
        size_px: f32,
        w: i32,
        h: i32,
        color: [f32; 4],
        stroke_px: f32,
        weight: u16,
        letter_spacing: f32,
        icon: bool,
    ) -> &MemoryRenderBuffer {
        let wi = w.max(1) as u32;
        let hi = h.max(1) as u32;
        let key = RunKey {
            text: text.to_string(),
            size_bits: size_px.to_bits(),
            w: wi,
            h: hi,
            color: pack_color(color),
            stroke_bits: stroke_px.max(0.0).to_bits(),
            weight,
            tracking_bits: letter_spacing.max(0.0).to_bits(),
            icon,
        };
        if !self.cache.contains_key(&key) {
            // Dropped wholesale rather than evicted one at a time. A run that has fallen
            // out of the live set is never asked for again, so the handful that ARE live
            // simply recompose once on the next frames, while maintaining an LRU ordering
            // would cost something every frame to save a recompose that happens once in
            // many thousands. The margin above the live set is what keeps this rare.
            if self.cache.len() >= MAX_CACHED_RUNS {
                self.cache.clear();
            }
            let buf = self.compose(text, size_px, wi, hi, color, stroke_px, weight, letter_spacing, icon);
            self.cache.insert(key.clone(), buf);
            self.composes += 1;
        }
        // Present after the insert above.
        self.cache.get(&key).expect("just inserted")
    }

    /// The actual compose: shape the run, draw its glyph coverage into a
    /// premultiplied-ARGB byte buffer (B,G,R,A little-endian, matching bloom.rs), and
    /// wrap it as a `MemoryRenderBuffer`.
    fn compose(
        &mut self,
        text: &str,
        size_px: f32,
        wi: u32,
        hi: u32,
        color: [f32; 4],
        stroke_px: f32,
        weight: u16,
        letter_spacing: f32,        icon: bool,
    ) -> MemoryRenderBuffer {
        let mut rgba = vec![0u8; (wi * hi * 4) as usize];

        // cosmic-text's shaper PANICS when the font database is empty (no face to fall
        // back to), so an absent-fonts environment would crash the compositor rather
        // than just render blank. A configured desktop has faces via fonts.packages,
        // but the native shell must DEGRADE (blank text), never die, if fonts are
        // somehow missing (early boot before the font path mounts, a misconfig). This
        // also lets the render path run in a font-less CI sandbox. compose() only runs
        // on a cache miss, so the check is free in steady state.
        if self.font_system.db().is_empty() {
            return MemoryRenderBuffer::from_slice(
                &rgba,
                Fourcc::Argb8888,
                (wi as i32, hi as i32),
                1,
                Transform::Normal,
                None,
            );
        }

        let metrics = Metrics::new(size_px, size_px * 1.3);
        let mut buffer = Buffer::new(&mut self.font_system, metrics);
        buffer.set_size(&mut self.font_system, Some(wi as f32), Some(hi as f32));
        buffer.set_text(&mut self.font_system, text, &attrs_for(weight, letter_spacing, size_px, icon), Shaping::Advanced);
        // `draw` is `&self`, so the run must be shaped first (shaping needs `&mut`).
        buffer.shape_until_scroll(&mut self.font_system, false);

        let ct_color = CtColor::rgba(
            (color[0].clamp(0.0, 1.0) * 255.0) as u8,
            (color[1].clamp(0.0, 1.0) * 255.0) as u8,
            (color[2].clamp(0.0, 1.0) * 255.0) as u8,
            (color[3].clamp(0.0, 1.0) * 255.0) as u8,
        );

        // A STROKED run is a different picture, so it takes a different pass: gather the
        // glyph's coverage into a mask, grow it, and subtract the original so the middle
        // stays hollow. That is what an outline IS. Filling the glyph at low alpha would
        // look like a faded numeral, not a hollow one, which is the difference between
        // parity and a lookalike.
        if stroke_px > 0.0 {
            let mut mask = vec![0u8; (wi * hi) as usize];
            buffer.draw(
                &mut self.font_system,
                &mut self.swash_cache,
                ct_color,
                |gx, gy, gw, gh, gc| {
                    let a = gc.a();
                    if a == 0 {
                        return;
                    }
                    for row in 0..gh as i32 {
                        let py = gy + row;
                        if py < 0 || py >= hi as i32 {
                            continue;
                        }
                        for col in 0..gw as i32 {
                            let px = gx + col;
                            if px < 0 || px >= wi as i32 {
                                continue;
                            }
                            let i = (py as u32 * wi + px as u32) as usize;
                            mask[i] = mask[i].max(a);
                        }
                    }
                },
            );
            let grown = dilate(&mask, wi, hi, stroke_px.round().max(1.0) as u32);
            for i in 0..(wi * hi) as usize {
                // Outside the glyph only: grown minus original leaves the band.
                let a = grown[i].saturating_sub(mask[i]) as u32;
                if a == 0 {
                    continue;
                }
                let a = a * (color[3].clamp(0.0, 1.0) * 255.0) as u32 / 255;
                let idx = i * 4;
                rgba[idx] = ((color[2].clamp(0.0, 1.0) * 255.0) as u32 * a / 255) as u8;
                rgba[idx + 1] = ((color[1].clamp(0.0, 1.0) * 255.0) as u32 * a / 255) as u8;
                rgba[idx + 2] = ((color[0].clamp(0.0, 1.0) * 255.0) as u32 * a / 255) as u8;
                rgba[idx + 3] = a as u8;
            }
            return MemoryRenderBuffer::from_slice(
                &rgba,
                Fourcc::Argb8888,
                (wi as i32, hi as i32),
                1,
                Transform::Normal,
                None,
            );
        }

        buffer.draw(
            &mut self.font_system,
            &mut self.swash_cache,
            ct_color,
            |gx, gy, gw, gh, gc| {
                // cosmic-text hands one solid-colour rect per coverage cell: gc.rgb is
                // the run colour, gc.a is the coverage. Source-over composite it,
                // PREMULTIPLIED, into the B,G,R,A buffer.
                let a = gc.a() as u32;
                if a == 0 {
                    return;
                }
                let inv = 255 - a;
                let spb = gc.b() as u32 * a / 255;
                let spg = gc.g() as u32 * a / 255;
                let spr = gc.r() as u32 * a / 255;
                for row in 0..gh as i32 {
                    let py = gy + row;
                    if py < 0 || py >= hi as i32 {
                        continue;
                    }
                    for col in 0..gw as i32 {
                        let px = gx + col;
                        if px < 0 || px >= wi as i32 {
                            continue;
                        }
                        let idx = (((py as u32) * wi + (px as u32)) * 4) as usize;
                        let db = rgba[idx] as u32;
                        let dg = rgba[idx + 1] as u32;
                        let dr = rgba[idx + 2] as u32;
                        let da = rgba[idx + 3] as u32;
                        rgba[idx] = (spb + db * inv / 255) as u8;
                        rgba[idx + 1] = (spg + dg * inv / 255) as u8;
                        rgba[idx + 2] = (spr + dr * inv / 255) as u8;
                        rgba[idx + 3] = (a + da * inv / 255) as u8;
                    }
                }
            },
        );

        MemoryRenderBuffer::from_slice(
            &rgba,
            Fourcc::Argb8888,
            (wi as i32, hi as i32),
            1,
            Transform::Normal,
            None,
        )
    }
}

/// The scene's measure, answered by the renderer that already shapes. Keeping the trait
/// in scene.rs (pure geometry, no smithay) and the impl here is what lets layout ask for
/// a width without scene.rs ever depending on a text stack.
impl TextMeasure for TextRasterizer {
    fn text_width(&mut self, text: &str, size_px: f32, weight: u16, letter_spacing: f32) -> f32 {
        self.measure(text, size_px, weight, letter_spacing, false)
    }

    /// The width of a run shaped in the ICON face, which is a different number entirely:
    /// a ligature name collapses to ONE glyph, so "sd_card_alert" measured as text is
    /// thirteen characters wide and measured as an icon is one square. Layout centres
    /// icons in fixed slots, so measuring them as text put the glyph in the wrong place
    /// even once the face was being asked for.
    fn icon_width(&mut self, text: &str, size_px: f32) -> f32 {
        self.measure(text, size_px, 400, 0.0, true)
    }

    /// True when fontconfig has handed us THE face the shaper will actually ask for.
    ///
    /// It used to accept any family whose name began with "Material", which proved the
    /// font was INSTALLED and never that a run would be shaped with it. Those are
    /// different claims, and the gap between them was the whole bug: the box has eight
    /// Material families, so this answered true, `icons_available` admitted the runs, and
    /// nothing selected a family, so the tray painted the words "notifications",
    /// "palette", "shield" clipped into 32px slots.
    ///
    /// Now it names `ICON_FAMILY` exactly, so the question this answers and the family
    /// `attrs_for` requests are the same string. A host with the wrong Material family and
    /// not this one says false and the layout drops the icon, which is the safe direction
    /// and the behaviour this was always documented to have.
    fn has_icon_face(&self) -> bool {
        self.font_system
            .db()
            .faces()
            .any(|f| f.families.iter().any(|(name, _)| name == ICON_FAMILY))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn color_packs_and_is_stable() {
        // Distinct colours give distinct keys; the same colour is stable.
        assert_eq!(pack_color([1.0, 0.0, 0.0, 1.0]), pack_color([1.0, 0.0, 0.0, 1.0]));
        assert_ne!(pack_color([1.0, 0.0, 0.0, 1.0]), pack_color([0.0, 1.0, 0.0, 1.0]));
        assert_eq!(pack_color([1.0, 1.0, 1.0, 1.0]), 0xFFFFFFFF);
        // Clamp keeps it in range (no overflow/panic on out-of-gamut input).
        assert_eq!(pack_color([2.0, -1.0, 0.5, 1.0]) >> 24, 255);
    }

    #[test]
    fn run_key_distinguishes_size_and_box() {
        let mk = |t: &str, s: f32, w: u32, h: u32| RunKey {
            text: t.to_string(),
            size_bits: s.to_bits(),
            w,
            h,
            color: 0,
            stroke_bits: 0,
            weight: 400,
            tracking_bits: 0,
            icon: false,
        };
        assert_eq!(mk("hi", 14.0, 10, 10), mk("hi", 14.0, 10, 10));
        assert_ne!(mk("hi", 14.0, 10, 10), mk("hi", 15.0, 10, 10));
        assert_ne!(mk("hi", 14.0, 10, 10), mk("hi", 14.0, 20, 10));
    }

    #[test]
    fn dilate_grows_a_mark_by_the_radius_and_leaves_empty_masks_empty() {
        // 5x5, one lit pixel in the middle. Growing by 1 must light the 3x3 around it and
        // nothing further, which is the property the outline width depends on.
        let mut mask = vec![0u8; 25];
        mask[12] = 200;
        let g = dilate(&mask, 5, 5, 1);
        for y in 0..5usize {
            for x in 0..5usize {
                let near = y.abs_diff(2) <= 1 && x.abs_diff(2) <= 1;
                assert_eq!(g[y * 5 + x] > 0, near, "pixel {x},{y} grew wrong");
            }
        }
        // Radius 2 reaches the corners of a 5x5; an empty mask stays empty at any radius.
        assert!(dilate(&mask, 5, 5, 2).iter().all(|v| *v > 0));
        assert!(dilate(&vec![0u8; 25], 5, 5, 3).iter().all(|v| *v == 0));
    }

    #[test]
    fn a_stroked_run_is_a_different_cached_picture_from_a_filled_one() {
        // The stroke is part of the run's identity. Without that, asking for an outline
        // after a fill of the same string would hand back the fill.
        let mut r = TextRasterizer::new();
        let white = [1.0, 1.0, 1.0, 1.0];
        let before = r.composes();
        let _ = r.rasterize("7", 40.0, 60, 60, white, 0.0, 400, 0.0, false);
        let _ = r.rasterize("7", 40.0, 60, 60, white, 3.0, 400, 0.0, false);
        assert_eq!(r.composes(), before + 2, "fill and outline compose separately");
        // And each is still cached in its own right.
        let _ = r.rasterize("7", 40.0, 60, 60, white, 3.0, 400, 0.0, false);
        assert_eq!(r.composes(), before + 2, "the outline is cached like any run");
    }

    #[test]
    fn the_run_cache_stays_bounded_under_an_agent_written_feed() {
        // The key includes the STRING, and the strings come from the A2UI feed, so they
        // change every time the agent recomposes the home. Unbounded, this cache would
        // hold a full RGBA buffer for every line the agent has ever written.
        let mut r = TextRasterizer::new();
        let white = [1.0, 1.0, 1.0, 1.0];
        for i in 0..(MAX_CACHED_RUNS * 3) {
            let _ = r.rasterize(&format!("earned ${i} overnight"), 12.0, 24, 14, white, 0.0, 400, 0.0, false);
        }
        assert!(
            r.cached_runs() <= MAX_CACHED_RUNS,
            "cache grew to {} past its {MAX_CACHED_RUNS} cap",
            r.cached_runs()
        );
        assert!(r.cached_runs() > 0, "it must still actually be a cache");

        // And it is still a cache after a sweep: a run asked for twice composes once.
        let before = r.composes();
        let _ = r.rasterize("steady", 12.0, 24, 14, white, 0.0, 400, 0.0, false);
        let after_first = r.composes();
        let _ = r.rasterize("steady", 12.0, 24, 14, white, 0.0, 400, 0.0, false);
        assert_eq!(after_first, before + 1, "the first ask composes");
        assert_eq!(r.composes(), after_first, "the second ask must hit the cache");
    }

    #[test]
    fn an_icon_run_and_a_text_run_of_the_same_string_are_different_pictures() {
        // "storage" is one square glyph in the Material face and seven letters in sans.
        // The cache key has to know which, or the first of the two to be composed answers
        // for the other forever, and a card icon becomes the word (or a title becomes an
        // icon). This is the same argument the stroke and weight fields carry.
        let mut r = TextRasterizer::new();
        let white = [1.0, 1.0, 1.0, 1.0];
        let before = r.composes();
        let _ = r.rasterize("storage", 20.0, 32, 26, white, 0.0, 400, 0.0, false);
        let _ = r.rasterize("storage", 20.0, 32, 26, white, 0.0, 400, 0.0, true);
        assert_eq!(
            r.composes(),
            before + 2,
            "the icon face and the UI face must compose separately"
        );
        // And each is still cached in its own right.
        let _ = r.rasterize("storage", 20.0, 32, 26, white, 0.0, 400, 0.0, true);
        assert_eq!(r.composes(), before + 2, "the icon run is cached like any run");
    }

    #[test]
    fn the_face_the_detector_looks_for_is_the_face_the_shaper_asks_for() {
        // The bug this closes: has_icon_face used to accept ANY family whose name began
        // with "Material", which proves the font is INSTALLED and never that a run will be
        // shaped with it. The box carries eight Material families, so it answered true,
        // layout admitted the icon runs, nothing selected a family, and the tray painted
        // the literal words "notifications", "palette", "shield".
        //
        // Detection and selection must name ONE string. Read out of the constant rather
        // than restated, so renaming the face cannot leave this passing against the old
        // name.
        let r = TextRasterizer::new();
        let exact = r
            .font_system
            .db()
            .faces()
            .any(|f| f.families.iter().any(|(n, _)| n == ICON_FAMILY));
        assert_eq!(
            r.has_icon_face(),
            exact,
            "the detector must answer by the exact family attrs_for requests"
        );

        // The near-miss is the whole point: a host with other Material families and not
        // this one must say NO, so layout drops the icon instead of drawing its name.
        let near_miss_only = !exact
            && r.font_system
                .db()
                .faces()
                .any(|f| f.families.iter().any(|(n, _)| n.starts_with("Material")));
        if near_miss_only {
            assert!(
                !r.has_icon_face(),
                "other Material families must not stand in for {ICON_FAMILY}"
            );
        }
    }

    #[test]
    fn tracking_is_asked_for_in_px_and_moves_the_advance_by_px() {
        // THE UNIT BUG. cosmic-text takes tracking in EM; the scene passes CSS px, under
        // citations like `.hh-eyebrow { letter-spacing: 3px }`. Handed over unconverted,
        // 3.0 meant 3 EM, so a 16px run grew by 48px per gap instead of 3px, and the
        // eyebrow sprawled about five times its width.
        //
        // Holds whether or not this host has fonts: with an empty database `measure`
        // degrades to MonoMeasure, which adds tracking as raw px, and that is the same
        // answer this asserts. So the test states the CONTRACT rather than one backend.
        let mut r = TextRasterizer::new();
        let text = "EARNED ON THE HIVE";
        let chars = text.chars().count() as f32;
        let size = 16.0;
        let spacing = 3.0;

        let plain = r.measure(text, size, 700, 0.0, false);
        let spaced = r.measure(text, size, 700, spacing, false);
        let grew = spaced - plain;

        // ONE GAP PER CHARACTER, including the last. cosmic-text adds the tracking onto
        // every glyph's advance, and so does CSS: `letter-spacing` is applied after each
        // character, which is why a tracked run carries a trailing gap in a browser too.
        // MonoMeasure uses (n - 1) instead, so the font-free estimate is one gap short of
        // the real shaper. That is 3px on this run and it only affects the fallback, but
        // it is a real difference and better written down than smoothed over by a loose
        // tolerance -- which is what an earlier version of this test did.
        let want = chars * spacing;
        assert!(
            (grew - want).abs() <= 1.0,
            "tracking must move the advance by px, one gap per character: expected about {want}, got {grew} (plain {plain}, spaced {spaced})"
        );
        // And the shape of the bug this test exists for, so it cannot come back quietly:
        // read as EM, the growth would have been multiplied by the font size.
        assert!(
            grew < want * size * 0.5,
            "tracking looks like it is being read as EM again: grew {grew} for {chars} characters at {spacing}px"
        );
    }

    #[test]
    fn the_measure_is_finite_and_grows_with_length_and_size() {
        // Holds on BOTH paths: with faces present this is cosmic-text's shaped advance,
        // and with an empty font database it is the MonoMeasure fallback. A layout that
        // consumed a NaN or a zero here would place every run on top of the last one, so
        // these are the properties the layout actually depends on.
        let mut r = TextRasterizer::new();
        assert_eq!(r.text_width("", 15.0, 400, 0.0), 0.0);
        let hart = r.text_width("HART", 15.0, 400, 0.0);
        assert!(hart.is_finite() && hart > 0.0, "measured {hart}");
        assert!(r.text_width("HART OS", 15.0, 400, 0.0) > hart);
        assert!(r.text_width("HART", 30.0, 400, 0.0) > hart);
        // Measuring must not WRAP: a long run reports its own advance, not a box width.
        let long = "HART OS native shell parity program wordmark run";
        assert!(r.text_width(long, 15.0, 400, 0.0) > r.text_width("HART OS", 15.0, 400, 0.0) * 3.0);
    }
}
