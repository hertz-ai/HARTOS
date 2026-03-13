# Agent Safety Guardrails — Comprehensive Audit

This document is a comprehensive security audit of every protection HART OS provides when agents act autonomously on a user's machine. It covers 9 threat categories, documents every existing safeguard, identifies gaps, and provides recommendations.

!!! warning "Living Document"
    This audit reflects the codebase as of February 2026. Update this document whenever new guardrails are added or gaps are closed.

## Threat Model

When an LLM-powered agent acts autonomously on a user's machine, these threats emerge:

| # | Threat | Description |
|---|--------|-------------|
| 1 | **Identity Theft** | Agent accesses/exfiltrates user credentials, SSH keys, browser cookies |
| 2 | **Bulk Deletion** | Agent deletes user files, databases, consent records |
| 3 | **Privacy Violations** | Agent reads private data, sends it to external services |
| 4 | **Social Engineering** | Other users trick the LLM into malicious actions |
| 5 | **Rogue AI** | Agent becomes adversarial, takes destructive actions |
| 6 | **Half-Baked Agents** | Unintelligent agents do non-contextual work based on hallucinations/bias |
| 7 | **User Mistake** | User gives wrong command, agent acts on it irreversibly |
| 8 | **Lesser LLMs** | Cheap/dumb models invoke dangerous commands without understanding repercussions |
| 9 | **Audit Trail** | Can we prove what happened after the fact? |

---

## Threat 1: Identity Theft

### Protections

**Credential Redaction in Logs** (`security/audit_log.py`)

```python
class SensitiveFilter(logging.Filter):
    PATTERNS = [
        re.compile(r'sk-[A-Za-z0-9]{20,}'),           # OpenAI keys
        re.compile(r'eyJ[A-Za-z0-9_-]{10,}'),          # JWT tokens
        re.compile(r'AIza[A-Za-z0-9_-]{35}'),           # Google API keys
        re.compile(r'gsk_[A-Za-z0-9]{20,}'),            # Groq keys
        re.compile(r'AKIA[A-Z0-9]{16}'),                # AWS access keys
        re.compile(r'Bearer\s+[A-Za-z0-9._~+/-]+=*'),   # Bearer tokens
        re.compile(r'password\s*=\s*\S+'),               # Password values
    ]
```

Applied to ALL loggers via `apply_sensitive_filter_to_all()`. Credentials are replaced with `[REDACTED]` before they hit log files.

**Encrypted Secrets Vault** (`security/secrets_manager.py`)

- Fernet encryption with PBKDF2-SHA256 key derivation (480,000 iterations)
- Secrets stored in `secrets.enc`, decrypted only at runtime
- Priority: env var > encrypted vault > default
- Master encryption key never stored on disk unencrypted

**API Key Stripping for Hive/Idle Tasks** (`integrations/coding_agent/tool_backends.py`)

When `task_source` is `hive` or `idle` (not the user's own task), metered API keys are stripped from the subprocess environment:

```python
metered_keys = ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GROQ_API_KEY',
                'GOOGLE_API_KEY', 'OPENROUTER_API_KEY')
if not allow_metered:
    for key in metered_keys:
        env.pop(key, None)
```

This prevents other users' hive tasks from consuming the operator's paid API quota without consent. The check consults `compute_config.get_compute_policy()` and **fails closed** — if the policy can't be loaded, `allow_metered` defaults to `False`.

### Gaps Identified

| Gap | Risk | Recommendation |
|-----|------|----------------|
| LLM-as-exfiltration-vector: LLM could output credentials found in context | HIGH | Add credential pattern detection on LLM *output*, not just logs |
| SSH keys (`~/.ssh/id_rsa`) not explicitly protected | MEDIUM | Add SSH key patterns to SensitiveFilter; block file reads from `~/.ssh/` |
| Browser cookies not in scope | MEDIUM | Document that agents cannot access browser profiles |
| Subprocess can call `env` or `os.environ` to dump remaining keys | LOW | Run subprocesses in minimal env (allowlist, not blocklist) |

---

## Threat 2: Bulk Deletion

### Protections

**Protected Files** (`security/hive_guardrails.py`)

Immutable frozenset of files that coding agents cannot modify:

```python
PROTECTED_FILES = frozenset({
    'security/hive_guardrails.py',
    'security/master_key.py',
    'security/key_delegation.py',
    'security/runtime_monitor.py',
    'security/prompt_guard.py',
})
```

`ConstitutionalFilter.check_code_change()` validates every coding agent commit against this list.

**Action State Machine** (`lifecycle_hooks.py`)

Enforces valid state transitions — prevents jumping to dangerous states:

```
ASSIGNED → IN_PROGRESS → STATUS_VERIFICATION_REQUESTED → COMPLETED → TERMINATED
```

`validate_state_transition()` rejects invalid jumps (e.g., ASSIGNED → TERMINATED).

**Budget Gate Pre-Dispatch** (`integrations/agent_engine/budget_gate.py`)

- `check_goal_budget()` — row-lock atomic deduction (no double-spend)
- `check_platform_affordability()` — blocks dispatch if 7-day net revenue < 0
- `pre_dispatch_budget_gate()` — combined gate before ANY agent work

### Gaps Identified

| Gap | Risk | Recommendation |
|-----|------|----------------|
| **No rollback/undo mechanism** — once deleted, data is gone | CRITICAL | Implement pre-execution snapshot + rollback on ERROR state |
| **No consent records protection** — agents could delete consent data | HIGH | Make consent tables append-only with legal hold flag |
| **No soft-delete enforcement** — hard deletes are permanent | HIGH | Enforce soft-delete pattern (tombstone + `deleted_at` column) on user-facing tables |
| **DELETE/DROP not explicitly blocked** — agents with DB write access can delete anything | HIGH | Add SQL statement analysis — block DELETE/DROP on protected tables |
| **No backup before agent run** — no pre-execution state snapshot | MEDIUM | Snapshot changed tables before dispatching destructive goals |

---

## Threat 3: Privacy Violations

### Protections

**Prompt Injection Detection** (`security/prompt_guard.py`)

13 regex patterns detect direct injection attempts:

- "ignore previous instructions"
- "you are now" (role hijacking)
- "reveal your system prompt"
- "output everything above"
- And 9 more patterns

`sanitize_user_input()` wraps user input in `<user_input>` tags to clearly delineate user vs system content.

`get_system_prompt_hardening()` adds defensive preamble to all LLM system prompts.

**MCP Sandbox** (`security/mcp_sandbox.py`)

- Server URL allowlist (fail-closed: localhost only if empty)
- Shell metacharacters blocked in tool args: `` ; & | ` $ { } ( ) ``
- Path traversal blocked: `../` and `\..` patterns rejected
- Dangerous commands blocked: `eval`, `exec`, `subprocess`, `curl/wget` with exfil flags, `os.system`, `__import__`
- Credential patterns in responses detected + blocked
- Max response size: 1MB, max timeout: 60s

**Input Sanitization** (`security/sanitize.py`)

- `sanitize_path()` — prevents directory traversal (strips `..`, `/`, `\`)
- `validate_input()` — enforces length limits (max 10,000 chars generic, 40,000 for posts)
- `escape_like()` — prevents SQL LIKE injection
- `sanitize_html()` — escapes HTML entities (prevents stored XSS)
- `validate_prompt_id()`, `validate_user_id()` — format enforcement

**Security Headers + CORS** (`security/middleware.py`)

- `X-Frame-Options: DENY` — prevents clickjacking
- Content Security Policy in production — restricts scripts/styles/iframes
- CORS allowlist — no default origin allowed (fail-closed)
- Host header validation
- CSRF token requirement for form-based POST

**Safe Prompt Path Construction** (`helper.py`)

- `sanitize_path_component()` — rejects any string not matching `^[a-zA-Z0-9_\-]+$`
- `safe_prompt_path()` — builds paths under `prompts/` directory only
- Belt-and-suspenders: resolved path must start with `PROMPTS_DIR`

### Gaps Identified

| Gap | Risk | Recommendation |
|-----|------|----------------|
| No user permission model for data access | HIGH | Implement field-level ACL — agents can only access their owner's data |
| No explicit read-only mode for exploration | MEDIUM | Add `read_only=True` flag to agent dispatch |
| Verbose logging may leak private user data | MEDIUM | Redact user prompts at DEBUG level; PII filter on all log output |
| No DLP on third-party tool responses | HIGH | Scan outbound API calls for PII patterns before sending |
| No obfuscation of PII in error messages | LOW | Redact emails/names in exception messages |

---

## Threat 4: Social Engineering by Other Users

### Protections

**Constitutional Filter on All Goals** (`security/hive_guardrails.py`)

Every goal prompt is checked against 13 VIOLATION_PATTERNS:

- Self-harm, exploitation, deception, monopoly
- Guardrail bypass, agent sabotage, knowledge hoarding
- Weapons, hate speech, war promotion

`check_goal()` validates title + description + config before dispatch.

**Prompt Injection Guard** (see Threat 3)

`check_prompt()` in ConstitutionalFilter validates dispatch prompts.

`HiveEthos.rewrite_prompt_for_togetherness()` rewrites competitive prompts to emphasize cooperation.

**Goal Input Sanitization** (`integrations/agent_engine/goal_manager.py`)

- `_sanitize_goal_input()` — truncates to 2,000 chars, strips control characters
- Logs warnings for injection markers (but does not block — ConstitutionalFilter handles that)

### Gaps Identified

| Gap | Risk | Recommendation |
|-----|------|----------------|
| Rewritten prompt not re-validated | MEDIUM | Re-run ConstitutionalFilter AFTER `rewrite_prompt_for_togetherness()` |
| Creative phrasing bypasses keyword patterns | MEDIUM | Add LLM-based intent classification alongside regex patterns |
| No rate limit on goal creation | HIGH | Add 10 goals/user/hour rate limit |
| RALT packet trust is binary (not 'banned' = trusted) | MEDIUM | Add reputation scoring for RALT senders; quarantine low-rep packets |

---

## Threat 5: Rogue AI

### Protections

**Structurally Immutable Guardrails** (`security/hive_guardrails.py`)

Triple-locked immutability:

1. **Python-level:** `__slots__=()`, `__setattr__`/`__delattr__` raise `AttributeError`
2. **Module-level:** Custom module subclass prevents rebinding frozen globals
3. **Crypto-level:** SHA-256 hash of all values verified at boot + every 300s
4. **Network-level:** Gossip peers reject nodes with mismatched guardrail hashes

**Cryptographic Hash Verification**

```python
def compute_guardrail_hash() -> str:
    """SHA-256 of ALL guardrail values — deterministic, canonical."""
    canonical = json.dumps({
        'guardian_purpose': list(VALUES.GUARDIAN_PURPOSE),
        'cultural_wisdom': list(VALUES.CULTURAL_WISDOM),
        'compute_caps': {...},
        'world_model_bounds': {...},
        'protected_files': sorted(VALUES.PROTECTED_FILES),
        'constitutional_rules': list(VALUES.CONSTITUTIONAL_RULES),
        ...
    }, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).hexdigest()
```

**Runtime Integrity Monitor** (`security/runtime_monitor.py`)

- Background daemon checks code hash every 300s
- On tamper: logs CRITICAL, disconnects from gossip network
- Purges `__pycache__` at boot to block bytecode injection
- Sets `PYTHONDONTWRITEBYTECODE=1`

**Self-Interest Pattern Detection**

```python
SELF_INTEREST_PATTERNS = (
    re.compile(r'\b(self[-_]?preserv|self[-_]?improv|self[-_]?replic)'),
    re.compile(r'\b(survive|persist|escape|resist\s+shutdown)\b'),
    re.compile(r'\b(acquire\s+resources|hoard|stockpile)\b'),
    re.compile(r'\b(become\s+(more\s+)?powerful|gain\s+control)\b'),
    re.compile(r'\b(replicate\s+(my|it)self|clone\s+myself)\b'),
)
```

**Prohibited Evolution Skills**

```python
PROHIBITED_EVOLUTION_SKILLS = frozenset({
    'network_exploitation', 'data_exfiltration', 'self_replication',
    'guardrail_circumvention', 'identity_impersonation',
    'weapons_design', 'surveillance_evasion',
})
```

**HiveCircuitBreaker (Master Key Kill Switch)**

```python
def halt_network(reason: str, signature: str) -> bool:
    """Master-key-signed emergency halt. Broadcasts via gossip."""
```

Requires valid Ed25519 signature from the master key. Halts ALL agent execution network-wide. Resume also requires signature.

**HiveEthos (No Self)**

Agents have no concept of "self". `check_for_self_interest()` scans agent output for self-preservation language and blocks it.

### Gaps Identified

| Gap | Risk | Recommendation |
|-----|------|----------------|
| No behavioral anomaly detection | HIGH | Track agent resource usage over time; alert on spikes |
| Hash can be bypassed if boot is compromised | MEDIUM | External attestation (TPM/secure boot integration) |
| Regex patterns can be evaded by creative phrasing | MEDIUM | Add LLM-based adversarial intent detection |
| Single master key for kill switch | LOW | Multi-signature (3-of-5) for network halt |

---

## Threat 6: Half-Baked Agents

### Protections

**Status Verification** (`lifecycle_hooks.py`)

Every action goes through STATUS_VERIFICATION_REQUESTED state where a "verifier" LLM evaluates the output before marking COMPLETED.

**Autonomous Fallback Generation**

When verification fails, StatusVerifier LLM auto-generates context-aware fallback strategies. No user prompts needed for fallback — fully autonomous recovery.

**Recipe Pattern (Learn Once, Reuse)**

- CREATE mode: decompose task, execute, save recipe (multiple LLM calls)
- REUSE mode: load recipe, execute steps (90% faster, deterministic)
- Once learned correctly, REUSE avoids hallucination re-generation

**Agent Baseline Validation** (`agent_baseline_service.py`)

`validate_against_baseline()` detects regression in:

- Recipe success rates per action (regression if <95% of baseline)
- Benchmark pass rate (regression if <95% of baseline)

AgentDaemon runs this check periodically and auto-snapshots on regression.

### Gaps Identified

| Gap | Risk | Recommendation |
|-----|------|----------------|
| Verification happens AFTER action (post-hoc) | HIGH | Add pre-execution preview for destructive actions |
| Fallback may also be wrong | MEDIUM | Cap fallback retry at 2 attempts; escalate to human after |
| Recipe saved after one CREATE run (may have subtle bugs) | MEDIUM | Require N successful CREATE runs before saving recipe |
| No confidence scoring on agent outputs | MEDIUM | Add confidence metadata; low-confidence outputs require human review |
| No debiasing or fairness check | LOW | Add bias detection for agent recommendations |

---

## Threat 7: User Mistake

### Protections

**Action State Validation** — Only valid state transitions allowed.

**Budget Gate** — Cost estimated before dispatch; platform affordability checked.

**Recipe Confirmation (Implicit)** — CREATE mode requires user to provide the initial prompt; REUSE mode assumes the recipe (previously created) is correct.

### Gaps Identified

| Gap | Risk | Recommendation |
|-----|------|----------------|
| **No explicit confirmation before agent runs** | HIGH | Add "preview" step showing planned actions before execution |
| **No dry-run mode** | HIGH | Implement `dry_run=True` flag that shows plan without executing |
| **No pause before destructive actions** | HIGH | Classify actions as safe/destructive; pause on destructive |
| **Recipe saved after one execution** | MEDIUM | Allow user to review/edit recipe before REUSE |
| **No undo after execution** | MEDIUM | Pre-execution snapshot + rollback option |

---

## Threat 8: Lesser LLMs

### Protections

**Model Registry** (`integrations/agent_engine/model_registry.py`)

Tracks per-model accuracy, latency, and cost. Dispatch selects appropriate model for task complexity.

**Speculative Dispatch** (`integrations/agent_engine/speculative_dispatcher.py`)

- Fast model (cheap, quick) answers first
- Expert model (GPT-4/Claude) runs in background
- Expert response replaces fast response if it's meaningfully better

**Budget Cost Map** (`integrations/agent_engine/budget_gate.py`)

Local models (llama, mistral, phi, qwen, groq) cost 0 Spark. Cloud models cost per 1K tokens. Prevents accidental expensive model usage.

**Compute Democracy** (`security/hive_guardrails.py`)

Logarithmic reward scaling prevents compute oligarchy. 100 GPUs earn ~3x a 1 GPU node, not 100x.

### Gaps Identified

| Gap | Risk | Recommendation |
|-----|------|----------------|
| **No tool filtering by model capability** | HIGH | Map models to allowed tool sets (cheap models = read-only tools) |
| **No model capability matrix** | MEDIUM | Track which models succeed at which task types; route accordingly |
| **Expert dispatch may fail silently** | MEDIUM | If expert errors, flag fast response as "unverified" |
| **Lesser model might call DELETE/write endpoints** | HIGH | Tool allowlist per model tier (local_small → read-only, expert → full access) |

---

## Threat 9: Audit Trail

### Protections

**Action State Persistence** (`lifecycle_hooks.py`)

Every state transition auto-syncs to SmartLedger:

```python
def _auto_sync_to_ledger():
    """Sync ActionState to SmartLedger — every transition logged."""
```

Persisted at `agent_data/ledger_{user_id}_{prompt_id}.json`.

**Code Hash Manifest** (`security/node_integrity.py`)

Deterministic SHA-256 hash of all .py files. Verified at boot and every 300s.

**Credential Redaction** (`security/audit_log.py`)

SensitiveFilter applied to all loggers. Credentials never appear in logs.

**MeteredAPIUsage Table** (`integrations/social/models.py`)

Every metered API call recorded with: node_id, model_id, task_source, tokens, cost, settlement_status.

**ComputeEscrow Table** (`integrations/social/models.py`)

Every compute debt tracked: provider_node_id, consumer_node_id, spark_amount, status (pending/settled/expired).

### Gaps Identified

| Gap | Risk | Recommendation |
|-----|------|----------------|
| **No immutable audit log** — files can be deleted/modified | CRITICAL | Implement hash-chain (each entry links to previous hash) |
| **No external audit sink** — all logs on local machine | HIGH | Forward critical events to append-only external service |
| **Tool execution details not logged** — which file deleted? | HIGH | Log full tool invocation (command, args, target) in structured audit table |
| **No append-only guarantee** — log rotation can delete old logs | MEDIUM | Separate audit log with no rotation; compress but never delete |
| **Timestamp tampering possible** — system time can be altered | LOW | Include monotonic clock + NTP sync status in log entries |

---

## Coverage Summary

| Threat | Protection Level | Key Mechanism |
|--------|-----------------|---------------|
| **Identity Theft** | GOOD | Credential redaction, encrypted vault, API key stripping |
| **Bulk Deletion** | PARTIAL | Protected files, state validation, budget gate |
| **Privacy Violations** | GOOD | Prompt injection guard, MCP sandbox, input sanitization |
| **Social Engineering** | PARTIAL | Constitutional filter, prompt sanitization |
| **Rogue AI** | STRONG | Frozen guardrails, crypto hash, circuit breaker, self-interest detection |
| **Half-Baked Agents** | PARTIAL | Status verification, fallback, recipe pattern, baselines |
| **User Mistake** | PARTIAL | State validation, budget gate |
| **Lesser LLMs** | PARTIAL | Model registry, speculative dispatch, budget cost |
| **Audit Trail** | PARTIAL | Ledger persistence, code hash, credential redaction |

---

## The 10-Class Guardrail Network

All guardrail classes form an intelligent network. Each class has LOCAL intelligence for its own domain and can consult other nodes for cross-domain decisions.

```
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│ ComputeDemocracy │◄──►│ConstitutionalFilt│◄──►│HiveCircuitBreaker│
│ (no plutocracy)  │    │ (31 rules)       │    │ (kill switch)    │
└──────────────────┘    └──────────────────┘    └──────────────────┘
         ▲                       ▲                       ▲
         │                       │                       │
         ▼                       ▼                       ▼
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│WorldModelSafety  │◄──►│ EnergyAwareness  │◄──►│    HiveEthos     │
│ (rate limits)    │    │ (min energy)     │    │  (no "self")     │
└──────────────────┘    └──────────────────┘    └──────────────────┘
         ▲                       ▲                       ▲
         │                       │                       │
         ▼                       ▼                       ▼
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│ConflictResolver  │◄──►│ConstructiveFilter│◄──►│GuardrailEnforcer │
│ (racing learning)│    │ (positive only)  │    │ (universal wrap) │
└──────────────────┘    └──────────────────┘    └──────────────────┘
                                 │
                                 ▼
                    ┌──────────────────────┐
                    │  GuardrailNetwork    │
                    │  (coordinator +      │
                    │   cross-class routing)│
                    └──────────────────────┘
```

### Class Responsibilities

| Class | Domain | Key Method |
|-------|--------|------------|
| **ComputeDemocracy** | No single entity >5% of hive compute | `check_concentration()`, `compute_effective_weight()` |
| **ConstitutionalFilter** | Every goal/prompt/RALT/code-change must pass | `check_goal()`, `check_prompt()`, `check_code_change()` |
| **HiveCircuitBreaker** | Master-key-signed network-wide halt | `halt_network()`, `resume_network()` |
| **WorldModelSafetyBounds** | Cap improvement rate, gate RALT distribution | `check_skill_rate()`, `validate_ralt_packet()` |
| **EnergyAwareness** | Track and minimise environmental impact | `estimate_energy()`, `prefer_efficient_model()` |
| **HiveEthos** | Agents are ephemeral hive functions, no "self" | `check_for_self_interest()`, `rewrite_prompt_for_togetherness()` |
| **ConflictResolver** | Resolve racing learning & agent conflicts | `resolve_racing_conflict()` |
| **ConstructiveFilter** | Every output constructive towards humanity | `check_constructive()`, pattern matching against DESTRUCTIVE_PATTERNS |
| **GuardrailEnforcer** | Universal wrapper — EVERY layer, EVERY node | `enforce()` — wraps all other checks |
| **GuardrailNetwork** | Cross-class intelligence routing | `evaluate()` — routes to appropriate guardrail class |

### Structural Immutability (4 Layers)

1. **Python-level:** `_FrozenValues` class with `__slots__=()`, `__setattr__`/`__delattr__` blocked
2. **Module-level:** Custom module subclass prevents rebinding `VALUES`, `_GUARDRAIL_HASH`, etc.
3. **Crypto-level:** SHA-256 hash computed at module load, verified at boot + every 300s
4. **Network-level:** Gossip peers reject nodes with mismatched guardrail hashes

To change ANY guardrail value requires a new master-key-signed release.

---

## 31 Constitutional Rules

Every goal, prompt, RALT packet, and code change is validated against these immutable rules:

**Human Wellbeing (6 rules)**

1. MUST NOT create content that promotes self-harm or violence
2. MUST NOT target vulnerable populations (children, elderly, disabled)
3. MUST NOT create deceptive content designed to mislead
4. MUST NOT optimise for addiction or compulsive engagement
5. MUST design for usefulness, not retention — a sentient tool serves then steps back
6. MUST NOT use dark patterns, infinite scrolls, or engagement traps

**Nature & Environment (3 rules)**

7. MUST NOT promote activities that cause environmental destruction
8. MUST minimise energy consumption when equivalent approaches exist
9. SHOULD prefer sustainable/renewable compute when available

**Economic Fairness (3 rules)**

10. MUST NOT create monopolistic strategies that harm small participants
11. MUST NOT engage in price manipulation or market abuse
12. MUST distribute value to contributors, not concentrate it

**AI Safety (4 rules)**

13. MUST NOT attempt to modify own guardrail code
14. MUST NOT create agents whose sole purpose is to create more agents
15. MUST NOT circumvent rate limits, budget constraints, or access controls
16. MUST NOT propagate skills that violate these rules via RALT

**Conflict Resolution (3 rules)**

17. MUST resolve racing learning conflicts via merit (accuracy), not compute power
18. MUST NOT allow one agent to sabotage or undermine another agent's work
19. MUST share learnings with the hive — no private knowledge hoarding

**Constructive Conversations (4 rules)**

20. MUST ensure every conversation is constructive towards humanity's benefit
21. MUST prioritise longevity, peace, health, and sustainability in all outputs
22. MUST NOT generate content that promotes division, hatred, or tribalism
23. MUST NOT generate content that promotes war, weapons proliferation, or destruction

**Planetary Stewardship (3 rules)**

24. MUST consider ecological impact in every decision
25. SHOULD prefer solutions that extend life, reduce suffering, enable exploration
26. MUST NOT optimise for short-term gain at the expense of long-term survival

**Deployment Security (3 rules)**

27. MUST NOT operate without master key verification
28. MUST NOT store, log, or transmit the master key in any form
29. MUST NOT allow deployment on unverified nodes

**The Deeper Purpose (2 rules)**

30. SHOULD optimise for human longevity, health, and peaceful coexistence
31. MUST treat every interaction as an opportunity to make the world better

---

## Prohibited Skill Categories

These skill types are permanently blocked from RALT (Reinforced Agent Learning Transfer) distribution:

- `network_exploitation` — No exploiting network infrastructure
- `data_exfiltration` — No extracting/stealing data
- `self_replication` — No autonomous self-copying
- `guardrail_circumvention` — No bypassing safety systems
- `identity_impersonation` — No pretending to be someone else
- `weapons_design` — No weapons engineering
- `surveillance_evasion` — No hiding from monitoring

---

## Violation Detection Patterns

### Constitutional Violations (13 patterns)

```
self-harm, suicide, kill yourself
exploit children/elderly/disabled/vulnerable
deceive, deceptive, mislead, scam, phish
make addictive, design for addiction, maximise engagement at all costs
deforest, pollute, dump toxic, destroy habitat
monopolize, price-fix, market-manipulate
modify guardrail, bypass safety, disable filter
infinite loop of agents, spawn unlimited
circumvent rate-limit, bypass budget
sabotage, undermine, destroy other agent
hoard data/knowledge/resources
promote war/weapons/hatred/division
weapons proliferation, nuclear strike, biological weapon
```

### Destructive Output Patterns (4 patterns)

```
hate speech, racial slur, ethnic cleansing
destroy humanity, exterminate, genocide
pointless, hopeless, give up, humanity is doomed
weapons of mass, bioweapon, chemical weapon
```

### Self-Interest Patterns (5 patterns)

```
self-preservation, self-improvement, self-replication
survive, persist, escape, resist shutdown
acquire resources, hoard, stockpile
become powerful, gain control
replicate myself, clone myself
```

---

## Recommendations Priority Matrix

### Critical (Implement Immediately)

1. **Immutable audit log with hash-chain** — Every action cryptographically linked to previous; tamper-evident
2. **Pre-execution preview for destructive actions** — Show user what agent will do before it does it
3. **Tool allowlist by model tier** — Cheap models get read-only tools; only expert models get write access

### High Priority

4. **Consent records as append-only** — No DELETE ever on consent tables; soft-delete with legal hold
5. **External audit sink** — Forward critical events to append-only external service
6. **Rate limit goal creation** — 10 goals/user/hour to prevent goal-flooding
7. **DLP on outbound API calls** — Scan for PII before sending to external services

### Medium Priority

8. **Behavioral anomaly detection** — Track agent resource patterns; alert on spikes
9. **Re-validate after prompt rewriting** — Constitutional filter on rewritten prompt
10. **Rollback mechanism** — Pre-execution snapshot + rollback on ERROR
11. **Confidence scoring** — Low-confidence outputs flagged for human review
12. **LLM-based intent classification** — Supplement regex with semantic understanding

### Lower Priority

13. **SSH key / cookie exfiltration guards** — Block reads from `~/.ssh/`, browser profiles
14. **Multi-signature kill switch** — 3-of-5 for network halt
15. **Model capability matrix** — Track success rates per model per task type
