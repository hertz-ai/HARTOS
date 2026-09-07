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

use cosmic_text::{Attrs, Buffer, Color as CtColor, FontSystem, Metrics, Shaping, SwashCache};
use smithay::backend::allocator::Fourcc;
use smithay::backend::renderer::element::memory::MemoryRenderBuffer;
use smithay::utils::Transform;

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
        }
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
    ) -> &MemoryRenderBuffer {
        let wi = w.max(1) as u32;
        let hi = h.max(1) as u32;
        let key = RunKey {
            text: text.to_string(),
            size_bits: size_px.to_bits(),
            w: wi,
            h: hi,
            color: pack_color(color),
        };
        if !self.cache.contains_key(&key) {
            let buf = self.compose(text, size_px, wi, hi, color);
            self.cache.insert(key.clone(), buf);
        }
        // Present after the insert above.
        self.cache.get(&key).expect("just inserted")
    }

    /// The actual compose: shape the run, draw its glyph coverage into a
    /// premultiplied-ARGB byte buffer (B,G,R,A little-endian, matching bloom.rs), and
    /// wrap it as a `MemoryRenderBuffer`.
    fn compose(&mut self, text: &str, size_px: f32, wi: u32, hi: u32, color: [f32; 4]) -> MemoryRenderBuffer {
        let mut rgba = vec![0u8; (wi * hi * 4) as usize];

        // cosmic-text's shaper PANICS when the font database is empty (no face to fall
        // back to), so an absent-fonts environment would crash the compositor rather
        // than just render blank. A configured desktop has faces via fonts.packages,
        // but the native shell must DEGRADE (blank text), never die, if fonts are
        // somehow missing (early boot before the font path mounts, a misconfig). This
        // also lets the render path run in a font-less CI sandbox. compose() only runs
        // on a cache miss, so the check is free in steady state.
        if self.font_system.db().len() == 0 {
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
        buffer.set_text(&mut self.font_system, text, &Attrs::new(), Shaping::Advanced);
        // `draw` is `&self`, so the run must be shaped first (shaping needs `&mut`).
        buffer.shape_until_scroll(&mut self.font_system, false);

        let ct_color = CtColor::rgba(
            (color[0].clamp(0.0, 1.0) * 255.0) as u8,
            (color[1].clamp(0.0, 1.0) * 255.0) as u8,
            (color[2].clamp(0.0, 1.0) * 255.0) as u8,
            (color[3].clamp(0.0, 1.0) * 255.0) as u8,
        );

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
        };
        assert_eq!(mk("hi", 14.0, 10, 10), mk("hi", 14.0, 10, 10));
        assert_ne!(mk("hi", 14.0, 10, 10), mk("hi", 15.0, 10, 10));
        assert_ne!(mk("hi", 14.0, 10, 10), mk("hi", 14.0, 20, 10));
    }
}
