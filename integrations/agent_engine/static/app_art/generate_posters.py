#!/usr/bin/env python3
"""Generate the bundled, no-network brand-art posters for prebundled apps + agents.

This is the SINGLE SOURCE for the cinematic on-brand poster art the Netflix HOME
cards (Flagship agent row) and the desktop app icons render OFFLINE. Run it to
(re)emit the sibling ``*.svg`` files; those committed SVGs are the actual bundled
assets served at ``/shell/static/app_art/<name>.svg`` (Flask static_folder).

Why static + bundled (no network):
  - The home + desktop must look rich on a FRESH OFFLINE boot, before the
    continuous per-source sourcing (app_poster.py -> card.image_url -> W10
    ImageCache, #143/d8) has resolved any real web/marketplace artwork. The
    static pack is the floor; the network poster layers ON TOP only where no
    static ``image`` is set (hartHome.makeCard prefers card.image, then
    card.image_url). Decentralization/privacy lenses: zero egress to look good.

Design language (one template, varied per app so the grid reads as a spectrum):
  a deep near-black canvas + a brand-hue radial BLOOM (primary -> neighbour),
  faint concentric "hive" orbit rings + node dots, a white line-art emblem of
  the app/agent's FUNCTION (so it is recognizable, not a flat tile), and a
  bottom scrim so the client's text-over-art title always reads. 480x300 with a
  centered emblem so an object-fit:cover crop to either a wide home card or a
  square desktop icon still frames the emblem.

NO em dashes in any product-visible string (there is no visible text in the art;
the card/icon supplies the name).
"""
import os

OUT_DIR = os.path.dirname(os.path.abspath(__file__))

# Centered "hive" orbit rings + node dots (shared; primary-hued, faint).
_RINGS = (
    "<g fill='none' stroke='{prim}' stroke-opacity='.13'>"
    "<circle cx='240' cy='150' r='172'/><circle cx='240' cy='150' r='116'/></g>"
    "<g fill='{prim}' fill-opacity='.45'>"
    "<circle cx='240' cy='34' r='5'/><circle cx='356' cy='150' r='4'/>"
    "<circle cx='150' cy='206' r='4'/><circle cx='330' cy='84' r='3'/></g>"
)

# Function emblems (white line-art), each centered on (240,150).
EMBLEMS = {
    'research': (
        "<g fill='none' stroke='#EAF6FF' stroke-opacity='.85' stroke-width='9' stroke-linecap='round'>"
        "<circle cx='228' cy='140' r='40'/><line x1='258' y1='170' x2='294' y2='206'/></g>"
        "<g fill='#EAF6FF' fill-opacity='.7'><circle cx='208' cy='94' r='6'/>"
        "<circle cx='300' cy='116' r='5'/><circle cx='196' cy='192' r='5'/></g>"
    ),
    'trading': (
        "<g stroke='#EAF6FF' stroke-opacity='.8' stroke-width='4'>"
        "<line x1='196' y1='96' x2='196' y2='210'/><line x1='228' y1='110' x2='228' y2='200'/>"
        "<line x1='260' y1='84' x2='260' y2='196'/><line x1='292' y1='120' x2='292' y2='214'/></g>"
        "<g fill='#EAF6FF' fill-opacity='.85'>"
        "<rect x='186' y='120' width='20' height='56' rx='3'/><rect x='218' y='132' width='20' height='44' rx='3'/>"
        "<rect x='250' y='104' width='20' height='64' rx='3'/><rect x='282' y='150' width='20' height='40' rx='3'/></g>"
    ),
    'tutor': (
        "<polygon points='240,104 312,140 240,176 168,140' fill='#EAF6FF' fill-opacity='.85'/>"
        "<g fill='none' stroke='#EAF6FF' stroke-opacity='.85' stroke-width='8' stroke-linecap='round'>"
        "<path d='M196 154 V190 Q240 214 284 190 V154'/><line x1='312' y1='140' x2='312' y2='184'/></g>"
        "<circle cx='312' cy='190' r='6' fill='#EAF6FF'/>"
    ),
    'book': (
        "<g fill='none' stroke='#EAF6FF' stroke-opacity='.85' stroke-width='8' stroke-linejoin='round'>"
        "<path d='M240 110 Q206 96 176 104 V196 Q206 188 240 200 Z'/>"
        "<path d='M240 110 Q274 96 304 104 V196 Q274 188 240 200 Z'/></g>"
    ),
    'spoken': (
        "<circle cx='206' cy='150' r='20' fill='#EAF6FF' fill-opacity='.85'/>"
        "<g fill='none' stroke='#EAF6FF' stroke-opacity='.8' stroke-width='8' stroke-linecap='round'>"
        "<path d='M250 120 Q274 150 250 180'/><path d='M280 102 Q316 150 280 198'/></g>"
    ),
    'waveform': (
        "<g stroke='#EAF6FF' stroke-opacity='.85' stroke-width='10' stroke-linecap='round'>"
        "<line x1='180' y1='138' x2='180' y2='162'/><line x1='206' y1='116' x2='206' y2='184'/>"
        "<line x1='232' y1='94' x2='232' y2='206'/><line x1='258' y1='110' x2='258' y2='190'/>"
        "<line x1='284' y1='128' x2='284' y2='172'/><line x1='308' y1='120' x2='308' y2='180'/></g>"
    ),
    'store': (
        "<path d='M180 120 H300 L314 150 H166 Z' fill='#EAF6FF' fill-opacity='.85'/>"
        "<rect x='178' y='150' width='124' height='66' rx='4' fill='none' stroke='#EAF6FF' stroke-opacity='.85' stroke-width='8'/>"
        "<rect x='222' y='176' width='36' height='40' rx='3' fill='#EAF6FF' fill-opacity='.85'/>"
    ),
    'folder': (
        "<path d='M176 122 H216 L230 138 H306 V202 H176 Z' fill='#EAF6FF' fill-opacity='.85'/>"
    ),
    'terminal': (
        "<g fill='none' stroke='#EAF6FF' stroke-opacity='.85' stroke-width='10' stroke-linecap='round' stroke-linejoin='round'>"
        "<polyline points='186,116 224,150 186,184'/><line x1='238' y1='192' x2='302' y2='192'/></g>"
    ),
    'shield': (
        "<path d='M240 100 L296 122 V160 Q296 196 240 212 Q184 196 184 160 V122 Z' "
        "fill='#EAF6FF' fill-opacity='.18' stroke='#EAF6FF' stroke-opacity='.85' stroke-width='8' stroke-linejoin='round'/>"
        "<polyline points='216,154 234,172 268,132' fill='none' stroke='#EAF6FF' stroke-opacity='.9' "
        "stroke-width='9' stroke-linecap='round' stroke-linejoin='round'/>"
    ),
    'weather': (
        "<circle cx='288' cy='116' r='22' fill='#EAF6FF' fill-opacity='.85'/>"
        "<path d='M196 198 Q174 198 174 176 Q174 158 196 156 Q200 130 228 130 Q254 130 260 156 "
        "Q286 156 286 178 Q286 198 264 198 Z' fill='#EAF6FF' fill-opacity='.85'/>"
    ),
    'heart': (  # "Light your HART": a heart with spark rays
        "<path d='M240 202 C200 170 180 152 180 126 C180 108 196 96 214 96 C227 96 236 105 240 113 "
        "C244 105 253 96 266 96 C284 96 300 108 300 126 C300 152 280 170 240 202 Z' fill='#EAF6FF' fill-opacity='.9'/>"
        "<g stroke='#EAF6FF' stroke-opacity='.55' stroke-width='6' stroke-linecap='round'>"
        "<line x1='150' y1='150' x2='170' y2='150'/><line x1='310' y1='150' x2='330' y2='150'/>"
        "<line x1='240' y1='68' x2='240' y2='86'/></g>"
    ),
}

TEMPLATE = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 480 300' width='480' height='300' "
    "preserveAspectRatio='xMidYMid slice' role='img' aria-label='{label}'>"
    "<defs>"
    "<radialGradient id='b' cx='38%' cy='32%' r='82%'>"
    "<stop offset='0' stop-color='{prim}' stop-opacity='.55'/>"
    "<stop offset='52%' stop-color='{sec}' stop-opacity='.20'/>"
    "<stop offset='100%' stop-color='#06070D' stop-opacity='0'/></radialGradient>"
    "<linearGradient id='s' x1='0' y1='0' x2='0' y2='1'>"
    "<stop offset='.44' stop-color='#04050B' stop-opacity='0'/>"
    "<stop offset='1' stop-color='#04050B' stop-opacity='.80'/></linearGradient>"
    "</defs>"
    "<rect width='480' height='300' fill='#070812'/>"
    "<rect width='480' height='300' fill='url(#b)'/>"
    "{rings}{emblem}"
    "<rect width='480' height='300' fill='url(#s)'/>"
    "</svg>"
)

# filename -> (label, primary hue, secondary hue, emblem key). Brand spectrum:
# teal #00E6C3, cyan #29C5FF, blue #3B82F6, violet #9B5CFF, magenta #FF2E9A,
# amber #FFC83D, mint #34C759.
POSTERS = {
    # ── Flagship agents (hartHome.js samplePayload) ──
    'agent-auto-research':   ('Auto Research', '#29C5FF', '#3B82F6', 'research'),
    'agent-trading':         ('Trading',       '#34C759', '#00E6C3', 'trading'),
    'agent-tutor':           ('Tutor',         '#9B5CFF', '#3B82F6', 'tutor'),
    'agent-english-learning':('English Learning', '#FFC83D', '#FF2E9A', 'book'),
    'agent-spoken-english':  ('Spoken English', '#00E6C3', '#29C5FF', 'spoken'),
    'agent-speech-therapy':  ('Speech Therapy', '#FF2E9A', '#9B5CFF', 'waveform'),
    # ── Prebundled apps (shell_manifest.py 'image') ──
    'app-store':             ('App Store',     '#FFC83D', '#FF2E9A', 'store'),
    'app-files':             ('Files',         '#29C5FF', '#3B82F6', 'folder'),
    'app-terminal':          ('Terminal',      '#34C759', '#00E6C3', 'terminal'),
    'app-security':          ('Security',      '#34C759', '#00E6C3', 'shield'),
    'app-weather':           ('Weather',       '#29C5FF', '#3B82F6', 'weather'),
    'app-hart-setup':        ('Light your HART', '#00E6C3', '#9B5CFF', 'heart'),
}


def build_svg(label, prim, sec, emblem_key):
    rings = _RINGS.format(prim=prim)
    emblem = EMBLEMS[emblem_key]
    return TEMPLATE.format(label=label, prim=prim, sec=sec, rings=rings, emblem=emblem)


def main():
    for name, (label, prim, sec, emblem_key) in POSTERS.items():
        svg = build_svg(label, prim, sec, emblem_key)
        path = os.path.join(OUT_DIR, name + '.svg')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(svg + '\n')
        print('wrote', path, '(%d bytes)' % len(svg))
    print('done:', len(POSTERS), 'posters')


if __name__ == '__main__':
    main()
