# Federated Asset Cache (FAC)

Status: DESIGN (read-only investigation + proposed schema). No framework code
yet. This document grounds the steward ask of 2026-06-29 in the infrastructure
that already exists, names exactly what to reuse (with file:line), identifies the
gaps, and specifies the schema + framework shape.

Related: harness task #144 (Federated content-addressed asset cache framework),
consumers task #140 (orb varieties) and task #143 (per-source card images), and
`HART_OS_FULL_DESKTOP_SPEC.md` (the alive-home producer that motivates this).

## 0. Synthesis: a reactive CORE plus an optional proactive ACCELERANT

Two designs were drafted for "reuse/cache assets peer to peer and central":

- The REACTIVE design resolves a cache miss the moment a caller asks: hash the
  request, look local then peer then central, and on a total miss generate once
  and announce the result. It is the irreducible, decentralization-first core: it
  has no required central authority and works with central OFF.
- The PROACTIVE design adds a seeding layer that moves the work OFF the hot path,
  so that most nodes already hold the popular asset in a warm local cache before
  the home ever asks for it. It is a pure accelerant.

This document is the SYNTHESIS, and it is layered on purpose:

- LAYER 1 (the CORE, binding): content-addressed reuse with one `get_or_create`
  facade, tiered `local -> peer -> central -> generate-once -> announce`,
  integrity-by-hash, consent-gated generic-only sharing. This layer is the whole
  contract. It must work with central and regional OFF. Nothing below it may
  become a precondition for resolving an asset.
- LAYER 2 (the ACCELERANT, optional): proactive seeding. A regional or central
  host watches sub-fleet demand, pre-positions the popular CIDs, and pushes a
  tiny SIGNED manifest of CIDs (never the bytes) to its sub-fleet, which pull at
  idle and pin. This makes the common interactive path a tier-1 local hit. It can
  ONLY ADD warm CIDs to a node. Remove it entirely and Layer 1 still resolves
  every asset.

The decentralization-first lens (`memory/decentralization_first_lens.md`) forces
the split: Layer 2 introduces the one place this design could drift toward
a gatekeeper (a regional seed origin), so Layer 2 is fenced as an accelerant that
degrades to nothing. The operational test "does this feature REQUIRE a central
authority?" answers NO for the whole design, because Layer 1 alone is complete.

## 1. The ask (steward, 2026-06-29)

> The alive-home producer is local; generation/fetch should NOT cost per-node.
> When an asset is created (centrally OR by any node) it should be REUSED. Come
> up with a design pattern for reusing/caching assets peer to peer and central,
> maybe a framework on top to encapsulate this.

Concretely: a card poster, an orb-glow texture, a generated image, a thumbnail,
a TTS clip - any byte blob that is expensive to make or fetch - should be made
or fetched ONCE on the whole network, then reused by every node that wants the
same thing, over P2P first and central as an optional accelerant.

Read at its strongest, "generation should NOT cost per-node" means the common
assets (the alive-home posters, the orb textures, the popular agent art) should
already be SITTING in a node's local cache the first time the home wants to paint
them, with no first-hit latency, cold generate, or WANT round-trip on the hot path.
That strongest reading is Layer 2. The floor (Layer 1) is: an asset is generated
or fetched at most once per node and, network-wide, the bytes are shared so the
SECOND node never regenerates.

## 2. Layer 1 (CORE): content-addressed, generate-once, tiered reuse

The recipe pattern (learn once in CREATE mode, replay in REUSE mode) is HARTOS's
founding idea applied to task execution. FAC applies the SAME idea to binary
assets:

- The key IS the content. `cid = hash(bytes)` for a fetched/known blob;
  `spec_key = hash(canonical-json(spec))` for a deterministic generation request.
  A content-addressed key is tamper-evident: a fetcher recomputes the hash and
  rejects on mismatch, so a blob fetched (or seeded) from an untrusted peer needs
  no trust in the transport, only in the math.
- ONE API. `get_or_create(spec, generate_fn) -> AssetRef`. The caller declares
  WHAT it wants (the spec) and HOW to make it on a total miss (generate_fn). The
  caller never reasons about where the bytes came from (seed, peer, central, or a
  local generate). A pluggable per-kind resolver/generator means agent art, app
  posters, captions, and embeddings all flow through ONE store.
- Tiered lookup, cheapest first:
  `local CAS -> peer (P2P) -> central (bounded fallback) -> generate-once -> announce`.
- Generic-only sharing. An asset leaves the node (and is eligible for seeding)
  ONLY when it is generic (no user PII / private-photo provenance) AND the
  consent/egress gate passes.
- Works with central OFF. Central is one tier among many, never a gatekeeper.
  With central unreachable the order collapses to `local -> peer -> generate`.
- Generalizes beyond images. The store is kind-agnostic; images are the first
  consumer (the W10 ImageCache becomes a thin adapter over FAC).

Cost model: a deterministic asset is generated AT MOST ONCE per CID across the
whole hive in the steady state. The first node to want it pays the generate, then
announces; every later node gets the bytes over P2P (or central) and verifies by
hash. Long-tail assets only one node ever needs are generated exactly once on that
node and never shared. That covers "create centrally OR by any node, then reuse"
with central OFF.

## 3. Layer 2 (ACCELERANT): proactive seeding

Layer 1 resolves a miss the moment a caller asks. Layer 2 moves the work OFF the
hot path by pre-positioning the popular assets. Six steps, every one of which
rides existing, signature-gated transport:

1. Demand signal. Every `get_or_create` records a cheap demand tick for the
   resolved `cid` (and its `spec_key`). This is the popularity input. It rides the
   EXISTING bus/telemetry metadata legs (counts only, never content), so a
   regional host can see which CIDs its sub-fleet keeps asking for.
2. Seed-set computation. A regional host aggregates the demand ticks from the
   locals it hosts and computes a bounded "hot seed set": the top-K most-requested
   GENERIC, shareable CIDs that fit within a seed byte budget. Central aggregates
   the regional sets into a global hot set for cross-region warm-start.
3. Pre-generate + hold. The regional host ensures it actually HOLDS the bytes for
   every CID in its seed set (it pulls from a holder, or runs the generate itself
   for a deterministic spec). It becomes a CDN seed origin for that set.
4. Push the manifest, not the bytes. The regional host publishes a SIGNED seed
   MANIFEST (a small list of CIDs + sizes + scope, NOT the blobs) to exactly its
   sub-fleet, over the existing `fleet.command` push fan-out scoped by `node_ids`,
   staged/canary so a bad seed never blasts the whole region.
5. Node pulls at idle (OTA pinned-pull shape). On receiving the manifest (or at
   region-join / boot), a node pulls the manifest's CIDs it does not already hold,
   from a peer first then the regional/central origin, at idle, foreground-yielded,
   bounded by its local store cap. Verify-on-fetch (CID match) gates every byte.
6. Gossip pre-announce. Independently of the manifest, every node piggybacks a
   compact HAVE-set (or just its hot CIDs) on the EXISTING gossip rounds, so the
   provider index is populated BEFORE any WANT. A node that misses a seed still
   finds a nearby holder in one targeted GET instead of a broadcast.

Net effect: in the steady state the alive-home calls `get_or_create(poster_spec)`
and tier 1 (local) hits, because the poster was seeded to this node minutes ago at
idle. Generation cost is paid once per region (often once globally), not per node
and not on the interactive path.

## 4. Decentralization-first (binding)

Per `memory/decentralization_first_lens.md`: P2P is the primary path, central is
an optional fallback/accelerant (a CDN seed and a bounded backup origin), never a
required authority. The layered split keeps this true even though Layer 2 adds a
seed origin:

- The operational test "does this feature REQUIRE a central authority?" answers
  NO. With `HEVOLVE_CENTRAL_URL` and `HEVOLVE_REGIONAL_URL` both empty
  (`SyncEngine.parent_tier_url()` returns ""), there is no seed origin and no
  manifest push; Layer 2 is a no-op and FAC runs as pure Layer 1 (lazy
  generate-once plus peer gossip pre-announce). No feature is lost; only the
  pre-warming is gone, so the first hit on a fresh node is a cold generate instead
  of a seeded local hit.
- A regional seed origin is a CONVENIENCE host, not a gatekeeper. A node that
  never receives a manifest, or distrusts the manifest signer, still resolves
  every asset via `local -> peer -> generate`. The manifest can only ADD warm
  CIDs to a node; it can never be a precondition for resolving an asset.
- Trust is master-anchored. A seed manifest is signed by the regional/central
  node's Ed25519 identity, whose authority chains back to `MASTER_PUBLIC_KEY_HEX`
  via the existing certificate chain. A relay forwards only a signature-valid
  manifest and holds no key (the fleet.command precedent). A forged manifest is
  dropped; a valid manifest can still be ignored by the receiver.
- Seeding pushes PUBLIC assets DOWN; it never pulls user data UP. The manifest
  carries only CIDs of GENERIC, consent-cleared assets. There is no path by which
  a seed push reads a node's private cache. EDGE_ONLY demand is never up-synced,
  so private usage can never drive a seed.

## 5. What to REUSE (do not reinvent)

### 5.1 P2P transport - PeerLink

| Reuse | Where | For |
|---|---|---|
| `PeerLink.send(channel, data, wait_response=True, timeout)` | `core/peer_link/link.py:244` | Request/response WANT->bytes over a channel (the blob fetch round-trip). |
| `PeerLink.send_binary(channel, data)` | `core/peer_link/link.py:296` | Stream raw blob bytes (binary frame `[1B channel][4B id][payload]`). |
| `PeerLinkManager.send(peer_id, channel, data, peer_url, wait_response, timeout)` | `core/peer_link/link_manager.py:109` | Per-peer GET with HTTP fallback when no live link (also the per-CID pull during a seed warm). |
| `PeerLinkManager.broadcast(channel, data, trust_filter, exclude_peer)` | `core/peer_link/link_manager.py:140` | Fan a WANT/HAVE announce to connected peers. |
| `PeerLinkManager.collect(channel, timeout_ms)` | `core/peer_link/link_manager.py:174` | Broadcast a WANT and gather HAVE replies (already used by HiveMind fusion). |
| `PeerLinkManager.register_channel_handler(channel, handler)` | `core/peer_link/link_manager.py:280` | Attach the asset handler to all current + future links. |
| `MessageBus` relay pattern: `_LRUDedup` + `hop_ttl` + `relay_path` | `core/peer_link/message_bus.py:159`, relay leg around `:282-323` | The loop-safe, storm-proof fan-out template for a signed CID announce AND for the seed manifest (copy the gap-#57 shape; do NOT invent a new relay). |
| `MessageBus.bootstrap_peerlink_ingress` (events channel -> receive_from_peer) | `core/peer_link/message_bus.py` (ingress bootstrap) | The proven inbound wire so a manifest/announce reaches NAT'd sub-trees over PeerLink, not just WAMP. |
| `CHANNEL_REGISTRY` | `core/peer_link/channels.py:28` | Add ONE channel `asset` (next free id `0x0A`; registry currently ends at `0x09` dm), `data_class=DataClass.OPEN` for generic blobs, low `priority` (bulk) so seed pulls never starve interactive channels. |
| `TrustLevel` (SAME_USER / PEER / RELAY) | `core/peer_link/link.py:46` | A SAME_USER peer (your own devices) shares any scope; a PEER shares generic-only. |

Note: the PeerLink handshake already exchanges + verifies Ed25519 node identity
and runs a trust ratchet (`link.py:385-516`), so a peer that answers a GET or
relays a manifest is already authenticated. The asset layer does not redo
identity; it adds only the content-hash verify on the received bytes.

### 5.2 Proactive push + sub-fleet scope + staged rollout (the seeding transport, Layer 2)

| Reuse | Where | For |
|---|---|---|
| `FleetCommandService.push_broadcast(db, cmd_type, params, tier_filter, issued_by, node_ids)` | `integrations/social/fleet_command.py:120` | THE one signed fan-out path. The `node_ids` allowlist is exactly the "a regional can only command the locals its region hosts" scope - the seed manifest fans out to the sub-fleet, never globally. An empty allowlist means zero targets, never "all" (`:157`). |
| `FleetCommandService.verify_command_signature` | `integrations/social/fleet_command.py:310` | The single authority check (central/regional Ed25519 anchored at master). A node NEVER acts on an unverified manifest. |
| `FleetCommandService.get_pending_commands(db, node_id)` | `integrations/social/fleet_command.py:187` | Offline-first durable delivery: a manifest sent while a node was OFF is drained on boot (the same shape OTA drain uses). |
| `VALID_COMMAND_TYPES` frozenset | `integrations/social/fleet_command.py:29` | Add `"seed_assets"` here so the manifest flows through the existing signed fleet bus with no new transport. |
| OTA staged-rollout / canary (task #59) | `nixos/modules/hart-ota.nix` pipeline, `api_fleet_update` | Seed a sub-fleet in stages (canary first, halt on regression), so a bad seed manifest is contained, exactly like an OTA push. |
| OTA pinned-pull model (central publishes WHICH, node pulls/receives, signed, never force-applied) | `integrations/agent_engine/ota_push_listener.py` (whole file; verify at `:128`) | The proven template: the manifest says WHICH CIDs are hot; the node PULLS them at idle and NEVER force-applies. The listener's drain-then-subscribe shape is copied for the seed listener. |
| gossip peer-announce over `/api/social/peers/broadcast` | `integrations/social/peer_discovery.py` (gossip fanout config `:62-80`) | The gossip pre-announce leg: piggyback a HAVE-set so providers are known before a WANT (the same path RALT skill distribution uses). |
| `peers` gossip channel (`id 0x03`, `data_class OPEN`) | `core/peer_link/channels.py:50` | The existing peer-announce round to attach a compact HAVE digest to. |

### 5.3 Central / regional store - the bounded 10TB origin + CDN/OTA pull source

| Reuse | Where | For |
|---|---|---|
| `FederationManager.ASSET_CEILING_BYTES = 10 * 1024**4` | `integrations/social/federation.py:400` | The exact 10 TB total-bytes ceiling concept for the central/regional blob origin (and the regional seed store). |
| `FederationManager.enforce_asset_ceiling(db, max_bytes, batch)` | `integrations/social/federation.py:402` | LRU eviction of the COLDEST non-pinned rows; copy this for the central CAS table AND the regional seed store (seed-set CIDs are pinned while hot). |
| `FederationManager._maybe_enforce_ceiling` (amortized) | `integrations/social/federation.py:440` | The same throttled-enforce hook for blob PUTs. |
| `FederationManager.pull_with_central_fallback(db, peer_url, limit)` | `integrations/social/federation.py:376` | The canonical "source peer first, parent origin fallback, skip self-pull" tiered shape - both the cold miss AND the seed warm-pull mirror it. |
| `receive_inbox` dedup by `(origin_node_id, origin_post_id)` | `integrations/social/federation.py:271`, model `_models_local.py:638` | The provenance-dedup precedent (FAC upgrades it to content-dedup by CID). |
| `SyncEngine.parent_tier_url()` (central else regional, empty on flat) | `integrations/social/sync_engine.py:697` | THE single resolver for "where is my parent tier / my seed origin". Reuse verbatim. Empty == no seeding (central-OFF). |
| `SyncEngine.queue` offline-first queue + `MAX_QUEUE_SIZE` backpressure | `integrations/social/sync_engine.py:53` | Offline-first PUT of a new generic CID up to central, and offline-first demand-tick up-sync. |

### 5.4 Local asset cache - the W10 ImageCache

`integrations/agent_engine/media_semantic_index.py`:

| Reuse | Where | For |
|---|---|---|
| `ImageCache` bounded LRU (bytes cap + `last_access` eviction) | `:768`, `_evict_locked` `:894` | The eviction policy, gzip index pattern, atomic-write index, fetch-once contract - generalize to a content-addressed `BlobStore`. Seeded CIDs are written through the SAME store; the LRU + a `pinned` flag keep the hot seed set resident. |
| `ImageCache.get_path(url)` fetch-once-then-serve, degrade to None | `:824` | The shape of `get_or_create`'s local tier; FAC swaps the key from url to cid. |
| `_IMG_CACHE_MAX_BYTES`, `_IMG_FETCH_TIMEOUT` | `:70-71` | Default bounds for the local store; the seed budget is a sub-cap of this. |
| `hashlib.sha1` helpers: `ImageCache._key(url)` `:811`, `_fingerprint` `:118`, `_doc_id` `:134` | The hashing precedent (FAC introduces a full sha256-of-bytes CID; see GAP 1). |
| `_default_base_dir()` under `get_data_dir()/data/media_index` | `:76` | Sibling layout; FAC lands at `get_data_dir()/data/asset_cache`. |
| `GET /api/media/image?url=` route + `_require_system_auth` | `:1006`, `:959` | The local-only auth + serve-file precedent for `GET /api/assets/<cid>`. |
| `export_captions` egress flatten-then-`check_egress` | `:649-699` (the `scan_text` flatten at `:684`) | The exact consent-gated egress shape FAC reuses before a CID is seeded or announced. |

IMPORTANT: the current `ImageCache` key is `sha1(url)` (`:811`) - that is
LOCATION-addressed, not content-addressed. Two URLs serving the same bytes are
two cache entries; one URL whose bytes change goes stale. FAC fixes this by
addressing on content. After FAC lands, `ImageCache` should become a thin
adapter: `fetch(url) -> bytes -> content_id(bytes) -> BlobStore.put`, so the web
image cache, the seed cache, and the federated cache share ONE store (Gate 4 - no
parallel path).

### 5.5 Consent / privacy

| Reuse | Where | For |
|---|---|---|
| `ScopeGuard.check_egress(data, destination, context) -> (allowed, reason)` | `security/edge_privacy.py:90` | The single egress gate. FAC asks it before announcing, seeding, or serving a blob's metadata + provenance. |
| `PrivacyScope` (EDGE_ONLY < USER_DEVICES < TRUSTED_PEER < FEDERATED < PUBLIC, default EDGE_ONLY) | `security/edge_privacy.py:39` | The scope an asset is tagged with at create time. Only FEDERATED/PUBLIC blobs may leave the perimeter or enter a seed set. |
| `scope_allows(data_scope, destination_scope)` | `security/edge_privacy.py:68` | The deterministic scope comparison `_may_share` calls first. |
| `get_scope_guard()` singleton | `security/edge_privacy.py:281` | No second guard. |
| `ConsentService.check_consent(db, user_id, consent_type, scope, agent_id)` | `integrations/social/consent_service.py:322` | Gate "may THIS user's generated asset be shared/seeded publicly" via `public_exposure` (the SAME gate agent up-sync uses) or a cloud-egress type for central PUT. |
| `ConsentService.auto_grant_with_notice` | `integrations/social/consent_service.py:218` | Safe-by-default central egress with one-tap revoke, where blocking would degrade UX. |

The privacy-first default (`memory/hartos_privacy_first_defaults_2026-06-24.md`)
holds: the local cache and any local generate/fetch need NO consent; only the
LEAVE-the-perimeter steps (announce a CID, seed it, contribute demand up-sync,
serve bytes to a non-SAME_USER peer, PUT to central) are gated. The default scope
is EDGE_ONLY, so an asset is private and seed-ineligible until the producer
explicitly tags it generic.

### 5.6 Content hashing / dedup (precedents)

| Precedent | Where | Note |
|---|---|---|
| FederatedPost dedup `(origin_node_id, origin_post_id)` + `UniqueConstraint` | `federation.py:271`, `_models_local.py:638` | Provenance-addressed dedup (who made it), not content-addressed (what it is). |
| `MessageBus._LRUDedup` (msg_id, O(1)) | `message_bus.py:159` | Reuse for the announce + manifest de-dup ring. |
| `ImageCache._key = sha1(url)` | `media_semantic_index.py:811` | Location hash. |
| `_fingerprint = sha1(size + head 64KB)` | `media_semantic_index.py:118` | A FAST fingerprint, NOT a CAS key (truncated input, collision-prone). |
| `compute_guardrail_hash()` / `compute_code_hash()` | `master_key.py`, `node_integrity.py:169` | Manifest/identity hashing, not asset bytes. |

There is NO existing hash-of-the-full-asset-bytes used as an addressable key.
That canonical CID is GAP 1.

### 5.7 Trust - master-anchored announce + manifest

| Reuse | Where | For |
|---|---|---|
| `node_integrity.sign_json_payload(payload)` / `verify_json_signature(pub, payload, sig)` | `security/node_integrity.py:137`, `:157` | Sign a CID announce AND a seed manifest with the node's Ed25519 key; any receiver verifies it. |
| `node_integrity.get_public_key_hex()` | `security/node_integrity.py:126` | The announcing/seeding node's identity. |
| `master_key.MASTER_PUBLIC_KEY_HEX` + `verify_master_signature` | `security/master_key.py` | The trust anchor for a central CDN-seed manifest. READ-ONLY: FAC never touches the private key. |
| `key_delegation.verify_certificate_chain(cert)` (parent_signature -> master) | `security/key_delegation.py:223` | A regional seed origin proves authority via its master-chained cert. |
| `key_delegation.get_node_tier()` | `security/key_delegation.py:103` | Decide whether this node may act as a regional seed origin (only regional/central may publish a manifest). |
| relay propagates only verified (holds no key) | used at `ota_push_listener.py:128`, `message_bus.py` relay leg | The "a relay holds no signing key and cannot forge authority" pattern - the manifest relay copies it. |

## 6. The GAPS (what does not exist yet)

Gaps 1-8 are in Layer 1 (the core); gaps 9-11 are in Layer 2 (seeding).

1. No content-addressed key (CID). Everything is location- or provenance-keyed
   (`sha1(url)`, `(origin_node, origin_id)`). No canonical `content_id(bytes)`
   nor `spec_key(spec)`. `_fingerprint` is a fast fingerprint over truncated
   input, unsafe as a CAS key.
2. No `get_or_create(spec, generate_fn)` facade. `ImageCache.get_path` only
   fetches a URL; nothing runs a local generate_fn on a total miss and then
   caches + dedups the RESULT by content.
3. No P2P blob protocol. PeerLink has `compute`/`federation`/`sensor` channels
   and `send_binary`, but no `asset` channel with HAVE / WANT / GET.
4. No content-addressed provider index. There is gossip peer-announce and the
   fleet.command relay, but no `AssetProvider(cid, node_id, scope, last_seen)`
   so a node can discover WHO has a blob without blasting every peer.
5. No general bounded CAS blob store keyed by CID. `ImageCache` is bounded LRU
   but URL-keyed and scoped to web images under `imgcache/`.
6. No central CAS origin endpoint. The 10 TB ceiling + `pull_with_central_fallback`
   are post-shaped (`federated_posts.content` TEXT). No `GET /api/assets/<cid>`
   blob origin or `PUT /api/assets` storing raw bytes under a bounded CAS table.
7. No egress scope for a binary BLOB. `ScopeGuard._extract_text` scans only
   top-level string fields (`edge_privacy.py:204`); a blob has no text. FAC needs
   an asset-level decision: only GENERIC (shareable) assets leave or seed.
8. No tamper-verify on fetch. With `cid = hash(bytes)`, the fetcher MUST recompute
   and reject on mismatch. That verify step does not exist.
9. No demand/popularity signal for assets. Nothing counts how often a CID/spec is
   requested, so no node can compute a "hot seed set". Telemetry carries node
   metrics, not per-asset demand.
10. No seed-set computation or manifest. `push_broadcast` + `node_ids` scoping and
    the OTA staged-rollout exist, but there is no `seed_assets` command type, no
    top-K-by-demand-within-budget selector, and no signed seed-manifest payload
    (list of CIDs) for a regional host to publish to its sub-fleet.
11. No idle warm-pull / seed listener. The OTA pinned-pull listener applies an OS
    closure; there is no asset analogue that, on a verified manifest, pulls the
    listed CIDs at idle (foreground-yielded, store-cap-bounded) and pins them.

## 7. The schema

### 7.1 AssetSpec - the request (Layer 1)

A spec is a small, canonically-serializable dict that fully determines the bytes.
`spec_key` is its content hash, so two nodes asking for the same thing compute the
same key without coordination.

```
AssetSpec = {
  "kind":   str,    # "image" | "thumbnail" | "orb_texture" | "poster" | "tts" | ...
  "source": str,    # "url" | "generate" | "render"
  "params": dict,   # source-specific, canonical:
                    #   url:      {"url": "...", "max_px": 512}
                    #   generate: {"prompt": "...", "model": "...", "seed": 7, "w": 1024, "h": 1024}
                    #   render:   {"template": "card_v4", "title": "...", "art_cid": "..."}
  "scope":  str,    # PrivacyScope value; producer-declared. Default "edge_only".
                    # Only "federated"/"public" assets may be shared/announced/seeded.
  "ttl":    int,    # optional soft freshness hint (seconds); 0 = immutable
}
```

`spec_key = "spec:sha256:" + sha256(canonical_json(spec_without_scope_ttl))`.
Scope and ttl are policy, not identity, so they are excluded from the key (two
nodes that disagree on scope must still resolve to the same bytes; the more
restrictive scope wins for sharing).

### 7.2 CID - the content id (Layer 1)

```
content_id(data: bytes) -> str   # "blob:sha256:" + sha256(data).hexdigest()
```

sha256 (not sha1) for a collision-resistant CAS key. A 2-char prefix shards the
on-disk store. The CID is the integrity proof: any fetcher (cold miss OR seed
warm-pull) recomputes `content_id(received)` and discards a mismatch.

Spec-key vs CID: `spec_key` lets two nodes agree on a request BEFORE the bytes
exist (the dedup that makes generation cost once network-wide, and what a regional
can seed by). `cid` addresses the bytes AFTER they exist (the dedup that makes
identical bytes from different specs share storage, and the tamper proof). The
index maps `spec_key -> cid` so a spec-hit returns the known cid without
regenerating.

### 7.3 AssetRecord - the local index entry (gzip JSON, like ImageCache index)

```
AssetRecord = {
  "cid":         str,     # "blob:sha256:..."  (primary key, == filename)
  "spec_keys":   [str],   # one or more specs that resolved to this cid
  "kind":        str,
  "size":        int,
  "scope":       str,     # PrivacyScope; gates sharing + seeding
  "shareable":   bool,    # derived: scope in {federated, public} AND consent ok
  "source":      str,     # "local_generate" | "peer:<node8>" | "central" | "seed:<origin8>" | "url:<host>"
  "created_at":  float,
  "last_access": float,   # LRU eviction key (reuse ImageCache._evict_locked)
  "pinned":      bool,     # exempt from eviction (seed-set members are pinned while hot)
  "demand":      int,      # local request counter (the popularity input, gap 9)
}
```

On-disk layout (sibling of media_index):
```
<data>/data/asset_cache/
  blobs/<cid[12:14]>/<cid-without-prefix>      # raw bytes, content-addressed
  index.json.gz                                # {cid: AssetRecord}, gzip, atomic write
  seed_manifest.json                           # last verified manifest this node pulled
```

### 7.4 AssetProvider - the federated provider index (closes GAP 4)

A DHT-lite "who has what" record, populated by signed announces AND by gossip
pre-announce digests. Lives in the social DB next to PeerNode (local nodes) and is
also held centrally/regionally as the durable directory and the seed-set input.

```
AssetProvider = {
  "cid":        str,    # indexed
  "node_id":    str,    # announcer's public key hex
  "scope":      str,    # the announced (shareable) scope
  "size":       int,
  "last_seen":  datetime,
  "signature":  str,    # node_integrity.sign_json_payload over (cid,node_id,scope,size)
}
# UniqueConstraint(cid, node_id)  -- one provider row per (blob, holder)
```

### 7.5 AssetDemand - the popularity signal (closes GAP 9)

A compact per-CID request counter a node up-syncs (metadata only, counts not
content) so a regional host can compute the hot seed set. Reuses the offline-first
`SyncEngine.queue` for delivery; aggregated regional-side.

```
AssetDemand = {
  "cid":        str,    # indexed
  "spec_key":   str,    # so a regional can seed by spec for deterministic gens
  "kind":       str,
  "size":       int,
  "count":      int,    # requests in the window
  "window":     str,    # e.g. "2026-06-29T20"  (hourly bucket)
  "scope":      str,    # only shareable demand is eligible for seeding
}
```

Only `shareable` CIDs contribute demand that can drive a seed; EDGE_ONLY demand is
never up-synced (privacy-first).

### 7.6 SeedManifest - the regional push payload (closes GAP 10)

A small, SIGNED list of CIDs a regional/central origin tells its sub-fleet to
pre-warm. It carries NO blob bytes (the node pulls them), so the manifest is tiny
and storm-safe.

```
SeedManifest = {
  "origin":     str,        # regional/central node_id (the seed origin + signer)
  "tier":       str,        # "regional" | "central"
  "epoch":      int,        # monotonic; a node ignores an older epoch (anti-rollback)
  "entries":    [ {cid, size, kind, scope} ],   # all scope in {federated, public}
  "budget":     int,        # total bytes; a node may pull a prefix if its cap is smaller
  "stage":      str,        # "canary" | "full"  (staged rollout, reuse OTA gate)
  "cert":       dict,       # key_delegation cert chaining origin -> master
  "signature":  str,        # node_integrity.sign_json_payload over the above
}
```

### 7.7 P2P protocol - the `asset` PeerLink channel (closes GAP 3)

One new channel in `CHANNEL_REGISTRY` (id `0x0A`, `data_class=OPEN`, reliable, low
priority - bulk so seed pulls never starve interactive channels). Message types,
all small JSON except GET_OK which streams bytes via `send_binary`:

```
ANNOUNCE  {t:"announce", cid, scope, size, node_id, sig}   # broadcast (signed)
HAVEDIGEST{t:"have", cids:[...], node_id, sig}             # gossip pre-announce digest
WANT      {t:"want", cid}            -> reply HAVE {t:"have", cid, size} | none
GET       {t:"get", cid}             -> GET_OK (binary frame, raw bytes) | GET_NO
```

- ANNOUNCE and the SeedManifest ride the EXISTING loop-safe relay shape (LRU msg
  dedup + hop_ttl + relay_path from `message_bus.py`). A relay forwards only a
  signature-valid payload; it holds no key and cannot forge one.
- HAVEDIGEST is the gossip pre-announce: a node piggybacks its shareable CID set
  on the existing gossip round (the `peers` channel), so the provider index is
  warm BEFORE a WANT.
- WANT is `PeerLinkManager.broadcast('asset', want)` then `collect('asset', ...)`,
  or a targeted `send(peer_id, 'asset', want, wait_response=True)` when a provider
  is already known from the index (the common case under proactive seeding).
- GET is `send_binary` of the raw bytes from the holder; the requester verifies
  `content_id(bytes) == cid` before storing (closes GAP 8).
- SAME_USER peers (your own devices) may exchange any scope; PEER trust exchanges
  only `shareable` assets.

### 7.8 Central / regional origin - the bounded CAS endpoint (closes GAP 6)

Optional tier. Mirrors `pull_with_central_fallback` and the 10 TB ceiling. The
regional origin also serves the seed set it pre-positioned.

```
GET  /api/assets/<cid>            # serve bytes if held; 404 if not; verify on read
PUT  /api/assets   (auth)         # store a shareable blob; bounded by the ceiling
GET  /api/assets/<cid>/providers  # the AssetProvider directory for a cid
GET  /api/assets/seed/manifest    # the current signed SeedManifest for this region
POST /api/assets/demand  (auth)   # ingest an AssetDemand tick (regional aggregation)
```

All routes behind `_require_system_auth` (the `GET /api/media/image` precedent).
Backed by a central/regional `Asset(cid PK, bytes/blob-path, kind, size, pinned,
received_at)` table with `enforce_asset_ceiling`-style LRU eviction (copy
`federation.py:402`, key the SUM on `size`, evict coldest non-pinned; seed-set
CIDs are pinned while hot). PUT is offline-first via `SyncEngine.queue`. Central is
a CDN seed + backup origin, never a required hop.

### 7.9 The facade - `get_or_create` (Layer 1 core)

```
AssetCache.get_or_create(spec: AssetSpec,
                         generate_fn: Callable[[AssetSpec], bytes] | None = None
                         ) -> AssetRef   # {cid, path, source, scope}
```

Tiered resolution (cheapest first; every tier optional, central/regional may be
OFF). Under Layer 2 seeding the COMMON case terminates at step 1:

```
1. LOCAL spec-hit:  index[spec_key] -> cid -> blobs/<cid> present?  bump demand; return (source=local/seed)
2. LOCAL cid-hit:   (a sibling spec already produced this cid)      bump demand; return (source=local)
3. PEER:            known providers (from gossip pre-announce/index)? targeted GET.
                    else broadcast WANT + collect HAVE, pick a provider, GET,
                    verify content_id==cid, store, return.
4. CENTRAL/REGIONAL: parent_tier_url() set?  GET /api/assets/<cid>; verify; store; return.
5. GENERATE:        generate_fn(spec) -> bytes; cid=content_id(bytes); store;
                    map spec_key->cid; bump demand; if shareable: announce + best-effort PUT.
6. give up:         return None (caller degrades, e.g. shows a placeholder).
```

Every tier bumps the local `demand` counter for the resolved CID; that counter is
the input the seeding layer (section 8) consumes. Generation runs at most once per
CID network-wide in the steady state, and under proactive seeding it usually runs
on a regional host BEFORE any flat node needs it.

### 7.10 The seeding control surface (Layer 2; closes GAP 10, 11)

```
# Node side (every node)
AssetCache.record_demand(cid, spec_key, kind, size, scope)  # cheap tick; section 7.5
AssetCache.warm_from_manifest(manifest)   # verify sig+epoch+cert; pull missing CIDs
                                          # at idle (foreground-yielded), verify each,
                                          # pin them; bounded by local cap + budget

# Seed origin (regional/central only - gated by key_delegation.get_node_tier())
SeedPlanner.collect_demand(db)            # aggregate AssetDemand from sub-fleet
SeedPlanner.compute_seed_set(budget)      # top-K shareable CIDs within byte budget
SeedPlanner.ensure_held(seed_set)         # pull or generate each CID so origin holds it
SeedPlanner.publish_manifest(db, seed_set, node_ids, stage)
    # sign manifest; FleetCommandService.push_broadcast(cmd_type="seed_assets",
    #   params=manifest, node_ids=<sub-fleet>, issued_by=origin) staged/canary

# Seed listener (the OTA-push-listener analogue, gap 11)
run_seed_listener()   # drain durable seed cmds, then subscribe 'fleet.command';
                      # on a verified seed_assets cmd -> AssetCache.warm_from_manifest
```

### 7.11 Consent / egress gate (closes GAP 7)

```
AssetCache._may_share(record, dest_scope) -> bool
  # 1. scope_allows(record.scope, dest_scope)        edge_privacy.scope_allows
  # 2. ScopeGuard.check_egress({_privacy_scope, kind, spec_summary}, dest, ctx)
  #    -- flatten any spec text (prompt/title/url) into a top-level scan_text
  #       field so DLP + secret scanners actually inspect it (the export_captions
  #       trick at media_semantic_index.py:684).
  # 3. for a user-owned generated asset bound for central/seed:
  #    ConsentService.check_consent(db, owner_id, 'public_exposure')  (or
  #    auto_grant_with_notice for safe-by-default central egress).
  # Fail CLOSED: any gate unavailable -> not shareable, not seedable.
```

Only `_may_share` == True assets are ANNOUNCEd, contribute demand, enter a seed
set, are served to PEER-trust peers, or PUT to central. EDGE_ONLY assets (default)
live and die on the node that made them and are NEVER seeded. SAME_USER peers
(your own devices) may exchange any scope.

## 8. The seeding flow end to end (Layer 2)

```
Flat node A renders alive-home, calls get_or_create(poster_spec) x N times
   -> each call bumps demand[cid]; A up-syncs AssetDemand (shareable only) to its regional R
Regional R (idle):
   SeedPlanner.collect_demand()  -> sees poster_cid is hot across the sub-fleet
   compute_seed_set(budget)      -> poster_cid, orb_tex_cid, ... within byte budget
   ensure_held()                 -> R pulls or generates each; R now holds the bytes
   publish_manifest(stage=canary, node_ids=<subset>)  -> signed seed_assets cmd
Canary nodes:
   seed listener verifies sig+epoch+cert -> warm_from_manifest():
     for each cid not held: GET from a peer (gossip-known) else from R; verify; pin
   report health; no regression -> R re-publishes stage=full to the whole sub-fleet
Later, flat node B opens the alive-home:
   get_or_create(poster_spec) -> tier 1 LOCAL hit (poster was seeded). 0ms, 0 generate.
Central OFF / R unreachable:
   no manifest arrives; B resolves poster via local -> peer (gossip pre-announce
   still finds node A) -> generate. Slower first hit, identical result.
```

## 9. Framework module homes (proposed, not yet built)

A new package `core/asset_cache/` (loads cheaply, no heavy imports at module top,
same discipline as `media_semantic_index.py`):

```
core/asset_cache/
  __init__.py        get_asset_cache() singleton, AssetRef
  cid.py             content_id(bytes), spec_key(spec), canonical_json()
  store.py           BlobStore: content-addressed bounded LRU (generalize
                     ImageCache._evict_locked; key=cid; gzip atomic index; pin)
  cache.py           AssetCache.get_or_create + the tiered resolver + single-flight
                     + record_demand + warm_from_manifest
  egress.py          _may_share (ScopeGuard + ConsentService + scope_allows)
  peer_transport.py  the 'asset' channel HAVE/WANT/GET + HAVEDIGEST gossip pre-announce
  announce.py        signed CID announce over the existing relay shape
  seed_planner.py    SeedPlanner: collect_demand, compute_seed_set, ensure_held,
                     publish_manifest (REGIONAL/CENTRAL only)
  seed_listener.py   run_seed_listener (the ota_push_listener analogue)
  routes.py          GET/PUT /api/assets/<cid> (+ /providers, /seed/manifest, /demand)
```

Central/regional-side `Asset`, `AssetProvider`, and `AssetDemand` models land in
`integrations/social/_models_local.py` (next to FederatedPost) so they share the
existing DB, sync, and ceiling machinery. The ceiling enforcement reuses the
`enforce_asset_ceiling` algorithm verbatim, keyed on `Asset.size`. The seed
`cmd_type="seed_assets"` is added to `VALID_COMMAND_TYPES` so it flows through the
existing signed fleet bus, drain, and verify path with no new transport.

Gate 6 (cx_Freeze): every new `core/asset_cache/*.py` must be added to
`Nunba-HART-Companion/scripts/setup_freeze_nunba.py` `packages[]` with an
`__init__.py`, or the installed Nunba.exe will `ModuleNotFoundError` on first use.

## 10. Phased build plan (Layer 1 first; Layer 2 strictly after)

The synthesis builds bottom-up: ship the reactive CORE end to end and prove it
works with central OFF before adding ANY seeding. Each phase is independently
shippable and testable.

- Phase 1 (CORE local + adapter). `cid.py` + `store.py` BlobStore +
  `get_or_create` with only the LOCAL and GENERATE tiers. Wrap `ImageCache` as a
  FAC adapter (deletes the `sha1(url)` parallel key, Gate 4). Outcome: per-node
  generate-once + the web-image cache now content-addressed.
- Phase 2 (CORE peer tier). The `asset` PeerLink channel (HAVE/WANT/GET) +
  `AssetProvider` index + signed ANNOUNCE over the existing relay + verify-on-
  fetch. Outcome: the second node fetches a blob P2P instead of regenerating;
  works with central OFF. This is the complete decentralization-first contract.
- Phase 3 (CORE central fallback). `GET/PUT /api/assets` bounded CAS origin +
  `pull_with_central_fallback`-shaped tier 4 + offline-first PUT. Outcome: a
  bounded CDN/backup origin as an accelerant, never a gatekeeper.
- Phase 4 (ACCELERANT seeding). Demand ticks + `AssetDemand` up-sync +
  `SeedPlanner` (collect/compute/ensure/publish) + `seed_assets` cmd_type +
  `run_seed_listener` + `warm_from_manifest`, canary-staged. Outcome: the
  alive-home's first paint is a tier-1 local hit because the popular CIDs were
  seeded at idle.

Each phase ships its behavioral tests (section 14) and is no-op-safe with the tier
above it absent.

## 11. First consumers (proves reuse, retires a parallel path)

1. W10 ImageCache becomes a FAC adapter (Phase 1). `ImageCache.get_path(url)`
   keeps its signature but internally: `fetch(url) -> bytes -> cid=content_id(bytes)
   -> BlobStore.put`. The web-image cache, the seed cache, and the federated cache
   become ONE store. This deletes the `sha1(url)` parallel key (Gate 4).
2. The alive-home producer (Netflix-home cards, orb textures, agent generator art
   - tasks #140, #143). Each card/texture is a spec; the home calls
   `get_or_create(spec, generate_fn)`. Under proactive seeding the regional host
   pre-generates the popular ones and pushes them, so the FIRST paint on a fresh
   node is a local hit, which is what "generation should not cost per-node" asks
   for.
3. Generated images / TTS clips. Any deterministic generate becomes a spec; the
   network makes it once and seeds the hot ones.

## 12. Failure modes and central-OFF behavior

- Central/regional unreachable: `parent_tier_url()` empty or the manifest never
  arrives -> the seeding layer is a no-op; resolution proceeds local -> peer
  (gossip pre-announce still works P2P) -> generate. No feature loss; only the
  pre-warming is gone. Layer 2 degrades cleanly to Layer 1.
- No peers AND no seed: tier 3/4 yield nothing -> generate locally, announce when a
  peer later appears. The node is fully functional standalone.
- Bad / poisoned seed: the manifest is signed + cert-chained to master; a forged
  manifest is dropped by `verify_command_signature`. A valid-but-bad seed is caught
  by the canary stage (reuse OTA staged rollout) before it reaches the full
  sub-fleet, and every pulled byte is CID-verified, so a manifest can never inject
  wrong bytes for a CID.
- Stale seed: a CID is immutable bytes, so a seed is never "wrong", only possibly
  unused. The `epoch` field lets a node ignore an older manifest; the LRU evicts a
  seeded-but-cold CID once it is unpinned at the next epoch.
- Seed floods the disk: `warm_from_manifest` is bounded by the local store cap and
  the manifest `budget`; a node pulls only the prefix that fits, pins those, and
  lets the LRU manage the rest. A seed never forces eviction of a pinned local
  asset.
- Malicious peer serves wrong bytes: `content_id(received) != cid` -> discard, try
  the next provider. The CID makes transport trust unnecessary on the seed path
  and the cold path alike.
- Announce/manifest storm: bounded by the reused LRU msg-dedup + hop_ttl +
  relay_path; a node relays each payload at most once.
- Foreground contention: `warm_from_manifest` and the seed pulls yield via the
  same `should_yield_to_user` gate every background daemon uses, on the low-priority
  `asset` channel, so seeding never competes with an interactive request.

## 13. Tradeoffs (CORE alone vs CORE + seeding)

- Bandwidth vs latency/compute. Seeding spends idle bandwidth pre-positioning
  popular assets to eliminate first-hit latency AND first-hit generation. The core
  alone spends nothing until asked but pays a cold generate or a WANT round-trip on
  the FIRST request per node. Seeding wins when an asset is popular (many nodes
  want it); the core wins for long-tail assets only one node ever needs. Because
  the seed set is top-K-by-demand-within-budget, seeding only pre-positions the
  popular head and lets the long tail resolve lazily, so the full design is the
  core PLUS a warm head, never a replacement.
- Freshness vs reuse. Immutable CIDs mean a seed is never stale-wrong; the only
  cost of a stale seed is wasted cache space, bounded by the budget + LRU. Mutable
  url-source specs use the soft `ttl` and are seeded conservatively or not at all.
- Centralization risk. Proactive seeding INTRODUCES a regional/central seed origin,
  which is the one place the design could drift toward a gatekeeper. The binding
  mitigation (section 4): the manifest can only ADD warm CIDs, never gate
  resolution; a node may ignore it; with the origin OFF, seeding == nothing. The
  seed origin is a convenience CDN, master-anchored and signature-verified, that
  pushes only PUBLIC assets DOWN and never reads private state UP.
- Complexity. Seeding adds three pieces over the core (demand signal, seed planner,
  seed listener). All three reuse existing machinery (SyncEngine.queue,
  push_broadcast + node_ids scope + OTA staged rollout, the ota_push_listener
  drain-then-subscribe shape), so the new surface is small and rides proven,
  signature-gated transports rather than a new one.

## 14. Non-goals / out of scope

- Not a general file-sync or backup system (that is the sync_engine's job).
- Not for private user data sharing: EDGE_ONLY is the default and is never seeded.
- Not a replacement for OTA: OS closures keep their signed pinned-pull path; FAC is
  for app/UI assets. (FAC reuses the OTA push/pull SHAPE, not its OS apply path.)
- No new identity or trust layer: FAC rides the existing PeerLink handshake +
  node_integrity signatures + master-anchored cert chain + fleet bus.
- No mutable assets: a CID is immutable bytes; "update" means a new spec -> new cid
  (the ttl is only a soft freshness hint for re-fetch of url-source specs).
- No master-key interaction: FAC reads `MASTER_PUBLIC_KEY_HEX` to verify, never the
  private key, and never signs a release.

## 15. Test plan (behavioral, no grep tests - Gate 5)

- `content_id` / `spec_key`: same bytes/spec -> same key; one bit flip -> different
  key; spec key stable across key-order permutations (canonical json).
- `BlobStore`: put/get round-trip; LRU evicts coldest non-pinned over cap; pinned
  (seeded) retained; corrupt/missing blob file -> miss, not crash.
- `get_or_create` tiers: mock each tier; assert local-hit skips peer/central/gen;
  peer-hit verifies `content_id` and rejects a wrong-bytes provider; central OFF
  (empty `parent_tier_url`) still resolves via generate; generate-then-announce
  fires only for a shareable scope; every tier bumps demand.
- `_may_share`: EDGE_ONLY never announced/seeded; FEDERATED with PII in spec text
  blocked by `check_egress`; missing edge_privacy -> fail closed.
- Demand + planner: demand ticks aggregate; `compute_seed_set` picks top-K within
  budget and EXCLUDES non-shareable CIDs; `ensure_held` makes the origin hold each.
- Manifest: `publish_manifest` signs and scopes `push_broadcast` to the sub-fleet
  `node_ids` only (never global); a node with a NEWER epoch ignores an older
  manifest; an unsigned/forged manifest is dropped by `verify_command_signature`.
- `warm_from_manifest`: pulls only CIDs not held, verifies each CID, pins them,
  stops at the budget/cap; an unreachable origin -> no-op, node still functional;
  a wrong-bytes pull is discarded.
- Central-OFF (the binding invariant): with `parent_tier_url` empty, no manifest,
  the design resolves identically with and without seeding (local -> peer gossip
  pre-announce -> generate); no exceptions, no feature loss.
- Central ceiling: over-cap PUT evicts coldest non-pinned; pinned/seeded retained; a
  `GET /api/assets/<cid>` for an evicted blob 404s and the caller falls back to the
  source peer (never a silent wrong-bytes claim).

## 16. Open questions for the build phase

1. Demand transport: dedicated `AssetDemand` rows up-synced via `SyncEngine.queue`
   vs piggyback counts on the existing telemetry metadata leg. Leaning a small
   `POST /api/assets/demand` ingested regionally, queued offline-first.
2. Seed-set selector: pure top-K by demand vs a freshness/recency-weighted score,
   and how big the per-region byte budget is relative to a flat node's store cap.
   Start with top-K-within-budget; tune from real demand histograms.
3. Canary fraction + halt criterion for a seed rollout: reuse the OTA canary
   percentage and health-regression gate, or a lighter "N nodes pulled OK" check
   (a seed is far less risky than an OS switch).
4. Announce vs manifest overlap: a node that learns a CID from gossip pre-announce
   may pull it before the manifest arrives. That is fine (idempotent, CID-verified)
   but the planner should not double-count it as demand.
5. Should a flat node ever act as a mini seed origin for its SAME_USER devices
   (push the home posters to your phone before you open it)? Likely yes, reusing
   the same manifest shape scoped to `TrustLevel.SAME_USER` peers.
