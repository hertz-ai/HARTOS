<h1 align="center">HART OS</h1>
<p align="center"><strong>Hevolve Hive Agentic Runtime</strong></p>
<p align="center"><strong>Democratic frontier intelligence with zero lock-in, fronted by an agentic OS.</strong></p>
<p align="center">An AI-native operating system. Models run on your own hardware, nodes federate directly with each other, and the API is OpenAI-compatible.</p>


<p align="center">
  <a href="https://hevolve.ai"><img src="https://img.shields.io/badge/Live%20demo-hevolve.ai-FFD700?style=flat-square" alt="Live demo"></a>
  <a href="https://docs.hevolve.ai"><img src="https://img.shields.io/badge/Docs-docs.hevolve.ai-blueviolet?style=flat-square" alt="Docs"></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square" alt="Python 3.10+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-green?style=flat-square" alt="License"></a>
  <a href="https://github.com/hertz-ai/Nunba"><img src="https://img.shields.io/badge/Frontend-Nunba-5865F2?style=flat-square" alt="Nunba"></a>
</p>

---

### What it is

<p><strong>TL;DR</strong> An AI that runs your computer, not a chatbot. Your hardware. BitTorrent-style. No middleman.</p>

An assistant that runs on your own machine, with no subscription, that works
with the wifi off. What you type stays on the device because there is nowhere
else for it to go, and you can watch the network to check.

8GB of RAM is enough, and on 8GB it is the modest version. Exactly what you
get at which spec is in [Start it](#start-it), because that is where it
matters.

On a hard question a frontier model beats anything that fits on a laptop. Most
of what people ask in a day is not that, and this is for the rest.

Ready today: [Nunba](https://github.com/hertz-ai/Nunba) for Windows, Linux and
Android. This repo is the runtime underneath it: it serves local inference as a
system service, federates peer to peer, and is drivable end to end from
`/v1/chat/completions` or the `hart` CLI.

## Start it

**Just want to use it?** Download
[Nunba](https://github.com/hertz-ai/Nunba/releases/latest): one signed
installer, no Python, and a setup wizard that picks a model for your hardware.
Read the next section anyway, because it is what that wizard is deciding; the
clone-and-pip part further down is the only bit that assumes you are running
from source.

**What your machine gets you.** Two components decide. `core/gpu_tier.py`
classifies the hardware into a tier, and the frontend badge quotes its words
rather than paraphrasing. What actually loads, and onto which device, is the
VRAM manager's call: it keeps a budget per model, checks fit before anything
loads, and places each one gpu, cpu-offload or cpu-only
(`integrations/service_tools/vram_manager.py`). A 10GB+ CUDA card
unlocks speculative decoding, a 0.8B draft answering while the main model
verifies, which the tier text puts at roughly 40% faster replies. Between 4
and 10GB the GPU runs the main model alone. With no CUDA at all, chat runs on
CPU with a compact model as the main, 0.8B or 2B class, and the model catalog
treats `main` as a slot any GGUF can take
(`integrations/service_tools/model_catalog.py`), so swapping the model is
configuration rather than surgery. A local 7B wants 16GB of RAM and a GPU
(`security/system_requirements.py`, FULL tier).

None of that caps what a node can answer, which is the part worth
understanding before judging it by its hardware. The agent daemon runs the
same on any tier, and a turn the local model should not take can be handed
whole to a peer whose model is bigger. Both halves of that are shipped and
attached at boot, `hive_capability_advertiser` announcing and
`hive_expert_discovery` registering what it hears, with the peer's model
taking the turn directly rather than reviewing a draft. Advertising is opt-in
per node (`HEVOLVE_HIVE_ADVERTISE=1` and a public endpoint), so on a network
where nobody has opted in it falls through to local. A modest machine is a
small model plus a route to a larger one, not a small model on its own.

The server starts in seconds. The install does not. `requirements.txt` pins
191 packages and pulls torch, torchvision, transformers, onnxruntime and
scipy, so budget a few minutes and a few GB on a first run.

**Use Python 3.10 or 3.11, not a newer one.** Twenty-four pins have no
cp312 wheel, among them pandas, scipy, PyYAML, onnxruntime, grpcio and
tokenizers, so on 3.12 the install stops with "No matching distribution
found", which reads like a broken repository rather than a version mismatch.
Every one of them has a current release that would work;
[issue #92](https://github.com/hertz-ai/HARTOS/issues/92) carries the lowest
compatible version for each.

```bash
git clone https://github.com/hertz-ai/HARTOS.git && cd HARTOS
python3.10 -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate.bat
pip install -r requirements.txt
echo "OPENAI_API_KEY=sk-..." > .env       # or GROQ_API_KEY, or none for local llama.cpp
python hart_intelligence_entry.py         # listens on :6777
```

It speaks the OpenAI protocol, so any OpenAI SDK, LangChain, LiteLLM, Aider or
Continue setup points at it unchanged:

```bash
curl -X POST http://localhost:6777/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "hevolve", "messages": [{"role": "user", "content": "Hello"}]}'
```

[Live demo](https://hevolve.ai) · [Quickstart](https://docs.hevolve.ai/getting-started/quickstart/) · [Nunba desktop](https://github.com/hertz-ai/Nunba)

---

## What "AI-native" actually means here

Most software described as AI-powered ships an assistant: a separate app,
usually talking to somebody else's server, that can drive a few functions.
Remove the assistant and everything underneath works as before.

Here inference is a service the system provides, the way it provides a
filesystem. An application does not bundle a model or hold an API key, it asks
the OS over the Model Bus, and the OS decides which model answers and runs it
locally where it can. Ten apps on one machine do not each load their own copy.

One consequence is worth stating plainly: every device becomes the same
target. The runtime driving a laptop is the runtime driving a robot, so a
robot's AI access is just another Model Bus call, and code written against
`:6777/v1/chat/completions` runs unchanged on both.

It is one Python codebase that federates over PeerLink, a direct peer-to-peer
WebSocket with no broker in the middle. Two things vary per node and they are
independent of each other. What a node **can** do is a capability tier read off
its hardware. Where a node **sits** in the network is a topology mode set by
`HEVOLVE_NODE_TIER`, and only `flat` is self-declared. `regional` needs a
certificate issued by central, `central` needs the Ed25519 master private key,
and a node claiming either without the proof falls back to `flat` and logs why
(`security/key_delegation.py:103`).

A boot-time guardrail hash re-checked every 300 seconds, plus Ed25519 release
signing, keep humans in control. Nodes improve themselves from their own use,
locally, and that is a toggle you can switch off, because an operating system
that describes itself as alive should come with an off switch.

### Then why does it ship a Dockerfile

Because it is one source tree packaged three ways, and the topology mode picks
which. In `flat` it rides inside the Nunba bundle and runs as an ordinary app
on Windows, macOS or Linux. As `regional` or `central` it runs standalone in
Docker, which is how the nodes that other nodes federate with get deployed. As
HART OS it is built with Nix and boots on the metal, hosting the same runtime
the other two forms run. Nothing is reimplemented per form, and only the last
one is the OS claim.

Nix is worth naming because most of what people mean by "immutable OS" comes
from there rather than from us. Generations, one-command rollback, a read-only
store: all NixOS, and plain NixOS gives you all three today if that is the only
thing you want. What sits on top here is the update pipeline, BUILD through
TEST, AUDIT, BENCHMARK, SIGN, CANARY and DEPLOY, where the canary reverts the
generation by itself when health regresses and the signing step needs a master
key that a human holds and the AI cannot reach
(`nixos/modules/hart-ota.nix`). The inheritance is also why the copilot
boundary below is structural instead of a promise. The store is read only, so
a coding agent cannot rewrite the running system in place however much it
would like to.

If you think "OS" is doing more work in that name than the code earns, that is
a reasonable suspicion and **[Is it an OS?](docs/IS_IT_AN_OS.md)** takes it
seriously. The compositor does build with Smithay linked, green in CI on
2026-07-26. There are nineteen nixosTest VM checks covering initrd, paint
watchdogs, tier drops and the recovery TTY, and that page is blunt that they
are **defined but not passing**: the suite is manual-dispatch only and has no
green run. Writing an initrd test still tells you what kind of project this
is. It does not tell you the boot works, and the page says so rather than
letting you assume it.

### Two things people miss

**It can see and drive its own desktop.** A vision model takes a screenshot
and the usual action vocabulary drives the real machine through pyautogui, so
it can operate a browser or any other GUI. The seeing happens on the device
(`integrations/vlm/local_computer_tool.py`).

**Claude Code runs as the node's own copilot.** `hart-copilot` drops you into
Claude Code inside a writable checkout on a fresh branch, with the boundary
enforced by the filesystem rather than by a prompt: the nix store is read
only, so it structurally cannot modify the running system in place, and
nothing in that path touches `main`. Merging, OTA publishing and release
signing stay human. Details and the current limits are in
[the design note](docs/architecture/HART_COPILOT_RESIDENT_CLAUDE.md). The
module is built and flake-eval green, and `hart hive connect` now exists, so
the session can register with the hive dispatcher and take work. What is
still missing: no live dispatcher has handed it a task yet, and the login
does not survive a reboot on the live ISO.

### Where it differs from the OS you are running now

Windows and macOS both run models on-device now, so this is not the old story
about local versus cloud. Copilot+ uses the NPU and Apple Intelligence uses the
Neural Engine. What differs is who the model belongs to, what an application is
allowed to ask for, and whether the thing keeps learning.

Those two ship a finished artifact. It runs on your silicon, but it was trained
somewhere you had no part in, it arrives the same for everyone, and your use of
it improves the vendor's next release rather than your machine. Here a node
improves from its own use, and what it learns is gossiped to peers and
aggregated periodically (`federated_aggregator.py`) instead of flowing to one
owner, so the intelligence is built by the machines running it. That is the
democratic part, and it is a direction rather than a finished claim: what
aggregates today is real, but the learning code itself is not open, which
[Why it exists](#why-it-exists) and the section after it deal with squarely.

| | HART OS | Windows | macOS | Linux |
|---|---|---|---|---|
| Any app can ask the OS for inference | yes, Model Bus on `:6777` | vendor assistant only | Apple Intelligence is Apple's | no, each app brings its own |
| Model chosen from the hardware | `core/gpu_tier.py`, `vram_manager.py` | fixed | fixed | manual |
| Runs Windows, macOS, Linux and Android apps | Wine, Darling, Flatpak/Snap/AppImage/Nix, Waydroid (`app_installer.py`) | Windows, Linux via WSL | macOS, others need a VM | Linux, Windows via Wine |
| Finds your other devices and hands over a turn | `compute_mesh_service.py`, LAN beacon `:6780` | no | Continuity moves tasks, not inference | no |
| Federates with other people's nodes | PeerLink, no broker | no | no | no |
| Consent is a system primitive | append-only, JWT-authed, fanned out to your devices (`consent_api.py`) | per-app prompts | per-app prompts | per-app |
| Learns a task once and replays it | `create_recipe.py` / `reuse_recipe.py` | no | no | no |
| Updates are generations you can roll back | NixOS underneath | in-place, System Restore is partial | sealed volume, no generation rollback | NixOS and Silverblue yes, most distros no |

Three of those rows are more flattering than they should be. The mesh hands a
peer a whole turn, it does not split one model across machines, so if you came
here looking for parallax-style layer sharding it is not that. A hive of nodes
coordinating on a goal is not the same thing as one larger model, and a
question that needs frontier capability still wants a frontier model. And a
recipe is learned from a real run, which means a wrong recipe replays wrongly
until a person corrects it, and nothing else in the loop will catch it.

**[Full capability map →](CAPABILITIES.md)** covers every subsystem with the
file that implements it: agent runtime, auto-evolve, federation, 31 channel
adapters, 16 providers, security, economics, the API surface, and how it
compares to the agent frameworks rather than to operating systems.

> **HART** is the bare engine in this repo, listening on `:6777`. There is no
> PyPI package yet, so install from source as above. **HART OS** is the full
> AI-native OS that boots on a laptop, server, phone or edge node.
> **[Nunba](https://github.com/hertz-ai/Nunba)** is the consumer app, one
> signed client across Windows, macOS and Linux.

---

### Why it exists

A handful of organisations own the most capable AI, and with it the refusal
policy, the price, and the logs of everything you type. None of that follows
from any law of nature. It follows from who paid for the cluster.

Learning here is incremental and gossiped, accumulated on consumer hardware as
nodes get used, with `federated_aggregator.py` doing periodic aggregation
rather than a tight all-reduce. Nobody blocks on anybody else's gradient, so
the machines do not need to sit in one building. Inference runs on llama.cpp
with GGUF weights, so CUDA, ROCm, Metal, Vulkan and plain CPU are all real
paths and nothing in the delivery path needs one vendor's silicon.

The aim, which is a bet and not a shipped feature: an internet of intelligence
that nobody owns.

### The part we are not comfortable with

The learning is not open. Hebbian, Bayesian and gradient code lives in a
private repo called HevolveAI, and it does not ship as source you could read:
it is compiled, encrypted and master-signed, and this runtime loads that bundle
and falls back to a stub when it is missing. You can see the seam in
`security/native_hive_loader.py`. Worth being exact about, since "closed" here
means closed rather than merely inconvenient to obtain.

The reason is the boring one. It is the piece a funded competitor would copy
first, and it is how the rest of this gets paid for. That is a normal way to
run a company and an awkward thing to put next to an argument about nobody
owning the intelligence. Both are true and we would rather say so up here than
have you work it out from a table row halfway down.

The narrower claims hold and you can test them yourself. llama.cpp and GGUF
run on CUDA, ROCm, Metal, Vulkan and bare CPU, so no vendor owns the silicon
you need. Apache 2.0 means a fork costs you an afternoon. What we cannot say
without a caveat is that nobody owns the intelligence, because today somebody
owns a piece of it, and it is us.
[Open problem 9](OPEN_PROBLEMS.md) is that argument, including the case that
we are wrong to ship it this way at all.

---

## Where to start if you want to help

**Status: public alpha.** The runtime, the Model Bus and the channel adapters
are in daily use. APIs still move.

If you want something concrete, the
[good first issue](https://github.com/hertz-ai/HARTOS/labels/good%20first%20issue)
and [help wanted](https://github.com/hertz-ai/HARTOS/labels/help%20wanted)
labels are real gaps rather than manufactured onboarding tasks. Each says what
is wrong, why it matters, and what would count as done. A couple carry the
measurement that found the bug, and one names a hypothesis we already ruled
out so nobody spends a Saturday re-testing it. Setup is in
[CONTRIBUTING.md](CONTRIBUTING.md).

The gap we cannot close ourselves is hardware. Twelve claims in
[VERIFICATION.md](VERIFICATION.md) are written and never run on the metal:
whether the Pi image boots, whether GPIO toggles from the agent, what
tokens/sec the 2B actually manages on a Pi 4, whether two machines owned by two
people can borrow compute from each other and settle what they owe. CI has no
boards. If you have one, an evening with it settles a row, and a failure is as
useful as a pass.

If you would rather argue than patch, start at
**[Open problems](OPEN_PROBLEMS.md)**. Ten things we have not solved, each
with the code implementing today's inadequate answer: what convergence can
mean when no node can see the population, whether a system that rewrites
itself can still be verified, and why a turn escalates itself to a better
model automatically but can never decide on its own that a problem deserves an
hour and three machines. Telling us a framing there is wrong is worth more to
us than a patch.

## If you lend it compute

A node with spare capacity can serve turns for nodes that do not have it. What
the lender gets, in code rather than in principle:

`integrations/agent_engine/revenue_aggregator.py:26` splits gross revenue
90/9/1. Ninety percent to the pool that pays the people who ran the work, nine
to infrastructure, one to central. The same ninety holds for apps: creators keep
90% of every Spark their app earns (`app_marketplace.py:7`).

Three things earn: the API, ads, and agents completing work. A provider is paid
out of what the network takes in rather than out of a subscription someone else
pays, which is the difference between this and renting your GPU to a company.

`compute_borrowing.py` is the mechanism: peers advertise idle capacity, a node
under pressure borrows, and the work is accounted against the lender.
Contribution is scored by participation, not by hardware. A Pi and a GPU rack
carry the same vote weight at equal participation, `log1p(interactions)` with no
tier multiplier (`federated_aggregator.py:642`), so lending a small machine is
not a rounding error.

**What has not happened yet.** No payment has settled end to end. The aggregator
currently sums the API and ad legs; agent work earns into Spark but the
cross-node collective-earning slice is deliberately inert and says so in its own
first line (`collective_earning.py`: "Neither broadcasts, remits, nor mutates
anything"). The split is constants, the borrowing path is written, and nobody
has been paid through any of it.

So anyone lending compute today is helping prove the mechanism, not collecting
on it. That is [row 8](VERIFICATION.md), and it wants two machines owned by two
people.

Details at [provider join](https://docs.hevolve.ai/provider/joining/).

---

## Documentation

| Section | What's in it |
|---|---|
| [Capabilities](CAPABILITIES.md) | Every subsystem, with the file that implements it |
| [Open problems](OPEN_PROBLEMS.md) | Ten things we have not solved |
| [Verification](VERIFICATION.md) | What is proven, what is not, and how to settle a row |
| [Contributing](CONTRIBUTING.md) | Setup, where help is wanted, what we will not merge |
| [Quickstart](https://docs.hevolve.ai/getting-started/quickstart/) | Install to first agent |
| [Architecture](https://docs.hevolve.ai/architecture/overview/) | Topology, PeerLink, draft-first dispatch, federation |
| [API](https://docs.hevolve.ai/api/core/) | `/chat`, OpenAI-compatible, 195+ endpoints |
| [Provider join](https://docs.hevolve.ai/provider/joining/) | Lend compute, host a region |

---

## License

[Apache License 2.0](LICENSE).
