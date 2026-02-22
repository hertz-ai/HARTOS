[HART OS]
title=HART OS Agentic Intelligence Platform
description=HART OS requires ports for backend API, peer discovery, and optional vision/LLM services.
ports=6777/tcp|6780/udp

# Internal services (bound to 127.0.0.1, NOT exposed externally):
# - 9891/tcp + 5460/tcp: Vision sidecar (MiniCPM WebSocket)
# - 8080/tcp: LLM inference (llama.cpp)
# - 6006/tcp: Database service (Nunba desktop only)
# These ports are NOT opened in the firewall profile.
# Services bind to localhost via ExecStart arguments.
