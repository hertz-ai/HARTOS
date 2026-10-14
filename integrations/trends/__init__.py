"""integrations.trends: the songs makeup artists' reels are doing well with,
for Reel Studio's song suggestion (plan WS-6).

Central makes the list once a day; desktops read it when her agent asks.

  central_job.py            the daily run, driven from the agent daemon's
                            tick on a central node only (owner ruling U6):
                            starts ig_worker.py as a child process, polls it
                            on later ticks, ranks and publishes what it
                            brought back.  No thread, no LLM.
  ig_worker.py              the child.  Runs in central's separate trends
                            interpreter (HEVOLVE_TRENDS_PYTHON, with -I),
                            where instagrapi is installed; instagrapi never
                            enters the HARTOS venv (its pydantic and requests
                            pins conflict).  It imports nothing from HARTOS
                            except sources.py, loaded by path.
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
  api_trends.py             GET /api/trends/songs/latest, registered on a
                            central node only.
  trending_songs_client.py  a desktop's read of that list for her agent's
                            reel_trending_songs tool: cached six hours, each
                            song's language from its title's script, or for
                            a Latin title from this node's own LLM.

Files (under core.platform_paths.get_data_dir()):
  central: trends/watchlist.json (the accounts watched, kept by hand),
           trends/state.json (the job's schedule and outcome),
           trends/.ig_session.json (the research account's session, 0600),
           trends/songs/latest.json and trends/songs/YYYY-MM-DD.json.
  desktop: trends/client/latest.json and trends/client/song_lang.json.

The published list carries no URLs and no session data
(integrations.reel_studio.contracts.validate_trending_songs refuses both).
"""
