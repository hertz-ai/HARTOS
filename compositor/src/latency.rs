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
//!
//! THE FRAME-TIME INSTRUMENT shares this module, this window and these rules,
//! because the two NFRs it serves ("frame budget 16.6 ms, p99 < 12 ms" and
//! "p99.9 zero dropped frames") had no instrument at all while the input-to-photon
//! one had been proven on hardware. It measures the compositor's OWN cost per frame:
//! from the render tick starting to build a frame to `queue_frame` accepting it.
//! That is deliberately NOT queue-to-present, which on a 60 Hz panel is always about
//! one refresh interval whatever the compositor did, so a p99 of it against 12 ms
//! would FAIL forever and be ignored. Queue-to-present is measured too, but as the
//! thing it actually tells you: a flip that took more than one and a half refresh
//! intervals missed its vblank, and is counted as a DROPPED frame. One line per 10 s
//! window: `hart-frame n=600 p50=3.2ms p99=9.8ms max=17.1ms budget=16.6ms
//! target=12ms violations=1 dropped=0 verdict=PASS`. Same anti-gaming posture: a
//! distribution, never a mean; every queued frame counts, under load or idle; and
//! the window closes on the compositor's own clock, so a box nobody has touched
//! still reports its frame times rather than staying silent until the first input.

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
    /// Frames queued in the same span. Zero means `frame_queued` is not being
    /// reached, so nothing can bind.
    pub queued: u64,
    /// Inputs waiting to be bound to a frame.
    pub pending: usize,
    /// Samples that actually resolved. Zero WITH a non-zero `queued` means
    /// binding happened but the sample was refused -- an unanchored clock or a
    /// latency outside the sane window.
    pub samples: u64,
    /// Whether the clock offset has been established at all. `false` means no
    /// input observation was ever accepted, which is its own distinct fault.
    pub anchored: bool,
    /// Render passes in the span, and how many decided nothing had changed.
    /// `attempted` high with `unchanged` equally high is a compositor that
    /// believes the screen is static; `attempted` near zero is a render loop
    /// that is not running at all. The two need completely different fixes.
    pub attempted: u64,
    pub unchanged: u64,
}

impl Stall {
    /// Same `hart-latency` prefix as the numbers and the drops, so one filter
    /// catches the reading, the reason to distrust it, and the reason there is
    /// no reading at all.
    pub fn journal_line(&self) -> String {
        format!(
            "hart-latency stalled rendered={} unchanged={} queued={} presented={} \
             pending={} samples={} anchored={} verdict=NO-SAMPLES",
            self.attempted, self.unchanged, self.queued, self.presented,
            self.pending, self.samples, self.anchored
        )
    }
}

/// One 10 s window of the compositor's own frame times, and the flips that missed.
///
/// `violations` counts frames over `FRAME_BUDGET_US` (each one missed the vblank it was
/// built for). `dropped` counts flips whose vblank came more than one and a half
/// refresh intervals after the queue, which is a frame the person saw held. The verdict
/// is the NFR pair read as the user experiences it: p99 inside the target AND nothing
/// dropped. A window with a 9 ms p99 and one dropped frame is a stutter, not a pass.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FrameSummary {
    pub n: usize,
    pub p50_us: u64,
    pub p99_us: u64,
    pub max_us: u64,
    pub violations: u64,
    pub dropped: u64,
    pub pass: bool,
}

impl FrameSummary {
    /// Byte-stable, like `Summary::journal_line`, and greppable by its own prefix so a
    /// reader can ask for frame times without wading through per-component lines. The
    /// budget and target are printed so the verdict's basis travels with the numbers.
    pub fn journal_line(&self) -> String {
        format!(
            "hart-frame n={} p50={:.1}ms p99={:.1}ms max={:.1}ms budget={:.1}ms target={}ms violations={} dropped={} verdict={}",
            self.n,
            self.p50_us as f64 / 1000.0,
            self.p99_us as f64 / 1000.0,
            self.max_us as f64 / 1000.0,
            FRAME_BUDGET_US as f64 / 1000.0,
            FRAME_P99_TARGET_US / 1000,
            self.violations,
            self.dropped,
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

/// Vblanks between stall reports.
///
/// Was 600, chosen as "ten seconds at 60Hz". That reasoning assumed a desktop
/// that flips 60 times a second, and the desktop this runs on is DAMAGE-TRACKED:
/// when nothing moves it flips a handful of times a minute. 600 vblanks is then
/// tens of minutes away, so the diagnostic that exists to explain silence was
/// itself silent through a two-minute probe on real hardware 2026-09-10.
///
/// 60 is reachable on a quiet box within a probe, and still rare enough on a
/// busy one (one line per second at full rate) to stay readable.
const STALL_REPORT_EVERY: u64 = 60;
/// Offset observations kept for the rolling-min estimator.
const OFFSET_WINDOW: usize = 64;

/// The compositor's own frame budget: one 60 Hz refresh. Mirrors
/// latency_budgets.json `_frame.budget_ms`; a Python guard pins the two together. A
/// frame that took longer than this from the tick starting to build it to `queue_frame`
/// accepting it has missed the vblank it was aimed at, and is counted as a VIOLATION
/// whether or not the flip later landed.
pub const FRAME_BUDGET_US: u64 = 16_600;
/// The binding NFR on the distribution: p99 of frame time under 12 ms
/// (NATIVE_SHELL_PARITY_PROGRAM, "frame budget 16.6 ms, p99 < 12 ms"). Mirrors
/// `_frame.p99_ms`.
pub const FRAME_P99_TARGET_US: u64 = 12_000;
/// The refresh period assumed until the DRM backend reports the mode it actually set.
const DEFAULT_REFRESH_US: u64 = 16_667;

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
    samples_recorded: u64,
    /// Render attempts, and how many reported "nothing changed". These are the
    /// only counters that move on a box that never presents, which is why the
    /// stall report is gated on them rather than on presented frames.
    renders_attempted: u64,
    renders_unchanged: u64,
    stall_reported_at: u64,
    /// [surface][kind]. Forty-two fixed buckets, allocated once and reused: an input
    /// rate this cannot cover does not exist, and a map would put an allocation on the
    /// input path for no benefit.
    window: [[Vec<u64>; 6]; 8],
    /// Frame times (tick start to `queue_frame` Ok) in the open window, same cap as the
    /// latency buckets.
    frame_window: Vec<u64>,
    frame_violations: u64,
    frame_dropped: u64,
    /// Instant-domain queue time of each frame handed to DRM and not yet presented,
    /// paired FIFO with vblanks exactly as `inflight` is. The F1 gate allows one flip in
    /// flight per CRTC, and this stream is single-CRTC by the documented limitation, so
    /// at queue time this is empty unless a vblank was LOST.
    queued_at: VecDeque<u64>,
    refresh_us: u64,
    /// The closed window's frame summary, waiting for `take_frame_report`.
    frame_report: Option<FrameSummary>,
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
            samples_recorded: 0,
            renders_attempted: 0,
            renders_unchanged: 0,
            stall_reported_at: 0,
            window: Default::default(),
            frame_window: Vec::new(),
            frame_violations: 0,
            frame_dropped: 0,
            queued_at: VecDeque::new(),
            refresh_us: DEFAULT_REFRESH_US,
            frame_report: None,
        }
    }

    /// The output's refresh period, from the mode the backend actually set (millihertz,
    /// as wl_output reports it). Only decides what "this flip missed its vblank" means.
    pub fn set_refresh_mhz(&mut self, mhz: u64) {
        if mhz > 0 {
            self.refresh_us = 1_000_000_000 / mhz;
        }
    }

    /// A frame was handed to DRM (`queue_frame` Ok), `frame_us` after the tick started
    /// building it, at `instant_us`. Records the frame time against the budget and starts
    /// the flip timer the matching vblank will stop.
    ///
    /// Anything still waiting here at queue time is a flip whose vblank never came: the
    /// gate does not queue on top of an in-flight flip, so the only way to arrive with the
    /// queue non-empty is the lost-vblank hatch having retired one. Each is a frame the
    /// person never saw, counted as dropped, and cleared so the FIFO pairing stays honest
    /// instead of misattributing every later vblank by one.
    pub fn note_frame_queued(&mut self, frame_us: u64, instant_us: u64) {
        if self.frame_window.len() < MAX_WINDOW_SAMPLES {
            self.frame_window.push(frame_us);
        }
        if frame_us > FRAME_BUDGET_US {
            self.frame_violations += 1;
        }
        while self.queued_at.pop_front().is_some() {
            self.frame_dropped += 1;
        }
        self.queued_at.push_back(instant_us);
    }

    /// The frame summary of the last closed window, once.
    pub fn take_frame_report(&mut self) -> Option<FrameSummary> {
        self.frame_report.take()
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

    /// One pass of the render loop finished. `unchanged` is the compositor's own
    /// verdict that nothing needed drawing, which is the branch that does NOT
    /// queue a frame and therefore cannot bind any input.
    pub fn note_render(&mut self, unchanged: bool) {
        self.renders_attempted += 1;
        if unchanged {
            self.renders_unchanged += 1;
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
        // The flip that just completed is the oldest queued one. Longer than one and a
        // half refresh intervals from queue to vblank means it missed the vblank it was
        // aimed at and the person saw the previous frame held: a dropped frame.
        if let Some(queued) = self.queued_at.pop_front() {
            let flip_us = instant_us.saturating_sub(queued);
            if flip_us > self.refresh_us + self.refresh_us / 2 {
                self.frame_dropped += 1;
            }
        }
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
                self.samples_recorded += 1;
                let w = &mut self.window[surface.idx()][kind.idx()];
                if w.len() < MAX_WINDOW_SAMPLES {
                    w.push(lat);
                }
            }
        }
        // The 10 s window is paced on the compositor's own clock. It used to be paced on
        // the kernel epoch and only once an input had anchored the estimator, which was
        // harmless while every sample needed an input anyway. Frame times do not: an
        // untouched box still builds and presents frames, and the frame instrument must
        // report them rather than stay silent until someone touches the mouse. Both
        // clocks are monotonic and the window is a duration, so the latency summaries
        // close on exactly the boundary they did before.
        if self.window_start_us.is_none() {
            self.window_start_us = Some(instant_us);
        }
        if let Some(start) = self.window_start_us {
            if instant_us.saturating_sub(start) >= WINDOW_US {
                return self.close_window(instant_us);
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
        // The frame-time summary for the same window, parked for `take_frame_report` so
        // the return type the latency tests pin stays what it was. Same percentile
        // arithmetic as above, deliberately: one definition of p99 in this module.
        if !self.frame_window.is_empty() {
            let w = &mut self.frame_window;
            w.sort_unstable();
            let n = w.len();
            let p50 = w[(n - 1) / 2];
            let p99 = w[((n - 1) * 99) / 100];
            let max = *w.last().unwrap();
            self.frame_report = Some(FrameSummary {
                n,
                p50_us: p50,
                p99_us: p99,
                max_us: max,
                violations: self.frame_violations,
                dropped: self.frame_dropped,
                pass: p99 <= FRAME_P99_TARGET_US && self.frame_dropped == 0,
            });
            w.clear();
        }
        self.frame_violations = 0;
        self.frame_dropped = 0;
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
        // Gated on RENDER ATTEMPTS, not presented frames. Gating on presentation
        // made this silent on exactly the box it was written for: one that
        // presents almost nothing. A diagnostic must not require the absence of
        // the fault it reports.
        let since = self.renders_attempted - self.stall_reported_at;
        if since < STALL_REPORT_EVERY {
            return None;
        }
        self.stall_reported_at = self.renders_attempted;

        // Nothing waiting AND nothing ever anchored means nobody has touched the
        // box. That is not a stall, and saying so at an idle desk is how a
        // diagnostic becomes noise and then gets ignored.
        if self.pending.is_empty() && self.offset_obs.is_empty() {
            return None;
        }
        // Samples ARE resolving, so the instrument works end to end. Any silence
        // after this is a genuine absence of interaction.
        if self.samples_recorded > 0 {
            return None;
        }
        // Input has been seen and frames have been presented, yet nothing
        // resolved. Report the counters rather than a guess: `queued == 0` says
        // frame_queued is never reached, `anchored == false` says no clock
        // observation was accepted, and both non-zero with samples == 0 says the
        // sample was computed and refused.
        Some(Stall {
            presented: self.frames_presented,
            queued: self.frames_queued,
            pending: self.pending.len(),
            samples: self.samples_recorded,
            anchored: !self.offset_obs.is_empty(),
            attempted: since,
            unchanged: self.renders_unchanged,
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

/// Called once per render pass with the compositor's own "nothing changed"
/// verdict, so the instrument can tell a static desktop from a dead render loop.
///
/// Returns the stall record when the span has earned one, and THIS is the only
/// place it is taken. It used to be taken inside `on_frame_presented`, which
/// meant the diagnostic whose entire job is to report "frames are not reaching
/// the screen" could only speak from the code path that runs when a frame
/// reaches the screen. Moving the GATE onto render attempts earlier the same day
/// fixed which counter it watched and left that reachability untouched, so it
/// stayed silent on hardware for another day. A diagnostic has to be reachable
/// on the path that is still alive during the fault it describes.
pub fn on_render(unchanged: bool) -> Option<Stall> {
    let g = global();
    let mut c = g.core.lock().ok()?;
    c.note_render(unchanged);
    c.take_stall()
}

/// The output's refresh, in millihertz as the DRM backend has it, so "dropped" is
/// judged against the panel actually driven rather than an assumed 60 Hz.
pub fn on_output_refresh_mhz(mhz: u64) {
    let g = global();
    if let Ok(mut c) = g.core.lock() {
        c.set_refresh_mhz(mhz);
    }
}

/// A frame was handed to DRM. `frame_us` is how long the tick took to build, composite
/// and queue it (the compositor's own frame time); the pending inputs bind to it.
pub fn on_frame_queued(frame_us: u64) {
    let g = global();
    if let Ok(mut c) = g.core.lock() {
        c.frame_queued();
        c.note_frame_queued(frame_us, instant_us());
    }
}

/// Called from the vblank reaper. Emits the journal lines and (opt-in) the
/// jsonl sink here so udev.rs stays one line.
pub fn on_frame_presented() -> (Vec<Summary>, Option<Drops>, Option<FrameSummary>) {
    let g = global();
    // All three under ONE lock: the drops and the frame report belong to the window the
    // summaries describe, and taking them separately would let a drop land between the
    // two and be attributed to the next window, which is the one place this record must
    // not lie.
    let (summaries, drops, frames) = match g.core.lock() {
        Ok(mut c) => {
            let s = c.frame_presented(instant_us());
            let d = c.take_drops();
            let f = c.take_frame_report();
            (s, d, f)
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
    // The frame report rides the same opt-in sink for the same reason: a harness run
    // diffing builds needs the frame-time distribution beside the latency one.
    if let Some(fr) = &frames {
        if std::env::var("HART_LATENCY_JSONL").ok().as_deref() == Some("1") {
            use std::io::Write;
            if let Ok(mut f) = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open("/run/hart/latency.jsonl")
            {
                let _ = writeln!(
                    f,
                    "{{\"frame\":true,\"n\":{},\"p50_us\":{},\"p99_us\":{},\"max_us\":{},\"violations\":{},\"dropped\":{},\"pass\":{}}}",
                    fr.n, fr.p50_us, fr.p99_us, fr.max_us, fr.violations, fr.dropped, fr.pass
                );
            }
        }
    }
    (summaries, drops, frames)
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

    // ── the frame-time instrument: the NFR that had none ─────────────────────

    #[test]
    fn the_frame_line_matches_its_contract_byte_for_byte() {
        let f = FrameSummary {
            n: 600,
            p50_us: 3_200,
            p99_us: 9_800,
            max_us: 17_100,
            violations: 1,
            dropped: 0,
            pass: true,
        };
        assert_eq!(
            f.journal_line(),
            "hart-frame n=600 p50=3.2ms p99=9.8ms max=17.1ms budget=16.6ms target=12ms violations=1 dropped=0 verdict=PASS"
        );
    }

    #[test]
    fn frame_times_are_reported_per_window_on_an_untouched_box() {
        // No input ever: the estimator is unanchored and the latency half must stay
        // silent (anti-gaming). The frame half must NOT, because the box was building
        // and presenting frames the whole time, and a window that closes only once a
        // person shows up would report the easy case only.
        let mut c = LatencyCore::new();
        let mut t = 0u64;
        for i in 0..100u64 {
            // 99 frames at 5 ms and one 20 ms frame that missed its vblank.
            let frame_us = if i == 42 { 20_000 } else { 5_000 };
            c.note_frame_queued(frame_us, t);
            assert!(c.frame_presented(t + 16_000).is_empty(), "no anchored samples, no latency line");
            t += 16_667;
        }
        assert!(c.take_frame_report().is_none(), "the window has not closed yet");
        // One more present past the 10 s boundary closes the window.
        c.note_frame_queued(5_000, t + WINDOW_US);
        assert!(c.frame_presented(t + WINDOW_US + 16_000).is_empty());
        let r = c.take_frame_report().expect("the frame window closed on the compositor clock");
        assert_eq!(r.n, 101);
        assert_eq!(r.p50_us, 5_000);
        assert_eq!(r.p99_us, 5_000, "one outlier in a hundred is max's job, not p99's");
        assert_eq!(r.max_us, 20_000);
        assert_eq!(r.violations, 1, "the 20 ms frame is over the 16.6 ms budget");
        assert_eq!(r.dropped, 0, "every flip landed on its next vblank");
        assert!(r.pass);
        assert!(r.journal_line().starts_with("hart-frame "));
        assert!(c.take_frame_report().is_none(), "reported once, not restated");
    }

    #[test]
    fn a_flip_that_missed_its_vblank_is_a_dropped_frame_and_fails_the_window() {
        // Queue-to-present on a 60 Hz panel is normally under one interval. Forty
        // milliseconds is more than one and a half, so that frame was held on screen
        // while the next waited: the stutter the p99.9 NFR is about. A single one fails
        // the window even with a comfortable p99.
        let mut c = LatencyCore::new();
        c.note_frame_queued(4_000, 0);
        let _ = c.frame_presented(16_000); // landed
        c.note_frame_queued(4_000, 16_667);
        let _ = c.frame_presented(16_667 + 40_000); // missed
        c.note_frame_queued(4_000, WINDOW_US);
        let _ = c.frame_presented(WINDOW_US + 16_000);
        let r = c.take_frame_report().expect("window closed");
        assert_eq!(r.dropped, 1);
        assert_eq!(r.violations, 0, "the frame itself was cheap; the FLIP was late");
        assert!(r.p99_us <= FRAME_P99_TARGET_US);
        assert!(!r.pass, "a dropped frame is a FAIL whatever the p99 says");
    }

    #[test]
    fn a_lost_vblank_is_counted_as_dropped_and_does_not_skew_every_later_flip() {
        // The lost-vblank hatch retires a flip whose event never came and the tick
        // queues again. Without clearing the FIFO here, every later vblank would pair
        // with the frame before it and read as ~two intervals late forever.
        let mut c = LatencyCore::new();
        c.note_frame_queued(4_000, 0); // its vblank is lost
        c.note_frame_queued(4_000, 100_000); // the hatch fired, we queued again
        assert_eq!(c.frame_dropped, 1, "the lost flip is a dropped frame");
        let _ = c.frame_presented(100_000 + 16_000); // pairs with the SECOND queue
        assert_eq!(c.frame_dropped, 1, "and the healthy flip after it is not blamed");
    }

    #[test]
    fn the_refresh_period_decides_what_dropped_means() {
        // A 120 Hz panel drops at 12.5 ms where a 60 Hz one is still inside its interval.
        let mut c = LatencyCore::new();
        c.set_refresh_mhz(120_000);
        assert_eq!(c.refresh_us, 8_333);
        c.note_frame_queued(1_000, 0);
        let _ = c.frame_presented(13_000);
        assert_eq!(c.frame_dropped, 1);
        let mut c = LatencyCore::new();
        c.set_refresh_mhz(0); // a bogus mode leaves the 60 Hz default
        assert_eq!(c.refresh_us, DEFAULT_REFRESH_US);
        c.note_frame_queued(1_000, 0);
        let _ = c.frame_presented(13_000);
        assert_eq!(c.frame_dropped, 0);
    }

    #[test]
    fn the_frame_consts_are_the_nfr_and_in_the_right_order() {
        // latency_budgets.json `_frame` is the declaration; the Python guard pins these
        // to it. This pins the shape: the p99 target sits inside a one-refresh budget.
        assert_eq!(FRAME_BUDGET_US, 16_600);
        assert_eq!(FRAME_P99_TARGET_US, 12_000);
        assert!(FRAME_P99_TARGET_US < FRAME_BUDGET_US);
        assert!(FRAME_BUDGET_US <= DEFAULT_REFRESH_US);
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
        const GAP: u64 = 1_000_000;
        let mut c = LatencyCore::new();
        let t = 2_000_000;
        c.note_input(Surface::Shell, Kind::Hover, t, t - GAP);
        // Renders happen and all report "nothing changed", so nothing ever
        // queues. This is the shape the report is gated on now: render passes,
        // not presented frames.
        for i in 0..STALL_REPORT_EVERY {
            c.note_render(true);
            assert!(c.frame_presented(t - GAP + i).is_empty());
        }
        let st = c.take_stall().expect("silence with input waiting must explain itself");
        assert_eq!(st.queued, 0, "zero queued frames IS the diagnosis");
        assert_eq!(st.attempted, STALL_REPORT_EVERY, "gated on render passes");
        assert_eq!(st.unchanged, STALL_REPORT_EVERY, "every pass said nothing changed");
        assert!(st.pending >= 1, "the unbound input is what makes it a stall");
        assert!(st.journal_line().contains("verdict=NO-SAMPLES"));
        assert!(
            st.journal_line().starts_with("hart-latency "),
            "one filter must catch the numbers, the drops and the silence"
        );
    }

    #[test]
    fn no_stall_is_reported_when_frames_are_binding() {
        // Frames queue AND samples resolve, so the instrument works end to end;
        // any silence after this is a real absence of interaction and must not
        // be blamed on the pipeline. Clocks paired the way a booted node pairs
        // them (kernel stamps since BOOT, Instant readings since COMPOSITOR
        // START) -- the old same-origin pairing recorded no offset at all, so
        // this test passed for the wrong reason.
        const GAP: u64 = 1_000_000;
        let mut c = LatencyCore::new();
        let t = 2_000_000; // kernel stamp, since boot
        c.note_input(Surface::Shell, Kind::Hover, t, t - GAP);
        c.frame_queued();
        // The flip that carried it, 8ms later, expressed in the Instant domain.
        let out = c.frame_presented(t - GAP + 8_000);
        assert!(!out.is_empty() || c.samples_recorded > 0,
                "the pairing must actually resolve a sample");
        for i in 0..STALL_REPORT_EVERY {
            let _ = c.frame_presented(t - GAP + 20_000 + i);
        }
        assert!(
            c.take_stall().is_none(),
            "a pipeline that binds and resolves must never be reported as stalled"
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
        const GAP: u64 = 1_000_000;
        let mut c = LatencyCore::new();
        let t = 2_000_000;
        c.note_input(Surface::Shell, Kind::Hover, t, t - GAP);
        for i in 0..STALL_REPORT_EVERY {
            c.note_render(true);
            let _ = c.frame_presented(t - GAP + i);
        }
        assert!(c.take_stall().is_some(), "first crossing reports");
        assert!(c.take_stall().is_none(), "and does not repeat until the next span");
        for i in 0..STALL_REPORT_EVERY {
            c.note_render(true);
            let _ = c.frame_presented(t - GAP + 10_000 + i);
        }
        assert!(c.take_stall().is_some(), "the next span reports again");
    }

    #[test]
    fn the_stall_is_reachable_with_nothing_ever_presented() {
        // THE REACHABILITY GUARD. Every other stall test drives take_stall()
        // directly, so all of them passed while the only production caller sat
        // inside the vblank handler: on hardware the line could not be reached
        // unless frames were being presented, which is the opposite of the
        // condition it reports. This test uses the shape of a box that renders
        // and never presents -- frames_presented stays 0 throughout.
        let mut c = LatencyCore::new();
        c.note_input(Surface::Shell, Kind::Press, 1_000_000, 0);
        for _ in 0..STALL_REPORT_EVERY {
            c.note_render(true);
        }
        let st = c
            .take_stall()
            .expect("a render loop that never presents must be able to say so");
        assert_eq!(st.presented, 0, "nothing was ever presented");
        assert_eq!(st.attempted, STALL_REPORT_EVERY, "the renders are what counted");
        assert_eq!(st.unchanged, STALL_REPORT_EVERY, "and all of them were no-ops");
        assert_eq!(st.samples, 0, "so no sample could resolve");

        // The structural half of the guard: `on_frame_presented` no longer
        // returns a Stall at all, so the presented path CANNOT be the emitter
        // again by accident. `on_render` is the only source, and it is called
        // from every arm of the render match including the failure arms.
    }

    #[test]
    fn a_render_loop_that_fails_every_tick_still_reports() {
        // udev.rs:1006's shape: render_frame refuses on every tick. The loop is
        // running at full speed, nothing reaches the screen, and before the
        // error arms started counting, `attempted` stayed 0 and the instrument
        // read this as an idle desk.
        let mut c = LatencyCore::new();
        c.note_input(Surface::Shell, Kind::Press, 1_000_000, 0);
        for _ in 0..STALL_REPORT_EVERY {
            c.note_render(false); // a failed attempt is not "unchanged"
        }
        let st = c.take_stall().expect("a failing render loop must report");
        assert_eq!(st.unchanged, 0, "nothing claimed the screen was static");
        assert_eq!(st.queued, 0, "and nothing ever reached queue_frame");
    }
}
