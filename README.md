# Podcast Ad Cleaner for Windows

A Windows system-tray port of [adamc199/podcast-ad-cleaner](https://github.com/adamc199/podcast-ad-cleaner).
It watches a list of podcast RSS feeds, downloads new episodes, transcribes them with Whisper,
finds ads with a local Ollama model, cuts them out with ffmpeg, and writes chapters into the MP3.

## What's new vs. the original

| Original (bash, Linux/macOS) | This version (Python, Windows) |
|---|---|
| Interactive one-off run | Runs in the **system tray**, checks on a schedule |
| iTunes search every run | **RSS feed per podcast** in `config.yaml` (iTunes `search:` still supported, resolved once) |
| One podcast per run | **List of podcasts** checked for new episodes |
| Only the latest episode, re-downloaded every time | **Tracks processed episodes** (by GUID) and never redoes them |
| Settings hard-coded in the script | **All settings in `config.yaml`** (Whisper, Ollama, ffmpeg, ad rules, schedule) |
| All files in one folder | **Folder per podcast**, with `assets/` for `.vtt`, `.json`, `.txt` |
| — | **Old assets deleted** when a new episode is processed |

Also fixed along the way:

- **Accurate ad boundaries.** The original guessed times from word counts (~150 wpm) and assumed every ad was 60–90 s. Now the model sees real Whisper timestamps and its answers are snapped to sentence boundaries, so the actual ad length is cut.
- **Transcript and chapters match the cleaned audio.** The VTT/text transcript drops the ad portions and is re-timed; chapters are shifted by the removed time.
- **Publisher chapters** from `<psc:chapters>` and `<podcast:chapters>` are used when present; chapters titled "Ad", "[Ad]", "Sponsor"… are removed as ads.
- **Valid JSON** (titles with quotes no longer break `chapters.json`), Windows-safe filenames, ID3 title/album/date/artwork tags.
- No console windows flash up while running in the tray.
- **faster-whisper** is the default transcription engine: 2–4× faster on CPU, less memory, no PyTorch install. Progress (%) shows in the tray tooltip.
- **Built-in RSS feed server**: subscribe to your ad-free episodes from your phone's podcast app.
- **Sponsor memory**: sponsors found in earlier episodes of a show are added to the AI prompt, so recurring ads are caught more reliably. You can also list known sponsors per podcast.
- **Smooth cuts**: each cut is joined with a short crossfade (40 ms by default) instead of a hard splice.
- **Transcript archive**: every episode's transcript is kept in `transcripts\`, and the feed links it for each episode.
- **Retention by count or age** (`keep_episodes`, `keep_days`).
- **Single `.exe` build** with PyInstaller, so the PC running it doesn't need Python.

## Install

Requirements: Windows 10/11, ~3 GB free disk, Python 3.10–3.12 (installer can fetch it via winget).

```powershell
# from the project folder
powershell -ExecutionPolicy Bypass -File install.ps1            # CPU
powershell -ExecutionPolicy Bypass -File install.ps1 -Cuda      # NVIDIA GPU (much faster)
# run PowerShell as administrator to also add the firewall rule for the feed server
powershell -ExecutionPolicy Bypass -File install.ps1 -Startup   # also start with Windows
```

The installer creates `.venv`, installs packages, installs **ffmpeg** and **Ollama** with winget if missing,
pulls the Ollama model, copies `config.example.yaml` → `config.yaml`, and runs a self-check.

## Use

1. Edit `config.yaml` and list your podcasts:
   ```yaml
   podcasts:
     - name: "The Daily"
       rss: "https://feeds.simplecast.com/54nAGcIl"
     - name: "Hard Fork"
       search: "Hard Fork"      # looked up on iTunes once
       initial_episodes: 2
   ```
   Find a feed URL: `.venv\Scripts\python -m podcast_cleaner search "show name"`
2. Start it one of two ways:
   - **Start in Tray.bat** starts it normally, hidden in the tray. It checks feeds and processes episodes
     straight away. Windows startup (`install.ps1 -Startup`) starts it this way too.
   - **Start Podcast Cleaner.bat** opens the window **paused**: nothing is checked or downloaded until you press
     **Resume**. This is handy for changing settings or arranging Up next first.

   Closing the window keeps it running in the tray; double-click the icon to bring it back. From a terminal:
   `python -m podcast_cleaner tray [--minimized] [--paused]`.

### The window
- **Header**: what it's doing right now, next check time, and **Check now**, **Pause / Resume**, **Open folder**, **Phone feeds**.
- **Activity**:
  - **Now**: the episode being processed, its step (downloading, transcribing %, finding ads, ...), a progress bar and
    time so far.
    - **Stop After This Episode**: let this one finish, then pause instead of starting the next (click **Keep Going**
      to undo).
    - **Stop Now**: pause immediately. The current episode is interrupted and started over when you click **Resume**.
    - **Also:** (when stages overlap) the next episode, already being downloaded / transcribed. Stop After This
      Episode doesn't start another one meanwhile; one already being transcribed is redone when you resume.
    - **Ollama:** (with other PCs) what each computer is doing: busy, idle, waiting for Whisper, offline.
  - **Up next**: everything waiting, both new episodes and ones you chose. **Skip selected** removes them from the queue.
    **Reorder** with **Top / Up / Down / Bottom** (select one or several - click, then Shift+click or Ctrl+click) or
    by dragging a row. The next episode
    started always follows your order, even mid-check, and it's remembered (`<output_dir>\.queue-order.json`) for
    episodes waiting for the next check and across restarts. New episodes you haven't placed go after the ones you
    have; a fresh Re-analyze still goes first until you move it.
  - **Recently finished**: cleaned and failed episodes with ads/time removed or the error. Double-click to open the folder.
    **Re-analyze** (select one or more) downloads the original again and redoes transcription and ad detection with your
    current Whisper model, Ollama model, ad rules and sponsor lists, e.g. after improving your Ad rules or switching to
    a bigger model. It goes to the front of the queue (shown under **Up next**), replaces the cleaned file when done,
    and updates the episode's rows in `ads.csv` (ads you marked "Not an ad" are kept). If it fails, the existing file is
    kept and the reason is shown. It also works for episodes since deleted by retention, and for ones that have left
    the feed if they were processed with this version or later (the audio address is now saved). The episode's files
    replace whatever is in its show's `assets\` folder.
- **Choose Episodes**: new episodes of shows set to "You choose", grouped by show. Select episodes (or a whole show) →
  **Download** or **Skip**. Double-click downloads. The tab title shows how many are waiting.
- **Browse Feeds**: pick any podcast from the dropdown to list **every episode in its feed** (newest first) with its
  date, length and status: Downloaded, Queued, Failed, Not downloaded (including older episodes skipped when the
  show was first added). Click episodes to tick them, or use **Select all shown** (works with the search box), then
  **Download selected**. They're queued and processed right away, oldest first. Works for automatic and "You choose"
  shows alike. **Hide downloaded** shortens long lists; **Refresh** re-reads the feed.
- **Up next** always lists what's waiting, also when nothing is running or the app is paused. Episodes found
  by a check are remembered, and while paused the app still looks for new episodes on its schedule (without
  processing anything; `check_while_paused: false` turns that off). Starting paused (Start Podcast Cleaner.bat)
  fills the list straight away.
- **Up next > ▶ Start now**: select one or more episodes, including low-priority ones outside their hours, and
  start them.
  - **Paused**: only the episodes you picked are processed, then it pauses again.
  - **Running**: they move to the top and go next.
  - **Resume** or **Check now** go back to processing everything.
- **Stats**: time saved, per podcast and per sponsor, with a weekly chart.
- **Found Ads**: at the top, **Held for review**: possible ads left in for you to **Approve & cut** or **Dismiss** (see
  *More checks* below). Below that, every ad ever cut, across all podcasts: date, show, episode, where it was, length, sponsor, and whether the
  AI or the feed found it. Select one to see **what was said**. Filter by podcast or search. The **Sponsors** panel
  totals ads and time per sponsor. **Not an ad** / **It is an ad** corrects a cut. The audio already cut stays cut,
  but future checks use the correction. **Open ads.csv** opens the full history in Excel.
  **Always watch for** (below the Sponsors panel, sortable like the other lists): **Add sponsor...** adds advertisers to look for on every show, even
  ones the AI has never found (several at once, comma-separated); **Remove** takes them off. Stored as
  `ollama.watch_sponsors` in `config.yaml`. Adding a sponsor you once marked "Not an ad" overrides that correction.
- **Podcasts**: every show with its mode, priority, episode count and feed. **Add podcast...** (search by name or paste an RSS
  address), **Edit...**, **Auto / Choose**, **Enable / Disable**, **Remove**, **Clean files...**, **OPML** (import /
  export), **Open folder**. Renaming a show also
  renames its folder.
- **Ad Rules**: tell the AI what counts as an ad, in plain English, e.g. "Patreon plugs and tour dates are ads; hosts
  recommending something without a promo code is not". **Insert example** adds a starting point. Also the length limits
  (ignore ads shorter/longer than, extra trim around each cut). Saved as `ollama.extra_instructions` and `ad_removal.*`.
- **Settings**: **General** (output folder, check interval, notifications, start with Windows), **Transcription**
  (Whisper engine - faster-whisper, whisper.cpp or openai-whisper - model, device, speed and batch), **Other Settings**
  (crossfade at cuts, transcript archive, overlap, volume), **Phone feeds**, **Low Priority Hours**, and
  **Phone Notifications** (ntfy topic, server, what to tell you, Send a test). **Edit config.yaml** opens the file for everything else.
- **AI**: **AI Service** is the one place to choose the AI: Ollama, LM Studio (GPU), or a cloud service (see *Cloud AI*
  below). With Ollama it also has this PC's Ollama address and the **Model** - a dropdown of the models installed in
  Ollama with their size (embedding-only models are hidden; ↻ re-checks after starting Ollama or pulling a model).
  Below it, the PCs running Ollama or LM Studio (see *More computers* below). **Add PC...**, **Remove**, **Test all** (reachable,
  and has the model), and per PC its status, sections done and average time per section, live while working.
  **This PC's Ollama helps**: when_idle / always / never.
- **Log**: live log, errors in red.

The tabs are in this order: Activity, Podcasts, Choose Episodes, Browse Feeds, Ad Rules, Found Ads, Stats, AI, Settings,
Log. The look is a warm "editorial" style: a cream page with white cards, one honey-amber accent, DM Serif Display
for headings and figures, Instrument Sans for everything else, and section labels in spaced capitals. Both fonts
ship with the app (`podcast_cleaner\fonts`, SIL Open Font License) and are loaded for the app only; nothing is
installed on the PC.

Changes made in the window are saved to `config.yaml` with your comments kept (the previous version is saved as
`config.yaml.bak`), and take effect right away. Editing `config.yaml` by hand still works too.

The tray icon still has the quick menu (Check now, Pause, Choose episodes, Quit). To run with only the tray icon, use
`python -m podcast_cleaner tray --no-gui`.

### Lists: sorting and search
Click any column heading to sort that list by it; click again to reverse (an arrow shows which column and which
way). Numbers, dates, times and lengths sort by value ("#702" before "#2198", "40:00" before "1:00:00"), and names
ignore a leading "The". The sort stays put as the list updates. **Up next** is the exception: its order is the order
episodes are processed in, so it's arranged with Top / Up / Down / Bottom or by dragging instead.

**Recently finished** has a **Search** box: type part of a show, episode, result or detail (e.g. "failed",
"incomplete", a show name) to filter it. Search looks through all saved history, not just the latest 80.

### Schedule
A check runs at startup, then every `check_interval_minutes` (default 60). Feeds are re-checked on that schedule
**even while episodes are being processed**: the check runs in the background, and new episodes are added to
**Up next** of the work already under way (the Now panel shows "Checking feeds in the background"). Episodes you
pick in **Browse feed** / **Choose episodes**, or send to **Re-analyze**, while it's busy are picked up within a few
seconds the same way. An episode already in progress or waiting is never added twice. When the work is done, the
next check is due `check_interval_minutes` after the latest one.

### Download priority
Each podcast has a priority (**Podcasts > Edit > Priority**, or `priority:` in its config entry), shown in the
Podcasts tab:
- **Highest**: its new episodes go to the very top of **Up next**, ahead of everything waiting, including episodes
  you've arranged yourself. They're shown in bold blue with "(highest priority)".
- **High**: its new episodes go right after the Highest ones, still ahead of episodes you've arranged and everything
  else waiting. Shown in bold blue with "(high priority)".
- **Medium** (default): its new episodes join after the ones already waiting.
- **Low**: its new episodes go to the bottom, and are shown greyed out.

Once an episode is in the list, moving it yourself (Top / Up / Down / Bottom or dragging) always wins.

### Checking each show only when it usually publishes
Each show's release pattern is learned from the publish dates in its feed (it works from the first check, and is
updated every time the feed is read). A show that posts weekdays around 6 AM is then only checked weekdays from about
5 AM to 9 AM; a show that never posts at weekends isn't checked at weekends; a weekly show only on its day. The
**Podcasts** tab shows what was learned in the **Checked** column (e.g. "weekdays, 4:50 AM - 9:40 AM").

- Nothing is missed: a feed lists all its episodes, so the next check finds anything posted in between. At worst an
  episode is found a little later.
- If an expected episode doesn't appear in its window, the show keeps being checked (every check) for up to a day.
- Shows without a clear pattern, or with fewer than 6 recent episodes, are checked every time.
- **Check now**, episodes you pick or re-analyze, and newly added shows always check right away.
- Turn it off for one show in **Podcasts > Edit > Check feed** (`smart_schedule: false`), or for all in
  `config.yaml` under `smart_schedule:` (margins, late time, and an optional safety-net
  `max_hours_between_checks`).
- The log lists which shows were skipped at each check.

### Output layout
Episode names start with the publish date as month-day-year, e.g. `10-18-26 - Episode title.mp3`, and the same
date leads the title your podcast app shows. Change the format with `output.date_format` (e.g. `"%Y-%m-%d"` for
2026-10-18, which also sorts files in date order in Explorer), or set `output.date_in_title: false` to keep the date
in the file name only. Episodes already processed keep their names until re-analyzed.

```
<output_dir>\
  The Daily\
    10-08-26 - Episode title.mp3      ← ad-free, chapters + artwork embedded
    10-07-26 - Previous episode.mp3
    .podcast-state.json                 ← which episodes are done/skipped/failed
    transcripts\                        ← every episode's .vtt + .transcript.txt (archive)
    assets\                             ← only the most recent episode's files
      10-08-26 - Episode title.vtt
      10-08-26 - Episode title.transcript.txt
      10-08-26 - Episode title.chapters.json
      10-08-26 - Episode title.report.txt
```

### Choosing episodes yourself
Add `auto_download: false` to any podcast you don't want downloaded automatically:

```yaml
  - name: "The Joe Rogan Experience"
    rss: "https://feeds.megaphone.fm/GLT1412515089"
    auto_download: false
```

New episodes of those shows are checked as usual but only **listed**, and you get a notification. Pick them in the
window's **Choose episodes** tab, or right-click the tray
icon → **Choose episodes** → the show → an episode → **Download** or **Skip**. Each show also has **Download all** and
**Skip all**.

- **Download**: processed right away, or straight after the current episode if it's busy. Queued episodes show a ✓.
- **Skip**: removed from the list immediately and never downloaded. "Skip (cancel download)" also cancels a queued one.
- Failed episodes stay in the list as "(failed)" with **Retry download** or **Skip**.
- An undecided episode just stays in the list (the newest 15 per show are shown).

From a terminal: `python -m podcast_cleaner list` shows the choices; `get 3 7` / `get all` downloads,
`skip 4` / `skip all` skips. Choices are kept in `<output_dir>\.download-requests.json` until the next check applies them.
To un-skip an episode, delete its entry from the show's `.podcast-state.json`; it reappears after the next check.

### Episode tracking
- The first time a podcast is seen, only the newest `initial_episodes` are processed; the back catalogue is marked *skipped* (otherwise you'd download years of episodes).
- To reprocess an episode, delete its entry from `.podcast-state.json` (or delete the file to start that podcast over).
- Failed episodes are retried on later checks up to `max_retries` times; the error is saved in the state file and log.
- If Ollama isn't running and `ollama.required: true`, episodes are **postponed** (not marked failed).

### Subscribe from your phone (feed server)
The tray app also runs a small web server (port 8585 by default). Right-click the tray icon →
**Open feed page** to see each podcast's feed address with a Copy button. In your podcast app choose
*Add by URL* / *Add RSS feed* and enter the address, e.g.

```
http://192.168.1.20:8585/feed/the-daily.xml     ← one podcast
http://192.168.1.20:8585/feed/all.xml           ← every cleaned episode in one feed
```

- Episodes appear as soon as they're processed; deleted MP3s (e.g. via `keep_episodes`) drop out of the feed.
- Seeking and resumed downloads work (HTTP Range requests). The newest episode also carries Podcasting 2.0 chapters and a transcript link.
- The phone must be on the same Wi-Fi. Away from home, use **Tailscale** (free) and set `server.public_url` to the PC's Tailscale address.
- If the server could be reached from outside your network, set `server.token` to a random word; it becomes part of every URL.
- Windows asks to allow network access the first time; choose **Private networks**. (Running `install.ps1` as admin adds the firewall rule for you.)
- Feeds are marked `itunes:block` so they never get listed in public directories.
- `python -m podcast_cleaner serve` runs only the feed server, without processing.

### Transcription speed
Settings > Transcription has the main controls; everything is also in `config.yaml` under `whisper:`.

- **Engine**
  - **faster-whisper** (default): CPU, or an NVIDIA GPU (install with `-Cuda`, then `model: turbo` or `large-v3`).
  - **whisper.cpp**: runs on **AMD, Intel or NVIDIA graphics through Vulkan**, including integrated graphics such as
    the Radeon 680M, which faster-whisper can't use. Run `build-whisper-vulkan.ps1` once from an administrator
    PowerShell: it installs Git, CMake, the Visual Studio C++ build tools and the Vulkan SDK if needed, builds
    whisper.cpp with Vulkan and puts `whisper-cli.exe` in `.\whisper.cpp\`. Then choose Engine **whisper.cpp**.
    The speech model downloads on first use (to `.\models\whisper.cpp\`). Good models: `large-v3-turbo-q5_0`
    (accurate) or `small-q5_1` (fast). The log says `whisper.cpp on GPU (Vulkan)` when the graphics are used, and
    warns if it fell back to the CPU.
  - **openai-whisper**: the original library (slowest; `pip install openai-whisper`).
- **Distilled models** (`distil-small.en`, `distil-medium.en`, `distil-large-v3`, `distil-large-v3.5`): about twice
  as fast as the model they're based on, with nearly the same accuracy (faster-whisper; `.en` = English only).
- **Batch** (`whisper.batch_size`, default 8): faster-whisper transcribes several 30-second pieces at once, about 2x
  faster on a CPU for about twice the memory. It always skips music and silence. 0 turns it off.
- **Speed** (`beam_size`): 1 is fastest, 5 slightly more accurate.
- The log shows how long each transcription took, so settings can be compared.

### Ad detection tuning
Everything below except sponsor memory can also be set in the window (**Ad rules** tab, and **Podcasts → Edit → Known sponsors**).

- **What counts as an ad** (`ollama.extra_instructions`): plain-English guidance added to the prompt for every show. Saying what is *not* an ad helps as much as saying what is.
- **Sponsor memory** (`ollama.remember_sponsors`): sponsors of ads that were removed are saved in each podcast's `.podcast-state.json` (with a count and last-seen date), and the most recent `max_known_sponsors` are named in the prompt for future episodes. Delete a name there if it was a false positive.
- **Known sponsors** per podcast: `sponsors: [BetterHelp, Squarespace]` in its config entry.
- **Crossfade** (`output.crossfade_ms`, default 40): set `0` for hard cuts.
- **Volume** (`output.normalize`, on by default; Settings > Volume): every cleaned episode is brought to the podcast
  loudness standard, **-16 LUFS** with peaks below -1.5 dBTP, so quiet and loud shows play at the same level. The cut
  audio is measured first (EBU R128 meter) and one fixed gain is applied during the (single) encode, with a limiter
  only if peaks would go over the ceiling, so the show's own dynamics are kept. Each episode's `report.txt` and the log show the change, e.g. "-22.4 LUFS -> -16.0 LUFS (+6.4 dB)".

### LM Studio and LM Studio Bionic (GPU)
LM Studio can find the ads instead of Ollama, on this PC and/or other PCs, using their GPUs. LM Studio Bionic's local
models run on the same LM Studio runtime, so the app talks to LM Studio's server either way.

Setup, in LM Studio on each PC: download a model, then **Developer > Start server** (other PCs: also turn on
**Serve on Local Network**, and allow TCP port 1234 in Windows Firewall for the home network). Then in the app's
**AI** tab, choose **LM Studio / LM Studio Bionic**, click ↻ to list LM Studio's models, pick one, and click
**Test**, then **Save**. The list below then becomes **Computers running LM Studio**: **Add PC...** for other PCs (port
1234 assumed), **Test all** shows whether each is reachable and has the model loaded (or will load it), and while
episodes are analyzed it shows each PC's status, sections done and average time, just like the Ollama list.

- **GPU use:** if the model isn't loaded, the app loads it with your settings: **GPU offload** (`max` = the whole
  model on the GPU), **context length**, **flash attention** and **cache on GPU**. GPU offload is applied through
  LM Studio's `lms` command, which is installed with LM Studio (on other PCs it uses `lms --host`). Without `lms`, the
  model is loaded through LM Studio's API and LM Studio picks the offload itself. A model you've already loaded in
  LM Studio is used as it is.
- **Several sections at once** (default 4 per PC): LM Studio answers requests in parallel on the GPU, so one GPU
  does much more than one section at a time.
- **Context window:** LM Studio shares a model's context length between the requests it runs at once (context 8192
  with 4 at once = about 2,000 tokens each). The app reads this from each PC and splits sections that wouldn't fit;
  if a PC still reports "Context size has been exceeded", the section is split in half and retried instead of being
  left unchecked, and that PC gets smaller pieces from then on. For best results give the model enough context in
  LM Studio: about **4,000 tokens per request** (e.g. context 16384 for 4 at once). **Test** in the AI tab shows the
  tokens each request gets.
- Answers are requested in a fixed JSON format (LM Studio's structured output), so small models stay on format;
  "thinking" models' reasoning is ignored.
- **This PC** defaults to `always` (a GPU doesn't compete with Whisper on the CPU). If Whisper also uses the GPU
  (`whisper.device: cuda`) and the GPU runs short of memory, set `lmstudio.this_pc: when_idle` or lower the offload.

### Cloud AI: ChatGPT, Claude, Gemini and others
Instead of Ollama, the ads can be found by a cloud AI service: **OpenAI** (ChatGPT models), **Anthropic** (Claude),
**Google Gemini**, or any service with an OpenAI-compatible API (Groq, OpenRouter, Mistral, DeepSeek, Together, xAI,
or LM Studio / vLLM on another machine). Set it up in the **AI** tab under **AI Service**:
1. Pick the service and paste an API key (from the service's developer site; the tab shows where).
2. Click ↻ to list the models your key can use and pick one. The defaults are small, inexpensive models.
3. Click **Test**, then **Save**.

- They get exactly the same instructions, ad rules, sponsor lists and transcript sections as Ollama, and answer
  in the same format; everything else (cutting, chapters, history) is unchanged.
- **Sections at once** (default 4) are sent in parallel, so a whole episode usually takes well under a minute.
- **If it fails or runs out of credit, use local instead** (on by default; shown for the cloud services only): sections
  it can't do go to your Ollama
  PCs, so episodes still finish. Rate limits ("too many requests") are waited out and retried first.
- **Cost and privacy:** transcripts are sent to that company, and you're billed per token. A 2-hour episode is roughly
  40,000 words of input. Token usage is logged for every episode, and shown in the Activity tab while it works.
- The API key is stored in `config.yaml` (never in the GitHub copy - it's in `.gitignore`), or leave it blank and set
  the `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` environment variable.

### More computers (Ollama pool) and overlapping stages
Ad finding is the slow part on a CPU. Other PCs on your network running Ollama with **the same model** can share it:
each episode's transcript is split into sections (`chunk_minutes`), and every section goes to whichever PC is free,
fastest first. Nothing but Ollama is needed on the other PCs; the transcript, sponsor lists and ad rules are all
sent from this PC, and all results, history and files stay here.

On each other PC:
1. Install Ollama and run `ollama pull <model>` (the same model as `ollama.model`).
2. Set the Windows environment variable `OLLAMA_HOST` = `0.0.0.0` (System Properties > Environment Variables), then
   quit Ollama from its tray icon and start it again. To let a strong PC take 2 sections at once, also set
   `OLLAMA_NUM_PARALLEL` = `2` and use `parallel: 2` for it below.
3. Allow inbound TCP port 11434 in Windows Firewall, for the Private (home) network only. Ollama has no password,
   so never expose it to the internet.

Then add it in the window (**Computers > Add PC...**) or in `config.yaml`:
```yaml
ollama:
  servers:
    - http://192.168.1.20:11434
    - {url: http://gaming-pc:11434, name: Gaming PC, parallel: 2}
  this_pc: when_idle     # when_idle | always | never
processing:
  overlap_stages: true
```

How it behaves:
- **Overlapping stages** (`processing.overlap_stages`): while the other PCs analyze episode 1, this PC downloads and
  transcribes episode 2. It transcribes at most one episode ahead (`prepare_ahead`), then Whisper goes quiet and
  this PC's Ollama can join in.
- **This PC** (`ollama.this_pc`): `when_idle` (default) takes sections only while Whisper has nothing to transcribe,
  because both share the CPU. `always` suits a PC with a GPU. `never` leaves it to the others (unless none is reachable).
- **Pause / Resume** (AI tab, select a computer): a paused computer finishes the section(s) it's working on, then gets
  no new ones until you resume it; the other computers carry on (the status shows "pausing - finishing..." then
  "paused"). Use it to free up a PC you want to use - e.g. for a game. It's remembered across checks and restarts
  (`<output_dir>\.paused-computers.json`). If every computer is paused, ad finding waits until one is resumed (or, with
  a fallback, goes to Ollama).
- A PC that's off, asleep or missing the model is skipped (and looked at again every 5 minutes); one that fails
  mid-section rests for 2 minutes and the section is redone on another. If every PC fails a section, it's
  reported as unchecked as before (Re-analyze retries it).
- With no other PC reachable, and `this_pc` not `always`, episodes are done one at a time as before, since on one CPU
  overlapping would just make Whisper and Ollama take turns.
- With overlap on, the next episode is picked when its transcription starts, so a change to **Up next** applies
  from the episode after the one already being transcribed.
- Results are the same as with one PC, because the same model gets the same prompt; a section's answer doesn't
  depend on which PC gave it.

### How ads are found (ideas from Podly)
Several detection ideas come from [Podly](https://github.com/podly-pure-podcasts/podly_pure_podcasts) (MIT licence).
All are set on the **Ad rules** tab (Detection) or in `config.yaml`:

- **Publisher chapters from more places**: besides the feed's chapters, chapters inside the MP3 file and a timestamp
  list in the episode description ("12:30 Sponsor: Acme") are used. When they mark the ads you can **trust them**:
  skip the AI for that episode, or skip transcription too - the fastest possible (`chapters.publisher_ads`).
- **Ad clues** (no AI needed): web addresses, promo codes, phone numbers, calls to action and "brought to you by" are
  highlighted in the text the AI reads, raise its confidence, and pull an ad's **lead-in and "back to the show" lines**
  into the cut. An ad in the first 45 seconds also gets its intro, if there's a lead-in just before it.
- **Confidence**: the AI says how sure it is and whether it's a sponsor, a promo for another show, or the hosts' own
  product. Below **Cut only when at least** (default 60%) it's left in and listed under "Left in" in `report.txt`. The
  hosts' own products are cut only if you choose to (see *What to cut* below). `ads.csv` has a confidence column.
- **Better instructions**: the AI is told the show's name and what it's about, and gets a worked example of an ad and
  of a company mention that isn't one.
- **Second pass on ad edges** (off by default): one extra request per ad, asking exactly where it starts and ends.
  Edges move at most 30 s earlier / 20 s later. Worth it with a GPU or cloud service; slow on a CPU.

### More checks (ideas from MinusPod)
These ideas come from [MinusPod](https://github.com/ttlequals0/minuspod) (MIT licence). They're set on the
**Ad rules** tab (More checks) or in `config.yaml`:

- **Learned ad reads** (`ad_removal.learn_ads`): the words of every ad that's cut are remembered in
  `.learned-ads.json`. When the same read turns up in a later episode of any show, it's found by a text match with
  no AI call, and those lines aren't even sent to the AI. Small transcription differences don't matter. Marking
  an ad **Not an ad** in the Found Ads tab flips its learned read, so that text is never cut again.
- **Held for review** (`review`): possible ads that weren't cut on their own appear at the top of the **Ads** tab:
  - ads the AI was 40–59% sure of
  - parts that differed on a second download, when nothing else found an ad there
  - speech missing from the publisher's transcript, when nothing else found an ad there

  They stay in the audio until you decide. **Approve & cut** re-cuts the episode right away from the kept
  original, with no new download, transcription or AI, and teaches future episodes. **Dismiss** teaches that
  it's not an ad. Originals are kept in `.originals` only for episodes with something held
  (`keep_original: when_held`), for 14 days.
- **Ads in the same break are joined** (`ad_removal.ad_break_gap_seconds`, default 12): when no more than
  12 seconds of talk separates two ads, they're cut as one, so stings, music and "more after this" between
  them go too. Music and silence don't count as talk. A joined cut is never longer than 5 minutes, and
  nothing you marked Not an ad is swallowed.
- **Ad endings finished off** (`ad_removal.complete_tails`): if the line or two after an ad still names the
  sponsor or gives a web address or promo code, the cut is extended over them.
- **Download twice** (`second_download.mode`): ad servers insert different ads into each download. The episode is
  downloaded a second time, looking like a different podcast app, and the two are compared by sound. Parts of
  the first download not found anywhere in the second are inserted ads; their edges are found to within
  about 0.1 s. They confirm and extend ads already found, or are held for review. The default `auto` only does
  this for hosts known to insert ads (Megaphone, Art19, Acast, Omny, Simplecast, Podtrac and others), since it
  doubles the downloading. The same audio re-encoded differently isn't mistaken for a difference.
- **Publisher's transcript** (`ad_removal.use_publisher_transcript`): when the feed links one
  (`<podcast:transcript>`), it was usually made before ads were inserted. Stretches of at least 30 seconds of
  speech it doesn't have confirm an ad, or are held for review.

Each episode's `report.txt` says what each check found, for example "confirmed: different on a second download"
or "joined to the next ad", and lists what's held.

### What to cut, cut tones, badge, OPML and stats (ideas from Podcast Ad Remover)
These ideas come from [Podcast Ad Remover](https://github.com/jdcb4/podcast-ad-remover) (MIT licence):

- **What to cut** (Ad rules > What to cut, or per show in Podcasts > Edit > Also cut): sponsor ads are always cut.
  You choose whether to also cut promos for other shows, the hosts' own promos (Patreon, merch, tour), generic
  intros, generic outros, and ad jingles or dead air. Intros and outros only cover the routine parts: greeting,
  housekeeping, credits, "rate and review". A cold open, the real content or a summary always stays. The AI is
  only asked to find intros and outros for shows where you cut them. A show's own choices only store what
  differs from the Ad Rules tab, so changing the defaults later still applies to it.
- **Stretches with no speech** (music, jingles, silence of 4 s or more) are shown to the AI as
  "(no speech for 12 s)" lines, so it can include a jingle in the ad around it. Unexplained gaps stay in.
- **Cut tones** (`output.cut_tones`): a soft, short wooden "tock" where something was cut. Two rising notes mean
  the start was cut, two falling notes the end, and one low note a cut in the middle. Each tone takes the place
  of the first fraction of a second of its cut, so chapters and transcripts stay exact. Untick it on the
  Ad Rules tab if you prefer silent cuts.
- **"AD-FREE" badge** (`output.artwork_badge`): a small badge on the artwork embedded in each MP3, and on the
  show's cover in your phone feeds.
- **OPML** (Podcasts tab): **Import OPML...** adds the shows from another podcast app's export (Pocket Casts,
  Apple Podcasts, Overcast...). Shows you already have are skipped, and you choose whether new ones download
  automatically. **Export OPML...** saves either your **ad-free feeds**, to subscribe to all of them in your
  phone's app at once, or the original feeds.
- **Stats** tab: episodes cleaned, ads removed, time saved, per episode and as a share of the audio, for the
  last 7 days, 30 days, 12 months or all time. It also shows a per-podcast table, top sponsors, and a chart of
  time removed per week.

### Checking, listening, testing and phone alerts
- **▶ Listen** (Found Ads tab): **Listen to the cut** plays 8 seconds either side of where an ad was removed, so you can
  hear the join. In *Held for review*, **▶ Listen** plays the held part, from the kept original if there is one.
  Clips open in your usual audio player.
- **Test AI models** (AI tab): runs the AI setups you tick over episodes whose ads you've checked, and scores each
  one. Setups can be each Ollama or LM Studio computer, and each cloud service with an API key. The score covers
  how much of the real ad time it found, which ads it missed, how much it would have cut wrongly, and how long it
  took. "Real" means `ads.csv` as you've corrected it with Not an ad and approvals. Nothing is cut or changed.
  Multi-computer setups are listed too: **As used for episodes** (exactly your AI tab setup, fallback included),
  and **all Ollama / LM Studio computers together**. These split the sections between computers like a normal run,
  so you can see what an extra PC gains. Tests that use different computers run at the same time; tests sharing a
  computer take turns, so the times stay fair. A progress bar shows sections done across all tests, with time left,
  and each test's status: waiting for its computer, episode 1/3, done...
  Reports are saved in `Model tests\`. The test needs each episode's original-timeline transcript
  (`transcripts\*.timeline.json`), which is saved for episodes processed from now on.
- **Broken AI answers are retried**: if a model replies with broken or cut-off JSON, that section is asked once more
  with a "JSON only" reminder before it's marked as not checked.
- **Phone notifications** (the Phone Notifications card in Settings, or `phone_notifications:`): free pushes through
  [ntfy](https://ntfy.sh) when an episode is ready, when something is held for review, and about problems (failed
  episodes, or a feed failing 3 checks in a row). Install the ntfy app, subscribe to a topic name only you know,
  enter it, and press **Send a test**.
- **Share learned ads** (Found Ads > Learned ads): export the learned ad reads (only the words and sponsor names, no
  episode names) and import them on another PC, or from a friend with the same shows, so it starts out already
  recognising those sponsors.
- **Clean files...** (Podcasts tab): clean audio files already on your PC, without a feed. The cleaned copies go
  into an **Ad-free** folder next to them, with chapters, transcript and report; your files aren't changed.
- **Low Priority Hours** (Settings > Low Priority Hours, or `processing.low_priority_hours`): new episodes of
  **Low**-priority shows only start between the times you pick, e.g. 01:00-06:00, or 22:00-06:00 across midnight.
  Outside those hours they wait in Up next, marked "Low priority - waits for 01:00-06:00", and the app wakes up when
  the hours begin. Other shows, re-analyses and Clean files aren't affected; an episode already started finishes.

### Stuck feeds and downloads are skipped
Nothing the app downloads can hang it:
- **Feeds**: each feed check has a time limit (`download.feed_timeout_seconds`, default 60). A server that doesn't
  answer, stalls, or trickles data is cut off. That show is skipped this time ("no answer within 60 s - skipped")
  and tried again at the next check.
- **Episode downloads**: one that gets no data for `download.stall_seconds` (default 120) is cut off and retried
  at the next check.
- **Small files** (publisher chapters, transcripts, artwork, iTunes search) get 30 seconds in all.
- **Reading a feed** happens in a separate helper process. On some feeds the feed library can get stuck in a
  way that freezes the whole app (the window, every time limit, even the log). Now only the helper is stuck: it's
  stopped after 2 minutes and that feed is skipped this time. The helper is started once and reused. If it can't
  run on a PC, feeds are read in the app itself (a warning is logged); `PODCAST_CLEANER_FEED_HELPER=0` turns
  it off.
- **Hang report**: if the app still stays on the same step for 10 minutes while working
  (`download.hang_report_minutes`), it writes where each part of it is waiting into the log, under "Nothing has
  changed for 10 minutes". Send that part of the log to find the cause. It also writes **hang-report.txt** next to
  the log, which works even if the whole app is frozen.

A feed that fails 3 checks in a row is flagged in the log, and by a phone notification if set up.

### Ad history and the global sponsor list
Every removed ad is appended to **`<output_dir>\ads.csv`** (e.g. `D:\Podcasts\ads.csv`). It's permanent, covers all
podcasts, and opens in Excel. Each analysis prompt then includes:

0. **Always watch for**: sponsors you added in the Found Ads tab (or `ollama.watch_sponsors`).
1. **This show's sponsors**: the ones you listed for it, plus those found in its earlier episodes.
2. **The global sponsor list**: advertisers found on *any* of your shows (top 40 by frequency). A BetterHelp read
   learned on one show is recognised on all the others.
3. **Corrections**: anything you marked **Not an ad** in the Found Ads tab, listed as *not* ads.

`ads.csv` can safely be opened, edited or re-saved in Excel (comma or semicolon separated, UTF-8 or Windows
encoding). Rows you add by hand only need a `sponsor` column (`status` = `ad` or `not an ad`). Missing or duplicate
ids are fixed automatically, and a file the app can't read never stops an episode. Close it in Excel when done:
while it's open, new ads can't be recorded (the app won't overwrite it, and logs a warning instead).

Turn these off with `ollama.use_global_sponsors: false` / `ollama.use_ad_feedback: false`.

### Retention
Per podcast (or in `podcast_defaults`): `keep_episodes: 10` keeps the newest 10, `keep_days: 30` deletes episodes
published more than 30 days ago; both can be combined. Deleting an episode removes its MP3 and archived transcripts,
and it drops out of the feed. The newest episode is never deleted, and deleted episodes are never downloaded again.

### Other commands
```
python -m podcast_cleaner once     # one check in the console (or "Run Once (console).bat")
python -m podcast_cleaner check    # verify config, ffmpeg, Ollama, Whisper, feeds
python -m podcast_cleaner search "name"
python -m podcast_cleaner serve    # only the feed server
python -m podcast_cleaner list     # episodes you can choose (auto_download: false shows)
python -m podcast_cleaner get 3 7  # queue them for download
python -m podcast_cleaner skip 4   # skip one (never download it)
```

## Engine, web pages and database

**Two parts.** The *engine* (checking feeds, downloading, Whisper, the AI, cutting) runs in its own background
process. The window and tray talk to it, so if the window closes or hangs, cleaning keeps going, and if the engine
stops answering the window starts it again. Opening the app starts the engine automatically. To run the engine on
its own with no window: `python -m podcast_cleaner engine` (add `--console` to see the log). To go back to everything
in one process, set the environment variable `PODCAST_CLEANER_ENGINE=inprocess`.

**One set of pages, two ways to open them.** The app's window now shows the same web pages you can open on your
phone or in any browser at `http://<this PC>:8585/app/` (the feed page's address plus `/app/`). Every tab is there:
Activity, Podcasts (add, edit, search, OPML, clean files), Choose Episodes, Browse Feeds, Ad Rules, Found Ads (with
Listen, sponsors, learned-ad sharing), Stats, AI (service, computers, Test AI models), Settings and Log.
On a phone, the bottom bar has Activity, Podcasts and Found Ads, and More for the rest.
A few things only work from the PC itself: opening folders and files, Clean files, and Start with Windows.
The window needs Microsoft Edge WebView2, which Windows 10 and 11 already have. If it can't open, the older window is
used instead; to choose the older window yourself, start with `python -m podcast_cleaner tray --classic`.
Who can open the pages: this PC always; other devices on your home network when `server.token` is empty. With a token
set, use `http://<this PC>:8585/<token>/app/`. Don't forward the port to the internet without a token.
With `server.enabled: false` the engine still runs, but only this PC can reach it (no feeds, no phone pages).

**Transcribing on other computers (Transcription tab).** Whisper's settings now have their own tab, together with
**Computers running Whisper**: add other PCs running either
[speaches](https://speaches.ai) (faster-whisper with an OpenAI-style API; best with an Nvidia GPU, runs in Docker) or
whisper.cpp's `whisper-server` (AMD/Intel graphics through Vulkan, or CPU). Each episode is transcribed whole on one
computer, so with two PCs two episodes are transcribed at once; the app sends 16 kHz mono audio (about 30 MB an hour
to speaches, 115 MB to whisper.cpp) and gets the timed transcript back. For speaches, the app asks it to download the
same model as this PC's (or the one you type for that PC) the first time. A computer that fails rests for 5 minutes
and its episode goes to another one, in the end this PC. "This PC transcribes": always, when the others are busy, or
never (then only if none of the others can be reached). Pause/Resume works like the AI computers. Setup steps for both
kinds are under "How to set up another computer" in the tab. These computers only transcribe; the AI is set in the AI tab.

**One database.** Everything the app remembers is now in `podcast-cleaner.db` in your podcasts folder (SQLite):
each show's episodes, your download choices, Up next order, paused computers, files to clean, review decisions and
learned ads. The old `.json` files are imported automatically the first time and renamed to `*.json.migrated`
(delete them once you're happy). `ads.csv` stays a CSV so you can still open it in Excel.
Back it up by copying the file while the app is closed.

**Clearer config errors.** Every setting in `config.yaml` is checked when it's read, and mistakes are listed plainly,
for example `ad_removal.min_confidence: must be a number (you wrote 'high')`.

## Tests

The `tests` folder has 67 automatic checks that run the real program against stand-ins, so nothing real is
downloaded or sent anywhere:
- a pretend podcast feed and audio files, served on this PC
- a stand-in Ollama that answers with known ads, or misbehaves on purpose (broken answers, slow answers)
- a stand-in Whisper that "transcribes" instantly
- deliberately bad servers: one that never answers, one that trickles data, one that inserts different ads on a
  second download

They cover:
- downloading, cutting and the files written
- publisher chapters and learned ad reads
- Held for review, Approve & cut, and the second download
- the publisher's transcript, and what to cut (intros, gaps)
- retrying broken AI answers
- low-priority hours, Start now, and paused start
- Clean files and OPML
- stuck feeds and stalled downloads
- cut tones and their timing, and loudness
- the model test with several computers, and phone notifications
- the window: tab order, Shift-select in Up next, the AI tab, saving settings

Double-click **Run Tests.bat**, or run `.venv\Scripts\python -m pytest`. It takes about 3 minutes and ends with
"67 passed", or names what failed. `Run Tests.bat -m "not gui"` skips the window tests, since the window flashes up
briefly while they run. The audio tests need ffmpeg on the PATH and are skipped without it.

## Single .exe build
On a PC where `install.ps1` has been run:

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1                  # dist\PodcastAdCleaner\PodcastAdCleaner.exe
powershell -ExecutionPolicy Bypass -File build.ps1 -BundleFfmpeg    # also copy ffmpeg.exe/ffprobe.exe next to it
powershell -ExecutionPolicy Bypass -File build.ps1 -Folder          # folder build instead: starts much faster
```

Copy `dist\PodcastAdCleaner\` (or the `.zip` next to it) to any Windows PC and run `PodcastAdCleaner.exe`.
On first run it creates `config.yaml` next to the exe, opens it, and stays paused until you choose **Check now**.
Logs go to `logs\` beside the exe. Command-line use still works, e.g. `PodcastAdCleaner.exe check` from a terminal.

- The exe still needs **Ollama** installed on that PC; **ffmpeg** too unless you used `-BundleFfmpeg`. Whisper models download on first use.
- Expect a few hundred MB. A single-file exe unpacks itself to a temp folder at every start (10–30 s); the `-Folder` build avoids that.
- The exe transcribes on the CPU. For GPU, use the Python install with `install.ps1 -Cuda`, or place the NVIDIA cuBLAS/cuDNN DLLs on PATH.
- The exe always uses faster-whisper (`engine: openai-whisper` needs the Python install).
- Some antivirus tools flag unsigned PyInstaller executables. If that happens, use the `-Folder` build or the Python install.

## Suggestions (not yet built)

Ranked roughly by value for effort:

1. **Use a larger Ollama model** (`qwen2.5:7b` or `llama3.1:8b`) if you have 8 GB+ RAM/VRAM. A 3B model misses host-read ads and sometimes flags normal content. Change `ollama.model`, no code changes.
2. **Dry-run / review mode.** Write the report and a short audio clip of each detected ad without cutting, so you can tune `min/max_ad_seconds` and the prompt before trusting it.
3. **Battery/idle awareness**: skip heavy work when on battery or when the PC isn't idle; transcription will pin the CPU for a while.

## Troubleshooting downloads
Episode links often go through several tracking services before the real file, for example
`arttrk.com → podscribe → … → traffic.megaphone.fm/xyz.mp3`. If your network blocks tracking domains (Pi-hole,
NextDNS, router or antivirus filtering), the first one fails with "Failed to resolve". With
`download.skip_trackers: true` (default) the app goes straight to the real host and only falls back through the chain
if needed. The log shows which address worked.

## Troubleshooting "Ollama request failed ... Read timed out"
The model is loaded before the first section, answers are streamed (so `ollama.timeout_seconds` only applies when
Ollama goes completely silent), and a failed section is retried once. If a section still can't be checked, the episode
is finished anyway, but its `report.txt` lists the unchecked times and Recently finished says "ad check incomplete" -
use **Re-analyze** later. If it keeps happening: an 8B model on a CPU is slow - try `ollama.chunk_minutes: 5` (shorter
sections), `ollama.num_ctx: 4096`, a smaller model, or raise `timeout_seconds`; and avoid running other heavy programs.

## Troubleshooting "content-encoding: gzip, but failed to decode it"
Some podcast servers label their data as compressed (gzip) when it isn't, or compress it twice or incompletely. The
app now detects this and fetches the feed again uncompressed, decoding it itself; audio downloads are always requested
uncompressed. If one feed still fails with this error every time, something between your PC and the server may be
damaging the data (an antivirus "web shield" that inspects HTTPS, or a proxy/VPN). Try excluding the app from it.

## Troubleshooting "Access is denied" on .podcast-state.json (older versions)
Windows won't let a file be replaced while another program has it open (antivirus, backup or sync tools such as
OneDrive or Dropbox, or Excel). The app now retries for about 15 seconds, and if a show's file stays locked, only
that show is skipped for this check (it's listed under "errors"); the others carry on. If it keeps happening for the
same folder, exclude your podcasts folder from real-time antivirus scanning or sync.

## Notes and limitations

- Long episodes use memory: audio is held in RAM as 16 kHz samples (~230 MB per hour) during transcription.
- **Quit** while an episode is transcribing exits immediately; that episode is simply retried on the next run. **Pause** / **Stop Now** stop at the current step; the episode is redone when you resume.
- Built and tested on Linux with a simulated feed, a stub Ollama and a stand-in Whisper model (model downloads weren't possible in the test environment), plus real HTTP tests of the feed server; the Windows-specific parts (tray, notifications, installer, startup shortcut) haven't been run on Windows yet, so check the log on first use.
- Downloading episodes and removing ads is for personal use; respect each podcast's terms. Ads fund the shows you like.

## License
MIT, same as the original project.
