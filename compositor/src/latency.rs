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
//! base" and "kernel event µs" is ESTIMATED instead.
//!
//! THE DIRECTION OF THAT ESTIMATE MATTERS, and getting it backwards is what
//! made this instrument silent on every node from the day it was written until
//! 2026-09-10. The two clocks share a SOURCE and not an EPOCH:
//!
//!     event_us    = t - boot          (libinput, CLOCK_MONOTONIC since boot)
//!     instant_us  = t - comp_start    (base.elapsed(), base set at first use)
//!
//! The compositor starts AFTER boot, so for the same instant `t` the Instant
//! reading is the SMALLER number, by the entire boot-to-compositor gap. The
//! original code observed `delta = instant_us - event_us` and kept it only
//! `if instant_us >= event_us`, which is never true, so no observation was ever
//! recorded, `offset_us()` stayed None, and the anti-gaming rule below fired on
//! EVERY sample instead of on bad ones. Zero journal lines, forever, and the
//! unit tests all passed because they hand the core a same-epoch pairing.
//!
//! So: every input contributes `delta = event_us - instant_us`, which is
//! `(comp_start - boot) - delivery_delay`, and the rolling MAXIMUM of recent
//! deltas is the offset. Delivery delay is still strictly one-sided (an event
//! can only be observed AFTER the kernel stamped it), but with this sign a
//! LONGER delay SHRINKS the delta, so the maximum converges from BELOW onto the
//! true offset — error is the best-case delivery latency, tens of microseconds
//! on an idle dispatch loop, against budgets of 16,000. The photon time is then
//! `instant_us + offset`, moving the Instant reading INTO the kernel epoch. The estimator is pure and its convergence is
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
    /// The input that STARTED an animation, measured to the first frame that shows it.
    ///
    /// Not another way of saying Press or Key: those measure "the thing I touched
    /// reacted", this measures "the transition I asked for began". A workspace switch
    /// whose keypress echoes instantly but whose fade starts 200ms later is a pass on
    /// `key` and a failure the user actually sees, and only this bucket can tell them
    /// apart. The budget is looser (33ms, two frames) for the same reason: an animation
    /// is allowed one frame to be composed before the frame that shows it.
    AnimateStart,
}

impl Kind {
    pub fn label(self) -> &'static str {
        match self {
            Kind::Press => "press",
            Kind::Drag => "drag",
            Kind::Hover => "hover",
            Kind::Scroll => "scroll",
            Kind::Key => "key",
            Kind::AnimateStart => "animate-start",
        }
    }
    /// latency_budgets.json `_defaults`, mirrored (see module doc).
    pub fn budget_ms(self) -> u64 {
        match self {
            Kind::Drag | Kind::Hover | Kind::Scroll => 16,
            Kind::Press | Kind::Key => 25,
            Kind::AnimateStart => 33,
        }
    }
    const ALL: [Kind; 6] = [
        Kind::Press,
        Kind::Drag,
        Kind::Hover,
        Kind::Scroll,
        Kind::Key,
        Kind::AnimateStart,
    ];
    fn idx(self) -> usize {
        match self {
            Kind::Press => 0,
            Kind::Drag => 1,
            Kind::Hover => 2,
            Kind::Scroll => 3,
            Kind::Key => 4,
            Kind::AnimateStart => 5,
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
    /// The whole-desktop workspace transition. The only surface here that is NOT a
    /// `scene::Component`: it is not a thing on the desktop, it is the desktop changing,
    /// and latency_budgets.json gives it its own row with `animate-start` alone.
    WorkspaceSwitch,
    Orb,
    TopBar,
    Omnibox,
    Taskbar,
    HomeCard,
    HomeRow,
}

impl Surface {
    /// The budget file's key, and the journal line's `component=`.
    pub fn label(self) -> &'static str {
        match self {
            Surface::Shell => "shell",
            Surface::WorkspaceSwitch => "workspace-switch",
            Surface::Orb => "orb",
            Surface::TopBar => "top-bar",
            Surface::Omnibox => "omnibox",
            Surface::Taskbar => "taskbar",
            Surface::HomeCard => "home-card",
            Surface::HomeRow => "home-row",
        }
    }
    const ALL: [Surface; 8] = [
        Surface::Shell,
        Surface::WorkspaceSwitch,
        Surface::Orb,
        Surface::TopBar,
        Surface::Omnibox,
        Surface::Taskbar,
        Surface::HomeCard,
        Surface::HomeRow,
    ];
    fn idx(self) -> usize {
        match self {
            Surface::Shell => 0,
            Surface::WorkspaceSwitch => 1,
            Surface::Orb => 2,
            Surface::TopBar => 3,
            Surface::Omnibox => 4,
            Surface::Taskbar => 5,
            Surface::HomeCard => 6,
            Surface::HomeRow => 7,
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

/// What the instrument REFUSED during one window, and therefore what the numbers
/// beside it are missing.
///
/// Both counters existed and were tested; `dropped()`'s own comment calls them "the
/// no silent caps discipline", and nothing outside this module ever read them. A
/// discipline nobody reads is a silent cap with extra steps.
///
/// They are not bookkeeping. `inflight` rises only when vblanks stop being reaped,
/// which IS the #50 freeze, and `pending` rises only when frames stop being queued at
/// all. So the two conditions under which the reported p50 stops meaning anything are
/// exactly the two the journal never mentioned. A window that reports
/// `p99=6.2ms verdict=PASS` while silently discarding 900 samples is worse than no
/// instrument, because it reads as evidence.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Drops {
    /// Un-bound inputs dropped past `MAX_PENDING_INPUTS`: frames are not being queued.
    pub pending: u64,
    /// Whole batches dropped past `MAX_INFLIGHT_FRAMES`: vblanks are not being reaped.
    pub inflight: u64,
}

impl Drops {
    /// Same shape as `Summary::journal_line`, and greppable by the same `hart-latency`
    /// prefix, so one filter catches both the numbers and the reason to distrust them.
    pub fn journal_line(&self) -> String {
        format!(
            "hart-latency dropped pending={} inflight={} verdict=SUSPECT",
            self.pending, self.inflight
        )
    }
}

/// The dual of `Drops`, and the case that had no voice until 2026-09-10.
///
/// `Drops` covers "samples existed and were thrown away". This covers "samples
/// could never be MADE": vblanks are being reaped and input is arriving, but no
/// frame is ever QUEUED, so `frame_queued` never binds `pending` to anything and
/// `frame_presented` keeps popping an empty batch. The journal then says nothing
/// at all, which is the same thing an untouched machine says.
///
/// That cost hours on real hardware. The box had flips (the primary plane's
/// framebuffer id alternated), had input (the #134 seat beacon fired), and
/// reported zero `hart-latency` lines, and the only way to tell "no interaction"
/// from "the render path never queued" was to read the compositor source. An
/// instrument that cannot explain its own silence is not finished.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Stall {
    /// Vblanks reaped since the last report.
    pub presented: u64,
    /// Frames queued in the same span. Zero is the diagnosis.
    pub queued: u64,
    /// Inputs sitting unbound because nothing ever queued.
    pub pending: usize,
}

impl Stall {
    /// Same `hart-latency` prefix as the numbers and the drops, so one filter
    /// catches the reading, the reason to distrust it, and the reason there is
    /// no reading at all.
    pub fn journal_line(&self) -> String {
        format!(
            "hart-latency stalled presented={} queued={} pending={} verdict=NO-SAMPLES",
            self.presented, self.queued, self.pending
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

/// Vblanks between stall reports. At 60Hz this is about ten seconds, matching
/// the summary window, so a stalled box speaks at the same cadence a healthy
/// one does and neither floods the journal.
const STALL_REPORT_EVERY: u64 = 600;
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
    /// How much of `pending_dropped` has already been reported, so a window says
    /// "this window went wrong" rather than "something went wrong since boot", which
    /// is the difference between a signal and a stain.
    pending_reported: u64,
    /// Batches riding queued-but-not-yet-presented frames (FIFO by seq).
    inflight: VecDeque<Vec<(Surface, Kind, u64)>>,
    inflight_dropped: u64,
    inflight_reported: u64,
    button_down: bool,
    window_start_us: Option<u64>,
    /// Frames that actually bound a batch, and vblanks reaped, since start.
    /// Their DIVERGENCE is the stall signal (see `Stall`).
    frames_queued: u64,
    frames_presented: u64,
    stall_reported_at: u64,
    /// [surface][kind]. Forty-two fixed buckets, allocated once and reused: an input
    /// rate this cannot cover does not exist, and a map would put an allocation on the
    /// input path for no benefit.
    window: [[Vec<u64>; 6]; 8],
}

impl LatencyCore {
    pub fn new() -> Self {
        LatencyCore {
            offset_obs: VecDeque::new(),
            pending: Vec::new(),
            pending_dropped: 0,
            pending_reported: 0,
            inflight: VecDeque::new(),
            inflight_dropped: 0,
            inflight_reported: 0,
            button_down: false,
            window_start_us: None,
            frames_queued: 0,
            frames_presented: 0,
            stall_reported_at: 0,
            window: Default::default(),
        }
    }

    /// The estimated (event-domain minus instant-domain) clock offset, or None
    /// before any input has been observed. max() over the window: each
    /// observation is `(comp_start - boot) - delivery_delay`, so delivery delay
    /// only ever SUBTRACTS and the largest observation is the closest to truth
    /// (see the module doc for the direction and the error bound).
    pub fn offset_us(&self) -> Option<u64> {
        self.offset_obs.iter().copied().max()
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
        //
        // event_us - instant_us, NOT the reverse: the kernel epoch (boot) is
        // EARLIER than the Instant base (compositor start), so the kernel
        // reading is the larger of the two. The reverse subtraction was never
        // once satisfied on a real node, which is precisely why this instrument
        // reported nothing until 2026-09-10.
        if event_us >= instant_us {
            if self.offset_obs.len() == OFFSET_WINDOW {
                self.offset_obs.pop_front();
            }
            self.offset_obs.push_back(event_us - instant_us);
        }
        if self.pending.len() == MAX_PENDING_INPUTS {
            self.pending.remove(0);
            self.pending_dropped += 1;
        }
        self.pending.push((surface, kind, event_us));
    }

    /// The input just processed turned out to START an animation.
    ///
    /// Re-kinds the most recent pending input rather than recording a second sample: the
    /// keypress and the transition it caused are ONE interaction, and counting it twice
    /// would put the same photon in two buckets. Whatever surface the pointer was over is
    /// replaced too, because a workspace switch is not about the thing under the cursor.
    ///
    /// Called AFTER the event is applied, which is the only moment the compositor can
    /// know: an animation starting is a consequence, not a property of the event. The
    /// input's own kernel timestamp is untouched, so the sample still measures from the
    /// key the user pressed to the frame that showed the fade.
    ///
    /// A no-op when the batch has already been bound to a frame (nothing to re-kind), and
    /// when no input is pending at all, which is what a client-caused animation looks
    /// like from here: those are correctly not attributed to any input.
    pub fn note_animation_started(&mut self, surface: Surface) {
        if let Some(last) = self.pending.last_mut() {
            last.0 = surface;
            last.1 = Kind::AnimateStart;
        }
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
        self.frames_queued += 1;
    }

    /// A vblank completed (`reap_completed_vblanks`): the OLDEST queued batch
    /// just reached the screen. `instant_us` is the caller's monotonic reading
    /// at the reap; it is converted into the event clock via the estimator.
    /// Returns finished window summaries (empty most calls) — io is the
    /// caller's job.
    pub fn frame_presented(&mut self, instant_us: u64) -> Vec<Summary> {
        self.frames_presented += 1;
        let batch = self.inflight.pop_front().unwrap_or_default();
        if let Some(off) = self.offset_us() {
            // Refuse to fabricate: no offset means no anchored photon time.
            // ADD: `off` carries the Instant reading forward into the kernel
            // epoch, where `t_in` already lives. Subtracting moved it the wrong
            // way by twice the gap.
            let photon_event_us = instant_us.saturating_add(off);
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

    /// Diagnostics for the drop counters (the "no silent caps" discipline). Running
    /// totals since construction; `take_drops` is what the journal reports.
    pub fn dropped(&self) -> (u64, u64) {
        (self.pending_dropped, self.inflight_dropped)
    }

    /// Is the instrument unable to MAKE samples, and has it not said so yet?
    ///
    /// `Some` only when vblanks are being reaped, input is waiting, and NOTHING
    /// has been queued in the span — the one shape that produces silence rather
    /// than numbers. Reported at most once per `STALL_REPORT_EVERY` vblanks so a
    /// genuinely wedged box says it periodically instead of every frame.
    pub fn take_stall(&mut self) -> Option<Stall> {
        let since = self.frames_presented - self.stall_reported_at;
        if since < STALL_REPORT_EVERY {
            return None;
        }
        // Nothing waiting means nobody touched the box, which is not a stall.
        if self.pending.is_empty() {
            self.stall_reported_at = self.frames_presented;
            return None;
        }
        // Frames ARE binding, so the instrument is working; silence would then
        // be a real absence of interaction, and this must not cry wolf.
        if self.frames_queued > 0 {
            self.stall_reported_at = self.frames_presented;
            return None;
        }
        self.stall_reported_at = self.frames_presented;
        Some(Stall {
            presented: since,
            queued: 0,
            pending: self.pending.len(),
        })
    }

    /// What was dropped since the last call, or `None` when nothing was.
    ///
    /// `None` rather than a zeroed record so a healthy box logs nothing extra: the
    /// line has to be rare to be worth reading.
    pub fn take_drops(&mut self) -> Option<Drops> {
        let d = Drops {
            pending: self.pending_dropped - self.pending_reported,
            inflight: self.inflight_dropped - self.inflight_reported,
        };
        if d.pending == 0 && d.inflight == 0 {
            return None;
        }
        self.pending_reported = self.pending_dropped;
        self.inflight_reported = self.inflight_dropped;
        Some(d)
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

/// The input just handled started an animation (see `note_animation_started`).
pub fn on_animation_started(surface: Surface) {
    let g = global();
    if let Ok(mut c) = g.core.lock() {
        c.note_animation_started(surface);
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
pub fn on_frame_presented() -> (Vec<Summary>, Option<Drops>, Option<Stall>) {
    let g = global();
    // Both under ONE lock: the drops belong to the window the summaries describe, and
    // taking them separately would let a drop land between the two and be attributed
    // to the next window, which is the one place this record must not lie.
    let (summaries, drops, stall) = match g.core.lock() {
        Ok(mut c) => {
            let s = c.frame_presented(instant_us());
            let d = c.take_drops();
            let st = c.take_stall();
            (s, d, st)
        }
        Err(_) => (Vec::new(), None, None),
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
    // The drop record rides the SAME opt-in sink, because a run whose jsonl says
    // PASS and whose journal says SUSPECT is a run whose two halves disagree.
    if let Some(d) = drops {
        if std::env::var("HART_LATENCY_JSONL").ok().as_deref() == Some("1") {
            use std::io::Write;
            if let Ok(mut f) = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open("/run/hart/latency.jsonl")
            {
                let _ = writeln!(
                    f,
                    "{{\"dropped\":true,\"pending\":{},\"inflight\":{}}}",
                    d.pending, d.inflight
                );
            }
        }
    }
    (summaries, drops, stall)
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── the drop record: the instrument saying its own numbers are suspect ──

    #[test]
    fn a_window_that_refused_samples_says_so_instead_of_reporting_a_clean_pass() {
        // Both counters existed and were tested; nothing outside this module read
        // them. So a window could discard hundreds of samples and still print
        // `verdict=PASS`, which is worse than no instrument because it reads as
        // evidence. The two conditions that trip them are the two that make the
        // numbers meaningless: `inflight` rises only when vblanks stop being reaped
        // (the #50 freeze), `pending` only when frames stop being queued at all.
        let mut c = LatencyCore::new();
        assert_eq!(c.take_drops(), None, "a healthy window says nothing");

        // Overrun the un-bound input cap: no frame is ever queued, so nothing binds.
        for i in 0..(MAX_PENDING_INPUTS as u64 + 10) {
            c.note_motion(Surface::Shell, 1_000 + i, 1_000 + i);
        }
        let d = c.take_drops().expect("the refusal is reported");
        assert_eq!(d.pending, 10, "exactly the samples past the cap");
        assert_eq!(d.inflight, 0);
        assert!(
            d.journal_line().starts_with("hart-latency "),
            "one grep catches the numbers and the reason to distrust them: {}",
            d.journal_line()
        );
        assert!(d.journal_line().contains("verdict=SUSPECT"));

        // Drained, not restated: the next window is about the next window.
        assert_eq!(c.take_drops(), None, "a quiet window after a loud one is quiet");
        // ...while the running total is still the running total.
        assert_eq!(c.dropped().0, 10, "take_drops reports a delta, not a reset");

        // And the freeze counter reports on its own terms.
        for _ in 0..(MAX_INFLIGHT_FRAMES + 3) {
            c.note_motion(Surface::Shell, 2_000, 2_000);
            c.frame_queued();
        }
        let d = c.take_drops().expect("dropped batches are reported");
        assert_eq!(d.inflight, 3, "vblanks stopped being reaped, and it is said");
    }

    #[test]
    fn the_drop_record_belongs_to_the_window_it_is_reported_with() {
        // A drop taken outside the flush would be attributed to the NEXT window,
        // which is the one place this record must not lie: it exists to qualify the
        // numbers printed beside it.
        // Clocks paired the way a booted node pairs them: kernel stamps are
        // since BOOT, Instant readings are since COMPOSITOR START, so the
        // kernel number is larger by the gap. Written as `t + 500` before,
        // which is the impossible direction and stopped producing a sample
        // once the estimator was corrected.
        const GAP: u64 = 1_000_000;
        let mut c = LatencyCore::new();
        let t = 2_000_000; // kernel stamp, since boot
        let inst = |kernel: u64| kernel - GAP; // the same moment, Instant domain
        // A real, well-formed sample, so the window has something to report.
        // 500µs of delivery delay: observed slightly later than stamped.
        c.note_motion(Surface::Shell, t, inst(t) + 500);
        c.frame_queued();
        c.frame_presented(inst(t + 8_000));
        // Then a burst that overruns the cap before the window closes.
        for i in 0..(MAX_PENDING_INPUTS as u64 + 5) {
            c.note_motion(Surface::Shell, t + 10_000 + i, inst(t + 10_000 + i));
        }
        let out = c.frame_presented(inst(t + WINDOW_US + 8_000));
        let d = c.take_drops().expect("the same window carries both");
        assert!(!out.is_empty(), "the window still reports its summary");
        assert_eq!(d.pending, 5, "and says what it had to throw away to get it");
    }

    // ── the offset estimator ────────────────────────────────────────────────

    #[test]
    fn the_offset_estimator_converges_from_below() {
        // A REALISTIC pairing: the kernel epoch is boot, the Instant base is
        // compositor start, so the kernel reading is LARGER by the gap between
        // them. The old version of this test had instant_us AHEAD of event_us,
        // which would require the Instant base to predate boot, and that
        // impossible pairing is why the suite stayed green while the instrument
        // emitted nothing on hardware.
        const EPOCH_GAP: u64 = 1_000_000; // compositor started 1s after boot
        let mut c = LatencyCore::new();
        for (i, delay) in [900u64, 40, 300, 15, 700, 90].iter().enumerate() {
            let ev = EPOCH_GAP + (i as u64) * 16_000; // kernel stamp, since boot
            let instant = ev - EPOCH_GAP + delay; // observed, since comp start
            c.note_input(Surface::Shell, Kind::Hover, ev, instant);
        }
        // max picks the fastest delivery: error == 15µs against 16ms budgets.
        assert_eq!(c.offset_us(), Some(EPOCH_GAP - 15));
    }

    #[test]
    fn a_realistic_epoch_gap_still_produces_a_sample() {
        // THE REGRESSION GUARD. Every other test in this module pairs the two
        // clocks at the same origin, which is the one case that cannot happen
        // on a real machine. This one uses the shape a booted node actually
        // has, and it fails outright against the pre-2026-09-10 code: there the
        // observation was skipped, offset_us() stayed None, and frame_presented
        // recorded nothing at all.
        const EPOCH_GAP: u64 = 30_000_000; // compositor up 30s after boot
        let mut c = LatencyCore::new();
        let t_in = EPOCH_GAP + 500_000; // kernel stamp, since boot
        c.note_input(Surface::Shell, Kind::Press, t_in, t_in - EPOCH_GAP);
        assert_eq!(c.offset_us(), Some(EPOCH_GAP), "the offset IS the epoch gap");
        c.frame_queued();
        // Photon 8.1ms after the input, expressed in the Instant domain.
        c.frame_presented(t_in - EPOCH_GAP + 8_100);
        let got = c.window[Surface::Shell.idx()][Kind::Press.idx()].first().copied();
        assert_eq!(got, Some(8_100), "a real epoch gap must still measure 8.1ms");
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
    fn the_key_that_starts_a_transition_is_measured_as_the_transition() {
        // A workspace switch whose keypress echoes instantly but whose fade starts
        // 200ms later passes `key` and fails the user. The two are ONE interaction, so
        // the input is RE-KINDED rather than counted twice: the same photon must not
        // land in two buckets.
        let mut c = LatencyCore::new();
        c.note_input(Surface::Shell, Kind::Key, 1_000, 1_000); // offset 0
        c.note_animation_started(Surface::WorkspaceSwitch);
        c.frame_queued();
        c.frame_presented(31_000);
        // Nothing in the plain `key` bucket: it became the transition.
        assert!(c.window[Surface::Shell.idx()][Kind::Key.idx()].is_empty());
        let ws = &c.window[Surface::WorkspaceSwitch.idx()][Kind::AnimateStart.idx()];
        assert_eq!(ws, &vec![30_000], "measured from the KEY, not from the fade's start");

        // And the budget it is checked against is the looser animation one: 30ms is a
        // FAIL for a 25ms keypress and a PASS for a 33ms animation start. Getting this
        // wrong in either direction is a verdict about the wrong thing.
        assert_eq!(Kind::Key.budget_ms(), 25);
        assert_eq!(Kind::AnimateStart.budget_ms(), 33);
    }

    #[test]
    fn an_animation_nobody_asked_for_is_attributed_to_nobody() {
        // A client mapping its own window animates without any input causing it. Whatever
        // key happens to be pending is NOT the cause, and re-kinding it would invent a
        // number. With no pending input there is nothing to re-kind, which is the correct
        // and only honest outcome.
        let mut c = LatencyCore::new();
        c.note_animation_started(Surface::WorkspaceSwitch);
        c.frame_queued();
        assert!(c.frame_presented(10_000).is_empty(), "no sample was fabricated");
        assert!(c
            .window
            .iter()
            .all(|per_kind| per_kind.iter().all(|w| w.is_empty())));

        // An input already bound to an earlier frame is likewise past re-kinding: the
        // photon it is waiting on is not this animation's.
        let mut c = LatencyCore::new();
        c.note_input(Surface::Shell, Kind::Key, 1_000, 1_000);
        c.frame_queued();
        c.note_animation_started(Surface::WorkspaceSwitch);
        c.frame_presented(9_000);
        assert_eq!(
            c.window[Surface::Shell.idx()][Kind::Key.idx()],
            vec![8_000],
            "the bound sample stays the keypress it was"
        );
        assert!(c.window[Surface::WorkspaceSwitch.idx()][Kind::AnimateStart.idx()].is_empty());
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
            ["shell", "workspace-switch", "orb", "top-bar", "omnibox", "taskbar",
             "home-card", "home-row"]
        );
        for (i, a) in labels.iter().enumerate() {
            assert!(!a.is_empty() && !a.contains(' '), "{a:?} is not a bare slug");
            for b in labels.iter().skip(i + 1) {
                assert_ne!(a, b, "two surfaces share a budget row");
            }
        }
        // The index each one buckets under must be unique and in range, since the window
        // is a fixed array rather than a map.
        let mut seen = [false; 8];
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

    // ── The stall diagnostic: explaining silence ───────────────────────────
    //
    // These three cover the whole decision, because the failure they guard
    // against is a FALSE alarm as much as a missed one. An instrument that
    // shouts "stalled" at an idle desk is noise, and noise gets filtered, and
    // then the real stall is invisible again.

    #[test]
    fn a_stall_is_reported_when_vblanks_reap_but_nothing_ever_queues() {
        // The real-hardware shape, 2026-09-10: flips happening, input arriving,
        // no frame ever queued, and a journal that said nothing at all.
        let mut c = LatencyCore::new();
        c.note_input(Surface::Shell, Kind::Hover, 1_000, 1_100);
        for i in 0..STALL_REPORT_EVERY {
            assert!(c.frame_presented(2_000 + i).is_empty());
        }
        let st = c.take_stall().expect("silence with input waiting must explain itself");
        assert_eq!(st.queued, 0, "zero queued frames IS the diagnosis");
        assert_eq!(st.presented, STALL_REPORT_EVERY);
        assert!(st.pending >= 1, "the unbound input is what makes it a stall");
        assert!(st.journal_line().contains("verdict=NO-SAMPLES"));
        assert!(
            st.journal_line().starts_with("hart-latency "),
            "one filter must catch the numbers, the drops and the silence"
        );
    }

    #[test]
    fn no_stall_is_reported_when_frames_are_binding() {
        // Frames queue, so the instrument works; any silence after this is a
        // real absence of interaction and must not be blamed on the pipeline.
        let mut c = LatencyCore::new();
        c.note_input(Surface::Shell, Kind::Hover, 1_000, 1_100);
        c.frame_queued();
        c.note_input(Surface::Shell, Kind::Hover, 3_000, 3_100);
        for i in 0..STALL_REPORT_EVERY {
            let _ = c.frame_presented(4_000 + i);
        }
        assert!(
            c.take_stall().is_none(),
            "a pipeline that binds must never be reported as stalled"
        );
    }

    #[test]
    fn an_untouched_box_is_not_a_stall() {
        // No input at all is exactly what a headless machine nobody has touched
        // looks like, and it is NOT a defect. This is the false-positive guard:
        // the node that started this whole investigation had zero input for its
        // entire uptime, and calling that a stall would have been wrong.
        let mut c = LatencyCore::new();
        for i in 0..STALL_REPORT_EVERY {
            let _ = c.frame_presented(1_000 + i);
        }
        assert!(
            c.take_stall().is_none(),
            "no input pending means nobody interacted, not that the pipeline broke"
        );
    }

    #[test]
    fn the_stall_report_is_rate_limited() {
        // A wedged box should say so periodically, not 60 times a second.
        let mut c = LatencyCore::new();
        c.note_input(Surface::Shell, Kind::Hover, 1_000, 1_100);
        for i in 0..STALL_REPORT_EVERY {
            let _ = c.frame_presented(2_000 + i);
        }
        assert!(c.take_stall().is_some(), "first crossing reports");
        assert!(c.take_stall().is_none(), "and does not repeat until the next span");
        for i in 0..STALL_REPORT_EVERY {
            let _ = c.frame_presented(10_000 + i);
        }
        assert!(c.take_stall().is_some(), "the next span reports again");
    }
}
