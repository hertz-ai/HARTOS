//! M1 of the NATIVE SHELL PARITY PROGRAM -- the aura cosmic bloom, drawn by
//! hart-comp itself.
//!
//! WHY THIS EXISTS (steward, 2026-07-20): "i wanted a native ui and we are
//! settling for a lesser 1". The desktop backdrop was a flat splash clear
//! (`HART_SPLASH_RGBA`) with the real bloom painted by a browser inside a
//! WebView. This module is the first pixel the COMPOSITOR owns: the same aurora
//! the HTML shell composes (`hartBloom.js`), produced natively.
//!
//! PERFORMANCE CONTRACT (the program's binding NFRs -- "ultrafast and snappy...
//! no lag whatsoever"):
//!   * COMPOSE ONCE, REUSE FOREVER. The gaussian-ish falloff is evaluated on the
//!     CPU exactly once per (size, palette) and cached as an RGBA buffer the
//!     renderer imports as a texture. There is NO per-frame blur, no per-frame
//!     allocation, and no shader dependency -- so it is identical on the GLES
//!     GPU path and on the pixman software floor (the never-fail renderer of
//!     record). Re-composed ONLY when the output size or the theme palette
//!     changes.
//!   * It is a plain element in the existing frame builder, so the #137 frame
//!     budget gate still skips idle ticks: a static desktop paints ZERO times.
//!
//! PARITY SOURCE: the palette is the SAME `nixos/assets/conky-themes/*.json`
//! the HTML shell reads (ambient_1..4 + background). One palette source for both
//! renderers -- no parallel theme table (Gate 4). The blob field mirrors
//! hartBloom.js: violet lead, cyan upper-right, pink lower-left, amber accent,
//! violet reinforce, additively blended over the deep base.

use std::path::Path;

/// Deep base + four ambient hues, linear RGB in 0..=255. Mirrors the aura theme
/// (`background` + `ambient_1..4`); the fallbacks ARE aura's values so a missing
/// or unreadable theme file still paints the shipped desktop, never a void.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct BloomPalette {
    pub base: [u8; 3],
    pub amb: [[u8; 3]; 4],
}

impl Default for BloomPalette {
    fn default() -> Self {
        // aura.json: background 04050B, ambient_1 B182FF, ambient_2 00DDF9,
        // ambient_3 FB66B6, ambient_4 FFB330.
        BloomPalette {
            base: [0x04, 0x05, 0x0B],
            amb: [
                [0xB1, 0x82, 0xFF],
                [0x00, 0xDD, 0xF9],
                [0xFB, 0x66, 0xB6],
                [0xFF, 0xB3, 0x30],
            ],
        }
    }
}

fn hex3(s: &str) -> Option<[u8; 3]> {
    let s = s.trim().trim_start_matches('#');
    if s.len() < 6 {
        return None;
    }
    let b = s.as_bytes();
    let h = |i: usize| -> Option<u8> {
        let hi = (b[i] as char).to_digit(16)?;
        let lo = (b[i + 1] as char).to_digit(16)?;
        Some((hi * 16 + lo) as u8)
    };
    Some([h(0)?, h(2)?, h(4)?])
}

/// Read the palette from a theme JSON without pulling a JSON dependency into the
/// compositor: the file is a flat `"key": "VALUE"` map for the fields we need, so
/// a scan for each key is enough and cannot panic on malformed input. Any field
/// that does not parse keeps its aura default (fail-to-shipped-look, never void).
pub fn palette_from_theme_file(path: &Path) -> BloomPalette {
    let mut p = BloomPalette::default();
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(_) => return p,
    };
    let find = |key: &str| -> Option<[u8; 3]> {
        let k = format!("\"{}\"", key);
        let i = text.find(&k)?;
        let rest = &text[i + k.len()..];
        let c = rest.find(':')?;
        let rest = &rest[c + 1..];
        let q1 = rest.find('"')?;
        let rest2 = &rest[q1 + 1..];
        let q2 = rest2.find('"')?;
        hex3(&rest2[..q2])
    };
    if let Some(v) = find("background") {
        p.base = v;
    }
    for (i, key) in ["ambient_1", "ambient_2", "ambient_3", "ambient_4"].iter().enumerate() {
        if let Some(v) = find(key) {
            p.amb[i] = v;
        }
    }
    p
}

/// Where the shipped theme JSONs land at runtime. This is the SAME directory
/// `hart-liquid-ui.nix` hands the HTML shell as `HART_THEME_DIR`, so both
/// renderers read one palette source (Gate 4: no parallel theme table).
const THEME_DIR_DEFAULT: &str = "/run/current-system/sw/share/hart/conky-themes";

/// Resolve the active palette from the environment, degrading at every step.
///
/// `HART_THEME_DIR` / `HART_THEME` follow the convention the conky + liquid-ui
/// modules already export. Every failure path lands on aura's shipped values
/// rather than a void, because this is the DESKTOP BACKDROP: an unreadable theme
/// file must never produce a black screen the user cannot explain.
pub fn theme_palette() -> BloomPalette {
    let dir = std::env::var("HART_THEME_DIR").unwrap_or_else(|_| THEME_DIR_DEFAULT.to_string());
    let id = std::env::var("HART_THEME").unwrap_or_else(|_| "aura".to_string());
    theme_palette_from(&dir, &id)
}

/// The resolution rule itself, with the environment read out of the way.
///
/// Split from `theme_palette` so it is testable WITHOUT mutating process-global
/// environment: cargo runs tests as parallel threads in one process, so an
/// env-mutating test would race every other test in this module.
pub fn theme_palette_from(dir: &str, id: &str) -> BloomPalette {
    // Reject a theme id that could escape the theme directory. The id reaches us
    // from the environment, and a path separator would let it name any file on
    // disk; a bad id falls back to the shipped look rather than reading around.
    if id.is_empty() || id.contains('/') || id.contains('\\') || id.contains("..") {
        return BloomPalette::default();
    }
    palette_from_theme_file(&Path::new(dir).join(format!("{}.json", id)))
}

/// One additive radial blob: centre as a fraction of the output, radius as a
/// fraction of the longer edge, peak intensity 0..1. Mirrors hartBloom.js.
struct Blob {
    cx: f32,
    cy: f32,
    r: f32,
    hue: usize,
    a: f32,
}

const BLOBS: [Blob; 5] = [
    Blob { cx: 0.32, cy: 0.40, r: 0.42, hue: 0, a: 0.42 }, // violet lead
    Blob { cx: 0.84, cy: 0.24, r: 0.34, hue: 1, a: 0.30 }, // cyan upper-right
    Blob { cx: 0.20, cy: 0.84, r: 0.34, hue: 2, a: 0.22 }, // pink lower-left
    Blob { cx: 0.86, cy: 0.84, r: 0.25, hue: 3, a: 0.18 }, // amber accent
    Blob { cx: 0.58, cy: 0.62, r: 0.30, hue: 0, a: 0.20 }, // violet reinforce
];

/// Compose the bloom field ONCE into a premultiplied ARGB8888 buffer.
///
/// The falloff is `smoothstep(1 - d/r)^2` -- visually a gaussian, but evaluated
/// with two multiplies per blob per pixel and no kernel pass, so a full 1366x768
/// compose is a few milliseconds ONCE, versus a per-frame blur that would cost
/// that every 16ms (the exact tax the WebView shell was paying).
///
/// Returns rows of `Argb8888` (B,G,R,A byte order in little-endian u32), the
/// layout `MemoryRenderBuffer::from_slice` expects, fully opaque.
pub fn compose(width: i32, height: i32, pal: &BloomPalette) -> Vec<u8> {
    let w = width.max(1) as usize;
    let h = height.max(1) as usize;
    let mut buf = vec![0u8; w * h * 4];
    let maxdim = w.max(h) as f32;
    // Precompute per-blob pixel-space centres/radii so the inner loop is pure math.
    let blobs: Vec<(f32, f32, f32, [u8; 3], f32)> = BLOBS
        .iter()
        .map(|b| {
            (
                b.cx * w as f32,
                b.cy * h as f32,
                (b.r * maxdim).max(1.0),
                pal.amb[b.hue],
                b.a,
            )
        })
        .collect();

    for y in 0..h {
        let fy = y as f32;
        let row = y * w * 4;
        for x in 0..w {
            let fx = x as f32;
            let mut r = pal.base[0] as f32;
            let mut g = pal.base[1] as f32;
            let mut b = pal.base[2] as f32;
            for (cx, cy, rad, hue, amp) in &blobs {
                let dx = fx - cx;
                let dy = fy - cy;
                let d2 = dx * dx + dy * dy;
                let r2 = rad * rad;
                if d2 >= r2 {
                    continue;
                }
                // t: 1 at centre -> 0 at edge; squared for a soft shoulder.
                let t = 1.0 - (d2 / r2).sqrt();
                let f = t * t * amp;
                r += hue[0] as f32 * f;
                g += hue[1] as f32 * f;
                b += hue[2] as f32 * f;
            }
            let i = row + x * 4;
            // Argb8888 little-endian => bytes are B, G, R, A. Opaque alpha, and the
            // colour is already "premultiplied" because alpha is 255.
            buf[i] = b.min(255.0) as u8;
            buf[i + 1] = g.min(255.0) as u8;
            buf[i + 2] = r.min(255.0) as u8;
            buf[i + 3] = 255;
        }
    }
    buf
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compose_fills_every_pixel_opaque() {
        let p = BloomPalette::default();
        let (w, h) = (64, 40);
        let buf = compose(w, h, &p);
        assert_eq!(buf.len(), (w * h * 4) as usize);
        // Every pixel opaque: a transparent backdrop would show the splash clear
        // through the desktop (a visible seam).
        assert!(buf.chunks(4).all(|px| px[3] == 255));
    }

    #[test]
    fn corner_is_near_base_and_violet_lead_is_brighter() {
        let p = BloomPalette::default();
        let (w, h) = (128, 96);
        let buf = compose(w, h, &p);
        let at = |x: usize, y: usize| -> [u8; 4] {
            let i = (y * w as usize + x) * 4;
            [buf[i], buf[i + 1], buf[i + 2], buf[i + 3]]
        };
        // Bottom-right-ish corner sits outside the violet lead: close to the deep base.
        let corner = at(w as usize - 1, 0);
        // The violet lead blob centre must be measurably brighter than that corner
        // (this is the actual "there is an aurora" assertion).
        let lead = at((0.32 * w as f32) as usize, (0.40 * h as f32) as usize);
        let lum = |c: [u8; 4]| c[0] as u32 + c[1] as u32 + c[2] as u32;
        assert!(
            lum(lead) > lum(corner) + 40,
            "violet lead ({:?}) is not brighter than the far corner ({:?}) -- the bloom is flat",
            lead,
            corner
        );
    }

    #[test]
    fn palette_parses_hex_and_falls_back_to_aura() {
        assert_eq!(hex3("#B182FF"), Some([0xB1, 0x82, 0xFF]));
        assert_eq!(hex3("04050B"), Some([0x04, 0x05, 0x0B]));
        assert_eq!(hex3("nope"), None);
        // A missing file must yield the shipped aura look, never a void.
        let p = palette_from_theme_file(Path::new("/definitely/not/here.json"));
        assert_eq!(p, BloomPalette::default());
    }

    #[test]
    fn palette_reads_the_real_theme_shape() {
        let dir = std::env::temp_dir().join("hart_bloom_test");
        std::fs::create_dir_all(&dir).unwrap();
        let f = dir.join("aura.json");
        std::fs::write(
            &f,
            r#"{"id":"aura","colors":{"background":"04050B","accent":"00E6C3",
               "ambient_1":"B182FF","ambient_2":"00DDF9","ambient_3":"FB66B6",
               "ambient_4":"FFB330"}}"#,
        )
        .unwrap();
        let p = palette_from_theme_file(&f);
        assert_eq!(p.base, [0x04, 0x05, 0x0B]);
        assert_eq!(p.amb[1], [0x00, 0xDD, 0xF9]);
        assert_eq!(p.amb[3], [0xFF, 0xB3, 0x30]);
    }

    #[test]
    fn theme_id_that_escapes_the_theme_dir_is_rejected() {
        // The id arrives from the environment. A separator would let it name any
        // file on disk, so a hostile or fat-fingered value must land on the
        // shipped look rather than reading around the theme directory.
        for bad in ["../../etc/shadow", "a/b", "a\\b", "..", ""] {
            assert_eq!(
                theme_palette_from("/share/hart/conky-themes", bad),
                BloomPalette::default(),
                "id {:?} was not rejected",
                bad
            );
        }
    }

    #[test]
    fn theme_is_read_from_the_named_dir_and_missing_keys_keep_aura() {
        let dir = std::env::temp_dir().join("hart_bloom_resolve_test");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("moss.json"),
            r#"{"colors":{"background":"010203","ambient_1":"0A0B0C"}}"#,
        )
        .unwrap();
        let p = theme_palette_from(dir.to_str().unwrap(), "moss");
        assert_eq!(p.base, [0x01, 0x02, 0x03]);
        assert_eq!(p.amb[0], [0x0A, 0x0B, 0x0C]);
        // Ambients the file does not mention keep aura's values, never black:
        // a partial theme must not punch holes in the backdrop.
        assert_eq!(p.amb[3], BloomPalette::default().amb[3]);
    }

    #[test]
    fn a_theme_that_is_not_installed_still_paints_the_shipped_look() {
        assert_eq!(
            theme_palette_from("/nonexistent/theme/dir", "whatever"),
            BloomPalette::default()
        );
    }

    #[test]
    fn recompose_is_deterministic() {
        // The cache key is (size, palette): the same inputs MUST yield identical
        // bytes, otherwise the "compose once" contract would silently re-upload a
        // different texture and the desktop would shimmer.
        let p = BloomPalette::default();
        assert_eq!(compose(80, 60, &p), compose(80, 60, &p));
    }
}
