//! Permanent history of every ad cut, across all podcasts: <output_dir>/ads.csv.
//!
//! One row per removed ad. Opens in Excel. The user can mark rows as "not an ad" from the
//! window; that feedback is fed back into the AI prompt, as is the list of sponsors seen on
//! any show (the "global sponsor list").

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::SystemTime;

use anyhow::Result;
use chrono::Local;
use once_cell::sync::Lazy;
use regex::Regex;

use crate::feeds::Ad;
use crate::utils::{atomic_write_text, now_iso, parse_timestamp, prefix, squash};

pub const FIELDS: [&str; 12] =
    ["id", "processed", "podcast", "episode", "published", "start", "end", "seconds", "sponsor", "found_by", "status", "excerpt"];
const GENERIC: [&str; 8] = ["advertisement", "ad", "ads", "sponsor", "sponsors", "unknown", "promo", "commercial"];
const NOT_AD_WORDS: [&str; 10] = ["not_ad", "not ad", "not an ad", "notad", "no", "false", "0", "n", "x", ""];

static LOCK: Lazy<Mutex<()>> = Lazy::new(|| Mutex::new(()));
static PREFIX: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)^(\[\s*ads?\s*\]|ads?\s*[:\-]|ad break\s*[:\-]?|sponsor(ed by)?\s*[:\-]|advertisement\s*[:\-]?)\s*").unwrap()
});

/// Merged ads carry labels like 'Acme; Other Co' - return the individual names.
pub fn split_sponsors(label: &str) -> Vec<String> {
    let mut out = vec![];
    for part in label.split(';') {
        let name = squash(part);
        let name = name.trim_matches([' ', '.', ',', ':', '-']);
        let name = PREFIX.replace(name, "");
        let name = name.trim_matches([' ', '.', ',', ':', '-']).to_string();
        if !name.is_empty() && !GENERIC.contains(&name.to_lowercase().as_str()) && name.chars().count() <= 60 {
            out.push(name);
        }
    }
    out
}

pub type RawRow = BTreeMap<String, String>;

#[derive(Debug, Clone)]
pub struct AdRow {
    pub raw: RawRow,
    pub start: f64,
    pub end: f64,
    pub seconds: f64,
}

impl AdRow {
    pub fn get(&self, k: &str) -> &str {
        self.raw.get(k).map(String::as_str).unwrap_or("")
    }
    pub fn is_not_ad(&self) -> bool {
        self.get("status") == "not_ad"
    }
}

/// Excel saves CSV as UTF-8 or as the Windows code page, depending on the option chosen.
fn decode(data: &[u8]) -> String {
    let data = data.strip_prefix(&[0xEF, 0xBB, 0xBF]).unwrap_or(data);
    match std::str::from_utf8(data) {
        Ok(s) => s.to_string(),
        Err(_) => encoding_rs::WINDOWS_1252.decode_without_bom_handling(data).0.into_owned(),
    }
}

fn parse(text: &str) -> Result<Vec<RawRow>> {
    let text = text.replace('\0', "");
    let first = text.split('\n').next().unwrap_or("");
    // Excel in some regions saves with ';' (or tabs) instead of ','
    let delim = [b',', b';', b'\t'].into_iter().max_by_key(|d| first.matches(*d as char).count()).unwrap();
    let delim = if first.matches(delim as char).count() == 0 { b',' } else { delim };
    let mut rdr = csv::ReaderBuilder::new().delimiter(delim).flexible(true).from_reader(text.as_bytes());
    let headers: Vec<String> = rdr.headers()?.iter().map(|h| h.trim().to_lowercase()).collect();
    let mut out = vec![];
    for rec in rdr.records() {
        let rec = rec?;
        let mut row = RawRow::new();
        for (i, v) in rec.iter().enumerate() {
            if let Some(h) = headers.get(i) {
                row.insert(h.clone(), v.trim().to_string());
            }
        }
        if row.values().any(|v| !v.is_empty()) {
            out.push(row);
        }
    }
    Ok(out)
}

fn as_int(v: &str) -> Option<i64> {
    v.trim().parse::<f64>().ok().filter(|f| f.is_finite()).map(|f| f.trunc() as i64)
}

fn normalise(mut rows: Vec<RawRow>) -> Vec<RawRow> {
    let mut used = HashSet::new();
    let mut next_id = rows.iter().filter_map(|r| r.get("id").and_then(|v| as_int(v))).max().unwrap_or(0).max(0);
    for r in rows.iter_mut() {
        for f in FIELDS {
            r.entry(f.to_string()).or_default();
        }
        let id = match as_int(&r["id"]) {
            Some(i) if i > 0 && !used.contains(&i.to_string()) => i,
            _ => {
                next_id += 1;
                next_id
            }
        };
        r.insert("id".into(), id.to_string());
        used.insert(id.to_string());
        let status = r["status"].trim().to_lowercase();
        let not_ad = (NOT_AD_WORDS.contains(&status.as_str()) && !status.is_empty()) || status.starts_with("not");
        r.insert("status".into(), if not_ad { "not_ad" } else { "ad" }.into());
    }
    rows
}

fn seconds(v: &str) -> f64 {
    match parse_timestamp(v) {
        Some(t) if t >= 0.0 => t,
        _ => 0.0,
    }
}

fn typed(r: RawRow) -> AdRow {
    let start = seconds(r.get("start").map(String::as_str).unwrap_or(""));
    let end = seconds(r.get("end").map(String::as_str).unwrap_or(""));
    let secs = seconds(r.get("seconds").map(String::as_str).unwrap_or(""));
    AdRow { start, end, seconds: if secs > 0.0 { secs } else { (end - start).max(0.0) }, raw: r }
}

fn round1(x: f64) -> String {
    let r = (x * 10.0).round() / 10.0;
    format!("{r:?}") // Python-style: 12.0, 3.5
}

#[derive(Debug, Clone, Default)]
pub struct SponsorStat {
    pub name: String,
    pub ads: usize,
    pub not_ads: usize,
    pub shows: HashSet<String>,
    pub last: String,
    pub seconds: f64,
}

type CacheKey = (SystemTime, u64);
static CACHE: Lazy<Mutex<HashMap<PathBuf, (CacheKey, Arc<Vec<AdRow>>)>>> = Lazy::new(Default::default);

pub struct AdLog {
    pub path: PathBuf,
}

impl AdLog {
    pub fn new(output_dir: &Path) -> Self {
        Self { path: output_dir.join("ads.csv") }
    }

    /// All rows, cleaned up. Never fails - a damaged or hand-edited file just gives what can be read.
    pub fn rows(&self) -> Arc<Vec<AdRow>> {
        let key = match std::fs::metadata(&self.path) {
            Ok(m) => (m.modified().unwrap_or(SystemTime::UNIX_EPOCH), m.len()),
            Err(_) => return Arc::new(vec![]),
        };
        let mut cache = CACHE.lock().unwrap();
        if let Some((k, rows)) = cache.get(&self.path) {
            if *k == key {
                return rows.clone();
            }
        }
        let loaded = {
            let _g = LOCK.lock().unwrap();
            self.load(false)
        };
        match loaded {
            Ok(raw) => {
                let rows = Arc::new(raw.into_iter().map(typed).collect::<Vec<_>>());
                cache.insert(self.path.clone(), (key, rows.clone()));
                rows
            }
            Err(e) => {
                log::warn!("Could not read ads.csv ({e}) - ignoring it for now");
                cache.get(&self.path).map(|x| x.1.clone()).unwrap_or_default()
            }
        }
    }

    /// strict=true is used before rewriting the file: if it exists but can't be opened (e.g. Excel has
    /// it locked) this fails instead of returning [], so the history is never overwritten with nothing.
    fn load(&self, strict: bool) -> Result<Vec<RawRow>> {
        if !self.path.exists() {
            return Ok(vec![]);
        }
        let data = match std::fs::read(&self.path) {
            Ok(d) => d,
            Err(e) if strict => return Err(e.into()),
            Err(_) => return Ok(vec![]),
        };
        match parse(&decode(&data)) {
            Ok(rows) => Ok(normalise(rows)),
            Err(e) if !strict => Err(e),
            Err(e) => {
                let backup = self.path.with_file_name(format!("ads.unreadable-{}.csv", Local::now().format("%Y%m%d-%H%M%S")));
                std::fs::rename(&self.path, &backup)?;
                log::warn!("ads.csv was damaged ({e}); kept it as {} and started a new one", backup.display());
                Ok(vec![])
            }
        }
    }

    fn write_all(&self, rows: &[RawRow]) -> Result<()> {
        let mut w = csv::WriterBuilder::new().terminator(csv::Terminator::CRLF).from_writer(vec![]);
        w.write_record(FIELDS)?;
        for r in rows {
            w.write_record(FIELDS.iter().map(|f| r.get(*f).map(String::as_str).unwrap_or("")))?;
        }
        let body = String::from_utf8(w.into_inner()?)?;
        // a BOM so Excel shows names with accents correctly
        atomic_write_text(&self.path, &format!("\u{feff}{body}"))?;
        Ok(())
    }

    /// Append this episode's ads. With replace (re-analysis), its earlier AI/feed rows are dropped
    /// first - but rows you marked 'not an ad' are kept, so your corrections aren't lost.
    pub fn add_episode(&self, podcast: &str, episode: &str, published: &str, ads: &[Ad], excerpts: &[String], replace: bool) -> Result<()> {
        if ads.is_empty() && !replace {
            return Ok(());
        }
        let _g = LOCK.lock().unwrap();
        let mut existing = self.load(true)?;
        if replace {
            existing.retain(|r| {
                !(r.get("podcast").map(String::as_str) == Some(podcast)
                    && r.get("episode").map(String::as_str) == Some(episode)
                    && r.get("status").map(String::as_str) != Some("not_ad"))
            });
        }
        let next_id = existing.iter().filter_map(|r| as_int(&r["id"])).max().unwrap_or(0) + 1;
        let now = now_iso();
        for (i, a) in ads.iter().enumerate() {
            let mut r = RawRow::new();
            r.insert("id".into(), (next_id + i as i64).to_string());
            r.insert("processed".into(), now.clone());
            r.insert("podcast".into(), podcast.into());
            r.insert("episode".into(), episode.into());
            r.insert("published".into(), prefix(published, 10));
            r.insert("start".into(), round1(a.start));
            r.insert("end".into(), round1(a.end));
            r.insert("seconds".into(), round1(a.end - a.start));
            r.insert("sponsor".into(), a.label.clone());
            r.insert("found_by".into(), a.source.clone());
            r.insert("status".into(), "ad".into());
            r.insert("excerpt".into(), prefix(excerpts.get(i).map(String::as_str).unwrap_or(""), 400));
            existing.push(r);
        }
        self.write_all(&existing)
    }

    pub fn set_status(&self, ids: &HashSet<String>, status: &str) -> Result<()> {
        let _g = LOCK.lock().unwrap();
        let mut rows = self.load(true)?;
        for r in rows.iter_mut() {
            if ids.contains(&r["id"]) {
                r.insert("status".into(), status.into());
            }
        }
        self.write_all(&rows)
    }

    /// Per sponsor (keyed by lowercase name).
    pub fn sponsor_stats(&self) -> HashMap<String, SponsorStat> {
        let mut stats: HashMap<String, SponsorStat> = HashMap::new();
        for r in self.rows().iter() {
            for name in split_sponsors(r.get("sponsor")) {
                let s = stats.entry(name.to_lowercase()).or_default();
                if s.name.is_empty() {
                    s.name = name.clone();
                }
                if r.is_not_ad() {
                    s.not_ads += 1;
                } else {
                    s.ads += 1;
                    s.seconds += r.seconds;
                    s.shows.insert(r.get("podcast").to_string());
                    let p = prefix(r.get("processed"), 10);
                    if p > s.last {
                        s.last = p;
                    }
                }
            }
        }
        stats
    }

    /// Advertisers confirmed on any show, most frequent / recent first.
    pub fn global_sponsors(&self, limit: usize) -> Vec<String> {
        let mut v: Vec<SponsorStat> =
            self.sponsor_stats().into_values().filter(|s| s.ads > 0 && s.ads > s.not_ads).collect();
        v.sort_by(|a, b| (b.ads + b.shows.len(), &b.last).cmp(&(a.ads + a.shows.len(), &a.last)));
        v.into_iter().take(limit).map(|s| s.name).collect()
    }

    /// Things the user said were NOT ads (and that were never confirmed as ads).
    pub fn not_ads(&self, limit: usize) -> Vec<String> {
        let mut v: Vec<SponsorStat> =
            self.sponsor_stats().into_values().filter(|s| s.not_ads > 0 && s.not_ads >= s.ads).collect();
        v.sort_by(|a, b| b.not_ads.cmp(&a.not_ads));
        v.into_iter().take(limit).map(|s| s.name).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ad(s: f64, e: f64, l: &str) -> Ad {
        Ad { start: s, end: e, label: l.into(), source: "ai".into() }
    }

    #[test]
    fn roundtrip() {
        let dir = tempfile::tempdir().unwrap();
        let log = AdLog::new(dir.path());
        log.add_episode("Show", "Ep", "2026-10-08T00:00:00+00:00", &[ad(1.0, 31.25, "Acme; Ad: Beta")], &["hi".into()], false).unwrap();
        log.add_episode("Show2", "Ep", "", &[ad(5.0, 40.0, "Acme")], &[], false).unwrap();
        let rows = log.rows();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].get("end"), "31.3");
        assert_eq!(log.global_sponsors(10)[0], "Acme");
        log.set_status(&HashSet::from(["2".to_string()]), "not_ad").unwrap();
        log.set_status(&HashSet::from(["1".to_string()]), "not_ad").unwrap();
        assert_eq!(log.not_ads(10).len(), 2);
        // re-analysis keeps "not an ad" rows
        log.add_episode("Show", "Ep", "", &[ad(2.0, 20.0, "Gamma")], &[], true).unwrap();
        assert_eq!(log.rows().len(), 3);
    }

    #[test]
    fn tolerant() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("ads.csv"), "Sponsor;Status;id\nAcme;not an ad;x\nBeta;;3\nGamma;ad;3\n").unwrap();
        let rows = AdLog::new(dir.path()).rows();
        assert_eq!(rows.len(), 3);
        assert!(rows[0].is_not_ad());
        let ids: HashSet<_> = rows.iter().map(|r| r.get("id").to_string()).collect();
        assert_eq!(ids.len(), 3);
        assert_eq!(split_sponsors("[Ad] Acme; Sponsor - Beta; ad"), vec!["Acme", "Beta"]);
    }
}
