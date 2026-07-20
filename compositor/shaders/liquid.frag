#version 100
// HART OS -- THE LIQUID SURFACE. Native desktop field, drawn by hart-comp.
//
// GLSL ES 1.00 (WebGL1-class) on purpose: smithay's GlesRenderer targets GLES2
// on the widest hardware, and the Intel HD 620 runs this at full rate. Nothing
// here needs GLES3 -- the whole field is analytic (no loops over data, no
// dependent texture reads, no derivatives beyond the implicit ones).
//
// WHY A SHADER AND NOT A WIDGET TREE (the program's core bet): every visual here
// is a CONTINUOUS FIELD evaluated per pixel per frame. That is what makes it
// "liquid": blobs MERGE (smooth-min) instead of overlapping like boxes, light
// BLEEDS instead of being a box-shadow, and the whole surface responds to live
// signal (voice energy, mood, pointer) by reading a uniform -- never by starting
// an animation and easing toward a target. That distinction is the entire reason
// the CSS orb drag rubber-banded and this cannot.
//
// EVERYTHING IS AGENT-STEERABLE. Each uniform below is a knob the local LLM
// writes through A2UI: palette, orb pose, breath rate, ring geometry, mood mix,
// warp amount. New looks are DATA, not a rebuild.
//
// COST (the binding NFR: 60fps on HD 620 @1366x768): ~5 value-noise taps for the
// warped aurora + a handful of length()/smoothstep() ops. No branches on varying
// data, no loops with unbounded trip counts. Budget measured, not assumed --
// M0 records fps + frame time in the journal.

precision highp float;

// ── Agent-steerable uniform block ───────────────────────────────────────────
uniform vec2  u_res;        // output size in px
uniform float u_time;       // seconds; FROZEN when reduced-motion is on
uniform float u_energy;     // 0..1 live voice energy (mic RMS / TTS envelope)
uniform vec3  u_base;       // deep background (aura #04050B)
uniform vec3  u_amb0;       // violet lead
uniform vec3  u_amb1;       // cyan
uniform vec3  u_amb2;       // pink
uniform vec3  u_amb3;       // amber
uniform vec4  u_orb;        // xy centre (0..1), z radius (frac of min edge), w breathe Hz
uniform vec4  u_ring;       // x radius scale, y thickness px, z dash count, w spin turns/s
uniform vec4  u_mood;       // x listening, y thinking, z speaking, w warp amount
uniform float u_alive;      // 0 = static composition (reduced motion / calm floor)

// ── value noise + fbm (the organic layer) ───────────────────────────────────
// Without this the aurora is just radial blobs, which reads synthetic. Domain
// warping is what makes it look like light moving through medium.
float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}

float vnoise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);      // smoothstep interpolation
    float a = hash(i);
    float b = hash(i + vec2(1.0, 0.0));
    float c = hash(i + vec2(0.0, 1.0));
    float d = hash(i + vec2(1.0, 1.0));
    return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

// 3 octaves is the sweet spot: enough structure to read organic, cheap enough
// to stay inside the frame budget on an iGPU.
float fbm(vec2 p) {
    float v = 0.0;
    float amp = 0.5;
    v += amp * vnoise(p);        p *= 2.02; amp *= 0.5;
    v += amp * vnoise(p);        p *= 2.03; amp *= 0.5;
    v += amp * vnoise(p);
    return v;
}

// ── sdf ops ─────────────────────────────────────────────────────────────────
// Polynomial smooth minimum: THE liquid operator. Fields merge like fluid.
float smin(float a, float b, float k) {
    float h = clamp(0.5 + 0.5 * (b - a) / max(k, 1e-4), 0.0, 1.0);
    return mix(b, a, h) - k * h * (1.0 - h);
}

// Soft radial energy for the aurora blooms.
float bloom(vec2 p, vec2 c, float r) {
    float t = clamp(1.0 - length(p - c) / max(r, 1e-4), 0.0, 1.0);
    return t * t;
}

void main() {
    vec2 frag = gl_FragCoord.xy;
    vec2 uv = frag / u_res;
    // Aspect-correct so circles are circles.
    float aspect = u_res.x / max(u_res.y, 1.0);
    vec2 p = vec2((uv.x - 0.5) * aspect + 0.5, 1.0 - uv.y);

    float t = u_time * u_alive;   // u_alive = 0 freezes every clock (a11y)

    // ── DOMAIN WARP ─────────────────────────────────────────────────────────
    // Push sample coordinates through low-frequency noise so the blooms flow
    // and fold instead of sliding rigidly. This single step is the difference
    // between "gradient wallpaper" and "aurora".
    float warpAmt = u_mood.w;
    vec2 q = vec2(fbm(p * 2.1 + vec2(0.0, t * 0.03)),
                  fbm(p * 2.1 + vec2(5.2, t * 0.026) + 1.7));
    vec2 pw = p + (q - 0.5) * warpAmt;

    // ── AURORA FIELD ────────────────────────────────────────────────────────
    // Four palette blooms on slow independent drifts (never a visible loop).
    vec2 c0 = vec2(0.32 * aspect + (1.0 - aspect) * 0.5, 0.40)
            + vec2(sin(t * 0.037), cos(t * 0.041)) * 0.045;
    vec2 c1 = vec2(0.84 * aspect + (1.0 - aspect) * 0.5, 0.24)
            + vec2(cos(t * 0.029), sin(t * 0.033)) * 0.040;
    vec2 c2 = vec2(0.20 * aspect + (1.0 - aspect) * 0.5, 0.84)
            + vec2(sin(t * 0.023), cos(t * 0.031)) * 0.038;
    vec2 c3 = vec2(0.86 * aspect + (1.0 - aspect) * 0.5, 0.84)
            + vec2(cos(t * 0.043), sin(t * 0.027)) * 0.030;

    vec3 col = u_base;
    col += u_amb0 * bloom(pw, c0, 0.42) * 0.42;
    col += u_amb1 * bloom(pw, c1, 0.34) * 0.30;
    col += u_amb2 * bloom(pw, c2, 0.34) * 0.22;
    col += u_amb3 * bloom(pw, c3, 0.25) * 0.18;

    // Filament detail: a faint high-frequency ridge riding the warp, so the
    // field has texture up close instead of going flat under inspection.
    float fil = fbm(pw * 6.0 + vec2(t * 0.05, -t * 0.04));
    col += mix(u_amb0, u_amb1, 0.5) * pow(fil, 3.0) * 0.05;

    // Mood tint: listening leans cyan, thinking violet, speaking warm.
    col += (u_amb1 * u_mood.x + u_amb0 * u_mood.y + u_amb3 * u_mood.z) * 0.06;

    // ── THE ORB ─────────────────────────────────────────────────────────────
    vec2 oc = vec2((u_orb.x - 0.5) * aspect + 0.5, u_orb.y);
    vec2 d  = p - oc;

    // Breathing is intrinsic to the orb (never gated on a GPU tier -- the
    // software floor breathes too, which is the FEEL-alive pillar).
    float breathe = sin(t * 6.28318 * u_orb.w) * 0.5 + 0.5;
    float r = u_orb.z * (1.0 + 0.045 * breathe + 0.16 * u_energy);

    // Metaball body: the core disc smooth-min'd with an energy-driven satellite,
    // so at voice peaks the orb SWELLS and reabsorbs rather than merely scaling.
    float body = smin(length(d) - r,
                      length(d - vec2(0.0, r * 0.12 * u_energy)) - r * 0.92,
                      r * 0.55);

    // Inner core: bright, fast falloff.
    float core = clamp(1.0 - abs(body) / max(r * 0.75, 1e-4), 0.0, 1.0);
    col += u_amb0 * pow(core, 3.0) * (0.55 + 0.45 * u_energy);

    // Iridescent fresnel rim: hue shifts across the edge (violet -> cyan), which
    // is what sells "glass" rather than "flat disc".
    float rim = exp(-abs(body) * (34.0 / max(r, 1e-4)));
    float ang = atan(d.y, d.x);
    vec3 irid = mix(u_amb0, u_amb1, 0.5 + 0.5 * sin(ang * 2.0 + t * 0.5));
    col += irid * rim * (0.30 + 0.25 * u_energy);

    // Outer halo: the orb's presence in the room.
    float halo = exp(-max(body, 0.0) * (13.0 / max(r, 1e-4)));
    col += mix(u_amb0, u_amb1, 0.35) * halo * (0.32 + 0.28 * u_energy);

    // ── ORBITAL DASHED RINGS ────────────────────────────────────────────────
    // Pure field math: dashes from the polar angle, spin from time. No geometry.
    float thick = u_ring.y / max(u_res.y, 1.0);
    float spin  = t * u_ring.w * 6.28318;
    float dashN = max(u_ring.z, 1.0);

    float ringR = r * u_ring.x;
    float dr    = abs(length(d) - ringR);
    float dash  = smoothstep(0.35, 0.5,
                    abs(fract((ang + spin) * dashN / 6.28318) - 0.5));
    col += u_amb1 * (1.0 - smoothstep(0.0, thick, dr)) * dash * 0.55;

    // Counter-rotating inner ring for depth.
    float ringR2 = ringR * 0.78;
    float dr2    = abs(length(d) - ringR2);
    float dash2  = smoothstep(0.35, 0.5,
                     abs(fract((ang - spin * 0.62) * (dashN * 0.75) / 6.28318) - 0.5));
    col += u_amb0 * (1.0 - smoothstep(0.0, thick * 0.7, dr2)) * dash2 * 0.32;

    // ── FINISH ──────────────────────────────────────────────────────────────
    // Vignette for depth.
    float vig = 1.0 - 0.35 * pow(length((uv - 0.5) * vec2(1.0, 1.15)) * 1.35, 2.0);
    col *= clamp(vig, 0.0, 1.0);

    // Dither: kills 8-bit banding across the huge smooth gradients. Banding is
    // the single clearest tell between "cheap" and "premium" on a dark desktop.
    col += (hash(uv * u_res) - 0.5) * (1.0 / 255.0);

    gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
