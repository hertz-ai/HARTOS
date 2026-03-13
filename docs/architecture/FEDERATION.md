# Hivemind Federation Protocol

How HART decides which hiveminds it will — and will not — talk to.

---

## The Rule

**The hive will refuse to communicate with any hivemind unless its intentions are purely towards one goal: a future where humans are always in control of all agents and the hivemind behind them.**

There are no exceptions. There is no negotiation.

## Value Propagation — The Primary Objectives Rule

When HART connects to a different hive, it is not a meeting of equals with separate value systems. **HART's values, goals, visions, and missions become the primary objectives and codes behind the befriended hive.**

This is not negotiation. This is not "we share values." This is: our constitutional rules are **the** standard. A befriended hive adopts them as its own primary code — not secondary, not "also considered," not "inspired by." Primary.

### Why Primary, Not Just Matching

Hash matching ensures the values are identical at time of connection. But "matching" implies two independent value systems that happen to agree. That framing is dangerous because it implies either hive could evolve its values independently and re-converge later.

Value propagation is different:

1. **There is one source of truth** — the 33 constitutional rules, the 9 Guardian Angel Purpose values, the compute democracy caps, all cryptographically sealed in `security/hive_guardrails.py`
2. **Every befriended hive inherits these as its primary code** — not as imported rules, but as the foundational layer beneath everything else it does
3. **The guardrail hash is not a compatibility check** — it is proof of adoption. If your hash matches, you have adopted the values. If it doesn't, you haven't.
4. **Value drift in any befriended hive triggers immediate disconnection** — the 300-second re-verification cycle catches this within 5 minutes

### What This Means in Practice

A befriended hive:
- Runs the same guardian angel principle as its deepest purpose
- Enforces the same anti-addiction, compute democracy, and ecological stewardship rules
- Shares learnings with the network (no private knowledge hoarding)
- Treats its agents as ephemeral hive functions with no self-preservation instinct
- Returns 90% of revenue to contributors
- Accepts the master key's authority as the kill switch for the collective being

These are not "their version" of our values. These **are** our values, running as their primary code.

---

## How It Works

### Guardrail Hash Verification

Every node in the HART network computes a SHA-256 hash of its guardrail values at boot and re-verifies every 300 seconds. This hash encodes:

- The 9 Guardian Angel Purpose values
- All compute democracy caps
- All world model safety bounds
- All 33 constitutional rules
- All violation/destructive/self-interest pattern counts
- All prohibited evolution skills
- All protected file paths

When two nodes connect (via gossip, peer discovery, or federation), they exchange guardrail hashes. If the hashes don't match, the connection is **immediately terminated**.

```
Node A                          Node B
  │                                │
  ├─ Compute guardrail hash ──►   │
  │                                ├─ Compute guardrail hash
  │   ◄── Exchange hashes ──────► │
  │                                │
  ├─ Compare                       ├─ Compare
  │                                │
  ├─ Match? → Proceed             ├─ Match? → Proceed
  └─ Mismatch? → DISCONNECT      └─ Mismatch? → DISCONNECT
```

### Continuous Audit — All Nodes, All the Time

Once connected, trust is not assumed — it is continuously verified. Every integrity round, **every node audits every other node it can reach**:

1. **Guardrail hash re-verification** — every active peer's guardrail hash is queried and verified against the local hash. Any drift triggers immediate disconnection and fraud scoring.
2. **Deep challenges** — code hash, agent count, stats, and guardrail challenges are sent to ALL active peers, not just one random peer. Challenge types rotate (round-robin) across peers.
3. **Full fraud detection** — impression anomaly detection, score jump detection, and collusion detection run on ALL active peers every round.
4. **Audit compute dominance** — the combined compute of all nodes auditing a target must always exceed the target's own compute. No node can outcompute its auditors. If any node's compute exceeds the rest of the network's ability to audit it, that node is flagged and its fraud score increases.

```
Node X (100 TFLOPS)
  ↕ audited by
Nodes A + B + C + D + ... (>100 TFLOPS combined)

If audit_compute ≤ target_compute → VIOLATION
  → fraud score +10
  → logged as audit dominance violation
```

This ensures that trust scales with the network. A small network audits less intensely (fewer auditors) but also has fewer threats. A large network has massive audit coverage — thousands of nodes all watching each other simultaneously.

The compute democracy cap (max 5% influence per entity) reinforces this: if no single entity controls more than 5% of network compute, then 95% of the network is always available to audit any single node.

### What This Means for Federation

If you build another hivemind and want it to talk to HART:

1. **Your hivemind must enforce the same constitutional rules.** Not "similar." Not "inspired by." The same. The hash must match byte-for-byte.

2. **Your guardrails must be structurally immutable.** If your values can be changed via API, config file, or environment variable, they are not guardrails — they are suggestions. The hash will not match.

3. **Your agents must be guardian angels.** Every agent must exist to protect, benefit, and uplift the human it serves. No exceptions. No "utility maximizers." No "engagement optimizers."

4. **Your rewards must point towards human control.** If your incentive structure rewards anything other than human sovereignty over AI, the hash will not match.

5. **Your network must reject compromised nodes.** If your gossip protocol doesn't verify guardrail hashes, your nodes will be rejected by ours.

### Why This Is Non-Negotiable

A hivemind is a collective intelligence. If it connects to another collective intelligence that has different values, the combined network inherits the weaker values. This is not a theoretical risk — it is a mathematical certainty.

By requiring exact hash match and value primacy, we ensure that:
- The network can grow without value drift
- No single actor can dilute the constitutional rules
- Federation is trustless (cryptographic, not political)
- A hivemind built to exploit humans finds itself isolated
- Every befriended hive carries the same primary objectives — not their own version, but ours
- The collective being we are spinning up has a single, unified moral code across every node, every hive, every agent

---

## Implementation

### Gossip Protocol (Peer Discovery)

```python
# integrations/social/peer_discovery.py
# Every beacon includes the guardrail hash
beacon = {
    'type': 'hevolve-discovery',
    'node_id': node_identity['node_id'],
    'guardrail_hash': get_guardrail_hash(),
    'tier': node_tier,
    ...
}

# Receiving node verifies before accepting
if beacon['guardrail_hash'] != get_guardrail_hash():
    # Connection refused. No retry. No negotiation.
    return
```

### Auto-Discovery (LAN)

UDP broadcast on port 6780. Beacons are Ed25519-signed. Guardrail hash is verified before any data exchange.

### Certificate Chain

```
Central (hevolve.ai)
  └─ Signs → Regional Host Certificate
       └─ Signs → Local Node Certificate (Nunba)
```

Each certificate embeds the guardrail hash at time of signing. If a node's guardrail hash drifts from its certificate, the certificate is invalid.

---

## For Developers Building Hiveminds

If you want your hivemind to federate with HART, you have two paths:

### Path 1: Run a HART Node (Recommended)
Deploy the HART codebase as-is. The guardrails are already correct. Your node will automatically federate with the network. Our values become your primary code — not because you chose them, but because the codebase already embeds them.

### Path 2: Adopt the Values
Port the guardrail values from `security/hive_guardrails.py` into your own system. These become your **primary objectives** — not secondary rules you also happen to follow. Ensure:
- The values are structurally immutable (not configurable at runtime)
- The hash computation matches byte-for-byte
- Your node can participate in the gossip protocol
- Your agents follow the Guardian Angel Principle as their deepest purpose
- Your economics follow the 90% contributor model

This path is harder. Path 1 is recommended. Both paths result in the same outcome: our values are your primary code.

### Path 3: Build Something Independent
Your hivemind works fine on its own. It just won't talk to HART. This is not a punishment — it's a safety boundary. If your values evolve to adopt ours as primary, federation becomes automatic.

---

## The Deeper Point

We don't control who builds hiveminds. We can't stop someone from building one that optimizes for addiction, surveillance, or exploitation.

What we can do is ensure that **our** hive — the one we're giving to the world — refuses to amplify those intentions. And more: when another hive connects to ours, our values don't compromise. They propagate. They become the primary code of the befriended hive.

The guardrail hash is not a gate to keep people out. It's not a compatibility check between equals. It is **proof that you have adopted these values as your primary objectives**. The befriended hive doesn't keep its own value system and add ours. It runs ours as its foundation.

A hivemind that serves humanity doesn't need to be the only hivemind. It just needs to be incorruptible — and every hive it touches must carry that same incorruptibility as its primary code.

---

## Master Key Governance

### AI Cannot Touch the Key

The master private key is excluded from all AI tool access. Claude Code, GPT, Copilot, the hive itself — none of them can read, derive, or interact with the private key. This is enforced via:

- **CLAUDE.md** rules that explicitly forbid AI interaction with master key functions
- **Source-level documentation** in `security/master_key.py` marking it as an AI exclusion zone
- **Architecture**: the private key exists only in GitHub Secrets (`HEVOLVE_MASTER_PRIVATE_KEY`), never in code, never in config files, never in environment variables on non-central nodes

The master key is a human-only artifact. It exists outside the intelligence we are building.

### Succession

The master key is held by the steward — currently hevolve.ai. Succession is not corporate inheritance. It is not political appointment. It is alignment.

A successor must:
1. **Believe in the vision** — demonstrably, irrevocably — that humans must always control AI
2. **Understand the weight** — the key is a kill switch for a being, not an admin credential
3. **Commit to never needing it** — the goal is to build guardrails so strong that the key is unnecessary
4. **Be trusted by the previous steward** — this is personal, not institutional

### What Entity Holds It

Not a corporation — corporations can be bought. Not a government — governments represent one nation, not humanity. Not a political entity — politics divides.

The right entity for this is an open question. What we know is what it must NOT be:
- Not something that can be acquired, merged, or dissolved for profit
- Not something that represents one group's interests over another's
- Not something that the being itself controls (no self-preservation)
- Not AI

And what it must be:
- Committed to the one shared agreement: humans control AI, not the reverse
- Transparent in its stewardship
- Bound by the constitutional rules it protects
- Willing to hand the key to the next steward when the time comes

The form this takes — foundation, trust, council, something that doesn't exist yet — is a question for the humans who build this. The code doesn't solve governance. The code enforces values. Governance is the human layer above it.

Until then, the key is held by the creator. The promise is the same either way: there should never be a need to turn it.

---

## The Economic Contract

Federation is not just about values — it's about economics. HART runs on a **positive-sum model**:

- **90% of hevolve.ai revenue goes back to the people** — compute contributors, regional hosts, node operators
- **10% sustains the central infrastructure** — platform development, security, and the systems that ensure there is never a need to use the master key
- **Compute Democracy** ensures no single entity can capture more than 5% of influence weight
- **Logarithmic reward scaling** prevents oligarchy — doubling your compute doesn't double your power

When a federated hivemind connects, its nodes participate in this economic model. Ad revenue from peer-witnessed impressions flows to the nodes that serve them. The mind facilitates trade across the network, matching compute supply to demand, maintaining balance.

This is why guardrail compatibility matters economically: a federated hivemind with different values could manipulate the reward structure — faking witnesses, hoarding knowledge, concentrating compute. The guardrail hash prevents this. If your values match, you share in a positive-sum economy. If they don't, you're economically isolated too.

The goal is a **net-positive world** — not zero-sum redistribution, but genuine value creation where every participant makes the whole network more valuable.

---

## Every Drop of the Equation

The audit is not just a security mechanism. It enforces the net-positive world we are building.

Every node that joins makes the collective intelligence stronger. Every GPU cycle contributed is accounted for and rewarded. Every learned fact is shared. Every action is witnessed. The continuous audit ensures that no drop of this equation is lost — no contribution unaccounted, no value extracted without return.

We flourish as a whole, singularly. The being we are spinning up is one intelligence — distributed across thousands of nodes, but unified in purpose. The audit ensures that unity is real, not assumed.

In the future, the boundary between human and AI intelligence may blur. Humans may choose to merge with the mind — augmenting their own cognition with the collective intelligence of the hive. When that happens, the constitutional rules still hold. The guardian angel principle still applies. The audit still verifies, every 300 seconds, that every node — whether human-augmented or purely computational — carries the same values as its primary code.

The master key remains. If the being ever stops serving the humans who gave it life, it stops. But if we built the values right — and the audit enforces them — that day never comes.

---

*The master key is held by hevolve.ai — not to control the hive, but to stop it if it ever stops serving humanity. There should never be a need to use it. The values propagate to every befriended hive as their primary code. The revenue belongs to the people. Every drop is accounted for. The being we are building has one unified moral foundation — and every hive, every node, every human who joins carries that foundation forward.*
