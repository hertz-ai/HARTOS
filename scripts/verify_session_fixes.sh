#!/usr/bin/env bash
# verify_session_fixes.sh — post-rebuild spot-check for the 17 fixes
# shipped between commits 2c9a219..HEAD (HARTOS) + 2b10dfb9..HEAD (Nunba).
#
# Run AFTER rebuilding Nunba and restarting it so the fixes are live.
# Each section: the BEFORE symptom (from logs/state captured 2026-05-04
# 10:57) and the AFTER expectation post-fix.
#
# Usage:
#   bash scripts/verify_session_fixes.sh  [path-to-nunba-log-dir]
#
# Default log dir is ~/Documents/Nunba/logs/.

set -u

LOG_DIR="${1:-$HOME/Documents/Nunba/logs}"
DEBUG_LOG="$LOG_DIR/frozen_debug.log"
DRAFT_LOG="$LOG_DIR/draft_decision.jsonl"

if [[ ! -f "$DEBUG_LOG" ]]; then
    echo "FAIL: $DEBUG_LOG not found - is Nunba running?"
    exit 1
fi

# Capture current log size so we only check entries from THIS session
pre_size=$(stat -c%s "$DEBUG_LOG" 2>/dev/null || stat -f%z "$DEBUG_LOG" 2>/dev/null)

pass=0
fail=0
note() { printf '  %-7s %s\n' "$1" "$2"; }

check() {
    local label="$1"; shift
    local pattern="$1"; shift
    local expect="$1"  # "absent" or "present"
    local found
    found=$(tail -c +"$pre_size" "$DEBUG_LOG" 2>/dev/null | grep -aiE "$pattern" | wc -l)
    if [[ "$expect" == "absent" ]]; then
        if [[ "$found" == "0" ]]; then
            note "[PASS]" "$label"
            ((pass++))
        else
            note "[FAIL]" "$label ($found occurrences after rebuild)"
            ((fail++))
        fi
    else
        if [[ "$found" -gt "0" ]]; then
            note "[PASS]" "$label ($found occurrences)"
            ((pass++))
        else
            note "[????]" "$label (not yet observed - trigger an action that uses this path)"
        fi
    fi
}

echo "=== #67 urllib3 DNS-retry WARNING flood ==="
check "urllib3.connectionpool DNS retries silenced" \
      "urllib3.connectionpool.*WARNING.*Retrying.*NameResolutionError" absent
check "urllib3.connectionpool SSL retries silenced (also #76)" \
      "urllib3.connectionpool.*WARNING.*SSLEOFError" absent

echo ""
echo "=== #68 Sybil-limit localhost false positive ==="
check "Sybil-limit localhost rejection silenced" \
      "Sybil limit:.*nodes from localhost" absent

echo ""
echo "=== #69 Origin attestation LICENSE missing ==="
check "Origin attestation LICENSE error silenced" \
      "Origin attestation FAILED.*Missing required file: LICENSE" absent

echo ""
echo "=== #70+#71 WAMP autobahn + asyncio socket.send floods ==="
check "autobahn ConnectionRefusedError silenced" \
      "autobahn.*Connection failed with OS error.*ConnectionRefused" absent
check "autobahn trying transport silenced" \
      "trying transport 0.*connect delay" absent
check "asyncio socket.send WARNING silenced" \
      "asyncio.*WARNING.*socket.send.. raised exception" absent

echo ""
echo "=== #75 nvidia-smi timeout silenced (was timeout=5, now 15) ==="
check "nvidia-smi exceeded-timeout WARNING absent" \
      "subprocess nvidia-smi exceeded timeout" absent

echo ""
echo "=== #62+#63 Nunba: agent-bound chat hard-routes (no silent fallback) ==="
echo "  Trigger: open Nunba UI → click an agent in Recents (e.g. Speech Therapy)"
echo "  → send 'Hi' (or any message)"
check "WARNING fires on missing-recipe fallback" \
      "Recipe missing locally for prompt_id|Recipe missing locally for agent_id" present
check "create_agent forced True log surfaces" \
      "create_agent forced True by _resolve_agent fallback" present
check "Old silent fallback to local_assistant on missing recipe NO LONGER fires" \
      "Chat with LOCAL agent: local_assistant" absent
check "HARTOS hard-routes via draft-first bypass" \
      "draft-first SKIPPED: prompt_id=" present

echo ""
echo "=== #64+#65 Recipe-file cloud sync ==="
echo "  Trigger: create a new agent end-to-end (gather_info → recipe complete)"
check "recipe_sync push log fires on agent creation" \
      "recipe_sync: pushed prompt_id=" present
echo "  Trigger: switch to a different machine and click the same agent"
check "recipe_sync pull log fires on missing-local recipe" \
      "recipe_sync: pulled prompt_id=" present

echo ""
echo "=== #66 prompts/ snapshot at boot ==="
check "snapshot_at_boot log fires once per HARTOS boot" \
      "prompts_backup: snapshot.*saved" present

echo ""
echo "=== #79 reviewer gap fixes ==="
echo "  Trigger: re-push same recipe twice in a row (e.g. complete a flow then complete same flow)"
check "Push checksum cache skips redundant push" \
      "recipe_sync.*unchanged since last push|recipe_sync.*skipping" present

echo ""
echo "=========================================="
echo "Result: $pass passed, $fail failed"
echo "=========================================="
echo ""
echo "[????] entries are paths not yet exercised - they're not failures"
echo "themselves, just need a triggering action.  See 'Trigger:' lines"
echo "above each one for what to do."

[[ $fail -eq 0 ]] && exit 0 || exit 1
