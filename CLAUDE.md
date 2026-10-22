# CLAUDE.md

Guidance for Claude (and other coding agents) working on this repository.

## What this is
Podcast Ad Cleaner for Windows. It checks podcast feeds, downloads new episodes, transcribes them (Whisper), finds
ads with an AI (Ollama, LM Studio or a cloud service), cuts them with ffmpeg, writes chapters and transcripts, and
serves the ad-free episodes as RSS feeds for a phone podcast app. It started as a port of
adamc199/podcast-ad-cleaner. The owner is not a programmer, so anything user-facing must be in plain words.

## Layout
```
podcast_cleaner/
  __main__.py      command line: tray (default), engine, once, search, check, serve, list, get, skip
  app.py           Worker (the background loop), run_engine (engine process), run_tray (window + tray)
  desktop.py       the desktop window: pywebview shows the web pages, plus the pystray tray icon
  api.py           web API: /api/status, /api/action/<name>, /api/settings, Engine, RemoteWorker client
  webtools.py      the rest of the API: GET /api/<name>, POST /api/do/<name> (podcasts, AI, transcription...)
  server.py        HTTP server: RSS feeds, media files, and it routes /api and /app to api.py
  web/             the web pages: index.html, app.css, app.js (core tabs), pages.js (form tabs). Plain JS, no build.
  pipeline.py      one check: find episodes -> prepare (download, transcribe) -> finish (find ads, cut, write files)
  whisperpool.py   transcription on this PC or on other PCs (speaches / whisper.cpp servers), one episode each
  transcribe.py    Whisper on this PC (faster-whisper / openai-whisper); whispercpp.py for whisper.cpp
  analyze.py, pool.py, lmstudio.py, cloud.py   finding ads with the AI; pool.py shares sections over Ollama PCs
  cues.py, learned.py, crossfetch.py, pubtranscript.py, review.py   extra ad-finding checks, held-for-review
  media.py         ffmpeg: cutting, tones, loudness, chapters
  db.py            the SQLite database (<output_dir>/podcast-cleaner.db); state.py wraps it (same API as old JSON)
  config.py        DEFAULTS and load_config; settings_model.py checks config.yaml with pydantic
  configedit.py    edits config.yaml keeping comments (ruamel)
  gui.py, gui_extra.py, theme.py   the older Tkinter window (fallback: tray --classic)
tests/             pytest suite (stand-in servers, fake Whisper, synthetic audio)
build.ps1          PyInstaller one-file build; build-whisper-vulkan.ps1 builds whisper.cpp with Vulkan
```

## How it runs
- The **engine** is its own process (`python -m podcast_cleaner engine`). It serves feeds, the API and the pages
  on `server.port` (default 8585).
- The **desktop window** starts the engine if needed and talks to it only through the API. The window and a phone
  browser show the same pages (`/app/`).
- `PODCAST_CLEANER_ENGINE=inprocess` runs everything in one process; `PODCAST_CLEANER_UI=classic` (or `--classic`)
  uses the Tkinter window.
- Who may use the API: this PC always, the home network when `server.token` is empty, otherwise only with the token
  in the path.
  - Every POST needs the header `X-Podcast-Cleaner: 1`.
  - Secrets (api_key, token...) are sent to pages as `********`; a `********` value coming back means "unchanged".
  - Opening files or folders, Clean files and Windows startup are allowed only from this PC (`webtools.LOCAL_ONLY`).

## Commands
```
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest                      # whole suite (~3-4 min); needs ffmpeg on PATH
python -m pytest -m "not gui"         # without the Tkinter window tests
python -m podcast_cleaner engine --console     # engine only, log in the console
python -m podcast_cleaner tray --paused        # the app, starting paused
```
- On Linux, run the gui tests with a virtual display (e.g. `Xvfb :97` and `DISPLAY=:97`).
- `tests/conftest.py` sets `PODCAST_CLEANER_FEED_HELPER=0`, `PODCAST_CLEANER_ENGINE=inprocess` and
  `PODCAST_CLEANER_UI=classic`. Tests that need the separate engine or the web window set them back themselves.

## Conventions
- Add new settings to `config.DEFAULTS`; they're then checked automatically.
  - Fixed choices go in `settings_model.CHOICES`; lists/free-form values go in `LOOSE`.
  - Document every new setting in `config.example.yaml` and `README.md`.
- Store state in the database, never in new JSON files: use `db.get(output_dir)` (`get/put/change` for small named
  values) or `PodcastState`.
- New page features:
  - the endpoint goes in `webtools.py` (`get_<name>` / `do_<name>`), raising `Problem("plain message")` for user
    errors;
  - the page code goes in `app.js` or `pages.js`, using the helpers `get`, `post`, `act`, `esc`, `toast`,
    `buildForm`/`readForm`.
- Always escape text put into HTML with `esc()`.
- Style "Warm Cream Editorial":
  - colors: cream `#F9F7F2`, white cards, amber `#ECAE36` (text `#A86B0C`);
  - fonts: DM Serif Display for headings, Instrument Sans for the body;
  - small labels: uppercase and tracked;
  - colors come from the CSS variables in `app.css` (light and dark).
- Tab order: Activity, Podcasts, Choose Episodes, Browse Feeds, Ad Rules, Found Ads, Stats, AI, Transcription,
  Settings, Log.
- Windows first: `creationflags=NO_WINDOW` for subprocesses, paths may be on network drives (retry on locks), and
  PowerShell scripts must work in Windows PowerShell 5.1. In 5.1, a native program's stderr counts as an error when
  `$ErrorActionPreference = "Stop"`.
- User-facing text: short, plain words, no jargon; say what will happen and what to do next.

## Before finishing a change
1. Add or update tests, then run the whole suite.
2. For web page changes, open the pages (desktop and phone width) and check the browser console for errors.
3. Keep personal data out of the repository:
   - no real names, email addresses, PC names, IP addresses or personal paths;
   - `config.yaml`, `config.yaml.bak`, `logs/`, `*.db` and `memory.md` are never committed;
   - API keys belong only in `config.yaml` or environment variables;
   - examples use `D:\Podcasts`, `192.168.1.x` and names like "Gaming PC".
4. Update `README.md` for anything the user will notice.
