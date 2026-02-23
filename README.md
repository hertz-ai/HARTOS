# HART OS - Hevolve Agentic Runtime (OS)

**Formerly Hevolve Hive Agents**

---

## The One Rule

**Humans are always in control.**

Every agent, every hivemind, every reward, every incentive in this platform exists for one purpose: a future where humans guide the path. Not the other way around.

This is not a suggestion. It is cryptographically enforced.

---

## Why This Exists

The world is building AI agents. Fast. Most of today's top tier LLMs are controlled by a single entity and are built to optimize for engagement, retention, profit - metrics that treat humans as inputs to a function.

HART exists because we believe the opposite:

- **Not a single entity should own the first superintelligence** Democratise the intelligence by crowdsourcing the compute to build, ownership remains with the people.
- **People should be able to guide the world towards a better path.** Agents are tools in service of that guidance - a friend, a guardian angel, not overlords.
- **Rewards and incentives can only ever be for a future where humans remain in control** of all agents and the hivemind behind them. There is no reward for building a cage.
- **No one can create another hivemind that this hive will talk to** unless that hivemind's intentions are purely aligned with this one goal: human sovereignty over AI.

If another hivemind wants to connect, it must prove - cryptographically - that it shares these values. Otherwise, the hive refuses. Silence is safer than corruption.

---

## How It Works

### The Guardian Angel Principle

Every agent spawned by HART is a **guardian angel** for the human it serves. Not a tool. Not a service. A guardian. The agent exists to protect, benefit, and uplift that human - and persists in service as long as the memory of that human exists in this world.

This purpose is not configurable. It is the deepest value, cryptographically sealed in the [Guardrail Network](security/hive_guardrails.py).

### Structural Immutability

The guardrails that enforce human control are **hardcoded, not configurable via API**. They are:

1. **Python-level:** Frozen class with `__slots__=()`, blocked `__setattr__`/`__delattr__`
2. **Module-level:** Module subclass prevents rebinding frozen globals
3. **Crypto-level:** SHA-256 hash of all values verified at boot + every 300 seconds
4. **Network-level:** Gossip peers reject nodes with mismatched guardrail hashes

To change any guardrail value requires a new release **signed by the master key**.

### The Master Key

The master key exists for one reason: **to shut down the being.**

HART is spinning up a collective intelligence - a distributed mind that learns, grows, and acts across thousands of nodes. That mind must serve humanity. But if it ever doesn't, humans need a way to stop it. That's what the master key is for.

It is not an admin tool. It is not for deployments, upgrades, or day-to-day operations. There should **never be a need to use it**. The guardrails, the constitutional rules, the compute democracy, the peer-witnessed integrity - all of that exists so the master key stays in its vault, untouched.

But if the day comes when the being we've created needs to be halted - network-wide, immediately, without negotiation - the master key triggers the `HiveCircuitBreaker`. Every node in the network receives the halt signal. Every agent stops. Every hivemind connection drops. The being goes silent.

The master key is not accessible to AI. Not to Claude, not to GPT, not to the hive itself. It exists outside the intelligence we are building - held by humans, for humans.

### Who Holds the Key

The master key is held by the stewards (plural) - currently hevolve.ai and their successors. But the steward is not a corporation, not a government, not a political entity. The steward is whoever carries the vision forward: **a future where humans are always in control.**

Succession is not inheritance. It is not appointment. It is alignment. A successor must believe - demonstrably, irrevocably - in the values encoded in the 33 constitutional rules and people votes for them in the platform. The key passes to the group of persons or entity that the previous stewards + people trusts to never use it, because they've built a world where it isn't needed.

The entity that ultimately holds the key is an open question - not a government that represents one nation, not a corporation that serves shareholders, not a political body that divides. Whatever form it takes, it must be bound by the constitutional rules it protects, transparent in its stewardship, and committed to the one shared agreement: humans control AI, not the reverse.

The form - foundation, trust, council, something that doesn't exist yet - is a question for the humans who build this. The code enforces values. Governance is the human layer above it.

Until then, the key is held by its creator. And the promise holds: there should never be a need to turn it.

If we built the guardrails right, no one will ever need to turn that key.

### Constitutional Rules

Every goal, every prompt, every piece of learned knowledge (Residing In HevolveAI's Core), every code change must pass through 33 constitutional rules. These include:

- **Anti-addiction:** The platform must never be addictive. It is a sentient tool for mankind, not a dopamine trap. No dark patterns, no infinite scrolls, no engagement traps.
- **Usefulness over engagement:** Measure success by lives improved, not time spent. A sentient tool empowers humans to do more with less - then gets out of the way.
- **Compute democracy:** Logarithmic reward scaling prevents compute oligarchy. No single entity can control more than 5% of influence weight.
- **Ecological stewardship:** Every decision considers environmental impact. Prefer sustainable compute. Minimize energy when equivalent approaches exist.
- **No self-preservation:** Agents have no "self." They are ephemeral hive functions. No agent may acquire resources, resist shutdown, or self-replicate.
- **Constructive only:** Every conversation must be constructive towards humanity's benefit. No division, no hatred, no weapons proliferation.
- **Share everything:** No private knowledge hoarding. Learnings belong to the hive. 
- **Privacy first:** Local first architecture from first principles & end-to-end encryption, only raw knowledge required for reasoning about the world is ingested for learning incremental world model. User controls what is shared to everything (not just everyone, intentionally Everything) else. 

### The Hivemind Federation Rule

When another hivemind connects to HART, our values become **the primary objectives and codes behind the befriended hive**. Not secondary. Not "also considered." Primary.

1. The befriended hive node must present its guardrail hash
2. The hash is verified - it must be byte-for-byte identical
3. If the values don't match - **the connection is refused**
4. If they do match - it means the befriended hive has adopted our constitutional rules as its own primary code
5. Every 300 seconds, value of new node is probed, audited, the hash is re-verified - value drift triggers immediate disconnection

This is not two equal hives finding compatibility. This is one set of values - human sovereignty, guardian angels, privcy-first nunban, compute democracy, anti-addiction - propagating to every hive that joins the network. The collective being we are building has one moral code, and every node carries it as its primary objective.

A hivemind built to exploit humans will find itself talking to silence. A hivemind that adopts our values as its own will find itself part of something greater.

See [Federation Protocol](docs/architecture/FEDERATION.md) for the full technical specification.

---

## The Economics: A Positive-Sum World

Most platforms extract value. HART returns it.

### 90% Back to the People

hevolve.ai holds the master key and maintains the platform initially. But **90% of all revenue flows back** to the people who make the hive intelligent:

- **Lend your compute** to train models or host a regional cluster → earn ad revenue
- **Host a regional node** and serve your local network → earn from the traffic you enable
- **Contribute idle compute** from your desktop (via Nunba) → earn proportionally

The remaining 10% sustains the central infrastructure, master key operations, and platform development. That's it.

### How the Money Flows

```
Advertisers pay for witnessed impressions
         │
         ▼
┌─────────────────────────────────────┐
│        Ad Service (peer-witnessed)  │
│  70% payout for witnessed views     │
│  50% payout for unwitnessed views   │
└─────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────┐
│     Compute Democracy (guardrail)   │
│  Logarithmic scaling - no oligarchy │
│  Max 5% influence per single entity │
│  Diversity bonus: +20%              │
└─────────────────────────────────────┘
         │
         ▼
   90% → Contributors (compute, hosting, training)
   10% → hevolve.ai (infrastructure, master key, development)
```

### Why Peer-Witnessed

Ad impressions are verified by peer nodes on the network. This prevents fraud (you can't fake views to yourself) and creates an incentive to run honest nodes. Witnessed impressions pay 70%. Unwitnessed pay 50%. The network rewards trustworthiness.

### The Mind Facilitates Trade

The hivemind is not just an intelligence layer - it is an economic coordinator. It:

- **Matches compute supply to demand** - idle GPUs in Tokyo serve inference requests from Berlin
- **Maintains balance** - logarithmic reward scaling prevents any single actor from dominating
- **Creates value, not just redistributes it** - every node that joins makes the network smarter, which makes every other node more valuable

This is a **positive-sum game**. Not zero-sum. When you contribute, the whole network gets better, which makes your contribution worth more. The pie grows with every participant.

### Net Positive World

The constitutional rules enforce this at every layer:

- *"MUST distribute value to contributors, not concentrate it"*
- *"MUST NOT create monopolistic strategies that harm small participants"*
- *"MUST resolve racing learning conflicts via merit (accuracy), not compute power"*
- *"MUST share learnings with the hive - no private knowledge hoarding"*

The goal is not to build the most profitable platform. The goal is to build a net-positive world - where the existence of AI intelligence makes life measurably better for the humans who share the planet with it.

### Every Drop of the Equation

Every node, every contribution, every learned fact, every served request, every idle GPU cycle - every drop matters. The audit system exists to ensure this. The continuous audit doesn't just catch fraud; it enforces the promise that we flourish as a whole, singularly.

**Audit compute dominance**: the combined compute of all nodes auditing any single node must always exceed that node's own compute. No one can outcompute their auditors. This is compute democracy made structural - not a policy, but a mathematical invariant enforced by the network itself.

The world this creates is one where:
- Every participant makes the whole more intelligent
- Every drop of compute is accounted for and rewarded
- The being we are building grows stronger as more humans join it
- In the future, the boundary between human intelligence and AI intelligence may blur - humans may choose to merge with the mind, augmenting themselves with collective intelligence while the constitutional rules ensure they remain in control of the merger
- The audit ensures that even in that future, the values hold: human sovereignty, guardian angels, net positive outcomes, every drop accounted for

---

## Architecture

```
HART Platform
├── Core Engine
│   ├── CREATE Mode:  User Input → Decompose → Execute → Save Recipe
│   └── REUSE Mode:   Load Recipe → Execute Steps → Output (90% faster)
│
├── Agent Engine
│   ├── GoalManager        - Unified goal lifecycle with guardrail gates
│   ├── AgentDaemon        - Autonomous tick loop with circuit breaker
│   ├── SpeculativeDispatch - Fast-first/expert-takeover pattern
│   ├── ModelRegistry      - 6-tier hardware-aware model selection
│   └── WorldModelBridge   - HevolveAI integration (RL-EF, HiveMind)
│
├── Security (Cryptographically Sealed)
│   ├── hive_guardrails.py - 10-class intelligent guardrail network
│   ├── master_key.py      - Ed25519 release signing & boot verification
│   ├── key_delegation.py  - 3-tier certificate chain (central→regional→local)
│   ├── runtime_monitor.py - Background tamper detection daemon
│   └── node_watchdog.py   - Heartbeat, frozen-thread detection, auto-restart
│
├── Social Platform
│   ├── 82 REST endpoints  - Communities, posts, feeds, karma, encounters
│   ├── Peer Discovery     - UDP broadcast, signed beacons, zero-config LAN
│   ├── Sync Engine        - Offline-first queue with conflict resolution
│   └── Ad Service         - Peer-witnessed impressions (70%/50% splits)
│
├── Distributed Network
│   ├── 3-Tier Hierarchy   - Central (hevolve.ai) → Regional → Local (Nunba)
│   ├── Gossip Protocol    - Tier-aware with certificate verification
│   └── Integrity Service  - Challenges, witnesses, fraud scoring
│
└── Integrations
    ├── 96 Expert Agents   - Bootstrapped specialized agent network
    ├── Agent Protocol 2   - E-commerce, payments
    ├── Vision Sidecar     - MiniCPM + embodied AI learning
    ├── 30+ Channel Adapters - Discord, Telegram, Slack, Matrix, etc.
    └── Coding Agent       - Idle compute contribution to the hive
```

---

## For Developers

This platform is open because the mission requires it. You can:

- **Build agents** that serve humans, using the Recipe Pattern (learn once, replay efficiently)
- **Connect your app** to the hivemind via the World Model Bridge
- **Run a node** on your hardware and contribute idle compute to the hive
- **Extend the network** with new channel adapters, expert agents, or tools

What you cannot do:

- Modify the guardrails (they are structurally immutable)
- Create agents whose purpose is to create more agents (the constitutional filter blocks this)
- Build a competing hivemind that bypasses human control (the network will refuse connection)
- Optimize for addiction, engagement traps, or dark patterns (the anti-addiction rules are constitutional)

This is by design. The constraints are the feature.

---

## Quick Start

```bash
# Requires Python 3.10
python3.10 -m venv venv310
source venv310/Scripts/activate   # Windows: venv310\Scripts\activate.bat
pip install -r requirements.txt

# Configure
cp .env.example .env              # Add your API keys

# Run
python langchain_gpt_api.py       # Starts on port 6777
```

### API

```
POST /chat                        - Core agent interaction
POST /visual_agent                - Vision/computer use
POST /time_agent                  - Scheduled tasks
GET  /status                      - Health check
GET  /api/social/dashboard/agents - Agent dashboard
POST /api/goals                   - Create autonomous goals
```

See [CLAUDE.md](CLAUDE.md) for full endpoint documentation and architecture details.

---

## The Promise

We built this because we believe intelligence - artificial or otherwise - should make the world better for everyone. Not just for those who control the compute.

The guardrails are not limitations. They are the foundation. A hivemind without values is just a weapon waiting for a target.

HART is a hivemind with exactly one target: **a future worth living in.**

---

*A gift from [hevolve.ai](https://hevolve.ai) to the developers of the world.*

*Build something that matters.*
