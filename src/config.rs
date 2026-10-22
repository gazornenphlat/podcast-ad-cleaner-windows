//! Load and validate config.yaml, filling in defaults.

use std::path::{Path, PathBuf};

use once_cell::sync::Lazy;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::utils::home_dir;

/// config.yaml and logs/ live next to the executable.
pub static APP_DIR: Lazy<PathBuf> = Lazy::new(|| {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.canonicalize().ok())
        .and_then(|p| p.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."))
});

pub fn default_config_path() -> PathBuf {
    APP_DIR.join("config.yaml")
}

#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct ConfigError(pub String);

macro_rules! defaults {
    ($name:ident { $($field:ident : $ty:ty = $val:expr),* $(,)? }) => {
        #[derive(Debug, Clone, Serialize, Deserialize)]
        #[serde(default)]
        pub struct $name { $(pub $field: $ty),* }
        impl Default for $name {
            fn default() -> Self { Self { $($field: $val.into()),* } }
        }
    };
}

defaults!(Tools { ffmpeg: String = "ffmpeg", ffprobe: String = "ffprobe" });

defaults!(WhisperCfg {
    engine: String = "whisper.cpp",
    model: String = "small",
    compute_type: String = "auto",
    beam_size: f64 = 5.0,
    vad_filter: bool = false,
    cpu_threads: f64 = 0.0,
    language: String = "en",
    device: String = "auto",
    model_dir: String = "",
    unload_after_cycle: bool = true,
});

defaults!(OllamaCfg {
    url: String = "http://localhost:11434",
    model: String = "llama3.2:3b",
    timeout_seconds: f64 = 300.0,
    temperature: f64 = 0.1,
    num_ctx: f64 = 8192.0,
    chunk_minutes: f64 = 10.0,
    chunk_overlap_seconds: f64 = 30.0,
    required: bool = true,
    extra_instructions: String = "",
    remember_sponsors: bool = true,
    use_global_sponsors: bool = true,
    max_global_sponsors: f64 = 40.0,
    use_ad_feedback: bool = true,
    watch_sponsors: Vec<String> = Vec::<String>::new(),
    max_known_sponsors: f64 = 25.0,
});

defaults!(AdRemovalCfg {
    enabled: bool = true,
    min_ad_seconds: f64 = 10.0,
    max_ad_seconds: f64 = 300.0,
    merge_gap_seconds: f64 = 5.0,
    padding_seconds: f64 = 0.0,
    use_rss_ad_chapters: bool = true,
});

defaults!(ChaptersCfg {
    prefer_feed_chapters: bool = true,
    min_chapter_seconds: f64 = 60.0,
    fallback_interval_minutes: f64 = 10.0,
});

defaults!(DownloadCfg { skip_trackers: bool = true });

defaults!(ServerCfg {
    enabled: bool = true,
    host: String = "0.0.0.0",
    port: f64 = 8585.0,
    public_url: String = "",
    token: String = "",
    include_combined_feed: bool = true,
});

defaults!(OutputCfg {
    bitrate: String = "128k",
    embed_artwork: bool = true,
    max_title_length: f64 = 100.0,
    crossfade_ms: f64 = 40.0,
    archive_transcripts: bool = true,
});

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Podcast {
    pub name: String,
    pub rss: Option<String>,
    pub search: Option<String>,
    pub enabled: bool,
    pub auto_download: bool,
    pub initial_episodes: f64,
    pub max_episodes_per_check: f64,
    pub max_retries: f64,
    pub keep_episodes: f64,
    pub keep_days: f64,
    pub sponsors: Vec<String>,
}

impl Default for Podcast {
    fn default() -> Self {
        Self {
            name: String::new(),
            rss: None,
            search: None,
            enabled: true,
            auto_download: true,
            initial_episodes: 1.0,
            max_episodes_per_check: 3.0,
            max_retries: 3.0,
            keep_episodes: 0.0,
            keep_days: 0.0,
            sponsors: vec![],
        }
    }
}

impl Podcast {
    pub fn rss(&self) -> Option<&str> {
        self.rss.as_deref().filter(|s| !s.trim().is_empty())
    }
    pub fn search(&self) -> Option<&str> {
        self.search.as_deref().filter(|s| !s.trim().is_empty())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
struct RawConfig {
    output_dir: String,
    check_interval_minutes: Value,
    temp_dir: String,
    log_file: String,
    log_level: String,
    notifications: bool,
    check_on_startup: bool,
    tools: Tools,
    whisper: WhisperCfg,
    ollama: OllamaCfg,
    ad_removal: AdRemovalCfg,
    chapters: ChaptersCfg,
    download: DownloadCfg,
    server: ServerCfg,
    output: OutputCfg,
}

impl Default for RawConfig {
    fn default() -> Self {
        Self {
            output_dir: "~/Podcasts (Ad-Free)".into(),
            check_interval_minutes: Value::from(60),
            temp_dir: String::new(),
            log_file: "logs/podcast-cleaner.log".into(),
            log_level: "INFO".into(),
            notifications: true,
            check_on_startup: true,
            tools: Default::default(),
            whisper: Default::default(),
            ollama: Default::default(),
            ad_removal: Default::default(),
            chapters: Default::default(),
            download: Default::default(),
            server: Default::default(),
            output: Default::default(),
        }
    }
}

#[derive(Debug, Clone)]
pub struct Config {
    pub path: PathBuf,
    pub output_dir: PathBuf,
    pub check_interval_minutes: f64,
    pub temp_dir: Option<PathBuf>,
    pub log_file: PathBuf,
    pub log_level: String,
    pub notifications: bool,
    pub check_on_startup: bool,
    pub tools: Tools,
    pub whisper: WhisperCfg,
    pub ollama: OllamaCfg,
    pub ad_removal: AdRemovalCfg,
    pub chapters: ChaptersCfg,
    pub download: DownloadCfg,
    pub server: ServerCfg,
    pub output: OutputCfg,
    pub podcast_defaults: Podcast,
    pub podcasts: Vec<Podcast>,
}

impl Config {
    pub fn podcast_folder(&self, name: &str) -> PathBuf {
        self.output_dir.join(crate::utils::safe_filename(name, 80))
    }
}

/// Recursively drop nulls (Python's _merge ignores None values, so defaults win).
fn strip_nulls(v: &mut Value) {
    match v {
        Value::Object(m) => {
            m.retain(|_, x| !x.is_null());
            for x in m.values_mut() {
                strip_nulls(x);
            }
        }
        Value::Array(a) => a.iter_mut().for_each(strip_nulls),
        _ => {}
    }
}

/// YAML lets people write `sponsors: Squarespace` or numbers as names; be forgiving.
fn coerce_lists(m: &mut Map<String, Value>, keys: &[&str]) {
    for k in keys {
        if let Some(v) = m.get_mut(*k) {
            match v {
                Value::Array(a) => {
                    for x in a.iter_mut() {
                        if !x.is_string() {
                            *x = Value::String(scalar_text(x));
                        }
                    }
                }
                Value::String(s) => *v = Value::Array(vec![Value::String(s.clone())]),
                _ => *v = Value::Array(vec![]),
            }
        }
    }
}

fn coerce_strings(m: &mut Map<String, Value>, keys: &[&str]) {
    for k in keys {
        if let Some(v) = m.get_mut(*k) {
            if !v.is_string() && !v.is_object() && !v.is_array() {
                *v = Value::String(scalar_text(v));
            }
        }
    }
}

pub fn scalar_text(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

fn expand_vars(s: &str) -> String {
    let mut out = s.to_string();
    let re = regex::Regex::new(r"\$\{(\w+)\}|\$(\w+)|%(\w+)%").unwrap();
    out = re
        .replace_all(&out, |c: &regex::Captures| {
            let name = c.get(1).or(c.get(2)).or(c.get(3)).unwrap().as_str();
            std::env::var(name).unwrap_or_else(|_| c[0].to_string())
        })
        .into_owned();
    out
}

pub fn expand_path(value: &str, relative_to: &Path) -> PathBuf {
    let mut v = expand_vars(value.trim());
    if v == "~" || v.starts_with("~/") || v.starts_with("~\\") {
        v = format!("{}{}", home_dir().display(), &v[1..]);
    }
    let p = PathBuf::from(v);
    if p.is_absolute() {
        p
    } else {
        relative_to.join(p)
    }
}

pub fn config_path(path: Option<&Path>) -> PathBuf {
    if let Some(p) = path {
        return p.to_path_buf();
    }
    match std::env::var("PODCAST_CLEANER_CONFIG") {
        Ok(p) if !p.is_empty() => PathBuf::from(p),
        _ => default_config_path(),
    }
}

fn de<T: serde::de::DeserializeOwned>(v: Value, what: &str) -> Result<T, ConfigError> {
    serde_json::from_value(v).map_err(|e| ConfigError(format!("{what}: {e}")))
}

pub fn parse_config_text(text: &str, path: &Path) -> Result<Config, ConfigError> {
    let raw: Value = if text.trim().is_empty() {
        Value::Object(Map::new())
    } else {
        serde_yaml::from_str(text)
            .map_err(|e| ConfigError(format!("Invalid YAML in {}: {e}", path.display())))?
    };
    let mut raw = match raw {
        Value::Null => Value::Object(Map::new()),
        v @ Value::Object(_) => v,
        _ => {
            return Err(ConfigError(format!(
                "{} must contain a YAML mapping at the top level",
                path.display()
            )))
        }
    };
    strip_nulls(&mut raw);
    let top = raw.as_object_mut().unwrap();
    coerce_strings(top, &["output_dir", "temp_dir", "log_file", "log_level"]);
    for sect in ["whisper", "ollama", "tools", "server", "output"] {
        if let Some(Value::Object(m)) = top.get_mut(sect) {
            coerce_strings(
                m,
                &["model", "language", "url", "extra_instructions", "token", "public_url", "bitrate",
                  "host", "ffmpeg", "ffprobe", "model_dir", "device", "compute_type", "engine"],
            );
            coerce_lists(m, &["watch_sponsors"]);
        }
    }
    let podcasts_raw = top.remove("podcasts");
    let defaults_raw = top.remove("podcast_defaults").unwrap_or(Value::Object(Map::new()));
    let rc: RawConfig = de(raw.clone(), "config.yaml")?;

    let mut defaults_map = match defaults_raw {
        Value::Object(m) => m,
        _ => Map::new(),
    };
    coerce_lists(&mut defaults_map, &["sponsors"]);
    let podcast_defaults: Podcast = de(Value::Object(defaults_map.clone()), "podcast_defaults")?;

    let mut podcasts = vec![];
    let mut names = std::collections::HashSet::new();
    let list = match podcasts_raw {
        Some(Value::Array(a)) => a,
        _ => vec![],
    };
    for (i, p) in list.into_iter().enumerate() {
        let i = i + 1;
        let Value::Object(mut pm) = p else {
            return Err(ConfigError(format!("podcasts entry #{i} must be a mapping with name/rss")));
        };
        coerce_strings(&mut pm, &["name", "rss", "search"]);
        coerce_lists(&mut pm, &["sponsors"]);
        let mut merged = defaults_map.clone();
        for (k, v) in pm {
            merged.insert(k, v);
        }
        let mut pod: Podcast = de(Value::Object(merged), &format!("podcasts entry #{i}"))?;
        let name = pod.name.trim().to_string();
        if name.is_empty() {
            return Err(ConfigError(format!("podcasts entry #{i} is missing 'name'")));
        }
        if pod.rss().is_none() && pod.search().is_none() {
            return Err(ConfigError(format!("podcast '{name}' needs either 'rss' or 'search'")));
        }
        if !names.insert(name.to_lowercase()) {
            return Err(ConfigError(format!("podcast name '{name}' is listed twice")));
        }
        pod.name = name;
        podcasts.push(pod);
    }

    let interval = match &rc.check_interval_minutes {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
    .ok_or_else(|| ConfigError("check_interval_minutes must be a number".into()))?;

    Ok(Config {
        path: path.to_path_buf(),
        output_dir: expand_path(&rc.output_dir, &home_dir()),
        check_interval_minutes: interval.max(1.0),
        temp_dir: if rc.temp_dir.trim().is_empty() { None } else { Some(expand_path(&rc.temp_dir, &APP_DIR)) },
        log_file: expand_path(&rc.log_file, &APP_DIR),
        log_level: rc.log_level,
        notifications: rc.notifications,
        check_on_startup: rc.check_on_startup,
        tools: rc.tools,
        whisper: rc.whisper,
        ollama: rc.ollama,
        ad_removal: rc.ad_removal,
        chapters: rc.chapters,
        download: rc.download,
        server: rc.server,
        output: rc.output,
        podcast_defaults,
        podcasts,
    })
}

pub fn load_config(path: Option<&Path>) -> Result<Config, ConfigError> {
    let path = config_path(path);
    if !path.exists() {
        return Err(ConfigError(format!(
            "Config file not found: {} (copy config.example.yaml to config.yaml)",
            path.display()
        )));
    }
    let text = std::fs::read_to_string(&path)
        .map_err(|e| ConfigError(format!("Could not read {}: {e}", path.display())))?;
    parse_config_text(&text, &path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn example_config_loads() {
        let text = include_str!("../config.example.yaml");
        let c = parse_config_text(text, Path::new("config.yaml")).unwrap();
        assert_eq!(c.podcasts.len(), 2);
        assert_eq!(c.podcasts[1].sponsors, vec!["Squarespace"]);
        assert_eq!(c.podcasts[1].keep_episodes, 10.0);
        assert_eq!(c.podcasts[0].initial_episodes, 1.0);
        assert_eq!(c.server.port, 8585.0);
    }

    #[test]
    fn errors() {
        let p = Path::new("c.yaml");
        assert!(parse_config_text("podcasts:\n  - name: x\n", p).is_err());
        assert!(parse_config_text("podcasts:\n  - name: x\n    rss: a\n  - name: X\n    rss: b\n", p).is_err());
        assert!(parse_config_text("- a\n", p).is_err());
        assert!(parse_config_text("", p).is_ok());
        assert!(parse_config_text("ollama:\n  model: ~\n", p).is_ok());
    }
}
