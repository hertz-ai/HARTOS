---
name: ig_caption
description: Write the Instagram caption for her reel (first line, one ask, search words, up to five hashtags) and check it with reel_check_caption.
license: MIT, adapted from instagram-agent-skill skills/ig-caption @d03c56b by Jake Schincariol
disable-model-invocation: true
---

# ig_caption

The reel carries the hook. The caption gives the context, one ask and the words people search for.

## Shape
- Line 1, the part shown before "... more" (about 125 characters): the look and the occasion, plainly. Never a greeting, a hashtag or an emoji first.
- Body: two to four short lines: the look, the products, her city.
- One ask: "DM for bookings", "Bookings open for December", or her WhatsApp line. One, not three.
- Hashtags: up to five, picked from her hashtag bank, on the last line. Never #viral, #fyp or #explorepage.

## Search words
Put her city and service in a normal sentence, e.g. "bridal makeup artist in Madurai". Pass her search terms to reel_check_caption as keywords.

## Check
1. Run reel_check_caption on the draft with her keywords.
2. Fix every FAIL. For each WARN, decide and say why.
3. It also returns a cleaned version and a detection score. Use the cleaned text unless it changed her own words; her emoji and words from the profile are kept.

## Rules
- Write in her caption language and keep her code-mix (Tamil with English words is fine).
- No links in the caption: they are not clickable.
- Never invent prices, reviews or client names.
- Nothing is posted. She copies the caption herself.
