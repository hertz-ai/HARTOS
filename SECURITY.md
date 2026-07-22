# Security Policy

## Reporting a vulnerability

Email **security@hertzai.com**. Do not open a public issue.

Include what you found, how to reproduce it, and what an attacker gets. If you
are unsure whether something counts, send it anyway — a false alarm costs us
ten minutes and the alternative costs more.

We will acknowledge within 3 working days and tell you whether we can
reproduce it. If we cannot, we will say what we tried rather than close it
silently.

## What is in scope

HART OS is an agent runtime: it serves a model over the Model Bus on
`:6777`, federates with peers over PeerLink, and hosts up to 31 channel
adapters. The interesting boundaries are:

- **The `:6777` API surface.** It speaks the OpenAI protocol, so anything
  that treats it as trusted-by-default matters. Reachability from another
  process, another user on the host, or the LAN when it should be local.
- **Channel adapters.** They accept input from Discord, WhatsApp, Telegram,
  email and others. Untrusted text reaching a code path that assumes trusted
  input is the shape we care about most.
- **PeerLink and federation.** Node identity is Ed25519. Anything that lets a
  peer impersonate another node, poison a federated delta, or read data a
  node never agreed to share.
- **The guardrail hash.** Boot-time, re-checked every 300 seconds. Anything
  that disables, spoofs or races that check.
- **Credential storage.** Provider keys are encrypted at rest (AES-256,
  PBKDF2). Anything recovering them, weakening the derivation, or writing
  them in the clear — including to logs.
- **Supply chain.** Anything letting a downloaded model, update or plugin
  execute code the operator did not intend. Releases are Ed25519-signed.
- **The locality claim itself.** HART OS states that data does not leave the
  node without an explicit action. A path that transmits it anyway is a
  security bug, and we will treat it as one.

## Out of scope

- Attacks needing physical access to an unlocked machine.
- Vulnerabilities in a model's *outputs* — a model saying something wrong is
  a quality issue, not a vulnerability.
- Reports from automated scanners with no demonstrated impact.

## Disclosure

Report privately, give us a reasonable window to ship a fix, then publish
whatever you like. We will credit you unless you ask us not to. We will not
ask you to stay quiet indefinitely, and we will not threaten anyone for
reporting in good faith.
