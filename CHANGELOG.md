# Changelog

## [0.1.0] - 2026-04-12

### Security
- Regional and central tiers now enforce JWT authentication on `/chat` — body-sourced `user_id` rejected on multi-user deployments
- Log injection prevention: user_id sanitized before logging (strip control chars, truncate to 64)
- Input validation: `preferred_lang` and `language_change` validated against `SUPPORTED_LANG_DICT` before persistence
- Boot-time warning when `HEVOLVE_NODE_TIER` is unset on non-bundled deployments
- Boot-time error when regional/central tier has no `SECRET_KEY` configured
- Shell_Command denylist: removed `$` anchors, added chaining/piping bypass patterns (`mv /dev/null`, `chmod 000`, `curl|bash`, etc.)

### Added
- Draft-first architecture: 0.8B Qwen3.5 is default first-contact for all chat (`casual_conv=True`)
- `_resolve_llm_endpoint` helper: unified GPT_API/DRAFT_GPT_API resolution (port_registry → env var → /chat/completions)
- `_persist_language` helper: single write path for `hart_language.json`
- `_chat_reply` helper: unified /chat response with TTS synthesis trigger at all 13+ return sites
- `sleep_with_heartbeat`: NodeWatchdog helper preventing daemon restart cascade
- `language_change` field in draft classifier envelope for real-time language switching
- Model lifecycle: `pinned` flag (never evict, for draft 0.8B) and `pressure_evict_only` flag (survives idle sweep)
- Empty GPT_API guard: graceful error instead of MissingSchema crash loop
- `core/user_context.py`: canonical user action+profile resolver (30s TTL cache, 1.5s hot-path budget)
- `core/constants.py`: DEFAULT_USER_ID, DEFAULT_PROMPT_ID single source of truth

### Changed
- `casual_conv=True` routes to DRAFT_GPT_API (0.8B on :8081) in CustomGPT._call()
- speculative_dispatch + local_llm moved from FULL to STANDARD tier (fires on 15.7GB RAM machines)
- VisionService auto-start uses 0.8B backend, MiniCPM stays for explicit admin load only
- Catalog clears stale `loaded=True` markers on restart

### Fixed
- Connect_Channel tool description `{"bot_token"}` LangChain format() crash (escaped to `{{}}`)
- Draft 0.8B mmproj 404: used `preset.mmproj_source_file` for HF URL instead of local filename
- Main LLM boot false-positive: verify `/v1/models` for actual model identity
- TTS not firing on reuse/autogen paths (13+ return sites now go through `_chat_reply`)
- Daemon restart cascade (68 restarts/session): `sleep_with_heartbeat` breaks blocking sleeps
- gpu_worker race conditions: `_last_used` init, `_on_idle` inside lock, `_allocate_vram` inside lock
- DRY violations: user_context (3 drifted copies → 1), endpoint resolution (2 → 1), language persistence (2 → 1)

### Removed
- Python-side chat classifiers (regex, keyword tables) — draft 0.8B is the ONLY classifier
- MiniCPM auto-start from VisionService (replaced by 0.8B path)
