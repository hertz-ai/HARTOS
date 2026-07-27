# HARTOS Marketing Flywheel: Recovery Brief

This is a handoff brief for whoever picks up the marketing-flywheel work on HARTOS. The infrastructure is alive but the autonomous outreach loop is not closing. This document captures what the loop is supposed to do end-to-end, what is currently broken, and what needs to hold for "the flywheel is working" to be a true statement.

The work was last touched and claimed operational on 2026-03-25. A re-check on 2026-04-30 (via SSH + live container inspection + log review) found the loop functionally idle. The "operational" claim does not survive evidence. Treat this work as initial bring-up, not as a small repair.

## 1. Infrastructure inventory

This is the full picture of what runs where so a HARTOS session does not have to rediscover any of it. Verified by direct SSH inspection on 2026-04-30 unless marked otherwise.

### 1.1 Public-facing DNS and inbound entry

All Hertz AI public hostnames resolve to the same public IP. Traffic to the LAN comes through one NAT path.

| Hostname | Resolves to | Role |
| --- | --- | --- |
| hevolve.ai | 106.51.181.24 | Main landing site |
| www.hertzai.com | 106.51.181.24 | Corporate landing |
| hevolve.hertzai.com | 106.51.181.24 | Hevolve API endpoint |
| etime.hertzai.com | 106.51.181.24 | Landing/hosting (currently 502, upstream down) |
| azurekong.hertzai.com | 106.51.181.24 | Kong gateway endpoint |
| aws_hevolve.hertzai.com | 106.51.181.24 | Legacy AWS Hevolve API alias |
| docs.hevolve.ai | hertz-ai.github.io | Docs site on GitHub Pages (Nunba_Setup.exe, .dmg, .AppImage downloads) |

The 106.51.181.24 address NATs into the LAN and lands on Kong on 192.168.0.9:8100 (or its TLS ports 8543/8544). Confirm with Sathish exactly which inbound ports the WAN router forwards.

### 1.2 LAN production hosts

There are two production-relevant hosts on 192.168.0.0/24. Other addresses on the LAN are not production (see section 1.6).

#### Host: 192.168.0.9 (sathish-linux-deep)

This is the "central" node. The HARTOS agent backend, email service, Kong, monitoring, and most ancillary services live here.

- SSH: port 422, user sathish, standard password (`506066Hertzai2021.`).
- OS: Ubuntu/Debian-based Linux. Hostname `sathish-linux-deep`. NIC `enp3s0` is the LAN-facing interface.
- Has 5 Docker bridge networks (`docker0`, `br-d45274ea1c8b`, `br-918d4626ec3e`, `br-5e3895f01c64`, `br-a39656213c1e`) in addition to host networking.
- Repos checked out under `/opt/hzai-*` (one per service).

Hevolve-relevant containers and services on .9 (only the components the flywheel touches or depends on are listed):

| Container / Process | Port | Purpose in the flywheel |
| --- | --- | --- |
| langchain (Docker) | 6777 | HARTOS agent backend. The flywheel's agent_daemon, goal_manager, dispatcher, and outreach tools all run inside this container. This is the box the fix lives on |
| email-service (PM2 node) | 4000 | Outbound email API. Repo at `/opt/hzai-email/repo/email-service/`. SMTP relay to smtp.1and1.com:465. From cortext@hertzai.com. Tracks opens/clicks in `email_tracking.json` written next to the source. Every outreach send the agent makes ends up here |
| kong (Docker) | 8100→8000 (proxy), 8101→8001 (admin), 8543→8443 (TLS proxy), 8544→8444 (TLS admin) | Public API gateway. Inbound public traffic for `azurekong.hertzai.com`, `hevolve.hertzai.com`, `hevolve.ai` API paths terminates here. The agent daemon bypasses Kong when it dispatches to localhost:6777 directly, which is what creates the auth gap (Fault B in section 4) |
| konga (Docker) | 1337 | Kong admin UI. Use this to inspect or change Kong routing |
| kong-database (Docker) | internal :5432 | Kong's Postgres. Internal only |
| crawl4ai-service (Docker) | 8094 | Web crawler. Used in step 1 of the flywheel (prospect ingestion). Confirm health at http://192.168.0.9:8094/health before relying on it |
| hevolve_db_final (Docker) | none published, internal-only | Hevolve database container. Hosts the Hevolve user database and is the persistence layer the Hevolve API talks to |
| chatbot_pipeline (Docker) | 8001→9890 (also internal 6006, 8888) | Hevolve chatbot pipeline. Source of the API exposed as `aws_hevolve.hertzai.com:6006` |
| gatus (Docker) | 8091→8080 | Uptime monitoring for Hevolve services. This is the source of the `GATUS_EMAIL TEST` mails currently dominating the email-service log. When you do not yet see real outreach mails in that log, gatus tests are what you are seeing instead |
| allvms2_dns (Docker) | 5011 | DNS coordination for the Hevolve cloud name scheme. Populates `azurekong.hertzai.com`, `azure_all_vms.hertzai.com`, `aws_hevolve.hertzai.com` |
| named + dnsmasq (host) | 53 | Recursive DNS for the host and Docker bridges. Required for in-container name lookups |
| sshd (host) | 422 | SSH (non-standard port) for ops access |

The langchain container was started by this command (recovered from shell history):
```
sudo docker run -d --name langchain --restart unless-stopped \
  -p 6777:6777 --env-file .env \
  -e HEVOLVE_MASTER_PRIVATE_KEY="$(sudo cat /etc/hevolve/master_private_key.hex)" \
  -v "$(pwd)/config.json:/app/config.json:ro" \
  -v "$(pwd)/release_manifest.json:/app/release_manifest.json:ro" \
  -v /opt/hzai-LLM-Langchain-Chatbot-Agent/logs:/app/logs \
  -v /opt/hzai-LLM-Langchain-Chatbot-Agent/mount/images:/app/output_images \
  langchain_gpt:main
```

Note the four mounts. `/app/agent_data` is NOT mounted. State written there does not survive a rebuild. This is the persistence gap that wiped the flywheel state on 2026-04-28.

The host `.env` file at `/opt/hzai-LLM-Langchain-Chatbot-Agent/repo/LLM-langchain_Chatbot-Agent/.env` is what supplies the container env via `--env-file`. Its current contents:
```
OPENAI_API_KEY="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"
LANGCHAIN_API_KEY="ls__7099736e1e5e4079bb9f6e5b3db0d15c"
GROQ_API_KEY="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"
LANGCHAIN_PROJECT="hz-langchain-test"
HEVOLVE_NODE_TIER=central
HEVOLVE_ENFORCEMENT_MODE=hard
ALLOWED_HOSTS=localhost,127.0.0.1,172.17.0.1,azurekong.hertzai.com,hevolve.hertzai.com,hevolve.ai,azure_all_vms.hertzai.com
CORS_ORIGINS=http://localhost:3000,https://hevolve.ai,https://www.hevolve.ai
```

This is where `HEVOLVE_NODE_TIER=central` enters the container. The decision to drop to `flat` (option 3 from section 4 Fault B) would happen here.

Treat these API keys as compromised: they are committed in plaintext in a publicly-readable file on the host and now in this brief. Rotate them on the next opportunity. Do not commit this brief into a public repo.

#### Host: 192.168.0.83 (Erxes CRM)

This is the registry / CRM machine. Memory calls it the "registry machine". It hosts only the Erxes CRM stack.

- SSH: port 22, user sathish, standard password.
- Note: from 192.168.0.9 the host is currently `No route to host` for direct TCP connects. From the dev box (192.168.0.165) it is reachable. Confirm whether this is a VLAN/firewall separation or a transient ARP issue with Sathish. The flywheel script depends on 192.168.0.9 being able to reach 192.168.0.83 over HTTP for Erxes API calls, so this is potentially a real blocker that needs to be checked the first time you try the autonomous loop.
- Erxes UI on :3000.
- Erxes API on :3300 (GraphQL).
- 6 containers: erxes-mongo, erxes-redis, erxes-elasticsearch, erxes-api, erxes-ui, erxes-integrations.
- Admin login: sathish@hevolve.ai / Hertzai2021.
- Configured Board: "HARTOS Sales". Pipeline: "Robotics Outreach".
- Pipeline stages (in order): New, Contacted, Replied, Meeting, Negotiation, Won, Lost.
- Known quirk: after a redis container restart, the Redis hostname key must be re-set to `erxes-redis`. The flywheel setup script does this automatically (`redis-cli SET erxes:hostname erxes-redis`).
- Known quirk: deal create/edit returns "Url is invalid" webhook error in the API response. This is non-fatal. The DB update still succeeds. erxes_client.py handles it as a non-fatal warning.

### 1.3 Cloud hosts (Hevolve / Hertz AI only)

The shared `moba.txt` on the dev box lists four cloud hosts under the Hertz AI / Hevolve account. Confirm current role and state with Sathish before relying on any of them. Reachability shown is from 192.168.0.9 directly.

| Host | User | Reachable from .9? | Role |
| --- | --- | --- | --- |
| 104.254.246.77 | root | SSH open | Hevolve infra cloud node. Confirm specific role with Sathish |
| 4.224.23.154 | azureuser | unreachable from .9 directly | Azure VM in the Hevolve cloud pool. Reached via SSH from the dev box |
| 20.235.122.145 | azureuser | unreachable from .9 directly | Azure VM in the Hevolve cloud pool |
| 20.193.147.18 | azureuser | unreachable from .9 directly | Azure VM in the Hevolve cloud pool |

These Azure nodes are registered into the Hevolve cloud DNS scheme through `/opt/allvms2_auto_dns/` on 192.168.0.9 and surface under names like `azurekong.hertzai.com` and `azure_all_vms.hertzai.com`. The flywheel does not need to talk to them directly. They become relevant only if outbound traffic from email-service or HARTOS has to route through Azure, or if Erxes-side webhook receivers are hosted there. Confirm with Sathish.

Cloud SSH keys and passwords are in `~/Downloads/moba.txt` on the dev box (192.168.0.165). Do not move that file off the dev box.

### 1.4 The dev machine

192.168.0.165 (Windows 11) is Sathish's primary dev box. All Hevolve and HARTOS source repos live under `C:\Users\sathi\PycharmProjects\` (HARTOS, Hevolve, Hevolve_Database, Nunba, Nunba-HART-Companion, email-service, kong, crawl4ai-service). Cloud SSH credentials in `~/Downloads/moba.txt`. SSH private keys in `~/.ssh/`.

### 1.5 Hevolve DNS scheme

192.168.0.9 runs `named` (bind) plus an `allvms2_dns` Docker container on :5011. The shell script `/opt/allvms2_dns.sh` orchestrates Hevolve cloud-name registration across Azure and AWS through a Python helper at `/opt/allvms2_auto_dns/`. This is what populates `azurekong.hertzai.com`, `azure_all_vms.hertzai.com`, `aws_hevolve.hertzai.com`. If a Hevolve hostname stops resolving, check this stack first. Do not assume cloud DNS.

### 1.6 Memory-side reference for the user

A user-side memory file at `C:\Users\sathi\.claude\projects\C--Users-sathi-PycharmProjects-Hevolve-Database\memory\infra_map.md` on the dev box holds the cached version of the Hevolve infra map. Keep it in sync with this brief when anything material changes. The 2026-04-30 verification has already been applied.

## 2. What the autonomous loop is supposed to do end-to-end

1. **Prospect ingestion.** Crawl4AI scrapes target lists (robotics teams, university labs, integrators). Each scraped contact becomes a row in `/app/agent_data/outreach_prospects.json` AND a customer in Erxes with a deal in the Robotics Outreach pipeline at stage "New".
2. **Sequence assignment.** Each new prospect gets a marketing sequence: Day 0 cold email, Day 3 follow-up, Day 7 second follow-up, Day 14 final. Sequence state persists in `outreach_prospects.json["sequences"]`.
3. **Goal dispatch.** The agent_daemon picks up active sales/outreach goals each tick and dispatches them to the LLM via `/chat`. The LLM has access to outreach_crm_tools (send_email, log_send, update_prospect_stage, mark_replied) loaded based on the goal_type's tool_tags.
4. **Email send.** While executing the goal, the LLM calls send_email which POSTs to email-service on :4000. Each send is logged in `outreach_prospects.json["sent_log"]` and reflected as deal activity in Erxes. The deal moves from New to Contacted.
5. **Reply detection.** A separate poller checks the IMAP inbox for cortext@hertzai.com. Inbound replies match against prospects by sender email. On match: pause that prospect's sequence, set the Erxes stage to "Replied", push a notification, dispatch a response-draft goal to the agent.
6. **Follow-up daemon.** Every tick the follow-up daemon checks `sequences` for prospects whose next-touch date has arrived. It queues the next email goal for the agent.

The loop is closed when steps 1 through 6 happen on a clock without human input. Currently none of these are happening end-to-end.

## 3. Current state, verified 2026-04-30

The infrastructure is up. The loop is not closed.

- agent_daemon ticks every cycle. Log line `Agent daemon: dispatched 1 goal(s) to idle agents` recurs in the container logs.
- The work the daemon picks is not outreach. It is robotics-health-monitor goals that immediately skip with `no locomotion, manipulation, or sensors detected`.
- The only marketing-tagged goal in the daemon's in-memory state, id `2bd035c5-6302-43db-ba96-c8c6e1c474ea`, gets HTTP 401 on every dispatch attempt. The exact response from `localhost:6777/chat` is `{"error":"Authentication required (Bearer token)"}`. After 7 consecutive failures the daemon auto-paused this goal. The log shows `Goal 2bd035c5-... AUTO-PAUSED after 7 dispatch failures`.
- `/app/agent_data/outreach_prospects.json` is the empty stub `{"prospects":{}, "sequences":{}, "sent_log":[]}`. Zero prospects, zero sequences, zero sends.
- email-service log shows only GATUS heartbeat test mails in recent traffic. No outreach mail going out.
- The container was rebuilt at 2026-04-28 16:43 (file Birth times under `/app/agent_data` all show that timestamp). All in-container state from before the rebuild is gone.

## 4. The two compounding faults that need to be addressed

Neither one alone explains the failure. Fixing only one leaves the loop broken.

### Fault A: container state is ephemeral and the canonical setup script was never re-run after the 2026-04-28 rebuild

There is an idempotent setup script at `deploy/marketing_flywheel_setup.sh` in this repo. Read it end-to-end. It does the following:
1. Verifies Erxes containers on 192.168.0.83 are running.
2. Verifies crawl4ai and email-service are responsive on 192.168.0.9.
3. SCPs and `docker cp`s a specific list of agent-engine Python files into the container.
4. Writes `/app/.env` inside the container with HEVOLVE_DB_URL=sqlite:////app/agent_data/hevolve.db, ERXES_API_URL, ERXES_EMAIL, ERXES_PASSWORD, HEVOLVE_ENFORCEMENT_MODE=warn.
5. Installs `sitecustomize.py` in the container's site-packages so Python loads `/app/.env` at process start.
6. Creates `/app/agent_data/hevolve.db` via SQLAlchemy and seeds two AgentGoal rows: goal_type=sales ("HARTOS Robotics Partnership Outreach") and goal_type=outreach ("HARTOS Outreach Follow-up Daemon"), both status=active with config_json={continuous:True, persistent:True}.
7. Re-signs the release manifest if code hashes drifted.
8. Restarts the container and runs a verification sweep.

None of these artifacts exist in the current container. Verified by direct inspection on 2026-04-30:

| Path / variable | Expected | Actual |
| --- | --- | --- |
| `/app/.env` | exists with HEVOLVE_DB_URL etc | missing |
| `/app/agent_data/hevolve.db` | SQLite file with AgentGoal rows | missing |
| `/app/agent_data/outreach_prospects.json.bak` | backup written before restart | missing |
| `/usr/local/lib/python3.10/site-packages/sitecustomize.py` | env loader installed | missing |
| runtime env HEVOLVE_DB_URL | set | unset |
| runtime env ERXES_API_URL | set | unset |
| runtime env ERXES_EMAIL | set | unset |
| runtime env HEVOLVE_API_KEY | (optional) | unset |

Root cause for A is straightforward. The container was rebuilt 2026-04-28 16:43 and the script was not re-run afterwards. The container has only four volume mounts (config.json, release_manifest.json, logs, output_images). `/app/agent_data` is NOT volume-mounted, so anything the script wrote there last time was wiped.

This is also a persistence design problem in addition to a missed re-run. Even if you run the script today, the next rebuild will wipe state again. The fix is one of:
- Bind-mount `/app/agent_data` to a host directory in the `docker run` line.
- Move the setup script into the container's boot sequence so a rebuild re-runs it automatically.
- Both, ideally.

Recommendation: bind-mount AND make the script idempotent enough to safely run on every container boot.

### Fault B: the agent_daemon's HTTP dispatcher cannot authenticate against its own backend on central tier

This is the harder one. It would silently keep the loop from closing even after Fault A is fixed.

Background:
- Container env has `HEVOLVE_NODE_TIER=central` (sourced from the host `.env` passed via `--env-file`).
- Commit `8cce62d` on 2026-03-14 ("CRIT-1: Auth enforcement on /chat for exposed deployments") added a middleware gate. `security/middleware.py` line 260 onward enforces it. The relevant branch is at line 306:
  ```
  if node_tier == 'central':
      resp = _require_api_key_or_bearer(expected_key='')
      if resp is not None:
          return resp
  ```
  With `expected_key=''` the function skips the X-API-Key check and falls through to require a Bearer JWT. With no Authorization header on the request it returns `{"error":"Authentication required (Bearer token)"}` at line 264. This is the exact 401 the daemon sees.
- The agent_daemon's Tier-2 HTTP dispatch path is in `integrations/agent_engine/dispatch.py` around line 530. It calls:
  ```
  resp = pooled_post(f'{base_url}/chat', json=body, timeout=120)
  ```
  No Authorization header. The body has user_id, prompt_id, prompt, etc. but no token.
- The dispatcher's Tier-1 in-process path tries `from routes.hartos_backend_adapter import chat as hevolve_chat`. That module lives in the Nunba-HART-Companion repo, NOT in HARTOS. It does not exist in the langchain image. Tier-1 always fails silently here (the outer `except ImportError: pass` swallows it), so Tier-2 HTTP is the only real path on this server.

The commit's documented design intent is:
- Behind Kong: Kong handles auth at the edge.
- Central tier without Kong: client must provide JWT.

The internal daemon to localhost:6777 dispatch does neither. Kong listens on :8100, so the daemon bypasses Kong when it hits localhost:6777 directly.

Options to fix Fault B:
1. **Mint a node-local JWT in dispatch.py and attach `Authorization: Bearer <jwt>`** to internal /chat calls. `HEVOLVE_MASTER_PRIVATE_KEY` is already set in the container env, and `integrations/social/auth.py` has the JWT mint/decode primitives. This is the cleanest fix because it preserves the central-tier security model.
2. **Re-route internal dispatch through Kong** on :8100. Only do this if Kong is already configured to accept a known internal token and re-issue downstream. Check Kong's current routing config before going this way.
3. **Drop HEVOLVE_NODE_TIER to flat for this container** if the box is not actually meant to be a multi-user central node. Check whether anything outside this host legitimately POSTs to :6777. If nothing does, flat is honest and the auth block goes away.

Recommendation: option 1, with option 3 as the fallback if minting is too invasive. Do not pick option 2 without first reading Kong's config.

## 5. What needs to be true for "the flywheel is working" to be a true statement

All of these must hold simultaneously, verified by direct inspection on 192.168.0.9. If any one of them is false, the flywheel is not working.

1. **Persistent state.** `docker inspect langchain --format '{{range .Mounts}}{{.Destination}} {{end}}'` includes `/app/agent_data`.
2. **Setup is reproducible across rebuilds.** Either the docker run command bind-mounts `/app/agent_data`, or the container boot script re-runs the setup. A fresh rebuild should not require manual intervention.
3. **Goal DB exists and has active goals.** `docker exec langchain ls -la /app/agent_data/hevolve.db` returns a non-empty file. Querying AgentGoal returns at least one row each of goal_type sales and outreach, both status=active.
4. **Prospects are seeded.** `docker exec langchain cat /app/agent_data/outreach_prospects.json` returns at least one prospect with an active sequence.
5. **Dispatch succeeds.** `docker logs langchain --since 1h | grep -E '401|AUTO-PAUSED'` is empty. `docker logs langchain --since 1h | grep -iE 'sent|reply_detected|email_sent'` shows real activity in the last hour.
6. **Email is actually going out.** `pm2 logs email-service --lines 50 --nostream | grep -i cortext` shows outreach sends, not just GATUS heartbeats.
7. **Erxes shows new deal activity.** Recent deal entries on http://192.168.0.83:3000 in the Robotics Outreach pipeline with timestamps from the last day.
8. **Reply detection closes the loop.** Send a test reply from a known address that exists in prospects. Within one tick, the prospect's Erxes stage moves to "Replied" AND `outreach_prospects.json["sequences"]` for that prospect shows paused.

If you cannot verify all eight, do not claim operational.

## 6. Files to read first when picking this up

In this order:

1. `deploy/marketing_flywheel_setup.sh`. The canonical setup. Read every step. Understand what it writes and where.
2. `integrations/agent_engine/dispatch.py` lines roughly 420-625. The Tier-1 then Tier-2 dispatch flow that 401s.
3. `security/middleware.py` lines roughly 195-312. The central-tier gate that blocks the dispatch.
4. `integrations/agent_engine/outreach_crm_tools.py`. The tools the LLM calls when it picks up a marketing goal.
5. `integrations/agent_engine/erxes_client.py`. The Erxes integration. Cookie-based auth with auto-refresh. Customer find uses paginated listing (the ES searchValue path is broken in this Erxes version).
6. `integrations/agent_engine/agent_daemon.py`. The tick loop. Look for goal picking, tool tag loading, and the call into dispatch.
7. `integrations/agent_engine/goal_seeding.py`. Where stub goals get auto-seeded. Trace where goal `2bd035c5-6302-43db-ba96-c8c6e1c474ea` (goal_type=marketing) actually comes from. The setup script creates sales and outreach, not marketing.
8. `integrations/social/auth.py`. JWT mint and decode. Find a function you can call from dispatch.py to get a local-node token.
9. `hart_intelligence_entry.py` lines roughly 6100-6240. The /chat handler. The body-level user_id check at line 6229 is a second auth layer behind the middleware gate.

## 7. Open questions to resolve before claiming a fix

1. **Where does goal `2bd035c5-6302-43db-ba96-c8c6e1c474ea` come from?** With no hevolve.db on disk, it must be auto-seeded into in-memory state. Trace its origin. Decide whether this stub goal should exist at all or whether it is leftover scaffolding from an earlier design.
2. **Was the loop ever actually closed?** The 2026-03-14 auth enforcement commit landed 11 days before the "fully operational" claim on 2026-03-25. During those 11 days the daemon's HTTP dispatch on central tier would already have been failing the same way it is now. Either someone briefly ran the box on flat tier to seed prospects manually, or "operational" was a deployment claim rather than an end-to-end observation. Verify with Sathish.
3. **Should HEVOLVE_NODE_TIER actually be central on this box?** It is the main Linux VM. The only external entry path for /chat is via Kong on :8100 anyway. If nothing legitimately hits :6777 from outside the host, flat is more honest.
4. **Where does outreach_prospects.json get its initial population?** The setup script does not seed it. Look for a separate seed script, an Erxes-to-HARTOS import path, or a Crawl4AI pipeline that writes into it.

## 8. Order of operations for the recovery work

1. Read this brief. Read `deploy/marketing_flywheel_setup.sh`. Read dispatch.py around line 530 and middleware.py around line 260. Confirm the picture matches.
2. Decide the auth path. Pick option 1 (mint JWT in dispatcher) or option 3 (drop tier to flat). Do not pick option 2 without first checking Kong.
3. Bind-mount `/app/agent_data` to a host path so state persists across rebuilds. Update the docker run command wherever it is durably stored (systemd unit, ansible, or a wrapper script alongside `.env`).
4. Apply the auth fix from step 2.
5. Re-run `deploy/marketing_flywheel_setup.sh`. Verify all 8 truths from section 5 hold.
6. Seed prospects. Either via the Crawl4AI pipeline if you trust it, or via a controlled import of 5 to 10 known-good prospects to validate the loop without sending to a wide list.
7. Watch one full sequence cycle. Day 0 send, Day 3 follow-up, reply path. Verify Erxes deal stage moves.
8. Only then call the flywheel operational and update the memory entry on the user side.

## 9. What not to do

- Do not assume the older "FULLY OPERATIONAL" claim was correct. The evidence is that the autonomous loop has likely never closed under central-tier auth. Treat this as bring-up.
- Do not blanket-restart the container without first capturing the in-memory goal state and any non-persisted prospect data. There is none right now (verified empty), but do not lose new state to a careless restart once you start populating it.
- Do not disable middleware auth globally. The whole point of the central-tier gate is to keep /chat from being a free LLM endpoint for anyone who can reach :6777. Fix the dispatcher properly, do not bypass the gate.
- Do not commit `.env` files or any `HEVOLVE_MASTER_PRIVATE_KEY` value. The current key is already in the host `.env` in plain text. That is a separate hygiene problem worth flagging to Sathish but do not make it worse by checking it into a repo.
- Do not write follow-up emails that read like AI campaign mail. Lowercase subjects, short sentences, no em dashes. Sathish has been explicit about this.

## 10. Acceptance criteria

The work is done when:
- All eight truths in section 5 hold continuously for at least 24 hours of unattended operation.
- A clean container rebuild does not require manual setup re-run.
- A test prospect added on day 0 receives all three follow-ups on schedule, AND a manual reply from that test prospect's address moves their Erxes stage to "Replied" within one tick.
- Memory entry `project_outreach_campaign.md` on the user side is updated to reflect the new verified state, with the verification commands embedded so future re-checks are mechanical.
