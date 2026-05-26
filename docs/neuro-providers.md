# Neuro & Biometric Providers

HARTOS exposes a single registry + adapter + discovery pattern for every neural, brainwave, and biometric device it can talk to. EEG headsets (Muse, Neurosity, Emotiv, OpenBCI), earpiece EEG (NextSense), wearables (Whoop, Oura), and contact-gated brainwave services like Yneuro all live behind the same three-function surface:

```python
from integrations.providers.neuro_providers import get_provider, list_providers
from integrations.providers.neuro_adapter   import choose_adapter
from integrations.providers.neuro_discovery import scan, known_providers_summary
```

## Why a dedicated registry

`integrations/providers/registry.py` is the canonical registry for compute APIs (OpenAI, Replicate, Together). Its fields (`context_length`, `pricing_per_1k_tokens`) don't apply to a physical biometric device. Forcing hardware into that shape would have been a parallel path.

`integrations/providers/neuro_providers.py` carries the axes that matter for devices: **form factor, transport, signal types, sample rate, channel count, BLE service UUID**.

## The three pieces

| Module | Responsibility |
|---|---|
| `neuro_providers.py` | Static `NEURO_REGISTRY` dict + `NeuroProvider` dataclass + filter helpers |
| `neuro_adapter.py` | `NeuroAdapter` abstract base + `choose_adapter(provider)` factory + per-transport stub adapters |
| `neuro_discovery.py` | `scan()` — unified fail-soft discovery across BLE, LSL, USB-serial |

## Transport decision matrix

| Need | Connection | Right transport |
|---|---|---|
| Raw EEG, research grade | Wired | `USB_SERIAL` or `LSL` |
| EEG, mid quality | Local wireless | `BLE` (or `LSL` if vendor exposes it) |
| Aggregate HRV / sleep | Internet OK | `REST` (OAuth) |
| Low-latency neurofeedback | Local | `WEBSOCKET` or `LSL` |

## Seeded providers

| Provider | Form factor | Transport | Signals | Status |
|---|---|---|---|---|
| **Yneuro** | unknown | unknown | brainwave fingerprint | **contact-gated** (`hello@yneuro.com`) |
| Muse 2 / Muse S | headset EEG | BLE, LSL | EEG, PPG, ACC | public (`muselsl`) |
| Neurosity Crown | headset EEG | WebSocket, REST | EEG | public (`neurosity`) |
| OpenBCI (Cyton / Ganglion) | cap EEG | USB-Serial, BLE, LSL | EEG, EMG, ECG | public (`brainflow`) |
| Emotiv (EPOC / Insight) | headset EEG | WebSocket | EEG | license-gated |
| NextSense | earpiece EEG | BLE | EEG | invite-only |
| Whoop | wristband PPG | REST | HRV, sleep, PPG | public |
| Oura | ring PPG | REST | HRV, sleep, PPG, temperature | public |

## Yneuro (https://www.yneuro.com/)

Yneuro ships Neuro ID — brainwave-based personal authentication. As of 2026-04 they do not publish a public SDK or API, so the registry entry is `CONTACT_REQUIRED` and `choose_adapter()` returns a `ContactGatedAdapter` that raises `PermissionError` with the vendor's email rather than pretending a client exists. When Yneuro replies to `hello@yneuro.com`, fill in the transport, sample rate, and SDK fields on the registry entry and implement the adapter — the slot is already wired.

## Adding a provider in ~30 lines

1. Append a `NeuroProvider` entry to `NEURO_REGISTRY` in `neuro_providers.py` (fill in what you know, leave the rest empty).
2. If the vendor's primary transport already has a stub (BLE / REST / WebSocket / LSL / USB-Serial), `choose_adapter()` routes to it automatically. The stub's error message tells you the exact `pip install` and docs URL to finish the implementation.
3. If you want first-class support, subclass `NeuroAdapter` and call `register_adapter(Transport.X, MyAdapter)` once at import time. `choose_adapter()` now returns your concrete adapter for every provider that uses that transport.

## Auto-discovery

`neuro_discovery.scan()` runs every enabled transport in parallel and returns `(provider_id_or_none, device_info)` tuples. Missing optional dependencies (`bleak`, `pylsl`, `pyserial`) degrade silently — no dep, no crash.

```python
from integrations.providers.neuro_discovery import scan

for provider_id, info in scan():
    if provider_id is None:
        print(f"Unknown device on {info['transport']}: {info}")
    else:
        print(f"{provider_id} -> {info}")
```

## Cross-network reads

Sensor data that crosses the network reuses PeerLink's existing `sensor` channel (`core/peer_link/channels.py`) — E2E-encrypted, already in place. No new wire protocol, no parallel security story.

## Humans-first

Biometric data is the highest-trust data a person can share. Nothing in this stack reads a signal, caches a signal, or transmits a signal without the user's explicit consent flow — same gate the rest of HARTOS uses. The `ContactGatedAdapter` exists precisely because "ask before pretending to integrate" is the correct default when a vendor hasn't published how.
