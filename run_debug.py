"""Debug launcher for HARTOS — all output to debug_output.log."""
import logging
import sys
import os

# Force all logging to file
log_file = os.path.join(os.path.dirname(__file__), 'debug_output.log')
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

# Now import app
from hart_intelligence_entry import app
from core.port_registry import get_port
from waitress import serve

# Set Flask app logger to debug
app.logger.setLevel(logging.DEBUG)
app.logger.addHandler(handler)

if __name__ == '__main__':
    port = get_port('backend')
    print(f"HARTOS Debug on port {port}, logging to {log_file}", file=sys.stderr)
    serve(app, host='0.0.0.0', port=port, threads=50)
