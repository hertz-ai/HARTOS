---
name: ig_reel
description: Write the hook, the on-screen card and an optional voice-over for her makeup reel, checked with reel_score_hooks and reel_beat_sheet.
license: MIT, adapted from instagram-agent-skill skills/ig-reel @d03c56b by Jake Schincariol
disable-model-invocation: true
---

# ig_reel

reel_make cuts the reel from her clips. You write the words around it. How it cuts, and what you choose for it: reel_skill reel_edit.

## The story
Her clips play in the order she filmed them, and the reel ends on the finished look, held at least 1.5 s. Open with a short final-look teaser only when she asks for it ("start with the result").

## The hook
1. Write three hook lines from different angles: the time ("Bare face to bride in 20 seconds"), the problem solved ("Dusky skin, no grey cast in flash"), the reveal ("Wait for the temple jewellery").
2. Run reel_score_hooks on them. It flags weak hooks; it does not predict views. Fix the weakest check it names, or drop that line.
3. The on-screen card is six words or fewer (card_ok), in her caption language. Give it to reel_make as hook_text.

## Voice-over (only if she wants one)
Run reel_beat_sheet with her target length and fix every note: a hook past 3 s, a beat over 4 s, three beats with nothing concrete, no loop back to the hook.

## Rules
- Her voice profile comes first in this text. Follow it: language, code-mix, the words she uses and never uses.
- Numbers over adjectives: minutes, hours of wear, the occasion (muhurtham, reception). Never invent a number, a client or a review; if one is missing, ask her.
- No greeting, no "in this video".
- Show a bride's face only with that client's consent. If unsure, ask.
- You never post. She posts the reel on Instagram herself.
