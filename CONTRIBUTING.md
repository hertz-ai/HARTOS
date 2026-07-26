# Contributing to HART OS

HART OS is the agent runtime. [Nunba](https://github.com/hertz-ai/Nunba) is
the desktop app on top of it. A change to how the agent *thinks* belongs
here; a change to what a user *sees* usually belongs there. If you are not
sure, open an issue and we will point you at the right repo rather than
bounce the PR.

## Get it running

```bash
git clone https://github.com/hertz-ai/HARTOS.git && cd HARTOS
python3.10 -m venv venv && source venv/bin/activate    # Windows: venv\Scripts\activate.bat
pip install -r requirements.txt
python -m hart_intelligence_entry            # serves :6777
```

It speaks the OpenAI protocol, so the fastest sanity check is to point any
OpenAI client at `http://localhost:6777/v1/chat/completions` and see whether
it answers.

Tests:

```bash
pytest tests/ -q
```

## Where the interesting problems are

Areas of the codebase where help is wanted, ranked by how much rather than by
difficulty. If you would rather take on something unsolved than something
unfinished, [OPEN_PROBLEMS.md](OPEN_PROBLEMS.md) is the other list: ten
questions we do not have good answers to, each with the code implementing
today's inadequate one. Arguing that a framing there is wrong is a genuinely
welcome contribution and needs no setup at all.

- **Channel adapters** (31 of them, `integrations/channels/`), the most
  self-contained entry point. Each is an inbound → agent → outbound loop and
  they fail in findable ways: reconnect logic, message-type filters, ID
  mapping between a provider's format and ours. If you use a platform we
  support badly, that is the best possible reason to work on its adapter.
- **The auto-evolve loop** (`autoresearch_loop.py`) turns usage into
  candidate optimisations in runtime rather than in a nightly retrain. The
  open question is exploration: escaping local minima without destabilising
  a model somebody is mid-conversation with.
- **The guardrails** (`hive_guardrails.py`, `cultural_wisdom.py`). Every
  self-improvement passes them before commit. Adversarial test cases are
  worth more here than new features; this is where "it got worse and nobody
  noticed" is supposed to be caught.
- **Hardware tiering**. Engines are skipped on ≤6GB cards. The boundaries
  are educated guesses that would benefit from measurements on hardware we
  do not own.

## Before you open a PR

- One change per PR. A fix plus a refactor in one diff takes three times as
  long to review and usually gets one of them rejected on the other's
  account.
- Say what breaks if the change is wrong. "Nothing, it is a docs change" is
  a perfectly good answer; it tells a reviewer where to spend attention.
- Add a test that fails without your fix. We shipped a validator that
  rejected every valid input and passed its whole suite, because the suite
  only ever fed it malformed data, so we are particular about this.
- Run `pytest tests/ -q`. If something unrelated is already broken, say so
  in the PR rather than fixing it silently in the same diff.

Commit message format is not policed. The body explaining *why* is, because
the diff already shows *what*.

## Reporting bugs

Include OS, Python version, GPU/VRAM, and the topology you are running
(flat / regional / central), because a lot of code paths branch on those. Attach the
relevant log rather than a screenshot of it.

Security issues do **not** go in the tracker. See [SECURITY.md](SECURITY.md).

## What we will not merge

- Telemetry, analytics, or anything transmitting user content off-device.
- Anything making a cloud provider mandatory. Bring-your-own-key is fine;
  required-key is not.
- Removing a guardrail to make a benchmark look better.

## License

Contributions are accepted under the repository's [LICENSE](LICENSE). Opening
a PR confirms you have the right to submit the code under it.
