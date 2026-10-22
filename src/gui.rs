//! Main window (egui) and system tray icon.
//!
//! The window runs on the main thread. The worker runs in its own thread, and the tray icon
//! in another (with its own message loop, so its menu works while the window is hidden).

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicIsize, Ordering::SeqCst};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use chrono::{Local, NaiveDateTime};
use eframe::egui::{self, Color32, RichText};
use egui_extras::{Column, TableBuilder};
use serde_json::{json, Map, Value};
use unicode_normalization::UnicodeNormalization;

use crate::adlog::{split_sponsors, AdLog};
use crate::analyze::{list_models, OllamaModel};
use crate::app::{self, desktop_notify, startup_enabled, Event, Worker};
use crate::config::{load_config, scalar_text, Config, ConfigError};
use crate::configedit::ConfigEditor;
use crate::feeds::{self, Feed, SearchResult};
use crate::server::FeedServer;
use crate::state::{num, peek_state, s, truthy, DownloadRequests, QueueOrder};
use crate::transcribe::MODELS as WHISPER_MODELS;
use crate::utils::{fmt_duration, open_path, open_url, parse_iso, prefix, safe_filename, squash};

const GREEN: Color32 = Color32::from_rgb(46, 160, 67);
const BLUE: Color32 = Color32::from_rgb(31, 111, 235);
const GREY: Color32 = Color32::from_rgb(130, 130, 130);
const RED: Color32 = Color32::from_rgb(207, 34, 46);
const AMBER: Color32 = Color32::from_rgb(154, 103, 0);
const MUTED: Color32 = Color32::from_rgb(102, 112, 133);

fn state_color(state: &str) -> Color32 {
    match state {
        "busy" => BLUE,
        "paused" => GREY,
        "error" => RED,
        _ => GREEN,
    }
}

#[allow(dead_code)]
static WINDOW_HANDLE: AtomicIsize = AtomicIsize::new(0);
static QUITTING: AtomicBool = AtomicBool::new(false);

// ---------------------------------------------------------------- icon

/// Coloured circle with a white waveform that has a gap (= ad cut out).
pub fn make_icon(state: &str, size: u32) -> Vec<u8> {
    let c = state_color(state);
    let mut px = vec![0u8; (size * size * 4) as usize];
    let sz = size as f32;
    let bars = [0.30f32, 0.55, 0.80, 0.0, 0.80, 0.55, 0.30];
    let bw = (size / 11).max(1) as f32;
    let x0 = sz / 2.0 - (bars.len() as f32 * bw * 1.5) / 2.0 + bw / 2.0;
    for y in 0..size {
        for x in 0..size {
            let (fx, fy) = (x as f32 + 0.5, y as f32 + 0.5);
            let r = sz / 2.0 - 2.0;
            let d = ((fx - sz / 2.0).powi(2) + (fy - sz / 2.0).powi(2)).sqrt();
            let i = ((y * size + x) * 4) as usize;
            if d > r + 0.5 {
                continue;
            }
            let alpha = ((r + 0.5 - d).clamp(0.0, 1.0) * 255.0) as u8;
            let mut col = [c.r(), c.g(), c.b(), alpha];
            for (n, h) in bars.iter().enumerate() {
                if *h == 0.0 {
                    continue;
                }
                let bx = x0 + n as f32 * bw * 1.5;
                let half = h * sz * 0.32;
                if fx >= bx && fx <= bx + bw && (fy - sz / 2.0).abs() <= half {
                    col = [255, 255, 255, alpha];
                }
            }
            px[i..i + 4].copy_from_slice(&col);
        }
    }
    px
}

// ---------------------------------------------------------------- small helpers

fn ago(dt: Option<NaiveDateTime>) -> String {
    let Some(dt) = dt else { return String::new() };
    let now = Local::now().naive_local();
    let secs = (now - dt).num_seconds();
    if secs < 90 {
        "just now".into()
    } else if secs < 3600 {
        format!("{} min ago", secs / 60)
    } else if dt.date() == now.date() {
        dt.format("today %H:%M").to_string()
    } else {
        dt.format("%b %d %H:%M").to_string()
    }
}

fn clock(sec: f64) -> String {
    let sec = sec as i64;
    let (h, rem) = (sec / 3600, sec % 3600);
    if h > 0 { format!("{h}:{:02}:{:02}", rem / 60, rem % 60) } else { format!("{}:{:02}", rem / 60, rem % 60) }
}

/// Alphabetical, ignoring upper/lower case and accents/curly quotes.
fn sort_key(name: &str) -> String {
    name.nfkd().filter(|c| c.is_ascii()).collect::<String>().to_lowercase().trim().to_string()
}

/// Feed durations come as seconds ("3725") or clock time ("1:02:05").
fn length(v: &str) -> String {
    let v = v.trim();
    if v.is_empty() {
        return String::new();
    }
    let mut secs = 0i64;
    for p in v.split(':') {
        let Ok(x) = p.trim().parse::<f64>() else { return String::new() };
        secs = secs * 60 + x as i64;
    }
    clock(secs as f64)
}

fn local(v: &str) -> Option<NaiveDateTime> {
    parse_iso(v).map(|t| t.local_naive())
}

fn capitalize(s: &str) -> String {
    let mut c = s.chars();
    c.next().map(|f| f.to_uppercase().collect::<String>() + c.as_str()).unwrap_or_default()
}

fn muted(ui: &mut egui::Ui, text: impl Into<String>) {
    ui.label(RichText::new(text.into()).color(MUTED));
}

// ---------------------------------------------------------------- tables

struct Row {
    id: String,
    cells: Vec<String>,
    color: Option<Color32>,
    bold: bool,
}

#[derive(Default)]
struct TableOut {
    clicked: Option<String>,
    double: Option<String>,
}

/// A sortable-looking, selectable table. `sel` holds selected row ids.
fn table(ui: &mut egui::Ui, id: &str, cols: &[(&str, f32, bool)], rows: &[Row], sel: &mut Vec<String>, multi: bool, height: f32) -> TableOut {
    let mut out = TableOut::default();
    let mods = ui.input(|i| i.modifiers);
    ui.push_id(id, |ui| {
        let mut tb = TableBuilder::new(ui).striped(true).sense(egui::Sense::click()).min_scrolled_height(0.0).max_scroll_height(height)
            .auto_shrink([false, false]).cell_layout(egui::Layout::left_to_right(egui::Align::Center));
        for (_, w, stretch) in cols {
            tb = tb.column(if *stretch { Column::remainder().at_least(*w * 0.5).clip(true) } else { Column::initial(*w).at_least(30.0).resizable(true).clip(true) });
        }
        tb.header(22.0, |mut h| {
            for (name, _, _) in cols {
                h.col(|ui| {
                    ui.strong(*name);
                });
            }
        })
        .body(|body| {
            body.rows(24.0, rows.len(), |mut r| {
                let row = &rows[r.index()];
                r.set_selected(sel.contains(&row.id));
                for c in &row.cells {
                    r.col(|ui| {
                        let mut t = RichText::new(c);
                        if let Some(col) = row.color {
                            t = t.color(col);
                        }
                        if row.bold {
                            t = t.strong();
                        }
                        ui.add(egui::Label::new(t).truncate().selectable(false));
                    });
                }
                let resp = r.response();
                if resp.clicked() {
                    out.clicked = Some(row.id.clone());
                    if multi && (mods.ctrl || mods.command) {
                        if let Some(p) = sel.iter().position(|x| x == &row.id) {
                            sel.remove(p);
                        } else {
                            sel.push(row.id.clone());
                        }
                    } else if multi && mods.shift && !sel.is_empty() {
                        let last = sel.last().cloned().unwrap();
                        let a = rows.iter().position(|x| x.id == last).unwrap_or(0);
                        let b = r.index();
                        let (lo, hi) = (a.min(b), a.max(b));
                        for x in &rows[lo..=hi] {
                            if !sel.contains(&x.id) {
                                sel.push(x.id.clone());
                            }
                        }
                    } else {
                        sel.clear();
                        sel.push(row.id.clone());
                    }
                }
                if resp.double_clicked() {
                    out.double = Some(row.id.clone());
                }
            });
        });
    });
    // forget rows that are gone
    sel.retain(|id| rows.iter().any(|r| &r.id == id));
    out
}

// ---------------------------------------------------------------- dialogs

enum Confirm {
    Reanalyze(Vec<(String, String, String)>),
    SkipChoices(Vec<(String, String, String)>),
    BrowseDownload(String, Vec<(String, String)>),
    RemovePodcasts(Vec<String>),
    StopNow,
    CheckAfterAdd,
}

enum Modal {
    Info(String, String),
    Error(String, String),
    Confirm(String, String, Confirm),
    AddSponsor(String),
}

#[derive(Default)]
struct PodcastForm {
    orig_name: Option<String>,
    search: String,
    searching: bool,
    results: Vec<SearchResult>,
    results_msg: String,
    name: String,
    rss: String,
    auto: bool,
    enabled: bool,
    initial: String,
    keep: String,
    days: String,
    sponsors: String,
    search_term: Option<String>,
    default_initial: f64,
}

enum Bg {
    Browse(String, Result<Arc<Feed>, String>),
    Models(Vec<OllamaModel>, Option<String>, String),
    Search(Result<Vec<SearchResult>, String>),
}

#[derive(PartialEq, Clone, Copy)]
enum Tab {
    Activity,
    Choose,
    Browse,
    Ads,
    Podcasts,
    AdRules,
    Settings,
    Log,
}

const AD_RULES_EXAMPLE: &str = "Treat plugs for the hosts' Patreon, live tour dates and merch as ads.\n\
Ads for other shows on the same network are ads.\n\
Do NOT treat the hosts talking about a product they personally like as an ad unless they give a promo code or URL.\n";

#[derive(Clone)]
struct RecentRow {
    id: String,
    when: String,
    podcast: String,
    title: String,
    result: String,
    detail: String,
    color: Option<Color32>,
    folder: String,
    guid: String,
}

struct BrowseRow {
    guid: String,
    title: String,
    can: bool,
    date: String,
    length: String,
    status: String,
}

// ---------------------------------------------------------------- the app

pub struct MainApp {
    worker: Arc<Worker>,
    feed_server: Arc<FeedServer>,
    cfg_path: PathBuf,
    tab: Tab,
    modal: Option<Modal>,
    podcast_form: Option<PodcastForm>,
    bg_tx: Sender<Bg>,
    bg_rx: Receiver<Bg>,
    hidden_hint_shown: bool,
    last_slow: Instant,
    has_tray: bool,

    queue_sel: Vec<String>,
    recent: Vec<RecentRow>,
    recent_sel: Vec<String>,
    choose_sel: Vec<String>,
    choose_open: HashSet<String>,
    choose_total: usize,

    browse_show: String,
    browse_feed: Option<(String, Arc<Feed>)>,
    browse_cache: HashMap<String, (Instant, Arc<Feed>)>,
    browse_checked: HashSet<String>,
    browse_q: String,
    browse_hide: bool,
    browse_msg: String,
    browse_sel: Vec<String>,

    ads_show: String,
    ads_q: String,
    ads_hide: bool,
    ads_sel: Vec<String>,
    sponsor_sel: Vec<String>,
    watch_sel: Vec<String>,

    pods_sel: Vec<String>,

    settings: HashMap<&'static str, String>,
    settings_bool: HashMap<&'static str, bool>,
    startup: bool,
    models: Vec<OllamaModel>,
    models_error: Option<String>,
    models_fetched: Option<Instant>,
    models_msg: (String, Color32),

    rules_text: String,
    rules: HashMap<&'static str, String>,

    log_lines: Vec<(String, Option<Color32>)>,
    log_pos: u64,
    log_follow: bool,
}

const TEXT_SETTINGS: [&str; 7] = ["output_dir", "check_interval_minutes", "whisper.model", "whisper.device", "ollama.url", "ollama.model", "server.token"];
const NUM_SETTINGS: [&str; 2] = ["output.crossfade_ms", "server.port"];
const BOOL_SETTINGS: [&str; 4] = ["notifications", "ad_removal.enabled", "output.archive_transcripts", "server.enabled"];
const RULE_KEYS: [&str; 3] = ["ad_removal.min_ad_seconds", "ad_removal.max_ad_seconds", "ad_removal.padding_seconds"];

fn default_setting(key: &str) -> Value {
    let d = crate::config::parse_config_text("", Path::new("x.yaml")).unwrap();
    match key {
        "output_dir" => json!("~/Podcasts (Ad-Free)"),
        "check_interval_minutes" => json!(60),
        "notifications" => json!(d.notifications),
        "whisper.model" => json!(d.whisper.model),
        "whisper.device" => json!(d.whisper.device),
        "ollama.url" => json!(d.ollama.url),
        "ollama.model" => json!(d.ollama.model),
        "server.token" => json!(""),
        "output.crossfade_ms" => json!(40),
        "server.port" => json!(8585),
        "ad_removal.enabled" => json!(true),
        "output.archive_transcripts" => json!(true),
        "server.enabled" => json!(true),
        "ad_removal.min_ad_seconds" => json!(10),
        "ad_removal.max_ad_seconds" => json!(300),
        "ad_removal.padding_seconds" => json!(0.0),
        _ => Value::Null,
    }
}

fn tidy_num(v: f64) -> Value {
    if v.fract() == 0.0 && v.abs() < 1e15 { json!(v as i64) } else { json!(v) }
}

impl MainApp {
    fn new(worker: Arc<Worker>, feed_server: Arc<FeedServer>, cfg_path: PathBuf, first_run: bool, has_tray: bool) -> Self {
        let (bg_tx, bg_rx) = channel();
        let mut app = Self {
            worker, feed_server, cfg_path, tab: if first_run { Tab::Podcasts } else { Tab::Activity },
            modal: first_run.then(|| Modal::Info("Welcome".into(),
                "Welcome to Podcast Ad Cleaner.\n\n1. Settings: choose where podcasts are saved.\n2. Podcasts: add the shows you want (RSS feed or search).\n3. Click 'Check now'.".into())),
            podcast_form: None, bg_tx, bg_rx, hidden_hint_shown: false, last_slow: Instant::now() - Duration::from_secs(60), has_tray,
            queue_sel: vec![], recent: vec![], recent_sel: vec![], choose_sel: vec![], choose_open: HashSet::new(), choose_total: 0,
            browse_show: String::new(), browse_feed: None, browse_cache: HashMap::new(), browse_checked: HashSet::new(),
            browse_q: String::new(), browse_hide: false, browse_msg: "Pick a podcast to list every episode in its feed.".into(), browse_sel: vec![],
            ads_show: "All podcasts".into(), ads_q: String::new(), ads_hide: false, ads_sel: vec![], sponsor_sel: vec![], watch_sel: vec![],
            pods_sel: vec![],
            settings: HashMap::new(), settings_bool: HashMap::new(), startup: startup_enabled(),
            models: vec![], models_error: None, models_fetched: None, models_msg: (String::new(), MUTED),
            rules_text: String::new(), rules: HashMap::new(),
            log_lines: vec![], log_pos: 0, log_follow: true,
        };
        app.load_settings();
        app.load_adrules();
        app.refresh_models();
        app
    }

    fn cfg(&self) -> Option<Config> {
        self.worker.cfg()
    }

    fn folder(&self, cfg: &Config, name: &str) -> PathBuf {
        cfg.podcast_folder(name)
    }

    fn info(&mut self, title: &str, msg: &str) {
        self.modal = Some(Modal::Info(title.into(), msg.into()));
    }

    fn error(&mut self, title: &str, msg: &str) {
        self.modal = Some(Modal::Error(title.into(), msg.into()));
    }

    /// Change config.yaml (keeping comments), validate, reload. Returns true on success.
    fn edit_config(&mut self, f: impl FnOnce(&mut ConfigEditor) -> anyhow::Result<()>) -> bool {
        let res = (|| -> anyhow::Result<()> {
            let mut ed = ConfigEditor::open(&self.cfg_path)?;
            f(&mut ed)?;
            ed.save()
        })();
        if let Err(e) = res {
            let msg = match e.downcast_ref::<ConfigError>() { Some(c) => c.to_string(), None => format!("{e:#}") };
            log::error!("Config edit failed: {msg}");
            self.error("Could not save", &msg);
            return false;
        }
        if let Some(err) = self.worker.reload_config() {
            self.error("Config problem", &err);
        }
        self.last_slow = Instant::now() - Duration::from_secs(60);
        true
    }

    // ------------------------------------------------------------ background results
    fn poll_bg(&mut self) {
        while let Ok(msg) = self.bg_rx.try_recv() {
            match msg {
                Bg::Browse(name, res) => {
                    if name != self.browse_show {
                        continue; // you picked another show meanwhile
                    }
                    match res {
                        Ok(feed) => {
                            self.browse_cache.insert(name.clone(), (Instant::now(), feed.clone()));
                            self.browse_feed = Some((name, feed));
                            self.browse_msg.clear();
                        }
                        Err(e) => self.browse_msg = format!("Couldn't load {name}: {e}"),
                    }
                }
                Bg::Models(models, err, url) => self.apply_models(models, err, &url),
                Bg::Search(res) => {
                    if let Some(f) = self.podcast_form.as_mut() {
                        f.searching = false;
                        match res {
                            Ok(r) => {
                                f.results_msg = if r.is_empty() { "No podcasts found.".into() } else { String::new() };
                                f.results = r;
                            }
                            Err(e) => {
                                f.results.clear();
                                f.results_msg = format!("Search failed: {e}");
                            }
                        }
                    }
                }
            }
        }
    }

    // ------------------------------------------------------------ header
    fn header(&mut self, ui: &mut egui::Ui) {
        let w = self.worker.clone();
        let snap = w.activity.snapshot();
        ui.horizontal(|ui| {
            let (rect, _) = ui.allocate_exact_size(egui::vec2(14.0, 14.0), egui::Sense::hover());
            ui.painter().circle_filled(rect.center(), 6.0, state_color(&w.state()));
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.add_enabled(self.feed_server.running(), egui::Button::new("Phone feeds")).clicked() {
                    open_url(&self.feed_server.base_url(true));
                }
                if ui.button("Open folder").clicked() {
                    if let Some(c) = self.cfg() {
                        let _ = std::fs::create_dir_all(&c.output_dir);
                        open_path(&c.output_dir);
                    }
                }
                if ui.button(if w.is_paused() { "Resume" } else { "Pause" }).clicked() {
                    w.toggle_pause();
                }
                if ui.add_enabled(!w.is_busy(), egui::Button::new(RichText::new("Check now").strong())).clicked() {
                    w.check_now();
                }
                ui.with_layout(egui::Layout::top_down(egui::Align::Min), |ui| {
                    let status = if let Some(c) = &snap.current {
                        format!("Working on {}", c.item.podcast)
                    } else {
                        let st = w.state();
                        let s = if st == "error" || st == "paused" || w.is_busy() { w.status() } else if w.is_paused() { "Paused".into() } else { "Idle".into() };
                        if s.chars().count() > 90 { format!("{}...", prefix(&s, 88)) } else { s }
                    };
                    ui.add(egui::Label::new(RichText::new(status).strong().size(15.0)).truncate());
                    let mut sub = vec![];
                    if let Some(n) = w.next_check() {
                        if !w.is_busy() && !w.is_paused() {
                            sub.push(format!("Next check {}", n.format("%H:%M")));
                        }
                    }
                    if let Some(l) = snap.last_check {
                        sub.push(format!("last check {}", ago(Some(l.naive_local()))));
                    }
                    if w.state() == "error" {
                        sub.push("see the Log tab for details".into());
                    }
                    muted(ui, sub.join("   "));
                });
            });
        });
    }

    // ------------------------------------------------------------ Activity tab
    fn queue_rows(&self, cfg: &Option<Config>) -> Vec<Row> {
        let snap = self.worker.activity.snapshot();
        let mut rows: Vec<(Row, bool)> = vec![];
        let mut seen = HashSet::new();
        for q in &snap.queue {
            seen.insert(q.key());
            let why = match q.source.as_str() { "auto" => "New episode", "reanalyze" => "Re-analyze", _ => "You chose it" };
            rows.push((Row { id: q.key(), cells: vec![q.podcast.clone(), q.title.clone(), prefix(&q.published, 10), why.into()], color: None, bold: false }, why == "Re-analyze"));
        }
        if let Some(cfg) = cfg {
            let names: HashMap<String, String> = cfg.podcasts.iter().map(|p| (safe_filename(&p.name, 80), p.name.clone())).collect();
            for r in DownloadRequests::new(&cfg.output_dir).all() {
                let key = format!("{}|{}", r.folder, r.guid);
                if seen.contains(&key) || r.action == "skip" {
                    continue;
                }
                let re = r.action == "reanalyze";
                let why = if re { "Re-analyze - next check" } else { "You chose it - next check" };
                rows.push((Row { id: key, cells: vec![names.get(&r.folder).cloned().unwrap_or(r.folder.clone()), r.title.clone(), String::new(), why.into()], color: Some(BLUE), bold: false }, re));
            }
            rows = QueueOrder::sort(rows, |r| r.0.id.clone(), &QueueOrder::new(&cfg.output_dir).load(), |r| r.1);
        }
        rows.into_iter().map(|r| r.0).collect()
    }

    fn refresh_recent(&mut self, cfg: &Option<Config>) {
        let snap = self.worker.activity.snapshot();
        let pending: HashSet<(String, String)> = cfg.as_ref().map(|c| DownloadRequests::new(&c.output_dir).reanalyses().into_iter().map(|r| (r.folder, r.guid)).collect()).unwrap_or_default();
        let mut rows: Vec<(Option<NaiveDateTime>, RecentRow)> = vec![];
        let mut seen: HashSet<(String, String)> = HashSet::new();
        for r in &snap.recent {
            let it = &r.item;
            seen.insert(if it.guid.is_empty() { (it.podcast.clone(), it.title.clone()) } else { (it.folder.clone(), it.guid.clone()) });
            let mut result = match r.result.as_str() { "done" => "Cleaned", "failed" => "Failed", "stopped" => "Stopped (skipped)", "paused" => "Paused", x => x }.to_string();
            if it.source == "reanalyze" && r.result == "done" {
                result = "Re-analyzed".into();
            }
            let detail = if r.result == "done" { format!("{}  ({})", r.detail, fmt_duration(r.seconds)) } else { r.detail.clone() };
            rows.push((Some(r.time.naive_local()), RecentRow { id: String::new(), when: String::new(), podcast: it.podcast.clone(), title: it.title.clone(), result, detail,
                color: (r.result == "failed").then_some(RED), folder: it.folder.clone(), guid: it.guid.clone() }));
        }
        // plus history saved in each podcast's state file (survives restarts)
        if let Some(cfg) = cfg {
            for p in &cfg.podcasts {
                let folder = self.folder(cfg, &p.name);
                if !folder.is_dir() {
                    continue;
                }
                let fname = folder.file_name().unwrap().to_string_lossy().to_string();
                let st = peek_state(&folder);
                for (guid, ep) in st.episodes() {
                    let Some(ep) = ep.as_object() else { continue };
                    let status = s(ep, "status");
                    if !(status == "done" || status == "failed") || seen.contains(&(fname.clone(), guid.clone())) || seen.contains(&(p.name.clone(), s(ep, "title").into())) {
                        continue;
                    }
                    let (mut result, detail) = if status == "done" {
                        let mut d = format!("{} ad(s), {} removed", num(ep, "ads_removed") as i64, fmt_duration(num(ep, "seconds_removed")));
                        if let Some(Value::Array(u)) = ep.get("unchecked") {
                            if !u.is_empty() {
                                d += &format!(" - ad check incomplete ({} part(s) timed out)", u.len());
                            }
                        }
                        if truthy(ep, "reanalyze_error") {
                            d += &format!(" - re-analysis failed: {}", prefix(s(ep, "reanalyze_error"), 120));
                        }
                        if truthy(ep, "deleted") {
                            d += " - since deleted";
                        }
                        ("Cleaned".to_string(), d)
                    } else {
                        ("Failed".to_string(), prefix(s(ep, "error"), 200))
                    };
                    if status == "done" && truthy(ep, "reanalyzed") {
                        result = "Re-analyzed".into();
                    }
                    let failed = status == "failed" || truthy(ep, "reanalyze_error");
                    rows.push((local(s(ep, "updated")), RecentRow { id: String::new(), when: String::new(), podcast: p.name.clone(), title: s(ep, "title").into(), result, detail,
                        color: failed.then_some(RED), folder: fname.clone(), guid: guid.clone() }));
                }
            }
        }
        rows.sort_by(|a, b| b.0.cmp(&a.0));
        let mut out = vec![];
        let mut ids = HashSet::new();
        for (i, (when, mut r)) in rows.into_iter().take(80).enumerate() {
            r.id = if r.guid.is_empty() { format!("r{i}") } else { format!("r|{}|{}", r.folder, r.guid) };
            if !ids.insert(r.id.clone()) {
                continue;
            }
            if pending.contains(&(r.folder.clone(), r.guid.clone())) {
                r.result = "Re-analysis queued".into();
                r.color = Some(BLUE);
            }
            r.when = ago(when);
            out.push(r);
        }
        self.recent = out;
    }

    fn move_queued(&mut self, rows: &[Row], to: &str) {
        if self.queue_sel.is_empty() {
            return self.info("Up next", "Select one or more episodes to move first.");
        }
        let mut ids: Vec<String> = rows.iter().map(|r| r.id.clone()).collect();
        let chosen: HashSet<String> = self.queue_sel.iter().cloned().collect();
        let sel: Vec<String> = ids.iter().filter(|i| chosen.contains(*i)).cloned().collect();
        match to {
            "top" => {
                ids.retain(|i| !chosen.contains(i));
                ids.splice(0..0, sel.clone());
            }
            "bottom" => {
                ids.retain(|i| !chosen.contains(i));
                ids.extend(sel.clone());
            }
            "up" => {
                for i in 1..ids.len() {
                    if chosen.contains(&ids[i]) && !chosen.contains(&ids[i - 1]) {
                        ids.swap(i, i - 1);
                    }
                }
            }
            _ => {
                for i in (0..ids.len().saturating_sub(1)).rev() {
                    if chosen.contains(&ids[i]) && !chosen.contains(&ids[i + 1]) {
                        ids.swap(i, i + 1);
                    }
                }
            }
        }
        self.worker.set_queue_order(&ids);
    }

    fn activity_tab(&mut self, ui: &mut egui::Ui) {
        let w = self.worker.clone();
        let snap = w.activity.snapshot();
        let cfg = self.cfg();
        egui::Frame::group(ui.style()).show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.horizontal(|ui| {
                ui.vertical(|ui| {
                    let (title, show) = match (&snap.current, snap.checking.is_empty()) {
                        (Some(c), _) => (c.item.title.clone(), c.item.podcast.clone()),
                        (None, false) => ("Checking feeds".into(), snap.checking.clone()),
                        _ => (if w.is_paused() { "Paused" } else { "Idle" }.into(), String::new()),
                    };
                    ui.add(egui::Label::new(RichText::new(title).strong().size(16.0)).truncate());
                    muted(ui, show);
                });
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Min), |ui| {
                    let has = snap.current.is_some();
                    if ui.add_enabled(has, egui::Button::new("Stop Now")).clicked() {
                        if let Some(c) = &snap.current {
                            self.modal = Some(Modal::Confirm("Stop Now".into(), format!("Stop working on:\n\n{}\n\nThe work done on it so far is discarded; it starts over when you click Resume (or at the next check after you resume). Processing pauses until then.", c.item.title), Confirm::StopNow));
                        }
                    }
                    let label = if w.stop_after_current.load(SeqCst) && has { "Keep Going" } else { "Stop After This Episode" };
                    if ui.add_enabled(has, egui::Button::new(label)).clicked() {
                        w.toggle_stop_after();
                    }
                });
            });
            match &snap.current {
                Some(c) => {
                    let elapsed = fmt_duration(c.started.elapsed().as_secs_f64());
                    let mut text = match c.pct {
                        Some(p) => {
                            ui.add(egui::ProgressBar::new(p as f32 / 100.0));
                            format!("{} {p}%   -   {elapsed} so far", capitalize(&c.step))
                        }
                        None => {
                            ui.add(egui::ProgressBar::new(0.3).animate(true));
                            format!("{}   -   {elapsed} so far", capitalize(&c.step))
                        }
                    };
                    if w.stop_after_current.load(SeqCst) {
                        text += "   -   will pause when this episode is done";
                    }
                    ui.label(text);
                }
                None if !snap.checking.is_empty() => {
                    ui.add(egui::ProgressBar::new(0.3).animate(true));
                }
                None => {
                    ui.add(egui::ProgressBar::new(0.0));
                }
            }
        });
        ui.add_space(8.0);
        let half = ((ui.available_height() - 90.0) / 2.0).max(120.0);

        ui.strong("Up next");
        let rows = self.queue_rows(&cfg);
        let mut sel = std::mem::take(&mut self.queue_sel);
        table(ui, "queue", &[("Podcast", 200.0, false), ("Episode", 400.0, true), ("Published", 100.0, false), ("Why", 180.0, false)], &rows, &mut sel, true, half - 40.0);
        self.queue_sel = sel;
        ui.horizontal(|ui| {
            for (label, to) in [("Top", "top"), ("Up", "up"), ("Down", "down"), ("Bottom", "bottom")] {
                if ui.button(label).clicked() {
                    self.move_queued(&rows, to);
                }
            }
            ui.add_space(8.0);
            if ui.button("Skip selected").clicked() {
                if self.queue_sel.is_empty() {
                    self.info("Up next", "Select episodes to skip first.");
                } else {
                    for id in self.queue_sel.clone() {
                        if let Some((folder, guid)) = id.split_once('|') {
                            let title = rows.iter().find(|r| r.id == id).map(|r| r.cells[1].clone()).unwrap_or_default();
                            w.skip_episode(folder, guid, &title);
                            w.activity.drop_queued(folder, guid);
                        }
                    }
                }
            }
            muted(ui, if rows.is_empty() { "Nothing waiting".to_string() } else { format!("{} waiting", rows.len()) });
        });
        ui.add_space(8.0);
        ui.strong("Recently finished");
        let rrows: Vec<Row> = self.recent.iter().map(|r| Row { id: r.id.clone(), cells: vec![r.when.clone(), r.podcast.clone(), r.title.clone(), r.result.clone(), r.detail.clone()], color: r.color, bold: false }).collect();
        let mut sel = std::mem::take(&mut self.recent_sel);
        let out = table(ui, "recent", &[("When", 110.0, false), ("Podcast", 180.0, false), ("Episode", 340.0, true), ("Result", 140.0, false), ("Details", 260.0, false)], &rrows, &mut sel, true, half - 40.0);
        self.recent_sel = sel;
        if out.double.is_some() {
            self.open_recent();
        }
        ui.horizontal(|ui| {
            if ui.button("Re-analyze").clicked() {
                let picked: Vec<(String, String, String)> = self.recent.iter().filter(|r| self.recent_sel.contains(&r.id) && !r.guid.is_empty())
                    .map(|r| (r.folder.clone(), r.guid.clone(), r.title.clone())).collect();
                if picked.is_empty() {
                    self.info("Re-analyze", "Select one or more finished episodes first.");
                } else {
                    let what = if picked.len() == 1 { picked[0].2.clone() } else { format!("{} episodes", picked.len()) };
                    self.modal = Some(Modal::Confirm("Re-analyze".into(), format!("Re-analyze {what}?\n\nThe original is downloaded again, transcribed, and its ads re-detected using your current Whisper model, Ollama model and ad rules. The cleaned file is replaced when it's done; if something goes wrong, the current one is kept."), Confirm::Reanalyze(picked)));
                }
            }
            if ui.button("Open folder").clicked() {
                self.open_recent();
            }
            muted(ui, "Re-analyze downloads the episode again and redoes the ads with your current model and ad rules.");
        });
    }

    fn open_recent(&mut self) {
        let Some(cfg) = self.cfg() else { return };
        if let Some(r) = self.recent.iter().find(|r| self.recent_sel.first() == Some(&r.id)) {
            let f = self.folder(&cfg, &r.podcast);
            if f.exists() {
                open_path(&f);
            }
        }
    }

    // ------------------------------------------------------------ Choose tab
    fn choose_data(&self, cfg: &Config) -> Vec<(String, String, Vec<(String, Map<String, Value>)>, HashMap<(String, String), String>)> {
        let choice: HashMap<(String, String), String> = DownloadRequests::new(&cfg.output_dir).all().into_iter().map(|r| ((r.folder, r.guid), r.action)).collect();
        cfg.podcasts.iter().filter(|p| p.enabled && !p.auto_download).map(|p| {
            let folder = self.folder(cfg, &p.name);
            let fname = folder.file_name().unwrap().to_string_lossy().to_string();
            let eps = if folder.is_dir() { peek_state(&folder).choosable(50) } else { vec![] };
            let eps = eps.into_iter().filter(|(g, _)| choice.get(&(fname.clone(), g.clone())).map(String::as_str) != Some("skip")).collect();
            (p.name.clone(), fname, eps, choice.clone())
        }).collect()
    }

    fn choose_tab(&mut self, ui: &mut egui::Ui) {
        let Some(cfg) = self.cfg() else { return muted(ui, "Config not loaded.") };
        let data = self.choose_data(&cfg);
        muted(ui, if data.is_empty() {
            "No podcasts are set to 'You choose'. In the Podcasts tab, select a show and click 'Auto / Choose' - its new episodes will then wait here for you."
        } else {
            "Select episodes (or a whole show) and click Download or Skip. Episodes you don't decide on stay here. Ctrl/Shift-click to pick several."
        });
        let mut rows = vec![];
        let mut total = 0;
        for (name, folder, eps, choice) in &data {
            let sid = format!("show|{folder}");
            let undecided = eps.iter().filter(|(g, e)| choice.get(&(folder.clone(), g.clone())).map(String::as_str) != Some("download") && s(e, "status") != "queued").count();
            total += undecided;
            let open = self.choose_open.contains(&sid) || self.choose_open.is_empty();
            let label = if undecided > 0 { format!("{undecided} new") } else if eps.is_empty() { "nothing new".into() } else { String::new() };
            rows.push(Row { id: sid, cells: vec![if open { "▾" } else { "▸" }.into(), String::new(), name.clone(), label], color: None, bold: true });
            if !open {
                continue;
            }
            for (g, e) in eps {
                let wanted = choice.get(&(folder.clone(), g.clone())).map(String::as_str) == Some("download") || s(e, "status") == "queued";
                let failed = s(e, "status") == "failed";
                let status = if wanted { "Queued for download".to_string() } else if failed { format!("Failed - {}", prefix(s(e, "error"), 60)) } else { "New".into() };
                rows.push(Row { id: format!("ep|{folder}|{g}"), cells: vec![String::new(), prefix(s(e, "published"), 10), s(e, "title").into(), status],
                    color: if wanted { Some(BLUE) } else if failed { Some(RED) } else { None }, bold: false });
            }
        }
        self.choose_total = total;
        let mut sel = std::mem::take(&mut self.choose_sel);
        let h = ui.available_height() - 40.0;
        let out = table(ui, "choose", &[("", 24.0, false), ("Published", 100.0, false), ("Episode", 520.0, true), ("Status", 220.0, false)], &rows, &mut sel, true, h);
        self.choose_sel = sel;
        if let Some(id) = &out.clicked {
            if id.starts_with("show|") && ui.input(|i| !i.modifiers.ctrl && !i.modifiers.shift) {
                if self.choose_open.is_empty() {
                    self.choose_open = rows.iter().filter(|r| r.id.starts_with("show|")).map(|r| r.id.clone()).collect();
                }
                if !self.choose_open.remove(id) {
                    self.choose_open.insert(id.clone());
                }
                self.choose_open.insert("_".into());
            }
        }
        if let Some(id) = &out.double {
            if id.starts_with("ep|") {
                self.choose_action(&rows, "download");
            }
        }
        ui.horizontal(|ui| {
            if ui.button(RichText::new("Download").strong()).clicked() {
                self.choose_action(&rows, "download");
            }
            if ui.button("Skip").clicked() {
                self.choose_action(&rows, "skip");
            }
            muted(ui, "Tip: select a show to act on all its episodes.");
        });
    }

    fn choose_action(&mut self, rows: &[Row], action: &str) {
        let Some(cfg) = self.cfg() else { return };
        let data = self.choose_data(&cfg);
        let mut picked: Vec<(String, String, String)> = vec![];
        for id in &self.choose_sel {
            if let Some(folder) = id.strip_prefix("show|") {
                if let Some((_, _, eps, _)) = data.iter().find(|d| d.1 == folder) {
                    picked.extend(eps.iter().map(|(g, e)| (folder.to_string(), g.clone(), s(e, "title").to_string())));
                }
            } else if let Some(rest) = id.strip_prefix("ep|") {
                if let Some((folder, guid)) = rest.split_once('|') {
                    let title = rows.iter().find(|r| &r.id == id).map(|r| r.cells[2].clone()).unwrap_or_default();
                    picked.push((folder.into(), guid.into(), title));
                }
            }
        }
        let mut seen = HashSet::new();
        picked.retain(|p| seen.insert((p.0.clone(), p.1.clone())));
        if picked.is_empty() {
            return self.info("Choose episodes", "Select one or more episodes (or a show) first.");
        }
        if action == "skip" && picked.len() > 3 {
            self.modal = Some(Modal::Confirm("Skip episodes".into(), format!("Skip {} episodes? They won't be downloaded.", picked.len()), Confirm::SkipChoices(picked)));
            return;
        }
        for (f, g, t) in picked {
            if action == "skip" { self.worker.skip_episode(&f, &g, &t) } else { self.worker.request_episode(&f, &g, &t) }
        }
    }

    // ------------------------------------------------------------ Browse feed tab
    fn load_browse(&mut self, force: bool) {
        let Some(cfg) = self.cfg() else { return };
        let name = self.browse_show.clone();
        let Some(p) = cfg.podcasts.iter().find(|p| p.name == name).cloned() else { return };
        if self.browse_feed.as_ref().map(|f| &f.0) != Some(&name) {
            self.browse_checked.clear();
            self.browse_feed = None;
        }
        if let Some((t, f)) = self.browse_cache.get(&name) {
            if !force && t.elapsed() < Duration::from_secs(600) {
                self.browse_feed = Some((name, f.clone()));
                return;
            }
        }
        let url = p.rss().map(String::from).or_else(|| peek_state(&self.folder(&cfg, &name)).feed_url());
        let search = p.search().map(String::from);
        self.browse_msg = format!("Loading {name}...");
        self.browse_feed = None;
        let tx = self.bg_tx.clone();
        std::thread::spawn(move || {
            let res = (|| -> anyhow::Result<Arc<Feed>> {
                let agent = feeds::make_session();
                let u = match (url, search) {
                    (Some(u), _) => u,
                    (None, Some(term)) => feeds::itunes_search(&term, &agent, 1)?.first().map(|r| r.feed_url.clone()).ok_or_else(|| anyhow::anyhow!("no RSS feed address for this podcast"))?,
                    _ => anyhow::bail!("no RSS feed address for this podcast"),
                };
                Ok(Arc::new(feeds::fetch_feed(&u, &agent)?))
            })();
            let _ = tx.send(Bg::Browse(name, res.map_err(|e| prefix(&e.to_string(), 200))));
        });
    }

    fn browse_status(info: Option<&Map<String, Value>>, req: Option<&str>) -> (String, bool) {
        let st = info.map(|i| s(i, "status")).unwrap_or("");
        if st == "done" {
            return (if info.is_some_and(|i| truthy(i, "deleted")) { "Downloaded (since deleted)" } else { "Downloaded" }.into(), false);
        }
        if req == Some("download") || st == "queued" {
            return ("Queued for download".into(), false);
        }
        match st {
            "failed" => ("Failed - tick to retry".into(), true),
            "skipped" => (if info.is_some_and(|i| truthy(i, "skipped_by_user")) { "Skipped by you" } else { "Not downloaded" }.into(), true),
            "available" => ("New - not downloaded".into(), true),
            _ => ("Not downloaded".into(), true),
        }
    }

    fn browse_rows(&mut self, cfg: &Config) -> (Vec<BrowseRow>, usize, usize) {
        let Some((name, feed)) = self.browse_feed.clone() else { return (vec![], 0, 0) };
        let folder = self.folder(cfg, &name);
        let fname = folder.file_name().unwrap().to_string_lossy().to_string();
        let state = peek_state(&folder);
        let choice: HashMap<String, String> = DownloadRequests::new(&cfg.output_dir).all().into_iter().filter(|r| r.folder == fname).map(|r| (r.guid, r.action)).collect();
        let q = self.browse_q.trim().to_lowercase();
        let (mut out, mut downloaded) = (vec![], 0);
        for ep in &feed.episodes {
            let (status, can) = Self::browse_status(state.episode(&ep.guid), choice.get(&ep.guid).map(String::as_str));
            if !can {
                self.browse_checked.remove(&ep.guid);
            }
            if status.starts_with("Downloaded") {
                downloaded += 1;
                if self.browse_hide {
                    continue;
                }
            }
            if !q.is_empty() && !ep.title.to_lowercase().contains(&q) {
                continue;
            }
            out.push(BrowseRow {
                guid: ep.guid.clone(), title: ep.title.clone(), can,
                date: ep.published.map(|d| d.with_timezone(&Local).format("%Y-%m-%d").to_string()).unwrap_or_default(),
                length: length(&ep.duration), status,
            });
        }
        (out, feed.episodes.len(), downloaded)
    }

    fn browse_tab(&mut self, ui: &mut egui::Ui) {
        let Some(cfg) = self.cfg() else { return muted(ui, "Config not loaded.") };
        let mut names: Vec<String> = cfg.podcasts.iter().map(|p| p.name.clone()).collect();
        names.sort_by_key(|n| sort_key(n));
        ui.horizontal(|ui| {
            ui.label("Podcast");
            let before = self.browse_show.clone();
            egui::ComboBox::from_id_salt("browse_show").width(280.0).selected_text(&self.browse_show).show_ui(ui, |ui| {
                for n in &names {
                    ui.selectable_value(&mut self.browse_show, n.clone(), n);
                }
            });
            if self.browse_show != before {
                self.load_browse(false);
            }
            if ui.button("↻ Refresh").clicked() {
                self.load_browse(true);
            }
            ui.add_space(12.0);
            ui.label("Search");
            ui.add(egui::TextEdit::singleline(&mut self.browse_q).desired_width(180.0));
            ui.checkbox(&mut self.browse_hide, "Hide downloaded");
        });
        let (rows, total, downloaded) = self.browse_rows(&cfg);
        if self.browse_feed.is_some() {
            let shown = rows.len();
            muted(ui, format!("{}: {total} episode{} in the feed, {downloaded} downloaded{}", self.browse_show, if total != 1 { "s" } else { "" },
                if shown != total { format!(" - showing {shown}") } else { String::new() }));
        } else {
            muted(ui, self.browse_msg.clone());
        }
        let trows: Vec<Row> = rows.iter().map(|r| {
            let ticked = self.browse_checked.contains(&r.guid);
            Row { id: r.guid.clone(), cells: vec![if ticked { "☑" } else if r.can { "☐" } else { "" }.into(), r.date.clone(), r.title.clone(), r.length.clone(), r.status.clone()],
                color: if ticked { Some(BLUE) } else if !r.can { Some(MUTED) } else { None }, bold: false }
        }).collect();
        let mut sel = std::mem::take(&mut self.browse_sel);
        let h = ui.available_height() - 40.0;
        let out = table(ui, "browse", &[("Get", 34.0, false), ("Published", 96.0, false), ("Episode", 520.0, true), ("Length", 80.0, false), ("Status", 180.0, false)], &trows, &mut sel, false, h);
        self.browse_sel = sel;
        if let Some(id) = out.clicked {
            if rows.iter().any(|r| r.guid == id && r.can) && !self.browse_checked.remove(&id) {
                self.browse_checked.insert(id);
            }
        }
        ui.horizontal(|ui| {
            let n = self.browse_checked.len();
            if ui.add_enabled(n > 0, egui::Button::new(RichText::new(if n > 0 { format!("Download selected ({n})") } else { "Download selected".into() }).strong())).clicked() {
                self.submit_browse();
            }
            if ui.button("Select all shown").clicked() {
                for r in rows.iter().filter(|r| r.can) {
                    self.browse_checked.insert(r.guid.clone());
                }
            }
            if ui.button("Clear selection").clicked() {
                self.browse_checked.clear();
            }
            muted(ui, "Click an episode to tick it. To redo a downloaded one, use Re-analyze (Activity tab).");
        });
    }

    fn submit_browse(&mut self) {
        let Some((name, feed)) = self.browse_feed.clone() else { return };
        let picked: Vec<(String, String)> = feed.episodes.iter().filter(|e| self.browse_checked.contains(&e.guid)).map(|e| (e.guid.clone(), e.title.clone())).collect();
        if picked.len() > 1 {
            self.modal = Some(Modal::Confirm("Download".into(), format!("Download {} episodes of {name}?\n\nEach one is downloaded, transcribed and cleaned in turn - long episodes can take a while each.", picked.len()), Confirm::BrowseDownload(name, picked)));
        } else {
            self.do_browse_download(&name, picked);
        }
    }

    fn do_browse_download(&mut self, name: &str, picked: Vec<(String, String)>) {
        let Some(cfg) = self.cfg() else { return };
        let folder = self.folder(&cfg, name);
        let _ = std::fs::create_dir_all(&folder);
        let fname = folder.file_name().unwrap().to_string_lossy().to_string();
        for (g, t) in picked {
            self.worker.request_episode(&fname, &g, &t);
        }
        self.browse_checked.clear();
    }

    // ------------------------------------------------------------ Ads tab
    fn ads_tab(&mut self, ui: &mut egui::Ui) {
        let Some(cfg) = self.cfg() else { return muted(ui, "Config not loaded.") };
        let log = AdLog::new(&cfg.output_dir);
        let rows = log.rows();
        let mut shows: Vec<String> = rows.iter().map(|r| r.get("podcast").to_string()).collect::<HashSet<_>>().into_iter().collect();
        shows.sort();
        ui.horizontal(|ui| {
            ui.label("Podcast");
            egui::ComboBox::from_id_salt("ads_show").width(220.0).selected_text(&self.ads_show).show_ui(ui, |ui| {
                ui.selectable_value(&mut self.ads_show, "All podcasts".into(), "All podcasts");
                for sh in &shows {
                    ui.selectable_value(&mut self.ads_show, sh.clone(), sh);
                }
            });
            ui.label("Search");
            ui.add(egui::TextEdit::singleline(&mut self.ads_q).desired_width(180.0));
            ui.checkbox(&mut self.ads_hide, "Hide 'not an ad'");
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.button("Open ads.csv").clicked() {
                    if log.path.exists() { open_path(&log.path) } else { self.info("Ads", "No ads recorded yet.") }
                }
            });
        });
        let q = self.ads_q.trim().to_lowercase();
        let mut sel: Vec<&crate::adlog::AdRow> = rows.iter().filter(|r| {
            (self.ads_show == "All podcasts" || r.get("podcast") == self.ads_show) && (!self.ads_hide || !r.is_not_ad())
                && (q.is_empty() || format!("{} {} {}", r.get("sponsor"), r.get("episode"), r.get("excerpt")).to_lowercase().contains(&q))
        }).collect();
        sel.sort_by(|a, b| (b.get("processed"), b.start).partial_cmp(&(a.get("processed"), a.start)).unwrap_or(std::cmp::Ordering::Equal));
        let real: Vec<_> = sel.iter().filter(|r| !r.is_not_ad()).collect();
        let total: f64 = real.iter().map(|r| r.seconds).sum();
        let eps = real.iter().map(|r| (r.get("podcast"), r.get("episode"))).collect::<HashSet<_>>().len();
        let (h, m) = ((total / 60.0) as i64 / 60, (total / 60.0) as i64 % 60);
        ui.label(RichText::new(if rows.is_empty() {
            "No ads recorded yet. Every ad cut from now on is listed here (and in ads.csv in your podcasts folder).".to_string()
        } else {
            format!("{} ads removed from {eps} episode{} - {}{m}m of ads", real.len(), if eps != 1 { "s" } else { "" }, if h > 0 { format!("{h}h ") } else { String::new() })
        }).strong());

        let avail = ui.available_size();
        ui.horizontal_top(|ui| {
            ui.allocate_ui(egui::vec2(avail.x * 0.68, avail.y), |ui| {
                ui.vertical(|ui| {
                    let trows: Vec<Row> = sel.iter().take(2000).map(|r| {
                        let by = match r.get("found_by") { "ai" => "AI".into(), "feed" => "Feed".into(), x => x.replace('+', " + ").replace("ai", "AI").replace("feed", "Feed") };
                        let sponsor = format!("{}{}", r.get("sponsor"), if r.is_not_ad() { "  (not an ad)" } else { "" });
                        Row { id: r.get("id").into(), cells: vec![local(r.get("processed")).map(|d| d.format("%b %d").to_string()).unwrap_or_default(), r.get("podcast").into(), r.get("episode").into(),
                            format!("{} - {}", clock(r.start), clock(r.end)), fmt_duration(r.seconds), sponsor, by], color: r.is_not_ad().then_some(AMBER), bold: false }
                    }).collect();
                    let mut s_ = std::mem::take(&mut self.ads_sel);
                    table(ui, "ads", &[("Date", 60.0, false), ("Podcast", 120.0, false), ("Episode", 150.0, false), ("At", 100.0, false), ("Length", 64.0, false), ("Sponsor", 140.0, true), ("Found by", 64.0, false)], &trows, &mut s_, true, avail.y - 150.0);
                    self.ads_sel = s_;
                    let excerpt = self.ads_sel.first().and_then(|id| sel.iter().find(|r| r.get("id") == id)).map(|r| {
                        let e = r.get("excerpt");
                        format!("What was said: \u{201c}{}\u{201d}", if e.is_empty() { "(no transcript)" } else { e })
                    }).unwrap_or_default();
                    egui::ScrollArea::vertical().id_salt("excerpt").max_height(60.0).show(ui, |ui| { ui.label(excerpt); });
                    ui.horizontal(|ui| {
                        if ui.button("Not an ad").clicked() {
                            self.mark_ads(&log, "not_ad");
                        }
                        if ui.button("It is an ad").clicked() {
                            self.mark_ads(&log, "ad");
                        }
                        muted(ui, "Teaches future checks; audio already cut stays cut.");
                    });
                });
            });
            ui.separator();
            ui.vertical(|ui| {
                ui.strong("Sponsors found (used in the AI prompt)");
                let mut stats = log.sponsor_stats();
                if self.ads_show != "All podcasts" {
                    let names: HashSet<String> = sel.iter().flat_map(|r| split_sponsors(r.get("sponsor"))).map(|n| n.to_lowercase()).collect();
                    stats.retain(|k, _| names.contains(k));
                }
                let mut st: Vec<_> = stats.into_iter().collect();
                st.sort_by(|a, b| (b.1.ads, &b.1.last).cmp(&(a.1.ads, &a.1.last)));
                let srows: Vec<Row> = st.iter().map(|(k, v)| {
                    let notad = v.not_ads >= v.ads && v.not_ads > 0;
                    Row { id: k.clone(), cells: vec![format!("{}{}", v.name, if notad { "  (not an ad)" } else { "" }), v.ads.to_string(), v.shows.len().to_string(), fmt_duration(v.seconds)], color: notad.then_some(AMBER), bold: false }
                }).collect();
                let mut s_ = std::mem::take(&mut self.sponsor_sel);
                let out = table(ui, "sponsors", &[("Sponsor", 120.0, true), ("Ads", 40.0, false), ("Shows", 48.0, false), ("Time", 70.0, false)], &srows, &mut s_, false, (avail.y - 300.0).max(100.0));
                self.sponsor_sel = s_;
                if let Some(id) = out.double {
                    if let Some((_, v)) = st.iter().find(|(k, _)| *k == id) {
                        self.ads_q = v.name.clone();
                    }
                }
                muted(ui, "Named in every prompt so they're recognised on all shows. 'Not an ad' ones are listed as NOT ads. Double-click to filter.");
                ui.add_space(8.0);
                ui.strong("Always watch for (every show)");
                let watch = cfg.ollama.watch_sponsors.clone();
                let wrows: Vec<Row> = watch.iter().enumerate().map(|(i, w)| Row { id: i.to_string(), cells: vec![w.clone()], color: None, bold: false }).collect();
                let mut s_ = std::mem::take(&mut self.watch_sel);
                table(ui, "watch", &[("Sponsor", 200.0, true)], &wrows, &mut s_, true, 110.0);
                self.watch_sel = s_;
                ui.horizontal(|ui| {
                    if ui.button("Add sponsor...").clicked() {
                        self.modal = Some(Modal::AddSponsor(String::new()));
                    }
                    if ui.button("Remove").clicked() {
                        if self.watch_sel.is_empty() {
                            self.info("Always watch for", "Select a sponsor to remove first.");
                        } else {
                            let keep: Vec<Value> = watch.iter().enumerate().filter(|(i, _)| !self.watch_sel.contains(&i.to_string())).map(|(_, w)| json!(w)).collect();
                            self.watch_sel.clear();
                            self.edit_config(|ed| ed.set("ollama.watch_sponsors", Value::Array(keep)));
                        }
                    }
                });
                muted(ui, "Named in every show's prompt, even if the AI has never found them.");
            });
        });
    }

    fn add_watch(&mut self, text: &str) {
        let Some(cfg) = self.cfg() else { return };
        let current = cfg.ollama.watch_sponsors.clone();
        let mut have: HashSet<String> = current.iter().map(|x| x.to_lowercase()).collect();
        let mut new = vec![];
        for part in text.split(',') {
            let name = squash(part).trim_matches([' ', '.', ';', ':']).to_string();
            if !name.is_empty() && name.chars().count() <= 60 && have.insert(name.to_lowercase()) {
                new.push(name);
            }
        }
        if new.is_empty() {
            return self.info("Add sponsor", "Already in the list.");
        }
        let all: Vec<Value> = current.into_iter().chain(new).map(Value::String).collect();
        self.edit_config(|ed| ed.set("ollama.watch_sponsors", Value::Array(all)));
    }

    fn mark_ads(&mut self, log: &AdLog, status: &str) {
        if self.ads_sel.is_empty() {
            return self.info("Ads", "Select one or more ads first.");
        }
        if let Err(e) = log.set_status(&self.ads_sel.iter().cloned().collect(), status) {
            self.error("Ads", &format!("Couldn't save the change - is ads.csv open in Excel? Close it and try again.\n\n{e}"));
        }
    }

    // ------------------------------------------------------------ Podcasts tab
    fn podcasts_tab(&mut self, ui: &mut egui::Ui) {
        let Some(cfg) = self.cfg() else {
            muted(ui, "config.yaml couldn't be loaded - see the Log tab, or edit it by hand.");
            if ui.button("Edit config.yaml").clicked() {
                open_path(&self.cfg_path);
            }
            return;
        };
        let mut pods = cfg.podcasts.clone();
        pods.sort_by_key(|p| sort_key(&p.name));
        let rows: Vec<Row> = pods.iter().map(|p| {
            let folder = self.folder(&cfg, &p.name);
            let (mut have, mut waiting, mut latest) = (0, 0, String::new());
            if folder.is_dir() {
                for ep in peek_state(&folder).episodes().values().filter_map(Value::as_object) {
                    let st = s(ep, "status");
                    if st == "done" && !s(ep, "file").is_empty() && folder.join(s(ep, "file")).exists() {
                        have += 1;
                        latest = latest.max(prefix(s(ep, "published"), 10));
                    } else if st == "available" || st == "failed" {
                        waiting += 1;
                    }
                }
            }
            Row { id: p.name.clone(), cells: vec![p.name.clone(), if p.auto_download { "Automatic" } else { "You choose" }.into(), if p.enabled { "Yes" } else { "No" }.into(),
                have.to_string(), if p.auto_download { String::new() } else { waiting.to_string() }, latest,
                p.rss().map(String::from).unwrap_or_else(|| format!("search: {}", p.search().unwrap_or("")))], color: (!p.enabled).then_some(MUTED), bold: false }
        }).collect();
        let mut sel = std::mem::take(&mut self.pods_sel);
        let h = ui.available_height() - 40.0;
        let out = table(ui, "pods", &[("Podcast", 240.0, false), ("Downloads", 100.0, false), ("Enabled", 64.0, false), ("Episodes", 70.0, false), ("To choose", 76.0, false), ("Latest", 96.0, false), ("Feed", 300.0, true)], &rows, &mut sel, true, h);
        self.pods_sel = sel;
        if let Some(id) = out.double {
            self.open_podcast_form(Some(&id));
        }
        ui.horizontal(|ui| {
            if ui.button(RichText::new("Add podcast...").strong()).clicked() {
                self.open_podcast_form(None);
            }
            let names = self.pods_sel.clone();
            let need = |me: &mut Self| { if names.is_empty() { me.info("Podcasts", "Select a podcast first."); false } else { true } };
            if ui.button("Edit...").clicked() && need(self) {
                self.open_podcast_form(Some(&names[0]));
            }
            if ui.button("Auto / Choose").clicked() && need(self) {
                let cur: HashMap<String, bool> = cfg.podcasts.iter().map(|p| (p.name.clone(), p.auto_download)).collect();
                self.edit_config(|ed| {
                    for n in &names {
                        ed.update_podcast(n, &json!({"auto_download": !cur.get(n).copied().unwrap_or(true)}).as_object().unwrap().clone())?;
                    }
                    Ok(())
                });
            }
            if ui.button("Enable / Disable").clicked() && need(self) {
                let cur: HashMap<String, bool> = cfg.podcasts.iter().map(|p| (p.name.clone(), p.enabled)).collect();
                self.edit_config(|ed| {
                    for n in &names {
                        ed.update_podcast(n, &json!({"enabled": !cur.get(n).copied().unwrap_or(true)}).as_object().unwrap().clone())?;
                    }
                    Ok(())
                });
            }
            if ui.button("Remove").clicked() && need(self) {
                self.modal = Some(Modal::Confirm("Remove podcast".into(), format!("Stop checking {}?\n\nDownloaded episodes stay in their folder.", names.join(", ")), Confirm::RemovePodcasts(names.clone())));
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.button("Open folder").clicked() && need(self) {
                    for n in &names {
                        let f = cfg.podcast_folder(n);
                        let _ = std::fs::create_dir_all(&f);
                        open_path(&f);
                    }
                }
            });
        });
    }

    fn open_podcast_form(&mut self, name: Option<&str>) {
        let Some(cfg) = self.cfg() else { return };
        let entry = name.and_then(|n| cfg.podcasts.iter().find(|p| p.name == n)).cloned();
        let raw = name.and_then(|n| ConfigEditor::open(&self.cfg_path).ok().and_then(|e| e.find(n))).unwrap_or_default();
        let get = |k: &str| raw.get(k).map(scalar_text).unwrap_or_default();
        self.podcast_form = Some(PodcastForm {
            orig_name: name.map(String::from),
            name: get("name"),
            rss: get("rss"),
            auto: entry.as_ref().map(|e| e.auto_download).unwrap_or(cfg.podcast_defaults.auto_download),
            enabled: entry.as_ref().map(|e| e.enabled).unwrap_or(true),
            initial: get("initial_episodes"),
            keep: get("keep_episodes"),
            days: get("keep_days"),
            sponsors: raw.get("sponsors").and_then(Value::as_array).map(|a| a.iter().map(scalar_text).collect::<Vec<_>>().join(", ")).unwrap_or_default(),
            search_term: Some(get("search")).filter(|s| !s.is_empty()),
            default_initial: cfg.podcast_defaults.initial_episodes,
            ..Default::default()
        });
    }

    fn podcast_dialog(&mut self, ctx: &egui::Context) {
        let Some(mut f) = self.podcast_form.take() else { return };
        let mut open = true;
        let mut save = false;
        let title = if f.orig_name.is_some() { "Edit podcast" } else { "Add podcast" };
        egui::Window::new(title).collapsible(false).resizable(false).min_width(640.0).default_width(640.0)
            .anchor(egui::Align2::CENTER_CENTER, [0.0, 0.0]).open(&mut open).show(ctx, |ui| {
            ui.set_min_width(640.0);
            if f.orig_name.is_none() {
                ui.strong("Find a show");
                ui.horizontal(|ui| {
                    let r = ui.add(egui::TextEdit::singleline(&mut f.search).desired_width(360.0));
                    let go = r.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter));
                    if (ui.add_enabled(!f.searching, egui::Button::new("Search")).clicked() || go) && !f.search.trim().is_empty() {
                        f.searching = true;
                        f.results.clear();
                        f.results_msg = "Searching...".into();
                        let (tx, term) = (self.bg_tx.clone(), f.search.trim().to_string());
                        std::thread::spawn(move || {
                            let _ = tx.send(Bg::Search(feeds::itunes_search(&term, &feeds::make_session(), 15).map_err(|e| e.to_string())));
                        });
                    }
                });
                egui::ScrollArea::vertical().id_salt("results").max_height(130.0).show(ui, |ui| {
                    if !f.results_msg.is_empty() {
                        muted(ui, f.results_msg.clone());
                    }
                    for r in f.results.clone() {
                        if ui.selectable_label(f.rss == r.feed_url, format!("{}   -   {}", r.name, r.author)).clicked() {
                            f.name = r.name.clone();
                            f.rss = r.feed_url.clone();
                        }
                    }
                });
                muted(ui, "...or paste the RSS feed address below.");
                ui.separator();
            }
            egui::Grid::new("podform").num_columns(3).spacing([10.0, 6.0]).min_col_width(110.0).show(ui, |ui| {
                ui.label("Name");
                ui.add(egui::TextEdit::singleline(&mut f.name).desired_width(360.0));
                ui.end_row();
                ui.label("RSS feed");
                ui.add(egui::TextEdit::singleline(&mut f.rss).desired_width(360.0));
                ui.end_row();
                ui.label("New episodes");
                ui.vertical(|ui| {
                    ui.set_min_width(360.0);
                    ui.radio_value(&mut f.auto, true, "Download new episodes automatically");
                    ui.radio_value(&mut f.auto, false, "Let me choose (Choose episodes tab)");
                });
                ui.end_row();
                ui.label("Enabled");
                ui.checkbox(&mut f.enabled, "");
                ui.end_row();
                ui.label("First check takes");
                ui.add(egui::TextEdit::singleline(&mut f.initial).desired_width(60.0));
                muted(ui, format!("newest episodes (blank = {})", f.default_initial));
                ui.end_row();
                ui.label("Keep at most");
                ui.add(egui::TextEdit::singleline(&mut f.keep).desired_width(60.0));
                muted(ui, "episodes (blank/0 = all)");
                ui.end_row();
                ui.label("Delete after");
                ui.add(egui::TextEdit::singleline(&mut f.days).desired_width(60.0));
                muted(ui, "days (blank/0 = never)");
                ui.end_row();
                ui.label("Known sponsors");
                ui.add(egui::TextEdit::singleline(&mut f.sponsors).desired_width(360.0));
                muted(ui, "comma-separated");
                ui.end_row();
            });
            ui.add_space(8.0);
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.button("Cancel").clicked() {
                    f.orig_name = Some("\u{0}cancel".into());
                }
                if ui.button(RichText::new("Save").strong()).clicked() {
                    save = true;
                }
            });
        });
        if !open || f.orig_name.as_deref() == Some("\u{0}cancel") || ctx.input(|i| i.key_pressed(egui::Key::Escape)) {
            return;
        }
        if save && self.save_podcast(&f) {
            return;
        }
        self.podcast_form = Some(f);
    }

    fn save_podcast(&mut self, f: &PodcastForm) -> bool {
        let name = squash(&f.name);
        let rss = f.rss.trim().to_string();
        if name.is_empty() {
            self.error("Podcast", "Enter a name.");
            return false;
        }
        if rss.is_empty() && f.search_term.is_none() {
            self.error("Podcast", "Enter the RSS feed address (or search and pick a show).");
            return false;
        }
        let lower = rss.to_lowercase();
        if !rss.is_empty() && !(lower.starts_with("http://") || lower.starts_with("https://")) {
            self.error("Podcast", "The RSS feed should start with http:// or https://");
            return false;
        }
        let int = |v: &str, label: &str| -> Result<Value, String> {
            let v = v.trim();
            if v.is_empty() {
                return Ok(Value::Null);
            }
            v.parse::<i64>().map(|n| json!(n)).map_err(|_| format!("{label} must be a whole number"))
        };
        let zero_null = |v: Value| if v == json!(0) { Value::Null } else { v };
        let fields = (|| -> Result<Map<String, Value>, String> {
            let sponsors: Vec<Value> = f.sponsors.split(',').map(str::trim).filter(|s| !s.is_empty()).map(|s| json!(s)).collect();
            let mut m = Map::new();
            m.insert("name".into(), json!(name));
            m.insert("rss".into(), if rss.is_empty() { Value::Null } else { json!(rss) });
            m.insert("auto_download".into(), if f.auto { Value::Null } else { json!(false) });
            m.insert("enabled".into(), if f.enabled { Value::Null } else { json!(false) });
            m.insert("initial_episodes".into(), int(&f.initial, "First check")?);
            m.insert("keep_episodes".into(), zero_null(int(&f.keep, "Keep at most")?));
            m.insert("keep_days".into(), zero_null(int(&f.days, "Delete after")?));
            m.insert("sponsors".into(), if sponsors.is_empty() { Value::Null } else { Value::Array(sponsors) });
            if !rss.is_empty() {
                m.insert("search".into(), Value::Null);
            }
            Ok(m)
        })();
        let fields = match fields {
            Ok(x) => x,
            Err(e) => {
                self.error("Podcast", &e);
                return false;
            }
        };
        let cfg = self.cfg();
        let old_folder = f.orig_name.as_ref().and_then(|n| cfg.as_ref().map(|c| c.podcast_folder(n)));
        let orig = f.orig_name.clone();
        let ok = self.edit_config(|ed| match &orig {
            Some(n) => ed.update_podcast(n, &fields),
            None => ed.add_podcast(&fields.iter().filter(|(_, v)| !v.is_null()).map(|(k, v)| (k.clone(), v.clone())).collect()),
        });
        if !ok {
            return false;
        }
        // Renamed? Move its folder so downloaded episodes and history come along.
        if let (Some(old), Some(cfg)) = (old_folder, self.cfg()) {
            let new = cfg.podcast_folder(&name);
            if old.is_dir() && new != old && !new.exists() {
                if let Err(e) = std::fs::rename(&old, &new) {
                    self.modal = Some(Modal::Error("Podcast".into(), format!("Saved, but could not rename the folder:\n{e}")));
                }
            }
        }
        if orig.is_none() {
            self.tab = Tab::Podcasts;
            self.modal = Some(Modal::Confirm("Podcast added".into(), format!("Added {name}. Check for its episodes now?"), Confirm::CheckAfterAdd));
        }
        true
    }

    // ------------------------------------------------------------ Settings tab
    fn load_settings(&mut self) {
        let Ok(ed) = ConfigEditor::open(&self.cfg_path) else { return };
        for k in TEXT_SETTINGS.iter().chain(NUM_SETTINGS.iter()) {
            let v = ed.get(k).unwrap_or_else(|| default_setting(k));
            self.settings.insert(k, scalar_text(&v));
        }
        for k in BOOL_SETTINGS {
            let v = ed.get(k).unwrap_or_else(|| default_setting(k));
            self.settings_bool.insert(k, v.as_bool().unwrap_or(true));
        }
        self.startup = startup_enabled();
    }

    fn refresh_models(&mut self) {
        let url = self.settings.get("ollama.url").map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).unwrap_or_else(|| "http://localhost:11434".into());
        self.models_fetched = Some(Instant::now());
        self.models_msg = ("Checking Ollama...".into(), MUTED);
        let tx = self.bg_tx.clone();
        std::thread::spawn(move || {
            let msg = match list_models(&url, Duration::from_secs(5)) {
                Ok(m) => Bg::Models(m, None, url),
                Err(e) => Bg::Models(vec![], Some(e.to_string()), url),
            };
            let _ = tx.send(msg);
        });
    }

    fn model_installed(&self, name: &str) -> bool {
        self.models.iter().any(|m| m.name == name || m.name == format!("{name}:latest"))
    }

    fn apply_models(&mut self, models: Vec<OllamaModel>, err: Option<String>, url: &str) {
        // embedding-only models can't write answers - leave them out
        let mut m: Vec<OllamaModel> = models.into_iter().filter(|m| !m.name.is_empty() && !m.name.to_lowercase().contains("embed") && !m.family.to_lowercase().contains("bert")).collect();
        m.sort_by(|a, b| a.name.cmp(&b.name));
        self.models = m;
        self.models_error = err.clone();
        let current = self.settings.get("ollama.model").cloned().unwrap_or_default();
        self.models_msg = if err.is_some() {
            (format!("Can't reach Ollama at {url} - is it running? Start Ollama, then click ↻."), RED)
        } else if self.models.is_empty() {
            ("Ollama has no models yet. In a terminal run:  ollama pull llama3.1:8b".into(), RED)
        } else if !current.is_empty() && !self.model_installed(&current) {
            (format!("'{current}' isn't installed - pick one above, or run:  ollama pull {current}"), AMBER)
        } else {
            let n = self.models.len();
            (format!("{n} model{} installed. Bigger models catch more ads but run slower.", if n != 1 { "s" } else { "" }), MUTED)
        };
    }

    fn model_label(m: &OllamaModel) -> String {
        let mut bits = vec![];
        if !m.parameter_size.is_empty() {
            bits.push(m.parameter_size.clone());
        }
        if m.size > 0.0 {
            bits.push(format!("{:.1} GB", m.size / 1e9));
        }
        if bits.is_empty() { m.name.clone() } else { format!("{}   ({})", m.name, bits.join(", ")) }
    }

    fn settings_tab(&mut self, ui: &mut egui::Ui) {
        let field = |ui: &mut egui::Ui, settings: &mut HashMap<&'static str, String>, label: &str, key: &'static str, width: f32| {
            ui.label(label);
            ui.add(egui::TextEdit::singleline(settings.entry(key).or_default()).desired_width(width));
        };
        egui::ScrollArea::vertical().show(ui, |ui| {
            ui.columns(2, |cols| {
                let ui = &mut cols[0];
                ui.group(|ui| {
                    ui.set_width(ui.available_width());
                    ui.strong("General");
                    egui::Grid::new("gen").num_columns(2).spacing([10.0, 6.0]).show(ui, |ui| {
                        field(ui, &mut self.settings, "Save podcasts to", "output_dir", 300.0);
                        ui.end_row();
                        ui.label("");
                        if ui.button("Browse...").clicked() {
                            if let Some(d) = rfd::FileDialog::new().pick_folder() {
                                self.settings.insert("output_dir", d.display().to_string());
                            }
                        }
                        ui.end_row();
                        field(ui, &mut self.settings, "Check every", "check_interval_minutes", 60.0);
                        ui.end_row();
                        ui.label("");
                        muted(ui, "minutes");
                        ui.end_row();
                        ui.label("Notifications");
                        ui.checkbox(self.settings_bool.entry("notifications").or_default(), "show a message when episodes finish");
                        ui.end_row();
                        ui.label("Windows startup");
                        ui.add_enabled(cfg!(windows), egui::Checkbox::new(&mut self.startup, "start with Windows, in the tray"));
                        ui.end_row();
                    });
                });
                ui.add_space(8.0);
                ui.group(|ui| {
                    ui.set_width(ui.available_width());
                    ui.strong("Transcription (Whisper)");
                    egui::Grid::new("whisper").num_columns(2).spacing([10.0, 6.0]).show(ui, |ui| {
                        ui.label("Model");
                        let cur = self.settings.entry("whisper.model").or_default();
                        egui::ComboBox::from_id_salt("wmodel").selected_text(cur.clone()).show_ui(ui, |ui| {
                            for m in WHISPER_MODELS {
                                ui.selectable_value(cur, m.to_string(), m);
                            }
                        });
                        ui.end_row();
                        ui.label("");
                        muted(ui, "small: good on CPU.  turbo / large-v3: GPU builds");
                        ui.end_row();
                        ui.label("Device");
                        let cur = self.settings.entry("whisper.device").or_default();
                        egui::ComboBox::from_id_salt("wdevice").selected_text(cur.clone()).show_ui(ui, |ui| {
                            for m in ["auto", "cpu", "gpu"] {
                                ui.selectable_value(cur, m.to_string(), m);
                            }
                        });
                        ui.end_row();
                    });
                });
                let ui = &mut cols[1];
                ui.group(|ui| {
                    ui.set_width(ui.available_width());
                    ui.strong("Ad detection (Ollama)");
                    egui::Grid::new("ollama").num_columns(2).spacing([10.0, 6.0]).show(ui, |ui| {
                        ui.label("Remove ads");
                        ui.checkbox(self.settings_bool.entry("ad_removal.enabled").or_default(), "find and cut ads");
                        ui.end_row();
                        field(ui, &mut self.settings, "Ollama address", "ollama.url", 260.0);
                        ui.end_row();
                        ui.label("Ollama model");
                        ui.horizontal(|ui| {
                            let current = self.settings.get("ollama.model").cloned().unwrap_or_default();
                            let shown = self.models.iter().find(|m| m.name == current || m.name == format!("{current}:latest")).map(Self::model_label)
                                .unwrap_or_else(|| if self.models_error.is_some() || current.is_empty() { current.clone() } else { format!("{current}   (not installed)") });
                            let mut picked = None;
                            egui::ComboBox::from_id_salt("omodel").width(280.0).selected_text(shown).show_ui(ui, |ui| {
                                for m in &self.models {
                                    if ui.selectable_label(m.name == current, Self::model_label(m)).clicked() {
                                        picked = Some(m.name.clone());
                                    }
                                }
                            });
                            if let Some(p) = picked {
                                self.settings.insert("ollama.model", p);
                                self.models_msg = ("Click Save settings to use this model.".into(), MUTED);
                            }
                            if ui.button("↻").clicked() {
                                self.refresh_models();
                            }
                        });
                        ui.end_row();
                        ui.label("");
                        ui.label(RichText::new(self.models_msg.0.clone()).color(self.models_msg.1));
                        ui.end_row();
                        field(ui, &mut self.settings, "Crossfade at cuts", "output.crossfade_ms", 60.0);
                        ui.end_row();
                        ui.label("");
                        muted(ui, "milliseconds, 0 = hard cut");
                        ui.end_row();
                        ui.label("Transcripts");
                        ui.checkbox(self.settings_bool.entry("output.archive_transcripts").or_default(), "keep one for every episode");
                        ui.end_row();
                    });
                });
                ui.add_space(8.0);
                ui.group(|ui| {
                    ui.set_width(ui.available_width());
                    ui.strong("Phone feeds");
                    egui::Grid::new("feeds").num_columns(2).spacing([10.0, 6.0]).show(ui, |ui| {
                        ui.label("Feed server");
                        ui.checkbox(self.settings_bool.entry("server.enabled").or_default(), "serve cleaned episodes as RSS");
                        ui.end_row();
                        field(ui, &mut self.settings, "Port", "server.port", 70.0);
                        ui.end_row();
                        field(ui, &mut self.settings, "Secret token", "server.token", 140.0);
                        ui.end_row();
                        ui.label("");
                        muted(ui, "optional; restart the app after changes here");
                        ui.end_row();
                    });
                });
            });
            ui.add_space(8.0);
            ui.horizontal(|ui| {
                if ui.button(RichText::new("Save settings").strong()).clicked() {
                    self.save_settings();
                }
                if ui.button("Undo changes").clicked() {
                    self.load_settings();
                }
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if ui.button("Edit config.yaml (all options)...").clicked() {
                        open_path(&self.cfg_path);
                    }
                });
            });
        });
    }

    fn save_settings(&mut self) {
        let mut values: Vec<(&'static str, Value)> = vec![];
        for k in TEXT_SETTINGS {
            let v = self.settings.get(k).cloned().unwrap_or_default().trim().to_string();
            if k == "check_interval_minutes" {
                match v.parse::<f64>() {
                    Ok(n) => values.push((k, tidy_num(n))),
                    Err(_) => return self.error("Could not save", &format!("'{v}' is not a number")),
                }
            } else {
                values.push((k, json!(v)));
            }
        }
        for k in NUM_SETTINGS {
            let v = self.settings.get(k).cloned().unwrap_or_default();
            match v.trim().parse::<i64>() {
                Ok(n) => values.push((k, json!(n))),
                Err(_) => return self.error("Could not save", &format!("'{v}' is not a number")),
            }
        }
        for k in BOOL_SETTINGS {
            values.push((k, json!(self.settings_bool.get(k).copied().unwrap_or(false))));
        }
        if self.edit_config(|ed| {
            for (k, v) in values {
                ed.set_if_changed(k, v, &default_setting(k))?;
            }
            Ok(())
        }) {
            if cfg!(windows) && self.startup != startup_enabled() {
                app::set_startup(self.startup);
            }
            self.info("Settings", "Saved. They apply from the next episode.");
        }
    }

    // ------------------------------------------------------------ Ad rules tab
    fn load_adrules(&mut self) {
        let Ok(ed) = ConfigEditor::open(&self.cfg_path) else { return };
        self.rules_text = ed.get("ollama.extra_instructions").map(|v| scalar_text(&v)).unwrap_or_default().trim_end().to_string();
        for k in RULE_KEYS {
            self.rules.insert(k, scalar_text(&ed.get(k).unwrap_or_else(|| default_setting(k))));
        }
    }

    fn adrules_tab(&mut self, ui: &mut egui::Ui) {
        ui.group(|ui| {
            ui.set_width(ui.available_width());
            ui.strong("What counts as an ad");
            muted(ui, "Plain-English guidance added to the AI's instructions for every show. Say what IS an ad and what is NOT - the second part prevents real content from being cut. Sponsors for a single show go in Podcasts > Edit > Known sponsors.");
            egui::ScrollArea::vertical().id_salt("rules").max_height((ui.available_height() - 220.0).max(120.0)).show(ui, |ui| {
                ui.add(egui::TextEdit::multiline(&mut self.rules_text).desired_rows(10).desired_width(f32::INFINITY));
            });
            ui.horizontal(|ui| {
                if ui.button("Insert example").clicked() {
                    if !self.rules_text.trim().is_empty() {
                        self.rules_text.push('\n');
                    }
                    self.rules_text.push_str(AD_RULES_EXAMPLE);
                }
                muted(ui, "Keep it short and specific; long instructions can confuse small models.");
            });
        });
        ui.add_space(8.0);
        ui.group(|ui| {
            ui.set_width(ui.available_width());
            ui.strong("Length limits");
            egui::Grid::new("limits").num_columns(3).spacing([10.0, 6.0]).show(ui, |ui| {
                for (label, key, hint) in [
                    ("Ignore ads shorter than", "ad_removal.min_ad_seconds", "seconds - raise if it cuts short mentions you'd rather keep, lower if it misses quick sponsor tags"),
                    ("Ignore ads longer than", "ad_removal.max_ad_seconds", "seconds - protects real content from a bad detection"),
                    ("Trim extra around each ad", "ad_removal.padding_seconds", "seconds - e.g. 1 if bits of ads are left at the edges"),
                ] {
                    ui.label(label);
                    ui.add(egui::TextEdit::singleline(self.rules.entry(key).or_default()).desired_width(70.0));
                    muted(ui, hint);
                    ui.end_row();
                }
            });
        });
        ui.add_space(8.0);
        ui.horizontal(|ui| {
            if ui.button(RichText::new("Save ad rules").strong()).clicked() {
                self.save_adrules();
            }
            if ui.button("Undo changes").clicked() {
                self.load_adrules();
            }
            muted(ui, "Applies to episodes processed after saving; each episode's report.txt shows what was cut.");
        });
    }

    fn save_adrules(&mut self) {
        let get = |k: &str| self.rules.get(k).and_then(|v| v.trim().parse::<f64>().ok());
        let (Some(mn), Some(mx), Some(pad)) = (get(RULE_KEYS[0]), get(RULE_KEYS[1]), get(RULE_KEYS[2])) else {
            return self.error("Ad rules", "The length limits must be numbers.");
        };
        if mn < 0.0 || pad < 0.0 || mx <= mn {
            return self.error("Ad rules", "'Longer than' must be bigger than 'shorter than', and no value can be negative.");
        }
        let text = self.rules_text.trim().to_string();
        let text = if text.contains('\n') { format!("{text}\n") } else { text };
        if self.edit_config(|ed| {
            ed.set_if_changed("ollama.extra_instructions", json!(text), &json!(""))?;
            ed.set_if_changed("ad_removal.min_ad_seconds", tidy_num(mn), &default_setting(RULE_KEYS[0]))?;
            ed.set_if_changed("ad_removal.max_ad_seconds", tidy_num(mx), &default_setting(RULE_KEYS[1]))?;
            ed.set_if_changed("ad_removal.padding_seconds", json!(pad), &default_setting(RULE_KEYS[2]))?;
            Ok(())
        }) {
            self.info("Ad rules", "Saved. They apply to the next episode processed.");
        }
    }

    // ------------------------------------------------------------ Log tab
    fn refresh_log(&mut self) {
        let Some(cfg) = self.cfg() else { return };
        let Ok(meta) = std::fs::metadata(&cfg.log_file) else { return };
        let size = meta.len();
        if size < self.log_pos {
            self.log_pos = 0; // rotated
        }
        if self.log_pos == 0 {
            self.log_pos = size.saturating_sub(200_000);
            self.log_lines.clear();
        }
        if size == self.log_pos {
            return;
        }
        use std::io::{Read, Seek, SeekFrom};
        let Ok(mut f) = std::fs::File::open(&cfg.log_file) else { return };
        let _ = f.seek(SeekFrom::Start(self.log_pos));
        let mut buf = vec![];
        let _ = f.read_to_end(&mut buf);
        self.log_pos = size;
        for line in String::from_utf8_lossy(&buf).lines() {
            let col = if line.contains(" ERROR ") { Some(RED) } else if line.contains(" WARNING ") { Some(AMBER) } else { None };
            self.log_lines.push((line.to_string(), col));
        }
        if self.log_lines.len() > 3000 {
            let n = self.log_lines.len() - 3000;
            self.log_lines.drain(..n);
        }
    }

    fn log_tab(&mut self, ui: &mut egui::Ui) {
        self.refresh_log();
        ui.horizontal(|ui| {
            ui.checkbox(&mut self.log_follow, "Follow");
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.button("Open log file").clicked() {
                    if let Some(c) = self.cfg() {
                        if c.log_file.exists() {
                            open_path(&c.log_file);
                        }
                    }
                }
            });
        });
        let h = ui.text_style_height(&egui::TextStyle::Monospace);
        egui::ScrollArea::both().auto_shrink([false, false]).stick_to_bottom(self.log_follow).show_rows(ui, h, self.log_lines.len(), |ui, range| {
            for (line, col) in &self.log_lines[range] {
                let mut t = RichText::new(line).monospace();
                if let Some(c) = col {
                    t = t.color(*c);
                }
                ui.label(t);
            }
        });
    }

    // ------------------------------------------------------------ modals
    fn modals(&mut self, ctx: &egui::Context) {
        let Some(modal) = self.modal.take() else { return };
        let mut keep = true;
        let (title, body, color) = match &modal {
            Modal::Info(t, b) => (t.clone(), b.clone(), None),
            Modal::Error(t, b) => (t.clone(), b.clone(), Some(RED)),
            Modal::Confirm(t, b, _) => (t.clone(), b.clone(), None),
            Modal::AddSponsor(_) => ("Add sponsor".into(), "Sponsor name to watch for on every show.\nAdd several at once by separating them with commas:".into(), None),
        };
        let mut result: Option<bool> = None;
        let mut modal = modal;
        egui::Window::new(title).collapsible(false).resizable(false).min_width(360.0).anchor(egui::Align2::CENTER_CENTER, [0.0, 0.0]).show(ctx, |ui| {
            ui.set_max_width(460.0);
            let mut t = RichText::new(body);
            if let Some(c) = color {
                t = t.color(c);
            }
            ui.label(t);
            if let Modal::AddSponsor(text) = &mut modal {
                let r = ui.add(egui::TextEdit::singleline(text).desired_width(400.0));
                r.request_focus();
                if r.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter)) {
                    result = Some(true);
                }
            }
            ui.add_space(8.0);
            ui.horizontal(|ui| match &modal {
                Modal::Info(..) | Modal::Error(..) => {
                    if ui.button("OK").clicked() || ui.input(|i| i.key_pressed(egui::Key::Enter)) {
                        result = Some(true);
                    }
                }
                _ => {
                    if ui.button(RichText::new(if matches!(modal, Modal::AddSponsor(_)) { "Add" } else { "Yes" }).strong()).clicked() {
                        result = Some(true);
                    }
                    if ui.button(if matches!(modal, Modal::AddSponsor(_)) { "Cancel" } else { "No" }).clicked() || ui.input(|i| i.key_pressed(egui::Key::Escape)) {
                        result = Some(false);
                    }
                }
            });
        });
        if let Some(yes) = result {
            keep = false;
            if yes {
                match modal {
                    Modal::Confirm(_, _, action) => self.confirmed(action),
                    Modal::AddSponsor(text) => {
                        if !text.trim().is_empty() {
                            self.add_watch(&text);
                        }
                    }
                    _ => {}
                }
                return;
            }
        }
        if keep {
            self.modal = Some(modal);
        }
    }

    fn confirmed(&mut self, action: Confirm) {
        let w = self.worker.clone();
        match action {
            Confirm::Reanalyze(items) => items.iter().for_each(|(f, g, t)| w.reanalyze_episode(f, g, t)),
            Confirm::SkipChoices(items) => items.iter().for_each(|(f, g, t)| w.skip_episode(f, g, t)),
            Confirm::BrowseDownload(name, picked) => self.do_browse_download(&name, picked),
            Confirm::RemovePodcasts(names) => {
                self.edit_config(|ed| {
                    for n in &names {
                        ed.remove_podcast(n)?;
                    }
                    Ok(())
                });
            }
            Confirm::StopNow => w.stop_now(),
            Confirm::CheckAfterAdd => w.check_now(),
        }
    }

    fn hide(&mut self, ctx: &egui::Context) {
        if !self.has_tray {
            QUITTING.store(true, SeqCst);
            ctx.send_viewport_cmd(egui::ViewportCommand::Close);
            return;
        }
        set_window_visible(false, ctx);
        if !self.hidden_hint_shown {
            self.hidden_hint_shown = true;
            desktop_notify("Still running", "Podcast Ad Cleaner keeps working in the tray. Right-click the icon > Quit to exit.");
        }
    }
}

impl eframe::App for MainApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        if ctx.input(|i| i.viewport().close_requested()) && !QUITTING.load(SeqCst) {
            ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
            self.hide(ctx);
        }
        self.poll_bg();
        if self.last_slow.elapsed() > Duration::from_secs(4) {
            self.last_slow = Instant::now();
            let cfg = self.cfg();
            self.refresh_recent(&cfg);
        }
        let settings_age = self.models_fetched.map(|t| t.elapsed()).unwrap_or(Duration::MAX);

        egui::TopBottomPanel::top("header").show(ctx, |ui| {
            ui.add_space(6.0);
            self.header(ui);
            ui.add_space(6.0);
            ui.horizontal(|ui| {
                let choose = if self.choose_total > 0 { format!("Choose episodes ({})", self.choose_total) } else { "Choose episodes".into() };
                for (t, label) in [(Tab::Activity, "Activity".to_string()), (Tab::Choose, choose), (Tab::Browse, "Browse feed".into()), (Tab::Ads, "Ads".into()),
                    (Tab::Podcasts, "Podcasts".into()), (Tab::AdRules, "Ad rules".into()), (Tab::Settings, "Settings".into()), (Tab::Log, "Log".into())] {
                    if ui.selectable_label(self.tab == t, format!("  {label}  ")).clicked() {
                        self.tab = t;
                        self.last_slow = Instant::now() - Duration::from_secs(60);
                        if t == Tab::Settings && settings_age > Duration::from_secs(20) {
                            self.refresh_models();
                        }
                    }
                }
            });
            ui.add_space(2.0);
        });
        egui::CentralPanel::default().show(ctx, |ui| match self.tab {
            Tab::Activity => self.activity_tab(ui),
            Tab::Choose => self.choose_tab(ui),
            Tab::Browse => self.browse_tab(ui),
            Tab::Ads => self.ads_tab(ui),
            Tab::Podcasts => self.podcasts_tab(ui),
            Tab::AdRules => self.adrules_tab(ui),
            Tab::Settings => self.settings_tab(ui),
            Tab::Log => self.log_tab(ui),
        });
        self.podcast_dialog(ctx);
        self.modals(ctx);
        ctx.request_repaint_after(Duration::from_millis(500));
    }
}

// ---------------------------------------------------------------- window visibility (from any thread)

static EGUI_CTX: Mutex<Option<egui::Context>> = Mutex::new(None);

fn set_window_visible(visible: bool, ctx: &egui::Context) {
    #[cfg(windows)]
    {
        let h = WINDOW_HANDLE.load(SeqCst);
        if h != 0 {
            use windows_sys::Win32::UI::WindowsAndMessaging::{SetForegroundWindow, ShowWindow, SW_HIDE, SW_RESTORE, SW_SHOW};
            unsafe {
                if visible {
                    ShowWindow(h as _, SW_SHOW);
                    ShowWindow(h as _, SW_RESTORE);
                    SetForegroundWindow(h as _);
                } else {
                    ShowWindow(h as _, SW_HIDE);
                }
            }
            ctx.request_repaint();
            return;
        }
    }
    ctx.send_viewport_cmd(egui::ViewportCommand::Visible(visible));
    if visible {
        ctx.send_viewport_cmd(egui::ViewportCommand::Focus);
    }
    ctx.request_repaint();
}

fn show_window() {
    if let Some(ctx) = EGUI_CTX.lock().unwrap().clone() {
        set_window_visible(true, &ctx);
    }
}

// ---------------------------------------------------------------- tray icon

#[cfg_attr(not(any(windows, target_os = "macos")), allow(dead_code))]
mod tray {
    use super::*;
    use tray_icon::menu::{CheckMenuItem, IsMenuItem, Menu, MenuEvent, MenuItem, PredefinedMenuItem, Submenu};
    use tray_icon::{Icon, MouseButton, TrayIconBuilder, TrayIconEvent};

    #[derive(Clone)]
    enum Action {
        Show,
        CheckNow,
        Pause,
        StopAfter,
        OpenFolder,
        OpenFeeds,
        EditConfig,
        OpenLog,
        Quit,
        Choose(&'static str, String, Vec<(String, String)>),
    }

    fn icon(state: &str) -> Icon {
        Icon::from_rgba(make_icon(state, 64), 64, 64).unwrap()
    }

    fn build_menu(worker: &Worker, server: &FeedServer, use_gui: bool) -> (Menu, HashMap<tray_icon::menu::MenuId, Action>) {
        let menu = Menu::new();
        let actions = std::cell::RefCell::new(HashMap::new());
        let add = |item: &dyn IsMenuItem, id: Option<(tray_icon::menu::MenuId, Action)>| {
            let _ = menu.append(item);
            if let Some((i, a)) = id {
                actions.borrow_mut().insert(i, a);
            }
        };
        if use_gui {
            let i = MenuItem::new("Open Podcast Cleaner", true, None);
            add(&i, Some((i.id().clone(), Action::Show)));
        }
        let status = worker.status();
        add(&MenuItem::new(prefix(&status, 80).replace('&', "&&"), false, None), None);
        add(&PredefinedMenuItem::separator(), None);
        let i = MenuItem::new("Check now", !worker.is_busy(), None);
        add(&i, Some((i.id().clone(), Action::CheckNow)));
        let i = CheckMenuItem::new("Pause", true, worker.is_paused(), None);
        add(&i, Some((i.id().clone(), Action::Pause)));
        if worker.is_busy() {
            let i = CheckMenuItem::new("Stop after this episode", true, worker.stop_after_current.load(SeqCst), None);
            add(&i, Some((i.id().clone(), Action::StopAfter)));
        }
        // "Choose episodes" for shows with auto_download: false
        if let Some(cfg) = worker.cfg() {
            let manual: Vec<_> = cfg.podcasts.iter().filter(|p| p.enabled && !p.auto_download).collect();
            if !manual.is_empty() {
                let sub = Submenu::new("Choose episodes", true);
                let choice: HashMap<(String, String), String> = DownloadRequests::new(&cfg.output_dir).all().into_iter().map(|r| ((r.folder, r.guid), r.action)).collect();
                for p in manual {
                    let folder = cfg.podcast_folder(&p.name);
                    let fname = folder.file_name().unwrap().to_string_lossy().to_string();
                    let eps: Vec<_> = if folder.is_dir() { peek_state(&folder).choosable(15) } else { vec![] }
                        .into_iter().filter(|(g, _)| choice.get(&(fname.clone(), g.clone())).map(String::as_str) != Some("skip")).collect();
                    let name = p.name.replace('&', "&&");
                    if eps.is_empty() {
                        let _ = sub.append(&MenuItem::new(format!("{name} - nothing new"), false, None));
                        continue;
                    }
                    let show = Submenu::new("", true);
                    let mut undecided = vec![];
                    for (g, e) in &eps {
                        let wanted = choice.get(&(fname.clone(), g.clone())).map(String::as_str) == Some("download") || s(e, "status") == "queued";
                        let mut label = prefix(&format!("{}  {}", prefix(s(e, "published"), 10), s(e, "title")), 70).replace('&', "&&");
                        if wanted {
                            label = format!("\u{2713} {label}  (queued)");
                        } else if s(e, "status") == "failed" {
                            label += "  (failed)";
                        }
                        let one = vec![(g.clone(), s(e, "title").to_string())];
                        if !wanted {
                            undecided.push(one[0].clone());
                        }
                        let epm = Submenu::new(label, true);
                        let d = MenuItem::new(if s(e, "status") == "failed" { "Retry download" } else { "Download" }, !wanted, None);
                        let k = MenuItem::new(if wanted { "Skip (cancel download)" } else { "Skip" }, true, None);
                        actions.borrow_mut().insert(d.id().clone(), Action::Choose("download", fname.clone(), one.clone()));
                        actions.borrow_mut().insert(k.id().clone(), Action::Choose("skip", fname.clone(), one));
                        let _ = epm.append_items(&[&d, &k]);
                        let _ = show.append(&epm);
                    }
                    let _ = show.append(&PredefinedMenuItem::separator());
                    let all = MenuItem::new(format!("Download all {}", undecided.len()), !undecided.is_empty(), None);
                    actions.borrow_mut().insert(all.id().clone(), Action::Choose("download", fname.clone(), undecided.clone()));
                    let skip = MenuItem::new(format!("Skip all {}", eps.len()), true, None);
                    actions.borrow_mut().insert(skip.id().clone(), Action::Choose("skip", fname.clone(), eps.iter().map(|(g, e)| (g.clone(), s(e, "title").to_string())).collect()));
                    let _ = show.append_items(&[&all, &skip]);
                    show.set_text(if undecided.is_empty() { name } else { format!("{name} ({} new)", undecided.len()) });
                    let _ = sub.append(&show);
                }
                add(&sub, None);
            }
        }
        add(&PredefinedMenuItem::separator(), None);
        let i = MenuItem::new("Open podcasts folder", true, None);
        add(&i, Some((i.id().clone(), Action::OpenFolder)));
        if server.running() {
            let i = MenuItem::new("Open feed page (subscribe on phone)", true, None);
            add(&i, Some((i.id().clone(), Action::OpenFeeds)));
        }
        let i = MenuItem::new("Edit config.yaml", true, None);
        add(&i, Some((i.id().clone(), Action::EditConfig)));
        let i = MenuItem::new("Open log", true, None);
        add(&i, Some((i.id().clone(), Action::OpenLog)));
        add(&PredefinedMenuItem::separator(), None);
        let i = MenuItem::new("Quit", true, None);
        add(&i, Some((i.id().clone(), Action::Quit)));
        (menu, actions.into_inner())
    }

    fn pump_messages() {
        #[cfg(windows)]
        unsafe {
            use windows_sys::Win32::UI::WindowsAndMessaging::{DispatchMessageW, PeekMessageW, TranslateMessage, MSG, PM_REMOVE};
            let mut msg: MSG = std::mem::zeroed();
            while PeekMessageW(&mut msg, std::ptr::null_mut(), 0, 0, PM_REMOVE) != 0 {
                TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }
        }
    }

    /// Runs the tray icon on its own thread until Quit.
    pub fn spawn(worker: Arc<Worker>, server: Arc<FeedServer>, cfg_path: PathBuf, use_gui: bool, quit: Arc<dyn Fn() + Send + Sync>) {
        std::thread::Builder::new().name("tray".into()).spawn(move || {
            let (menu, mut actions) = build_menu(&worker, &server, use_gui);
            let mut shown_state = worker.state();
            let tray = match TrayIconBuilder::new().with_menu(Box::new(menu)).with_tooltip("Podcast Ad Cleaner").with_icon(icon(&shown_state)).build() {
                Ok(t) => t,
                Err(e) => {
                    log::error!("Could not create the tray icon: {e}");
                    return;
                }
            };
            let mut last_build = Instant::now();
            let mut last_tip = String::new();
            loop {
                pump_messages();
                while let Ok(ev) = TrayIconEvent::receiver().try_recv() {
                    match ev {
                        TrayIconEvent::DoubleClick { button: MouseButton::Left, .. } => {
                            if use_gui { show_window() } else { worker.check_now() }
                        }
                        _ => {}
                    }
                }
                while let Ok(ev) = MenuEvent::receiver().try_recv() {
                    let Some(a) = actions.get(&ev.id).cloned() else { continue };
                    match a {
                        Action::Show => show_window(),
                        Action::CheckNow => worker.check_now(),
                        Action::Pause => worker.toggle_pause(),
                        Action::StopAfter => worker.toggle_stop_after(),
                        Action::OpenFolder => {
                            if let Some(c) = worker.cfg() {
                                let _ = std::fs::create_dir_all(&c.output_dir);
                                open_path(&c.output_dir);
                            }
                        }
                        Action::OpenFeeds => open_url(&server.base_url(true)),
                        Action::EditConfig => open_path(&cfg_path),
                        Action::OpenLog => {
                            if let Some(c) = worker.cfg() {
                                if c.log_file.exists() {
                                    open_path(&c.log_file);
                                }
                            }
                        }
                        Action::Quit => {
                            log::info!("Quit requested");
                            quit();
                            return;
                        }
                        Action::Choose(act, folder, eps) => {
                            for (g, t) in eps {
                                if act == "skip" { worker.skip_episode(&folder, &g, &t) } else { worker.request_episode(&folder, &g, &t) }
                            }
                            last_build = Instant::now() - Duration::from_secs(10);
                        }
                    }
                }
                let st = worker.state();
                if st != shown_state {
                    let _ = tray.set_icon(Some(icon(&st)));
                    shown_state = st;
                }
                let tip = prefix(&format!("Podcast Ad Cleaner\n{}", worker.status()), 127);
                if tip != last_tip {
                    let _ = tray.set_tooltip(Some(&tip));
                    last_tip = tip;
                }
                if last_build.elapsed() > Duration::from_secs(3) {
                    let (menu, a) = build_menu(&worker, &server, use_gui);
                    tray.set_menu(Some(Box::new(menu)));
                    actions = a;
                    last_build = Instant::now();
                }
                std::thread::sleep(Duration::from_millis(80));
            }
        }).unwrap();
    }
}

// ---------------------------------------------------------------- entry point

pub fn run_tray(cfg_path: PathBuf, minimized: bool, use_gui: bool) -> i32 {
    let has_tray = cfg!(any(windows, target_os = "macos"));
    let first_run = app::create_config_if_missing(&cfg_path, !use_gui);
    let cfg = load_config(Some(&cfg_path)).ok();
    crate::logging::setup_logging(cfg.as_ref(), !cfg!(windows));
    if !app::single_instance() {
        log::info!("Another instance is already running");
        return 1;
    }
    if let Some(c) = &cfg {
        crate::pipeline::clean_temp(c);
    }
    log::info!("App started (config: {})", cfg_path.display());

    let worker = Worker::new(cfg_path.clone(), cfg.clone());
    worker.set_listener(Arc::new(|e| {
        if let Event::Notify(t, m) = e {
            desktop_notify(&t, &m);
        }
    }));
    let w2 = worker.clone();
    let feed_server = FeedServer::new(Arc::new(move || w2.cfg()));
    if let Some(c) = &cfg {
        if c.server.enabled && !feed_server.start(c) {
            worker.notify("Feed server not started", &format!("Port {} is in use - change server.port in config.yaml", c.server.port as u16));
        }
    }
    if first_run {
        worker.paused.store(true, SeqCst);
        worker.set_status("Paused - add your podcasts, then choose Check now", Some("paused"));
        desktop_notify("Welcome", if use_gui { "Add your podcasts, then choose Check now." } else {
            "config.yaml was created and opened. Add your podcasts, save it, then right-click the tray icon and choose Check now." });
    }
    let handle = worker.start();

    let (wq, sq) = (worker.clone(), feed_server.clone());
    let quit: Arc<dyn Fn() + Send + Sync> = Arc::new(move || {
        QUITTING.store(true, SeqCst);
        wq.stop();
        sq.stop();
        if let Some(ctx) = EGUI_CTX.lock().unwrap().clone() {
            ctx.send_viewport_cmd(egui::ViewportCommand::Close);
            ctx.request_repaint();
        }
        // Whisper can't be interrupted mid-step; give it a moment, then exit.
        let w = wq.clone();
        std::thread::spawn(move || {
            for _ in 0..50 {
                if !w.is_busy() {
                    break;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            log::info!("Exiting");
            log::logger().flush();
            std::process::exit(0);
        });
    });

    #[cfg(any(windows, target_os = "macos"))]
    tray::spawn(worker.clone(), feed_server.clone(), cfg_path.clone(), use_gui, quit.clone());

    if !use_gui {
        let _ = handle.join();
        return 0;
    }

    let icon = egui::IconData { rgba: make_icon("idle", 64), width: 64, height: 64 };
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default().with_title("Podcast Ad Cleaner").with_inner_size([1200.0, 720.0]).with_min_inner_size([760.0, 480.0])
            .with_icon(icon).with_visible(!(minimized && !first_run && has_tray)),
        persist_window: true,
        ..Default::default()
    };
    let (w3, s3, p3) = (worker.clone(), feed_server.clone(), cfg_path.clone());
    let res = eframe::run_native("Podcast Ad Cleaner", options, Box::new(move |cc| {
        *EGUI_CTX.lock().unwrap() = Some(cc.egui_ctx.clone());
        #[cfg(windows)]
        {
            use raw_window_handle::{HasWindowHandle, RawWindowHandle};
            if let Ok(h) = cc.window_handle() {
                if let RawWindowHandle::Win32(w) = h.as_raw() {
                    WINDOW_HANDLE.store(w.hwnd.get(), SeqCst);
                }
            }
        }
        cc.egui_ctx.set_theme(egui::ThemePreference::System);
        Ok(Box::new(MainApp::new(w3, s3, p3, first_run, has_tray)))
    }));
    if let Err(e) = res {
        log::error!("Could not open the window ({e}); running in the background only");
        if has_tray {
            let _ = handle.join();
        }
    }
    quit();
    std::thread::sleep(Duration::from_secs(10));
    0
}
