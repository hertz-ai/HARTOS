{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS - LAN-path diagnostics + network-up (netconsole + token-gated
#           read-only HTTP diag endpoint + optional periodic push)
# ═══════════════════════════════════════════════════════════════
#
# THE problem this solves (the steward's ask):
#   "log to the network path journalctl instead of in pendrive, or periodically
#    sync to local network since this machine and liveos machine are in the same
#    network."
#
#   hart-boot-log.nix lands the boot journal on a HARTLOG FAT32 partition and
#   hart-journal-export.nix dumps it to a second USB stick. Both are PHYSICAL
#   recovery paths: the user must yank a stick and read it on another machine.
#   But the dev box and the live-OS box sit on the SAME home LAN, so the journal
#   should just be reachable OVER THE NETWORK - no stick, no yanking, and (with
#   netconsole) even when the userspace shell has wedged.
#
# THE design - three INDEPENDENT, opt-in sub-features (each OFF unless turned on):
#
#   (a) netconsole - the kernel ring buffer streamed over UDP to the dev box.
#       Works very early and KEEPS working when userspace is wedged (it rides the
#       kernel's own netconsole target, not any service). A dynamic configfs
#       target so it survives a DHCP lease (re-targetable without a reboot). Best
#       on WIRED / USB-ethernet (the wifi stack may be the very thing failing).
#       Dev box receives with:  socat -u UDP-RECV:6666 -    (or  nc -u -l 6666).
#
#   (b) http - a tiny read-only HTTP endpoint on the LAN that, on
#       GET /diag?t=<TOKEN>, runs a diagnostic bundle (journalctl -b + dmesg +
#       lspci + lsusb + rfkill + wpctl + ip -br a + the hart boot-log) and returns
#       it as text/plain. Token-gated (constant-time compare, FAIL-CLOSED when no
#       token is set), LAN-scoped via the firewall, read-only (runs NO actions).
#       Dev box reads the journal with one curl:
#         curl "http://<liveos-ip>:6699/diag?t=<TOKEN>"
#
#   (c) push - a systemd TIMER (the boot-log/journal-export cadence) that POSTs
#       the SAME bundle to a configured LAN target periodically (the steward's
#       "periodically sync" path). Best-effort, bounded, OFF unless a target set.
#
# NETWORK-UP (so the LAN diag is actually REACHABLE - GOAL 2, additive):
#   (d) wifiUnblock - a boot oneshot that `rfkill unblock`s the radios, so a
#       soft-blocked wifi/bt chip is not silently "off" (clears the soft-rfkill
#       the wifi probe reports as blocked='soft'). NetworkManager + the firmware
#       blobs already ship (desktop.nix); this only clears the soft block.
#   (e) usbEthernet - load the common USB-NIC drivers so plugging a USB-ethernet
#       dongle brings up a wired interface that NetworkManager DHCP-auto-connects
#       instantly (no SSID, no password) - the "debug wifi without needing wifi"
#       shortcut. (Connecting to a Wi-Fi SSID still needs the user to enter it;
#       wired/USB-ethernet does not.)
#
# WHY a NEW module (not folded into boot-log / journal-export): those two write
#   to local sticks and are owned by the boot/recovery surface; this serves/pushes
#   over the LAN. Keeping them disjoint avoids colliding with the wifi-firmware
#   lines the hardware-resilience workstream edits and the compositor/GPU modules.
#   (A mild existing DRY gap - boot-log + journal-export each carry their own inline
#   capture script - is NOT widened here: this module ships its own let-bound bundle
#   script in the SAME section design. TODO: converge the three onto one shared
#   `hart-diag-collect` script in a later, dedicated refactor.)
#
# PRIVACY / SECURITY (mind decentralization-first + privacy-first + master-key
#   exclusion): the whole module is OFF unless hart.netDiag.enable. It is READ-ONLY
#   (serves/streams diagnostics, runs NO actions). The HTTP endpoint is token-gated
#   (fail-closed without a token) and LAN-scoped (the port is opened only on the
#   firewall - on named LAN interfaces when given, else restricted to RFC1918 +
#   link-local SOURCE ranges via an nftables rule - never the external/WAN zone).
#   The bundle is plain system diagnostics (journalctl/dmesg/lspci/lsusb/rfkill/
#   ip/wpctl) run through a REDACTION filter that strips key material before it ever
#   leaves the box (see `redact` below). It needs NO central authority (pure
#   peer-to-peer on the shared LAN), matching the decentralization-first lens.
#
# TASK #148 (harden hart-net-diag - generate token at first boot + bind LAN-only)
#   - the understanding that drives the hardening below:
#   (a) TOKEN: NOT a hardcoded source/config default. A random token is generated
#       at FIRST BOOT (head -c 16 /dev/urandom | base64url) by the one-shot
#       hart-net-diag-token.service and written 0600 to /run/hart/netdiag-token
#       (tmpfs - never the Nix store, never persisted, never in `systemctl show`).
#       The HTTP service READS it from that file (per request, fail-closed) instead
#       of carrying it in its unit Environment. It is surfaced OUT-OF-BAND so the
#       dev box knows what to curl: echoed to the boot-log/journal + shown in the
#       login MOTD. (A static hart.netDiag.http.token still overrides, written to
#       the SAME file - so there is ONE read path, never an Environment leak.)
#   (b) BIND LAN-ONLY: the firewall NEVER opens the port globally. With named
#       interfaces -> opened only on those (networking.firewall.interfaces.<if>).
#       With none -> an nftables rule accepts the port only from RFC1918 +
#       link-local SOURCES, so it is reachable on the LAN but never from a WAN/
#       public source. The HTTP server also prefers to BIND the detected LAN IP
#       (bindAddress = "auto") rather than all interfaces, failing safe.
#   (c) READ-ONLY: the endpoint execs ONLY the fixed diagScript (no action, no
#       write, no path/command is ever interpolated from the request).
#   (d) SECRET EXCLUSION: `redact` strips PEM private keys, *_PRIVATE_KEY /
#       MASTER_KEY / *_SECRET / *_TOKEN / *_PASSWORD / API-key assignments,
#       Authorization/Cookie headers, and the diag token itself from every captured
#       stream. The bundle NEVER cats /var/lib/hart *key* / security/*.pem / any
#       master-key material - it only emits diagnostics, now redacted.
#
# VM/HW-gated: tests/net-diag.nix BOOTS a desktop node, enables the module, and
#   proves the HTTP contract BEHAVIOURALLY (a real curl over loopback): a valid
#   token returns 200 + the diagnostic sections; a wrong/absent token returns 403
#   (fail-closed); the firewall opens the port; the rfkill-unblock oneshot ran; the
#   diag CLI is on PATH. The real "the dev box curls the live-OS box across the
#   home LAN" still needs two physical machines; the test proves every link short
#   of the second box.

let
  cfg = config.hart;
  nd  = config.hart.netDiag;

  # The GPU render verdict hart-gpu-probe writes (hardware|software) - REUSED here
  # as pure CONTEXT in the bundle header (the software-render CPU-peg is THE freeze
  # context). We read the existing signal; we never run a second GPU probe.
  gpuRenderFile = "/run/hart/gpu-render";

  # Every tool referenced by absolute store path - the unit PATH is minimal and
  # several of these (lspci/lsusb/rfkill/ip/wpctl) are NOT on it (the
  # iso_real_usb_boot lesson: awk/lspci/xxd/curl were off the minimal unit PATH).
  binPath = lib.makeBinPath (with pkgs; [
    coreutils util-linux systemd gnugrep gnused gawk kmod
    pciutils   # lspci  (network-class device enumeration)
    usbutils   # lsusb  (USB device enumeration - the USB-NIC / dongle surface)
    iproute2   # ip     (interface up/down + addresses)
  ]);

  # The first-boot-generated token lives here (tmpfs, 0600). The HTTP service
  # reads it; the diag bundle reads it to REDACT itself; the MOTD reads it to
  # surface it. ONE known runtime path - never the Nix store, never persisted.
  tokenFile = "/run/hart/netdiag-token";

  # wpctl ships with WirePlumber; attr-guarded so a nixpkgs rev lacking it cannot
  # break evaluation (the drm_info attr-guard pattern from hart-boot-log.nix).
  wpctlBin =
    if pkgs ? wireplumber then "${pkgs.wireplumber}/bin/wpctl"
    else "";

  # The HARTLOG boot-log latest file - the same single source of truth the
  # hart-boot-log writer uses (read-only here). Surfaced in the bundle when the
  # partition happens to be mounted, so the LAN reader gets the boot-log too.
  bootLogLatest = "hart-boot-latest.log";

  # ── The read-only diagnostic-bundle script (stdout only - never mounts) ──────
  # Pure POSIX sh. `set -u` only (NOT -e): a probe failing must NEVER abort the
  # bundle - we want a PARTIAL bundle from a half-wedged system; every probe is
  # `|| true`-guarded + bounded. UNLIKE the boot-log/journal-export scripts this
  # ONLY writes to STDOUT (the HTTP handler / push pipe its output) - it mounts
  # nothing and changes nothing, so it is safe to run on every request.
  diagScript = pkgs.writeShellScript "hart-net-diag-collect" ''
    set -u
    export PATH=${binPath}''${PATH:+:$PATH}

    GPU_FILE="${gpuRenderFile}"
    WPCTL="${wpctlBin}"
    JOURNAL_CAP_BYTES=4000000
    BOOTLOG_NAME="${bootLogLatest}"
    TOKEN_FILE="${tokenFile}"

    # ── SECRET EXCLUSION (#148d) ────────────────────────────────────────────────
    # Every stream that could carry a leaked secret (journalctl/dmesg/boot-log) is
    # piped through `redact` BEFORE it leaves the box. It strips, line by line:
    #   - PEM private-key blocks ("-----BEGIN ... PRIVATE KEY-----")
    #   - any *_PRIVATE_KEY / *_MASTER_KEY / *_SECRET / *_TOKEN / *_PASSWORD /
    #     *_API_KEY / *_ACCESS_KEY  KEY=VALUE / KEY: VALUE assignment
    #   - Authorization / X-Api-Key / Cookie / Set-Cookie header values
    #   - the LAN diag TOKEN itself (so the served journal never re-leaks it)
    # The bundle NEVER cats master-key material directly: it does not read the
    # /var/lib/hart node keys, security/*.pem, or any *.key - it only emits
    # diagnostics, now redacted. (A static token with sed-special chars is escaped.)
    TOKEN_VAL=$(cat "$TOKEN_FILE" 2>/dev/null) || TOKEN_VAL=""
    # Escape sed-metacharacters in the token so the final s||| can never misfire.
    TOKEN_ESC=$(printf '%s' "$TOKEN_VAL" | sed -e 's/[\\&|]/\\&/g')
    redact() {
      sed -E \
        -e 's/-----BEGIN[A-Za-z ]*PRIVATE KEY-----.*/<redacted: private key removed>/g' \
        -e 's/([A-Za-z0-9_]*(PRIVATE_KEY|MASTER_KEY|SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|ACCESS_KEY))[[:space:]]*[:=][[:space:]]*[^[:space:]]+/\1=<redacted>/gI' \
        -e 's/((Authorization|X-Api-Key|Cookie|Set-Cookie)[[:space:]]*:[[:space:]]*)[^[:space:]]+/\1<redacted>/gI' \
      | { if [ -n "$TOKEN_ESC" ]; then sed -e "s|$TOKEN_ESC|<redacted: diag token>|g"; else cat; fi ; }
    }

    BOOT_ID=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null | tr -d '-' | cut -c1-12) || BOOT_ID="unknown"
    STAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null) || STAMP="?"
    HOST=$(cat /proc/sys/kernel/hostname 2>/dev/null) || HOST="?"
    GPU=$(cat "$GPU_FILE" 2>/dev/null | tr -cd 'a-z') || GPU=""
    [ -n "$GPU" ] || GPU="unknown"

    echo "════════════════════════════════════════════════════════════"
    echo " HART OS LAN diagnostic bundle (read-only)"
    echo "   written  : $STAMP (UTC)"
    echo "   hostname : $HOST"
    echo "   boot_id  : $BOOT_ID"
    echo "   gpu      : $GPU  (from $GPU_FILE: hardware|software)"
    echo "════════════════════════════════════════════════════════════"
    echo ""

    echo "───────────── ip -br a / ip -br link (interfaces) ─────────────"
    ip -br a 2>/dev/null || ip a 2>/dev/null || echo "(ip unavailable)"
    echo ""
    ip -br link 2>/dev/null || true
    echo ""

    echo "───────────── rfkill (soft/hard block) ─────────────"
    rfkill list 2>/dev/null || {
      echo "(rfkill CLI produced no output - sysfs follows)"
      for r in /sys/class/rfkill/rfkill*; do
        [ -d "$r" ] || continue
        echo "== $r ==  type=$(cat "$r/type" 2>/dev/null)  soft=$(cat "$r/soft" 2>/dev/null)  hard=$(cat "$r/hard" 2>/dev/null)"
      done
    }
    echo ""

    echo "───────────── lspci -nn (network class) ─────────────"
    lspci -nn 2>/dev/null | grep -iE 'network|ethernet|wireless|wi-?fi' \
      || echo "(no network-class PCI device / lspci unavailable)"
    echo ""

    echo "───────────── lsusb (USB devices - dongles / USB-NIC) ─────────────"
    lsusb 2>/dev/null || echo "(lsusb unavailable)"
    echo ""

    echo "───────────── wpctl status (per-user audio graph) ─────────────"
    if [ -n "$WPCTL" ] && [ -x "$WPCTL" ]; then
      _did_user=0
      for RUNDIR in /run/user/*; do
        [ -d "$RUNDIR" ] || continue
        UNAME=$(stat -c %U "$RUNDIR" 2>/dev/null) || UNAME=""
        [ -n "$UNAME" ] || continue
        _did_user=1
        echo "== session $UNAME ($RUNDIR) =="
        runuser -u "$UNAME" -- env XDG_RUNTIME_DIR="$RUNDIR" "$WPCTL" status 2>/dev/null \
          || echo "(wpctl status unavailable for $UNAME)"
      done
      [ "$_did_user" = "1" ] || echo "(no /run/user/* session - headless/no graphical login)"
    else
      echo "(wpctl not in closure)"
    fi
    echo ""

    echo "───────────── hart boot-log (HARTLOG latest, if mounted) ─────────────"
    # Best-effort: if the HARTLOG partition is mounted somewhere, surface its
    # latest bundle so the LAN reader gets the boot-log too. We NEVER mount it here
    # (read-only diagnostics) - we only read it if it is already mounted.
    _bl=""
    for m in /run/hart/bootlog-mnt /run/media/*/* /mnt/* /media/*; do
      [ -f "$m/$BOOTLOG_NAME" ] || continue
      _bl="$m/$BOOTLOG_NAME"; break
    done
    if [ -n "$_bl" ]; then
      echo "(from $_bl)"
      head -c 200000 "$_bl" 2>/dev/null | redact || true
    else
      echo "(HARTLOG partition not mounted - the full journal below carries the same data)"
    fi
    echo ""

    echo "───────────── dmesg (tail) ─────────────"
    { dmesg 2>/dev/null | tail -n 300 | redact ; } || echo "(dmesg unavailable - kernel.dmesg_restrict? run as root)"
    echo ""

    echo "───────────── FULL current-boot journal (journalctl -b, capped, redacted) ─────────────"
    { journalctl -b --no-pager 2>/dev/null | head -c "$JOURNAL_CAP_BYTES" | redact ; } \
      || echo "(journalctl -b unavailable)"
    echo ""
    echo "═══════════════════ end of bundle ═══════════════════"
    exit 0
  '';

  # ── The token-gated read-only HTTP handler ──────────────────────────────────
  # A tiny stdlib http.server. It serves EXACTLY one route - GET /diag?t=<TOKEN> -
  # and execs ONLY the fixed diagScript above (no arbitrary command, no path is
  # ever interpolated into a shell). The token check is constant-time and FAILS
  # CLOSED: an empty configured token (or any mismatch / missing token) -> 403.
  httpHandler = pkgs.writeText "hart-net-diag-http.py" ''
    import http.server, socketserver, subprocess, hmac, os, socket, urllib.parse

    # #148a: the token is NOT in the environment (would leak via `systemctl show`).
    # It is read fresh from the 0600 tmpfs file on EVERY request, so token rotation
    # takes effect without a restart and a not-yet-generated token fails closed.
    TOKEN_FILE = os.environ.get("HART_NETDIAG_TOKEN_FILE", "")
    PORT   = int(os.environ.get("HART_NETDIAG_PORT", "6699"))
    BIND   = os.environ.get("HART_NETDIAG_BIND", "auto")
    SCRIPT = os.environ.get("HART_NETDIAG_SCRIPT", "")

    def _read_token():
        try:
            with open(TOKEN_FILE, "r") as f:
                return f.read().strip()
        except Exception:
            return ""

    def _is_private(ip):
        return (ip.startswith("10.") or ip.startswith("192.168.")
                or ip.startswith("169.254.")
                or any(ip.startswith("172.%d." % o) for o in range(16, 32)))

    def _resolve_bind(want):
        # #148b: prefer the detected LAN IP over all-interfaces. "auto" -> the
        # source IP the kernel would use to reach the LAN; bind it only if it is a
        # private (RFC1918/link-local) address. Fail SAFE: if no LAN IP is up yet,
        # fall back to 0.0.0.0 (the firewall still scopes the port to LAN sources),
        # never silently dead on loopback.
        if want != "auto":
            return want
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("10.255.255.255", 1))  # no packet leaves; picks egress
                ip = s.getsockname()[0]
            finally:
                s.close()
            if ip and _is_private(ip):
                return ip
        except Exception:
            pass
        return "0.0.0.0"

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code, body):
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

        def _deny(self):
            self._send(403, b"forbidden\n")

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path != "/diag":
                self._deny(); return
            q = urllib.parse.parse_qs(u.query)
            t = (q.get("t") or [""])[0]
            token = _read_token()
            # FAIL-CLOSED: no generated token yet -> never serve. Constant-time compare.
            if not token or not hmac.compare_digest(t, token):
                self._deny(); return
            # READ-ONLY (#148c): exec ONLY the fixed diag bundle - no request value
            # is ever interpolated into a command, path, or shell.
            try:
                out = subprocess.run(
                    [SCRIPT], capture_output=True, timeout=30).stdout
            except Exception as e:
                out = ("diag bundle failed: %r\n" % (e,)).encode()
            self._send(200, out)

        def log_message(self, *a):  # never log request lines (could carry the token)
            pass

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    if __name__ == "__main__":
        bind = _resolve_bind(BIND)
        with Server((bind, PORT), Handler) as httpd:
            httpd.serve_forever()
  '';

  # ── (b) the first-boot token generator (#148a) ───────────────────────────────
  # Generates a RANDOM token at first boot (idempotent: regenerates only when the
  # tmpfs file is absent/empty) and writes it 0600 to ${tokenFile}. A static
  # hart.netDiag.http.token, if set, is written to the SAME file instead - so the
  # HTTP service always has exactly ONE read path and the token is never carried in
  # the unit Environment (no `systemctl show` leak). Surfaced out-of-band to the
  # boot-log/journal so the operator can read it to curl from the dev box.
  tokenScript = pkgs.writeShellScript "hart-net-diag-token" ''
    set -u
    export PATH=${binPath}''${PATH:+:$PATH}
    umask 077
    TOKEN_FILE="${tokenFile}"
    STATIC="${nd.http.token}"

    if [ -n "$STATIC" ]; then
      printf '%s' "$STATIC" > "$TOKEN_FILE"
    elif [ ! -s "$TOKEN_FILE" ]; then
      # head -c 16 /dev/urandom | base64 -> URL-safe (base64url, no +/=), so it
      # drops straight into ?t=... with no escaping.
      head -c 16 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n' > "$TOKEN_FILE"
    fi

    # Lock it down: 0600, owned by the interactive desktop user (hart-admin) so the
    # login MOTD + desktop can read it out-of-band, while root (the HTTP service)
    # reads it via the root override. NOT world/group readable, NOT in the store,
    # NOT persisted (tmpfs /run). Best-effort chown so a variant without hart-admin
    # simply leaves it root-owned (HTTP still works; MOTD just won't show it there).
    chown hart-admin:hart "$TOKEN_FILE" 2>/dev/null || true
    chmod 0600 "$TOKEN_FILE"

    TOKEN=$(cat "$TOKEN_FILE" 2>/dev/null) || TOKEN=""
    IP=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1) || IP=""
    # Boot-log/journal surface (#148a): so the dev box knows what to curl. This
    # line rides the journal, which the LAN endpoint itself redacts before serving.
    echo "[hart-net-diag] LAN diag token ready -> curl \"http://''${IP:-<lan-ip>}:${toString nd.http.port}/diag?t=$TOKEN\"" >&2
    exit 0
  '';

  # ── The periodic-push script (the steward's "periodically sync" path) ────────
  # Runs the bundle once and POSTs it to the configured LAN target. Best-effort +
  # bounded: a down/slow target must never wedge the timer. curl --max-time bounds
  # the transfer; the unit's TimeoutStartSec bounds the whole thing.
  pushScript = pkgs.writeShellScript "hart-net-diag-push" ''
    set -u
    export PATH=${binPath}''${PATH:+:$PATH}
    TARGET="${nd.push.target}"
    [ -n "$TARGET" ] || { echo "[hart-net-diag-push] no target - no-op" >&2; exit 0; }
    HOST=$(cat /proc/sys/kernel/hostname 2>/dev/null) || HOST="hart"
    ${diagScript} 2>/dev/null \
      | ${pkgs.curl}/bin/curl -fsS --max-time 25 \
          -H "Content-Type: text/plain" \
          -H "X-HART-Host: $HOST" \
          --data-binary @- "$TARGET" >/dev/null 2>&1 \
      || echo "[hart-net-diag-push] push to $TARGET failed (best-effort) " >&2
    exit 0
  '';

  # ── The netconsole setup oneshot (dynamic configfs target) ───────────────────
  # Loads the netconsole module (inert without a target) + configfs, then creates a
  # dynamic target pointing at the dev box. Dynamic (configfs) NOT static
  # (kernelParams) so it survives a DHCP lease + is re-targetable without a reboot.
  # remote_mac defaults to the L2 broadcast (ff:ff:ff:ff:ff:ff) so it reaches the
  # dev box on the shared segment WITHOUT the operator first learning its MAC
  # (netconsole does no ARP). Best-effort throughout - never fails boot.
  netconsoleScript = pkgs.writeShellScript "hart-net-diag-netconsole" ''
    set -u
    export PATH=${binPath}''${PATH:+:$PATH}
    TARGET="${nd.netconsole.target}"
    PORT="${toString nd.netconsole.port}"
    IFACE="${nd.netconsole.iface}"
    MAC="${nd.netconsole.mac}"
    [ -n "$TARGET" ] || { echo "[hart-netconsole] no target - no-op" >&2; exit 0; }

    modprobe configfs  2>/dev/null || true
    modprobe netconsole 2>/dev/null || true

    CFG=/sys/kernel/config/netconsole
    if [ ! -d "$CFG" ]; then
      mkdir -p "$CFG" 2>/dev/null || true
      mount -t configfs none /sys/kernel/config 2>/dev/null || true
    fi
    [ -d "$CFG" ] || { echo "[hart-netconsole] configfs/netconsole unavailable" >&2; exit 0; }

    # Pick the egress interface if not pinned: the iface that owns the default route.
    if [ -z "$IFACE" ]; then
      IFACE=$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}') || IFACE=""
    fi
    [ -n "$IFACE" ] || { echo "[hart-netconsole] no egress interface yet - skipping" >&2; exit 0; }

    T="$CFG/hart"
    # Re-create idempotently: disable+remove a stale target first.
    if [ -d "$T" ]; then
      echo 0 > "$T/enabled" 2>/dev/null || true
      rmdir "$T" 2>/dev/null || true
    fi
    mkdir -p "$T" 2>/dev/null || { echo "[hart-netconsole] could not create target" >&2; exit 0; }

    echo "$IFACE"  > "$T/dev_name"    2>/dev/null || true
    echo "$PORT"   > "$T/local_port"  2>/dev/null || true
    echo "$TARGET" > "$T/remote_ip"   2>/dev/null || true
    echo "$PORT"   > "$T/remote_port" 2>/dev/null || true
    echo "$MAC"    > "$T/remote_mac"  2>/dev/null || true
    echo 1         > "$T/enabled"     2>/dev/null \
      && echo "[hart-netconsole] streaming kernel ring -> $TARGET:$PORT via $IFACE (mac $MAC)" >&2 \
      || echo "[hart-netconsole] could not enable target (best-effort)" >&2
    exit 0
  '';

  # ── The boot-time rfkill-unblock oneshot (GOAL 2) ────────────────────────────
  rfkillUnblockScript = pkgs.writeShellScript "hart-net-diag-rfkill-unblock" ''
    set -u
    export PATH=${binPath}''${PATH:+:$PATH}
    # Clear any SOFT block so a present radio is not silently "off". A HARD block
    # (physical switch) is untouched by `rfkill unblock` - that is correct. Pure
    # best-effort: a box with no radio simply has nothing to unblock.
    if command -v rfkill >/dev/null 2>&1; then
      rfkill unblock all 2>/dev/null \
        && echo "[hart-rfkill-unblock] cleared soft-blocks (rfkill unblock all)" >&2 \
        || echo "[hart-rfkill-unblock] rfkill unblock all returned non-zero (no radio?)" >&2
    else
      # sysfs fallback: write 0 to every rfkill soft node.
      for r in /sys/class/rfkill/rfkill*/soft; do
        [ -w "$r" ] && echo 0 > "$r" 2>/dev/null || true
      done
      echo "[hart-rfkill-unblock] rfkill CLI absent - cleared soft via sysfs" >&2
    fi
    exit 0
  '';

  # Common USB-ethernet driver modules. Loading a NIC driver with no device is
  # INERT (it binds nothing, faults nothing) - distinct from the GPU/wifi
  # modprobe-storm concern (a GPU driver can FAULT hardware; an idle NIC driver
  # just sits there). Loading them up-front guarantees a plugged USB-NIC enumerates
  # an interface immediately for NetworkManager to DHCP-auto-connect.
  usbNicModules = [
    "usbnet"        # the USB-net core (pulled in transitively, listed for clarity)
    "r8152"         # Realtek RTL8152/8153 - the most common USB3 gigabit dongles
    "ax88179_178a"  # ASIX AX88179 - very common USB3 gigabit
    "asix"          # ASIX AX88172/772 - common USB2 dongles
    "cdc_ether"     # CDC-Ethernet - many dongles + Android USB tethering
    "cdc_ncm"       # CDC-NCM - newer dongles + tethering
    "rtl8150"       # older Realtek USB ethernet
  ];
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.netDiag = {
    enable = lib.mkEnableOption ''
      LAN-path diagnostics + network-up. When ON, HART OS can stream the kernel
      ring buffer over UDP (netconsole), serve a token-gated read-only HTTP diag
      endpoint on the LAN (GET /diag?t=TOKEN -> journalctl/dmesg/lspci/lsusb/
      rfkill/wpctl/ip + the boot-log), and optionally PUSH that bundle to a LAN
      target on a timer - so the dev box reads the live-OS box's journal over the
      shared network instead of yanking a USB stick. It also clears soft-rfkill on
      boot and loads the USB-ethernet drivers so a plugged USB-NIC DHCP-auto-
      connects (the "debug wifi without wifi" shortcut). READ-ONLY (runs no
      actions), token-gated + LAN-scoped, OFF unless enabled - a pure no-op when
      disabled, and needs no central authority (peer-to-peer on the shared LAN)'';

    # ── (b) the read-only HTTP diag endpoint ──
    http = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          Serve the token-gated read-only HTTP diag endpoint. The dev box reads the
          journal off the LAN with:  curl "http://<liveos-ip>:PORT/diag?t=TOKEN".
        '';
      };
      port = lib.mkOption {
        type = lib.types.port;
        default = 6699;
        description = "TCP port for the read-only HTTP diag endpoint.";
      };
      token = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = ''
          The shared token the dev box must present (?t=TOKEN). #148a: LEAVE EMPTY
          (the default) to auto-GENERATE a random token at first boot - it is written
          0600 to ${tokenFile} (tmpfs), read by the HTTP service from that file (never
          carried in the unit Environment, so it never appears in `systemctl show`),
          and surfaced to the boot-log/journal + login MOTD so the operator can read
          it out-of-band. Set a NON-EMPTY value only to pin a STATIC token (it is then
          written to the same file - still never in the Environment). FAIL-CLOSED:
          until a token exists the endpoint always returns 403. LOW-SENSITIVITY LAN
          diagnostics token, NOT a credential to any service.
        '';
      };
      bindAddress = lib.mkOption {
        type = lib.types.str;
        default = "auto";
        description = ''
          The bind address. #148b: "auto" (the default) BINDS the detected LAN IP
          (the source the kernel would use to reach the LAN, accepted only when it is
          a private RFC1918/link-local address), so the socket is not on all
          interfaces; it fails SAFE to 0.0.0.0 when no LAN IP is up yet (the firewall
          still scopes the port to LAN sources), never silently dead on loopback. Set
          a literal address to pin one. LAN scoping is ALSO enforced by the firewall
          (interfaces when named, else RFC1918 source ranges) - never the WAN zone.
        '';
      };
      openFirewall = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Open the HTTP diag port in the firewall (scoped by `interfaces`).";
      };
      interfaces = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [];
        example = [ "enp3s0" "wlan0" ];
        description = ''
          The trusted LAN interfaces to open the diag port on. When NON-empty the
          port is opened ONLY on these interfaces (networking.firewall.interfaces.
          <iface>) - the strict, recommended setup. When EMPTY the port is opened
          globally (still token-gated + read-only); name the LAN interface to keep
          it off any other zone.
        '';
      };
    };

    # ── (a) netconsole ──
    netconsole = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = ''
          Stream the kernel ring buffer over UDP to the dev box (works very early +
          through a userspace wedge). Best on WIRED / USB-ethernet (the wifi stack
          may be the failing thing). Dev box receives with: socat -u UDP-RECV:PORT -
          (or nc -u -l PORT). OFF by default (advanced; needs a target).
        '';
      };
      target = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = "The dev box IP that receives the kernel ring over UDP.";
      };
      port = lib.mkOption {
        type = lib.types.port;
        default = 6666;
        description = "UDP port for netconsole (local + remote).";
      };
      iface = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = ''
          The egress interface to send from. Empty = auto-pick the interface that
          owns the default route (DHCP-friendly).
        '';
      };
      mac = lib.mkOption {
        type = lib.types.str;
        default = "ff:ff:ff:ff:ff:ff";
        description = ''
          The destination L2 MAC. Defaults to the broadcast MAC so the stream reaches
          the dev box on the shared segment without first learning its MAC (netconsole
          does no ARP). Set the dev box's real MAC for a unicast stream.
        '';
      };
    };

    # ── (c) periodic push ──
    push = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = ''
          Periodically PUSH the diag bundle to a LAN target (the steward's
          "periodically sync" path). OFF unless a target is set.
        '';
      };
      target = lib.mkOption {
        type = lib.types.str;
        default = "";
        example = "http://192.168.1.42:6700/ingest";
        description = "The LAN URL that receives the POSTed diag bundle (text/plain).";
      };
      intervalSeconds = lib.mkOption {
        type = lib.types.ints.positive;
        default = 60;
        description = "How often to push the bundle (seconds). Best-effort + bounded.";
      };
    };

    # ── (d) network-up: rfkill unblock ──
    wifiUnblock = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          Clear soft-rfkill on boot (`rfkill unblock all`) so a present radio is not
          silently "off". A HARD block (physical switch) is untouched. Best-effort.
        '';
      };
    };

    # ── (e) network-up: USB-ethernet drivers ──
    usbEthernet = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          Load the common USB-NIC drivers so plugging a USB-ethernet dongle brings up
          a wired interface NetworkManager DHCP-auto-connects instantly (no SSID, no
          password) - the "debug wifi without needing wifi" shortcut. Loading an
          idle NIC driver is inert (binds nothing) - distinct from the GPU/wifi
          modprobe-storm concern.
        '';
      };
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Configuration  (opt-in; pure no-op when hart.netDiag.enable = false)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && nd.enable) (lib.mkMerge [

    # ── Always-on when the module is enabled: the diag CLI + a private /run/hart ──
    {
      systemd.tmpfiles.rules = [
        "d /run/hart 0750 hart hart -"
      ];
      # The bundle script on PATH so an operator can also run it by hand:
      #   hart-net-diag-collect   (read-only; prints the bundle to stdout)
      environment.systemPackages = [
        (pkgs.runCommand "hart-net-diag-cli" { } ''
          mkdir -p $out/bin
          ln -s ${diagScript} $out/bin/hart-net-diag-collect
        '')
      ];
    }

    # ── (e) USB-ethernet drivers (load up-front; inert without a device) ──
    (lib.mkIf nd.usbEthernet.enable {
      boot.kernelModules = usbNicModules;
    })

    # ── (d) rfkill-unblock boot oneshot ──
    (lib.mkIf nd.wifiUnblock.enable {
      systemd.services.hart-net-diag-rfkill-unblock = {
        description = "HART OS - clear soft-rfkill on boot so a present radio is not silently off";
        wantedBy = [ "multi-user.target" ];
        after = [ "systemd-rfkill.service" ];
        restartIfChanged = false;
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = "${rfkillUnblockScript}";
          TimeoutStartSec = "15s";
        };
      };
    })

    # ── (b) the token-gated read-only HTTP diag endpoint ──
    (lib.mkIf nd.http.enable {
      # #148a: the first-boot token GENERATOR. Writes a random (or the pinned
      # static) token 0600 to ${tokenFile} BEFORE the HTTP service starts, so the
      # endpoint never serves with no token. Idempotent + boot-safe (exit 0 always).
      systemd.services.hart-net-diag-token = {
        description = "HART OS - generate the LAN diag token at first boot (0600, surfaced to the boot-log/MOTD)";
        wantedBy = [ "multi-user.target" ];
        before = [ "hart-net-diag-http.service" ];
        after = [ "local-fs.target" ];
        restartIfChanged = false;
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = "${tokenScript}";
          TimeoutStartSec = "10s";
        };
      };

      # #148a: surface the token in the login MOTD so the operator can read it
      # out-of-band (to curl from the dev box). Reads the 0600 file only if the
      # login user can (hart-admin owns it); a no-op line otherwise. The desktop
      # shell can read the same ${tokenFile} to show it on-screen.
      environment.etc."profile.d/hart-netdiag-motd.sh" = {
        mode = "0755";
        text = ''
          #!/bin/sh
          if [ -r ${tokenFile} ]; then
            _ndt=$(cat ${tokenFile} 2>/dev/null)
            _ndip=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)
            if [ -n "$_ndt" ]; then
              echo "  LAN diag:   curl \"http://''${_ndip:-<lan-ip>}:${toString nd.http.port}/diag?t=$_ndt\"   (read-only, LAN-only)"
            fi
          fi
        '';
      };

      systemd.services.hart-net-diag-http = {
        description = "HART OS - token-gated read-only LAN HTTP diagnostic endpoint";
        wantedBy = [ "multi-user.target" ];
        # Order AFTER the token generator so a token file exists before we serve.
        after = [ "network.target" "hart-net-diag-token.service" ];
        wants = [ "hart-net-diag-token.service" ];
        # Runs as root: a complete bundle needs dmesg (kernel.dmesg_restrict=1) +
        # the full system journal + runuser into user sessions for wpctl. The
        # attack surface is the token (constant-time, fail-closed, read from a 0600
        # file - never the Environment) + the fixed diagScript (no arbitrary command
        # is ever execed) + the redaction filter. Read-only by design.
        serviceConfig = {
          Type = "simple";
          ExecStart = "${pkgs.python3}/bin/python3 ${httpHandler}";
          # #148a: the token is NOT here (no `systemctl show` leak) - the handler
          # reads it from the 0600 file each request.
          Environment = [
            "HART_NETDIAG_TOKEN_FILE=${tokenFile}"
            "HART_NETDIAG_PORT=${toString nd.http.port}"
            "HART_NETDIAG_BIND=${nd.http.bindAddress}"
            "HART_NETDIAG_SCRIPT=${diagScript}"
          ];
          Restart = "on-failure";
          RestartSec = "3s";
          # Hardening that does NOT break the read paths (runuser needs privilege,
          # so no NoNewPrivileges; the journal/dmesg need root, so no User=).
          ProtectHome = false;
          ProtectKernelTunables = true;
          ProtectControlGroups = true;
          RestrictSUIDSGID = true;
        };
      };

      # #148b: open the port LAN-ONLY - NEVER a global/WAN-reachable accept.
      #   - named interfaces  -> opened ONLY on those (networking.firewall.interfaces)
      #   - else (nftables)   -> accepted ONLY from RFC1918 + link-local SOURCE ranges
      #                          (the hart.firewall backend is nftables), so it is
      #                          reachable on the LAN but never from a public source
      #   - else (iptables, no iface) -> global accept + a warning to name the iface
      # Lists/lines merge with hart-base + hart-firewall - no conflict.
      networking.firewall =
        if nd.http.openFirewall && nd.http.interfaces != [] then {
          interfaces = lib.genAttrs nd.http.interfaces
            (_: { allowedTCPPorts = [ nd.http.port ]; });
        } else if nd.http.openFirewall && config.networking.nftables.enable then {
          extraInputRules = ''
            ip saddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, 127.0.0.0/8 } tcp dport ${toString nd.http.port} accept comment "hart-netdiag LAN-only"
            ip6 saddr { ::1, fc00::/7, fe80::/10 } tcp dport ${toString nd.http.port} accept comment "hart-netdiag LAN-only"
          '';
        } else if nd.http.openFirewall then {
          allowedTCPPorts = [ nd.http.port ];
        } else { };

      # #148b: warn when the iptables-no-interface fallback opens the port globally.
      warnings = lib.optional
        (nd.http.openFirewall && nd.http.interfaces == [] && !config.networking.nftables.enable)
        ''hart.netDiag.http opens port ${toString nd.http.port} GLOBALLY on this iptables node (no nftables, no hart.netDiag.http.interfaces named). It is still token-gated + read-only, but name the LAN interface(s) to keep it off any external zone.'';
    })

    # ── (a) netconsole setup oneshot ──
    (lib.mkIf nd.netconsole.enable {
      boot.kernelModules = [ "netconsole" ];
      systemd.services.hart-net-diag-netconsole = {
        description = "HART OS - stream the kernel ring buffer over UDP (netconsole) to the dev box";
        wantedBy = [ "multi-user.target" ];
        # After the network so an egress interface + default route exist to auto-pick.
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];
        restartIfChanged = false;
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = "${netconsoleScript}";
          TimeoutStartSec = "30s";
        };
      };
    })

    # ── (c) periodic push (timer + oneshot) ──
    (lib.mkIf (nd.push.enable && nd.push.target != "") {
      systemd.services.hart-net-diag-push = {
        description = "HART OS - push the diagnostic bundle to a LAN target";
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];
        restartIfChanged = false;
        serviceConfig = {
          Type = "oneshot";
          ExecStart = "${pushScript}";
          TimeoutStartSec = "40s";
        };
      };
      systemd.timers.hart-net-diag-push = {
        description = "HART OS - periodic diagnostic-bundle push timer (the 'periodically sync' path)";
        wantedBy = [ "timers.target" ];
        timerConfig = {
          OnBootSec = "30s";
          OnUnitActiveSec = "${toString nd.push.intervalSeconds}s";
          AccuracySec = "5s";
        };
      };
    })
  ]);
}
