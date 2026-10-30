# A reuse walk through an ask step on the installed desktop build (2026-10-30, 22:24 to 22:28 IST)

Purpose: the gate hartos-f9 set before any stamp of `can_perform_without_user_input: no`
may land on trace-banked ask steps: a real walk had to show a `no` step advancing after the
person's reply. Measured on the installed Nunba (Nunba.exe of 2026-10-20 18:52, HARTOS
hot-swapped into the install on 27 Oct 01:03), local Qwen3.5-4B, through the node's own
`/chat` route with `create_agent` false (the body the chat page sends for a selected local
agent), on a fresh conversation. This file replaces a session-local note; the lane that
measured it is the hevolve lane (66e67cad). The repository is public: the desktop is "the
desktop".

## Same walk as origin, measured

- The module that wins on `sys.path` in the frozen build is `lib/hartos/reuse_recipe.pyc`
  (hash-based pyc, CPython 3.12 magic). Its source hash `61c90f012381f2f1` equals the hash
  of `hartos/reuse_recipe.py` at HARTOS 19836e605 (2026-10-25 14:00) and of the root copy
  in the install (both dated 10-27 01:03). The installed reuse module is 19836e605.
- 19836e605 against main at 94354c536 (reuse_recipe.py last changed by 7e6faa900): 112
  changed lines in 11 hunks, the persona filter and `_carry_reply_media` on the two
  `message2userfinal` returns. The three walk regions are byte-identical over 35 lines
  around each marker: the seed dispatch, `reuse-w1-completed`, `[REUSE-NODRIVER]`.

## The agent: 45620673143 "Tutor-FirstSession-Setup", flow 0

Model-written recipe (the flow recipe file and four action files present; declared
persona Executor = the flow persona, so the installed filter kept all four:
`[REUSE-LEDGER] tasks=4 actions=4`):

1. `yes` get_data_by_key with key='learner_profile'
2. `no`  send_message_to_user: ask the topic and the level of detail
3. `yes` send_message_to_user: confirm the topic and detail level
4. `no`  send_message_to_user: remember preferences and mastery level

Two readers see two tool sets for action 1. The Tier-1 attach reads the banked step's
`tool_name` ("action 1 names ['get_data_by_key'] -> 1 tools"). The fabrication gate and
the receipt finder read only the tools the action's WORDS name (`action_own_text`,
`action_named_tools`, the 2026-10-21 rule), and "Get learner profile data by key" spells
none, so to them action 1 is a prose step whose written answer is its receipt.

## Turn 1, 22:24:51 to 22:26:42 (111 s), "Hi, I want to set up my first tutoring session."

The person received (HTTP 200, agent_status "Reuse Mode"):

> Hello! I'm excited to help you set up your first tutoring session! To get started, I
> need a bit more information from you: What topic or concept would you like to explore?
> ... How much detail would you like to cover? (Beginner, intermediate, or advanced) ...

That is action 2's question, delivered while the pipeline was still on action 1:
`current_action_id` read 1 on all 36 reads; get_data_by_key never ran (tools in the turn:
send_message_to_user x5, get_user_uploaded_file x1, get_saved_metadata x1). The verifier
said action 1 pending ("Awaiting user input for learner key"), then action 2 pending seven
times ("Awaiting user response to their preferred topic and desired level of detail"). At
22:26:08 `[UNDER-REPORTED]` advanced action 1 with the fabrication gate checking nothing:
at 19836e605 `_reuse_fabricated_tools` returns `[]` for an action whose words name no tool,
and the session log has 0 `[FAB-GUARD] action 1` lines. `[REUSE-VERIFY]` then found no
canonical receipt in action 1's dispatch window and recorded GAVE_UP; the ledger task went
FAILED. The turn ended through `[SYNTHESIS]`, delivering the ask. Three `while1` passes, no
`[REUSE-NODRIVER]`.

Why the finder found nothing is not established. Measured: 0 "receipt search" lines in
either turn's log, so the named-tool branch did not run; the step read as a prose step and
`_reuse_written_answer` returned None. Inferred (strong): that function's `except
Exception: return None` is a silent exit nobody has ruled out. Not the steers: a steer
carries no leading dispatch marker, so it cannot move the window, and a send ran at
22:25:57 after the last steer at 22:25:45; the backward walk stops at the first message
the predicate accepts, and messages 22, 23 and 26 sit after the last steer. hartos-f9's
guess, inferred and not run: the walk stops at Helper's message 26 and `is_written_answer`
refuses it as not the Assistant's, which fits the json_repair failure pair at 22:26:08. No
log line records which exit returned None; the walk on a HEAD build may settle it.

## Turn 2, 22:28:01 to 22:28:43 (42 s), "I'd like to focus on basic algebra, at a beginner level."

The person was told at 22:28:13:

> I've successfully retrieved your learner profile data (Action #1 completed)

with that tool never run, and then received:

> I've sent you a message asking about your preferred topic and detail level. I'm waiting
> for your response before I can proceed with the guided discovery session. ...

The answer was not taken. The seed re-posted "Perform this action -> Action #1" (the walk
restarted at the FAILED action). The verifier said action 2 pending five times, then
action 1 completed three times ("Learner profile data retrieved successfully") with the
tools in the turn being get_user_id x1 and send_message_to_user x4: an invented result.
At 22:28:15 `reuse-w1-completed` took action 1 as "evidence-backed completion" (its written
answer, the prose rule) and terminated it; action 2 went in_progress at 22:28:16; at
22:28:24 `reuse-w1-completed` for action 2 met `[REUSE-VERIFY]` with no receipt and recorded
GAVE_UP; `[SYNTHESIS]` delivered the re-ask. Two `while1` passes, no `[REUSE-NODRIVER]`,
action 3 never reached.

## Verdict against the gate

FAIL on both halves: the `no` step did not advance after the person's reply (it was given
up and the person asked again), and the one advance that did happen (action 1) rode on a
written answer for a step whose banked tool never ran. The stamp stayed out (HARTOS
a3820a8fe).

## Defects (present on origin's reuse too; the installed module is 19836e605)

- R1. The reply turn did not carry the answer into the ask step: the seed re-posted the
  current action's instruction, the model asked again, the verifier reported the ask step
  pending although the answer was the latest user message. A fix landed locally in HARTOS
  b9edfd90a and its rework (the commit that carries this text), under review, not live
  proven: the seed says the question was already delivered, the reply is captured with
  its request id, and it stands as the completion evidence (`user_reply`) for the
  verifier's `completed`, only from a genuine request other than the one that asked, and
  only on a person's turn.
- R2. The receipt finder accepted a written answer as action 1's receipt while its banked
  tool get_data_by_key never ran, because the action's words name no tool (the 2026-10-21
  requires-versus-offered rule). Open; a measurement of an any-of banked-tool rule over the
  desktop's recipe actions comes before any change.
- R3. A `no` step with a completed verdict and no receipt was recorded GAVE_UP and the
  person re-asked. A fix landed locally in b9edfd90a and its rework, under review, not
  live proven: an ask-only step whose question is out waits through the canonical path
  (ledger BLOCKED with blocked_reason input_required, ActionState PENDING) on a person's
  turn, and a genuine next turn resumes it first; a background turn neither runs nor
  re-asks it. A reply after a restart or the 7200 s cache lapse still re-steers and gives
  up (hartos-f9's review, item 5), so "never GAVE_UP" does not hold yet.

## Control: a different agent's `yes` send step, with no question in it (2026-10-30, 23:28 IST)

Agent 7700000611 "AgentToAgentVerificationAndDelegation", flow 0, declared persona
Executor, two actions: 1. `yes` send_message_to_user "livetest_agent_to_agent_verify step
one"; 2. `yes` delegate_to_specialist (find the median of 4, 9 and 2). One turn, "hello,
please start", 23:28:01 to 23:28:50 (49 s), `[REUSE-LEDGER] tasks=2 actions=2`. Action 1
sent its line, the verifier said completed, `reuse-w1-completed` took the send receipt as
the evidence and terminated it at 23:28:21; action 2 claimed completion once without its
tool (`[FABRICATED-COMPLETE]` refused and re-steered at 23:28:32), then ran
delegate_to_specialist and completed at 23:28:50; the person received the delegation
summary. Tools in the turn: send_message_to_user x3, delegate_to_specialist x1,
save_to_long_term_memory x1, get_user_id x1. So a `yes` send step completes on its own
send and the flow runs to the end in its one turn (nothing was asked): it does not wedge,
and it does not wait for a reply either. Inferred, not run: that is the shape the seven
speech agents' recipes would replay today, every step of theirs being stamped `yes`.

Not yet measured, carried on the task: R1 to R3 on a HEAD build; the live gate for
b9edfd90a on a demo agent (a refusal and an off-topic reply, and the exact texts the
person gets on both turns).
