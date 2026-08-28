//! M2 of the NATIVE SHELL PARITY PROGRAM -- the voice orb, drawn by hart-comp.
//!
//! WHY THIS EXISTS. The orb is the most-animated thing on the desktop and the
//! most expensive: it breathes continuously, so in the HTML shell it forces the
//! browser to rasterise forever. Measured on the fleet box 2026-08-28 with
//! `strace -c` on WebKitWebProcess while it burned a full core: 0.64s of syscall
//! time out of ~6s and ZERO ioctls, i.e. ~5.4s of userspace pixel work that
//! never reached the GPU at all.
//!
//! That is not fixable by configuration here. Every GPU route for the
//! GTK4/WebKit stack on this hardware is closed with real-HW evidence: GSK GL
//! hangs the layer-shell surface (hence `GDK_GL=disable` always), GSK vulkan
//! dies on Ivy Bridge's incomplete driver (tried 2029a7b, reverted 888ebc3), and
//! dmabuf costs more than it saves without a GPU-side compositor (measured
//! 1.77 -> 1.04 cores when turned OFF). See hart-layer-shell-host.nix. But
//! hart-comp DOES hold a live GLES context on this GPU
//! (`render = HARDWARE armed (GLES on the verified iGPU)`), so the orb moves
//! here.
//!
//! THE DESIGN, and why it is this one. Every mature compositor animates the same
//! way: rasterise the thing ONCE into a texture, then vary cheap per-frame
//! parameters on the GPU. Core Animation interpolates transform/opacity on a
//! layer in the WindowServer; DWM does the same through DirectComposition. The
//! application does no per-frame pixel work at all. So:
//!
//!   * ONE buffer, composed once per (size, palette). Not one per animation
//!     phase -- a phase ring would quantise a smooth breath into steps, cost
//!     memory linear in the step count, and still be an approximation of what
//!     the GPU does exactly. Rejected on both counts.
//!   * Motion is TWO SCALARS per frame, `scale` and `alpha`, handed to
//!     smithay's element as its `size` and `alpha` arguments. The GPU applies
//!     them. Per-frame CPU cost is arithmetic on two floats: sub-microsecond,
//!     zero allocation, O(1) memory, and smooth at whatever rate the display
//!     runs rather than at a quantised step rate.
//!
//! MOTION RULE (the program's SEMANTICS TRAP, non-negotiable): `motion_at` is a
//! PURE FUNCTION of (elapsed, energy). It stores no target and eases toward
//! nothing. That is what makes the CSS-transition drag bug structurally
//! impossible here -- there is no interpolator between a signal and the pixel.
//! User-driven motion (drag) must likewise write the node transform directly
//! from the pointer event and must NOT be routed through this.
//!
//! REACTIVE BY CONSTRUCTION. `energy` is the live signal (mic RMS, 0..=1) that
//! P2 calls for: "breathing, energy-reactive". It is an argument rather than
//! internal state, so the caller can feed it from anywhere -- voice today, an
//! agent's activity level later -- without this module changing.
//!
//! PARITY SOURCE: `static/voiceOrbViz.js`'s STYLES table, whose comment marks
//! 'vibrant' as "the brand default (b1.2). EXACT legacy numbers => no visual
//! regression out of the box". Those numbers are reproduced verbatim below.

use std::time::Duration;

/// The orb's colour ramp, centre outward. Mirrors one entry of voiceOrbViz.js's
/// STYLES table; the defaults ARE 'vibrant', the shipped brand orb.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct OrbPalette {
    /// Bright almost-white centre.
    pub core: [u8; 3],
    /// The soft bright shell just outside the core.
    pub halo: [u8; 3],
    /// Mid-body brand colour.
    pub glow: [u8; 3],
    /// Darker rim, gives the sphere its curvature.
    pub dark: [u8; 3],
    /// The dashed orbital ring.
    pub ring: [u8; 3],
}

impl Default for OrbPalette {
    fn default() -> Self {
        // voiceOrbViz.js STYLES['vibrant'] — the EXACT legacy numbers.
        OrbPalette {
            core: [212, 254, 245],
            halo: [185, 253, 238],
            glow: [0, 230, 195],
            dark: [0, 170, 150],
            ring: [140, 252, 228],
        }
    }
}

/// What the renderer varies per frame. Two scalars, nothing else.
#[derive(Clone, Copy, PartialEq, Debug)]
pub struct OrbMotion {
    /// Multiplier on the orb's on-screen size. 1.0 is rest.
    pub scale: f32,
    /// Opacity multiplier, 0..=1.
    pub alpha: f32,
}

/// One full breath, matching the shell's `@keyframes hob-breathe{...3.2s...}` so
/// the native orb breathes at the same rate as the HTML one it replaces.
pub const BREATH_PERIOD: Duration = Duration::from_millis(3200);

/// Breath extremes at rest, taken from the shell's own keyframes:
///     @keyframes hob-breathe{0%,100%{transform:scale(1);opacity:.85}
///                            50%{transform:scale(1.08);opacity:1}}
const SCALE_REST: f32 = 1.00;
const SCALE_PEAK: f32 = 1.08;
const ALPHA_REST: f32 = 0.85;
const ALPHA_PEAK: f32 = 1.00;

/// How much a full-energy signal widens the breath beyond its resting peak.
/// Deliberately modest: the orb should read as *alive*, not as a VU meter, and
/// an orb that lunges at every syllable is the kind of motion the checklist's
/// "calm teal centre" language rules out.
const ENERGY_SCALE_GAIN: f32 = 0.10;

/// Where the sphere ends, as a fraction of the buffer's half-width. The rest is
/// headroom for the halo falloff and the orbital ring.
const BODY_R: f32 = 0.62;
/// The dashed orbital ring's radius, same units.
const RING_R: f32 = 0.86;
/// Ring line thickness, same units.
const RING_W: f32 = 0.035;
/// Dashes around the ring. `rings: true` in the parity source.
const RING_DASHES: f32 = 18.0;
/// Fraction of each dash period that is drawn.
const RING_DUTY: f32 = 0.55;

/// `smoothstep` 0..1 ramp. Two multiplies, no branches.
#[inline]
fn ramp(t: f32) -> f32 {
    let t = t.clamp(0.0, 1.0);
    t * t * (3.0 - 2.0 * t)
}

/// Linear blend between two colours.
#[inline]
fn mix(a: [u8; 3], b: [u8; 3], t: f32) -> [f32; 3] {
    let t = t.clamp(0.0, 1.0);
    [
        a[0] as f32 + (b[0] as f32 - a[0] as f32) * t,
        a[1] as f32 + (b[1] as f32 - a[1] as f32) * t,
        a[2] as f32 + (b[2] as f32 - a[2] as f32) * t,
    ]
}

/// The orb's motion RIGHT NOW: a pure function of the clock and the live signal.
///
/// No state, no allocation, no easing toward a target. Called once per frame per
/// output; the whole body is a handful of float ops, which is what makes the
/// animation free at the CPU and exact at the GPU.
///
/// `energy` is clamped, so a mis-scaled input signal can widen the breath a
/// little but can never make the orb inflate without bound or invert.
pub fn motion_at(elapsed: Duration, energy: f32) -> OrbMotion {
    let e = energy.clamp(0.0, 1.0);
    let period = BREATH_PERIOD.as_secs_f32().max(f32::EPSILON);
    let phase = (elapsed.as_secs_f32() % period) / period;
    // 0 -> 1 -> 0 across the cycle, smooth at both ends and seamless at the wrap.
    let breath = ramp(1.0 - (2.0 * phase - 1.0).abs());
    OrbMotion {
        scale: SCALE_REST + (SCALE_PEAK - SCALE_REST + ENERGY_SCALE_GAIN * e) * breath,
        // Energy lifts the floor rather than the peak: a speaking orb is
        // continuously brighter, not merely flashier at the top of the breath.
        alpha: (ALPHA_REST + (ALPHA_PEAK - ALPHA_REST) * breath + 0.10 * e).clamp(0.0, 1.0),
    }
}

/// Compose the orb ONCE into a premultiplied ARGB8888 buffer.
///
/// `side` is both width and height: the orb is square and centred, so a per-frame
/// scale never moves its centre and the caller's positioning stays trivial.
///
/// PREMULTIPLIED, unlike `bloom::compose`. The bloom is an opaque backdrop so its
/// premultiplication is a no-op at alpha 255; the orb is a translucent sphere
/// over whatever is behind it, so every channel must be scaled by alpha or the
/// halo composites as a bright fringe instead of a soft falloff.
///
/// Returns rows of `Argb8888`, which is B,G,R,A in memory on little-endian --
/// the layout `MemoryRenderBuffer::from_slice` expects.
pub fn compose(side: i32, pal: &OrbPalette) -> Vec<u8> {
    let n = side.max(1) as usize;
    let mut buf = vec![0u8; n * n * 4];

    let half = n as f32 / 2.0;
    let body = half * BODY_R;
    let ring = half * RING_R;
    let ring_w = half * RING_W;

    for y in 0..n {
        let dy = y as f32 + 0.5 - half;
        let row = y * n * 4;
        for x in 0..n {
            let dx = x as f32 + 0.5 - half;
            let d = (dx * dx + dy * dy).sqrt();

            // ── sphere body: core -> halo -> glow -> dark rim ──
            let (mut rgb, mut a) = if d <= body {
                let t = d / body.max(1.0);
                let c = if t < 0.30 {
                    mix(pal.core, pal.halo, t / 0.30)
                } else if t < 0.62 {
                    mix(pal.halo, pal.glow, (t - 0.30) / 0.32)
                } else {
                    mix(pal.glow, pal.dark, (t - 0.62) / 0.38)
                };
                // Soften the very edge so the sphere has no jaggies.
                let edge = ramp((body - d) / (body * 0.06).max(1.0));
                (c, edge)
            } else {
                // ── halo: the glow bleeding outward, fading to nothing ──
                let reach = body * 0.55;
                let t = ((d - body) / reach.max(1.0)).clamp(0.0, 1.0);
                (mix(pal.glow, pal.dark, t), (1.0 - t) * 0.33)
            };

            // ── dashed orbital ring ──
            let dr = (d - ring).abs();
            if dr < ring_w {
                // atan2 is the only transcendental here and it runs ONLY inside
                // the ring band, a thin annulus, not across the whole buffer.
                let ang = dy.atan2(dx);
                let phase = (ang / std::f32::consts::TAU + 0.5) * RING_DASHES;
                if phase.fract() < RING_DUTY {
                    let across = ramp((ring_w - dr) / ring_w);
                    let ra = across * 0.85;
                    if ra > a {
                        rgb = [pal.ring[0] as f32, pal.ring[1] as f32, pal.ring[2] as f32];
                        a = ra;
                    }
                }
            }

            let a = a.clamp(0.0, 1.0);
            let i = row + x * 4;
            // Argb8888 little-endian => B, G, R, A. Premultiply every channel.
            buf[i] = (rgb[2] * a).min(255.0) as u8;
            buf[i + 1] = (rgb[1] * a).min(255.0) as u8;
            buf[i + 2] = (rgb[0] * a).min(255.0) as u8;
            buf[i + 3] = (a * 255.0).min(255.0) as u8;
        }
    }
    buf
}

#[cfg(test)]
mod tests {
    use super::*;

    fn px(buf: &[u8], side: usize, x: usize, y: usize) -> [u8; 4] {
        let i = (y * side + x) * 4;
        [buf[i], buf[i + 1], buf[i + 2], buf[i + 3]]
    }

    #[test]
    fn compose_fills_the_whole_buffer() {
        let p = OrbPalette::default();
        let side = 64;
        assert_eq!(compose(side, &p).len(), (side * side * 4) as usize);
    }

    #[test]
    fn the_centre_is_bright_and_the_corner_is_transparent() {
        let p = OrbPalette::default();
        let side = 96usize;
        let buf = compose(side as i32, &p);
        let centre = px(&buf, side, side / 2, side / 2);
        let corner = px(&buf, side, 1, 1);
        assert!(centre[3] > 200, "orb centre is not opaque: {:?}", centre);
        assert_eq!(corner[3], 0, "buffer corner must be fully transparent: {:?}", corner);
    }

    #[test]
    fn every_pixel_is_premultiplied() {
        // The invariant that makes the halo composite as a soft falloff rather
        // than a bright fringe: no colour channel may exceed its own alpha.
        let p = OrbPalette::default();
        let side = 72usize;
        let buf = compose(side as i32, &p);
        for (i, c) in buf.chunks(4).enumerate() {
            assert!(c[0] <= c[3] && c[1] <= c[3] && c[2] <= c[3],
                    "pixel {} not premultiplied: {:?}", i, c);
        }
    }

    #[test]
    fn the_ring_is_dashed_not_solid() {
        // `rings: true` in the parity source means DASHED orbital rings.
        let p = OrbPalette::default();
        let side = 160usize;
        let buf = compose(side as i32, &p);
        let half = side as f32 / 2.0;
        let r = half * RING_R;
        let (mut on, mut off) = (false, false);
        for i in 0..360 {
            let th = (i as f32).to_radians();
            let x = (half + r * th.cos()) as usize;
            let y = (half + r * th.sin()) as usize;
            if x >= side || y >= side { continue; }
            if px(&buf, side, x, y)[3] > 120 { on = true } else { off = true }
        }
        assert!(on && off, "ring must alternate drawn/undrawn (on={} off={})", on, off);
    }

    #[test]
    fn compose_is_deterministic() {
        // The cache keys on (side, palette); identical inputs MUST give identical
        // bytes or a cached orb would differ from a recomposed one and flicker.
        let p = OrbPalette::default();
        assert_eq!(compose(48, &p), compose(48, &p));
    }

    // ── motion: the per-frame half ──────────────────────────────────────────

    #[test]
    fn breathing_is_a_seamless_cycle() {
        let a = motion_at(Duration::ZERO, 0.0);
        let mid = motion_at(BREATH_PERIOD / 2, 0.0);
        let wrap = motion_at(BREATH_PERIOD, 0.0);
        assert!(mid.scale > a.scale && mid.alpha > a.alpha,
                "the middle of the cycle must be the peak");
        // Seamless at the wrap: no visible jump when the breath repeats.
        assert!((wrap.scale - a.scale).abs() < 1e-5);
        assert!((wrap.alpha - a.alpha).abs() < 1e-5);
    }

    #[test]
    fn resting_motion_stays_inside_the_shell_keyframes() {
        // Parity with @keyframes hob-breathe at zero energy.
        for i in 0..64 {
            let t = BREATH_PERIOD.mul_f32(i as f32 / 64.0);
            let m = motion_at(t, 0.0);
            assert!(m.scale >= SCALE_REST - 1e-5 && m.scale <= SCALE_PEAK + 1e-5,
                    "scale {} outside the shell's keyframes", m.scale);
            assert!(m.alpha >= ALPHA_REST - 1e-5 && m.alpha <= ALPHA_PEAK + 1e-5,
                    "alpha {} outside the shell's keyframes", m.alpha);
        }
    }

    #[test]
    fn energy_makes_it_more_alive_not_unbounded() {
        let calm = motion_at(BREATH_PERIOD / 2, 0.0);
        let loud = motion_at(BREATH_PERIOD / 2, 1.0);
        assert!(loud.scale > calm.scale, "energy must widen the breath");
        assert!(loud.alpha >= calm.alpha, "energy must not dim the orb");
        // Bounded: a mis-scaled signal cannot inflate the orb without limit.
        let absurd = motion_at(BREATH_PERIOD / 2, 999.0);
        assert_eq!(absurd, loud, "energy must be clamped");
        assert!(absurd.alpha <= 1.0, "alpha must never exceed 1");
    }

    #[test]
    fn motion_never_inverts_or_vanishes() {
        // A negative or NaN-adjacent signal must not flip the orb inside out or
        // blink it away; this runs on the OS shell, not in a demo.
        for e in [-5.0f32, 0.0, 0.5, 1.0] {
            for i in 0..32 {
                let m = motion_at(BREATH_PERIOD.mul_f32(i as f32 / 32.0), e);
                assert!(m.scale > 0.0, "scale went non-positive: {}", m.scale);
                assert!((0.0..=1.0).contains(&m.alpha), "alpha out of range: {}", m.alpha);
            }
        }
    }
}
