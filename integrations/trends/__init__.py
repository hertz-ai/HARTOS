"""integrations.trends: the songs makeup artists' reels are doing well with,
for Reel Studio's song suggestion (plan WS-6).

Made on HER desktop, once a day while she is away, and read there by her
agent (owner ruling A10d: "all in local nunba build not in central").  No
central node makes it, serves it or fetches it.

  daily_job.py              the daily run, driven from the agent daemon's
                            tick on her desktop, never on a central node or
                            one whose tier cannot be read (owner ruling
                            U6): starts ig_worker.py as a child process,
                            polls it on later ticks, ranks and publishes
                            what it brought back.  No thread, no LLM.  It
                            needs a watchlist (the accounts to watch,
                            watchlist.py) and then her
                            research account (a DEDICATED Instagram account,
                            never her business account), read from this
                            desktop's vault, where the credential consent
                            card puts it (ask_for_account files the card
                            when her agent asks for songs, a watchlist
                            exists and the account is missing; never
                            without a watchlist); the worker's own venv is
                            built by Nunba's installer on the owner's yes,
                            when the disk has room for it.
  ig_worker.py              the child.  Runs in the worker's own venv under
                            the data dir (core.venv_paths, WORKER_VENV),
                            where instagrapi is installed; instagrapi never
                            enters the HARTOS interpreter (its pydantic and
                            requests pins conflict).  It imports nothing
                            from HARTOS except sources.py, loaded by path,
                            and reads the account from its stdin.
  sources.py                standard library only, shared by the child and
                            HARTOS: the TrendSource protocol, the one reader
                            of a reel's music (music_of), what a reel or a
                            track is cut down to before it leaves the child
                            (slim_reel, slim_track), and the run loop with
                            its stop rules and request budget (collect).
  trending_songs.py         a reel read into a record (normalize_reel), a
                            day's batch ranked through igkit.swipe, the one
                            implementation of the outlier maths (rank_batch),
                            the day merged with yesterday's list (merge_days),
                            and the one writer of the published list
                            (publish, mark_stale, read_latest).
  trending_songs_client.py  her agent's read of that list for the
                            reel_trending_songs tool: each song's language
                            from its title's script, or for a Latin title
                            from this node's own LLM; with no list, why
                            (daily_job.why_no_list) in words she can be
                            told, after asking the owner for the research
                            account when that is why; a list older than
                            two days is served as old, saying how old.
  watchlist.py              the accounts watched (owner ruling A17b): the
                            one writer and reader of watchlist.json, with
                            Instagram handles only, each once, at most
                            sources.MAX_ACCOUNTS, and a niche tag.  Her
                            agent changes it with the reel_watchlist tool;
                            no credential ever passes through it.

Files (under core.platform_paths.get_data_dir()/trends/): watchlist.json
(the accounts watched, written only by watchlist.py; a default install has
none, so it makes no list and asks for no account), state.json (the
job's schedule and outcome), .ig_session.json (the research account's
session, 0600), worker.log, run/, songs/latest.json and
songs/YYYY-MM-DD.json, and song_lang.json (the language cache).

The published list carries no URLs and no session data
(integrations.reel_studio.contracts.validate_trending_songs refuses both).
"""
