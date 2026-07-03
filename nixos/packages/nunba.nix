{ lib, pkgs
, hartSrc ? null   # Accepted only for call-site compatibility: hart-liquid-ui.nix
                   # and hart-nunba.nix both `callPackage` this file with
                   # `{ inherit hartSrc; }`, and callPackage passes the override
                   # through verbatim, so the formal MUST exist or eval fails with
                   # "called with unexpected argument 'hartSrc'". The native static
                   # dist no longer needs the HART repo tree.
, nunbaRev ? "72780cd43fd274057251e5e594f7d949a29e2237"
, nunbaHash ? lib.fakeHash
, npmDepsHash ? lib.fakeHash
, backendUrl ? "http://localhost:6777"
}:

# ═══════════════════════════════════════════════════════════════
# Nunba — HART OS desktop React UI, built NATIVELY into a static dist
# ═══════════════════════════════════════════════════════════════
#
# This derivation REPLACES the old runtime-AppImage-download launcher stub.
# That stub shipped `$out/bin/nunba` which, on first run, pulled a ~200 MB
# `Nunba-x86_64.AppImage` from GitHub releases and ran `nunba --server-only`
# on :5000 — a SECOND, redundant copy of the UI living outside the OS closure,
# fetched over the network, that drifted from the React source and from the
# HART backend it was supposed to talk to. The native-dist path
# (hart-liquid-ui.nix `NUNBA_STATIC_DIR`) was already wired but DEAD because
# this file never actually produced `$out/lib/nunba/static`.
#
# Now there is ONE path: the React SPA (the landing-page CRA tree in the
# hertz-ai/Nunba repo) is compiled here into `$out/lib/nunba/static`, and
# LiquidUIService (integrations/agent_engine/liquid_ui_service.py) serves it
# from inside the glass shell via `NUNBA_STATIC_DIR`. No AppImage, no
# :5000 daemon, no runtime download.
#
# Source: hertz-ai/Nunba @ ${nunbaRev}, subdir `landing-page/`
#   - CRA via react-app-rewired (config-overrides.js) -> ./build (NOT ./dist)
#   - package-lock.json is committed, lockfileVersion 3 -> deterministic
#     fetchNpmDeps (buildNpmPackage). The committed lock + the pinned rev are
#     bumped together; `npmDepsHash` is re-pinned in the SAME commit that bumps
#     `nunbaRev` (otherwise `npm ci` fails the lock-vs-deps integrity check).
#
# ── Pinning the two fixed-output hashes (steward / CI, once per source bump) ──
#   nunbaHash   (the repo tree):
#     nix run nixpkgs#nix-prefetch-github -- hertz-ai Nunba --rev ${nunbaRev}
#     (or seed lib.fakeHash and copy the "got: sha256-..." from the first build)
#   npmDepsHash (the npm dependency closure):
#     nix run nixpkgs#prefetch-npm-deps -- path/to/landing-page/package-lock.json
#   Both default to lib.fakeHash so the flake still EVALUATES (FOD hashes are
#   only checked at realise time); the first real `nix build .#iso-desktop` is
#   what surfaces the correct values. NOTE: the desktop ISO closure already sits
#   at the size/build-time ceiling (see desktop.nix isoImage + the hart.comp
#   build-hang history) and this is a heavy webpack build (MUI, phaser, pdfjs,
#   livekit, leaflet, chart.js). RECOMMENDED follow-up for the flake.nix owner:
#   expose this as `packages.nunba-static` so a dedicated CI job builds + caches
#   it ONCE (full cores) and the ISO closure substitutes the cached store path,
#   the same mitigation used for the hart.comp Rust crate.

let
  nunbaSrc = pkgs.fetchFromGitHub {
    owner = "hertz-ai";
    repo = "Nunba";
    rev = nunbaRev;
    hash = nunbaHash;
  };
in
pkgs.buildNpmPackage {
  pname = "nunba-static";
  version = "1.0.0";

  src = nunbaSrc;
  # package.json + package-lock.json live in the `landing-page/` subdir of the
  # repo. fetchFromGitHub unpacks the tree under `${name}` (default "source"),
  # so point the npm build at the CRA root inside it.
  sourceRoot = "${nunbaSrc.name}/landing-page";

  inherit npmDepsHash;

  # `canvas` is an OPTIONAL dependency (lockfile `optional: true`) that needs
  # cairo/pango + node-gyp to compile; react-pdf renders fine without it (the
  # repo's own committed build proves the SPA builds without canvas). Omit
  # optional native deps so the sandboxed build needs NO C toolchain and never
  # tries (and fails) to gyp-build canvas — including the buildNpmPackage rebuild
  # hook, which would otherwise hard-fail on canvas. `npm_config_omit` is honored
  # by EVERY npm invocation (ci + rebuild) regardless of the wrapper's flag attr,
  # so it is the actual guarantee; npmInstallFlags states the same intent.
  # Both only affect install/rebuild — fetchNpmDeps still prefetches the FULL
  # lock (it is a separate derivation), so npmDepsHash is unaffected.
  npm_config_omit = "optional";
  npmInstallFlags = [ "--omit=optional" ];

  # Defensive: covers any non-optional dep whose install script shells out to
  # node-gyp/python. The standard CRA toolchain (react-scripts 5.0.1, webpack 5,
  # Terser, dart-sass) is pure-JS and needs none of this on its own.
  nativeBuildInputs = [ pkgs.python3 ];

  # buildNpmPackage default npmBuildScript = "build" -> `npm run build`:
  #   prebuild (scripts/setup-env.sh) -> with .env.local/.env.production.enc
  #     gitignored + absent, hits the copy-.env.example branch and exits 0
  #     (harmless; needs no network, no openssl, no NUNBA_ENV_KEY), then
  #   react-app-rewired build -> ./build
  PUBLIC_URL = "/";                       # Keep assets root-absolute (/static/...)
                                          # to match Nunba's no-basename
                                          # <BrowserRouter>; the dist is served at
                                          # an origin root (the mount shim is the
                                          # sibling workstream — not built here).
  REACT_APP_API_BASE_URL = backendUrl;    # Re-point the baked API base off the dev
                                          # :5000 onto the HART backend (:6777).
                                          # process-env wins: CRA's dotenv never
                                          # overrides an already-set key.
  DISABLE_ESLINT_PLUGIN = "true";         # .env (which set this for prod builds) is
  ESLINT_NO_DEV_ERRORS = "true";          # gitignored, and .env.development is NOT
                                          # loaded when NODE_ENV=production, so the
                                          # ESLint-disable MUST come from the build
                                          # env or a lint error would fail the build.
  CI = "false";                           # CRA: CI=true makes warnings fatal.
  CYPRESS_INSTALL_BINARY = "0";           # Block the cypress postinstall net-fetch
  CYPRESS_SKIP_BINARY_INSTALL = "1";      # (no network in the sandbox).
  GENERATE_SOURCEMAP = "false";           # Smaller/faster build.
  NODE_OPTIONS = "--max-old-space-size=4096";

  # The output is a static HTML/JS/CSS tree — no shebangs/ELF to patch, and the
  # fixup phase would needlessly walk the whole bundle.
  dontFixup = true;

  # CRA emits ./build relative to sourceRoot. Land it at $out/lib/nunba/static —
  # exactly where hart-liquid-ui.nix points NUNBA_STATIC_DIR. The path suffix
  # is the byte-for-byte contract; do not change it.
  installPhase = ''
    runHook preInstall
    mkdir -p $out/lib/nunba/static
    cp -r build/. $out/lib/nunba/static/
    runHook postInstall
  '';

  meta = with lib; {
    description = "Nunba — HART OS desktop React UI (native static dist)";
    longDescription = ''
      The Nunba landing-page React SPA, compiled to a static asset tree and
      served from inside the HART OS glass shell by LiquidUIService via
      NUNBA_STATIC_DIR. Replaces the previous runtime-AppImage-download
      launcher: one native UI path, no second copy, no network fetch on boot.
    '';
    homepage = "https://hevolve.ai";
    license = licenses.asl20;
    platforms = platforms.linux;
  };
}
