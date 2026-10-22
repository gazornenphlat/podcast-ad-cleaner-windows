//! Log to a rotating file (5 MB x 5) and optionally the console, in the same format as before:
//!     2026-10-08 10:00:00 INFO    podcast_cleaner::pipeline: message

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use chrono::Local;
use log::{Level, LevelFilter, Log, Metadata, Record};

use crate::config::Config;

const MAX_BYTES: u64 = 5_000_000;
const BACKUPS: usize = 5;

struct Inner {
    file: Option<(PathBuf, File)>,
    console: bool,
}

struct Logger {
    inner: Mutex<Inner>,
}

static LOGGER: Logger = Logger { inner: Mutex::new(Inner { file: None, console: false }) };

fn rotate(path: &Path) {
    for i in (1..BACKUPS).rev() {
        let _ = std::fs::rename(format!("{}.{i}", path.display()), format!("{}.{}", path.display(), i + 1));
    }
    let _ = std::fs::rename(path, format!("{}.1", path.display()));
}

impl Log for Logger {
    fn enabled(&self, m: &Metadata) -> bool {
        m.level() <= log::max_level()
    }

    fn log(&self, r: &Record) {
        if !self.enabled(r.metadata()) {
            return;
        }
        let target = r.target();
        // whisper.cpp / http internals are noisy
        if r.level() > Level::Warn && !target.starts_with("podcast_ad_cleaner") && !target.starts_with("PodcastAdCleaner") {
            return;
        }
        let level = match r.level() {
            Level::Warn => "WARNING",
            l => l.as_str(),
        };
        let name = target.replace("podcast_ad_cleaner", "podcast_cleaner");
        let line = format!("{} {level:<7} {name}: {}\n", Local::now().format("%Y-%m-%d %H:%M:%S"), r.args());
        let mut g = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if g.console {
            eprint!("{line}");
        }
        let mut reopen = None;
        if let Some((path, f)) = g.file.as_mut() {
            let _ = f.write_all(line.as_bytes());
            if f.metadata().map(|m| m.len() > MAX_BYTES).unwrap_or(false) {
                reopen = Some(path.clone());
            }
        }
        if let Some(path) = reopen {
            g.file = None;
            rotate(&path);
            if let Ok(f) = OpenOptions::new().create(true).append(true).open(&path) {
                g.file = Some((path, f));
            }
        }
    }

    fn flush(&self) {
        if let Some((_, f)) = self.inner.lock().unwrap().file.as_mut() {
            let _ = f.flush();
        }
    }
}

/// (Re)configure logging; safe to call repeatedly (each check re-reads config.yaml).
pub fn setup_logging(cfg: Option<&Config>, console: bool) {
    let _ = log::set_logger(&LOGGER);
    let level = match cfg.map(|c| c.log_level.to_uppercase()).as_deref() {
        Some("DEBUG") => LevelFilter::Debug,
        Some("WARNING") | Some("WARN") => LevelFilter::Warn,
        Some("ERROR") | Some("CRITICAL") => LevelFilter::Error,
        _ => LevelFilter::Info,
    };
    log::set_max_level(level);
    let mut g = LOGGER.inner.lock().unwrap();
    g.console = console;
    if let Some(cfg) = cfg {
        let same = g.file.as_ref().is_some_and(|(p, _)| p == &cfg.log_file);
        if !same {
            if let Some(dir) = cfg.log_file.parent() {
                let _ = std::fs::create_dir_all(dir);
            }
            g.file = OpenOptions::new().create(true).append(true).open(&cfg.log_file).ok().map(|f| (cfg.log_file.clone(), f));
        }
    }
}
