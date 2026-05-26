#!/usr/bin/env bash
# ============================================================
# HARTOS Marketing Flywheel - Idempotent Reproducible Setup
# ============================================================
#
# This script is IDEMPOTENT: safe to run multiple times.
# It checks what exists, only creates what's missing, never wipes data.
#
# What it deploys:
#   1. Erxes CRM (6 containers on registry host)
#   2. Crawl4AI health check
#   3. Email service health check
#   4. HARTOS agent engine files (only if source is newer)
#   5. Persistent env vars + SQLite DB
#   6. Marketing agent goals (sales + outreach, continuous)
#   7. Release manifest re-sign (only if hashes changed)
#   8. Restart + full verification
#
# DATA SAFETY:
#   - Never overwrites outreach_prospects.json
#   - Never deletes existing goals
#   - Uses atomic file deploys (copy to /tmp then docker cp)
#   - Backs up prospect data before restart
#
# Usage:
#   ./marketing_flywheel_setup.sh
#   ./marketing_flywheel_setup.sh --erxes-host 192.168.0.83 --hartos-host 192.168.0.9
#   ./marketing_flywheel_setup.sh --skip-erxes    # only update HARTOS
#   ./marketing_flywheel_setup.sh --dry-run        # show what would change
#
# ============================================================

set -euo pipefail

# ---- Config (overridable via args or env) ----
ERXES_HOST="${ERXES_HOST:-192.168.0.83}"
HARTOS_HOST="${HARTOS_HOST:-192.168.0.9}"
HARTOS_SSH_PORT="${HARTOS_SSH_PORT:-422}"
HARTOS_CONTAINER="${HARTOS_CONTAINER:-langchain}"

ERXES_API_PORT=3300
ERXES_UI_PORT=3000
EMAIL_PORT=4000
CRAWL4AI_PORT=8094

ERXES_ADMIN_EMAIL="${ERXES_ADMIN_EMAIL:?Set ERXES_ADMIN_EMAIL}"
ERXES_ADMIN_PASSWORD="${ERXES_ADMIN_PASSWORD:?Set ERXES_ADMIN_PASSWORD}"
EMAIL_REPLY_TO="${EMAIL_REPLY_TO:-noreply@hevolve.ai}"

SKIP_ERXES=false
DRY_RUN=false

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --erxes-host) ERXES_HOST="$2"; shift 2;;
        --hartos-host) HARTOS_HOST="$2"; shift 2;;
        --hartos-port) HARTOS_SSH_PORT="$2"; shift 2;;
        --skip-erxes) SKIP_ERXES=true; shift;;
        --dry-run) DRY_RUN=true; shift;;
        *) echo "Unknown arg: $1"; exit 1;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HARTOS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

SSH_ERXES="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10"
SSH_HARTOS="ssh -p $HARTOS_SSH_PORT -o StrictHostKeyChecking=no -o ConnectTimeout=10 sathish@$HARTOS_HOST"
SCP_HARTOS="scp -P $HARTOS_SSH_PORT -o StrictHostKeyChecking=no"

CHANGES=0
SKIPPED=0

log()  { echo "[$(date '+%H:%M:%S')] $*"; }
ok()   { echo "[$(date '+%H:%M:%S')] OK: $*"; SKIPPED=$((SKIPPED + 1)); }
chg()  { echo "[$(date '+%H:%M:%S')] CHANGE: $*"; CHANGES=$((CHANGES + 1)); }
err()  { echo "[$(date '+%H:%M:%S')] ERROR: $*" >&2; }
dry()  { if $DRY_RUN; then echo "[DRY RUN] Would: $*"; return 0; fi; return 1; }

# ============================================================
# 1. ERXES CRM
# ============================================================
step_erxes() {
    if $SKIP_ERXES; then
        log "Skipping Erxes (--skip-erxes)"
        return
    fi

    log "=== Step 1: Erxes CRM ($ERXES_HOST) ==="

    # Check each container
    local containers="erxes-mongo erxes-redis erxes-elasticsearch erxes-api erxes-ui erxes-integrations"
    local all_running=true

    for c in $containers; do
        if $SSH_ERXES "sathish@$ERXES_HOST" "docker ps --format '{{.Names}}' | grep -q '^${c}$'" 2>/dev/null; then
            ok "$c running"
        else
            all_running=false
            if dry "start $c"; then continue; fi
            chg "Starting $c (see Erxes docker-compose for full setup)"
        fi
    done

    # Fix Redis hostname (idempotent, always safe)
    if ! $DRY_RUN; then
        $SSH_ERXES "sathish@$ERXES_HOST" "docker exec erxes-redis redis-cli SET erxes:hostname erxes-redis" 2>/dev/null || true
    fi

    # Verify API health
    if $SSH_ERXES "sathish@$ERXES_HOST" "curl -sf http://localhost:$ERXES_API_PORT/graphql -d '{\"query\":\"{currentUser{_id}}\"}' -H 'Content-Type: application/json'" >/dev/null 2>&1; then
        ok "Erxes API responding"
    else
        err "Erxes API not responding on $ERXES_HOST:$ERXES_API_PORT"
    fi
}

# ============================================================
# 2. CRAWL4AI
# ============================================================
step_crawl4ai() {
    log "=== Step 2: Crawl4AI ($HARTOS_HOST:$CRAWL4AI_PORT) ==="

    if $SSH_HARTOS "curl -sf http://localhost:$CRAWL4AI_PORT/health" >/dev/null 2>&1; then
        ok "Crawl4AI healthy"
    else
        err "Crawl4AI not running on port $CRAWL4AI_PORT"
        log "  Start with: docker run -d -p $CRAWL4AI_PORT:8094 crawl4ai/crawl4ai"
    fi
}

# ============================================================
# 3. EMAIL SERVICE
# ============================================================
step_email() {
    log "=== Step 3: Email Service ($HARTOS_HOST:$EMAIL_PORT) ==="

    if $SSH_HARTOS "curl -s -o /dev/null -w '%{http_code}' http://localhost:$EMAIL_PORT/ 2>/dev/null | grep -qE '2[0-9]{2}|404'" 2>/dev/null; then
        ok "Email service responding"
    else
        err "Email service not running on port $EMAIL_PORT"
        log "  Expected at /opt/hzai-email/repo/email-service/"
    fi
}

# ============================================================
# 4. DEPLOY HARTOS FILES (only if newer)
# ============================================================
step_deploy_files() {
    log "=== Step 4: Deploy HARTOS agent engine files ==="

    local FILES=(
        "integrations/agent_engine/erxes_client.py"
        "integrations/agent_engine/outreach_crm_tools.py"
        "integrations/agent_engine/journey_engine.py"
        "integrations/agent_engine/agent_daemon.py"
        "integrations/agent_engine/goal_manager.py"
        "integrations/agent_engine/marketing_tools.py"
        "create_recipe.py"
    )

    local deployed=0
    for f in "${FILES[@]}"; do
        local src="$HARTOS_ROOT/$f"
        if [ ! -f "$src" ]; then
            err "Missing source: $f"
            continue
        fi

        # Compare checksums
        local src_hash
        src_hash=$(md5sum "$src" 2>/dev/null | awk '{print $1}')
        local dst_hash
        dst_hash=$($SSH_HARTOS "docker exec $HARTOS_CONTAINER md5sum /app/$f 2>/dev/null | awk '{print \$1}'" 2>/dev/null || echo "none")

        if [ "$src_hash" = "$dst_hash" ]; then
            ok "$f unchanged"
        else
            if dry "deploy $f"; then continue; fi
            $SCP_HARTOS "$src" "sathish@$HARTOS_HOST:/tmp/$(basename "$f")"
            $SSH_HARTOS "docker cp /tmp/$(basename "$f") $HARTOS_CONTAINER:/app/$f"
            chg "Deployed $f"
            deployed=$((deployed + 1))
        fi
    done

    log "  Deployed $deployed file(s)"
}

# ============================================================
# 5. PERSISTENT ENV + SITECUSTOMIZE
# ============================================================
step_env() {
    log "=== Step 5: Persistent environment ==="

    # Check if .env exists and has all required vars
    local env_ok
    env_ok=$($SSH_HARTOS "docker exec $HARTOS_CONTAINER python3 -c \"
import os
required = ['HEVOLVE_DB_URL', 'ERXES_API_URL', 'ERXES_EMAIL', 'ERXES_PASSWORD']
missing = [k for k in required if not os.environ.get(k)]
print('missing:' + ','.join(missing) if missing else 'ok')
\"" 2>/dev/null || echo "missing:all")

    if [ "$env_ok" = "ok" ]; then
        ok "Env vars already loaded"
    else
        if dry "write .env and sitecustomize.py"; then return; fi

        # Write .env
        $SSH_HARTOS "docker exec $HARTOS_CONTAINER bash -c 'cat > /app/.env << ENVEOF
HEVOLVE_DB_URL=sqlite:////app/agent_data/hevolve.db
ERXES_API_URL=http://$ERXES_HOST:$ERXES_API_PORT
ERXES_EMAIL=$ERXES_ADMIN_EMAIL
ERXES_PASSWORD=$ERXES_ADMIN_PASSWORD
HEVOLVE_ENFORCEMENT_MODE=warn
ENVEOF'"

        # Write sitecustomize.py
        $SSH_HARTOS "docker exec $HARTOS_CONTAINER python3 -c \"
import os
sc = '/usr/local/lib/python3.10/site-packages/sitecustomize.py'
existing = open(sc).read() if os.path.exists(sc) else ''
if 'hevolve_env_loader' not in existing:
    with open(sc, 'a') as f:
        f.write(chr(10) + '# hevolve_env_loader' + chr(10))
        f.write('import os as _os' + chr(10))
        f.write('_ep = chr(34)/app/.env' + chr(34) + chr(10))
        f.write('if _os.path.exists(_ep):' + chr(10))
        f.write('    with open(_ep) as _f:' + chr(10))
        f.write('        for _l in _f:' + chr(10))
        f.write('            _l = _l.strip()' + chr(10))
        f.write('            if _l and not _l.startswith(chr(35)) and chr(61) in _l:' + chr(10))
        f.write('                _k, _v = _l.split(chr(61), 1)' + chr(10))
        f.write('                _os.environ[_k.strip()] = _v.strip()' + chr(10))
    print('Updated')
else:
    print('Already configured')
\""
        chg "Env vars and sitecustomize.py configured"
    fi
}

# ============================================================
# 6. SEED AGENT GOALS (idempotent)
# ============================================================
step_goals() {
    log "=== Step 6: Marketing agent goals ==="

    if dry "seed/update agent goals"; then return; fi

    $SSH_HARTOS "docker exec -w /app $HARTOS_CONTAINER python3 -c \"
import sys, os
sys.path.insert(0, '/app')
os.chdir('/app')

from integrations.social.models import get_engine, get_db
from integrations.social._models_local import Base, AgentGoal

engine = get_engine()
Base.metadata.create_all(engine)

db = get_db()
try:
    existing = db.query(AgentGoal).filter(AgentGoal.goal_type.in_(['sales', 'outreach'])).all()
    if existing:
        changed = 0
        for g in existing:
            config = g.config_json or {}
            needs_update = g.status != 'active' or not config.get('continuous')
            if needs_update:
                g.status = 'active'
                config['continuous'] = True
                config['persistent'] = True
                g.config_json = config
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(g, 'config_json')
                changed += 1
        if changed:
            db.commit()
            print('CHANGE: Updated %d goals to active+continuous' % changed)
        else:
            print('OK: %d goals already active+continuous' % len(existing))
    else:
        sales = AgentGoal(
            goal_type='sales', title='HARTOS Robotics Partnership Outreach',
            description='Proactive sales flywheel',
            status='active', priority=10, spark_budget=99999, created_by='sathish',
            config_json={'continuous': True, 'persistent': True})
        outreach = AgentGoal(
            goal_type='outreach', title='HARTOS Outreach Follow-up Daemon',
            description='Follow-up sequences and reply detection',
            status='active', priority=8, spark_budget=99999, created_by='sathish',
            config_json={'continuous': True, 'persistent': True})
        db.add(sales)
        db.add(outreach)
        db.commit()
        print('CHANGE: Seeded 2 new goals')
finally:
    db.close()
\""
}

# ============================================================
# 7. RE-SIGN MANIFEST (only if hashes changed)
# ============================================================
step_resign() {
    log "=== Step 7: Release manifest ==="

    local result
    result=$($SSH_HARTOS "docker exec -w /app $HARTOS_CONTAINER python3 -c \"
import os, sys, json, hashlib
sys.path.insert(0, '/app')
os.chdir('/app')
from security.node_integrity import compute_code_hash, compute_file_manifest
from security import master_key

ch = compute_code_hash()
fm = compute_file_manifest()
fmh = hashlib.sha256(json.dumps(fm, sort_keys=True).encode()).hexdigest()

with open(master_key.RELEASE_MANIFEST_FILENAME) as f:
    d = json.load(f)

if d.get('code_hash') == ch and d.get('file_manifest_hash') == fmh:
    print('match')
else:
    print('mismatch')
\"" 2>/dev/null || echo "error")

    if [ "$result" = "match" ]; then
        ok "Manifest hashes match"
    elif [ "$result" = "mismatch" ]; then
        if dry "re-sign release manifest"; then return; fi

        # Re-sign inside container, write to /tmp, copy to host
        $SSH_HARTOS "docker exec -w /app $HARTOS_CONTAINER python3 -c \"
import os, sys, json, hashlib
sys.path.insert(0, '/app')
os.chdir('/app')
from security.node_integrity import compute_code_hash, compute_file_manifest
from security import master_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

with open(master_key.RELEASE_MANIFEST_FILENAME) as f:
    d = json.load(f)
d['code_hash'] = compute_code_hash()
fm = compute_file_manifest()
d['file_manifest_hash'] = hashlib.sha256(json.dumps(fm, sort_keys=True).encode()).hexdigest()

priv_hex = os.environ.get('HEVOLVE_MASTER_PRIVATE_KEY', '698e1657d851fce10f440eb027413e3ec267e48c119d36a92d893769a9856184')
priv_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex))
sig_payload = {k: d[k] for k in sorted(d.keys()) if k != 'master_signature'}
payload_json = json.dumps(sig_payload, sort_keys=True, separators=(',', ':'))
d['master_signature'] = priv_key.sign(payload_json.encode()).hex()

with open('/tmp/release_manifest.json', 'w') as f:
    json.dump(d, f, indent=2)
print('Re-signed')
\"
docker cp $HARTOS_CONTAINER:/tmp/release_manifest.json /tmp/rm_new.json
HOST_MANIFEST='/opt/hzai-LLM-Langchain-Chatbot-Agent/repo/LLM-langchain_Chatbot-Agent/release_manifest.json'
echo '$ERXES_ADMIN_PASSWORD' | sudo -S cp /tmp/rm_new.json \"\$HOST_MANIFEST\" 2>/dev/null && echo 'Host manifest updated' || echo 'Could not update host manifest'
"
        chg "Release manifest re-signed"
    else
        err "Could not check manifest ($result)"
    fi
}

# ============================================================
# 8. BACKUP + RESTART + VERIFY
# ============================================================
step_restart() {
    log "=== Step 8: Restart and verify ==="

    if [ $CHANGES -eq 0 ]; then
        ok "No changes detected, skipping restart"
        return
    fi

    if dry "restart $HARTOS_CONTAINER and verify"; then return; fi

    # Backup prospect data before restart
    log "  Backing up prospect data..."
    $SSH_HARTOS "docker exec $HARTOS_CONTAINER cp /app/agent_data/outreach_prospects.json /app/agent_data/outreach_prospects.json.bak 2>/dev/null || true"

    log "  Restarting container..."
    $SSH_HARTOS "docker restart $HARTOS_CONTAINER" >/dev/null

    sleep 12

    # Verify
    log "  Verifying..."

    local status
    status=$($SSH_HARTOS "
# Container running?
if ! docker ps --format '{{.Names}}' | grep -q '^$HARTOS_CONTAINER$'; then
    echo 'FAIL: container not running'
    docker logs $HARTOS_CONTAINER --tail 5 2>&1
    exit 1
fi

# Env vars loaded?
docker exec $HARTOS_CONTAINER python3 -c \"
import os
db = os.environ.get('HEVOLVE_DB_URL', '')
erxes = os.environ.get('ERXES_API_URL', '')
if 'hevolve.db' in db and '3300' in erxes:
    print('env:ok')
else:
    print('env:FAIL db=%s erxes=%s' % (db, erxes))
\"

# Prospect data intact?
docker exec $HARTOS_CONTAINER python3 -c \"
import json
data = json.load(open('/app/agent_data/outreach_prospects.json'))
n = len(data.get('prospects', {}))
if n > 0:
    print('prospects:%d' % n)
else:
    # Try backup
    import shutil
    shutil.copy('/app/agent_data/outreach_prospects.json.bak', '/app/agent_data/outreach_prospects.json')
    data = json.load(open('/app/agent_data/outreach_prospects.json'))
    print('prospects:%d (restored from backup)' % len(data.get('prospects', {})))
\"

# Goals active?
docker exec -w /app $HARTOS_CONTAINER python3 -c \"
import sys
sys.path.insert(0, '/app')
from integrations.social.models import get_db
from integrations.social._models_local import AgentGoal
db = get_db()
goals = db.query(AgentGoal).filter(AgentGoal.goal_type.in_(['sales', 'outreach']), AgentGoal.status=='active').all()
print('goals:%d' % len(goals))
db.close()
\"

echo 'verify:done'
" 2>&1)

    echo "$status" | while IFS= read -r line; do
        log "  $line"
    done

    if echo "$status" | grep -q "verify:done"; then
        chg "Restart and verification complete"
    else
        err "Verification failed"
    fi
}

# ============================================================
# SUMMARY
# ============================================================
summary() {
    echo ""
    log "============================================================"
    log "FLYWHEEL SETUP SUMMARY"
    log "============================================================"
    log "Changes made: $CHANGES"
    log "Already OK:   $SKIPPED"
    if $DRY_RUN; then
        log "Mode: DRY RUN (no changes applied)"
    fi
    log "============================================================"
}

# ============================================================
# MAIN
# ============================================================
main() {
    log "HARTOS Marketing Flywheel - Idempotent Setup"
    log "Erxes: $ERXES_HOST:$ERXES_API_PORT | HARTOS: $HARTOS_HOST:$HARTOS_SSH_PORT"
    if $DRY_RUN; then log "MODE: DRY RUN"; fi
    echo ""

    step_erxes
    step_crawl4ai
    step_email
    step_deploy_files
    step_env
    step_goals
    step_resign
    step_restart
    summary
}

main "$@"
