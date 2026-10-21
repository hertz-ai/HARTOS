Fixtures for tests/unit/test_igkit_*.py (Reel Studio's port of
instagram-agent-skill, integrations/reel_studio/igkit).

- hindi_paragraph.txt: a human-written Hindi caption with varied sentence
  lengths, numbers and first/second-person pronouns, ending sentences with
  the danda. The upstream detector scored any Hindi paragraph REVIEW because
  it counted no Hindi words and no danda sentences.
- machine_draft.txt: an English draft built from stock AI phrasing and evenly
  sized sentences, which the detector must still flag.

The names and prices in them are invented for the tests.
