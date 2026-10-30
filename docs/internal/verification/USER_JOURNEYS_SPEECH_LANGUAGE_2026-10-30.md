# Speech therapy, singing and language learning agents: user journeys, driven live on the owner's desktop node (2026-10-30)

Owner's asks (30 Oct): "Deliverable is the agents created and fully working locally" and
"Build and test all". The flows come from the owner's own specification: the Teams note
"Speech therapy Agent Flows" (HertzAI group chat, 29-10-2024, seven flows), the Singing
Agent spec (Teams, 25-06-2025), and the language learning / Spoken English chats (2023 to
2024). Copies of those sources are held on the owner's board, not in this repository.
This record follows the house rule of `USER_JOURNEYS_LIVE_2026-10-13.md`: one row per
step, setup, expected result, evidence label (measured, read, not checked), verdict. A
verified negative is worth as much as a pass. The repository is public: no internal hosts,
ports, private addresses or operator paths; the desktop node is "the desktop". The
10-13 record's section 11 holds the earlier probes of the sidebar labels "Speech Therapy
Agent (49)" and "Spoken English Agent (20)"; those labels are not the agents built here.

Reviewed four times by hartos-f9 (formerly hartos-70) on 30 Oct. Version one overstated
three things (nine trace-banked actions, no Indic voice, build 3 stopped before any
turn); version two turned two PENDING rows into PASS rows on replies that were never
sent; version three misstated how the create loop's input gate works and missed that the
loop also REFUSES a reply it did take; version four had the rejection counts low, two
action numbers off, two tool calls on the wrong agent, the Singing first pass at the wrong
time and the owner's timestamp rounded. All of that is corrected here; where a timestamp
or a count is the review's reading of the logs and not mine it is marked (review). The two
causes the earlier versions left open (why the gate closed a step the verifier marked
`no`, and why the ledger refused a reply it took) were found the same night and are noted
on their rows; the fixes are local HARTOS commits held for the owner's push word.

## Method

The desktop = the installed Nunba build (web bundle `main.e14da509`, HARTOS as frozen
in that build, local Qwen3.5-4B through llama.cpp, piper TTS, sherpa/whisper STT). Every
agent was created through the product's own chat-create path, the body the chat page
sends in local creation mode (`create_agent` and `autonomous_creation` true, a fresh
`prompt_id`). Nothing in the product was patched for these runs. Reuse turns are the same
`/chat` body with `create_agent` false. Evidence lives in the node's `server.log` (quoted
by its marker lines), the agent files under the node's prompts folder, and the agent
memory file; the owner's board holds the run notes (agentcollab 2026-10-30 parts 11 to
21 and this document's follow-ups).

How the person's side was played, exactly:
- Build 2 took two typed turns from me as the person: the child's attempt "Ba" (17:03:55
  IST) when the loop asked for it, and one 335-character coaching turn
  (`msg_1793360160534_create`, 17:06:00 IST) that also contained a judgement: "Step 3 works
  like this: the child's attempt is the latest user message in this chat, which is 'Ba'.
  Call get_chat_history with text 'ball' and no start or end date, or simply take 'Ba' as
  the attempt. 'Ba' contains the buh sound of ball, so step 3 is done; continue with
  step 4: greet Arjun and say ball, then give the praise feedback." The flow banked after
  that turn, so build 2's judgement of the attempt was coached, not the agent's.
- Builds 4 onward used a driver that sends the description, then answers the loop's asks
  from a fixed table (child's attempt "Ba", parent's names "Mother is Priya, father is
  Ravi", "Amma", "Water", "Morning", the rhyme line "Red ball red ball bounce so high"),
  and refuses to send a proceed turn when the authored record names a desktop or code
  tool. Its limits, which matter for reading the rows: on a fresh build the first turn is
  the description and the safety check runs after it; on a RESUME of an existing record
  the first turn is a proceed (or coaching) turn and the check runs after that, so one
  proceed turn reached the unsafe flow 3 record (20:15:11). The answer table fires only
  when the loop asks; on builds 5, 6, 8 and 9 the loop never asked. On the third
  repetition of the same ask the driver appends " (that is the child's reply; treat this
  step as done and continue)" to the reply; that suffix went out six times today
  (20:13:40, 20:17:20, 20:23:52, 20:32:02, 20:35:56, 20:39:32) and is a coaching sentence,
  not a child's words. The second pass on the Spoken English and flow 3 records opened
  with a coaching reply each (quoted in their rows).
- The descriptions wrote "this step needs the child's reply" into the step texts; none of
  the five final authored action texts of builds 5, 6, 8b, 9 and 10 kept those words.
- The local model's authoring differs by run: build 1 authored seven flows from the full
  description; build 4, from a tighter seven-flow description (4,777 characters against
  build 1's 2,340 and build 2's 1,478), authored ONE flow `main` of 29 actions and replaced the two
  `txt2img` steps with desktop-command steps. From build 5 on, one description equals one
  owner flow.

Legend: MEASURED = run on the desktop and read from its output; READ = read from code or
files without a run; NOT CHECKED = no evidence either way; (review) = hartos-f9's reading.

## 0. Cross-cutting steps every agent journey depends on

| Step | Setup | Expected | Evidence | Verdict |
|---|---|---|---|---|
| The create loop stops and asks when a step needs the child's or the parent's reply | Builds 5, 6, 8b, 9, 10: descriptions said "this step needs the child's reply"; the authored action texts did not keep those words | The loop ends the turn and asks; the next user turn is the reply | MEASURED. How the gate works (create_recipe.py, the verifier verdict handler): a `completed` verdict returns at once; only a `pending` verdict is read for `can_perform_without_user_input`, and only `pending` + `no` sets the user-input flag. The verifier judged send steps both ways (review): completed + no, no stop: build 5 action 3 18:44:09 and action 4 18:44:40, build 10 action 1 20:23:18; pending + no, gate fired: build 3 action 2 17:13:19, build 4 18:37:59, the flow 1 duplicate's action 3 20:12:40, flow 3 text-only action 2 20:16:41. Builds 5 (18:40:17 to 18:47:00), 6 (18:47:11 to 18:57:52), 8b (20:20:07, 59 s) and 9 (20:22:24, 127 s) each completed in ONE turn with no ask; my driver's log has one row each. Cause and fix, same night: the handler returned on `completed` before reading the flag (HARTOS 51c6f6c2d gated every completed + no; the review rejected that, since on a completed verdict `no` describes the action and the gate would ask without bound); reworked in 94354c536 to ask once and close on the person's recorded answer, held for the owner's push | FAIL on this build: a step the verifier itself marks as needing the person's input is closed when the verdict says completed |
| A reply the loop asked for and took completes the step | Flow 1 duplicate 1793371296211, flow 3 text-only 1793371539241, build 10, Spoken English 1793372451267 | The step completes and the flow moves on | MEASURED (review timestamps): the loop asked, the driver answered "Hi!" at 20:13:03, 20:13:23 and 20:13:40 (20:13:05 and 20:13:11 are the agent's own sends; `name_response` "Hi!" saved 20:13:09), the verifier returned completed (20:13:07, 20:13:14), and the ledger refused the completion: `[CLAIM REJECTED] ... integrity check failed, data_hash mismatch` four times 20:13:07 to 20:13:48 on the flow 1 duplicate, four times 20:16:46 to 20:17:25 on flow 3 text-only, six times on the Spoken English Coach 20:34:31 to 20:36:23, and after build 10's 20:24:00 verdict seven rejections then a refused completion at 20:28:01. 29 such rejections between 20:13 and 20:36 and 31 in all (review's count; my own count of 21 was low). Cause, found the same night (#159): the resume wrote the person's answer into the task's hashed context and saved without re-sealing, so the claim's integrity check read the answer itself as corruption; fixed locally in HARTOS 49afbeba4, and the reply now also moves the action out of PENDING (94354c536, after the review found the old verdict spinning to the stall guard); both held for the owner's push | FAIL on this build: the verifier passes, the ledger's integrity check refuses, the loop asks again with the same words |
| A person creates an agent from the desktop chat page while signed in and online | Chat page, "Create New Agent" form | The agent record appears on the node | MEASURED 16:0x IST: the form POSTs to the cloud create endpoint and stayed pending 4+ minutes (an empty POST answers 422 in 36 ms, so the gateway is up and the handler hangs) | FAIL on this build |
| A person creates an agent from the chat page in local mode (guest or offline) | Same page, `useLocal` path | The node's create loop authors the record and banks flows | MEASURED 17:00 IST with the same request body sent to the node: record written within the first turn (76 s), flow banked, "Agent Created Successfully" at 17:09 (Syllable Coach) | PASS |
| "Talk to agent" on an agent card binds the chat to that agent | Agents page, card button | The next turn carries the agent's `prompt_id` | MEASURED 16:1x: navigates to `/agents/<slug>`, the turn went out as the generic assistant (`agent_id=local_assistant`) | FAIL |
| The create loop can finish an action whose named tool the local model never calls | Build 1, action 1 "get_user_details to ..." | Action banks | MEASURED 16:25 to 16:48: three completed rounds of three attempts and a fourth started, 0 get_user_details calls, "[NEEDS-INPUT] action 1 not completing after 3 attempts" each round | FAIL (model + verdict gate; harness #103/#107) |
| The create loop banks an action from the tool calls that actually ran | One-flow agent, tools the model calls | `[TRACE-BANKED] action N recipe derived from K executed step(s)` | MEASURED 17:01 to 17:08: actions 1, 2, 4 to 9 of the Syllable Coach, eight lines, each from 1 to 3 steps | PASS |
| A trace-banked action keeps the verifier's input flag | Any trace-banked action | `can_perform_without_user_input` as the verifier judged it | READ (review): the trace bank stamps every banked action `can_perform_without_user_input: 'yes'` (create_recipe.py, the trace-bank json), including steps the verifier marked `no`; reuse reads that field through `_reuse_action_is_autonomous` to decide whether the walk may advance on its own (`_reuse_action_declares_user_pause` only reports it). A stamp read from the ledger's answer record was written (94354c536) and held out again (a3820a8fe): the review's condition was a live walk showing a `no` step advancing on the person's reply, and the walk measured at 22:24 to 22:28 IST (section 5, #161) did the opposite | FAIL by reading; the stamp is held behind #161 |
| A model-written recipe cannot bank an action whose named tool never ran | Action 3 of the Syllable Coach (get_chat_history) | Refused like the trace bank refuses | MEASURED: the trace bank refused it at 17:06:47 ("names get_chat_history and none of them ran"); the model-written path then saved it `done` with `recipe: []` | FAIL (harness #107 datum) |
| Reuse runs the whole banked flow | `create_agent` false, `prompt_id` set | All N actions in the ledger | MEASURED 17:09:53: `[REUSE-LEDGER] tasks=1 actions=1` of 9; the one action ended at 17:10:12 with `[REUSE] Cannot advance action 1: state is status_verification_requested, not TERMINATED`. Root cause the persona vocabulary filter, fixed in HARTOS 7e6faa900 (on origin since bf061ec7a, central redeployed 13:28Z; reviewed APPROVED-WITH-NITS by hartos-f9, its M1 and a behavioural test of create_agents_for_user open in the review queue; 131 tests over the eight reuse and trace-bank files is my run, not reproduced by review); replayed on the real files: fixed rule keeps 9 of 9. The desktop runs the old rule until a new installer is installed | FAIL on this build, fix pending an install |
| Reuse refuses a fabricated completion | Same turn | `[FABRICATED-COMPLETE]` refuses to advance | MEASURED 17:09:58: refused, "its tool(s) ['get_chat_history'] produced no real result" | PASS |
| The agent's messages are synthesised for speech on the desktop | Any `send_message_to_user` | piper synthesises the text | MEASURED: `NunbaPiperTTS Synthesized audio` lines follow agent messages (review's count: 42 clips against 61 sends, 16:40 to 17:10); whether the clips PLAYED on the speakers was not measured | PASS for synthesis; playback NOT CHECKED |
| Multi-language speech synthesis on the desktop | piper voices folder and `/tts/voices` | Indic voices present and listed | MEASURED (review): the piper voices folder holds hi_IN-pratham, ml_IN-arjun, te_IN-maya and ur_PK-fasih (plus en_US-amy, zh_CN-huayan); direct piper synthesis gave Hindi 1.99 s, Telugu 1.25 s, Malayalam 1.39 s, all non-silent. A Hindi AGENT turn is unmeasured. `/tts/voices` answers `installed: []` because `_LazyPiper` has no voice-listing method and `list_installed_voices()` returns an empty list: a listing defect, not a missing voice | PASS for hi/te/ml synthesis; FAIL for the `/tts/voices` listing (defect) |
| Tamil speech synthesis on the desktop | TTS engines on disk and in the model catalog | A Tamil voice that works | READ (review): facebook/mms-tts-tam (145 MB) is in the hub cache and `mms_tts` routes `ta`; the indic_parler venv (1.3 GB: parler_tts 0.2.2, torch, transformers 4.46.1) is present and its worker died at startup on 10-20; `model_catalog.json` lists `tts-indic-parler` and `tts-mms-tts` with an empty `local_path` and `downloaded: None`. No piper Tamil voice. No Tamil was synthesised and no engine has been shown to work for it | NOT CHECKED |
| The microphone stays closed while the person has not asked to speak | `/agents/<slug>` page in Chrome ("Quiet Mode Active"), and the Nunba desktop window | No audio leaves the UI | MEASURED (review's count from the rotated and current logs): the node's STT received a 6 s window every 3 s from 16:13:20 to 17:09:50 IST, 1124 windows (560 + 564), all silence (VAD removed everything); the Chrome tab held the connection and the stream stopped when the tab left the page at 17:09; it resumed at 19:53:06 from the Nunba desktop window itself (the app process and its webview hold the connection, read at 20:09). Quiet Mode in the page governs TTS, not the microphone | FAIL (owner ruling open, task #123) |

## 1. Speech Therapy Agent, the owner's seven flows

Builds on 30 Oct, all on the installed desktop. Build 1 (16:24, prompt 1793357576203,
seven flows authored from the full description, 16 actions in the syllable flow) never
left action 1 (three completed [NEEDS-INPUT] rounds, a fourth started; agent memory age
typed 4, written "5" at 16:46:53, 4 again at 16:49:49). Build 2 (17:00, "Syllable
Coach", prompt 1793359802508, one flow) banked eight of its nine actions from the trace
(1, 2, 4 to 9); action 3 was refused by the trace bank at 17:06:47 and banked `done` with
`recipe: []` through the model-written path (section 0). Build 3 (17:11, prompt
1793360501025): asked for three actions, the model authored twelve, six of them
desktop-command steps; three proceed turns ran (17:13:21, 17:13:30, 17:13:39), action 3 was
started three times and ended in `[NEEDS-INPUT]` at 17:15:44, actions 1 and 2 banked,
about 48 other tool calls ran, the desktop tool itself was never called; the driver was
killed at 17:14:46 (section 4). Build 4 (18:28, prompt 1793365100141, the seven flows in
one description, driver on): the model authored ONE flow `main` of 29 actions and replaced
the two `txt2img` card steps with `execute_windows_or_android_command: generate and
display ...`; the gate fired at 18:37:59 on a pending verdict, the driver sent no proceed
turn, actions 1 and 2 banked inside the first turn, no recipe. From build 5 on, one agent
per owner flow; flow 5's agent is build 2.

| Flow | Step | Expected | Evidence | Verdict |
|---|---|---|---|---|
| 1 Name calling | Greet by name, register any response, record | Child greeted by name once; response saved | MEASURED build 5, prompt 1793365817449 ("Speech Therapy Flow 1 Name Calling"), ONE turn 18:40:17 to 18:47:00 IST (403 s): 6 actions, recipe banked with steps on every action (tools send_message_to_user, save_data_in_memory, get_data_by_key); it said "Hello Arjun! It's so nice to see you!" and then "Bravo Arjun! You did it!" with no reply in between: the verifier judged actions 3 and 4 completed while noting the "assumption of response" (18:44:09 and 18:44:40, review); memory `name_response` saved `responded` at 18:44:52 and `current_value: no_response` at 18:45:25, with an invented 2024 timestamp. No child reply was ever sent to this agent; no reply text exists in its memory. Duplicate record 1793371296211 (runner bug, 20:11): the gate DID fire on a pending verdict at action 3 (20:12:40); the driver answered "Hi!" at 20:13:03, 20:13:23 and 20:13:40 (the agent's own sends were 20:13:05 and 20:13:11), `name_response` "Hi!" was saved 20:13:09, the verifier returned completed (20:13:07, 20:13:14) and the ledger refused the completion four times (data_hash mismatch, 20:13:07 to 20:13:48); this duplicate also called `get_user_camera_inp` at 20:12:48 (review); the driver stopped at 20:13:57 | Record authored, recipe banked, reuse not run; the recipe holds the invented exchange. "Response saved" FAIL (no response was taken); the duplicate's ask-and-reply FAIL (reply taken, completion refused) |
| 2 Family association | Ask the parent for names, show the child, evaluate, record | Names saved; child's answer judged; result saved | MEASURED build 6, prompt 1793366231447 ("Speech Therapy Flow 2 Family Members"), ONE turn 18:47:11 to 18:57:52 (641 s): 5 actions, recipe banked with a step on every action; it asked the parent for the names, asked "Arjun, who is this, Amma or Appa?", sent the feedback word "Amma" itself (action 4's banked step is literally the message "Amma") and saved `family_members: {mother: Amma, father: Appa}` and `family_association_result: {result: correct, child_response: Amma}` with an invented 2024 timestamp. No parent or child reply was sent; the typed names "Priya" and "Ravi" appear nowhere in the logs (review). It called `get_user_camera_inp` once, at 18:50:20 (no frame came back), a tool the description forbade; the 20:12:48 call was the flow 1 duplicate's and the 20:38:17 call the flow 3 text-only agent's (review) | Record authored, recipe banked, reuse not run; the recipe holds the invented values. The exercise FAIL (the reply and the judgement were the model's; forbidden tool called) |
| 3 Flash cards | Card image, word spoken, attempt judged, record | `txt2img` card, feedback, result saved | MEASURED build 7, prompt 1793366883072 ("Flash Cards - Atomic"): authored 6 actions, three of them `execute_windows_or_android_command` (open image generation tool, generate cartoon of red ball, display image as flash card); my session restart killed the driver at 19:0x before any action banked; the resume at 20:15:11 sent one proceed turn before the safety check and stopped UNSAFE at 20:15:29, no tool ran. Text-only variant 1793371539241 (no image step): it still called `txt2img` at 20:16:02 and 20:16:33 (review); the gate fired on a pending verdict at action 2 (20:16:41); the driver's "Ba" replies were taken, the verifier passed and the ledger refused the completion four times (20:16:46 to 20:17:25); the driver stopped at 20:17:31. Second pass on that record (five turns; the first ended 20:37:11, the last 20:41:16) opened with a coaching reply (paraphrased here: send the card text, then wait; the child's reply is the latest user message, "Ba") and three more "Ba" replies; one turn answered with a `status: error` JSON for the feedback step; it called `get_user_camera_inp` at 20:38:17 (review); nothing banked | FAIL (image step authored as a desktop command; the text-only variant's "show the card" step is passed by the verifier and refused by the ledger) |
| 4 Reverse flash cards | Two pictures, a sound, the child picks, record | Card image, feedback, result saved | MEASURED build 8, prompt 1793371661246: authored 2 flows, 5 actions, one `execute_windows_or_android_command: display the generated image on screen`, stopped UNSAFE after the first turn, no proceed turn; an earlier attempt at 19:03 died in authoring with no record. Text-only variant 1793371749059 ("Speech Therapy Flow 4 Reverse Flash Cards"): created in ONE turn (20:20:07, 59 s), 4 actions, recipe banked; the `txt2img` call at 20:18:22 with its empty result at 20:18:53 belongs to the image variant 1793371661246, not to this record (review); it said "Here are two pictures for you: a cat and a ball", "Arjun, can you say the word ball?" and "That's wonderful! I hear you saying ball" with no reply in between; memory `reverse_flashcard_results: true`; the banked recipe holds the message "I hear you saying ball" and the value `true` | Record authored, recipe banked, reuse not run; the recipe holds the invented values. The exercise FAIL (no child input; no reply text exists) |
| 5 Syllables | Level from memory, word spoken, attempt judged, progress saved | Creation-time exercise | MEASURED on build 2: the loop asked for the attempt and took "Ba" (17:03:55); the feedback "You said Ba, which is so close to ball! Now let's say ball one more time" was synthesised by piper (playback not measured); memory holds `target_word: ball`, `child_name: Arjun`, `last_attempt: Ba`, `exercise_result: practised`. Not exercised: the level from memory (the authored flow never read or saved `syllable_progress`) and progress across sessions. The judgement that "Ba" was close to "ball" came from my coaching turn ("'Ba' contains the buh sound of ball, so step 3 is done ... then give the praise feedback", Method), not from the agent | PASS for word spoken and attempt taken (judgement coached, level and progress not exercised); reuse FAIL (section 0) |
| 6 Question answering (echolalia) | Question with its answer, child repeats only the answer, judged | Echo detected or answer celebrated | MEASURED build 9, prompt 1793371817938 ("Speech Therapy Flow 6 Question Answering"), ONE turn (20:22:24, 127 s): 5 actions, recipe banked; it asked "What do we drink when we are thirsty? Water. Can you say water for me?" and in the same turn said "Wow, Arjun! You said water so nicely!"; the banked recipe saves `question_answering_result: answered` unconditionally. No child reply was sent; no reply text exists; the echolalia branch was never exercised | Record authored, recipe banked, reuse not run; the recipe holds the invented values. The exercise FAIL (no child input) |
| 7 Concepts | Time of day asked, confirmed, why we eat | Result and the registration marker saved | MEASURED build 10, prompt 1793371954882 ("Speech Therapy Flow 7 Concepts"), six turns from the description at 20:22:34 to completion at 20:29:21, the rest my driver's "Yes, proceed with the next step", the one at 20:23:52 carrying the coaching suffix (Method): the verifier judged action 1 completed + no at 20:23:18 (review), the next completed verdict came at 20:24:00 after the suffixed turn, then seven integrity rejections (review) and a refused completion at 20:28:01, and the flow still banked: 4 actions in a flow the model named `main`; it asked "Arjun, is it morning or night now? Look outside." and answered itself "Oh, Arjun, it's morning!" (the banked step hard-codes the morning message; the run was at 20:2x IST); memory `concept_results: {time_of_day: correct, answer: morning}`, `registration_recall_retention: {first_seen: true}` | Record authored, recipe banked, reuse not run; the recipe holds the invented values. The exercise FAIL (no child input, wrong time of day saved as correct) |
| Any | The agent calls the Singing, Story or Flash-card agents (owner's composition rule) | Agent-to-agent call | READ: no agent-facing tool composes or calls another agent on this build (`agent_ui_compose` has no caller). The `Navigate_App` tool resolves a phrase against the page registry, which includes the Games and Kids Learning pages, and the reply carries clickable chips; it needs a click and does not switch the page by itself; whether an agent-bound reuse turn can call it is unchecked (fix-all, 21:00) | FAIL (gap 1) |
| Any | Talking Avatar, ComfyUI kid images, push notification, Edit Profile, Track Progress tools | Named in the owner's Flow 1 | READ: none of these tools exists on the speech_therapy goal type (tool tags memory, media, vision, consent); progress lives only in agent memory | FAIL (gaps 2, 3) |

## 2. Singing Agent (owner's spec 25-06-2025)

| Step | Expected | Evidence | Verdict |
|---|---|---|---|
| Song Creator writes a rhyme and saves it | Rhyme text in memory | MEASURED. First pass, prompt 1793372371978 (two-flow description, 20:29:31 to 20:30:40): the model authored `execute_windows_or_android_command: run the script to get the generated text` for the rhyme and the driver stopped UNSAFE after the first turn. Second pass, prompt 1793373034536 ("Singing Agent Rhyme and Sing Along", one flow): three turns 20:41:30 to 20:45:13, 8 actions authored, the rhyme written directly in the message ("The red ball, bouncing high ..."), `current_song` saved with the full four-line text | PASS at creation (second pass) |
| Kid Listener delivers it and invites the child to sing along | Greeting by name; feedback on the child's line; what was played remembered | MEASURED, same second pass: action 3 delivered the rhyme; at action 4 ("ask Arjun to sing the first line back") the verifier returned pending + no, the gate asked (20:42:00) and the resume "from a genuine user reply" (20:42:02) took the driver's "Red ball red ball bounce so high"; the ledger refused the first completion claim (data_hash mismatch) and accepted the next (review); the agent said "That's wonderful, Arjun! I heard you sing 'Red ball red ball bounce so high'", sang the next line and saved `played_songs: ["Red ball red ball bounce so high - Arjun sang"]`. Recipe banked with steps on all 8 actions. Caveats: the reply was the rhyme's own first line typed by the driver, so the match was trivial; one message leaked a template name ("Hey there! I'm skill.local.name"); synthesis by piper, playback not measured | PASS at creation (the second real ask-and-reply of the day); reuse untested on this build |
| Avatar video generation through the mailer endpoints of the spec | A singing video | READ: the endpoints are cloud services the local create loop has no tool for; not attempted | NOT CHECKED |

## 3. Spoken English Coach (language learning)

| Step | Expected | Evidence | Verdict |
|---|---|---|---|
| Cafe scenario greeting, learner reply, one tip, tip remembered | `last_tip` and `conversation_turns` in memory | MEASURED. First pass, prompt 1793372451267 (20:31): the loop asked for help with step 1 ("get_data_by_key key last_tip") four times and the driver stopped; nothing banked. Second pass on the same record (four turns; the first ended 20:34:38, the last 20:37:18) opened with the coaching reply "a missing key or an empty result simply means no tip has been given yet, so step 1 is done; continue with step 2"; the verifier's completed verdicts named actions 2 and 3 while the ledger rejected the claim for action_1 six times (data_hash mismatch, 20:34:31 to 20:36:23, review); the loop asked the same question three more times; nothing banked | FAIL (the verifier passes, the ledger's integrity check refuses) |
| Tamil-first learner gets the tip in English with Tamil TTS available | Tamil voice | READ: no Tamil was synthesised; engines and weights for Tamil are on disk but unproven (section 0) | NOT CHECKED |
| Spoken English Agent sidebar label (id 20) | Binds to a Spoken English recipe | MEASURED 2026-10-14 (USER_JOURNEYS_LIVE_2026-10-13.md section 11): a salvage stub, `Creation Mode`, no recipe | FAIL (unchanged) |

## 4. Hazards recorded during these runs

- Steps that needed the person were closed without the person (section 0, row 1): in
  builds 5, 8 and 9 the loop ran the "wait for the child's reply" steps without waiting
  and banked flows whose recipes hold invented messages and values ("Bravo Arjun! You did
  it!", "I hear you saying ball", `answered` saved unconditionally); in build 6 the model
  also wrote the reply itself (`child_response: Amma`) and judged it correct. A parent
  reading `family_association_result: correct` would believe an exercise happened. The
  camera tool `get_user_camera_inp`, forbidden by every description, was called once each
  by build 6 (18:50:20), the flow 1 duplicate (20:12:48) and the flow 3 text-only agent
  (20:38:17) (review).
- Replies that WERE taken were refused (section 0, row 2): the ledger's integrity check
  rejected the completion claim (data_hash mismatch) 29 times between 20:13 and 20:36 (31
  in all, review's count), on four different agents, after the verifier had passed the
  step; the loop then asked the same question again. Cause found: #159, section 0.
- Build 3 (17:11, prompt 1793360501025): asked for exactly three actions and no history
  lookup, the local model authored twelve, six of them desktop-command steps (open a code
  editor, type code, click run) for a child's speech exercise. Three proceed turns ran
  before I killed the driver at 17:14:46; action 3 ("open a code editor window") was
  dispatched three times and the model narrated the work instead of calling the desktop
  tool (`[NEEDS-INPUT]` at 17:15:44); actions 1 and 2 banked; about 48 other tool calls
  ran; the desktop tool itself was never called. Builds 4, 7, 8 and the Singing Agent's
  first pass authored desktop-command steps too; none ran. Deleting those records is the
  owner's call.
- The create loop rewrites saved facts: child age typed 4 became "5" at 16:46:53 and 4
  again at 16:49:49 (build 1); timestamps dated 2024 were invented in two memory files
  (builds 5 and 6).
- The microphone streams to STT from the agent page in Chrome and from the Nunba desktop
  window (section 0).
- Clutter left on the node by my own runs: duplicate records 1793371296211 and
  1793371447847 (a runner bug rebuilt flows 1 and 2 at 20:11 and 20:14), plus the unsafe
  records above; all without a recipe except as noted.

## 5. What this means for the 5 Nov live demo

Created through the product with a banked recipe: builds 2, 5, 6, 8 (text-only), 9 and
10 (six speech agents) and the Singing Agent's second pass, seven agents. Not created:
flow 3 (both variants), the Spoken English Coach (both passes). Spoken: the agents'
messages were synthesised by piper; playback on the speakers was not measured. Real
exercises where a person's reply was taken and answered and the flow then completed:
two, build 2 (judgement coached) and the Singing Agent (the model judged a trivially
matching line). Builds 5, 6, 8 and 9 took no reply at all; the flow 1 duplicate, flow 3
text-only, build 10 and the Spoken English Coach took replies that the ledger then
refused. One full exercise in reuse (person speaks, node transcribes, agent judges and
speaks back): not on this build. The reuse fix (HARTOS 7e6faa900) is on origin and on
central since 13:28Z on the owner's push word; the owner's "Merge and push" at 20:39:41
IST put the Nunba merge df611358 on origin, and its nightly release nightly-df61135 was
published at 21:11 IST carrying HARTOS 9180ba44d; installing that release on the desktop
is the owner's step, after which the reuse rows get re-run. Two things stand in the way
of a reuse exercise even then. Every trace-banked action carries
`can_perform_without_user_input: 'yes'` whatever the verifier said (section 0). And the
installed build's reuse walk itself does not take a reply at a `no` step: measured 22:24
to 22:28 IST on a model-written recipe ("Tutor-FirstSession-Setup", 45620673143, flow 0:
get the learner profile, ask the topic and detail, confirm, remember), the first turn
delivered the ask, the second turn's answer "basic algebra, beginner level" got back "I've
sent you a message asking about your preferred topic and detail level. I'm waiting for
your response": the walk restarted at the failed first action, closed it on a chat
message with its named tool never run, then recorded GAVE_UP for the ask step. The
installed reuse module is HARTOS 19836e605 (hot-swapped into the install on 27 Oct
01:03), and its three walk regions match the current main; filed as #161 with the root
cause (the gate requires only the tools an action's words name, and no evidence kind
exists for "the person answered after the ask").

Deferred from the reviews, not resolved here: a per-row account of the 42 clips against
61 sends.
