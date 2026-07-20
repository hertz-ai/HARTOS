"""CHAPTER 03b -- NETWORK & CONNECTIVITY: how the node finds and joins the world.

Chapter 03 taught the shell to mutate the box it runs on. This chapter is the
node reaching OUTWARD: scanning the air for networks, carrying a WiFi secret to
nmcli (and ONLY to nmcli -- never back out in a response), pairing radios,
raising hotspots and VPNs. Every conversation with the network stack crosses
the same hermetic FakeOS boundary, so nothing here ever touches a real radio.

The cast of boundaries in this chapter:
    nmcli          -- NetworkManager CLI: wifi scan/connect/saved/toggle,
                      hotspot, VPN (shell_system_apis + liquid_ui_service)
    bluetoothctl   -- BlueZ: status/scan/connect/power (shell_system_apis)
    ip / resolvectl-- routing + DNS for the net-diag status aggregate
                      (liquid_ui_service.shell_network_status)

Two standing constraints narrated throughout:
  * SECRETS: a wifi/hotspot password enters the flow as request JSON, is
    placed INTO the boundary argv (nmcli needs it), and must NEVER appear in
    the response body. Asserted on the full response text each time.
  * LOOPS ARE LOOPS: nmcli terse output is parsed line-by-line with dedup,
    disconnects retry across a device table, bluetooth status enriches each
    device with a per-MAC info call -- each narrated as the loop it is.

Same chapter rules: behavioural asserts only, every test drives `client`
(feeding the HITS registry), forced failures via fake_os.rc_for, invalid input
leaves the boundary log EMPTY, absent subsystems degrade controlled.
"""


# ═════════════════════════════════════════════════════════════════════════════
# ACT 1 -- WIFI: SCAN AND LIST (/api/shell/wifi/networks)
# ═════════════════════════════════════════════════════════════════════════════

def test_wifi_scan_parses_terse_nmcli_dedup_and_sort(client, fake_os):
    # ENTRY  GET /api/shell/wifi/networks
    #   -> shell_system_apis.shell_wifi_networks()  [shell_system_apis.py:1806]
    #      -> rescan param absent ==> no rescan trigger
    #      -> _run(['nmcli','-t','-f','SSID,SIGNAL,SECURITY,FREQ,BSSID',
    #         'device','wifi','list'], timeout=6)  [BOUNDARY]
    #      -> parse LOOP over ':'-separated terse rows  [shell_system_apis.py:1827-1836]:
    #         * needs >=3 fields AND a non-empty SSID (hidden-SSID rows with an
    #           empty first field are dropped);
    #         * `seen` set dedups repeated SSIDs (one AP name broadcast on 2.4
    #           and 5 GHz keeps only its FIRST row);
    #      -> post-loop sort by signal DESCENDING  [shell_system_apis.py:1837]
    # DATA   4 canned rows ==> 2 networks: the HiveNet dup (signal 44) and the
    #        hidden-SSID row vanish; HiveNet(87) sorts above CafeOpen(55);
    #        empty SECURITY field survives as ''.
    fake_os.stdout_for['SSID,SIGNAL,SECURITY,FREQ,BSSID'] = (
        'HiveNet:87:WPA2:5180 MHz:AABBCC\n'
        'CafeOpen:55::2412 MHz:DDEEFF\n'
        'HiveNet:44:WPA2:2437 MHz:001122\n'
        ':30:WPA2:2412 MHz:XXYYZZ\n')
    resp = client.get('/api/shell/wifi/networks')
    assert resp.status_code == 200
    assert resp.get_json() == {
        'networks': [
            {'ssid': 'HiveNet', 'signal': 87, 'security': 'WPA2',
             'frequency': '5180 MHz'},
            {'ssid': 'CafeOpen', 'signal': 55, 'security': '',
             'frequency': '2412 MHz'},
        ],
        'count': 2,
    }
    assert ['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY,FREQ,BSSID',
            'device', 'wifi', 'list'] in fake_os.calls


def test_wifi_rescan_triggers_then_reads_cache(client, fake_os):
    # ENTRY  GET /api/shell/wifi/networks?rescan=true
    #   -> shell_wifi_networks: rescan=true fires the trigger FIRST:
    #      _run(['nmcli','device','wifi','rescan'], timeout=4)  [BOUNDARY 1]
    #      ... and deliberately does NOT sleep waiting for it (NM scans async;
    #      the old time.sleep(2) here pinned the 1-2 thread shell pool -- the
    #      wifi-click-freezes-the-UI bug narrated in the handler's docstring)
    #   -> then immediately reads whatever the scan cache holds  [BOUNDARY 2]
    # DATA   the boundary log shows the rescan trigger argv BEFORE the list
    #        argv -- trigger-then-read, no wait between them. The list self-
    #        freshens on the caller's NEXT poll once the scan lands.
    resp = client.get('/api/shell/wifi/networks?rescan=true')
    assert resp.status_code == 200
    rescan_argv = ['nmcli', 'device', 'wifi', 'rescan']
    list_argv = ['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY,FREQ,BSSID',
                 'device', 'wifi', 'list']
    assert rescan_argv in fake_os.calls
    assert list_argv in fake_os.calls
    assert fake_os.calls.index(rescan_argv) < fake_os.calls.index(list_argv)


def test_wifi_scan_tool_failure_yields_honest_empty_list(client, fake_os):
    # ENTRY  GET /api/shell/wifi/networks, nmcli exits 1 (forced -- the shape
    #        of "no wifi device" / NetworkManager down)
    #   -> shell_wifi_networks: `if r and r.returncode == 0` fails ==> the
    #      parse loop never runs  [shell_system_apis.py:1826]
    # DATA   ==> 200 {'networks': [], 'count': 0} -- an honest empty list the
    #        popover renders as "no networks", never a crash or a stale lie.
    fake_os.rc_for['nmcli'] = 1
    resp = client.get('/api/shell/wifi/networks')
    assert resp.status_code == 200
    assert resp.get_json() == {'networks': [], 'count': 0}


# ═════════════════════════════════════════════════════════════════════════════
# ACT 2 -- WIFI: JOINING (/api/shell/wifi/connect) -- where the secret travels
# ═════════════════════════════════════════════════════════════════════════════

def test_wifi_connect_carries_ssid_and_secret_to_argv_only(client, fake_os):
    # ENTRY  POST /api/shell/wifi/connect {'ssid':'HiveNet','password':'s3cret-hive'}
    #   -> @_require_system_auth passes (127.0.0.1)  [shell_system_apis.py:413]
    #   -> shell_system_apis.shell_wifi_connect()  [shell_system_apis.py:1842]
    #      -> validates ssid present  [shell_system_apis.py:1854-1855]
    #      -> builds ['nmcli','device','wifi','connect','HiveNet'] and, because
    #         a password came in, appends ['password','s3cret-hive']
    #         [shell_system_apis.py:1859-1861]
    #      -> _run_async_bounded(cmd, run_timeout=20, wait=6)
    #         [shell_system_apis.py:33]: association+DHCP runs on a daemon
    #         worker; FakeOS settles instantly ==> finished True, REAL verdict
    #         [BOUNDARY]
    # DATA   rc 0 ==> {'connected': True, 'ssid': 'HiveNet'}
    # BRANCH SECRETS CONSTRAINT: the password exists exactly ONCE in this flow
    #        -- inside the boundary argv where nmcli needs it. The response
    #        body must never echo it; asserted against the raw response text.
    #        (Narrated, not forced: a SLOW join returns 202
    #        {'connecting': True} -- an honest in-progress, never a fake
    #        success.)
    resp = client.post('/api/shell/wifi/connect',
                       json={'ssid': 'HiveNet', 'password': 's3cret-hive'})
    assert resp.status_code == 200
    assert resp.get_json() == {'connected': True, 'ssid': 'HiveNet'}
    assert ['nmcli', 'device', 'wifi', 'connect', 'HiveNet',
            'password', 's3cret-hive'] in fake_os.calls
    assert 's3cret-hive' not in resp.get_data(as_text=True)


def test_wifi_connect_hidden_network_flag(client, fake_os):
    # ENTRY  POST /api/shell/wifi/connect {'ssid':'GhostNet','hidden':True}
    #   -> shell_wifi_connect: no password ==> no password pair; hidden truthy
    #      appends ['hidden','yes']  [shell_system_apis.py:1862-1863]
    # DATA   argv ['nmcli','device','wifi','connect','GhostNet','hidden','yes']
    #        -- the hidden flag makes nmcli probe for a non-broadcasting SSID.
    resp = client.post('/api/shell/wifi/connect',
                       json={'ssid': 'GhostNet', 'hidden': True})
    assert resp.status_code == 200
    assert ['nmcli', 'device', 'wifi', 'connect', 'GhostNet',
            'hidden', 'yes'] in fake_os.calls


def test_wifi_connect_without_ssid_never_reaches_nmcli(client, fake_os):
    # ENTRY  POST /api/shell/wifi/connect {'password': 'orphan-secret'}
    #   -> shell_wifi_connect: ssid missing ==> 400 {'error': 'ssid required'}
    #      [shell_system_apis.py:1854-1855]
    # BRANCH boundary log stays EMPTY -- validation rejects the request before
    #        any argv is assembled, so the orphan secret never even reaches the
    #        boundary, let alone the airwaves.
    resp = client.post('/api/shell/wifi/connect',
                       json={'password': 'orphan-secret'})
    assert resp.status_code == 400
    assert resp.get_json() == {'error': 'ssid required'}
    assert fake_os.calls == []


def test_wifi_connect_failure_degrades_controlled_no_secret_echo(client, fake_os):
    # ENTRY  POST /api/shell/wifi/connect (valid body), nmcli exits 1 (forced
    #        -- wrong passphrase / AP out of range)
    #   -> shell_wifi_connect: finished True, rc != 0 ==> CONTROLLED 400
    #      {'connected': False, 'error': <nmcli stderr>}  [shell_system_apis.py:1874-1875]
    # BRANCH even on the failure path the secret stays out of the response --
    #        only nmcli's stderr (empty here) is surfaced.
    fake_os.rc_for['nmcli'] = 1
    resp = client.post('/api/shell/wifi/connect',
                       json={'ssid': 'HiveNet', 'password': 's3cret-hive'})
    assert resp.status_code == 400
    assert resp.get_json()['connected'] is False
    assert 's3cret-hive' not in resp.get_data(as_text=True)


# ═════════════════════════════════════════════════════════════════════════════
# ACT 3 -- WIFI: STATUS, SAVED PROFILES, FORGET, RADIO TOGGLE
# ═════════════════════════════════════════════════════════════════════════════

def test_wifi_status_parses_active_row_and_ip(client, fake_os):
    # ENTRY  GET /api/shell/wifi/status
    #   -> shell_system_apis.shell_wifi_status()  [shell_system_apis.py:1770]
    #      three probes in series, each a bounded _run:
    #      1) ['nmcli','radio','wifi']  [BOUNDARY] -> 'enabled' ==> enabled True
    #      2) ['nmcli','-t','-f','ACTIVE,SSID,SIGNAL,FREQ','device','wifi']
    #         [BOUNDARY] -> parse LOOP walks rows until parts[0]=='yes' (the
    #         active AP): the inactive CafeOpen row is SKIPPED, HiveNet matched,
    #         then break  [shell_system_apis.py:1783-1792]
    #      3) only BECAUSE connected: ['nmcli','-t','-f','IP4.ADDRESS','device',
    #         'show','type','wifi']  [BOUNDARY] -> ip after the first ':'
    # DATA   'yes:HiveNet:87:5180 MHz' ==> connected True, ssid 'HiveNet',
    #        signal 87 (int), frequency '5180 MHz';
    #        'IP4.ADDRESS[1]:192.168.1.23/24' ==> ip '192.168.1.23/24'
    fake_os.stdout_for['radio wifi'] = 'enabled'
    fake_os.stdout_for['ACTIVE,SSID,SIGNAL,FREQ'] = (
        'no:CafeOpen:55:2412 MHz\n'
        'yes:HiveNet:87:5180 MHz\n')
    fake_os.stdout_for['IP4.ADDRESS'] = 'IP4.ADDRESS[1]:192.168.1.23/24'
    resp = client.get('/api/shell/wifi/status')
    assert resp.status_code == 200
    assert resp.get_json() == {
        'enabled': True, 'connected': True, 'ssid': 'HiveNet',
        'signal': 87, 'frequency': '5180 MHz', 'ip': '192.168.1.23/24',
    }


def test_wifi_status_all_down_reports_honest_disconnected(client, fake_os):
    # ENTRY  GET /api/shell/wifi/status, every nmcli probe exits 1 (forced)
    #   -> shell_wifi_status: all `r and r.returncode == 0` gates fail; the ip
    #      probe is SKIPPED entirely (connected stayed False)
    # DATA   ==> the zero-value skeleton, honestly: enabled False, connected
    #        False, everything else None. No crash, no stale state.
    fake_os.rc_for['nmcli'] = 1
    resp = client.get('/api/shell/wifi/status')
    assert resp.status_code == 200
    assert resp.get_json() == {
        'enabled': False, 'connected': False, 'ssid': None,
        'signal': None, 'frequency': None, 'ip': None,
    }


def test_wifi_saved_filters_wireless_profiles(client, fake_os):
    # ENTRY  GET /api/shell/wifi/saved
    #   -> shell_system_apis.shell_wifi_saved()  [shell_system_apis.py:1897]
    #      -> _run(['nmcli','-t','-f','NAME,TYPE,AUTOCONNECT','connection',
    #         'show'], timeout=5)  [BOUNDARY]
    #      -> filter LOOP keeps only rows whose TYPE contains '802-11-wireless'
    #         (the ethernet profile is dropped)  [shell_system_apis.py:1903-1909]
    # DATA   3 canned profiles ==> 2 wifi entries; AUTOCONNECT 'yes'/'no' maps
    #        to True/False.
    fake_os.stdout_for['NAME,TYPE,AUTOCONNECT'] = (
        'HiveNet:802-11-wireless:yes\n'
        'Wired connection 1:802-3-ethernet:yes\n'
        'OldCafe:802-11-wireless:no\n')
    resp = client.get('/api/shell/wifi/saved')
    assert resp.status_code == 200
    assert resp.get_json() == {'connections': [
        {'ssid': 'HiveNet', 'autoconnect': True},
        {'ssid': 'OldCafe', 'autoconnect': False},
    ]}


def test_wifi_forget_maps_to_connection_delete(client, fake_os):
    # ENTRY  POST /api/shell/wifi/forget {'ssid': 'OldCafe'}
    #   -> shell_system_apis.shell_wifi_forget()  [shell_system_apis.py:1914]
    #      -> validates ssid  [shell_system_apis.py:1918-1919]
    #      -> _run(['nmcli','connection','delete','OldCafe'], timeout=10)
    #         [BOUNDARY]
    #      -> _audit_system_op('wifi_forget') best-effort
    # DATA   rc 0 ==> {'forgotten': True, 'ssid': 'OldCafe'}
    resp = client.post('/api/shell/wifi/forget', json={'ssid': 'OldCafe'})
    assert resp.status_code == 200
    assert resp.get_json() == {'forgotten': True, 'ssid': 'OldCafe'}
    assert ['nmcli', 'connection', 'delete', 'OldCafe'] in fake_os.calls


def test_wifi_forget_without_ssid_blocked(client, fake_os):
    # ENTRY  POST /api/shell/wifi/forget {}
    #   -> shell_wifi_forget: ssid missing ==> 400 {'error': 'ssid required'}
    # BRANCH boundary log stays EMPTY -- no 'connection delete' argv is ever
    #        composed without a profile name to delete.
    resp = client.post('/api/shell/wifi/forget', json={})
    assert resp.status_code == 400
    assert fake_os.calls == []


def test_wifi_toggle_off_maps_to_radio_off(client, fake_os):
    # ENTRY  POST /api/shell/wifi/toggle {'enable': False}
    #   -> shell_system_apis.shell_wifi_toggle()  [shell_system_apis.py:1929]
    #      -> enable False ==> state 'off'
    #      -> _run(['nmcli','radio','wifi','off'], timeout=5)  [BOUNDARY]
    # DATA   rc 0 ==> {'enabled': False} -- the response reports the NEW radio
    #        state, matching what was just asked of NetworkManager.
    resp = client.post('/api/shell/wifi/toggle', json={'enable': False})
    assert resp.status_code == 200
    assert resp.get_json() == {'enabled': False}
    assert ['nmcli', 'radio', 'wifi', 'off'] in fake_os.calls


def test_wifi_toggle_failure_returns_structured_500(client, fake_os):
    # ENTRY  POST /api/shell/wifi/toggle {'enable': True}, nmcli exits 1 (forced)
    #   -> shell_wifi_toggle: rc != 0 ==> DELIBERATE jsonify 500
    #      {'error': 'Failed to toggle WiFi'}  [shell_system_apis.py:1937]
    # BRANCH controlled degrade with a structured payload -- an intentional
    #        (if blunt: a 503 would say 'radio unavailable' more precisely)
    #        error, not an unhandled crash; the radio state did not silently
    #        pretend to change.
    fake_os.rc_for['nmcli'] = 1
    resp = client.post('/api/shell/wifi/toggle', json={'enable': True})
    assert resp.status_code == 500
    assert resp.get_json() == {'error': 'Failed to toggle WiFi'}


def test_wifi_disconnect_retry_loop_walks_device_table(client, fake_os):
    # ENTRY  POST /api/shell/wifi/disconnect {}
    #   -> shell_system_apis.shell_wifi_disconnect()  [shell_system_apis.py:1879]
    #      a THREE-STEP retry cascade, narrated as the loop it is:
    #      1) blind attempt ['nmcli','device','disconnect','type','wifi']
    #         [BOUNDARY] -- forced rc 1 here (older nmcli rejects that syntax)
    #      2) enumerate the device table ['nmcli','-t','-f','DEVICE,TYPE',
    #         'device']  [BOUNDARY] -> LOOP over rows hunting TYPE == 'wifi';
    #         'enp3s0:ethernet' is skipped, 'wlan0:wifi' matches
    #      3) targeted ['nmcli','device','disconnect','wlan0']  [BOUNDARY]
    #         -> rc 0 ==> success, loop exits on first working device
    # DATA   ==> 200 {'disconnected': True} -- the caller never sees the
    #        detour; the boundary log preserves all three hops in order.
    fake_os.rc_for['disconnect type'] = 1
    fake_os.stdout_for['DEVICE,TYPE'] = 'wlan0:wifi\nenp3s0:ethernet'
    resp = client.post('/api/shell/wifi/disconnect', json={})
    assert resp.status_code == 200
    assert resp.get_json() == {'disconnected': True}
    assert ['nmcli', 'device', 'disconnect', 'type', 'wifi'] in fake_os.calls
    assert ['nmcli', '-t', '-f', 'DEVICE,TYPE', 'device'] in fake_os.calls
    assert ['nmcli', 'device', 'disconnect', 'wlan0'] in fake_os.calls


# ═════════════════════════════════════════════════════════════════════════════
# ACT 4 -- THE SHELL'S OWN WIFI DOOR (liquid_ui_service /api/shell/network/*)
# The quick-settings popover's routes: same nmcli, different surface.
# ═════════════════════════════════════════════════════════════════════════════

def test_shell_network_wifi_connect_success_message_no_secret(client, fake_os):
    # ENTRY  POST /api/shell/network/wifi/connect
    #        {'ssid': 'HiveNet', 'password': 'p0p0ver-secret'}
    #   -> liquid_ui_service.shell_network_wifi_connect()  [liquid_ui_service.py:6969]
    #      (endpoint deliberately named shell_network_wifi_connect so it cannot
    #      collide with shell_system_apis' shell_wifi_connect -- the historical
    #      clash silently dropped ~16 routes; the file narrates it at 6956-6966)
    #      -> validates ssid (stripped)  [liquid_ui_service.py:6971-6974]
    #      -> subprocess.run(['nmcli','device','wifi','connect','HiveNet',
    #         'password','p0p0ver-secret'], timeout=30)  [BOUNDARY]
    # DATA   rc 0 ==> {'success': True, 'message': 'Connected to HiveNet'} --
    #        the message names the SSID, never the secret (asserted on the raw
    #        body).
    resp = client.post('/api/shell/network/wifi/connect',
                       json={'ssid': 'HiveNet', 'password': 'p0p0ver-secret'})
    assert resp.status_code == 200
    assert resp.get_json() == {'success': True, 'message': 'Connected to HiveNet'}
    assert ['nmcli', 'device', 'wifi', 'connect', 'HiveNet',
            'password', 'p0p0ver-secret'] in fake_os.calls
    assert 'p0p0ver-secret' not in resp.get_data(as_text=True)


def test_shell_network_wifi_connect_requires_ssid(client, fake_os):
    # ENTRY  POST /api/shell/network/wifi/connect {'ssid': '   '}
    #   -> shell_network_wifi_connect: .strip() empties the ssid ==> 400
    #      {'success': False, 'error': 'SSID required'}  [liquid_ui_service.py:6973-6974]
    # BRANCH boundary log stays EMPTY -- whitespace is not a network name.
    resp = client.post('/api/shell/network/wifi/connect', json={'ssid': '   '})
    assert resp.status_code == 400
    assert resp.get_json() == {'success': False, 'error': 'SSID required'}
    assert fake_os.calls == []


def test_shell_network_wifi_disconnect_interface_fallback_loop(client, fake_os):
    # ENTRY  POST /api/shell/network/wifi/disconnect
    #   -> liquid_ui_service.shell_network_wifi_disconnect()  [liquid_ui_service.py:6992]
    #      a TWO-GUESS interface cascade (hardcoded names, narrated as-is):
    #      1) ['nmcli','device','disconnect','wlan0']  [BOUNDARY] -- forced
    #         rc 1 (no wlan0 on this box)
    #      2) fallback ['nmcli','device','disconnect','wlp0s20f3']  [BOUNDARY]
    #         -- rc 0 ==> success
    # DATA   ==> 200 {'success': True, 'message': 'Disconnected from WiFi'}
    # BRANCH semantic note pinned for the reader: unlike the system-apis
    #        disconnect (which ENUMERATES the device table), this popover route
    #        guesses two literal interface names -- a box whose wifi interface
    #        is named anything else falls through to 400. The sibling route is
    #        the robust one.
    fake_os.rc_for['disconnect wlan0'] = 1
    resp = client.post('/api/shell/network/wifi/disconnect', json={})
    assert resp.status_code == 200
    assert resp.get_json() == {'success': True, 'message': 'Disconnected from WiFi'}
    assert ['nmcli', 'device', 'disconnect', 'wlan0'] in fake_os.calls
    assert ['nmcli', 'device', 'disconnect', 'wlp0s20f3'] in fake_os.calls


def test_network_status_aggregates_nmcli_ip_resolvectl(client, fake_os):
    # ENTRY  GET /api/shell/network/status  (the net-diag aggregate)
    #   -> liquid_ui_service.shell_network_status()  [liquid_ui_service.py:7009]
    #      three INDEPENDENT probes, each in its own try (one failing never
    #      empties the others):
    #      1) ['nmcli','-t','-f','DEVICE,TYPE,STATE,CONNECTION','device',
    #         'status']  [BOUNDARY] -> LOOP builds the interface table
    #      2) ['ip','route','show','default']  [BOUNDARY] -> gateway = the
    #         token AFTER 'via'
    #      3) ['resolvectl','status','--no-pager']  [BOUNDARY] -> first 'DNS
    #         Servers' line, split after ':', whitespace-split into a list
    # DATA   canned outputs ==> interfaces [wlan0/wifi/connected/HiveNet,
    #        lo/loopback/unmanaged/''], gateway '192.168.1.1',
    #        dns ['1.1.1.1', '9.9.9.9']
    fake_os.stdout_for['DEVICE,TYPE,STATE,CONNECTION'] = (
        'wlan0:wifi:connected:HiveNet\n'
        'lo:loopback:unmanaged:\n')
    fake_os.stdout_for['ip route'] = (
        'default via 192.168.1.1 dev wlan0 proto dhcp metric 600')
    fake_os.stdout_for['resolvectl'] = (
        'Global\n  DNS Servers: 1.1.1.1 9.9.9.9\n')
    resp = client.get('/api/shell/network/status')
    assert resp.status_code == 200
    assert resp.get_json() == {
        'interfaces': [
            {'device': 'wlan0', 'type': 'wifi', 'state': 'connected',
             'connection': 'HiveNet'},
            {'device': 'lo', 'type': 'loopback', 'state': 'unmanaged',
             'connection': ''},
        ],
        'gateway': '192.168.1.1',
        'dns': ['1.1.1.1', '9.9.9.9'],
    }
    assert ['ip', 'route', 'show', 'default'] in fake_os.calls
    assert ['resolvectl', 'status', '--no-pager'] in fake_os.calls


# ═════════════════════════════════════════════════════════════════════════════
# ACT 5 -- BLUETOOTH (/api/shell/bluetooth/*)
# ═════════════════════════════════════════════════════════════════════════════

def test_bluetooth_status_parses_controller_then_enriches_devices(client, fake_os):
    # ENTRY  GET /api/shell/bluetooth/status
    #   -> shell_system_apis.shell_bt_status()  [shell_system_apis.py:1056]
    #      -> _bt_run('show') == _run(['bluetoothctl','show'])  [BOUNDARY 1]
    #         -> line parser: 'Controller <MAC>' row -> address; 'Powered:'/
    #            'Discoverable:'/'Pairable:' rows -> yes/no booleans;
    #            'Name:' -> controller name
    #      -> _bt_run('devices') == ['bluetoothctl','devices']  [BOUNDARY 2]
    #         -> per paired device a NESTED enrichment LOOP fires
    #            ['bluetoothctl','info',<mac>]  [BOUNDARY 3..N] parsing
    #            Connected/Trusted/Icon per device
    # DATA   one controller + one device ==> powered True, controller address
    #        AA:BB:CC:DD:EE:FF named 'hive-node'; HiveBuds enriched to
    #        connected True, trusted True, icon 'audio-headset'.
    fake_os.stdout_for['bluetoothctl show'] = (
        'Controller AA:BB:CC:DD:EE:FF (public)\n'
        '\tName: hive-node\n'
        '\tPowered: yes\n'
        '\tDiscoverable: no\n'
        '\tPairable: yes\n')
    fake_os.stdout_for['bluetoothctl devices'] = (
        'Device 11:22:33:44:55:66 HiveBuds\n')
    fake_os.stdout_for['bluetoothctl info'] = (
        '\tConnected: yes\n'
        '\tTrusted: yes\n'
        '\tIcon: audio-headset\n')
    resp = client.get('/api/shell/bluetooth/status')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['powered'] is True
    assert body['discoverable'] is False
    assert body['pairable'] is True
    assert body['controller'] == {'address': 'AA:BB:CC:DD:EE:FF',
                                  'name': 'hive-node'}
    assert body['devices'] == [{
        'mac': '11:22:33:44:55:66', 'name': 'HiveBuds', 'paired': True,
        'connected': True, 'trusted': True, 'icon': 'audio-headset',
    }]
    assert ['bluetoothctl', 'info', '11:22:33:44:55:66'] in fake_os.calls


def test_bluetooth_scan_is_async_then_discovered_poll(client, fake_os):
    # ENTRY  POST /api/shell/bluetooth/scan {'duration': 1}
    #   -> shell_system_apis.shell_bt_scan()  [shell_system_apis.py:1096]
    #      -> clears the shared _bt_discovered list (under _bt_lock)
    #      -> spawns a DAEMON thread and returns IMMEDIATELY with
    #         {'scanning': True, 'duration': 1} -- the request thread never
    #         waits out the scan window
    #      thread-> _run(['bluetoothctl','--timeout','1','scan','on'])
    #         [BOUNDARY, off-thread] -> parse LOOP over '[NEW] Device ...'
    #         rows: the 17-char colon-separated token is the MAC, the rest the
    #         name -> appends into _bt_discovered
    # ENTRY  GET /api/shell/bluetooth/discovered (the caller's POLL loop)
    #   -> shell_bt_discovered() snapshots the list under the lock
    # DATA   canned '[NEW] Device 11:22:33:44:55:66 HiveBuds' ==> the poll
    #        eventually reports [{'mac': '11:22:33:44:55:66',
    #        'name': 'HiveBuds'}] -- async write, polled read.
    fake_os.stdout_for['scan on'] = '[NEW] Device 11:22:33:44:55:66 HiveBuds'
    resp = client.post('/api/shell/bluetooth/scan', json={'duration': 1})
    assert resp.status_code == 200
    assert resp.get_json() == {'scanning': True, 'duration': 1}
    devices = []
    for _ in range(2000):                      # bounded poll, settles in ~1 hop
        got = client.get('/api/shell/bluetooth/discovered').get_json()
        if got['count']:
            devices = got['devices']
            break
    assert devices == [{'mac': '11:22:33:44:55:66', 'name': 'HiveBuds'}]
    assert ['bluetoothctl', '--timeout', '1', 'scan', 'on'] in fake_os.calls


def test_bluetooth_connect_requires_mac(client, fake_os):
    # ENTRY  POST /api/shell/bluetooth/connect {}
    #   -> shell_system_apis.shell_bt_connect(): mac missing ==> 400
    #      {'error': 'mac required'}  [shell_system_apis.py:1141-1142]
    # BRANCH boundary log stays EMPTY -- bluetoothctl is never invoked without
    #        a device address.
    resp = client.post('/api/shell/bluetooth/connect', json={})
    assert resp.status_code == 400
    assert fake_os.calls == []


def test_bluetooth_connect_maps_to_bluetoothctl(client, fake_os):
    # ENTRY  POST /api/shell/bluetooth/connect {'mac': '11:22:33:44:55:66'}
    #   -> shell_bt_connect -> _bt_run('connect <mac>') ==
    #      _run(['bluetoothctl','connect','11:22:33:44:55:66'], timeout=15)
    #      [BOUNDARY]
    # DATA   rc 0 ==> {'connected': True, 'mac': '11:22:33:44:55:66'}
    resp = client.post('/api/shell/bluetooth/connect',
                       json={'mac': '11:22:33:44:55:66'})
    assert resp.status_code == 200
    assert resp.get_json() == {'connected': True, 'mac': '11:22:33:44:55:66'}
    assert ['bluetoothctl', 'connect', '11:22:33:44:55:66'] in fake_os.calls


def test_bluetooth_power_off_failure_reports_actual_state(client, fake_os):
    # ENTRY  POST /api/shell/bluetooth/power {'powered': False},
    #        bluetoothctl exits 1 (forced)
    #   -> shell_system_apis.shell_bt_power()  [shell_system_apis.py:1180]
    #      -> ['bluetoothctl','power','off']  [BOUNDARY]
    # DATA   the response reports the state the radio is ACTUALLY in:
    #        `powered if ok else not powered` ==> the power-off FAILED, so the
    #        radio is still on ==> {'powered': True}. The caller asked for off
    #        and the payload honestly says on.
    fake_os.rc_for['bluetoothctl'] = 1
    resp = client.post('/api/shell/bluetooth/power', json={'powered': False})
    assert resp.status_code == 200
    assert resp.get_json() == {'powered': True}
    assert ['bluetoothctl', 'power', 'off'] in fake_os.calls


# ═════════════════════════════════════════════════════════════════════════════
# ACT 6 -- HOTSPOT (/api/shell/hotspot/*): the node becomes the network
# ═════════════════════════════════════════════════════════════════════════════

def test_hotspot_start_carries_secret_to_argv_only(client, fake_os):
    # ENTRY  POST /api/shell/hotspot/start
    #        {'ssid': 'HART-Mesh', 'password': 'mesh-secret-9', 'band': 'a'}
    #   -> @_require_shell_auth passes  [shell_os_apis.py:111]
    #   -> shell_os_apis.shell_hotspot_start()  [shell_os_apis.py:2037]
    #      -> builds ['nmcli','dev','wifi','hotspot','ssid','HART-Mesh'], the
    #         password appends ['password','mesh-secret-9'], band 'a' appends
    #         ['band','a']  [shell_os_apis.py:2043-2047]
    #      -> subprocess.run(cmd, timeout=15)  [BOUNDARY]
    #      -> _audit_shell_op('hotspot_start', {'ssid': ...}) -- the audit
    #         detail carries the SSID only, never the password
    # DATA   rc 0 ==> {'started': True, 'ssid': 'HART-Mesh'}; SECRETS
    #        CONSTRAINT: the passphrase lives in the argv alone -- asserted
    #        absent from the raw response body.
    resp = client.post('/api/shell/hotspot/start',
                       json={'ssid': 'HART-Mesh', 'password': 'mesh-secret-9',
                             'band': 'a'})
    assert resp.status_code == 200
    assert resp.get_json() == {'started': True, 'ssid': 'HART-Mesh'}
    assert ['nmcli', 'dev', 'wifi', 'hotspot', 'ssid', 'HART-Mesh',
            'password', 'mesh-secret-9', 'band', 'a'] in fake_os.calls
    assert 'mesh-secret-9' not in resp.get_data(as_text=True)


def test_hotspot_start_failure_returns_structured_500(client, fake_os):
    # ENTRY  POST /api/shell/hotspot/start {'ssid': 'HART-Mesh'},
    #        nmcli exits 1 (forced -- no AP-capable radio)
    #   -> shell_hotspot_start: rc != 0 ==> deliberate jsonify 500 carrying
    #      nmcli's stderr  [shell_os_apis.py:2053]
    # DATA   FakeOS stderr is empty ==> {'error': ''} -- controlled and
    #        structured, though the empty message is a small smell the story
    #        records: the caller learns THAT it failed but not why.
    fake_os.rc_for['nmcli'] = 1
    resp = client.post('/api/shell/hotspot/start', json={'ssid': 'HART-Mesh'})
    assert resp.status_code == 500
    assert 'error' in resp.get_json()


def test_hotspot_status_two_step_probe_loop(client, fake_os):
    # ENTRY  GET /api/shell/hotspot/status
    #   -> shell_os_apis.shell_hotspot_status()  [shell_os_apis.py:2013]
    #      -> step 1: ['nmcli','-t','-f','NAME,TYPE,DEVICE','connection',
    #         'show','--active']  [BOUNDARY 1] -> LOOP over active connections
    #         keeping rows whose TYPE contains 'wifi'
    #      -> step 2 (per candidate, the nested probe): ['nmcli','-t','-f',
    #         '802-11-wireless.mode','connection','show','Hotspot']
    #         [BOUNDARY 2] -> 'ap' in the mode output marks a REAL access
    #         point (a plain client connection is not a hotspot)
    # DATA   'Hotspot:wifi:wlan0' + mode 'ap' ==> {'active': True,
    #        'hotspot': {'name': 'Hotspot', 'device': 'wlan0'}}
    fake_os.stdout_for['NAME,TYPE,DEVICE'] = 'Hotspot:wifi:wlan0'
    fake_os.stdout_for['802-11-wireless.mode'] = '802-11-wireless.mode:ap'
    resp = client.get('/api/shell/hotspot/status')
    assert resp.status_code == 200
    assert resp.get_json() == {'active': True,
                               'hotspot': {'name': 'Hotspot', 'device': 'wlan0'}}
    assert ['nmcli', '-t', '-f', '802-11-wireless.mode',
            'connection', 'show', 'Hotspot'] in fake_os.calls


# ═════════════════════════════════════════════════════════════════════════════
# ACT 7 -- VPN (/api/shell/vpn/*): the tunnel out
# ═════════════════════════════════════════════════════════════════════════════

def test_vpn_list_filters_vpn_type_only(client, fake_os):
    # ENTRY  GET /api/shell/vpn/list
    #   -> shell_system_apis.shell_vpn_list()  [shell_system_apis.py:1943]
    #      -> _run(['nmcli','-t','-f','NAME,TYPE,ACTIVE','connection','show'],
    #         timeout=5)  [BOUNDARY]
    #      -> filter LOOP keeps rows whose TYPE contains 'vpn'
    #         [shell_system_apis.py:1948-1955]
    # DATA   3 canned profiles ==> only 'office-vpn' survives; the wifi profile
    #        AND the wireguard tunnel are dropped ('wireguard' does not contain
    #        'vpn' -- a semantic gap this story pins on purpose: a wireguard
    #        VPN is invisible to this panel until the filter learns the type).
    fake_os.stdout_for['NAME,TYPE,ACTIVE'] = (
        'office-vpn:vpn:yes\n'
        'HiveNet:802-11-wireless:no\n'
        'wg-home:wireguard:no\n')
    resp = client.get('/api/shell/vpn/list')
    assert resp.status_code == 200
    assert resp.get_json() == {'connections': [
        {'name': 'office-vpn', 'type': 'vpn', 'active': True},
    ]}


def test_vpn_connect_maps_to_connection_up(client, fake_os):
    # ENTRY  POST /api/shell/vpn/connect {'name': 'office-vpn'}
    #   -> @_require_system_auth passes  [shell_system_apis.py:413]
    #   -> shell_system_apis.shell_vpn_connect()  [shell_system_apis.py:1981]
    #      -> validates name  [shell_system_apis.py:1985-1986]
    #      -> _run(['nmcli','connection','up','office-vpn'], timeout=30)
    #         [BOUNDARY]
    #      -> _audit_system_op('vpn_connect') best-effort
    # DATA   rc 0 ==> {'connected': True, 'name': 'office-vpn'}
    resp = client.post('/api/shell/vpn/connect', json={'name': 'office-vpn'})
    assert resp.status_code == 200
    assert resp.get_json() == {'connected': True, 'name': 'office-vpn'}
    assert ['nmcli', 'connection', 'up', 'office-vpn'] in fake_os.calls


def test_vpn_connect_requires_name(client, fake_os):
    # ENTRY  POST /api/shell/vpn/connect {}
    #   -> shell_vpn_connect: name missing ==> 400 {'error': 'name required'}
    # BRANCH boundary log stays EMPTY -- and with that refused tunnel the
    #        chapter closes: every order the shell can give the OS, power to
    #        packet, crossed one observable boundary or none at all.
    resp = client.post('/api/shell/vpn/connect', json={})
    assert resp.status_code == 400
    assert fake_os.calls == []
