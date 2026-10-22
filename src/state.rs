//! Per-podcast record of processed episodes (.podcast-state.json in each podcast folder),
//! manual download requests, and the "Up next" order. File formats match the Python version.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::SystemTime;

use chrono::Local;
use once_cell::sync::Lazy;
use serde_json::{json, Map, Value};

use crate::utils::{atomic_write_text, now_iso, read_text_retry, replace_with_retry, squash};

pub const STATE_FILENAME: &str = ".podcast-state.json";

pub fn s<'a>(m: &'a Map<String, Value>, k: &str) -> &'a str {
    m.get(k).and_then(Value::as_str).unwrap_or("")
}

pub fn num(m: &Map<String, Value>, k: &str) -> f64 {
    match m.get(k) {
        Some(Value::Number(n)) => n.as_f64().unwrap_or(0.0),
        Some(Value::String(s)) => s.parse().unwrap_or(0.0),
        Some(Value::Bool(b)) => *b as i32 as f64,
        _ => 0.0,
    }
}

pub fn truthy(m: &Map<String, Value>, k: &str) -> bool {
    match m.get(k) {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

#[derive(Debug, Clone)]
pub struct PodcastState {
    pub path: PathBuf,
    pub data: Map<String, Value>,
}

impl PodcastState {
    pub fn empty(folder: &Path) -> Self {
        let mut data = Map::new();
        data.insert("feed_url".into(), Value::Null);
        data.insert("episodes".into(), Value::Object(Map::new()));
        Self { path: folder.join(STATE_FILENAME), data }
    }

    /// A locked/unreadable file is an error (the caller decides); it must NOT be treated as corrupt.
    pub fn load(folder: &Path, repair: bool) -> anyhow::Result<Self> {
        let mut st = Self::empty(folder);
        if st.path.exists() {
            let text = read_text_retry(&st.path)?;
            match serde_json::from_str::<Value>(&text) {
                Ok(Value::Object(m)) => {
                    for (k, v) in m {
                        st.data.insert(k, v);
                    }
                }
                Ok(_) => {}
                Err(e) => {
                    if !repair {
                        return Err(e.into());
                    }
                    let backup = st.path.with_file_name(format!(
                        ".podcast-state.corrupt-{}.json",
                        Local::now().format("%Y%m%d-%H%M%S")
                    ));
                    let _ = replace_with_retry(&st.path, &backup);
                }
            }
        }
        if !st.data.get("episodes").is_some_and(Value::is_object) {
            st.data.insert("episodes".into(), Value::Object(Map::new()));
        }
        Ok(st)
    }

    pub fn episodes(&self) -> &Map<String, Value> {
        self.data["episodes"].as_object().unwrap()
    }

    pub fn episodes_mut(&mut self) -> &mut Map<String, Value> {
        self.data.get_mut("episodes").unwrap().as_object_mut().unwrap()
    }

    pub fn episode(&self, guid: &str) -> Option<&Map<String, Value>> {
        self.episodes().get(guid).and_then(Value::as_object)
    }

    pub fn is_new(&self) -> bool {
        self.episodes().is_empty()
    }

    pub fn feed_url(&self) -> Option<String> {
        self.data.get("feed_url").and_then(Value::as_str).map(String::from)
    }

    pub fn set_feed_url(&mut self, url: &str) -> anyhow::Result<()> {
        self.data.insert("feed_url".into(), Value::String(url.into()));
        self.save()
    }

    pub fn status(&self, guid: &str) -> Option<String> {
        self.episode(guid).and_then(|e| e.get("status")).and_then(Value::as_str).map(String::from)
    }

    pub fn is_done(&self, guid: &str) -> bool {
        matches!(self.status(guid).as_deref(), Some("done") | Some("skipped"))
    }

    /// Episodes of a manual show the user can pick: available, queued or failed - newest first.
    pub fn choosable(&self, limit: usize) -> Vec<(String, Map<String, Value>)> {
        let mut eps: Vec<(String, Map<String, Value>)> = self
            .episodes()
            .iter()
            .filter_map(|(g, e)| e.as_object().map(|e| (g.clone(), e.clone())))
            .filter(|(_, e)| matches!(s(e, "status"), "available" | "queued" | "failed"))
            .collect();
        eps.sort_by(|a, b| s(&b.1, "published").cmp(s(&a.1, "published")));
        eps.truncate(limit);
        eps
    }

    pub fn attempts(&self, guid: &str) -> i64 {
        self.episode(guid).map(|e| num(e, "attempts") as i64).unwrap_or(0)
    }

    /// Update an episode's record. `info` values that are null are ignored (like Python's None).
    pub fn mark(&mut self, guid: &str, status: &str, save: bool, info: Value) -> anyhow::Result<()> {
        let eps = self.episodes_mut();
        let ep = eps.entry(guid.to_string()).or_insert_with(|| Value::Object(Map::new()));
        if !ep.is_object() {
            *ep = Value::Object(Map::new());
        }
        let ep = ep.as_object_mut().unwrap();
        if let Value::Object(info) = info {
            for (k, v) in info {
                if !v.is_null() {
                    ep.insert(k, v);
                }
            }
        }
        ep.insert("status".into(), Value::String(status.into()));
        ep.insert("updated".into(), Value::String(now_iso()));
        if status == "failed" {
            let n = num(ep, "attempts") as i64 + 1;
            ep.insert("attempts".into(), json!(n));
        }
        if save {
            self.save()?;
        }
        Ok(())
    }

    // ---- sponsors heard on this show ----
    pub fn record_sponsors(&mut self, names: &[String]) {
        if !self.data.get("sponsors").is_some_and(Value::is_object) {
            self.data.insert("sponsors".into(), Value::Object(Map::new()));
        }
        let sponsors = self.data.get_mut("sponsors").unwrap().as_object_mut().unwrap();
        let today = Local::now().format("%Y-%m-%d").to_string();
        for raw in names {
            let name = squash(raw);
            let name = name.trim_matches([' ', '.', ',', ':', ';', '-']).to_string();
            let lower = name.to_lowercase();
            if name.is_empty()
                || ["advertisement", "ad", "ads", "sponsor", "unknown"].contains(&lower.as_str())
                || name.chars().count() > 60
            {
                continue;
            }
            let key = sponsors.keys().find(|k| k.to_lowercase() == lower).cloned().unwrap_or(name);
            let entry = sponsors.entry(key).or_insert_with(|| json!({"count": 0}));
            if let Some(e) = entry.as_object_mut() {
                let c = num(e, "count") as i64 + 1;
                e.insert("count".into(), json!(c));
                e.insert("last_seen".into(), json!(today));
            }
        }
    }

    pub fn known_sponsors(&self, limit: usize) -> Vec<String> {
        let Some(Value::Object(sp)) = self.data.get("sponsors") else { return vec![] };
        let mut ranked: Vec<(&String, String, f64)> = sp
            .iter()
            .map(|(k, v)| {
                let m = v.as_object().cloned().unwrap_or_default();
                (k, s(&m, "last_seen").to_string(), num(&m, "count"))
            })
            .collect();
        ranked.sort_by(|a, b| (&b.1, b.2).partial_cmp(&(&a.1, a.2)).unwrap_or(std::cmp::Ordering::Equal));
        ranked.into_iter().take(limit).map(|x| x.0.clone()).collect()
    }

    pub fn save(&self) -> anyhow::Result<()> {
        atomic_write_text(&self.path, &to_json_pretty(&Value::Object(self.data.clone()), 2))?;
        Ok(())
    }
}

/// json.dumps(indent=n, ensure_ascii=False)
pub fn to_json_pretty(v: &Value, indent: usize) -> String {
    let ind = vec![b' '; indent];
    let fmt = serde_json::ser::PrettyFormatter::with_indent(&ind);
    let mut buf = Vec::new();
    let mut ser = serde_json::Serializer::with_formatter(&mut buf, fmt);
    serde::Serialize::serialize(v, &mut ser).unwrap();
    String::from_utf8(buf).unwrap()
}

// ---------------- read-only cached view for the window, tray and feed server ----------------

type PeekKey = (SystemTime, u64);
static PEEK: Lazy<Mutex<HashMap<PathBuf, (PeekKey, Arc<PodcastState>)>>> = Lazy::new(Default::default);

/// Only re-reads the file when it changed; never raises or alters the file.
pub fn peek_state(folder: &Path) -> Arc<PodcastState> {
    let path = folder.join(STATE_FILENAME);
    let key = match std::fs::metadata(&path) {
        Ok(m) => (m.modified().unwrap_or(SystemTime::UNIX_EPOCH), m.len()),
        Err(_) => return Arc::new(PodcastState::empty(folder)),
    };
    let mut cache = PEEK.lock().unwrap();
    if let Some((k, st)) = cache.get(&path) {
        if *k == key {
            return st.clone();
        }
    }
    match PodcastState::load(folder, false) {
        Ok(st) => {
            let st = Arc::new(st);
            cache.insert(path, (key, st.clone()));
            st
        }
        Err(_) => cache.get(&path).map(|x| x.1.clone()).unwrap_or_else(|| Arc::new(PodcastState::empty(folder))),
    }
}

// ---------------- manual download requests ----------------

static REQUESTS_LOCK: Lazy<Mutex<()>> = Lazy::new(|| Mutex::new(()));

#[derive(Debug, Clone)]
pub struct Request {
    pub folder: String,
    pub guid: String,
    pub title: String,
    pub action: String,
}

/// Episodes the user picked to download, skip or re-analyze: <output_dir>/.download-requests.json
pub struct DownloadRequests {
    pub path: PathBuf,
}

pub const REQUESTS_FILENAME: &str = ".download-requests.json";

impl DownloadRequests {
    pub fn new(output_dir: &Path) -> Self {
        Self { path: output_dir.join(REQUESTS_FILENAME) }
    }

    fn load_raw(&self) -> Vec<Map<String, Value>> {
        let Ok(text) = std::fs::read_to_string(&self.path) else { return vec![] };
        match serde_json::from_str::<Value>(&text) {
            Ok(Value::Array(a)) => a
                .into_iter()
                .filter_map(|v| match v {
                    Value::Object(m) if !s(&m, "folder").is_empty() && !s(&m, "guid").is_empty() => Some(m),
                    _ => None,
                })
                .collect(),
            _ => vec![],
        }
    }

    fn to_req(m: &Map<String, Value>) -> Request {
        let action = s(m, "action");
        Request {
            folder: s(m, "folder").into(),
            guid: s(m, "guid").into(),
            title: s(m, "title").into(),
            action: if action.is_empty() { "download".into() } else { action.into() },
        }
    }

    pub fn all(&self) -> Vec<Request> {
        let _g = REQUESTS_LOCK.lock().unwrap();
        self.load_raw().iter().map(Self::to_req).collect()
    }

    pub fn by_action(&self, action: &str) -> Vec<Request> {
        self.all().into_iter().filter(|r| r.action == action).collect()
    }

    pub fn downloads(&self) -> Vec<Request> {
        self.by_action("download")
    }
    pub fn skips(&self) -> Vec<Request> {
        self.by_action("skip")
    }
    pub fn reanalyses(&self) -> Vec<Request> {
        self.by_action("reanalyze")
    }

    /// Record a choice; a later choice for the same episode replaces the earlier one.
    pub fn add(&self, folder: &str, guid: &str, title: &str, action: &str) -> anyhow::Result<bool> {
        let _g = REQUESTS_LOCK.lock().unwrap();
        let mut items = self.load_raw();
        if items.iter().any(|r| {
            let x = Self::to_req(r);
            x.folder == folder && x.guid == guid && x.action == action
        }) {
            return Ok(false);
        }
        items.retain(|r| !(s(r, "folder") == folder && s(r, "guid") == guid));
        let mut m = Map::new();
        m.insert("folder".into(), json!(folder));
        m.insert("guid".into(), json!(guid));
        m.insert("title".into(), json!(title));
        m.insert("action".into(), json!(action));
        m.insert("requested".into(), json!(now_iso()));
        items.push(m);
        let v = Value::Array(items.into_iter().map(Value::Object).collect());
        atomic_write_text(&self.path, &to_json_pretty(&v, 2))?;
        Ok(true)
    }

    pub fn remove(&self, pairs: &HashSet<(String, String)>) {
        if pairs.is_empty() {
            return;
        }
        let _g = REQUESTS_LOCK.lock().unwrap();
        let items: Vec<Value> = self
            .load_raw()
            .into_iter()
            .filter(|r| !pairs.contains(&(s(r, "folder").to_string(), s(r, "guid").to_string())))
            .map(Value::Object)
            .collect();
        if let Err(e) = atomic_write_text(&self.path, &to_json_pretty(&Value::Array(items), 2)) {
            log::warn!("Could not update {}: {e}", REQUESTS_FILENAME);
        }
    }
}

// ---------------- your preferred order for "Up next" ----------------

pub struct QueueOrder {
    pub path: PathBuf,
}

const QUEUE_MAX: usize = 1000;

impl QueueOrder {
    pub fn new(output_dir: &Path) -> Self {
        Self { path: output_dir.join(".queue-order.json") }
    }

    pub fn load(&self) -> Vec<String> {
        let Ok(text) = std::fs::read_to_string(&self.path) else { return vec![] };
        match serde_json::from_str::<Value>(&text) {
            Ok(Value::Array(a)) => {
                a.into_iter().filter_map(|v| v.as_str().map(String::from)).take(QUEUE_MAX).collect()
            }
            _ => vec![],
        }
    }

    pub fn save(&self, keys: &[String]) -> anyhow::Result<()> {
        let _g = REQUESTS_LOCK.lock().unwrap();
        let mut seen = HashSet::new();
        let uniq: Vec<Value> = keys
            .iter()
            .filter(|k| seen.insert((*k).clone()))
            .take(QUEUE_MAX)
            .map(|k| Value::String(k.clone()))
            .collect();
        atomic_write_text(&self.path, &to_json_pretty(&Value::Array(uniq), 1))?;
        Ok(())
    }

    /// Order items: ones you placed, in your order; then new ones in their natural order.
    /// `front` items you haven't placed (e.g. a fresh re-analysis) go first.
    pub fn sort<T>(items: Vec<T>, key_of: impl Fn(&T) -> String, order: &[String], front: impl Fn(&T) -> bool) -> Vec<T> {
        let pos: HashMap<&str, usize> = order.iter().enumerate().map(|(i, k)| (k.as_str(), i)).collect();
        let n = order.len() as i64;
        let mut ranked: Vec<((i64, usize), T)> = items
            .into_iter()
            .enumerate()
            .map(|(i, item)| {
                let k = key_of(&item);
                let r = match pos.get(k.as_str()) {
                    Some(p) => (*p as i64, i),
                    None if front(&item) => (-1, i),
                    None => (n, i),
                };
                (r, item)
            })
            .collect();
        ranked.sort_by_key(|x| x.0);
        ranked.into_iter().map(|x| x.1).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn state_roundtrip() {
        let dir = tempfile::tempdir().unwrap();
        let mut st = PodcastState::load(dir.path(), true).unwrap();
        assert!(st.is_new());
        st.mark("g1", "failed", true, json!({"title": "T", "x": null})).unwrap();
        st.mark("g1", "failed", true, json!({})).unwrap();
        let st2 = PodcastState::load(dir.path(), true).unwrap();
        assert_eq!(st2.attempts("g1"), 2);
        assert_eq!(st2.status("g1").as_deref(), Some("failed"));
        assert!(!st2.episode("g1").unwrap().contains_key("x"));
        st.record_sponsors(&["Acme".into(), "acme".into(), "ad".into()]);
        assert_eq!(st.known_sponsors(5), vec!["Acme"]);
    }

    #[test]
    fn corrupt_is_backed_up() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join(STATE_FILENAME), "{oops").unwrap();
        assert!(PodcastState::load(dir.path(), false).is_err());
        let st = PodcastState::load(dir.path(), true).unwrap();
        assert!(st.is_new());
        assert!(!dir.path().join(STATE_FILENAME).exists());
    }

    #[test]
    fn requests_and_order() {
        let dir = tempfile::tempdir().unwrap();
        let r = DownloadRequests::new(dir.path());
        assert!(r.add("F", "g", "t", "download").unwrap());
        assert!(!r.add("F", "g", "t", "download").unwrap());
        assert!(r.add("F", "g", "t", "skip").unwrap());
        assert_eq!(r.all().len(), 1);
        assert_eq!(r.skips().len(), 1);
        let v = QueueOrder::sort(vec!["a", "b", "c", "d"], |x| x.to_string(), &["c".into(), "a".into()], |x| *x == "d");
        assert_eq!(v, vec!["d", "c", "a", "b"]);
    }
}
