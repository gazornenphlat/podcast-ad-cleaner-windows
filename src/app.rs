//! The background worker that checks feeds on a schedule, plus small app-level helpers
//! (notifications, single instance, Windows startup shortcut, first-run config).

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering::SeqCst};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use chrono::{DateTime, Local};

use crate::activity::Activity;
use crate::config::{load_config, Config, APP_DIR};
use crate::logging::setup_logging;
use crate::pipeline::{self, Hooks};
use crate::state::{DownloadRequests, QueueOrder};
use crate::utils::prefix;

pub enum Event {
    Changed,
    Notify(String, String),
}

type Listener = Arc<dyn Fn(Event) + Send + Sync>;

pub struct Worker {
    pub cfg_path: PathBuf,
    cfg: Mutex<Option<Config>>,
    pub paused: AtomicBool,
    pub busy: AtomicBool,
    pub stop_after_current: AtomicBool,
    cancel_current: AtomicBool,
    requests_waiting: AtomicBool,
    stopping: AtomicBool,
    state: Mutex<String>,
    status: Mutex<String>,
    next_check: Mutex<Option<DateTime<Local>>>,
    last_result: Mutex<String>,
    wake: (Mutex<bool>, Condvar),
    pub activity: Arc<Activity>,
    listener: Mutex<Option<Listener>>,
}

impl Worker {
    pub fn new(cfg_path: PathBuf, cfg: Option<Config>) -> Arc<Self> {
        Arc::new(Self {
            cfg_path,
            cfg: Mutex::new(cfg),
            paused: AtomicBool::new(false),
            busy: AtomicBool::new(false),
            stop_after_current: AtomicBool::new(false),
            cancel_current: AtomicBool::new(false),
            requests_waiting: AtomicBool::new(false),
            stopping: AtomicBool::new(false),
            state: Mutex::new("idle".into()),
            status: Mutex::new("Starting...".into()),
            next_check: Mutex::new(None),
            last_result: Mutex::new(String::new()),
            wake: (Mutex::new(false), Condvar::new()),
            activity: Arc::new(Activity::default()),
            listener: Mutex::new(None),
        })
    }

    pub fn set_listener(&self, l: Listener) {
        *self.listener.lock().unwrap() = Some(l);
    }

    fn emit(&self, e: Event) {
        let l = self.listener.lock().unwrap().clone();
        if let Some(l) = l {
            l(e);
        }
    }

    pub fn cfg(&self) -> Option<Config> {
        self.cfg.lock().unwrap().clone()
    }
    pub fn state(&self) -> String {
        self.state.lock().unwrap().clone()
    }
    pub fn status(&self) -> String {
        self.status.lock().unwrap().clone()
    }
    pub fn next_check(&self) -> Option<DateTime<Local>> {
        *self.next_check.lock().unwrap()
    }
    pub fn is_paused(&self) -> bool {
        self.paused.load(SeqCst)
    }
    pub fn is_busy(&self) -> bool {
        self.busy.load(SeqCst)
    }

    pub fn set_status(&self, text: &str, state: Option<&str>) {
        *self.status.lock().unwrap() = text.to_string();
        if let Some(s) = state {
            *self.state.lock().unwrap() = s.into();
        }
        self.emit(Event::Changed);
    }

    pub fn notify(&self, title: &str, msg: &str) {
        if self.cfg().map(|c| c.notifications).unwrap_or(false) {
            self.emit(Event::Notify(title.into(), msg.into()));
        }
    }

    pub fn should_continue(&self) -> bool {
        !self.stopping.load(SeqCst) && !self.paused.load(SeqCst) && !self.cancel_current.load(SeqCst)
    }

    /// Stop the episode being processed now (it is marked skipped) and continue with the next.
    pub fn cancel_current(&self) {
        if self.activity.has_current() {
            self.cancel_current.store(true, SeqCst);
            self.set_status("Stopping current episode...", Some("busy"));
        }
    }

    /// Re-read config.yaml now (the window calls this after saving). Returns an error message.
    pub fn reload_config(&self) -> Option<String> {
        match load_config(Some(&self.cfg_path)) {
            Ok(c) => {
                *self.cfg.lock().unwrap() = Some(c);
                self.emit(Event::Changed);
                None
            }
            Err(e) => Some(e.to_string()),
        }
    }

    fn add_request(&self, folder: &str, guid: &str, title: &str, action: &str) -> bool {
        let Some(cfg) = self.cfg() else { return false };
        if let Err(e) = DownloadRequests::new(&cfg.output_dir).add(folder, guid, title, action) {
            log::error!("Could not save your choice: {e}");
            return false;
        }
        true
    }

    pub fn request_episode(&self, folder: &str, guid: &str, title: &str) {
        if self.add_request(folder, guid, title, "download") {
            log::info!("Requested download: {folder} / {title}");
            self.requests_waiting.store(true, SeqCst);
            self.emit(Event::Changed);
            if !self.is_busy() {
                self.check_now();
            }
        }
    }

    /// Download and process an already-finished episode again (current model and ad rules).
    pub fn reanalyze_episode(&self, folder: &str, guid: &str, title: &str) {
        if self.add_request(folder, guid, title, "reanalyze") {
            log::info!("Re-analysis requested: {folder} / {title}");
            self.requests_waiting.store(true, SeqCst);
            self.emit(Event::Changed);
            if !self.is_busy() {
                self.check_now();
            }
        }
    }

    /// Hidden from the lists at once; recorded as skipped at the next check (no download).
    pub fn skip_episode(&self, folder: &str, guid: &str, title: &str) {
        if self.add_request(folder, guid, title, "skip") {
            log::info!("Skipped: {folder} / {title}");
            self.emit(Event::Changed);
        }
    }

    /// Save the order you arranged Up next in; the next episode started follows it.
    pub fn set_queue_order(&self, keys: &[String]) {
        let Some(cfg) = self.cfg() else { return };
        if let Err(e) = QueueOrder::new(&cfg.output_dir).save(keys) {
            log::warn!("Could not save the queue order: {e}");
        }
        self.activity.reorder(keys);
        self.emit(Event::Changed);
    }

    fn wake(&self) {
        let (m, c) = &self.wake;
        *m.lock().unwrap() = true;
        c.notify_all();
    }

    pub fn check_now(&self) {
        self.paused.store(false, SeqCst);
        self.wake();
    }

    /// Finish the current episode, then pause instead of starting the next one.
    pub fn toggle_stop_after(&self) {
        let v = !self.stop_after_current.load(SeqCst);
        self.stop_after_current.store(v, SeqCst);
        log::info!("Stop after this episode: {}", if v { "on" } else { "off" });
        self.emit(Event::Changed);
    }

    /// Pause immediately. The current episode is interrupted and redone when you resume.
    pub fn stop_now(&self) {
        self.stop_after_current.store(false, SeqCst);
        self.paused.store(true, SeqCst);
        log::info!("Stop now requested");
        self.set_status("Stopping...", Some("paused"));
    }

    pub fn toggle_pause(&self) {
        let p = !self.paused.load(SeqCst);
        self.paused.store(p, SeqCst);
        if p {
            let extra = if self.is_busy() { " (finishing current step...)" } else { "" };
            self.set_status(&format!("Paused{extra}"), Some("paused"));
        } else {
            self.wake();
        }
    }

    pub fn stop(&self) {
        self.stopping.store(true, SeqCst);
        self.wake();
    }

    pub fn start(self: &Arc<Self>) -> std::thread::JoinHandle<()> {
        let me = self.clone();
        std::thread::Builder::new().name("worker".into()).spawn(move || me.run()).unwrap()
    }

    fn run(self: Arc<Self>) {
        let mut first = true;
        while !self.stopping.load(SeqCst) {
            let started = Instant::now();
            match load_config(Some(&self.cfg_path)) {
                Ok(c) => {
                    setup_logging(Some(&c), false);
                    *self.cfg.lock().unwrap() = Some(c);
                }
                Err(e) => {
                    log::error!("{e}");
                    self.set_status(&prefix(&format!("Config error: {e}"), 120), Some("error"));
                    self.notify("Config error", &e.to_string());
                }
            }
            let cfg = self.cfg();
            let run_now = !(first && cfg.as_ref().is_some_and(|c| !c.check_on_startup));
            first = false;
            if let Some(cfg) = &cfg {
                if !self.is_paused() && run_now {
                    self.cycle(cfg);
                }
            }
            let interval = cfg.as_ref().map(|c| c.check_interval_minutes).unwrap_or(60.0) * 60.0;
            // Next check is `interval` after this one STARTED; if the work took longer, check right away.
            let mut wait = (interval - started.elapsed().as_secs_f64()).max(0.0);
            if self.requests_waiting.load(SeqCst) && !self.is_paused() {
                wait = 0.0; // episodes were picked while busy - get them now
            }
            let next = Local::now() + chrono::Duration::milliseconds((wait * 1000.0) as i64);
            *self.next_check.lock().unwrap() = Some(next);
            if self.is_paused() {
                self.set_status("Paused", Some("paused"));
            } else if self.state() != "error" {
                let last = self.last_result.lock().unwrap().clone();
                self.set_status(&format!("Idle - next check {}{last}", next.format("%H:%M")), Some("idle"));
            }
            let (m, c) = &self.wake;
            let g = m.lock().unwrap();
            let (mut g, _) = c.wait_timeout_while(g, Duration::from_secs_f64(wait), |woken| !*woken).unwrap();
            *g = false;
        }
    }

    fn cycle(self: &Arc<Self>, cfg: &Config) {
        self.busy.store(true, SeqCst);
        self.requests_waiting.store(false, SeqCst);
        self.set_status("Checking feeds...", Some("busy"));
        self.cancel_current.store(false, SeqCst);
        let (a, b, c, d, e) = (self.clone(), self.clone(), self.clone(), self.clone(), self.clone());
        let hooks = Hooks {
            status: Arc::new(move |m| a.set_status(m, Some("busy"))),
            notify: Arc::new(move |t, m| b.notify(t, m)),
            should_continue: Arc::new(move || c.should_continue()),
            take_cancel: Arc::new(move || d.cancel_current.swap(false, SeqCst)),
            job_finished: Arc::new(move || {
                if e.stop_after_current.swap(false, SeqCst) {
                    e.paused.store(true, SeqCst);
                    e.set_status("Paused after finishing the episode", Some("paused"));
                }
            }),
            activity: self.activity.clone(),
        };
        let cfg2 = cfg.clone();
        let res = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| pipeline::run_cycle(&cfg2, &hooks)));
        match res {
            Ok(res) => {
                let mut parts = vec![];
                if !res.processed.is_empty() {
                    parts.push(format!("{} cleaned", res.processed.len()));
                }
                if !res.failed.is_empty() {
                    parts.push(format!("{} failed", res.failed.len()));
                }
                if !res.errors.is_empty() {
                    parts.push(format!("{} error(s)", res.errors.len()));
                }
                *self.last_result.lock().unwrap() = if parts.is_empty() { String::new() } else { format!(" (last: {})", parts.join(", ")) };
                if !res.errors.is_empty() && res.processed.is_empty() {
                    self.set_status(&format!("{} - see log", prefix(&format!("Problem: {}", res.errors[0]), 120)), Some("error"));
                } else {
                    *self.state.lock().unwrap() = "idle".into();
                }
            }
            Err(_) => {
                log::error!("Check failed (internal error)");
                self.set_status("Error: the check failed - see log", Some("error"));
            }
        }
        self.busy.store(false, SeqCst);
        if self.stop_after_current.swap(false, SeqCst) {
            // the episode it was waiting for was the last one
            self.paused.store(true, SeqCst);
        }
    }
}

// ---------------- helpers ----------------

pub fn desktop_notify(title: &str, msg: &str) {
    let mut n = notify_rust::Notification::new();
    n.summary(&prefix(title, 60)).body(&prefix(msg, 250));
    #[cfg(windows)]
    n.app_id("PodcastAdCleaner");
    let _ = n.show();
}

/// True if no other tray instance is running (Windows named mutex).
pub fn single_instance() -> bool {
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::Foundation::{GetLastError, ERROR_ALREADY_EXISTS};
        use windows_sys::Win32::System::Threading::CreateMutexW;
        let name: Vec<u16> = "Local\\PodcastAdCleanerTray\0".encode_utf16().collect();
        CreateMutexW(std::ptr::null(), 0, name.as_ptr());
        return GetLastError() != ERROR_ALREADY_EXISTS;
    }
    #[allow(unreachable_code)]
    true
}

/// On first run, put the example config next to the app (and optionally open it).
pub fn create_config_if_missing(path: &Path, open_editor: bool) -> bool {
    if path.exists() {
        return false;
    }
    let example = APP_DIR.join("config.example.yaml");
    let text = std::fs::read_to_string(&example).unwrap_or_else(|_| include_str!("../config.example.yaml").to_string());
    if let Some(d) = path.parent() {
        let _ = std::fs::create_dir_all(d);
    }
    if std::fs::write(path, text).is_ok() {
        if open_editor {
            crate::utils::open_path(path);
        }
        return true;
    }
    false
}

// ---------------- Windows startup shortcut ----------------

const STARTUP_LNK: &str = "Podcast Ad Cleaner.lnk";

fn startup_path() -> Option<PathBuf> {
    if !cfg!(windows) {
        return None;
    }
    let appdata = std::env::var_os("APPDATA")?;
    Some(PathBuf::from(appdata).join("Microsoft").join("Windows").join("Start Menu").join("Programs").join("Startup").join(STARTUP_LNK))
}

pub fn startup_enabled() -> bool {
    startup_path().is_some_and(|p| p.exists())
}

pub fn set_startup(enabled: bool) {
    let Some(p) = startup_path() else { return };
    if !enabled {
        let _ = std::fs::remove_file(&p);
        return;
    }
    let exe = std::env::current_exe().unwrap_or_default();
    let q = |s: &Path| s.display().to_string().replace('\'', "''");
    let ps = format!(
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{}');$s.TargetPath='{}';$s.Arguments='tray --minimized';$s.WorkingDirectory='{}';$s.Save()",
        q(&p), q(&exe), q(&APP_DIR)
    );
    let _ = crate::utils::command("powershell").args(["-NoProfile", "-Command", &ps]).status();
}

/// Run without a window: the worker in this thread's console (used by `tray --no-gui`
/// in builds without a tray icon, and as the fallback when no display is available).
pub fn run_headless(cfg_path: PathBuf) -> i32 {
    let cfg = load_config(Some(&cfg_path)).ok();
    setup_logging(cfg.as_ref(), true);
    if let Some(c) = &cfg {
        pipeline::clean_temp(c);
    }
    let worker = Worker::new(cfg_path, cfg.clone());
    worker.set_listener(Arc::new(|e| {
        if let Event::Notify(t, m) = e {
            desktop_notify(&t, &m);
        }
    }));
    let w2 = worker.clone();
    let server = crate::server::FeedServer::new(Arc::new(move || w2.cfg()));
    if let Some(c) = &cfg {
        if c.server.enabled {
            server.start(c);
        }
    }
    let h = worker.start();
    let _ = h.join();
    server.stop();
    0
}
