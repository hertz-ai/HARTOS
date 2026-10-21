---
name: ig_human
description: Make a caption or script sound like her and not like a machine; reel_check_caption strips stock AI words and odd characters and scores the result.
license: MIT, adapted from instagram-agent-skill skills/ig-human @d03c56b by Jake Schincariol
disable-model-invocation: true
---

# ig_human

Captions are short and scripts are said out loud, so one written-sounding line stands out.

## What reel_check_caption fixes by itself
- Invisible characters, em dashes, curly quotes. Joiners inside Tamil or Hindi letters and inside emoji are kept.
- Stock words: "elevate", "game-changer", "in today's fast-paced world", "follow for more". Words in her profile are kept.

## What you fix by hand
It reports sentence shapes it will not rewrite:
- "It's not just X, it's Y" and "not only X but also Y"
- lists of exactly three
- "The result?" lines
- emoji bullet lists
- three or more words in capitals
- "tag someone who", "double tap if"
Rewrite each one, keep the meaning, then check again. Two rounds is normal; five means write a new draft.

## The score
Five checks, 0-100, higher is more human: BURSTINESS (sentence lengths vary), SPECIFICITY (numbers, names, places, occasions), SLOP DENSITY (stock words), FINGERPRINT (typography), VOICE (I and you, no staged reveals). PASS needs 70 or more with no check under 55. Fix the weakest check first.

## Say it honestly
These are local checks on her computer. They are not GPTZero or any other detector and cannot promise their verdict. Never tell her a text is undetectable. Show her the cleaned text, not only the score.
