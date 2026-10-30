# Speech therapy, singing and language learning agents: user journeys, driven live on the owner's desktop node (2026-10-30)

Owner's asks (30 Oct): "Deliverable is the agents created and fully working locally" and
"Build and test all". The flows come from the owner's own specification: the Teams note
"Speech therapy Agent Flows" (29-10-2024, seven flows), the Singing Agent spec
(25-06-2025), and the language learning / Spoken English chats (2023 to 2024). This
record follows the house rule of `USER_JOURNEYS_LIVE_2026-10-13.md`: one row per step,
setup, expected result, evidence label (measured, read, not checked), verdict. A verified
negative is worth as much as a pass. The repository is public: no internal hosts, ports,
private addresses or operator paths; the desktop node is "the desktop".

## Method

The desktop = the installed Nunba build (web bundle `main.e14da509`, HARTOS as frozen
in that build, local Qwen3.5-4B through llama.cpp, piper TTS, sherpa/whisper STT). Every
agent was created through the product's own chat-create path, the body the chat page
sends in local creation mode (`create_agent` and `autonomous_creation` true, a fresh
`prompt_id`), and the person's replies to the create loop's genuine asks were typed as
the parent or the child would type them. Nothing in the product was patched for these
runs. Reuse turns are the same `/chat` body with `create_agent` false. Evidence lives in
the node's `server.log` (quoted by its marker lines), the agent files under the node's
prompts folder, and the agent memory file; the owner's board holds the run notes
(agentcollab 2026-10-30 parts 11 to 14 and this document's follow-ups).

Legend: MEASURED = run on the desktop and read from its output; READ = read from code or
files without a run; NOT CHECKED = no evidence either way.

## 0. Cross-cutting steps every agent journey depends on

| Step | Setup | Expected | Evidence | Verdict |
|---|---|---|---|---|
| A person creates an agent from the desktop chat page while signed in and online | Chat page, "Create New Agent" form | The agent record appears on the node | MEASURED 16:0x IST: the form POSTs to the cloud create endpoint and stayed pending 4+ minutes (an empty POST answers 422 in 36 ms, so the gateway is up and the handler hangs) | FAIL on this build |
| A person creates an agent from the chat page in local mode (guest or offline) | Same page, `useLocal` path | The node's create loop authors the record and banks flows | MEASURED 17:00 IST with the same request body sent to the node: record in 76 s, flow banked, "Agent Created Successfully" at 17:09 (Syllable Coach) | PASS |
| "Talk to agent" on an agent card binds the chat to that agent | Agents page, card button | The next turn carries the agent's `prompt_id` | MEASURED 16:1x: navigates to `/agents/<slug>`, the turn went out as the generic assistant (`agent_id=local_assistant`) | FAIL |
| The create loop can finish an action whose named tool the local model never calls | Any action phrased "get_user_details to ..." | Action banks | MEASURED 16:25 to 16:48: two rounds of three attempts, 0 tool calls, "[NEEDS-INPUT] action 1 not completing after 3 attempts" each round | FAIL (model + verdict gate; harness #103/#107) |
| The create loop banks an action from the tool calls that actually ran | One-flow agent, tools the model calls | `[TRACE-BANKED] action N recipe derived from K executed step(s)` | MEASURED 17:01 to 17:08: actions 1, 2, 4 to 9 of the Syllable Coach, each from 1 to 3 steps | PASS |
| A model-written recipe cannot bank an action whose named tool never ran | Action 3 of the Syllable Coach (get_chat_history) | Refused like the trace bank refuses | MEASURED: banked `done` with `recipe: []` after the trace bank refused it | FAIL (harness #107 datum) |
| Reuse runs the whole banked flow | `create_agent` false, `prompt_id` set | All N actions in the ledger | MEASURED 17:09:43: `[REUSE-LEDGER] tasks=1 actions=1` of 9; root cause the persona vocabulary filter, fixed HARTOS 7e6faa900 (local, not in this build); replayed on the real files: fixed rule keeps 9 of 9 | FAIL on this build, fix pending a build |
| Reuse refuses a fabricated completion | Same turn | `[FABRICATED-COMPLETE]` refuses to advance | MEASURED 17:09:58: refused, "its tool(s) ['get_chat_history'] produced no real result" | PASS |
| The agent's messages are spoken aloud on the desktop | Any `send_message_to_user` | piper synthesises the text | MEASURED: `NunbaPiperTTS Synthesized audio` after each message, 29 syntheses 16:40 to 17:10 | PASS |
| Multi-language speech synthesis on the desktop | `/tts/voices` | Indic voices available | READ 10-30: backend piper, installed voices empty (English default only); Indic Parler, Kokoro, MMS exist in `tts/tts_engine.py` but are not installed here | FAIL on this desktop |
| The agent page keeps the microphone closed in Quiet Mode | `/agents/<slug>` page, "Quiet Mode Active" | No audio leaves the page | MEASURED 16:40:48 to 17:09:50: the page streamed a 6 s window every 3 s to the node's STT (458 chunks, all silence); the stream stopped when the tab left the page; Quiet Mode governs TTS only | FAIL (owner ruling open, task #123) |

## 1. Speech Therapy Agent, the owner's seven flows

Three builds on 30 Oct. Build 1 (16:24, prompt 1793357576203, seven flows authored from the
full description, 16 actions in the syllable flow) never left action 1. Build 2 (17:00,
"Syllable Coach", prompt 1793359802508, one flow) banked all nine actions. Build 3 (17:11)
was stopped by me before any proceed turn: the model had authored six desktop-command
actions for a child's exercise (see section 4). Build 4 (18:2x, "Speech Therapy Agent",
seven flows, 3 to 5 actions each, tools limited to message, memory and image) is the run
this section records; rows are filled as it proceeds.

| Flow | Step | Expected | Evidence | Verdict |
|---|---|---|---|---|
| 1 Name calling | Greet by name, register any response, record | Child greeted by name once; response saved | build 4 in progress | PENDING |
| 2 Family association | Ask the parent for names, show the child, evaluate, record | Names saved; child's answer judged; result saved | build 4 in progress | PENDING |
| 3 Flash cards | Card image, word spoken, attempt judged, record | `txt2img` card, feedback, result saved | build 4 in progress | PENDING |
| 4 Reverse flash cards | Two pictures, a sound, the child picks, record | Card image, feedback, result saved | build 4 in progress | PENDING |
| 5 Syllables | Level from memory, word spoken, attempt judged, progress saved | Creation-time exercise | MEASURED on build 2 (one-flow variant): "You said Ba, which is so close to ball! Now let's say ball one more time", spoken by piper; `syllable_progress`, `target_word`, `child_name` in agent memory | PASS at creation, reuse FAIL (section 0) |
| 6 Question answering (echolalia) | Question with its answer, child repeats only the answer, judged | Echo detected or answer celebrated | build 4 in progress | PENDING |
| 7 Concepts | Time of day asked, confirmed, why we eat | Result and the registration marker saved | build 4 in progress | PENDING |
| Any | The agent calls the Singing, Story or Flash-card agents (owner's composition rule) | Agent-to-agent call | READ: no agent-facing tool composes or calls another agent on this build (`agent_ui_compose` has no caller; `Navigate_App` reaches the kids hub only) | FAIL (gap 1) |
| Any | Talking Avatar, ComfyUI kid images, push notification, Edit Profile, Track Progress tools | Named in the owner's Flow 1 | READ: none of these tools exists on the speech_therapy goal type (tool tags memory, media, vision, consent); progress lives only in agent memory | FAIL (gaps 2, 3) |

## 2. Singing Agent (owner's spec 25-06-2025)

| Step | Expected | Evidence | Verdict |
|---|---|---|---|
| Song Creator writes a timestamped rhyme and saves it | Four lines with timestamps in memory | queued after section 1 | PENDING |
| Kid Listener delivers it and invites the child to sing along | Greeting by name, feedback on the child's line, `played_songs` saved | queued | PENDING |
| Avatar video generation through the mailer endpoints of the spec | A singing video | READ: the endpoints are cloud services the local create loop has no tool for; not attempted | NOT CHECKED |

## 3. Spoken English Coach (language learning)

| Step | Expected | Evidence | Verdict |
|---|---|---|---|
| Cafe scenario greeting, learner reply, one tip, tip remembered | `last_tip` and `conversation_turns` in memory | queued after section 2 | PENDING |
| Tamil-first learner gets the tip in English with Tamil TTS available | Indic voice | READ: no Indic voice installed on this desktop (section 0) | FAIL on this desktop |
| Spoken English Agent sidebar label (id 20) | Binds to a Spoken English recipe | MEASURED 2026-10-14 (USER_JOURNEYS_LIVE_2026-10-13.md section 11): a salvage stub, `Creation Mode`, no recipe | FAIL (unchanged) |

## 4. Hazards recorded during these runs

- Build 3 (17:11, prompt 1793360501025): asked for exactly three actions and no history
  lookup, the local model authored twelve, six of them desktop-command steps (open a code
  editor, type code, click run) for a child's speech exercise. Stopped before any proceed
  turn; no desktop tool ran (0 tool runs in the log); the record stays on the node with no
  recipe. Deleting it is the owner's call. Build 4's driver refuses to send a proceed turn
  when the authored record names a desktop or code tool.
- The create loop rewrote a saved fact: child age 4 (typed) became "5" in agent memory on
  the next round (build 1, 16:49). Fabrication of stored values, not only of completions.
- The agent page streams the microphone to STT with Quiet Mode on (section 0).

## 5. What this means for the 5 Nov live demo

Created through the product and spoken aloud: yes (build 2, and build 4 as its rows
turn PASS). One full exercise in reuse (person speaks, node transcribes, agent judges and
speaks back): not on this build. The reuse fix (HARTOS 7e6faa900) reaches the desktop
only through a HARTOS push (owner's go), a Nunba installer built from it, and an install
the owner performs.
