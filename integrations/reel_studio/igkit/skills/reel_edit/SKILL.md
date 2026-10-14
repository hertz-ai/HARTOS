---
name: reel_edit
description: How reel_make cuts her clips into a reel, and what you choose: length, opening, on-screen line, song.
license: Apache-2.0, written for HARTOS
disable-model-invocation: true
---

# reel_edit

reel_make plans every cut. You make a few choices and tell her plainly what it cannot do.

## What reel_make does
- Clips play in the order she filmed them, so the look builds stage by stage: bare face, base, eyes, lips, hair, jewellery. Weak or repeated shots are dropped; none moves out of time.
- It ends on the best shot of the finished look, held about 2-4 s and never under 1.5 s. Footage after that shot is left out.
- Every cut lands on a beat: her song's own beats when she attaches its audio, else a steady 120 BPM. The file is silent; she adds the song in Instagram at the start time given.
- Hard cuts only: no fades or wipes, no caption burned in.
- The face sits high in the frame. Your line and her logo stay clear of Instagram's buttons.

## What you choose
- target_seconds: 5-30 s, 20 s by default. On not_enough_footage, go shorter or ask for more clips.
- The first second decides if anyone stays. The reel opens on her earliest shot, usually the bare face, under hook_text for 2 s: at most 6 words that promise the change (the occasion, the time it took, the jewellery to come). Draft it with ig_reel.
- open_with=final_look only when she asks to start with the result (a teaser of 1 s at most).
- song_key: from reel_trending_songs.
- Pace follows her instructions: about 1.5 s a shot, 2.5 s if she asked for calm.

## Bridal footage
- One clip per stage, hair, maang tikka and nath included. Phone upright, window daylight: bulb light turns skin yellow, and a dark first frame gets the reel withheld.
- Hold the finished look at least 4 s, the bride turning slowly, so the reveal never needs slowing.
- Name the occasion in her words: muhurtham, haldi, mehendi, sangeet, reception.
- Show a bride's face only with the bride's consent.
