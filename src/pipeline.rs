//! Check feeds for new episodes and run each one through download -> transcribe -> analyze -> cut -> tag.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use anyhow::{anyhow, Result};
use chrono::{Duration as CDuration, Local};
use serde_json::{json, Value};

use crate::activity::{Activity, QueueItem};
use crate::adlog::AdLog;
use crate::analyze::{self, PromptLists};
use crate::config::{Config, Podcast};
use crate::feeds::{self, Ad, Episode, Feed};
use crate::media;
use crate::state::{s, DownloadRequests, PodcastState, QueueOrder};
use crate::transcribe;
use crate::utils::{fmt_duration, hms, now_iso, parse_iso, prefix, safe_filename, squash, Cancelled};

const ASSET_EXTENSIONS: [&str; 3] = ["vtt", "json", "txt"];
const AUDIO_EXTENSIONS: [&str; 7] = ["mp3", "m4a", "aac", "ogg", "opus", "wav", "flac"];

pub type SharedState = Arc<Mutex<PodcastState>>;

#[derive(Clone)]
pub struct Job {
    pub podcast: Podcast,
    pub feed: Arc<Feed>,
    pub episode: Episode,
    pub folder: PathBuf,
    pub state: SharedState,
    /// user asked to process an already-finished episode again
    pub reanalyze: bool,
}

impl Job {
    fn folder_name(&self) -> String {
        self.folder.file_name().unwrap_or_default().to_string_lossy().into()
    }
    fn key(&self) -> String {
        format!("{}|{}", self.folder_name(), self.episode.guid)
    }
    fn item(&self) -> QueueItem {
        QueueItem {
            podcast: self.podcast.name.clone(),
            title: self.episode.title.clone(),
            folder: self.folder_name(),
            guid: self.episode.guid.clone(),
            published: self.episode.published_iso().unwrap_or_default(),
            source: if self.reanalyze { "reanalyze" } else if self.podcast.auto_download { "auto" } else { "requested" }.into(),
        }
    }
}

#[derive(Debug, Default)]
pub struct CycleResult {
    pub processed: Vec<String>,
    pub failed: Vec<String>,
    pub errors: Vec<String>,
}

#[derive(Clone)]
pub struct Hooks {
    pub status: Arc<dyn Fn(&str) + Send + Sync>,
    pub notify: Arc<dyn Fn(&str, &str) + Send + Sync>,
    pub should_continue: Arc<dyn Fn() -> bool + Send + Sync>,
    /// True (once) if the user stopped just the current episode; it is then skipped, not retried.
    pub take_cancel: Arc<dyn Fn() -> bool + Send + Sync>,
    /// Called between episodes (after one finishes, before the next starts).
    pub job_finished: Arc<dyn Fn() + Send + Sync>,
    pub activity: Arc<Activity>,
}

impl Default for Hooks {
    fn default() -> Self {
        Self {
            status: Arc::new(|_| {}),
            notify: Arc::new(|_, _| {}),
            should_continue: Arc::new(|| true),
            take_cancel: Arc::new(|| false),
            job_finished: Arc::new(|| {}),
            activity: Arc::new(Activity::default()),
        }
    }
}

pub fn temp_root(cfg: &Config) -> PathBuf {
    let root = cfg.temp_dir.clone().unwrap_or_else(std::env::temp_dir).join("podcast-ad-cleaner");
    let _ = std::fs::create_dir_all(&root);
    root
}

/// Remove leftovers from a previous run that was killed mid-episode.
pub fn clean_temp(cfg: &Config) {
    let _ = std::fs::remove_dir_all(temp_root(cfg));
}

fn lock(st: &SharedState) -> std::sync::MutexGuard<'_, PodcastState> {
    st.lock().unwrap_or_else(|e| e.into_inner())
}

// ---------------- discovering work ----------------

pub fn find_new_episodes(cfg: &Config, agent: &ureq::Agent, hooks: &Hooks) -> (Vec<Job>, Vec<String>) {
    let mut jobs = vec![];
    let mut errors = vec![];
    let store = DownloadRequests::new(&cfg.output_dir);
    let all = store.all();
    let requests: Vec<_> = all.iter().filter(|r| r.action == "download").cloned().collect();
    let skips: Vec<_> = all.iter().filter(|r| r.action == "skip").cloned().collect();
    let reanalyses: Vec<_> = all.iter().filter(|r| r.action == "reanalyze").cloned().collect();
    let mut priority = vec![]; // re-analyses go first - you asked for them
    let mut consumed: HashSet<(String, String)> = HashSet::new();
    let mut newly_available = vec![];

    for podcast in &cfg.podcasts {
        if !podcast.enabled {
            continue;
        }
        let name = &podcast.name;
        let res = (|| -> Result<()> {
            let folder = cfg.podcast_folder(name);
            std::fs::create_dir_all(&folder)?;
            let fname = folder.file_name().unwrap().to_string_lossy().to_string();
            let state = Arc::new(Mutex::new(PodcastState::load(&folder, true)?));
            {
                let mut st = lock(&state);
                // Apply "Skip" choices from the window / tray / command line first.
                let mut any = false;
                for r in skips.iter().filter(|r| r.folder == fname) {
                    any = true;
                    if st.status(&r.guid).as_deref() != Some("done") {
                        st.mark(&r.guid, "skipped", false, json!({"skipped_by_user": true}))?;
                    }
                    consumed.insert((fname.clone(), r.guid.clone()));
                }
                if any {
                    st.save()?;
                }
            }
            (hooks.status)(&format!("Checking {name}"));
            hooks.activity.set_checking(name);

            let feed = match (|| -> Result<Feed> {
                let mut st = lock(&state);
                let url = match podcast.rss().map(String::from).or_else(|| st.feed_url()) {
                    Some(u) => u,
                    None => {
                        let term = podcast.search().unwrap_or_default();
                        let results = feeds::itunes_search(term, agent, 1)?;
                        let r = results.first().ok_or_else(|| anyhow!("No iTunes result for '{term}'"))?;
                        log::info!("{name}: resolved via iTunes to {} ({})", r.feed_url, r.name);
                        r.feed_url.clone()
                    }
                };
                if st.feed_url().as_deref() != Some(url.as_str()) {
                    st.set_feed_url(&url)?;
                }
                drop(st);
                let feed = feeds::fetch_feed(&url, agent)?;
                let mut st = lock(&state);
                st.data.insert("feed_meta".into(), json!({"title": feed.title, "author": feed.author,
                    "image": feed.image_url, "description": feed.description, "link": feed.link}));
                st.save()?;
                Ok(feed)
            })() {
                Ok(f) => Arc::new(f),
                Err(e) => {
                    log::error!("{name}: feed check failed: {e}");
                    errors.push(format!("{name}: {e}"));
                    return Ok(());
                }
            };

            let auto = podcast.auto_download;
            let mut st = lock(&state);
            let fresh: Vec<&Episode> = if st.is_new() {
                let take = (podcast.initial_episodes.max(0.0) as usize).min(feed.episodes.len());
                for ep in &feed.episodes[take..] {
                    st.mark(&ep.guid, "skipped", false, json!({"title": ep.title}))?;
                }
                st.save()?;
                log::info!("{name}: first run - newest {take} of {} episodes {}, the rest marked skipped",
                    feed.episodes.len(), if auto { "queued" } else { "offered for download" });
                feed.episodes[..take].iter().collect()
            } else {
                feed.episodes.iter().filter(|e| !st.is_done(&e.guid)).collect()
            };

            // Episodes picked in the window / tray / command line (or left half-done by a quit).
            // A download you asked for overrides "skipped"; only episodes already downloaded are left alone.
            let wanted: HashSet<String> = requests.iter().filter(|r| r.folder == fname).map(|r| r.guid.clone()).collect();
            let requested: Vec<&Episode> = feed.episodes.iter()
                .filter(|e| {
                    let stt = st.status(&e.guid);
                    (wanted.contains(&e.guid) && stt.as_deref() != Some("done")) || (stt.as_deref() == Some("queued") && !st.is_done(&e.guid))
                })
                .collect();
            for e in &requested {
                consumed.insert((fname.clone(), e.guid.clone()));
            }
            let in_feed: HashSet<&str> = feed.episodes.iter().map(|e| e.guid.as_str()).collect();
            for g in &wanted {
                if st.status(g).as_deref() == Some("done") || !in_feed.contains(g.as_str()) {
                    consumed.insert((fname.clone(), g.clone()));
                }
            }

            let mut candidates: Vec<&Episode> = if auto {
                let limit = podcast.max_retries as i64;
                fresh.iter().filter(|e| st.attempts(&e.guid) < limit).take(podcast.max_episodes_per_check.max(0.0) as usize).copied().collect()
            } else {
                let offered: Vec<&&Episode> = fresh.iter().filter(|e| st.status(&e.guid).is_none()).collect();
                for e in &offered {
                    st.mark(&e.guid, "available", false, json!({"title": e.title, "published": e.published_iso()}))?;
                }
                if !offered.is_empty() {
                    st.save()?;
                    newly_available.push(format!("{name}: {}", offered.len()));
                    log::info!("{name}: {} new episode(s) available to choose (auto_download is off)", offered.len());
                }
                vec![]
            };
            let chosen: HashSet<String> = candidates.iter().map(|e| e.guid.clone()).collect();
            candidates.extend(requested.iter().filter(|e| !chosen.contains(&e.guid)));
            candidates.sort_by(|a, b| b.published.cmp(&a.published));
            // Oldest first, so the newest episode's assets are the ones left in /assets.
            for ep in candidates.iter().rev() {
                if !auto || wanted.contains(&ep.guid) {
                    st.mark(&ep.guid, "queued", false, json!({"title": ep.title, "published": ep.published_iso()}))?;
                }
                jobs.push(Job { podcast: podcast.clone(), feed: feed.clone(), episode: (*ep).clone(), folder: folder.clone(), state: state.clone(), reanalyze: false });
            }
            st.save()?;
            if !candidates.is_empty() {
                log::info!("{name}: {} episode(s) to process", candidates.len());
            }

            // Re-analysis requests. Kept in the request file until they finish, so "Stop Now" doesn't lose them.
            let queued: HashSet<String> = candidates.iter().map(|e| e.guid.clone()).collect();
            for r in reanalyses.iter().filter(|r| r.folder == fname && !queued.contains(&r.guid)) {
                let ep = feed.episodes.iter().find(|e| e.guid == r.guid).cloned().or_else(|| episode_from_state(&st, &r.guid, &feed));
                match ep {
                    None => {
                        errors.push(format!("{name}: can't re-analyze '{}' - it's no longer in the podcast's feed",
                            if r.title.is_empty() { &r.guid } else { &r.title }));
                        consumed.insert((fname.clone(), r.guid.clone()));
                    }
                    Some(ep) => {
                        log::info!("{name}: re-analyzing '{}'", ep.title);
                        priority.push(Job { podcast: podcast.clone(), feed: feed.clone(), episode: ep, folder: folder.clone(), state: state.clone(), reanalyze: true });
                    }
                }
            }
            Ok(())
        })();
        if let Err(e) = res {
            // One show's problem (locked file, bad feed data...) must not stop the others.
            log::error!("{name}: check failed: {e:#}");
            errors.push(format!("{name}: {e}"));
        }
    }

    store.remove(&consumed);
    if !newly_available.is_empty() {
        let more = if newly_available.len() > 4 { "..." } else { "" };
        (hooks.notify)("New episodes to choose from",
            &format!("{}{more}\nOpen Podcast Cleaner > Choose episodes", newly_available[..newly_available.len().min(4)].join("; ")));
    }
    priority.extend(jobs);
    (priority, errors)
}

/// Rebuild an episode that has dropped out of the feed, from what was saved when it was processed.
fn episode_from_state(st: &PodcastState, guid: &str, feed: &Feed) -> Option<Episode> {
    let info = st.episode(guid)?;
    let audio = s(info, "audio_url");
    if audio.is_empty() {
        return None;
    }
    let title = s(info, "title");
    Some(Episode {
        guid: guid.into(),
        title: if title.is_empty() { "Untitled episode".into() } else { title.into() },
        audio_url: audio.into(),
        published: parse_iso(s(info, "published")).map(|t| t.aware().with_timezone(&chrono::Utc)),
        description: s(info, "description").into(),
        image_url: Some(s(info, "image_url").to_string()).filter(|x| !x.is_empty()).or_else(|| feed.image_url.clone()),
        ..Default::default()
    })
}

// ---------------- processing one episode ----------------

fn artwork(url: Option<&str>, agent: &ureq::Agent) -> Option<(Vec<u8>, String)> {
    let url = url.filter(|u| !u.is_empty())?;
    let res = (|| -> Result<Option<(Vec<u8>, String)>> {
        let r = feeds::http_get(agent, url, &[])?;
        let mime = r.header("Content-Type").unwrap_or("image/jpeg").split(';').next().unwrap_or("").trim().to_string();
        let data = feeds::read_all(r, 5_000_001)?;
        if !mime.starts_with("image/") || data.len() > 5_000_000 {
            return Ok(None);
        }
        Ok(Some((data, mime)))
    })();
    res.unwrap_or_else(|e| {
        log::warn!("Artwork download failed: {e}");
        None
    })
}

fn clear_assets(assets: &Path) {
    let Ok(rd) = std::fs::read_dir(assets) else { return };
    for f in rd.flatten() {
        let p = f.path();
        let ext = p.extension().map(|e| e.to_string_lossy().to_lowercase()).unwrap_or_default();
        if p.is_file() && ASSET_EXTENSIONS.contains(&ext.as_str()) {
            if let Err(e) = std::fs::remove_file(&p) {
                log::warn!("Could not delete old asset {}: {e}", p.display());
            }
        }
    }
}

/// Delete old cleaned episodes (MP3 + archived transcripts) by count and/or age.
fn apply_retention(job: &Job) -> Result<()> {
    let keep = job.podcast.keep_episodes.max(0.0) as usize;
    let keep_days = job.podcast.keep_days;
    if keep == 0 && keep_days <= 0.0 {
        return Ok(());
    }
    let mut st = lock(&job.state);
    let mut done: Vec<(String, serde_json::Map<String, Value>)> = st.episodes().iter()
        .filter_map(|(g, e)| e.as_object().map(|e| (g.clone(), e.clone())))
        .filter(|(_, e)| s(e, "status") == "done" && !s(e, "file").is_empty())
        .collect();
    let when = |e: &serde_json::Map<String, Value>| {
        let p = s(e, "published");
        if p.is_empty() { s(e, "updated").to_string() } else { p.to_string() }
    };
    done.sort_by_key(|(_, e)| std::cmp::Reverse(when(e)));
    let cutoff = (keep_days > 0.0).then(|| Local::now().fixed_offset() - CDuration::seconds((keep_days * 86400.0) as i64));
    let mut expired = vec![];
    for (i, (guid, ep)) in done.iter().enumerate() {
        if i == 0 || (job.reanalyze && guid == &job.episode.guid) {
            continue; // never delete the newest episode, or one you just asked for again
        }
        let too_many = keep > 0 && i >= keep;
        let too_old = cutoff.is_some_and(|c| parse_iso(&when(ep)).is_some_and(|t| t.aware() < c));
        if too_many || too_old {
            expired.push((guid.clone(), ep.clone()));
        }
    }
    for (guid, ep) in &expired {
        let mut targets = vec![job.folder.join(s(ep, "file"))];
        let base = s(ep, "assets_base");
        if !base.is_empty() {
            for ext in [".vtt", ".transcript.txt"] {
                targets.push(job.folder.join("transcripts").join(format!("{base}{ext}")));
            }
        }
        for p in targets {
            if p.exists() {
                match std::fs::remove_file(&p) {
                    Ok(()) => log::info!("Retention: deleted {}", p.file_name().unwrap_or_default().to_string_lossy()),
                    Err(e) => log::warn!("Retention: could not delete {}: {e}", p.display()),
                }
            }
        }
        st.mark(guid, "done", false, json!({"file": "", "deleted": true}))?;
    }
    if !expired.is_empty() {
        st.save()?;
    }
    Ok(())
}

/// Sponsor names for the AI prompt. Never fails - a problem just leaves that list out.
fn prompt_sponsors(cfg: &Config, job: &Job) -> PromptLists {
    let o = &cfg.ollama;
    let mut watch: Vec<String> = vec![];
    for x in &o.watch_sponsors {
        let x = squash(x);
        if !x.is_empty() && !watch.iter().any(|w| w == &x) {
            watch.push(x);
        }
    }
    let watched: HashSet<String> = watch.iter().map(|x| x.to_lowercase()).collect();
    let adlog = AdLog::new(&cfg.output_dir);
    // a sponsor you added on purpose overrides an earlier "not an ad"
    let not_ads: Vec<String> = if o.use_ad_feedback {
        adlog.not_ads(25).into_iter().filter(|x| !watched.contains(&x.to_lowercase())).collect()
    } else {
        vec![]
    };
    let blocked: HashSet<String> = not_ads.iter().map(|x| x.to_lowercase()).chain(watched.iter().cloned()).collect();
    let mut show: Vec<String> = job.podcast.sponsors.clone();
    if o.remember_sponsors {
        show.extend(lock(&job.state).known_sponsors(o.max_known_sponsors.max(0.0) as usize));
    }
    let mut seen = HashSet::new();
    show.retain(|x| !blocked.contains(&x.to_lowercase()) && seen.insert(x.clone()));
    let seen: HashSet<String> = show.iter().map(|x| x.to_lowercase()).collect();
    let global = if o.use_global_sponsors {
        adlog.global_sponsors(o.max_global_sponsors.max(0.0) as usize).into_iter()
            .filter(|x| !seen.contains(&x.to_lowercase()) && !blocked.contains(&x.to_lowercase()))
            .collect()
    } else {
        vec![]
    };
    PromptLists { watch, known: show, global, not_ads }
}

pub struct EpisodeOutcome {
    pub file: PathBuf,
    pub ads: usize,
    pub removed: f64,
    pub gaps: Vec<(f64, f64)>,
}

pub fn process_episode(cfg: &Config, job: &Job, agent: &ureq::Agent, ollama_ok: bool, hooks: &Hooks) -> Result<EpisodeOutcome> {
    let ep = &job.episode;
    let podcast_name = &job.podcast.name;
    let ffmpeg = media::resolve_tool(&cfg.tools.ffmpeg);
    let ffprobe = media::resolve_tool(&cfg.tools.ffprobe);
    let short = if ep.title.chars().count() <= 50 { ep.title.clone() } else { format!("{}...", prefix(&ep.title, 47)) };
    let sc = hooks.should_continue.clone();
    let step = |msg: &str| -> Result<()> {
        if !sc() {
            return Err(Cancelled.into());
        }
        (hooks.status)(&format!("{podcast_name}: {short} - {msg}"));
        hooks.activity.set_step(msg, None);
        log::info!("[{short}] {msg}");
        Ok(())
    };

    let work = tempfile::Builder::new().prefix("ep-").tempdir_in(temp_root(cfg))?;
    let started = Instant::now();

    // 1. download
    step("downloading")?;
    let ext = Path::new(ep.audio_url.split('?').next().unwrap_or(""))
        .extension().map(|e| e.to_string_lossy().to_lowercase()).unwrap_or_default();
    let original = work.path().join(format!("original.{}", if AUDIO_EXTENSIONS.contains(&ext.as_str()) { ext.as_str() } else { "mp3" }));
    feeds::download(&ep.audio_url, &original, &*sc, cfg.download.skip_trackers)?;
    let duration = media::probe_duration(&ffprobe, &original)?;
    log::info!("Duration {}", fmt_duration(duration));

    // 2. transcribe
    step(&format!("transcribing ({})", fmt_duration(duration)))?;
    let (status, activity, pn, sh) = (hooks.status.clone(), hooks.activity.clone(), podcast_name.clone(), short.clone());
    let segments = transcribe::transcribe(&original, &cfg.whisper, &ffmpeg, sc.clone(), Arc::new(move |pct| {
        status(&format!("{pn}: {sh} - transcribing {pct}%"));
        activity.set_step("transcribing", Some(pct));
    }))?;

    // 3. publisher chapters (and ads they mark)
    let publisher = feeds::load_publisher_chapters(ep, agent);
    let (pub_chapters, pub_ads) = feeds::split_ad_chapters(&publisher, duration);

    // 4. AI analysis
    let (mut ai_ads, mut ai_chapters) = (vec![], vec![]);
    let mut gaps = vec![];
    if ollama_ok && !segments.is_empty() {
        step("finding ads")?;
        let lists = prompt_sponsors(cfg, job);
        let (status, activity, pn, sh) = (hooks.status.clone(), hooks.activity.clone(), podcast_name.clone(), short.clone());
        let progress = move |m: String| {
            status(&format!("{pn}: {sh} - {m}"));
            activity.set_step(&m, None);
        };
        (ai_ads, ai_chapters) = analyze::analyze(&segments, &cfg.ollama, &*sc, &progress, &lists, &mut gaps)?;
    }
    let a = &cfg.ad_removal;
    let raw_ads: Vec<Ad> = if a.enabled {
        ai_ads.iter().cloned().chain(if a.use_rss_ad_chapters { pub_ads.clone() } else { vec![] }).collect()
    } else {
        vec![]
    };
    let ads = analyze::finalize_ads(&raw_ads, a, duration);

    // 5. cut
    step(&format!("removing {} ad(s)", ads.len()))?;
    let cleaned = work.path().join("cleaned.mp3");
    media::cut_ads(&ffmpeg, &original, &cleaned, &ads, &cfg.output.bitrate, duration, cfg.output.crossfade_ms)?;
    let clean_duration = media::probe_duration(&ffprobe, &cleaned)?;

    // 6. chapters on the cleaned timeline
    let c = &cfg.chapters;
    let feed_chapters_used = !pub_chapters.is_empty() && (c.prefer_feed_chapters || ai_chapters.is_empty());
    let source = if feed_chapters_used { &pub_chapters } else { &ai_chapters };
    let chapters = media::build_chapters(source, &ads, clean_duration, c.min_chapter_seconds, c.fallback_interval_minutes);
    let clean_segments = media::remap_segments(&segments, &ads);

    // 7. tag
    step("writing files")?;
    let date_str = ep.published.map(|d| d.with_timezone(&Local)).unwrap_or_else(Local::now).format("%Y-%m-%d").to_string();
    let art = if cfg.output.embed_artwork { artwork(ep.image_url.as_deref().or(job.feed.image_url.as_deref()), agent) } else { None };
    let removed = duration - clean_duration;
    media::tag_mp3(&cleaned, &chapters, &media::TagInfo {
        title: &ep.title, podcast: podcast_name, author: &job.feed.author, date: Some(&date_str),
        comment: &format!("Ad-free: removed {} ad(s), {}", ads.len(), fmt_duration(removed)),
        artwork: art, duration: clean_duration,
    })?;

    // 8. publish to podcast folder (only now that everything succeeded)
    let base = safe_filename(&format!("{date_str} - {}", ep.title), cfg.output.max_title_length.max(10.0) as usize);
    let assets = job.folder.join("assets");
    std::fs::create_dir_all(&assets)?;
    clear_assets(&assets);
    transcribe::write_vtt(&clean_segments, &assets.join(format!("{base}.vtt")))?;
    transcribe::write_txt(&clean_segments, &assets.join(format!("{base}.transcript.txt")), &format!("{podcast_name}\n{}\n{date_str}", ep.title))?;
    media::write_chapters_json(&assets.join(format!("{base}.chapters.json")), &chapters, &ep.title, podcast_name, &job.feed.author)?;
    let report = report(podcast_name, ep, duration, clean_duration, &ads, &chapters, &base, started.elapsed().as_secs_f64(), ollama_ok, feed_chapters_used, &gaps);
    std::fs::write(assets.join(format!("{base}.report.txt")), report)?;
    if cfg.output.archive_transcripts {
        let archive = job.folder.join("transcripts");
        std::fs::create_dir_all(&archive)?;
        for n in [format!("{base}.vtt"), format!("{base}.transcript.txt")] {
            std::fs::copy(assets.join(&n), archive.join(&n))?;
        }
    }

    let final_path = job.folder.join(format!("{base}.mp3"));
    let staged = job.folder.join(format!(".{base}.mp3.new"));
    // temp -> podcast drive (may be a different drive), then swap in
    if std::fs::rename(&cleaned, &staged).is_err() {
        std::fs::copy(&cleaned, &staged)?;
    }
    if let Err(e) = crate::utils::replace_with_retry(&staged, &final_path) {
        let _ = std::fs::remove_file(&staged);
        return Err(e.into());
    }
    let final_name = final_path.file_name().unwrap().to_string_lossy().to_string();
    {
        let mut st = lock(&job.state);
        let old_file = st.episode(&ep.guid).map(|e| s(e, "file").to_string()).unwrap_or_default();
        if job.reanalyze && !old_file.is_empty() && old_file != final_name && job.folder.join(&old_file).exists() {
            let _ = std::fs::remove_file(job.folder.join(&old_file)); // title changed since last time
        }
        // Remember sponsors whose ads were actually removed, for future episodes' prompts.
        let found: Vec<String> = ai_ads.iter().filter(|x| media::in_ad((x.start + x.end) / 2.0, &ads)).map(|x| x.label.clone()).collect();
        st.record_sponsors(&found);
    }

    // Permanent ad history for all podcasts (ads.csv) - also feeds the global sponsor list.
    let excerpts: Vec<String> = ads.iter().map(|a| {
        segments.iter().filter(|s| { let m = (s.start + s.end) / 2.0; a.start <= m && m < a.end }).map(|s| s.text.as_str()).collect::<Vec<_>>().join(" ")
    }).collect();
    if let Err(e) = AdLog::new(&cfg.output_dir).add_episode(podcast_name, &ep.title, &ep.published_iso().unwrap_or_default(), &ads, &excerpts, job.reanalyze) {
        log::warn!("Could not update ads.csv (is it open in Excel?): {e}");
    }

    let size = std::fs::metadata(&final_path).map(|m| m.len()).unwrap_or(0);
    let r1 = |x: f64| (x * 10.0).round() / 10.0;
    lock(&job.state).mark(&ep.guid, "done", true, json!({
        "title": ep.title, "file": final_name, "assets_base": base, "duration": r1(clean_duration), "size": size,
        "description": prefix(&ep.description, 4000), "published": ep.published_iso(),
        "ads_removed": ads.len(), "seconds_removed": r1(removed), "ai_used": ollama_ok, "error": "", "reanalyze_error": "",
        "audio_url": ep.audio_url, "unchecked": gaps.iter().map(|(a, b)| json!([r1(*a), r1(*b)])).collect::<Vec<_>>(),
        "image_url": ep.image_url.clone().unwrap_or_default(), "deleted": false,
        "reanalyzed": if job.reanalyze { Some(now_iso()) } else { None },
    }))?;
    apply_retention(job)?;
    Ok(EpisodeOutcome { file: final_path, ads: ads.len(), removed, gaps })
}

#[allow(clippy::too_many_arguments)]
fn report(podcast: &str, ep: &Episode, duration: f64, clean: f64, ads: &[Ad], chapters: &[feeds::Chapter], base: &str,
          elapsed: f64, ai: bool, feed_chapters: bool, gaps: &[(f64, f64)]) -> String {
    let mut l: Vec<String> = vec!["Podcast Processing Report".into(), "=".repeat(25), String::new(),
        format!("Podcast:   {podcast}"), format!("Episode:   {}", ep.title),
        match ep.published { Some(d) => format!("Published: {}", d.with_timezone(&Local).format("%Y-%m-%d %H:%M")), None => "Published: unknown".into() },
        format!("Processed: {} (took {})", Local::now().format("%Y-%m-%d %H:%M"), fmt_duration(elapsed)),
        format!("Source:    {}", ep.audio_url), String::new(),
        format!("Original duration: {}", fmt_duration(duration)),
        format!("Cleaned duration:  {}", fmt_duration(clean)),
        format!("Time removed:      {}", fmt_duration(duration - clean)), String::new()];
    if !ai {
        l.extend(["NOTE: Ollama was unavailable - only feed-marked ads were removed.".into(), String::new()]);
    }
    if !gaps.is_empty() {
        l.push("WARNING: these parts were NOT checked for ads (Ollama timed out twice), so any ads in".into());
        l.push("them are still in the episode. Use Re-analyze to try again:".into());
        l.extend(gaps.iter().map(|(a, b)| format!("  - {} - {}", hms(*a), hms(*b))));
        l.push(String::new());
    }
    l.push(format!("Advertisements removed: {}  (times are in the ORIGINAL audio)", ads.len()));
    l.extend(ads.iter().map(|a| format!("  - {} - {}  ({}) {} [{}]", hms(a.start), hms(a.end), fmt_duration(a.end - a.start), a.label, a.source)));
    l.push(String::new());
    l.push(format!("Chapters: {}  ({})", chapters.len(), if feed_chapters { "from feed" } else { "generated" }));
    l.extend(chapters.iter().map(|c| format!("  - {}  {}", hms(c.start), c.title)));
    l.extend([String::new(), "Files:".into(), format!("  {base}.mp3"), format!("  assets/{base}.vtt"),
        format!("  assets/{base}.transcript.txt"), format!("  assets/{base}.chapters.json"),
        format!("  assets/{base}.report.txt"), format!("  transcripts/{base}.vtt / .transcript.txt (archive)"), String::new()]);
    l.join("\n")
}

// ---------------- one full check ----------------

pub fn run_cycle(cfg: &Config, hooks: &Hooks) -> CycleResult {
    hooks.activity.cycle_started();
    let r = run_cycle_inner(cfg, hooks);
    hooks.activity.cycle_finished();
    r
}

fn run_cycle_inner(cfg: &Config, hooks: &Hooks) -> CycleResult {
    let mut result = CycleResult::default();
    let agent = feeds::make_session();
    for (tool, path) in [("ffmpeg", &cfg.tools.ffmpeg), ("ffprobe", &cfg.tools.ffprobe)] {
        if !media::check_tool(&media::resolve_tool(path)) {
            let msg = format!("{tool} not found ('{path}'). Install it or set tools.{tool} in config.yaml");
            log::error!("{msg}");
            (hooks.notify)("Podcast Ad Cleaner", &msg);
            result.errors.push(msg);
            return result;
        }
    }
    let (jobs, errors) = find_new_episodes(cfg, &agent, hooks);
    result.errors = errors;
    if jobs.is_empty() {
        return result;
    }
    let order = QueueOrder::new(&cfg.output_dir);
    let jobs = QueueOrder::sort(jobs, Job::key, &order.load(), |j| j.reanalyze);
    hooks.activity.set_queue(jobs.iter().map(Job::item).collect());

    let mut ollama_ok = false;
    if cfg.ad_removal.enabled {
        let (ok, msg) = analyze::check_ollama(&cfg.ollama);
        ollama_ok = ok;
        if !ok {
            log::warn!("{msg}");
            if cfg.ollama.required {
                result.errors.push(format!("{msg} - {} episode(s) postponed", jobs.len()));
                (hooks.notify)("Episodes postponed", &msg);
                return result;
            }
        }
    }

    let requests = DownloadRequests::new(&cfg.output_dir);
    let total = jobs.len();
    let mut remaining = jobs;
    let mut i = 0;
    while !remaining.is_empty() {
        if i > 0 {
            (hooks.job_finished)(); // between episodes (e.g. "Stop After This Episode")
        }
        if !(hooks.should_continue)() {
            break;
        }
        // Re-read your order before choosing each episode, so reordering Up next takes effect right away.
        remaining = QueueOrder::sort(remaining, Job::key, &order.load(), |j| j.reanalyze);
        let job = remaining.remove(0);
        i += 1;
        let label = format!("{} - {}", job.podcast.name, job.episode.title);
        let item = job.item();
        let pair = HashSet::from([(item.folder.clone(), item.guid.clone())]);
        // Skipped from the window while waiting in the queue?
        if requests.skips().iter().any(|r| r.folder == item.folder && r.guid == item.guid) {
            let done = lock(&job.state).status(&job.episode.guid).as_deref() == Some("done");
            if !(job.reanalyze && done) {
                let _ = lock(&job.state).mark(&job.episode.guid, "skipped", true, json!({"title": job.episode.title, "skipped_by_user": true}));
                log::info!("Skipped by user before processing: {label}");
            }
            requests.remove(&pair);
            hooks.activity.drop_queued(&item.folder, &item.guid);
            continue;
        }
        log::info!("=== [{i}/{total}] {label} ===");
        hooks.activity.start_job(&item);
        match process_episode(cfg, &job, &agent, ollama_ok, hooks) {
            Ok(out) => {
                if job.reanalyze {
                    requests.remove(&pair);
                }
                result.processed.push(label);
                let note = if out.gaps.is_empty() { String::new() } else {
                    format!(" - ad check incomplete: {} section(s) timed out (Re-analyze to retry)", out.gaps.len())
                };
                hooks.activity.finish_job("done", &format!("{} ad(s), {} removed{note}", out.ads, fmt_duration(out.removed)));
                (hooks.notify)(&format!("Cleaned: {}", job.podcast.name),
                    &format!("{}\n{} ad(s), {} removed", job.episode.title, out.ads, fmt_duration(out.removed)));
            }
            Err(e) if e.is::<Cancelled>() => {
                if (hooks.take_cancel)() {
                    log::info!("Stopped by user, skipping: {label}");
                    let _ = lock(&job.state).mark(&job.episode.guid, "skipped", true, json!({"title": job.episode.title, "skipped_by_user": true}));
                    hooks.activity.finish_job("stopped", "stopped by you - skipped");
                    continue;
                }
                log::info!("Cancelled: {label}");
                hooks.activity.finish_job("paused", "will resume at the next check");
                break;
            }
            Err(e) => {
                let msg = format!("{e:#}");
                log::error!("Failed: {label}: {msg}");
                if job.reanalyze {
                    requests.remove(&pair);
                }
                let mut st = lock(&job.state);
                let was_done = st.status(&job.episode.guid).as_deref() == Some("done");
                let _ = if job.reanalyze && was_done {
                    // keep the cleaned file you already have; just note what went wrong
                    st.mark(&job.episode.guid, "done", true, json!({"reanalyze_error": prefix(&msg, 500)}))
                } else {
                    st.mark(&job.episode.guid, "failed", true, json!({"title": job.episode.title, "error": prefix(&msg, 500)}))
                };
                drop(st);
                result.failed.push(label);
                hooks.activity.finish_job("failed", &prefix(&msg, 200));
                (hooks.notify)(&format!("Failed: {}", job.podcast.name), &format!("{}\n{}", job.episode.title, prefix(&msg, 120)));
            }
        }
    }
    if cfg.whisper.unload_after_cycle {
        transcribe::unload();
    }
    result
}
