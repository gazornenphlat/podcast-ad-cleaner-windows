//! Small shared helpers: Windows-safe filenames, atomic writes, subprocess, time formatting.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::thread::sleep;
use std::time::Duration;

use chrono::{DateTime, FixedOffset, Local, NaiveDate, NaiveDateTime, TimeZone};
use once_cell::sync::Lazy;
use regex::Regex;

/// Raised when the user quits or pauses mid-episode.
#[derive(Debug, thiserror::Error)]
#[error("cancelled")]
pub struct Cancelled;

#[derive(Debug, Clone, PartialEq)]
pub struct Segment {
    pub start: f64,
    pub end: f64,
    pub text: String,
}

static INVALID: Lazy<Regex> = Lazy::new(|| Regex::new(r#"[<>:"/\\|?*\x00-\x1f]"#).unwrap());
static SPACES: Lazy<Regex> = Lazy::new(|| Regex::new(r"\s+").unwrap());

fn is_reserved(stem: &str) -> bool {
    let s = stem.to_uppercase();
    if matches!(s.as_str(), "CON" | "PRN" | "AUX" | "NUL") {
        return true;
    }
    for p in ["COM", "LPT"] {
        if let Some(rest) = s.strip_prefix(p) {
            if rest.len() == 1 && ('1'..='9').contains(&rest.chars().next().unwrap()) {
                return true;
            }
        }
    }
    false
}

fn trim_dots_spaces(s: &str) -> &str {
    s.trim_end_matches(['.', ' '])
}

/// Python-compatible: same folder/file names as the original app.
pub fn safe_filename(name: &str, max_len: usize) -> String {
    let n = INVALID.replace_all(name, " ");
    let n = SPACES.replace_all(&n, " ");
    let mut n = trim_dots_spaces(n.trim()).to_string();
    if is_reserved(n.split('.').next().unwrap_or("")) {
        n = format!("_{n}");
    }
    if n.chars().count() > max_len {
        let cut: String = n.chars().take(max_len).collect();
        n = trim_dots_spaces(&cut).to_string();
    }
    if n.is_empty() {
        "untitled".into()
    } else {
        n
    }
}

fn is_permission(e: &std::io::Error) -> bool {
    e.kind() == std::io::ErrorKind::PermissionDenied
}

/// Rename, retried for ~15 s. On Windows the swap fails with 'Access is denied' while any
/// other program (antivirus, backup/sync tools, Excel) has the file open.
pub fn replace_with_retry(src: &Path, dst: &Path) -> std::io::Result<()> {
    let attempts = 25;
    for i in 0..attempts {
        match fs::rename(src, dst) {
            Ok(()) => return Ok(()),
            Err(e) if is_permission(&e) => {
                if i == 0 {
                    if let Ok(meta) = fs::metadata(dst) {
                        let mut p = meta.permissions();
                        #[allow(clippy::permissions_set_readonly_false)]
                        p.set_readonly(false);
                        let _ = fs::set_permissions(dst, p);
                    }
                }
                if i == attempts - 1 {
                    return Err(e);
                }
                sleep(Duration::from_millis((50 * (i as u64 + 1)).min(1000)));
            }
            Err(e) => return Err(e),
        }
    }
    unreachable!()
}

/// Read a file, retrying briefly if it's locked by another program.
pub fn read_text_retry(path: &Path) -> std::io::Result<String> {
    let attempts = 10;
    for i in 0..attempts {
        match fs::read(path) {
            Ok(b) => return Ok(String::from_utf8_lossy(&b).into_owned()),
            Err(e) if is_permission(&e) && i < attempts - 1 => {
                sleep(Duration::from_millis(50 * (i as u64 + 1)))
            }
            Err(e) => return Err(e),
        }
    }
    unreachable!()
}

pub fn atomic_write_text(path: &Path, text: &str) -> std::io::Result<()> {
    let dir = path.parent().unwrap_or(Path::new("."));
    fs::create_dir_all(dir)?;
    let suffix = path.extension().map(|e| format!(".{}", e.to_string_lossy())).unwrap_or_default();
    let mut tmp = tempfile::Builder::new().prefix(".tmp-").suffix(&suffix).tempfile_in(dir)?;
    tmp.write_all(text.as_bytes())?;
    tmp.flush()?;
    let (_f, tmp_path) = tmp.keep().map_err(|e| e.error)?;
    drop(_f);
    if let Err(e) = replace_with_retry(&tmp_path, path) {
        let _ = fs::remove_file(&tmp_path);
        return Err(e);
    }
    Ok(())
}

/// A Command that never flashes a console window on Windows.
pub fn command(program: &str) -> Command {
    #[allow(unused_mut)]
    let mut c = Command::new(program);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        c.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    c
}

pub fn run(program: &str, args: &[String]) -> std::io::Result<Output> {
    command(program).args(args).stdin(Stdio::null()).output()
}

pub fn fmt_hms(seconds: f64, millis: bool) -> String {
    let seconds = seconds.max(0.0);
    let h = (seconds / 3600.0).floor();
    let rem = seconds - h * 3600.0;
    let m = (rem / 60.0).floor();
    let s = rem - m * 60.0;
    if millis {
        format!("{:02}:{:02}:{:06.3}", h as i64, m as i64, s)
    } else {
        format!("{:02}:{:02}:{:02}", h as i64, m as i64, s.floor() as i64)
    }
}

pub fn hms(seconds: f64) -> String {
    fmt_hms(seconds, false)
}

pub fn fmt_duration(seconds: f64) -> String {
    let s = seconds.max(0.0).round() as i64;
    let (h, rem) = (s / 3600, s % 3600);
    let (m, s) = (rem / 60, rem % 60);
    if h > 0 {
        format!("{h}h {m:02}m {s:02}s")
    } else {
        format!("{m}m {s:02}s")
    }
}

/// Parse '1:02:03.5', '02:03', '123.4' into seconds.
pub fn parse_timestamp(value: &str) -> Option<f64> {
    let v = value.trim();
    if v.is_empty() {
        return None;
    }
    let mut total = 0.0;
    for p in v.split(':') {
        let x: f64 = p.trim().parse().ok()?;
        total = total * 60.0 + x;
    }
    Some(total)
}

pub fn parse_timestamp_value(v: &serde_json::Value) -> Option<f64> {
    match v {
        serde_json::Value::Number(n) => n.as_f64(),
        serde_json::Value::String(s) => parse_timestamp(s),
        _ => None,
    }
}

// ---------------- dates (compatible with Python's isoformat / fromisoformat) ----------------

/// Like Python's datetime.now().isoformat(timespec="seconds"): naive local time.
pub fn now_iso() -> String {
    Local::now().format("%Y-%m-%dT%H:%M:%S").to_string()
}

/// Python's aware datetime.isoformat() for a UTC time: "2026-10-08T10:00:00+00:00".
pub fn iso_utc(dt: &DateTime<FixedOffset>) -> String {
    dt.format("%Y-%m-%dT%H:%M:%S%:z").to_string()
}

pub enum IsoTime {
    Aware(DateTime<FixedOffset>),
    Naive(NaiveDateTime),
}

impl IsoTime {
    /// As local naive time (how the window shows it).
    pub fn local_naive(&self) -> NaiveDateTime {
        match self {
            IsoTime::Aware(d) => d.with_timezone(&Local).naive_local(),
            IsoTime::Naive(n) => *n,
        }
    }
    /// As an aware time; naive values are taken as local time.
    pub fn aware(&self) -> DateTime<FixedOffset> {
        match self {
            IsoTime::Aware(d) => *d,
            IsoTime::Naive(n) => Local
                .from_local_datetime(n)
                .earliest()
                .unwrap_or_else(|| Local::now())
                .fixed_offset(),
        }
    }
}

/// Python datetime.fromisoformat subset.
pub fn parse_iso(value: &str) -> Option<IsoTime> {
    let v = value.trim();
    if v.is_empty() {
        return None;
    }
    if let Ok(d) = DateTime::parse_from_rfc3339(v) {
        return Some(IsoTime::Aware(d));
    }
    for f in ["%Y-%m-%dT%H:%M:%S%.f%:z", "%Y-%m-%d %H:%M:%S%.f%:z", "%Y-%m-%dT%H:%M%:z"] {
        if let Ok(d) = DateTime::parse_from_str(v, f) {
            return Some(IsoTime::Aware(d));
        }
    }
    for f in ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"] {
        if let Ok(d) = NaiveDateTime::parse_from_str(v, f) {
            return Some(IsoTime::Naive(d));
        }
    }
    if let Ok(d) = NaiveDate::parse_from_str(v, "%Y-%m-%d") {
        return Some(IsoTime::Naive(d.and_hms_opt(0, 0, 0).unwrap()));
    }
    None
}

/// First `n` characters (Python's s[:n]).
pub fn prefix(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// Last `n` characters (Python's s[-n:]).
pub fn suffix(s: &str, n: usize) -> String {
    let c = s.chars().count();
    s.chars().skip(c.saturating_sub(n)).collect()
}

/// Collapse whitespace runs, like " ".join(s.split()).
pub fn squash(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

pub fn open_path(path: &Path) {
    #[cfg(windows)]
    {
        let _ = command("explorer").arg(path).spawn();
    }
    #[cfg(target_os = "macos")]
    {
        let _ = Command::new("open").arg(path).spawn();
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        let _ = Command::new("xdg-open").arg(path).spawn();
    }
}

pub fn open_url(url: &str) {
    #[cfg(windows)]
    {
        let _ = command("cmd").args(["/C", "start", "", url]).spawn();
    }
    #[cfg(not(windows))]
    {
        open_path(Path::new(url));
    }
}

pub fn home_dir() -> PathBuf {
    dirs::home_dir().unwrap_or_else(|| PathBuf::from("."))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn filenames() {
        assert_eq!(safe_filename("a: b/c?", 100), "a b c");
        assert_eq!(safe_filename("CON.txt", 100), "_CON.txt");
        assert_eq!(safe_filename("  ...  ", 100), "untitled");
        assert_eq!(safe_filename("abc. def", 4), "abc");
    }

    #[test]
    fn times() {
        assert_eq!(fmt_hms(3725.5, false), "01:02:05");
        assert_eq!(fmt_hms(3725.5, true), "01:02:05.500");
        assert_eq!(fmt_duration(65.0), "1m 05s");
        assert_eq!(fmt_duration(3725.0), "1h 02m 05s");
        assert_eq!(parse_timestamp("1:02:03.5"), Some(3723.5));
        assert_eq!(parse_timestamp("x"), None);
        assert!(parse_iso("2026-10-08T10:00:00+00:00").is_some());
        assert!(parse_iso("2026-10-08T10:00:00").is_some());
    }
}
