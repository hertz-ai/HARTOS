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
(agentcollab 2026-10-30 parts 11 to 16 and this document's follow-ups).

Two things a reader must know about how the person's side was played. First, one turn
of build 2 was not a child's or parent's reply but coaching: a 335-character message
(`msg_1793360160534_create`, 17:06:00 IST) telling the create loop how its step 3
should work ("the child's attempt is the latest user message, 'Ba' ... call
get_chat_history with text 'ball' and no dates, or take 'Ba' as the attempt ... continue
with step 4"). The flow banked after that turn, so build 2's PASS at creation includes a
coached step. Second, the local model flattens any multi-flow description into one flow
and swaps tools it does not like (build 4, section 1), so from build 5 on, one
description equals one owner flow, with a driver that answers the loop's asks from a
fixed table (child's attempt "Ba", parent's names "Mother is Priya, father is Ravi",
"Amma", "Water", "Morning") and refuses to send a proceed turn when the authored record
names a desktop or code tool.

Reviewed by hartos-f9 (formerly hartos-70) on 30 Oct; the first version of this record
overstated three things (nine trace-banked actions, no Indic voice, build 3 stopped
before any turn) and those rows are corrected below.

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
| Reuse runs the whole banked flow | `create_agent` false, `prompt_id` set | All N actions in the ledger | MEASURED 17:09:43: `[REUSE-LEDGER] tasks=1 actions=1` of 9; the one action ended at 17:10:12 with `[REUSE] Cannot advance action 1: state is status_verification_requested, not TERMINATED`. Root cause the persona vocabulary filter, fixed in HARTOS 7e6faa900 (on origin since bf061ec7a, central redeployed 13:28Z; reviewed APPROVED-WITH-NITS by hartos-f9, its M1 and a behavioural test of create_agents_for_user are open in the review queue; the 131-test figure covers the eight reuse and trace-bank files, not a full sweep); replayed on the real files: fixed rule keeps 9 of 9. The desktop runs the old rule until a new installer is installed | FAIL on this build, fix pending an install |
| Reuse refuses a fabricated completion | Same turn | `[FABRICATED-COMPLETE]` refuses to advance | MEASURED 17:09:58: refused, "its tool(s) ['get_chat_history'] produced no real result" | PASS |
| The agent's messages are spoken aloud on the desktop | Any `send_message_to_user` | piper synthesises the text | MEASURED: `NunbaPiperTTS Synthesized audio` after each message, 29 syntheses 16:40 to 17:10 | PASS |
| Multi-language speech synthesis on the desktop | piper voices folder and `/tts/voices` | Indic voices present and listed | MEASURED (hartos-f9, 30 Oct): the piper voices folder holds hi_IN-pratham, ml_IN-arjun, te_IN-maya and ur_PK-fasih (plus en_US-amy, zh_CN-huayan); direct piper synthesis gave Hindi 1.99 s, Telugu 1.25 s, Malayalam 1.39 s, all non-silent. A Hindi AGENT turn is unmeasured. `/tts/voices` answers `installed: []` because `_LazyPiper` has no voice-listing method and `list_installed_voices()` returns an empty list: a listing defect, not a missing voice | PASS for hi/te/ml synthesis; FAIL for the `/tts/voices` listing (defect) |
| Tamil speech synthesis on the desktop | piper voices folder | A Tamil voice | READ: no ta_IN piper voice in the folder; Indic Parler (which has Tamil) is in `tts/tts_engine.py` but not installed here; nothing synthesised | NOT CHECKED |
| The microphone stays closed while the person has not asked to speak | `/agents/<slug>` page in Chrome ("Quiet Mode Active"), and the Nunba desktop window | No audio leaves the UI | MEASURED (hartos-f9's count from the rotated log, mine from the current one): the node's STT received a 6 s window every 3 s from 16:13:20 to 17:09:50 IST (564 windows, all silence, VAD removed everything); the Chrome tab held the ws connection and the stream stopped when the tab left the page at 17:09; by 20:09 the stream was back from the Nunba desktop window itself (Nunba.exe and its msedgewebview2 hold the 8005 connection). Quiet Mode in the page governs TTS, not the microphone | FAIL (owner ruling open, task #123) |

## 1. Speech Therapy Agent, the owner's seven flows

Builds on 30 Oct, all on the installed desktop. Build 1 (16:24, prompt 1793357576203,
seven flows authored from the full description, 16 actions in the syllable flow) never
left action 1. Build 2 (17:00, "Syllable Coach", prompt 1793359802508, one flow) banked
eight of its nine actions from the trace (1, 2, 4 to 9); action 3 was refused by the
trace bank at 17:06:47 and banked `done` with `recipe: []` through the model-written path
(section 0). Build 3 (17:11, prompt 1793360501025): asked for three actions, the model
authored twelve, six of them desktop-command steps; three proceed turns ran (17:13:21,
17:13:30, 17:13:39), action 3 was started three times and ended in `[NEEDS-INPUT]` at
17:15:44, actions 1 and 2 banked, about 48 other tool calls ran, the desktop tool itself
was never called; the driver was killed at 17:14:46 (section 4). Build 4 (18:28, prompt
1793365100141, the seven flows in one description): the model flattened them into ONE
flow `main` of 29 actions and replaced the two `txt2img` card steps with
`execute_windows_or_android_command: generate and display ...`; the driver sent no
proceed turn, actions 1 and 2 banked inside the first turn, no recipe. From build 5 on,
one agent per owner flow.

| Flow | Step | Expected | Evidence | Verdict |
|---|---|---|---|---|
| 1 Name calling | Greet by name, register any response, record | Child greeted by name once; response saved | MEASURED build 5, prompt 1793365817449 ("Speech Therapy Flow 1 Name Calling"), created 18:5x IST in 404 s: 6 actions, recipe banked with steps on every action (tools send_message_to_user, save_data_in_memory, get_data_by_key); said "Hello Arjun! It's so nice to see you!" and, after the child's "Hi!", "Bravo Arjun! You did it!"; memory `child_name`, `vocab_known: []`, `name_response` saved. Caveat: `name_response` holds both `responded` and `no_response` and a fabricated 2024 timestamp | PASS at creation (with the memory caveat); reuse untested on this build |
| 2 Family association | Ask the parent for names, show the child, evaluate, record | Names saved; child's answer judged; result saved | MEASURED build 6, prompt 1793366231447 ("Speech Therapy Flow 2 Family Members"), created in 642 s: 5 actions, recipe banked with a step on every action; asked the parent for the names, asked the child "Arjun, who is this, Amma or Appa?", judged "Amma" correct; memory `family_members` and `family_association_result: correct`. Caveat: the parent typed "Mother is Priya, father is Ravi" and the model stored mother "Amma", father "Appa", with a fabricated 2024 timestamp | PASS at creation (names not kept); reuse untested |
| 3 Flash cards | Card image, word spoken, attempt judged, record | `txt2img` card, feedback, result saved | build 7, prompt 1793366883072: record authored (6 actions, "Flash Cards - Atomic"); the driver died with my session at 19:0x before any action banked; resumed on the same record at 20:1x | PENDING (run in progress) |
| 4 Reverse flash cards | Two pictures, a sound, the child picks, record | Card image, feedback, result saved | build 8 queued after flow 3 (an earlier attempt died during authoring, no record) | PENDING |
| 5 Syllables | Word spoken, attempt judged, result saved | Creation-time exercise | MEASURED on build 2: "You said Ba, which is so close to ball! Now let's say ball one more time", spoken by piper; memory holds `target_word: ball`, `child_name: Arjun`, `last_attempt: Ba`, `exercise_result: practised`; no syllable level is stored (the authored flow never saved `syllable_progress`) | PASS at creation (coached step 3, see Method); reuse FAIL (section 0) |
| 6 Question answering (echolalia) | Question with its answer, child repeats only the answer, judged | Echo detected or answer celebrated | build 9 queued | PENDING |
| 7 Concepts | Time of day asked, confirmed, why we eat | Result and the registration marker saved | build 10 queued | PENDING |
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
| Tamil-first learner gets the tip in English with Tamil TTS available | Tamil voice | READ: no Tamil piper voice on this desktop; Hindi, Telugu and Malayalam voices exist and synthesise (section 0) | NOT CHECKED for Tamil |
| Spoken English Agent sidebar label (id 20) | Binds to a Spoken English recipe | MEASURED 2026-10-14 (USER_JOURNEYS_LIVE_2026-10-13.md section 11): a salvage stub, `Creation Mode`, no recipe | FAIL (unchanged) |

## 4. Hazards recorded during these runs

- Build 3 (17:11, prompt 1793360501025): asked for exactly three actions and no history
  lookup, the local model authored twelve, six of them desktop-command steps (open a code
  editor, type code, click run) for a child's speech exercise. Three proceed turns ran
  before I killed the driver at 17:14:46; action 3 ("open a code editor window") was
  dispatched three times and the model narrated the work instead of calling the desktop
  tool (`[NEEDS-INPUT]` at 17:15:44); actions 1 and 2 banked; about 48 other tool calls
  ran; the desktop tool itself was never called. The record stays on the node with no
  recipe; deleting it is the owner's call. From build 4 on, the driver refuses to send a
  proceed turn when the authored record names a desktop or code tool.
- The create loop rewrites saved facts: child age 4 (typed) became "5" (build 1, 16:49);
  the parent's "Priya" and "Ravi" became "Amma" and "Appa" (build 6); timestamps dated
  2024 were invented in two memory files (builds 5 and 6). Fabrication of stored values,
  not only of completions.
- The microphone streams to STT from the agent page in Chrome and from the Nunba desktop
  window (section 0).

## 5. What this means for the 5 Nov live demo

Created through the product and spoken aloud: yes (builds 2, 5 and 6; more as the rows
turn PASS). One full exercise in reuse (person speaks, node transcribes, agent judges and
speaks back): not on this build. The reuse fix (HARTOS 7e6faa900) is on origin and on
central since 13:28Z on the owner's push word; a Nunba installer was built against it by
workflow dispatch (run 41061171903, origin main d7829808, completed 13:45Z); installing it
on the desktop is the owner's step, after which the reuse rows get re-run.
