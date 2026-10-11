"""The HART OS Nunba daemon adopts hart-llm, may bind its STT port, and has a config.json.

Measured on the Samsung node, generations 11 to 14 (2026-09-24 to 2026-10-11),
hart-nunba's journal: Nunba's LlamaConfig probed :8080 ("UNREACHABLE via HTTP
probe, queueing restart", 48 times in 30 min), then on Nunba main 4dbde904
"llama-server not found. Please install llama.cpp first" and "Nunba LLM restart
RETURNED FALSE", while hart-llm served on :808; "STT stream server failed:
[Errno 13] bind ('127.0.0.1', 801): permission denied" on every boot; and
"Failed to load config.json: No such file" at import. Each is a HART OS
packaging fact, pinned here as text.
"""
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
MOD = os.path.join(ROOT, 'nixos', 'modules', 'hart-nunba.nix')
PKG = os.path.join(ROOT, 'nixos', 'packages', 'nunba.nix')


def _read(p):
    return open(p, encoding='utf-8').read()


def test_the_unit_seeds_nunba_llama_config_to_the_os_llm_before_start():
    mod = _read(MOD)
    m = re.search(r'ExecStartPre = pkgs\.writeShellScript "hart-nunba-seed-llm" \'\'(.*?)\'\';', mod, re.S)
    assert m, 'hart-nunba.nix must seed ~/.nunba/llama_config.json in an ExecStartPre'
    body = m.group(1)
    assert '${cfg.dataDir}/.nunba' in body, 'the file lives under the daemon HOME (dataDir), not a literal /var/lib/hart'
    assert 'cfg.ports.llm' in body, 'the endpoint port is the ONE option hart-llm binds, never a literal 808 or 8080'
    for key in ('first_run: false', 'auto_start_server: false', 'use_external_llm: true',
                'external_llm_endpoint:', 'base_url: $base', 'completions: ($base + "/v1/chat/completions")',
                'type: "openai"', 'custom_api_base: $base', 'server_port: $port'):
        assert key in body, key
    assert '${pkgs.jq}/bin/jq' in body and "'. + {" in body, 'merge with jq so other keys the daemon wrote survive'
    assert '8080' not in body


def test_the_unit_may_bind_the_privileged_stt_port():
    mod = _read(MOD)
    svc = mod[mod.index('serviceConfig = {'):]
    assert re.search(r'AmbientCapabilities = \[ "CAP_NET_BIND_SERVICE" \];', svc)
    assert re.search(r'CapabilityBoundingSet = \[ "CAP_NET_BIND_SERVICE" \];', svc)
    assert 'NoNewPrivileges = true;' in svc, 'the cap grant must not loosen NoNewPrivileges'


def test_the_package_ships_a_config_json_default():
    pkg = _read(PKG)
    assert 'cp $out/lib/nunba/config.example.json $out/lib/nunba/config.json' in pkg
    assert '[ ! -e $out/lib/nunba/config.json ]' in pkg, 'a config.json the upstream repo ships must win over the example'
