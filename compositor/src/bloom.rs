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

impl BloomPalette {
    /// This palette with a composed MOOD folded in: the shell's resolution of the
    /// `mood` id, over the theme file's own hues, one slot at a time.
    ///
    /// The HTML bloom is `.hart-ambient` reading `--hart-amb-1..4` and the body reading
    /// `--hart-background`, both of which `paintPalette` rewrites from the mood's quad
    /// and `b`. This is the same recolour on the native backdrop, and it is what
    /// "mood reaches the pixels" mostly means: the Aura moods pin the functional accent
    /// teal and change ONLY this field. A slot the mood is silent on (the classic
    /// palettes name no a3/a4) keeps the theme's own hue, which is exactly what an
    /// unset CSS variable does.
    ///
    /// The cache key includes the result, so a mood change recomposes the field ONCE
    /// and a repeated mood does not recompose at all (compose-once NFR).
    pub fn with_mood(mut self, mood: &crate::scene::MoodPalette) -> BloomPalette {
        let to_u8 = |c: crate::scene::Color| -> [u8; 3] {
            let ch = |v: f32| (v.clamp(0.0, 1.0) * 255.0).round() as u8;
            [ch(c.r), ch(c.g), ch(c.b)]
        };
        if let Some(b) = mood.background {
            self.base = to_u8(b);
        }
        for (slot, hue) in self.amb.iter_mut().zip(mood.ambient.iter()) {
            if let Some(c) = hue {
                *slot = to_u8(*c);
            }
        }
        self
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

/// One loaded settings JSON, and the ONE reader for that shape in this process.
///
/// Born as the theme reader: the backdrop and the native scene both need colours out of
/// `conky-themes/<id>.json`, and the scene's `Theme` was a hardcoded copy of what that
/// file already carries, which is a parallel theme table inside a single binary, exactly
/// what Gate 4 forbids and exactly what a user changing their theme would have
/// discovered, the backdrop restyling under a desktop that did not.
///
/// It reads `/etc/hart/accessibility.json` too, which is the same shape and the same
/// posture, so the name is the shape rather than the subject. Two files, one scanner:
/// a second copy of this is how the drift it was written to end would start again.
///
/// No JSON dependency is pulled in for it even though the crate has one: the file is a
/// flat `"key": "VALUE"` map for every field either consumer needs, so a scan per key is
/// enough, cannot panic on malformed input, and cannot be made to allocate by a hostile
/// file. Any key that does not parse leaves the caller's default in place.
pub struct SettingsFile {
    text: Option<String>,
}

impl SettingsFile {
    /// Load the theme JSON at `path`. A missing or unreadable file is not an error: it
    /// yields a file that answers None to everything, so every caller keeps its shipped
    /// default. This is the desktop's own colours; an unreadable theme must degrade to
    /// the shipped look, never to a void.
    pub fn load(path: &Path) -> SettingsFile {
        SettingsFile {
            text: std::fs::read_to_string(path).ok(),
        }
    }

    /// Resolve the active theme file from the environment, degrading at every step.
    ///
    /// The FIRST of `theme_paths()` that exists: the theme the user is actually running
    /// (`active_theme.json`, the preset merged with their overrides, which the HTML
    /// shell renders from), and only when no theme has ever been applied the shipped
    /// preset the environment names.
    pub fn active() -> SettingsFile {
        SettingsFile::first_of(&theme_paths())
    }

    /// The first of `paths` that reads; none of them is the same absent file `load`
    /// answers for a missing path, so every caller keeps its shipped default.
    pub fn first_of(paths: &[std::path::PathBuf]) -> SettingsFile {
        for p in paths {
            let f = SettingsFile::load(p);
            if f.text.is_some() {
                return f;
            }
        }
        SettingsFile { text: None }
    }

    /// The preset file the environment names, or nothing when the id is unsafe.
    ///
    /// `HART_THEME_DIR` / `HART_THEME` follow the convention the conky + liquid-ui
    /// modules already export, so this is the same file the HTML shell is SEEDED from.
    pub fn preset_path() -> Option<std::path::PathBuf> {
        let dir = std::env::var("HART_THEME_DIR").unwrap_or_else(|_| THEME_DIR_DEFAULT.to_string());
        let id = std::env::var("HART_THEME").unwrap_or_else(|_| "aura".to_string());
        preset_path_in(&dir, &id)
    }

    /// The resolution rule with the environment read out of the way, so it is testable
    /// without mutating process-global state (cargo runs tests as threads in one
    /// process, and an env-mutating test would race every other test here).
    pub fn for_id(dir: &str, id: &str) -> SettingsFile {
        match preset_path_in(dir, id) {
            Some(p) => SettingsFile::load(&p),
            None => SettingsFile { text: None },
        }
    }

    /// The NUMERIC value of `key`, or None when it is absent or not a number.
    ///
    /// The shell's three shell-metric variables (`--hart-topbar-height`,
    /// `--hart-icon-size`, `--hart-radius`) come straight from this file's `shell` block,
    /// and the native scene hardcoded all three. Four of the ten shipped themes move the
    /// bar height and every one of them moves the corner radius, so that was not a
    /// theoretical drift: on `potato` the native bar would draw 40px over a 36px
    /// reservation, which is the 2026-08-29 "taskbar unreachable" report through a new
    /// renderer.
    ///
    /// Unquoted, unlike `hex`: JSON numbers carry no quotes, so this scans to the value
    /// separator and parses what follows up to the next delimiter. A malformed value
    /// yields None and the caller keeps its shipped default, the same posture every other
    /// read here takes.
    pub fn num(&self, key: &str) -> Option<f32> {
        let text = self.text.as_ref()?;
        let k = format!("\"{}\"", key);
        let i = text.find(&k)?;
        let rest = &text[i + k.len()..];
        let c = rest.find(':')?;
        let v = rest[c + 1..]
            .trim_start()
            .split([',', '}', '\n'])
            .next()?
            .trim();
        v.parse::<f32>().ok().filter(|n| n.is_finite())
    }

    /// The BOOLEAN value of `key`, or None when it is absent or not a JSON bool.
    ///
    /// `/etc/hart/accessibility.json` carries `reduced_motion`, and the CSS parity ledger
    /// is explicit that the shell's three motion kill-switches must all exist natively.
    /// The native scene honoured only the GPU floor, so a user who had declared reduced
    /// motion still got a breathing orb the moment the shell went native.
    ///
    /// Only the DECLARATIVE file is visible from here. A runtime PUT to
    /// /api/shell/accessibility lives in the shell process's memory, so it reaches the
    /// compositor at the next start, which is the same documented gap the theme and the
    /// backdrop palette already carry rather than a new one.
    pub fn flag(&self, key: &str) -> Option<bool> {
        let text = self.text.as_ref()?;
        let k = format!("\"{}\"", key);
        let i = text.find(&k)?;
        let rest = &text[i + k.len()..];
        let c = rest.find(':')?;
        match rest[c + 1..].trim_start() {
            v if v.starts_with("true") => Some(true),
            v if v.starts_with("false") => Some(false),
            _ => None,
        }
    }

    /// An `rgba(r, g, b, a)` value: three 0..255 channels and a 0..1 alpha.
    ///
    /// The theme writes its translucent colours this way rather than as hex, which is why
    /// `glass_border` could not be read before: `hex` finds no `#RRGGBB` and returns None,
    /// so the chrome strips had no separator and the shell's own 1px rule between the bars
    /// and the desktop simply did not exist natively. Spacing varies across the shipped
    /// themes (`rgba(255,255,255,0.10)` and `rgba(2, 136, 209, 0.15)` are both in the
    /// tree), so every field is trimmed.
    pub fn rgba(&self, key: &str) -> Option<([u8; 3], f32)> {
        let text = self.text.as_ref()?;
        let k = format!("\"{}\"", key);
        let i = text.find(&k)?;
        let rest = &text[i + k.len()..];
        let c = rest.find(':')?;
        let v = rest[c + 1..].trim_start();
        let open = v.find("rgba(")?;
        // Only accept it as the value itself, not something further down the file.
        if v[..open].trim_matches(['"', ' ']).len() > 1 {
            return None;
        }
        let body = &v[open + 5..];
        let close = body.find(')')?;
        let mut parts = body[..close].split(',');
        let ch = |p: Option<&str>| -> Option<u8> {
            let n = p?.trim().parse::<f32>().ok()?;
            Some(n.clamp(0.0, 255.0) as u8)
        };
        let rgb = [ch(parts.next())?, ch(parts.next())?, ch(parts.next())?];
        let a = parts.next()?.trim().parse::<f32>().ok()?;
        if !a.is_finite() {
            return None;
        }
        Some((rgb, a.clamp(0.0, 1.0)))
    }

    /// The `#RRGGBB` value of `key`, or None when the key is absent or malformed.
    pub fn hex(&self, key: &str) -> Option<[u8; 3]> {
        let text = self.text.as_ref()?;
        let k = format!("\"{}\"", key);
        let i = text.find(&k)?;
        let rest = &text[i + k.len()..];
        let c = rest.find(':')?;
        let rest = &rest[c + 1..];
        let q1 = rest.find('"')?;
        let rest2 = &rest[q1 + 1..];
        let q2 = rest2.find('"')?;
        hex3(&rest2[..q2])
    }
}

/// The backdrop palette out of a theme JSON. A thin consumer of `SettingsFile` now, so the
/// scan lives in one place rather than once per thing that needs a colour.
pub fn palette_from_theme_file(path: &Path) -> BloomPalette {
    palette_from(&SettingsFile::load(path))
}

/// The backdrop palette from an already-loaded file.
pub fn palette_from(file: &SettingsFile) -> BloomPalette {
    let mut p = BloomPalette::default();
    if let Some(v) = file.hex("background") {
        p.base = v;
    }
    for (i, key) in ["ambient_1", "ambient_2", "ambient_3", "ambient_4"].iter().enumerate() {
        if let Some(v) = file.hex(key) {
            p.amb[i] = v;
        }
    }
    p
}

/// Where the shipped theme JSONs land at runtime. This is the SAME directory
/// `hart-liquid-ui.nix` hands the HTML shell as `HART_THEME_DIR`, so both
/// renderers read one palette source (Gate 4: no parallel theme table).
const THEME_DIR_DEFAULT: &str = "/run/current-system/sw/share/hart/conky-themes";

/// `<dir>/<id>.json`, or None for an id that could escape the theme directory. The id
/// reaches us from the environment, and a path separator would let it name any file on
/// disk; a bad id falls back to the shipped look rather than reading around.
pub fn preset_path_in(dir: &str, id: &str) -> Option<std::path::PathBuf> {
    if id.is_empty() || id.contains('/') || id.contains('\\') || id.contains("..") {
        return None;
    }
    Some(Path::new(dir).join(format!("{}.json", id)))
}

/// The file the theme service writes the RUNNING theme to, inside the data dir: the
/// chosen preset merged with the user's custom overrides (`ThemeService.apply_theme`
/// and `update_custom` both rewrite it), and what `get_css_variables` renders the HTML
/// shell from. Before this the compositor read only the preset the environment named,
/// which is the SEED of that file and never changes: every theme the user picked
/// after first boot left the native desktop on aura.
pub const ACTIVE_THEME_FILE: &str = "active_theme.json";

/// The data dir, resolved as theme_service.py resolves it: `HEVOLVE_DATA_DIR`, then
/// `HART_DATA_DIR`, then the module default hart-base.nix gives every node.
pub fn data_dir() -> std::path::PathBuf {
    for var in ["HEVOLVE_DATA_DIR", "HART_DATA_DIR"] {
        if let Some(v) = std::env::var_os(var).filter(|v| !v.is_empty()) {
            return std::path::PathBuf::from(v);
        }
    }
    std::path::PathBuf::from("/var/lib/hart")
}

/// Where the active theme can be, in the order the shell itself would look: the
/// running theme first, the shipped preset as the seed. Every reader of the theme
/// resolves through this one list, and the hot-reload watch stamps every entry, so a
/// theme applied for the first time (the file appearing) is a change like any other.
pub fn theme_paths() -> Vec<std::path::PathBuf> {
    let mut v = vec![data_dir().join(ACTIVE_THEME_FILE)];
    if let Some(p) = SettingsFile::preset_path() {
        v.push(p);
    }
    v
}

/// Where the shell reads its declarative accessibility state
/// (shell_os_apis.py seeds `_A11Y_SETTINGS` from this exact path at import).
pub const A11Y_SETTINGS_PATH: &str = "/etc/hart/accessibility.json";

/// Does the user want motion stood down? The ONE reader of the key, out of the same
/// declarative file the shell seeds `_A11Y_SETTINGS` from (loaded by the theme watch in
/// comp_core, which re-reads it when it changes). FALSE when the file is absent or the
/// key is missing, which is the shipped default the shell seeds `reduced_motion` to.
pub fn reduced_motion_in(a11y: &SettingsFile) -> bool {
    a11y.flag("reduced_motion").unwrap_or(false)
}

/// The resolution rule itself, with the environment read out of the way. Kept as its own
/// entry point because the tests drive it directly; the id-safety and the fallback both
/// live in `SettingsFile::for_id` now, so this is the same rule, not a second one.
pub fn theme_palette_from(dir: &str, id: &str) -> BloomPalette {
    palette_from(&SettingsFile::for_id(dir, id))
}

// `.hart-vignette` (liquid_ui_service l.2430), which the shell emits UNCONDITIONALLY
// (no potato gate, no GPU gate): `radial-gradient(120% 120% at 50% 38%, transparent 56%,
// rgba(0,0,0,0.30) 100%)`. It is the framing that keeps the desktop from reading flat at
// the corners, the ledger files it under Field/M1, and the native scene had nothing like
// it, so standing the WebView down at M6 would have taken the framing with it.
//
// Folded into the bloom's own buffer rather than pushed as a second element: it is
// deterministic given the output size, so it recomposes exactly when the backdrop does,
// costs no extra per-frame blit, and lands in the right place in the stack for free. In
// the shell it sits at z-index 2 with nothing but the grain between it and the bloom
// canvas at z 1, and every piece of chrome is above it; here the native scene is pushed
// after the backdrop, so the same thing is true.
/// Ellipse radii as a fraction of the box, the CSS `120% 120%`.
const VIGNETTE_R: (f32, f32) = (1.2, 1.2);
/// Centre, the CSS `at 50% 38%`.
const VIGNETTE_C: (f32, f32) = (0.5, 0.38);
/// Where the darkening starts along the gradient ray (`transparent 56%`).
const VIGNETTE_INNER: f32 = 0.56;
/// Peak darkening at the ellipse edge (`rgba(0,0,0,0.30)`).
const VIGNETTE_ALPHA: f32 = 0.30;

/// The vignette's darkening factor at a pixel: 1.0 = untouched, 0.70 at full strength.
///
/// PURE, so the gradient's shape is testable without composing a buffer. `t` is the
/// normalised elliptical distance from the centre; CSS holds the last stop's colour
/// beyond the ending shape, so past `t = 1` the factor stays at its darkest rather than
/// continuing to fall, which matters because the corners of a 16:9 output are outside a
/// 120%/120% ellipse.
fn vignette_factor(x: f32, y: f32, w: f32, h: f32) -> f32 {
    let dx = (x - VIGNETTE_C.0 * w) / (VIGNETTE_R.0 * w).max(1.0);
    let dy = (y - VIGNETTE_C.1 * h) / (VIGNETTE_R.1 * h).max(1.0);
    let t = (dx * dx + dy * dy).sqrt();
    if t <= VIGNETTE_INNER {
        return 1.0;
    }
    let ramp = ((t - VIGNETTE_INNER) / (1.0 - VIGNETTE_INNER)).min(1.0);
    1.0 - VIGNETTE_ALPHA * ramp
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
            // The vignette darkens what the blobs just built. Multiplying is exact
            // here because the backdrop is OPAQUE: black at alpha `a` over an opaque
            // ground is that ground scaled by `1 - a`, with no alpha term left over.
            let vg = vignette_factor(fx, fy, w as f32, h as f32);
            r *= vg;
            g *= vg;
            b *= vg;
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
    fn the_vignette_frames_the_desktop_the_way_the_shell_does() {
        // `.hart-vignette` is emitted unconditionally by the shell, so standing the
        // WebView down at M6 would have taken the framing with it and left the corners
        // reading flat. Check the SHAPE, not just that something changed: untouched
        // inside the transparent stop, darkening beyond it, darkest at the edge.
        let (w, h) = (1920.0, 1080.0);
        let c = (VIGNETTE_C.0 * w, VIGNETTE_C.1 * h);
        assert_eq!(vignette_factor(c.0, c.1, w, h), 1.0, "the centre is untouched");

        // Just inside the transparent stop: still untouched. Just outside: darkening.
        let inner_x = c.0 + VIGNETTE_INNER * VIGNETTE_R.0 * w * 0.99;
        let outer_x = c.0 + VIGNETTE_INNER * VIGNETTE_R.0 * w * 1.01;
        assert_eq!(vignette_factor(inner_x, c.1, w, h), 1.0, "inside the clear stop");
        assert!(
            vignette_factor(outer_x, c.1, w, h) < 1.0,
            "past the clear stop it starts to darken"
        );

        // HOW SUBTLE IT ACTUALLY IS, which is the part worth pinning. The ellipse is
        // 120% of the box in EACH axis, so on a 16:9 output the far corner is only
        // t = 0.66 along the ray: about a 7% darkening, not the 30% the last stop names.
        // Anyone reimplementing this by eye would make it several times too strong.
        let corner = vignette_factor(0.0, h, w, h);
        assert!(
            (corner - 0.929).abs() < 0.01,
            "the 16:9 corner should sit at ~0.93, got {corner}"
        );
        assert!(corner > 1.0 - VIGNETTE_ALPHA, "the box never reaches the last stop");

        // Past the ending shape CSS holds the last stop rather than continuing to fall.
        // No pixel of a real output gets there, but the clamp is what stops a wider
        // aspect from going black in the corners.
        let far = vignette_factor(c.0 + 10.0 * w, c.1, w, h);
        assert!(
            (far - (1.0 - VIGNETTE_ALPHA)).abs() < 1e-6,
            "beyond the ellipse it holds at the last stop: {far}"
        );
        assert!(far > 0.0, "it is a darkening, never a blackout");

        // Monotonic outward along the ray: a vignette that brightened anywhere would be
        // a banding artifact rather than framing.
        let mut prev = 1.0;
        for i in 0..=20 {
            let x = c.0 + (i as f32 / 20.0) * VIGNETTE_R.0 * w;
            let f = vignette_factor(x, c.1, w, h);
            assert!(f <= prev + 1e-6, "brightened at step {i}: {f} after {prev}");
            prev = f;
        }
    }

    #[test]
    fn the_compose_applies_the_vignette_to_every_channel() {
        // The factor is one thing; that the COMPOSE applies it is another, and the two
        // have to be checked separately or a correct gradient can sit unused.
        //
        // ISOLATED from the blob field, with ambient hues that add nothing, so every
        // pixel is exactly `base * vignette_factor`. This measures the vignette rather
        // than the bloom's own centre-bright falloff, which matters: a "corner darker
        // than centre" check against the real palette passes with NO vignette at all,
        // and passes with only two of the three channels darkened. Both were written
        // that way first and both mutations sailed through.
        let (w, h) = (320, 180);
        let flat = BloomPalette {
            base: [200, 150, 100],
            amb: [[0, 0, 0]; 4],
        };
        let px = compose(w, h, &flat);
        for (x, y) in [
            (0usize, 0usize),
            (w as usize - 1, h as usize - 1),
            (w as usize / 2, (h as f32 * VIGNETTE_C.1) as usize),
        ] {
            let i = (y * w as usize + x) * 4;
            let vg = vignette_factor(x as f32, y as f32, w as f32, h as f32);
            // B, G, R in memory order, against the palette's R, G, B.
            for (byte, base) in [
                (px[i], flat.base[2]),
                (px[i + 1], flat.base[1]),
                (px[i + 2], flat.base[0]),
            ] {
                let want = (base as f32 * vg) as u8;
                assert_eq!(
                    byte, want,
                    "at ({x},{y}) the vignette must scale every channel: {byte} vs {want}"
                );
            }
        }
        // Every pixel stays opaque: the vignette darkens the ground, it does not punch a
        // hole in it, and a transparent backdrop would show the clear colour through.
        assert!(
            (0..(w as usize * h as usize)).all(|i| px[i * 4 + 3] == 255),
            "the backdrop must stay opaque"
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

    fn mood(hexes: [Option<&str>; 5]) -> crate::scene::MoodPalette {
        let c = |h: Option<&str>| h.and_then(crate::scene::Color::from_hex);
        crate::scene::MoodPalette {
            accent: None,
            secondary: None,
            background: c(hexes[0]),
            ambient: [c(hexes[1]), c(hexes[2]), c(hexes[3]), c(hexes[4])],
        }
    }

    #[test]
    fn a_mood_recolours_the_field_slot_by_slot_and_keeps_the_theme_where_silent() {
        let base = BloomPalette::default();
        // Sunset, as _home_resolve_mood emits it: a background, a1 and a2, no a3/a4.
        let sunset = base.with_mood(&mood([
            Some("#16090F"), Some("#FF8A4C"), Some("#FF2E9A"), None, None,
        ]));
        assert_eq!(sunset.base, [0x16, 0x09, 0x0F], "the ground takes the mood's b");
        assert_eq!(sunset.amb[0], [0xFF, 0x8A, 0x4C], "amb-1 is the lead hue");
        assert_eq!(sunset.amb[1], [0xFF, 0x2E, 0x9A]);
        assert_eq!(sunset.amb[2], base.amb[2], "a silent a3 keeps the theme's hue");
        assert_eq!(sunset.amb[3], base.amb[3], "and so does a4");
        // An empty mood is the identity, so `"palette": {}` cannot reset anything.
        assert_eq!(base.with_mood(&mood([None; 5])), base);
        // Round trip through the f32 colour and back lands on the same byte.
        let same = base.with_mood(&mood([Some("#04050B"), Some("#B182FF"), None, None, None]));
        assert_eq!(same, base, "aura's own hues fold back to aura's own bytes");
    }

    #[test]
    fn two_moods_paint_different_pixels_and_an_aura_mood_moves_only_the_ambient() {
        // THE PIXEL PROOF for the backdrop half of "mood reaches the pixels". Compose
        // the same output under two moods and read the bytes: the violet lead blob is
        // violet under aurora and orange under sunset, and the far corner sits on each
        // mood's own ground. A compose that ignored the mood would return equal buffers.
        let base = BloomPalette::default();
        let aurora = base.with_mood(&mood([
            Some("#04050B"), Some("#B182FF"), Some("#00DDF9"), Some("#FB66B6"), Some("#FFB330"),
        ]));
        let sunset = base.with_mood(&mood([
            Some("#16090F"), Some("#FF8A4C"), Some("#FF2E9A"), None, None,
        ]));
        let (w, h) = (160, 90);
        let a = compose(w, h, &aurora);
        let s = compose(w, h, &sunset);
        assert_ne!(a, s, "two moods must not compose the same field");
        // Bytes are B, G, R, A. The lead blob sits at (0.32, 0.40).
        let at = |buf: &[u8], x: usize, y: usize| -> (u8, u8, u8) {
            let i = (y * w as usize + x) * 4;
            (buf[i + 2], buf[i + 1], buf[i])
        };
        let lead = ((0.32 * w as f32) as usize, (0.40 * h as f32) as usize);
        let (ar, ag, ab) = at(&a, lead.0, lead.1);
        assert!(ab > ag && ar > ag, "under aurora the lead blob reads VIOLET: {:?}", (ar, ag, ab));
        let (sr, sg, sb) = at(&s, lead.0, lead.1);
        assert!(sr > sg && sg > sb, "under sunset it reads ORANGE: {:?}", (sr, sg, sb));
        // And the aurora field is byte-identical to the shipped default: an Aura mood
        // whose quad IS aura's changes nothing, which is the "only the ambient" half.
        assert_eq!(a, compose(w, h, &base));
    }
}
