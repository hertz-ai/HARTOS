# Open problems

Things we have not solved. Each one names what the code does today, why that
answer is unsatisfying, and what would count as progress.

This file exists because the interesting part of HART OS is not the parts that
work. An AI-native OS that runs local-first, federates without a broker, and is
allowed to modify itself raises questions that do not have settled answers
anywhere, and we would rather state them plainly than let a README imply they
are handled. If you disagree with a framing here, that is the most useful thing
you can bring. Open a discussion and argue with it.

Everything below is grounded in real code, with the file that implements the
current approach. Nothing here is a hypothetical.

---

## 1. What does convergence mean with no global view?

**Now:** nodes federate over PeerLink, a direct peer-to-peer WebSocket with
no broker and no aggregator (`core/peer_link/`). A running central node reports
`epoch=85, convergence=1.000`.

**Why that is unsatisfying:** convergence is a claim about a population, and no
node can see the population. Each one sees the peers it happens to be connected
to, and that set changes as laptops sleep and phones move networks. A number
that says 1.000 without a global view is measuring agreement among whoever
showed up, which is not the same thing and can be trivially high precisely when
participation is worst.

**What progress looks like:** a convergence statistic that is truthful about its
own sample. Something a node can compute locally that degrades visibly when
its view of the network is partial, rather than looking perfect. Bonus if it
survives an adversarial peer that reports whatever makes convergence look good.

---

## 2. Federated learning across wildly unequal hardware

**Now:** the model tier a node runs is chosen from available VRAM, spanning
roughly 0.8B to 27B parameters and MoE variants.

**Why that is unsatisfying:** classical federated averaging assumes participants
share an architecture. Here they emphatically do not. A phone running a 0.8B
model and a workstation running 27B cannot exchange gradients, and averaging
anything across them is not obviously meaningful. Today the accurate description
is that nodes share signals, not weights, which sidesteps the question rather
than answering it.

**What progress looks like:** a defensible account of what *should* flow between
heterogeneous nodes. Distilled behaviour? Preference data? Task traces? And an
experiment showing the small node actually gets better from the large one's
participation, rather than just receiving traffic.

---

## 3. Can a system rewrite itself and still be verifiable?

**Now:** a boot-time guardrail hash re-checked every 300 seconds, plus Ed25519
release signing (`security/node_integrity.py`). The self-improvement path is a
toggle, and every node is independently killable.

This stopped being hypothetical. `scripts/hart_copilot_daemon.py` is a resident
Claude Code session that rewrites the node it runs on, and what holds today is
reviewability rather than attestation: it can activate a config with
`nixos-rebuild test` but cannot change what the machine boots into, because the
verb lives in a root unit's ExecStart and the daemon has no argument to pass
(`nixos/modules/hart-copilot.nix`). Its output is a PR. So the answer to "is the
thing running still the thing you agreed to run" is currently "a human read the
diff", which is an answer, and it is not attestation.

**Why that is unsatisfying:** these two properties are in direct tension. A
hash proves the code is what was signed; a system that improves itself
necessarily stops being what was signed. Today the tension is resolved by
keeping learned behaviour out of the hashed code path, which works but also
means the interesting part is the part that is not verified.

**What progress looks like:** an integrity model where adaptation is in scope
rather than excluded. Attestation over a policy the adaptation must satisfy,
say, instead of over the bytes. The property worth preserving is not "the code
is unchanged" but "the thing running is still the thing you agreed to run."

---

## 4. Choosing a model portfolio is currently a guess

**Now:** tier thresholds are hand-picked constants (24 / 10 / 4 / 0 GB), and
speculative decoding turns on around 10 GB to leave headroom for voice.

**Why that is unsatisfying:** those numbers came from judgement, not
measurement. The real decision is a portfolio problem, with draft model, main
model, vision and speech all competing for the same memory, with a latency budget
and a workload mix that varies by user. A single threshold ladder cannot
express that.

**What progress looks like:** a policy that takes (hardware, workload mix,
latency target) and returns an allocation, evaluated against the hand-picked
ladder on real machines. Beating the constants would be a useful
result; failing to beat them would be almost as interesting.

---

## 5. Evaluating an OS that is different on every machine

**Now:** no benchmark exists for the adaptive behaviour. Individual components
are tested; the composed system is not.

**Why that is unsatisfying:** benchmarks assume a fixed configuration, and the
entire premise here is that the configuration is derived per device. Reporting a
score from one laptop says almost nothing, and averaging across machines hides
exactly the adaptation being claimed.

**What progress looks like:** an evaluation design where per-device variation is
the measured quantity rather than noise to be averaged away. What is the right
unit of comparison when every install is legitimately different?

---

## 6. Personality tuning with no ground truth

**Now:** `core/resonance_tuner.py` extracts warmth, formality, humour
receptivity, technical depth and pace from interaction text using pure
heuristics, deliberately with no LLM in the loop.

**Why that is unsatisfying:** there is no label. We adjust toward a profile
without ever establishing that the adjustment helped, and the heuristics are
keyword-shaped, which will mistake register for preference. Someone writing
tersely because they are busy is not the same as someone who prefers terse
answers.

**What progress looks like:** an evaluation that does not require a
questionnaire. A behavioural signal that distinguishes "the assistant matched
me" from "the assistant changed." And a test of whether the heuristics
beat doing nothing at all, which has never been checked.

---

## 7. Pricing local compute

**Now:** `integrations/agent_engine/budget_gate.py` is fail-closed: an agent
goal with insufficient budget is blocked before dispatch. Credits accrue for
contributed compute.

**Why that is unsatisfying:** the unit is arbitrary. Local inference has no
marginal cash cost, since the electricity is already being spent, so a credit
denominated in tokens or seconds does not correspond to anything scarce except
the user's own patience and battery. Fail-closed is the right default and also
means an agent can be starved by an accounting artifact.

**What progress looks like:** a cost model grounded in something actually
scarce on the device (contended memory, thermal budget, foreground latency)
rather than a synthetic unit, and a fairness argument for what a node earns by
hosting someone else's work.

---

## 8. The ladder auto-escalates capability, but not effort

**Now:** a turn escalates itself. The 0.8B draft answers and emits
`delegate: none | local | hive`; `speculative_dispatcher` overrides that on
refusal patterns or low confidence, and `_pick_expert_for_delegate` climbs
from the draft to the local fast model to a hive expert, with an optional MoE
HiveMind fusion consult above that. No user asked for any of it. That part
works and is the good half of this design.

**Why that is unsatisfying:** every rung on that ladder is *a better model
answering in one shot*. Some problems are not hard in a way a bigger model
fixes. They are hard because they need decomposition, several attempts, a
check that the answer is actually right, and more minutes than a request
should ever hold open.

HART OS has that machinery: `agent_daemon` runs goals over time,
`hive_task_protocol` carries tasks with a `validate_result` quality score,
`compute_mesh_service.offload_to_best_peer` recruits a peer's hardware.
None of it is reachable from difficulty. The chat path enters sustained work
only when the caller sets `create_agent` or `autonomous`
(`hart_intelligence_entry.py:8609-8611`), which are flags a person ticks, not a
conclusion the system draws. So autopilot stops exactly where the problem
stops being answerable in one breath, which is precisely where autonomy would
be worth something.

The asymmetry is stark. Ask something trivial and the system decides, on its
own, to answer it cheaply. Ask something that needs an hour of work
across three machines and it will hand you its best single paragraph, because
nothing is watching for "this one deserves more than a turn."

**What progress looks like:** a signal that distinguishes *needs a stronger
model* from *needs sustained work*, and an escalation that can cross that
line without being told to. Which raises the questions that make it hard:

- What may a system commit on a user's behalf? Minutes of their battery,
  their peers' compute, a budget under `budget_gate`? Where is consent, once
  per goal, once per session, or a standing policy?
- Interruption is a user-experience problem as much as a scheduling one. A
  turn that returns in 400 ms and a turn that returns in 40 minutes cannot be
  the same interaction, and pretending otherwise is how a good answer arrives
  after everyone has left.
- How does it know it is done? `validate_result` scores coding tasks against
  a known scope. There is no equivalent for "was this reasoning any good,"
  which is problem 6 wearing different clothes.
- What is the failure mode of being wrong in the expensive direction,
  spending twenty minutes on something a 4B model would have answered in two
  seconds? Cheap escalation is safe; this one is not.

`core/prompt_difficulty.py` is a first step at the signal, deliberately a
narrow one: deterministic, no model call, and it only decides fast-vs-full
routing when no draft model is loaded to decide it properly. It does not
attempt the question above. Nobody has.

---

## 9. The learning engine is closed, and the argument says it should not be

**Now:** this runtime is Apache 2.0. The OS, the Model Bus, PeerLink, the
agent engine, the escalation ladder, all of it. The learning is not. Hebbian,
Bayesian, probabilistic and gradient work lives in HevolveAI, a sibling repo
that is not public, loaded here at runtime as a signature-verified binary via
`security/native_hive_loader.py`, with a stub fallback when it is missing.
`core/resonance_tuner.py` and `core/resonance_identifier.py` both say as much
in their own docstrings, in those words: "All actual learning (Hebbian,
Bayesian, probabilistic, gradient descent) lives in the HevolveAI sibling
repo", and "All biometric ML ... lives in the HevolveAI sibling repo".

**Why that is unsatisfying:** the case this project makes is that
concentration of intelligence is a choice rather than a necessity, and that
the weights, the policy and the price should not belong to whoever paid for
the cluster. A closed learning core sits badly next to that.

Open core is an ordinary business shape and half the infrastructure you use is
built that way, so the shape is not the problem. The problem is the word
democratic. Democratic is a claim about who holds power, and right now we hold
a piece of it. Anyone who reads the top of the README and then hits the native
hive loader row in the capabilities table has caught us in that gap, and they
are right to.

Why it is closed is not mysterious. It is the part a funded competitor would
lift first, and it is what pays for the rest. We are not claiming that is
noble, only that it is the actual reason, which seems more useful to argue
with than a principled-sounding one we made up afterwards.

The narrower claims hold and are checkable. llama.cpp and GGUF run on CUDA,
ROCm, Metal, Vulkan and bare CPU, so no vendor owns the silicon you need.
Apache 2.0 means a fork costs an afternoon. What we cannot say without a
caveat is that nobody owns the intelligence, because today somebody owns a
piece of it, and it is us.

**What progress looks like:** a defensible line for what must be open in a
system making this argument, and an account of why. Some candidate answers,
none of them obviously right:

- The **protocol** is the thing that matters, not the implementation. If the
  aggregation format and the peer contract are open, anyone can write a
  competing engine and the network does not care which one you run. This is
  the argument that email is open even though most clients are not.
- The **weights and the deltas** are the thing. Whoever holds those holds the
  intelligence, and an open runtime around a closed learner is a nicer cage.
- **Nothing less than all of it.** If the claim is democratic, any closed link
  in the chain is where the concentration reappears later.

We do not have a settled answer. Stating the tension is the only position we
can defend while it is unresolved, and arguing us out of it is a real
contribution.

---

## 10. Concrete and unclaimed

Smaller, well-specified, and still open:

- **NAT traversal falls back to nothing.** `core/peer_link/nat.py` tries LAN
  direct, then STUN, then hole-punching; relay through a seed peer is a
  placeholder. Peers behind symmetric NAT simply do not connect.
- **Budget decisions rest on an approximation.** `core/token_utils.py` falls
  back to `chars / 3.5` when a real tokenizer is unavailable, and spend gates
  are enforced on that estimate. Nobody has measured the error, or which way it
  is biased.
- **Compute optimisation is explicitly heuristic.** `core/compute_optimizer.py`
  states "no ML in HARTOS — heuristic only." Whether a learned policy would beat
  it is unmeasured.

---

## Working here

Disagreement is the point. If a framing above is wrong, saying so is worth more
than a patch. Open a discussion, or an issue if it is concrete, and argue from
evidence. A measurement beats an opinion, and a reproduction beats both.

Two working rules that this project holds to, and that apply to contributions:

- **Validation is not proof.** That a thing is present, returns 200, or passes
  review does not establish that it works. Drive the real path and read what
  comes back. Several bugs in this repo passed review and failed on first
  contact with a real request.
- **Prefer extending an existing implementation to adding a parallel one.** If
  two callers need the same behaviour, they should share it, because a future
  fix will otherwise reach one and miss the other. That has already happened
  here more than once.

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup.
