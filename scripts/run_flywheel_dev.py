"""Run the FULL flywheel (chat routes + agent engine daemon) from SOURCE.

Live-verification harness: standalone ``hart_intelligence_entry`` serves the
chat pipeline on :6777 but does NOT wire the agent engine (that's
``hartos_bootstrap`` step 8, which only Nunba's embedded boot runs). This
runner boots BOTH — the same composition the bundled app ships — so flywheel
changes can be proven live (daemon tick → dispatch → CREATE/REUSE flow →
spark charge on completed work → goal COMPLETED) without a frozen rebuild.

Uses the dev-resolved environment: repo-local agent_data DB + repo prompts/
dir + whatever llama-server is live on :8080. Stop with Ctrl+C / TaskStop.

Usage:  python scripts/run_flywheel_dev.py
"""
import os
import sys
import logging

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s %(levelname)s %(message)s',
    stream=sys.stdout,
)

print('[flywheel-dev] importing hart_intelligence_entry (heavy, ~1-2 min)...',
      flush=True)
import hart_intelligence_entry as hie  # noqa: E402  (module-level boot)

print('[flywheel-dev] wiring agent engine (canonical init_agent_engine)...',
      flush=True)
from integrations.agent_engine import init_agent_engine  # noqa: E402
init_agent_engine(hie.app)

print('[flywheel-dev] serving chat + agent-engine on :6777; daemon starts '
      'via the phase-2 deferred thread. Watch for "Agent daemon: dispatched" '
      'and "Goal ... COMPLETED (spark_spent=...)".', flush=True)
hie.app.run(host='127.0.0.1', port=6777, threaded=True, use_reloader=False)
