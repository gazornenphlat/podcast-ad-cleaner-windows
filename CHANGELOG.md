# Changelog

## Unreleased

- Documentation: fixed the `ads.csv` comment in `config.example.yaml` (ads live
  in the database now), the Python version range in the README (3.10-3.13, as
  `install.ps1` accepts), and dropped the unused `processing.prepare_ahead`
  setting (`transcribe_ahead` is the live one).
- Removed the leftover root `prompt.txt` (the live prompt is `PROMPT_TEMPLATE`
  in `analyze.py`) and the duplicate screenshots in `podcast_cleaner/web/`
  (screenshots live in `screenshots/` only now).
- The feed server now tests loopback with `ipaddress` like `api.py`, so IPv6
  loopback (`::1`) is treated the same in both layers.
- `build.ps1` now names the zip after the version (`PodcastAdCleaner-2.0.0.zip`).

## 2.0.0 — 2026-11-14

- System-tray service that watches podcast feeds, transcribes new episodes
  with Whisper, finds ads with Ollama, LM Studio or a cloud AI, and cuts them
  out with ffmpeg, serving the cleaned episodes as private RSS feeds.
- Ad detection layers several detectors (AI with real timestamps, publisher
  chapters, publisher transcripts, a second download, learned reads, sponsor
  memory, cue extension); confident ads are cut, borderline ones are held
  for review, and every episode gets a written report.
- Web window (also on your phone) plus the classic Tkinter window; cuts snap
  to sentence boundaries with a short crossfade, chapters and transcripts are
  re-timed, and the original audio is kept when something is held.
- One SQLite database (`podcast-cleaner.db`) holds shows, episodes and the ad
  history; settings stay in `config.yaml` with comments preserved.
