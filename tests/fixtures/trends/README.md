Fixtures for the trending-songs tests (tests/unit/test_trending_songs_*.py,
test_ig_worker_contract.py, test_trends_daily_job.py; package
integrations/trends).

- reels.json: one reel of each kind the trends worker meets, shaped like
  Instagram's private clips/user/ items[].media as instagrapi 3.0.24 receives
  it (the field names were read from instagrapi's types.py, extractors.py and
  mixins, not captured live): a licensed song with its music_asset_info, an
  original sound, a reel whose song is named only by
  clips_music_attribution_info, one named only by music_canonical_id (the
  worker looks its track up), a muted song, a reel with no music, and the
  track that lookup returns.

Every id, handle, title and count is invented; URLs point at example.invalid
so the tests can prove none of them reaches a published list.
