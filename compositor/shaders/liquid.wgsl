// HART OS -- the LIQUID surface. M0 spike of the NATIVE SHELL PARITY PROGRAM.
//
// This is the desktop backdrop + orb drawn as a SIGNED DISTANCE FIELD, natively,
// by hart-comp. It replaces the flat splash clear (and, on the GPU path, the
// compose-once CPU bloom in bloom.rs -- a field this cheap is free per frame, so
// the aurora can actually DRIFT instead of being a baked still).
//
// WHY SDF (steward 2026-07-20: "futuristic liquid agentic ui ... create native
// components on the fly"): a retained widget tree can only draw what was
// compiled in. An SDF expression is DATA -- the agent composes primitives and
// operators, we codegen WGSL and hot-compile it. "Liquid" is literally this
// medium: smooth-min merges blobs like fluid, and glow/refraction fall out of the
// distance field for free.
//
// EVERYTHING HERE IS AGENT-STEERABLE via the uniform block: palette, orb
// position/size, breathe rate, energy (live mic RMS), ring geometry/spin, and the
// listening/thinking/speaking mood mix. The agent changes UI by writing DATA, and
// motion is steerable mid-flight (no recompile, no restart).
//
// PERF CONTRACT (the program's binding NFRs): every pixel costs a fixed, small
// number of smoothstep/length ops -- no loops over unbounded data, no dependent
// texture reads, no per-frame CPU work. Budgeted for 60fps at 1366x768 on the
// Intel HD 620. Reduced-motion (mood.w) freezes all clocks so a11y users get the
// exact same composition, static.

struct Uniforms {
    // xy = output resolution in px; z = seconds since start; w = live energy 0..1
    // (mic RMS while listening / TTS envelope while speaking) -- the orb reacts.
    res_time_energy: vec4<f32>,
    // Deep background base colour (rgb; a unused). aura: #04050B.
    base: vec4<f32>,
    // The four ambient hues (rgb) + per-hue intensity (a). Straight from the
    // SAME conky-themes JSON the HTML shell reads -- one palette source.
    amb0: vec4<f32>,
    amb1: vec4<f32>,
    amb2: vec4<f32>,
    amb3: vec4<f32>,
    // Orb: xy = centre (0..1 of the output), z = radius (fraction of min edge),
    // w = breathe frequency in Hz (0 disables the breath).
    orb: vec4<f32>,
    // Rings: x = radius scale vs the orb, y = thickness px, z = dash count,
    // w = spin rate (turns/sec, sign selects direction).
    ring: vec4<f32>,
    // Mood mix: x = listening, y = thinking, z = speaking, w = reduced_motion
    // (1.0 freezes every clock -- the a11y contract).
    mood: vec4<f32>,
};

@group(0) @binding(0) var<uniform> U: Uniforms;

// ---------------------------------------------------------------- vertex ----
// Fullscreen triangle: 3 verts, no vertex buffer, no index buffer. Cheapest
// possible way to run a fragment over the whole output.
struct VsOut {
    @builtin(position) pos: vec4<f32>,
    @location(0) uv: vec2<f32>,
};

@vertex
fn vs_main(@builtin(vertex_index) vi: u32) -> VsOut {
    // (-1,-1), (3,-1), (-1,3) -- covers the viewport with one triangle.
    let x = f32(i32(vi) / 2) * 4.0 - 1.0;
    let y = f32(i32(vi) & 1) * 4.0 - 1.0;
    var out: VsOut;
    out.pos = vec4<f32>(x, y, 0.0, 1.0);
    // uv in 0..1 with y down, matching the compositor's surface convention.
    out.uv = vec2<f32>((x + 1.0) * 0.5, 1.0 - (y + 1.0) * 0.5);
    return out;
}

// -------------------------------------------------------------- sdf tools ---
// Polynomial smooth minimum: THE liquid operator. Two fields merge like fluid
// instead of intersecting like glass, with a controllable blend radius k.
fn smin(a: f32, b: f32, k: f32) -> f32 {
    let h = clamp(0.5 + 0.5 * (b - a) / max(k, 1e-4), 0.0, 1.0);
    return mix(b, a, h) - k * h * (1.0 - h);
}

fn sd_circle(p: vec2<f32>, r: f32) -> f32 {
    return length(p) - r;
}

// Soft radial falloff used for the aurora blooms. Squared smoothstep gives a
// gaussian-looking shoulder for two multiplies -- the whole reason this is free.
fn bloom_at(p: vec2<f32>, c: vec2<f32>, r: f32) -> f32 {
    let d = length(p - c) / max(r, 1e-4);
    let t = clamp(1.0 - d, 0.0, 1.0);
    return t * t;
}

// Cheap value noise (hash-based) -- used only for a subtle grain so flat regions
// do not band on 8-bit output. No texture fetch.
fn hash21(p: vec2<f32>) -> f32 {
    var h = fract(p * vec2<f32>(0.1031, 0.1030));
    h += dot(h, h.yx + 33.33);
    return fract((h.x + h.y) * h.x);
}

// ------------------------------------------------------------- fragment -----
@fragment
fn fs_main(in: VsOut) -> @location(0) vec4<f32> {
    let res = U.res_time_energy.xy;
    // Reduced motion (a11y): freeze every clock. Composition is identical, still.
    let t = select(U.res_time_energy.z, 0.0, U.mood.w > 0.5);
    let energy = clamp(U.res_time_energy.w, 0.0, 1.0);

    // Aspect-correct space: x scaled by aspect so circles are round, not ovals.
    let aspect = res.x / max(res.y, 1.0);
    var p = in.uv;
    p.x = (p.x - 0.5) * aspect + 0.5;

    // ---- AURORA FIELD ----------------------------------------------------
    // Four drifting blooms echoing the Aura mock: violet lead, cyan upper-right,
    // pink lower-left, amber accent. Each drifts on its own slow lissajous so the
    // field breathes without ever repeating on a visible cycle.
    let d0 = vec2<f32>(sin(t * 0.037), cos(t * 0.041)) * 0.045;
    let d1 = vec2<f32>(cos(t * 0.029), sin(t * 0.033)) * 0.040;
    let d2 = vec2<f32>(sin(t * 0.023), cos(t * 0.031)) * 0.038;
    let d3 = vec2<f32>(cos(t * 0.043), sin(t * 0.027)) * 0.030;

    let c0 = vec2<f32>(0.32 * aspect + (1.0 - aspect) * 0.5, 0.40) + d0;
    let c1 = vec2<f32>(0.84 * aspect + (1.0 - aspect) * 0.5, 0.24) + d1;
    let c2 = vec2<f32>(0.20 * aspect + (1.0 - aspect) * 0.5, 0.84) + d2;
    let c3 = vec2<f32>(0.86 * aspect + (1.0 - aspect) * 0.5, 0.84) + d3;

    var col = U.base.rgb;
    col += U.amb0.rgb * bloom_at(p, c0, 0.42) * U.amb0.a;
    col += U.amb1.rgb * bloom_at(p, c1, 0.34) * U.amb1.a;
    col += U.amb2.rgb * bloom_at(p, c2, 0.34) * U.amb2.a;
    col += U.amb3.rgb * bloom_at(p, c3, 0.25) * U.amb3.a;

    // Mood tint: listening leans cyan, thinking violet, speaking warm -- the same
    // semantic the HTML shell expresses with html[data-voice] filters.
    let mood_tint = U.amb1.rgb * U.mood.x + U.amb0.rgb * U.mood.y + U.amb3.rgb * U.mood.z;
    col += mood_tint * 0.06;

    // ---- THE ORB ---------------------------------------------------------
    // Centre in aspect-corrected space; radius as a fraction of the short edge.
    var oc = U.orb.xy;
    oc.x = (oc.x - 0.5) * aspect + 0.5;
    // Breathing: a slow scale on the radius, amplitude lifted by live energy so
    // the orb visibly inhales when it hears you. Frequency is agent-set (Hz).
    let breathe = sin(t * 6.28318 * U.orb.w) * 0.5 + 0.5;
    let r = U.orb.z * (1.0 + 0.045 * breathe + 0.16 * energy);

    let q = p - oc;
    // The orb body is a circle SMIN'd with a slight energy-driven bulge, so at
    // high energy it reads as liquid swelling rather than a scaling disc.
    let body = smin(
        sd_circle(q, r),
        sd_circle(q - vec2<f32>(0.0, r * 0.10 * energy), r * 0.92),
        r * 0.55
    );

    // Core: a bright centre that falls off fast.
    let core = clamp(1.0 - abs(body) / max(r * 0.75, 1e-4), 0.0, 1.0);
    col += U.amb0.rgb * pow(core, 3.0) * (0.55 + 0.45 * energy);

    // Halo: wide soft glow OUTSIDE the body -- the orb's presence in the room.
    let halo = exp(-max(body, 0.0) * (14.0 / max(r, 1e-4)));
    col += mix(U.amb0.rgb, U.amb1.rgb, 0.35) * halo * (0.35 + 0.30 * energy);

    // ---- ORBITAL DASHED RINGS -------------------------------------------
    // The mock's signature. Dashes come from the polar angle, spin from time, so
    // there is no geometry to update -- pure field math.
    let ring_r = r * U.ring.x;
    let dist_ring = abs(length(q) - ring_r);
    let ang = atan2(q.y, q.x);
    let spin = t * U.ring.w * 6.28318;
    let dashes = max(U.ring.z, 1.0);
    // Duty cycle 0.5 -> even dash/gap; smoothstep keeps the edges anti-aliased.
    let dash = smoothstep(0.35, 0.5, abs(fract((ang + spin) * dashes / 6.28318) - 0.5));
    // Thickness in px converted to our normalized space.
    let thick = U.ring.y / max(res.y, 1.0);
    let ring_mask = (1.0 - smoothstep(0.0, thick, dist_ring)) * dash;
    col += U.amb1.rgb * ring_mask * 0.55;

    // Second, thinner counter-rotating ring for depth (the mock layers rings).
    let ring2_r = ring_r * 0.78;
    let dist_ring2 = abs(length(q) - ring2_r);
    let dash2 = smoothstep(0.35, 0.5, abs(fract((ang - spin * 0.62) * (dashes * 0.75) / 6.28318) - 0.5));
    let ring2_mask = (1.0 - smoothstep(0.0, thick * 0.7, dist_ring2)) * dash2;
    col += U.amb0.rgb * ring2_mask * 0.32;

    // ---- FINISH ----------------------------------------------------------
    // Vignette so the field reads as depth, not a flat wash.
    let vig = 1.0 - 0.35 * pow(length((in.uv - 0.5) * vec2<f32>(1.0, 1.15)) * 1.35, 2.0);
    col *= clamp(vig, 0.0, 1.0);

    // Dither: +-1/255 of hash noise kills 8-bit banding across the huge smooth
    // gradients (banding is the tell that separates "cheap" from "premium").
    let grain = (hash21(in.uv * res) - 0.5) * (1.0 / 255.0);
    col += vec3<f32>(grain);

    return vec4<f32>(clamp(col, vec3<f32>(0.0), vec3<f32>(1.0)), 1.0);
}
