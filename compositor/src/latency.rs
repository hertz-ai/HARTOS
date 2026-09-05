//! Input-to-photon latency instrument — M0 of LATENCY_HARNESS.md.
//!
//! "An unmeasured latency claim is an opinion." (docs/architecture/LATENCY_HARNESS.md)
//! This module is the instrument that document designs: T_input is the KERNEL
//! timestamp libinput stamps on every event (CLOCK_MONOTONIC microseconds,
//! `Event::time()`), T_photon is the DRM page-flip completion reaped in
//! `udev.rs::reap_completed_vblanks` — the only two points that bound the TRUE
//! path, compositor queue and scanout included. App- and browser-level numbers
//! are proxies by construction; ours is not, because hart-comp owns both ends.
//!
//! ATTRIBUTION IS LIVE. Samples bucket by (surface, kind), where the surface is
//! resolved from the retained scene tree at the moment the input arrives
//! (`scene::SceneNode::component_at`, deepest-wins, the same rule `hit_test` and
//! `hover_leaf` follow). A window therefore closes into one summary per surface
//! per kind, so a slow card cannot hide behind a fast orb.
//!
//! `Surface::Shell` is not a failure case: it is bare desktop, WebView chrome,
//! and every sample taken while the native scene is not on screen. The harness
//! wants the WEB shell measured by this same instrument ("'native is faster' is a
//! demonstrated delta, not a claim"), and the `shell` journal line is byte
//! identical to the one this instrument emitted before attribution existed.
//!
//! WHAT THIS M0 SLICE STILL IS, HONESTLY:
//!   * budgets are looked up per KIND, not per (surface, kind). That is not a
//!     shortcut today: every value in latency_budgets.json's `components` table
//!     equals the `_defaults` entry for its kind, so the table declares WHICH
//!     interactions a surface is expected to support rather than different
//!     numbers. A Python guard asserts exactly that and fails the moment someone
//!     lands a real override, because the override would otherwise do nothing.
//!   * the surface for a RELATIVE motion event is the one the pointer is
//!     LEAVING. T_input is captured before the event is applied (moving that
//!     capture would bias the clock estimator toward busy periods), so a boundary
//!     crossing attributes one sample to the wrong side. A drag stays inside its
//!     surface for hundreds of samples, which is where the headline numbers come
//!     from.
//!   * one frame stream, not per-CRTC: the appliance is single-display; on a
//!     multi-head box samples from two CRTCs would interleave into one stream.
//!   * the winit dev backend is not wired — numbers from a nested session would
//!     be lies about the hardware path (they'd include the HOST compositor).
//!
//! THE CLOCK PROBLEM, AND WHY THERE IS NO NEW DEPENDENCY:
//! libinput times are CLOCK_MONOTONIC µs. std::time::Instant is the same clock
//! on Linux but deliberately opaque — there is no stable way to read its raw
//! value, and the crate graph offers no direct monotonic reader: rustix is in
//! the lock twice (0.38 + 1.1) only as smithay's transitive dep, and promoting
//! it with a `time` feature would change feature resolution and desync
//! Cargo.toml from the offline-vendored Cargo.lock that CI builds from (this
//! box cannot regenerate the lock). So the offset between "µs since an Instant
//! base" and "kernel event µs" is ESTIMATED instead: every input contributes
//! one observation `delta = instant_us - event_us`, and the rolling MINIMUM of
//! recent deltas is the offset. Event delivery delay is strictly one-sided
//! (an event can only be observed AFTER the kernel stamped it), so the minimum
//! over many events converges from above onto the true offset plus the
//! best-case delivery latency — tens of microseconds on an idle dispatch loop,
//! against budgets of 16,000. The estimator is pure and its convergence is
//! unit-tested; `photon_time()` refuses to answer before the first observation
//! (anti-gaming rule: a sample not anchored to a kernel input timestamp and a
//! flip completion is invalid and MUST NOT be reported — so we report nothing
//! rather than something almost right).
//!
//! Budgets are the `_defaults` of docs/architecture/latency_budgets.json,
//! mirrored as consts. That file stays the source of truth reviewers edit; a
//! budget may only be RAISED with a justification recorded in the same commit,
//! and the mirror here must move with it. Per-component budget lookup joins the
//! attribution work.
//!
//! Output contract (harness §3): one aggregated journal line per kind per 10s
//! window — `hart-latency component=shell kind=drag n=142 p50=8.1ms p99=14.7ms
//! max=19.2ms budget=16ms verdict=PASS` — plus raw samples to
//! /run/hart/latency.jsonl only when HART_LATENCY_JSONL=1 (harness runs; the
//! always-on path costs a mutex and some arithmetic per event, no io).

#![allow(dead_code)] // the default (no-smithay) build compiles the pure core for tests

use std::collections::VecDeque;

/// Interaction kinds the harness names. Motion resolves to Drag or Hover from
/// button state at note time — a drag IS a motion with a button held; giving it
/// its own bucket is what lets the 2026-07-20 rubber-band class show up as a
/// drag p99 violation instead of vanishing into a hover average.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Press,
    Drag,
    Hover,
    Scroll,
    Key,
}

impl Kind {
    pub fn label(self) -> &'static str {
        match self {
            Kind::Press => "press",
            Kind::Drag => "drag",
            Kind::Hover => "hover",
            Kind::Scroll => "scroll",
            Kind::Key => "key",
        }
    }
    /// latency_budgets.json `_defaults`, mirrored (see module doc).
    pub fn budget_ms(self) -> u64 {
        match self {
            Kind::Drag | Kind::Hover | Kind::Scroll => 16,
            Kind::Press | Kind::Key => 25,
        }
    }
    const ALL: [Kind; 5] = [Kind::Press, Kind::Drag, Kind::Hover, Kind::Scroll, Kind::Key];
    fn idx(self) -> usize {
        match self {
            Kind::Press => 0,
            Kind::Drag => 1,
            Kind::Hover => 2,
            Kind::Scroll => 3,
            Kind::Key => 4,
        }
    }
}

/// Which surface a sample is attributed to, as the aggregator buckets it.
///
/// `Shell` is not a failure case. The harness wants the WEB shell measured by this same
/// instrument, so "native is faster" is a demonstrated delta rather than a claim, and a
/// sample over WebView chrome or bare desktop belongs to it. It is also what every sample
/// was before the scene could name anything, so the journal line for it is byte-identical
/// to the one this instrument has always emitted.
///
/// The five named ones mirror `scene::Component`. They are not the same type because
/// this module is deliberately free of every other module (no Smithay, no scene, no
/// clock), which is what lets its state machine run under `cargo test` on any dev box
/// including the default no-feature build. The mapping is one `From` at the wiring.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Surface {
    Shell,
    Orb,
    TopBar,
    Omnibox,
    Taskbar,
    HomeCard,
}

impl Surface {
    /// The budget file's key, and the journal line's `component=`.
    pub fn label(self) -> &'static str {
        match self {
            Surface::Shell => "shell",
            Surface::Orb => "orb",
            Surface::TopBar => "top-bar",
            Surface::Omnibox => "omnibox",
            Surface::Taskbar => "taskbar",
            Surface::HomeCard => "home-card",
        }
    }
    const ALL: [Surface; 6] = [
        Surface::Shell,
        Surface::Orb,
        Surface::TopBar,
        Surface::Omnibox,
        Surface::Taskbar,
        Surface::HomeCard,
    ];
    fn idx(self) -> usize {
        match self {
            Surface::Shell => 0,
            Surface::Orb => 1,
            Surface::TopBar => 2,
            Surface::Omnibox => 3,
            Surface::Taskbar => 4,
            Surface::HomeCard => 5,
        }
    }
}

/// One aggregated window per (surface, kind), ready to be logged. Pure data so the io
/// stays at the caller and the aggregation is testable.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Summary {
    pub surface: Surface,
    pub kind: Kind,
    pub n: usize,
    pub p50_us: u64,
    pub p99_us: u64,
    pub max_us: u64,
    pub budget_ms: u64,
    pub pass: bool,
}

impl Summary {
    /// The harness §3 journal line, byte-stable so tests can pin it.
    pub fn journal_line(&self) -> String {
        format!(
            "hart-latency component={} kind={} n={} p50={:.1}ms p99={:.1}ms max={:.1}ms budget={}ms verdict={}",
            self.surface.label(),
            self.kind.label(),
            self.n,
            self.p50_us as f64 / 1000.0,
            self.p99_us as f64 / 1000.0,
            self.max_us as f64 / 1000.0,
            self.budget_ms,
            if self.pass { "PASS" } else { "FAIL" },
        )
    }
}

/// Inputs bound to one queued frame await its vblank. More than a few in
/// flight means vblanks stopped being reaped (the #50 freeze class) — binding
/// newer frames would then attribute stale input to the wrong photon, so the
/// oldest batch is dropped instead and counted.
const MAX_INFLIGHT_FRAMES: usize = 8;
/// Un-bound inputs cap: a burst beyond this (a 1000Hz mouse between two 60Hz
/// frames delivers ~17) means frames are not being queued at all; older inputs
/// would produce garbage latencies against a much-later frame. Drop-oldest.
const MAX_PENDING_INPUTS: usize = 256;
/// Raw samples kept per kind per window. 4096 per 10s = 400+/s headroom.
const MAX_WINDOW_SAMPLES: usize = 4096;
/// Aggregation window (harness §3: one line per component per 10s).
const WINDOW_US: u64 = 10_000_000;
/// A sample farther than this from its photon is a clock or wedge artifact,
/// not an interaction; refuse it (anti-gaming: report nothing over almost).
const MAX_SANE_LATENCY_US: u64 = 5_000_000;
/// Offset observations kept for the rolling-min estimator.
const OFFSET_WINDOW: usize = 64;

/// The pure instrument core. NO clock reads, NO io, NO Smithay types — every
/// timestamp comes in as an argument, so the whole state machine runs under
/// `cargo test` on any dev box (the same idiom as `should_recover_frozen`,
/// `flip_action`, `next_claim`).
pub struct LatencyCore {
    /// Rolling one-sided offset observations (instant_us - event_us).
    offset_obs: VecDeque<u64>,
    /// Inputs seen since the last queued frame, each with the surface it touched.
    pending: Vec<(Surface, Kind, u64)>,
    pending_dropped: u64,
    /// Batches riding queued-but-not-yet-presented frames (FIFO by seq).
    inflight: VecDeque<Vec<(Surface, Kind, u64)>>,
    inflight_dropped: u64,
    button_down: bool,
    window_start_us: Option<u64>,
    /// [surface][kind]. Thirty fixed buckets, allocated once and reused: an input rate
    /// this cannot cover does not exist, and a map would put an allocation on the input
    /// path for no benefit.
    window: [[Vec<u64>; 5]; 6],
}

impl LatencyCore {
    pub fn new() -> Self {
        LatencyCore {
            offset_obs: VecDeque::new(),
            pending: Vec::new(),
            pending_dropped: 0,
            inflight: VecDeque::new(),
            inflight_dropped: 0,
            button_down: false,
            window_start_us: None,
            window: Default::default(),
        }
    }

    /// The estimated (instant-domain minus event-domain) clock offset, or None
    /// before any input has been observed. min() over the window: delivery
    /// delay only ever ADDS, so the smallest observation is the closest to
    /// truth (see module doc for the error bound).
    pub fn offset_us(&self) -> Option<u64> {
        self.offset_obs.iter().copied().min()
    }

    pub fn note_button(&mut self, surface: Surface, down: bool, event_us: u64, instant_us: u64) {
        self.button_down = down;
        self.note_input(surface, Kind::Press, event_us, instant_us);
    }

    pub fn note_motion(&mut self, surface: Surface, event_us: u64, instant_us: u64) {
        let kind = if self.button_down { Kind::Drag } else { Kind::Hover };
        self.note_input(surface, kind, event_us, instant_us);
    }

    pub fn note_input(&mut self, surface: Surface, kind: Kind, event_us: u64, instant_us: u64) {
        // Feed the offset estimator first — even inputs later dropped for
        // capacity still carry a valid clock observation.
        if instant_us >= event_us {
            if self.offset_obs.len() == OFFSET_WINDOW {
                self.offset_obs.pop_front();
            }
            self.offset_obs.push_back(instant_us - event_us);
        }
        if self.pending.len() == MAX_PENDING_INPUTS {
            self.pending.remove(0);
            self.pending_dropped += 1;
        }
        self.pending.push((surface, kind, event_us));
    }

    /// A frame carrying current damage was handed to DRM (`queue_frame` Ok).
    /// Binds every pending input to it.
    pub fn frame_queued(&mut self) {
        if self.pending.is_empty() {
            return;
        }
        if self.inflight.len() == MAX_INFLIGHT_FRAMES {
            self.inflight.pop_front();
            self.inflight_dropped += 1;
        }
        self.inflight.push_back(std::mem::take(&mut self.pending));
    }

    /// A vblank completed (`reap_completed_vblanks`): the OLDEST queued batch
    /// just reached the screen. `instant_us` is the caller's monotonic reading
    /// at the reap; it is converted into the event clock via the estimator.
    /// Returns finished window summaries (empty most calls) — io is the
    /// caller's job.
    pub fn frame_presented(&mut self, instant_us: u64) -> Vec<Summary> {
        let batch = self.inflight.pop_front().unwrap_or_default();
        if let Some(off) = self.offset_us() {
            // Refuse to fabricate: no offset means no anchored photon time.
            let photon_event_us = instant_us.saturating_sub(off);
            for (surface, kind, t_in) in batch {
                let lat = photon_event_us.saturating_sub(t_in);
                if lat == 0 || lat > MAX_SANE_LATENCY_US {
                    continue; // unanchored or wedge artifact, not a report
                }
                let w = &mut self.window[surface.idx()][kind.idx()];
                if w.len() < MAX_WINDOW_SAMPLES {
                    w.push(lat);
                }
            }
            if self.window_start_us.is_none() {
                self.window_start_us = Some(photon_event_us);
            }
            if let Some(start) = self.window_start_us {
                if photon_event_us.saturating_sub(start) >= WINDOW_US {
                    return self.close_window(photon_event_us);
                }
            }
        }
        Vec::new()
    }

    fn close_window(&mut self, now_us: u64) -> Vec<Summary> {
        let mut out = Vec::new();
        // Surface-major, so a window's lines read as one block per component rather than
        // interleaved by kind: that is how a reader sees "the orb is fine, the cards are
        // not" at a glance instead of reconstructing it from ten lines.
        for surface in Surface::ALL {
        for kind in Kind::ALL {
            let w = &mut self.window[surface.idx()][kind.idx()];
            if w.is_empty() {
                continue;
            }
            w.sort_unstable();
            let n = w.len();
            let p50 = w[(n - 1) / 2];
            let p99 = w[((n - 1) * 99) / 100];
            let max = *w.last().unwrap();
            let budget = kind.budget_ms();
            out.push(Summary {
                surface,
                kind,
                n,
                p50_us: p50,
                p99_us: p99,
                max_us: max,
                budget_ms: budget,
                // The budget verdict is the USER's experience of "it
                // stuttered": p99, not the mean (anti-gaming rule).
                pass: p99 <= budget * 1000,
            });
            w.clear();
        }
        }
        self.window_start_us = Some(now_us);
        out
    }

    /// Diagnostics for the drop counters (the "no silent caps" discipline).
    pub fn dropped(&self) -> (u64, u64) {
        (self.pending_dropped, self.inflight_dropped)
    }
}

// ─── The process-global instrument the wiring talks to ──────────────────────
//
// A Mutex, not clever lock-free machinery: inputs arrive at human rates and
// frames at 60Hz; the lock is uncontended in practice and correctness is
// auditable at a glance. The base Instant anchors the "instant domain" the
// offset estimator maps onto the kernel event clock.

use std::sync::Mutex;
use std::sync::OnceLock;
use std::time::Instant;

struct Global {
    core: Mutex<LatencyCore>,
    base: Instant,
}

fn global() -> &'static Global {
    static G: OnceLock<Global> = OnceLock::new();
    G.get_or_init(|| Global {
        core: Mutex::new(LatencyCore::new()),
        base: Instant::now(),
    })
}

fn instant_us() -> u64 {
    global().base.elapsed().as_micros() as u64
}

pub fn on_motion(surface: Surface, event_us: u64) {
    let g = global();
    if let Ok(mut c) = g.core.lock() {
        c.note_motion(surface, event_us, instant_us());
    }
}

pub fn on_button(surface: Surface, down: bool, event_us: u64) {
    let g = global();
    if let Ok(mut c) = g.core.lock() {
        c.note_button(surface, down, event_us, instant_us());
    }
}

pub fn on_input(surface: Surface, kind: Kind, event_us: u64) {
    let g = global();
    if let Ok(mut c) = g.core.lock() {
        c.note_input(surface, kind, event_us, instant_us());
    }
}

pub fn on_frame_queued() {
    let g = global();
    if let Ok(mut c) = g.core.lock() {
        c.frame_queued();
    }
}

/// Called from the vblank reaper. Emits the journal lines and (opt-in) the
/// jsonl sink here so udev.rs stays one line.
pub fn on_frame_presented() -> Vec<Summary> {
    let g = global();
    let summaries = match g.core.lock() {
        Ok(mut c) => c.frame_presented(instant_us()),
        Err(_) => Vec::new(),
    };
    if !summaries.is_empty() {
        let jsonl = std::env::var("HART_LATENCY_JSONL").ok().as_deref() == Some("1");
        for s in &summaries {
            // The journal is the always-on sink (harness §3). tracing's `info!`
            // is not imported here to keep this module dependency-free for the
            // default build; the caller logs, we format.
            if jsonl {
                use std::io::Write;
                if let Ok(mut f) = std::fs::OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open("/run/hart/latency.jsonl")
                {
                    let _ = writeln!(
                        f,
                        "{{\"component\":\"{}\",\"kind\":\"{}\",\"n\":{},\"p50_us\":{},\"p99_us\":{},\"max_us\":{},\"budget_ms\":{},\"pass\":{}}}",
                        s.surface.label(), s.kind.label(), s.n, s.p50_us, s.p99_us, s.max_us,
                        s.budget_ms, s.pass
                    );
                }
            }
        }
    }
    summaries
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── the offset estimator ────────────────────────────────────────────────

    #[test]
    fn the_offset_estimator_converges_from_above() {
        // True offset 1_000_000; delivery delays are one-sided noise on top.
        let mut c = LatencyCore::new();
        for (i, delay) in [900u64, 40, 300, 15, 700, 90].iter().enumerate() {
            let ev = (i as u64) * 16_000;
            c.note_input(Surface::Shell, Kind::Hover, ev, ev + 1_000_000 + delay);
        }
        // min picks the fastest delivery: error == 15µs against 16ms budgets.
        assert_eq!(c.offset_us(), Some(1_000_015));
    }

    #[test]
    fn no_input_means_no_offset_means_no_samples() {
        // Anti-gaming: a photon that cannot be anchored to a kernel input
        // timestamp must produce NOTHING, not something almost right.
        let mut c = LatencyCore::new();
        c.frame_queued();
        assert!(c.frame_presented(5_000_000).is_empty());
        // and nothing was smuggled into the window either
        assert!(c.window.iter().all(|per_kind| per_kind.iter().all(|w| w.is_empty())));
    }

    // ── the sample pipeline ─────────────────────────────────────────────────

    /// Drives one input through queue → present and returns the recorded
    /// latency, using a zero-delay clock pairing so numbers are exact.
    fn one_sample(kind: Kind, t_in: u64, t_photon: u64) -> Option<u64> {
        let mut c = LatencyCore::new();
        c.note_input(Surface::Shell, kind, t_in, t_in); // offset = 0 exactly
        c.frame_queued();
        c.frame_presented(t_photon);
        c.window[Surface::Shell.idx()][kind.idx()].first().copied()
    }

    #[test]
    fn a_sample_is_photon_minus_kernel_input_time() {
        assert_eq!(one_sample(Kind::Press, 100_000, 108_100), Some(8_100));
    }

    #[test]
    fn an_insane_latency_is_refused_not_reported() {
        // >5s: a wedge artifact (the #50 freeze class), not an interaction.
        assert_eq!(one_sample(Kind::Press, 0, 6_000_000), None);
    }

    #[test]
    fn motion_is_drag_with_a_button_held_and_hover_without() {
        let mut c = LatencyCore::new();
        c.note_motion(Surface::Shell, 10, 10);
        c.note_button(Surface::Shell, true, 20, 20);
        c.note_motion(Surface::Shell, 30, 30);
        c.note_button(Surface::Shell, false, 40, 40);
        c.note_motion(Surface::Shell, 50, 50);
        let kinds: Vec<Kind> = c.pending.iter().map(|(_, k, _)| *k).collect();
        assert_eq!(
            kinds,
            vec![Kind::Hover, Kind::Press, Kind::Drag, Kind::Press, Kind::Hover],
            "the rubber-band class lives in the DRAG bucket, not a hover average"
        );
    }

    #[test]
    fn inputs_bind_to_the_frame_queued_after_them() {
        let mut c = LatencyCore::new();
        c.note_input(Surface::Shell, Kind::Key, 1_000, 1_000);
        c.frame_queued();
        c.note_input(Surface::Shell, Kind::Key, 2_000, 2_000); // after the queue, so the next frame
        c.frame_presented(10_000);
        assert_eq!(c.window[Surface::Shell.idx()][Kind::Key.idx()], vec![9_000]);
        c.frame_queued();
        c.frame_presented(20_000);
        assert_eq!(c.window[Surface::Shell.idx()][Kind::Key.idx()], vec![9_000, 18_000]);
    }

    #[test]
    fn a_presented_frame_with_no_bound_input_is_silent() {
        let mut c = LatencyCore::new();
        c.note_input(Surface::Shell, Kind::Key, 1_000, 1_000); // pending, NOT queued
        assert!(c.frame_presented(5_000).is_empty());
        assert!(c.window[Surface::Shell.idx()][Kind::Key.idx()].is_empty());
        assert_eq!(c.pending.len(), 1, "unqueued input must stay pending");
    }

    #[test]
    fn overflow_drops_are_counted_never_silent() {
        let mut c = LatencyCore::new();
        for i in 0..(MAX_PENDING_INPUTS + 10) {
            c.note_input(Surface::Shell, Kind::Hover, i as u64, i as u64);
        }
        assert_eq!(c.dropped().0, 10);
        for _ in 0..(MAX_INFLIGHT_FRAMES + 3) {
            c.note_input(Surface::Shell, Kind::Hover, 1, 1);
            c.frame_queued();
        }
        assert_eq!(c.dropped().1, 3);
    }

    // ── aggregation + the journal contract ──────────────────────────────────

    fn run_window(latencies_us: &[u64]) -> Vec<Summary> {
        let mut c = LatencyCore::new();
        let mut t = 0u64;
        for &l in latencies_us {
            c.note_input(Surface::Shell, Kind::Drag, t, t);
            c.frame_queued();
            c.frame_presented(t + l);
            t += 20_000;
        }
        // force the window shut with one more presented frame far in the future
        c.note_input(Surface::Shell, Kind::Drag, t + WINDOW_US, t + WINDOW_US);
        c.frame_queued();
        c.frame_presented(t + WINDOW_US + 1_000)
    }

    #[test]
    fn the_window_reports_p50_p99_max_against_the_budget() {
        // 96 fast frames and FOUR stutters: >1% of the window, so p99 lands on
        // a stutter by definition. (My first version used ONE stutter in 101
        // samples and asserted p99 caught it -- that is statistically false:
        // a single outlier sits beyond the 99th percentile. One-in-a-hundred
        // events are max's job, which is also reported; p99's job is "this
        // stutters repeatedly", and four-in-a-hundred is that.)
        let mut ls = vec![8_000u64; 96];
        ls.extend([19_000u64; 4]); // four 19ms drag frames > the 16ms budget
        let s = run_window(&ls);
        let drag = s.iter().find(|x| x.kind == Kind::Drag).unwrap();
        assert_eq!(drag.n, 101); // 100 + the window-closing frame
        assert_eq!(drag.p50_us, 8_000);
        assert!(drag.p99_us >= 19_000, "p99 must surface repeated stutter");
        assert_eq!(drag.max_us, 19_000, "max reports the worst single frame");
        assert!(!drag.pass, "four visible stutters in 100 frames is a FAIL");
    }

    #[test]
    fn two_surfaces_are_two_verdicts_not_one_blended_number() {
        // The whole point of attribution. Before it, a fast orb and a slow card averaged
        // into one `component=shell` line, so a p99 violation told you the desktop was
        // slow and nothing else. Drive the same kind through two surfaces, one inside its
        // budget and one far outside, and the window must produce two summaries with
        // opposite verdicts rather than one blurred pass.
        let mut c = LatencyCore::new();
        let mut t = 1_000u64;
        // 40 comfortable hovers over the orb (8ms), 40 terrible ones over a card (40ms).
        for _ in 0..40 {
            c.note_input(Surface::Orb, Kind::Hover, t, t);
            c.frame_queued();
            c.frame_presented(t + 8_000);
            t += 16_000;
            c.note_input(Surface::HomeCard, Kind::Hover, t, t);
            c.frame_queued();
            c.frame_presented(t + 40_000);
            t += 16_000;
        }
        // Close the window with one more presented frame past the boundary.
        c.note_input(Surface::Orb, Kind::Hover, t, t);
        c.frame_queued();
        let out = c.frame_presented(t + WINDOW_US + 8_000);
        assert!(!out.is_empty(), "the window closed and produced summaries");

        let orb = out
            .iter()
            .find(|s| s.surface == Surface::Orb && s.kind == Kind::Hover)
            .expect("the orb got its own line");
        let card = out
            .iter()
            .find(|s| s.surface == Surface::HomeCard && s.kind == Kind::Hover)
            .expect("the card got its own line");
        assert!(orb.pass, "8ms hovers are inside the 16ms budget");
        assert!(!card.pass, "40ms hovers are not, and must not hide behind the orb");
        assert!(card.p99_us > orb.p99_us * 3, "the two are nowhere near each other");
        assert_eq!(orb.n + card.n, 80, "every sample landed in exactly one bucket");
    }

    #[test]
    fn an_unattributed_sample_reports_exactly_what_it_always_did() {
        // `Shell` is not a failure case: the harness wants the WEB shell measured by this
        // same instrument so "native is faster" is a demonstrated delta. Its line must be
        // byte-identical to the one this instrument emitted before attribution existed,
        // or every historical number silently changes format.
        let s = Summary {
            surface: Surface::Shell,
            kind: Kind::Drag,
            n: 142,
            p50_us: 8_100,
            p99_us: 14_700,
            max_us: 19_200,
            budget_ms: 16,
            pass: true,
        };
        assert_eq!(
            s.journal_line(),
            "hart-latency component=shell kind=drag n=142 p50=8.1ms p99=14.7ms max=19.2ms budget=16ms verdict=PASS"
        );
    }

    #[test]
    fn every_surface_label_is_a_bare_slug_and_they_are_all_distinct() {
        // The labels are the keys of latency_budgets.json's `components` map, joined to
        // it by a Python guard. A duplicate here would silently merge two components'
        // samples into one row; a label with a space would break the journal line's
        // key=value shape that the harness greps.
        let labels: Vec<&str> = Surface::ALL.iter().map(|s| s.label()).collect();
        assert_eq!(
            labels,
            ["shell", "orb", "top-bar", "omnibox", "taskbar", "home-card"]
        );
        for (i, a) in labels.iter().enumerate() {
            assert!(!a.is_empty() && !a.contains(' '), "{a:?} is not a bare slug");
            for b in labels.iter().skip(i + 1) {
                assert_ne!(a, b, "two surfaces share a budget row");
            }
        }
        // The index each one buckets under must be unique and in range, since the window
        // is a fixed array rather than a map.
        let mut seen = [false; 6];
        for s in Surface::ALL {
            assert!(!seen[s.idx()], "two surfaces share bucket {}", s.idx());
            seen[s.idx()] = true;
        }
    }

    #[test]
    fn the_journal_line_matches_the_harness_contract() {
        let s = Summary {
            surface: Surface::Shell,
            kind: Kind::Drag,
            n: 142,
            p50_us: 8_100,
            p99_us: 14_700,
            max_us: 19_200,
            budget_ms: 16,
            pass: true,
        };
        assert_eq!(
            s.journal_line(),
            "hart-latency component=shell kind=drag n=142 p50=8.1ms p99=14.7ms max=19.2ms budget=16ms verdict=PASS"
        );
    }

    #[test]
    fn budgets_mirror_the_declared_defaults() {
        // latency_budgets.json _defaults — if that file changes, this moves
        // WITH it in the same commit (the harness's raise-with-justification
        // rule). drag/hover/scroll are one-frame by construction.
        assert_eq!(Kind::Drag.budget_ms(), 16);
        assert_eq!(Kind::Hover.budget_ms(), 16);
        assert_eq!(Kind::Scroll.budget_ms(), 16);
        assert_eq!(Kind::Press.budget_ms(), 25);
        assert_eq!(Kind::Key.budget_ms(), 25);
    }
}
