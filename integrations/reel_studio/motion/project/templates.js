// Reel Studio's four animated overlay templates.  HARTOS, Apache-2.0;
// written for HARTOS, no third-party template code.
//
// Every composition is transparent outside what it draws (render.js renders
// ProRes 4444 with alpha), and takes its size and its length from its props
// (calculateMetadata), so the Python layout (integrations/reel_studio/motion/
// layout.py) decides where each one sits on the 1080x1920 reel and for how
// long.  Everything is a function of the frame number: no clocks, no random
// numbers, so the same props always render the same frames.
//
//   Title      a kinetic title: its words spring in one after another on a
//              soft dark plate, then fade together.
//   EndCard    a booking/price card that rises in: the first line large
//              (her service), her price -- the row priceRow names, none when
//              it is -1 -- in the accent colour, the rest smaller (how to
//              book).
//   LogoSting  her logo pops in with a ring of the accent colour behind it
//              and a light sweep across it.
//   Wipe       her "before" frame covers the picture and a glowing line
//              wipes it away left to right, uncovering the reel's reveal.
const React = require('react');
const {
  AbsoluteFill, Composition, Easing, Img, interpolate, spring, useCurrentFrame, useVideoConfig,
} = require('remotion');

const h = React.createElement;
// The Windows font with Tamil, Devanagari and Gurmukhi first, then common
// sans fonts; the browser falls back per character.
const FONT = "'Nirmala UI', 'Noto Sans', 'Noto Sans Tamil', 'Segoe UI', Roboto, Arial, sans-serif";
const CLAMP = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'};

function fadeOut(frame, total, fps) {
  const len = Math.max(1, Math.round(fps * 0.3));
  return interpolate(frame, [total - len, total - 1], [1, 0], CLAMP);
}

function Title({text, accent}) {
  const frame = useCurrentFrame();
  const {fps, width, height, durationInFrames} = useVideoConfig();
  const words = String(text || '').split(/\s+/).filter(Boolean);
  const longest = words.reduce((n, w) => Math.max(n, w.length), 1);
  const chars = Math.max(6, Math.min(String(text || '').length, 22));
  const size = Math.round(Math.min(height * 0.3, (width * 0.86) / (Math.max(chars / 1.6, longest) * 0.58)));
  const plate = spring({frame, fps, config: {damping: 18, stiffness: 140}});
  return h(AbsoluteFill, {style: {alignItems: 'center', justifyContent: 'center',
                                  opacity: fadeOut(frame, durationInFrames, fps)}},
    h('div', {style: {
      display: 'flex', flexWrap: 'wrap', justifyContent: 'center', alignItems: 'center',
      columnGap: Math.round(size * 0.28), rowGap: Math.round(size * 0.08),
      maxWidth: Math.round(width * 0.94), padding: `${Math.round(size * 0.22)}px ${Math.round(size * 0.4)}px`,
      borderRadius: Math.round(size * 0.35), background: 'rgba(0, 0, 0, 0.42)',
      transform: `scale(${0.9 + 0.1 * plate})`, opacity: plate,
      borderBottom: `${Math.max(3, Math.round(size * 0.06))}px solid ${accent}`,
    }},
      words.map((word, i) => {
        const s = spring({frame: frame - i * Math.round(fps * 0.12), fps,
                          config: {damping: 11, stiffness: 190}});
        return h('span', {key: i, style: {
          display: 'inline-block', color: '#FFFFFF', fontFamily: FONT, fontWeight: 800,
          fontSize: size, lineHeight: 1.12, opacity: Math.min(1, s * 1.4),
          transform: `translateY(${Math.round((1 - s) * size * 0.6)}px) scale(${0.55 + 0.45 * s})`,
          textShadow: '0 4px 18px rgba(0, 0, 0, 0.55)',
        }}, word);
      })));
}

function EndCard({lines, priceRow, accent}) {
  const frame = useCurrentFrame();
  const {fps, width, height, durationInFrames} = useVideoConfig();
  const rows = (Array.isArray(lines) ? lines : []).map((l) => String(l)).filter(Boolean);
  const price = Number.isInteger(priceRow) ? priceRow : -1;
  const rise = spring({frame, fps, config: {damping: 16, stiffness: 120}});
  const base = Math.round(Math.min(height / (rows.length * 1.9 + 0.6), width / 13));
  return h(AbsoluteFill, {style: {alignItems: 'center', justifyContent: 'center',
                                  opacity: fadeOut(frame, durationInFrames, fps)}},
    h('div', {style: {
      width: Math.round(width * 0.96), padding: `${Math.round(base * 0.45)}px ${Math.round(base * 0.6)}px`,
      boxSizing: 'border-box', borderRadius: Math.round(base * 0.5),
      background: 'rgba(255, 255, 255, 0.93)', boxShadow: '0 10px 40px rgba(0, 0, 0, 0.35)',
      borderTop: `${Math.max(4, Math.round(base * 0.12))}px solid ${accent}`,
      transform: `translateY(${Math.round((1 - rise) * height * 0.6)}px)`, opacity: rise,
      display: 'flex', flexDirection: 'column', alignItems: 'center', textAlign: 'center',
    }},
      rows.map((row, i) => {
        const s = spring({frame: frame - Math.round(fps * (0.18 + 0.14 * i)), fps,
                          config: {damping: 14, stiffness: 160}});
        const isPrice = i === price;
        const size = i === 0 ? base * 1.15 : (isPrice ? base * 1.05 : base * 0.8);
        return h('div', {key: i, style: {
          fontFamily: FONT, fontWeight: i === 0 || isPrice ? 800 : 600, fontSize: Math.round(size),
          lineHeight: 1.18, color: isPrice ? accent : '#1D1D1F',
          textShadow: isPrice ? '0 1px 0 rgba(0, 0, 0, 0.25)' : 'none',
          opacity: s, transform: `translateY(${Math.round((1 - s) * base * 0.5)}px)`,
        }}, row);
      })));
}

function LogoSting({logo, accent}) {
  const frame = useCurrentFrame();
  const {fps, width, height, durationInFrames} = useVideoConfig();
  const side = Math.min(width, height);
  const pop = spring({frame, fps, config: {damping: 9, stiffness: 150, mass: 0.8}});
  const ring = interpolate(frame, [0, Math.round(fps * 0.8)], [0.3, 1.25], {...CLAMP, easing: Easing.out(Easing.cubic)});
  const ringFade = interpolate(frame, [Math.round(fps * 0.2), Math.round(fps * 0.9)], [0.9, 0], CLAMP);
  const sweep = interpolate(frame, [Math.round(fps * 0.35), Math.round(fps * 1.0)], [-0.6, 1.6], CLAMP);
  return h(AbsoluteFill, {style: {alignItems: 'center', justifyContent: 'center',
                                  opacity: fadeOut(frame, durationInFrames, fps)}},
    h('div', {style: {
      position: 'absolute', width: Math.round(side * 0.9), height: Math.round(side * 0.9),
      borderRadius: '50%', border: `${Math.max(3, Math.round(side * 0.03))}px solid ${accent}`,
      transform: `scale(${ring})`, opacity: ringFade,
    }}),
    h('div', {style: {
      position: 'relative', width: Math.round(side * 0.72), height: Math.round(side * 0.72),
      transform: `scale(${0.5 + 0.5 * pop}) rotate(${Math.round((1 - pop) * -12)}deg)`,
      opacity: Math.min(1, pop * 1.5), overflow: 'hidden',
    }},
      logo ? h(Img, {src: logo, style: {width: '100%', height: '100%', objectFit: 'contain'}}) : null,
      h('div', {style: {
        position: 'absolute', top: 0, bottom: 0, width: '35%',
        left: `${Math.round(sweep * 100)}%`, transform: 'skewX(-18deg)',
        background: 'linear-gradient(90deg, rgba(255,255,255,0), rgba(255,255,255,0.55), rgba(255,255,255,0))',
        mixBlendMode: 'screen',
      }})));
}

function Wipe({before, accent}) {
  const frame = useCurrentFrame();
  const {width, durationInFrames} = useVideoConfig();
  const at = interpolate(frame, [0, Math.max(1, durationInFrames - 1)], [0, 100],
                         {...CLAMP, easing: Easing.inOut(Easing.cubic)});
  const line = Math.max(6, Math.round(width * 0.008));
  return h(AbsoluteFill, null,
    before ? h(Img, {src: before, style: {
      position: 'absolute', width: '100%', height: '100%', objectFit: 'cover',
      clipPath: `inset(0 0 0 ${at}%)`,
    }}) : null,
    at < 100 ? h('div', {style: {
      position: 'absolute', top: 0, bottom: 0, left: `calc(${at}% - ${Math.round(line / 2)}px)`,
      width: line, background: '#FFFFFF',
      boxShadow: `0 0 ${line * 3}px ${line}px ${accent}`,
    }}) : null);
}

// Size and length come from the props; the defaults only let the Studio open them.
const metadata = ({props}) => ({
  width: props.width, height: props.height, fps: props.fps,
  durationInFrames: props.durationInFrames,
});
const SIZE = {width: 770, height: 300, fps: 30, durationInFrames: 75, accent: '#E8A0B4'};
const TEMPLATES = [
  ['Title', Title, {...SIZE, text: 'Bridal glow'}],
  ['EndCard', EndCard, {...SIZE, height: 360, lines: ['Bridal makeup', 'From 15,000', 'DM to book'],
                        priceRow: 1}],
  ['LogoSting', LogoSting, {...SIZE, width: 300, logo: ''}],
  ['Wipe', Wipe, {...SIZE, width: 1080, height: 1920, before: ''}],
];

function Root() {
  return h(React.Fragment, null, TEMPLATES.map(([id, component, defaults]) => h(Composition, {
    key: id, id, component, defaultProps: defaults, calculateMetadata: metadata,
    width: defaults.width, height: defaults.height, fps: defaults.fps,
    durationInFrames: defaults.durationInFrames,
  })));
}

module.exports = {Root};
