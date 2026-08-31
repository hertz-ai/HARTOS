"""Debug launcher for HARTOS — all output to debug_output.log."""
import logging
import sys
import os

# Force all logging to file
log_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'debug_output.log')
handler = logging.FileHandler(log_file, mode='w', encoding='utf-8')
handler.setLevel(logging.DEBUG)
handler.setFormatter(logging.Formatter('%(asctime)s [%(name)s] %(levelname)s: %(message)s'))

# Root logger
root = logging.getLogger()
root.setLevel(logging.DEBUG)
root.addHandler(handler)

# Also add stderr handler for minimal feedback
stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setLevel(logging.WARNING)
root.addHandler(stderr_handler)

# Now import app — TID251 noqa: this is the dev/debug entry point, the
# only thread is main, no worker import-lock race possible.  See
# core/safe_hartos_attr.py for the rule that bans this elsewhere.
from hart_intelligence_entry import app  # noqa: TID251
from core.port_registry import get_port
from waitress import serve

# Set Flask app logger to debug
app.logger.setLevel(logging.DEBUG)
app.logger.addHandler(handler)

if __name__ == '__main__':
    port = get_port('backend')
    print(f"HARTOS Debug on port {port}, logging to {log_file}", file=sys.stderr)

    # Mount the generic inbound-webhook route + start the channel event loop so
    # webhook-based channels can be exercised against the debug server.  The
    # full hartos_bootstrap does this in production; run_debug.py otherwise
    # only serves the app.  Best-effort — never block the debug server on it.
    try:
        from integrations.channels.flask_integration import init_channels
        _channels = init_channels(app, {'agent_api_url': f'http://localhost:{port}/chat'})
        _channels.register_webhook_routes(app)
        _channels.start()
        print("[run_debug] webhook route mounted: /channels/webhook/<channel>",
              file=sys.stderr)
    except Exception as _wh_e:
        print(f"[run_debug] webhook route setup skipped: {_wh_e}", file=sys.stderr)

    serve(app, host='0.0.0.0', port=port, threads=50)
