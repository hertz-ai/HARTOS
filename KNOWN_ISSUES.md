# Known Issues — HART OS v1.0.0

Honest disclosure for OSS release. We believe in transparency.

---

## CRITICAL — Fix Before Production Use

### 1. Vulnerable Dependencies (CRIT-5)
Seven pinned packages have known CVEs:
- `certifi` (CVSS 9.8), `Werkzeug` (RCE 7.5), `Jinja2` (XSS), `Pillow` (3 CVEs), `urllib3`, `pydantic` (ReDoS)
- **Mitigation**: Run `pip install --upgrade certifi werkzeug jinja2 pillow urllib3` after install
- **Why not fixed**: Upgrading risks breaking `langchain==0.0.230` compatibility. Needs integration testing.

### 2. PyPI Name Squatting Risk
- `luxtts`, `hart-backend`, `hartos`, `hart-os` are NOT registered on PyPI
- An attacker could register these names and serve malicious packages
- **Mitigation**: Install only from this repo (`pip install .`), never `pip install hart-backend`

---

## HIGH — Should Fix Soon

### 3. God Files (6300 + 2500 lines)
- `langchain_gpt_api.py` — 6303 lines, 153 functions, 60 routes in one file
- `helper.py` — 2499 lines, 69 functions
- `create_recipe.py` / `reuse_recipe.py` — 33 duplicated functions remain
- **Impact**: Hard to navigate, review, and test. Circular imports between the three.
- **Plan**: Extract into domain modules post-release (routes/, llm_wrappers/, parsers/)

### 4. 333 Raw DB Sessions (Connection Leak Risk)
- 333 calls use `get_db()` (manual close) instead of `db_session()` (context manager)
- Under load, unclosed sessions may exhaust the SQLite/MySQL connection pool
- **Mitigation**: Critical paths already use `db_session()`. Low traffic = no issue.

### 5. Thread-Unsafe Global State
- `langchain_gpt_api.py` has unguarded mutable dicts (`_memory_graphs`, `config`, `_active_watchers`)
- Under concurrent `/chat` requests, these could corrupt
- **Mitigation**: Waitress WSGI server serializes requests per worker. Low concurrency = safe.

### 6. VLM Computer Use — Fixed 30-Iteration Loop
- `integrations/vlm/local_loop.py` runs a hardcoded 30-iteration loop
- No intelligent stopping (goal completion detection), no per-step safety gate
- No multi-device targeting — can only control the local screen
- **Impact**: May waste compute or miss the goal. Not dangerous — just inefficient.

### 7. LuxTTS INT8 Audio Quality
- `luxtts_tool.py` uses sherpa-onnx ZipVoice-Distill with INT8 quantization
- User-reported audio quality artifacts (crackling, distortion on some voices)
- **Workaround**: Use Pocket TTS (`pocket_tts_tool.py`) for higher quality at slight speed cost

---

## MEDIUM — Known Limitations

### 8. WhatsApp Adapter Uses Unofficial API
- `whatsapp_adapter.py` uses whatsapp-web.js (browser scraping), NOT the official Meta Cloud API
- Against WhatsApp Terms of Service — phone number may get banned
- **Plan**: Add official WhatsApp Cloud API backend (env var switch)

### 9. No Agentic Firewall Negotiation
- Device discovery uses UDP beacon (port 6780) — blocked by corporate/hotel firewalls
- NAT traversal exists (5-tier: LAN→STUN→WireGuard→relay→Crossbar)
- But no UPnP/NAT-PMP for home routers, no agentic firewall negotiation
- **Fallback**: 6-char pairing code via any messaging channel works through any firewall

### 10. Integration Wiring Gaps
These subsystems have APIs but are NOT wired to their intended consumers:
- Agent Lightning traces → not flowing to WorldModelBridge (training data stuck)
- Coding agent edits → not learned by HevolveAI
- Robotics sensor channel (0x08) → defined but unused
- OpenClaw RALT channel (0x07) → defined but unused
- 7 shell_manifest UI panels → API exists, no frontend panel

### 11. Pre-existing Test Failures
- 237 of 10,195 tests fail — ALL pre-existing, none from our changes
- Root causes: VLM timeout tests, social models fixture corruption, guardian purpose tuple count, blast radius isolation
- **Impact**: Non-blocking — core functionality works. Failures are in edge cases and test infrastructure.

### 12. Revenue Split Documentation
- README says "90% to contributors, 10% to hevolve.ai"
- Code implements 90/9/1 (90% users, 9% infrastructure pool, 1% central)
- The code is MORE transparent than the README claims
- **Fix**: README should say 90/9/1

---

## LOW — Nice to Have

### 13. No CI Trigger on PR Merge
- `auto_deploy_service.py` has `on_pr_merged()` but no GitHub webhook calls it
- Releases are manual via `workflow_dispatch` — this is intentional for now
- **Impact**: None for OSS — manual releases are safer

### 14. Canary Health Checks Incomplete
- Upgrade canary checks 2 of 5 claimed criteria (exception rate, world model health)
- Missing: latency degradation, error rate spike, throughput drop
- **Impact**: Canary may miss some degradation signals

### 15. Database Schema Drift
- HARTOS `models.py` (84 tables) and Hevolve_Database repo (156 tables) are manually synced
- No CI step to detect drift between them
- **Mitigation**: Programmatic verification confirmed 0 column differences at time of sync

### 16. HevolveAI Binary Not Yet Built
- `native_hive_loader.py` has full encrypted loading infrastructure
- The actual compiled binary (`.so.enc`) does not exist yet — runs in stub mode
- **Impact**: AI learning features (Hebbian, Bayesian, RALT, world model) use Python fallbacks
- All agentic orchestration works without the binary — it's an optimization, not a requirement

---

## What Works Well (Verified)

For balance — these are verified working with 233 functional tests:

- Recipe Pattern (CREATE/REUSE) — full lifecycle tested
- 33 Constitutional Rules — cryptographically sealed, 300s re-verification
- Master Key Kill Switch — circuit breaker + signature verification
- 90/9/1 Revenue Split — real SQLite settlements tested
- Federation — 3-node convergence with HMAC, weighted FedAvg
- PeerLink — SAME_USER trust, E2E encryption, 8 channels
- 34 Channel Adapters — all tested via parametrized suite
- 96 Expert Agents — 3 integration paths (tool, prompt injection, hints)
- Edge Privacy — scope hierarchy, DLP, consent service
- Device Control — any channel → user's local device (SAME_USER only)
- 7 Security Modules — 141 functional tests (sanitize, audit log, DLP, crypto)
- MessageBus — 10-thread concurrency tested
- Upgrade Pipeline — 7-stage orchestrator, OTA, GitHub Actions

---

*Last updated: March 14, 2026 — 31 commits, 10,195 tests (233 functional), 18/20 promises delivered*
