"""CHAPTER 04 -- APPS AND UPGRADES: software enters the node, software leaves
the node, and the node itself is rebuilt.

The story so far: requests arrive at the one deployed Flask app
(LiquidUIService._create_flask_app) and every OS side effect drains through the
subprocess boundary, which this suite fakes (conftest.FakeOS) so we can read
the EXACT argv the handlers issue without touching the machine.

This chapter follows a package through its whole life:

    catalog read  ->  install pipeline (per-platform argv)  ->  history ledger
    ->  uninstall  ->  the background job state machine
    ->  the node's OWN upgrade pipeline (BUILD..DEPLOY) up to the steward gate
    ->  and the flash wizard that births NEW nodes from this one.

Route owners (read before writing this chapter, narration is code-true):
  * /api/apps/* + /api/shell/apps/*  -> integrations/agent_engine/app_installer.py
        register_app_install_routes: ONE view function bound on BOTH prefixes.
  * /api/shell/apps (GET, no suffix)  -> liquid_ui_service.py shell_apps
        (.desktop scan; a different, older surface than the installer's).
  * /api/upgrades/*  -> shell_os_apis.py, thin bridge over
        upgrade_orchestrator.UpgradeOrchestrator (7-stage, state on disk).
  * /api/shell/flash/*  -> shell_flash_apis.py driving scripts/hart_usb_flasher.
"""
import json
import os
import threading
import time

import pytest


# ─── chapter-local boundary control ─────────────────────────────────────────

# The installer's own binaries. Background daemons that were started with the
# app (e.g. the idle media indexer probing the GPU via nvidia-smi) share the
# SAME faked subprocess boundary and can interleave their argv into a test's
# call log at any moment, so every argv assertion in this chapter reads the
# log THROUGH this filter: the installer's command stream, isolated.
_PKG_BINS = ('nix-env', 'flatpak', 'wine', 'wine64', 'waydroid', 'darling',
             'notify-send')


def _pkg_calls(fake_os):
    return [c for c in fake_os.calls
            if isinstance(c, (list, tuple)) and c and c[0] in _PKG_BINS]


@pytest.fixture()
def sandboxed_installer(monkeypatch, tmp_path):
    """The AppInstaller singleton points its writable dirs at HART_APP_DIR
    (default /var/lib/hart/apps). On this Windows dev box that would resolve to
    a REAL directory outside the suite's scratch cwd, and _ensure_flathub would
    os.makedirs() into it. Re-point the two instance attrs at tmp_path for the
    duration of the test (monkeypatch restores them) so the chapter is
    hermetic. This changes WHERE staging writes land, never WHAT the handlers
    do."""
    from integrations.agent_engine.app_installer import get_installer
    inst = get_installer()
    monkeypatch.setattr(inst, '_install_dir', str(tmp_path / 'apps'))
    monkeypatch.setattr(inst, '_flatpak_dir', str(tmp_path / 'apps' / 'flatpak'))
    return inst


# ─── Scene 1: where the app catalog comes from ──────────────────────────────

def test_ch04_scene1_installed_catalog_is_read_live_from_package_managers(
        client, fake_os, sandboxed_installer):
    """GET /api/apps/installed -- the 'what is installed' catalog.

    TOPOLOGY (app_installer.shell_apps_installed -> AppInstaller.list_installed):
      entry: GET, no params, no auth gate (read-only surface)
      -> list_installed() assembles the catalog PER REQUEST from four live
         sources, SEQUENTIALLY (each in its own try/except, a missing manager
         is silently skipped, never fatal):
           1. subprocess ['nix-env', '-q', '--json']      -> parse JSON dict
           2. subprocess ['flatpak', '--user', 'list', ...] -> parse TSV lines
           3. os.listdir(<install_dir>/appimages)          -> *.appimage files
           4. os.listdir(<install_dir>/wine)               -> *.desktop files
      -> sink: JSON {'apps': [...], 'count': N}

    There is NO cached registry behind this route: the package managers ARE
    the registry. We can prove it because the canned stdout we plant at the
    subprocess boundary comes straight back out as catalog entries.
    """
    # Seed the boundary: one nix package, one flatpak app.
    fake_os.stdout_for['nix-env'] = json.dumps(
        {'ch04-htop-3.2.2': {'version': '3.2.2'}})
    fake_os.stdout_for['flatpak'] = 'CH04 Gimp\torg.gimp.GIMP\t2.10.38'

    resp = client.get('/api/apps/installed')
    assert resp.status_code == 200
    body = resp.get_json()

    # Boundary argv, in the exact sequential probe order the code walks.
    pkg = _pkg_calls(fake_os)
    assert pkg[0] == ['nix-env', '-q', '--json']
    assert pkg[1] == [
        'flatpak', '--user', 'list', '--app',
        '--columns=name,application,version']

    # Data flow: canned manager stdout -> parsed entries in the response.
    by_name = {a.get('name'): a for a in body['apps']}
    assert by_name['ch04-htop-3.2.2']['platform'] == 'nix'
    assert by_name['ch04-htop-3.2.2']['version'] == '3.2.2'
    assert by_name['CH04 Gimp']['platform'] == 'flatpak'
    assert by_name['CH04 Gimp']['app_id'] == 'org.gimp.GIMP'
    assert body['count'] == len(body['apps'])


def test_ch04_scene1b_desktop_file_registry_is_a_separate_older_surface(
        client, fake_os):
    """GET /api/shell/apps (liquid_ui_service.shell_apps) -- NOT the installer.

    TOPOLOGY: entry -> os.listdir over the freedesktop .desktop dirs
    (/usr/share/applications and ~/.local/share/applications) -> id/name
    derived from the filename -> sink: {'apps': [...max 100]}.

    On this Windows dev box neither dir exists, so the loop body never runs
    and the DEGRADE BRANCH is the story: an empty-but-well-formed catalog,
    HTTP 200, never an error. No subprocess is involved at all (pure
    filesystem scan), so the fake OS log must stay empty.
    """
    resp = client.get('/api/shell/apps')
    assert resp.status_code == 200
    body = resp.get_json()
    assert isinstance(body['apps'], list)
    assert _pkg_calls(fake_os) == []    # filesystem-only source, no argv


# ─── Scene 2: the flatpak install pipeline, argv by argv ────────────────────

def test_ch04_scene2_flatpak_install_pipeline_exact_argv(
        client, fake_os, sandboxed_installer):
    """POST /api/apps/install with a flathub ref -- the full staged pipeline.

    TOPOLOGY (shell_apps_install -> _install_req_from_json -> installer.install
              -> _install_flatpak):
      entry: POST {'source': 'flathub:org.gimp.GIMP', 'platform': 'flatpak'}
      -> validation: 'source' required (missing -> 400, never reaches install)
      -> _require_shell_auth: loopback test client passes (127.0.0.1)
      -> install(): platform explicit, so detection is skipped; source is not
         a file, so the sha256 gate is skipped
      -> _install_flatpak, TWO sequential boundary stages:
           stage A  _ensure_flathub:
             argv 1: flatpak --user remote-add --if-not-exists flathub <url>
             (idempotent, BEST-EFFORT: its rc is never even read)
           stage B  the install itself:
             argv 2: flatpak --user install -y flathub org.gimp.GIMP
             (rc==0 is the SUCCESS signal for a package manager)
      -> on success: history ledger append -> audit log -> _auto_register_app
         (best-effort; no AppRegistry in this harness, so it no-ops)
      -> sink: JSON with success/staged/verified, HTTP 200
    """
    resp = client.post('/api/apps/install', json={
        'source': 'flathub:org.gimp.GIMP', 'platform': 'flatpak',
        'name': 'ch04-gimp'})
    assert resp.status_code == 200
    body = resp.get_json()

    # The EXACT two-stage argv sequence, in order, and nothing else.
    assert _pkg_calls(fake_os) == [
        ['flatpak', '--user', 'remote-add', '--if-not-exists', 'flathub',
         'https://dl.flathub.org/repo/flathub.flatpakrepo'],
        ['flatpak', '--user', 'install', '-y', 'flathub', 'org.gimp.GIMP'],
    ]

    # verified is DERIVED: a non-staged success IS the positive runtime
    # confirmation (package-manager exit 0), per InstallResult.verified.
    assert body['success'] is True
    assert body['staged'] is False
    assert body['verified'] is True
    assert body['platform'] == 'flatpak'
    assert body['app_id'] == 'org.gimp.GIMP'

    # The history ledger (installer._history) recorded the completed act.
    hist = client.get('/api/apps/history').get_json()['history']
    ours = [h for h in hist if h['name'] == 'ch04-gimp']
    assert ours and ours[-1]['success'] is True


def test_ch04_scene3_nix_install_autodetects_from_source_prefix(
        client, fake_os, sandboxed_installer):
    """POST /api/apps/install with a bare 'nixpkgs.' source -- detection leg.

    TOPOLOGY: entry {'source': 'nixpkgs.htop'} with NO platform
      -> install() platform==UNKNOWN -> the 'nixpkgs.' prefix routes to NIX
         (the string prefix IS the router; a non-file, non-prefixed source
         would ALSO fall through to nix as the catch-all)
      -> _install_nix: ONE boundary stage:
           argv: nix-env -iA nixpkgs.htop        (rc 0 -> success)
      -> sink: success JSON. Note the code returns install_path
         '/nix/store/.../htop' LITERALLY -- a placeholder, not a resolved
         store path; narrated as a fact of the data flow, asserted verbatim.
    """
    resp = client.post('/api/apps/install', json={
        'source': 'nixpkgs.htop', 'name': 'ch04-htop'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert _pkg_calls(fake_os) == [['nix-env', '-iA', 'nixpkgs.htop']]
    assert body['success'] is True and body['verified'] is True
    assert body['app_id'] == 'htop'
    assert body['install_path'] == '/nix/store/.../htop'   # placeholder literal


# ─── Scene 4: forcing a failure at each pipeline stage ──────────────────────

def test_ch04_scene4_failure_at_each_stage_aborts_without_rollback(
        client, fake_os, sandboxed_installer, tmp_path):
    """The abort semantics of the install pipeline, stage by stage.

    What the CODE does on failure (narrated, then proven): the pipeline has
    NO rollback machinery. A failed stage returns an error InstallResult and
    the route maps it to HTTP 400; whatever side effects earlier stages left
    (the flathub remote-add, a staged file) simply REMAIN. That is safe here
    by design: remote-add is idempotent and the sha256 gate refuses BEFORE
    any boundary call.
    """
    # Stage A (remote-add) forced to fail: rc is IGNORED by _ensure_flathub
    # ("best-effort -- never raises"), so the pipeline proceeds and SUCCEEDS.
    fake_os.rc_for['remote-add'] = 1
    resp = client.post('/api/apps/install', json={
        'source': 'flathub:org.x.A', 'platform': 'flatpak', 'name': 'ch04-a'})
    assert resp.status_code == 200
    assert resp.get_json()['success'] is True   # stage A cannot abort the flow

    # Stage B (the install exec) forced to fail: rc!=0 -> success False ->
    # route returns 400 (a CONTROLLED refusal, not a crash). Both argvs were
    # still issued IN ORDER, and no compensating command follows the failure:
    # partial state (the added remote) is left in place, by design.
    fake_os.calls.clear()
    fake_os.rc_for.clear()
    fake_os.rc_for['install -y'] = 1
    resp = client.post('/api/apps/install', json={
        'source': 'flathub:org.x.B', 'platform': 'flatpak', 'name': 'ch04-b'})
    assert resp.status_code == 400
    body = resp.get_json()
    assert body['success'] is False and body['verified'] is False
    assert [c[2] for c in _pkg_calls(fake_os)] == ['remote-add', 'install']
    joined = ' '.join(' '.join(c) for c in _pkg_calls(fake_os))
    assert 'uninstall' not in joined and 'remote-delete' not in joined

    # The failure was still written to the history ledger (the ledger records
    # attempts, not just wins).
    hist = client.get('/api/apps/history').get_json()['history']
    assert any(h['name'] == 'ch04-b' and h['success'] is False for h in hist)

    # Stage 0 (the sha256 verify gate) forced to fail: the refusal happens
    # BEFORE the handler dispatch, so (1) not one argv reaches the boundary
    # and (2) the early return also precedes the history append -- a checksum
    # refusal never enters the ledger. Narrated as data flow, proven here.
    fake_os.calls.clear()
    pkg = tmp_path / 'ch04-pkg.AppImage'
    pkg.write_bytes(b'ch04 payload')
    resp = client.post('/api/apps/install', json={
        'source': str(pkg), 'platform': 'appimage', 'name': 'ch04-sha',
        'sha256': '0' * 64})
    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'Checksum verification failed'
    assert _pkg_calls(fake_os) == []    # the boundary was never reached
    hist = client.get('/api/apps/history').get_json()['history']
    assert not any(h['name'] == 'ch04-sha' for h in hist)


# ─── Scene 5: uninstall, platform truth-table, detection ────────────────────

def test_ch04_scene5_uninstall_platforms_and_detect(
        client, fake_os, sandboxed_installer, tmp_path):
    """The removal leg and the honest platform table.

    UNINSTALL TOPOLOGY (shell_apps_uninstall -> installer.uninstall ->
    _uninstall_nix): POST {'app_id','platform'} -> validation (app_id
    required) -> argv ['nix-env', '-e', <pkg>] -> rc==0 becomes success.
    CONTRACT WRINKLE (code-true): unlike install (400 on failure), the
    uninstall route ALWAYS returns HTTP 200 and carries failure only in the
    body's success flag. Asymmetric but controlled.
    """
    resp = client.post('/api/apps/uninstall',
                       json={'app_id': 'ch04-goner', 'platform': 'nix'})
    assert resp.status_code == 200
    assert resp.get_json()['success'] is True
    assert _pkg_calls(fake_os) == [['nix-env', '-e', 'ch04-goner']]

    # Forced failure: rc 1 -> success False, yet STILL HTTP 200 (see above).
    fake_os.calls.clear()
    fake_os.rc_for['nix-env'] = 1
    resp = client.post('/api/apps/uninstall',
                       json={'app_id': 'ch04-goner', 'platform': 'nix'})
    assert resp.status_code == 200
    assert resp.get_json()['success'] is False

    # Validation-first: no app_id -> 400 before any dispatch.
    fake_os.calls.clear()
    resp = client.post('/api/apps/uninstall', json={})
    assert resp.status_code == 400
    assert _pkg_calls(fake_os) == []

    # The platform truth-table (shell_apps_platforms): availability comes from
    # shutil.which probes EXCEPT the three unconditional rows we can assert on
    # any host: appimage (always installable, just chmod), extension (HART's
    # own in-process registry), and snap (HARDCODED available=False -- the
    # honest 'unsupported on this image' refusal, shown so the UI greys it).
    table = {p['platform']: p
             for p in client.get('/api/apps/platforms').get_json()['platforms']}
    assert table['appimage']['available'] is True
    assert table['extension']['available'] is True
    assert table['snap']['available'] is False
    assert table['snap']['tool'] == 'snapd'
    assert 'unknown' not in table       # the UNKNOWN sentinel is never listed

    # Detection ingress (shell_apps_detect): extension mapping is stage 1 of
    # the detection chain (.AppImage -> appimage), before magic bytes.
    pkg = tmp_path / 'ch04-detect.AppImage'
    pkg.write_bytes(b'\x7fELF fake')
    resp = client.post('/api/apps/detect', json={'path': str(pkg)})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['platform'] == 'appimage'
    assert body['size'] == len(b'\x7fELF fake')


# ─── Scene 6: the background install job (determinate progress) ─────────────

def test_ch04_scene6_background_install_job_reaches_done_verified(
        client, fake_os, sandboxed_installer):
    """POST /api/apps/install/start -- the one-at-a-time background job.

    TOPOLOGY (shell_apps_install_start -> AppInstaller.start_install ->
    worker thread _run_install_job -> the SAME blocking install() as scene 3):
      entry: POST body identical to /install
      -> start_install: lock-guarded claim; a live job would 409 {busy:true};
         mints a monotonic per-job token the poller echoes back
      -> worker thread (PARALLEL to this request, which returns immediately):
         phase ladder downloading -> installing (a creeper thread advances the
         fraction asymptotically toward 0.85, never claiming completion) ->
         verifying (0.92, reading back the handler's positive confirmation)
         -> done | error (terminal)
      -> sink: GET /api/apps/install/progress, a lock-guarded snapshot.

    With the nix boundary canned to rc 0 the job must land phase='done',
    success=True, verified=True under OUR token.
    """
    resp = client.post('/api/apps/install/start', json={
        'source': 'nixpkgs.cowsay', 'name': 'ch04-bg'})
    assert resp.status_code == 200
    env = resp.get_json()
    assert env['ok'] is True
    token = env['token']
    assert env['progress']['phase'] == 'downloading'   # the honest first phase

    # Poll the deployed progress route until the worker reaches a terminal
    # phase. The subprocess boundary is instant, so this converges in
    # milliseconds; the deadline only guards against a wedged worker.
    snap = {}
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        snap = client.get('/api/apps/install/progress').get_json()
        if snap.get('token') == token and snap.get('phase') in ('done', 'error'):
            break
        threading.Event().wait(0.01)

    assert snap.get('token') == token, 'a foreign job superseded ours'
    assert snap['phase'] == 'done'
    assert snap['success'] is True
    assert snap['staged'] is False
    assert snap['verified'] is True     # positive confirmation, read back
    assert snap['fraction'] == 1.0
    assert snap['active'] is False      # terminal phase -> no longer active
    # The worker drove the very same single-argv nix pipeline as the
    # synchronous route (wrapping, never rewriting, install()).
    assert ['nix-env', '-iA', 'nixpkgs.cowsay'] in fake_os.calls


# ─── Scene 7: the node upgrades ITSELF, up to the steward gate ──────────────

def test_ch04_scene7_upgrade_pipeline_check_advance_and_the_sign_gate(
        client, fake_os, monkeypatch):
    """/api/upgrades/* -- the 7-stage OTA pipeline as the node exposes it.

    TOPOLOGY (shell_os_apis upgrade routes -> upgrade_orchestrator singleton,
    state persisted at agent_data/upgrade_state.json under the sandbox cwd):

      GET  /status    -> orch.get_status(): the raw persisted state dict
      POST /start     -> shell-auth gated; version required (else 400);
                         refused unless stage is idle/completed/rolled_back/
                         failed; on accept: state RESET to stage='building'
      POST /advance   -> executes exactly ONE stage handler per request and
                         moves to the next rung of the fixed ladder:
             BUILDING -> TESTING -> AUDITING -> BENCHMARKING -> SIGNING
                      -> CANARY -> DEPLOYING -> COMPLETED
      POST /rollback  -> shell-auth gated; any stage -> 'rolled_back';
                         gossip broadcast ONLY if already past signing
                         (canary/deploying) -- from earlier stages the
                         rollback is purely local.

    WHICH RUNGS A NODE CAN REACH AUTONOMOUSLY (as the code enforces it):
    BUILD (code hash), TEST (regression pass-rate gate), AUDIT (guardrail
    integrity + constitutional self-test) and BENCHMARK are all local,
    self-service stages. SIGNING is a VERIFICATION gate: _stage_sign proves
    the release was signed by the master key (security.master_key.
    full_boot_verification) and fails closed on a bad signature, a code-hash
    mismatch or a failed origin attestation.

    CORRECTED 2026-08-31. This paragraph used to say _stage_sign shells out
    to scripts/sign_release.py and that the pipeline therefore "stops at SIGN
    until a human signs" -- an AI exclusion zone. That was narration of a
    crash, not a contract. sign_release.py is a CI script needing
    MASTER_PRIVATE_KEY_HEX (a GitHub Actions secret); on a node it died on a
    relative path (`//scripts/sign_release.py`) and took stage='failed', which
    is not a gate that waits for anybody. It blocked every OTA on the .69 box
    for a day. A node signing its own release would also defeat the check it
    is standing at, so the stage now VERIFIES instead.

    The human-in-the-loop control is real but lives elsewhere: hart.ota
    .autoApply (nixos/modules/hart-ota.nix:313, default false -- "updates are
    downloaded and staged but require manual approval"), enforced at the
    switch. That is the knob a steward turns, not this rung.

    This test still neither runs nor drives SIGNING; the gate's own behaviour
    is covered by tests/unit/test_ota_signing_gate.py.

    FINDING (narrated, not driven): _stage_canary returns passed=False with
    detail 'canary started, check again later' on its FIRST call, and
    advance_pipeline maps EVERY passed=False to _fail() -> stage='failed'.
    As written, the first canary advance marks a healthy in-progress canary
    as a pipeline FAILURE, which contradicts the handler's own 'check again
    later' wording. Recorded here so the text surfaces the contradiction.
    """
    # Reset to a startable terminal state whatever earlier chapters did
    # (rollback is legal from ANY stage and needs no version).
    resp = client.post('/api/upgrades/rollback', json={'reason': 'ch04 reset'})
    assert resp.status_code == 200
    assert client.get('/api/upgrades/status').get_json()['stage'] == 'rolled_back'

    # Validation-first: no version -> 400, state untouched.
    assert client.post('/api/upgrades/start', json={}).status_code == 400
    assert client.get('/api/upgrades/status').get_json()['stage'] == 'rolled_back'

    # START: the pipeline begins at BUILDING and the state hits the disk sink.
    resp = client.post('/api/upgrades/start',
                       json={'version': 'ch04-vtest', 'sha': 'deadbeef'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {'success': True, 'stage': 'building', 'version': 'ch04-vtest'}
    from integrations.agent_engine.upgrade_orchestrator import STATE_FILE
    assert os.path.isfile(STATE_FILE)   # persistence sink, survives restarts

    # A second START while active is REFUSED -- but note the refusal rides an
    # HTTP 200 envelope with success=False (controlled, if unconventional).
    resp = client.post('/api/upgrades/start', json={'version': 'ch04-v2'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['success'] is False
    assert 'already active' in body['error']

    # ADVANCE exactly one rung: BUILD is a real GO/NO-GO gate, and both of
    # its legs are honest, so we assert whichever this environment takes:
    #   GO  : compute_code_hash succeeds (pinned here via the code's OWN
    #         precomputed-hash tier, HEVOLVE_CODE_HASH_PRECOMPUTED, the
    #         ROM/read-only deployment path) -> exactly ONE rung climbed,
    #         stage='testing', detail carries the hash evidence.
    #   NO-GO: the verifier itself cannot load (this dev box has no
    #         `cryptography`, so security.node_integrity fails to import)
    #         -> the gate fails CLOSED into stage='failed', a TERMINAL,
    #         restartable state. A broken build check can never advance an
    #         unverified build, and it never wedges the pipeline either.
    monkeypatch.setenv('HEVOLVE_CODE_HASH_PRECOMPUTED', 'f' * 64)
    resp = client.post('/api/upgrades/advance')
    assert resp.status_code == 200
    body = resp.get_json()
    if body['success']:
        assert body['stage'] == 'testing'              # exactly ONE rung climbed
        assert body['detail'].startswith('code_hash=') # BUILD's evidence
        reached = 'testing'
    else:
        assert body['stage'] == 'failed'               # fail-closed, terminal
        assert body['detail'].startswith('Build failed:')
        reached = 'failed'

    # ROLLBACK from a pre-sign stage: local-only (no gossip broadcast, that
    # fires only from canary/deploying), reason recorded, and the stage
    # history tells the whole story in order.
    resp = client.post('/api/upgrades/rollback',
                       json={'reason': 'ch04 story complete'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {'success': True, 'rolled_back_from': reached,
                    'reason': 'ch04 story complete'}
    status = client.get('/api/upgrades/status').get_json()
    assert status['stage'] == 'rolled_back'
    assert status['rollback_reason'] == 'ch04 story complete'
    assert [h['stage'] for h in status['stage_history']] == [
        'building', reached, 'rolled_back']


# ─── Scene 8: the flash wizard (this node births new nodes) ─────────────────

def test_ch04_scene8_flash_wizard_disks_validation_and_progress(
        client, fake_os):
    """/api/shell/flash/* -- the shell-driven USB flasher (shell_flash_apis).

    TOPOLOGY:
      GET /disks   -> _load_flasher() (one-time importlib exec of
                      scripts/hart_usb_flasher.py; a load failure degrades to
                      {'available': False, ...} at HTTP 200, never a crash)
                      -> list_disks_with_self_heal(allow_system=False): ONLY
                      removable/USB disks are ever offered, so the wizard is
                      structurally unable to target the running system disk
                      -> find_gh() + latest_nightly_tag() (both through the
                      faked boundary here) -> sink: the wizard's picker JSON.
      POST /start  -> validation BEFORE the job claim: device required (400),
                      variant must be desktop|server|edge (400); only then an
                      ATOMIC check-and-claim under _JOB_LOCK guards the one
                      flash at a time (a second concurrent start would 409).
      GET /progress-> a lock-guarded snapshot of the single job dict.

    We drive validation refusals only: actually claiming the job would spawn
    the download/flash worker, and a flash is exactly what a test must never
    start. Both refusals must leave the job state untouched.
    """
    resp = client.get('/api/shell/flash/disks')
    assert resp.status_code == 200
    body = resp.get_json()
    assert 'available' in body
    if body['available']:
        # The healthy branch: an offered-disks picker (possibly empty on this
        # box since the enumerator's argv is faked to empty stdout) plus the
        # fixed variant set.
        assert isinstance(body['disks'], list)
        assert body['variants'] == ['desktop', 'server', 'edge']
    else:
        # The degrade branch: flasher load/enumeration failed -> controlled
        # 200 with the reason, the wizard shows it instead of crashing.
        assert body.get('error')

    # Validation leg 1: no device.
    resp = client.post('/api/shell/flash/start', json={})
    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'device required'

    # Validation leg 2: bogus variant (device present) -- still refused
    # BEFORE the atomic claim, so no worker thread is ever spawned.
    resp = client.post('/api/shell/flash/start',
                       json={'device': 5, 'variant': 'bogus'})
    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'invalid variant'

    # The job snapshot proves neither refusal claimed the slot: the single
    # job dict is still in a non-running state (idle before any flash ever
    # started in this process; done/error only if a prior chapter finished
    # one, which none does).
    snap = client.get('/api/shell/flash/progress').get_json()
    assert snap['state'] in ('idle', 'done', 'error')
    assert snap['state'] != 'running'
    assert isinstance(snap['lines'], list)
