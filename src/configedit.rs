//! Edit config.yaml from the window while keeping its comments and layout.
//!
//! Only the lines for the settings that change are rewritten; everything else (comments,
//! blank lines, commented-out examples, key order) stays exactly as it was.

use std::path::{Path, PathBuf};

use anyhow::{anyhow, bail, Result};
use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::{Map, Value};

use crate::config::parse_config_text;
use crate::utils::atomic_write_text;

pub struct ConfigEditor {
    pub path: PathBuf,
    lines: Vec<String>,
}

#[derive(Clone, Copy, Debug)]
struct Block {
    start: usize,
    end: usize,
    indent: usize,
    /// the first line is a sequence item ("- key: ...")
    dash_first: bool,
}

fn indent_of(l: &str) -> usize {
    l.len() - l.trim_start_matches(' ').len()
}

fn is_blank_or_comment(l: &str) -> bool {
    let t = l.trim();
    t.is_empty() || t.starts_with('#')
}

/// The text of a line with a leading "- " shown as spaces (for items of a sequence).
fn as_mapping_line(l: &str, dash: bool) -> String {
    if dash {
        let i = indent_of(l);
        if l[i..].starts_with("- ") {
            return format!("{}  {}", &l[..i], &l[i + 2..]);
        }
    }
    l.to_string()
}

static KEY: Lazy<Regex> = Lazy::new(|| Regex::new(r#"^(\s*)("[^"]*"|'[^']*'|[^\s#'"\-][^:#]*?|-[^\s][^:#]*?)\s*:(\s|$)"#).unwrap());

fn key_of(line: &str) -> Option<(usize, String)> {
    let c = KEY.captures(line)?;
    let k = c[2].trim().trim_matches(['"', '\'']).to_string();
    Some((c[1].len(), k))
}

/// Where an inline comment starts (" #" outside quotes).
fn comment_start(s: &str) -> Option<usize> {
    let (mut sq, mut dq, mut prev_space) = (false, false, true);
    for (i, ch) in s.char_indices() {
        match ch {
            '\'' if !dq => sq = !sq,
            '"' if !sq => dq = !dq,
            '#' if !sq && !dq && prev_space => return Some(i),
            _ => {}
        }
        prev_space = ch == ' ' || ch == '\t';
    }
    None
}

static PLAIN_OK: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[A-Za-z0-9_./\\~()][A-Za-z0-9 _./\\:~()+,-]*$").unwrap());
static LOOKS_SPECIAL: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?i)^(true|false|yes|no|on|off|null|~|y|n|[-+]?[0-9][0-9_.,:eE+-]*|\.inf|\.nan)$").unwrap());

pub fn scalar(v: &Value) -> String {
    match v {
        Value::Null => "null".into(),
        Value::Bool(b) => b.to_string(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => {
            if PLAIN_OK.is_match(s) && !LOOKS_SPECIAL.is_match(s) && !s.ends_with(' ') && !s.contains(": ") && !s.ends_with(':') {
                s.clone()
            } else {
                serde_json::to_string(s).unwrap() // JSON strings are valid YAML double-quoted strings
            }
        }
        Value::Array(a) => format!("[{}]", a.iter().map(scalar).collect::<Vec<_>>().join(", ")),
        Value::Object(_) => serde_json::to_string(v).unwrap(),
    }
}

/// Lines for "key: value" at `indent` (multi-line text becomes a readable `|` block).
fn render(key: &str, v: &Value, indent: usize, comment: &str) -> Vec<String> {
    let pad = " ".repeat(indent);
    if let Value::String(s) = v {
        if s.contains('\n') {
            let mut out = vec![format!("{pad}{key}: |{}", if comment.is_empty() { String::new() } else { format!("  {comment}") })];
            for l in s.trim_end_matches('\n').split('\n') {
                out.push(if l.is_empty() { String::new() } else { format!("{pad}  {l}") });
            }
            return out;
        }
    }
    let c = if comment.is_empty() { String::new() } else { format!("  {comment}") };
    vec![format!("{pad}{key}: {}{c}", scalar(v))]
}

impl ConfigEditor {
    pub fn open(path: &Path) -> Result<Self> {
        let text = std::fs::read_to_string(path)?;
        Ok(Self { path: path.to_path_buf(), lines: text.replace("\r\n", "\n").split('\n').map(String::from).collect() })
    }

    pub fn text(&self) -> String {
        self.lines.join("\n")
    }

    fn parsed(&self) -> Value {
        serde_yaml::from_str::<Value>(&self.text()).unwrap_or(Value::Null)
    }

    pub fn get(&self, dotted: &str) -> Option<Value> {
        let mut node = self.parsed();
        for part in dotted.split('.') {
            node = node.get(part)?.clone();
        }
        (!node.is_null()).then_some(node)
    }

    fn line_key(&self, i: usize, b: &Block) -> Option<(usize, String)> {
        key_of(&as_mapping_line(&self.lines[i], b.dash_first && i == b.start))
    }

    /// Lines after `i` that belong to its value (more indented, or "- " items at the same indent).
    fn value_end(&self, i: usize, key_indent: usize, b: &Block) -> usize {
        let line = as_mapping_line(&self.lines[i], b.dash_first && i == b.start);
        let after = line.splitn(2, ':').nth(1).unwrap_or("");
        let inline = after.split(" #").next().unwrap_or("").trim().to_string();
        let mut end = i + 1;
        let mut j = i + 1;
        while j < b.end {
            let l = &self.lines[j];
            if l.trim().is_empty() {
                j += 1;
                continue;
            }
            let ind = indent_of(l);
            let is_item = l.trim_start().starts_with("- ") || l.trim() == "-";
            if ind > key_indent || (ind == key_indent && is_item && (inline.is_empty() || inline.starts_with('#'))) {
                j += 1;
                end = j;
            } else {
                break;
            }
        }
        // don't swallow trailing comment lines that sit at the key's own level
        while end > i + 1 && is_blank_or_comment(&self.lines[end - 1]) && indent_of(&self.lines[end - 1]) <= key_indent {
            end -= 1;
        }
        end
    }

    fn find_key(&self, b: &Block, key: &str) -> Option<usize> {
        (b.start..b.end).find(|&i| self.line_key(i, b).is_some_and(|(ind, k)| ind == b.indent && k == key))
    }

    /// The mapping under key line `i`.
    fn child_block(&self, i: usize, parent: &Block) -> Block {
        let end = self.value_end(i, parent.indent, parent);
        let indent = (i + 1..end).find(|&j| !is_blank_or_comment(&self.lines[j])).map(|j| indent_of(&self.lines[j])).unwrap_or(parent.indent + 2);
        Block { start: i + 1, end, indent, dash_first: false }
    }

    fn root(&self) -> Block {
        Block { start: 0, end: self.lines.len(), indent: 0, dash_first: false }
    }

    fn set_in(&mut self, b: Block, key: &str, v: &Value) {
        match self.find_key(&b, key) {
            Some(i) => {
                let end = self.value_end(i, b.indent, &b);
                if v.is_null() {
                    if i == b.start && b.dash_first {
                        // keep the "- " on the first line of an item
                        self.lines.drain(i + 1..end);
                        bail_remove_first(&mut self.lines, i);
                    } else {
                        self.lines.drain(i..end);
                    }
                    return;
                }
                let line = as_mapping_line(&self.lines[i], b.dash_first && i == b.start);
                let after = &line[line.find(':').map(|p| p + 1).unwrap_or(line.len())..];
                let comment = if !after.trim_start().starts_with('|') && !after.trim_start().starts_with('>') {
                    comment_start(after).map(|c| after[c..].trim_end().to_string()).unwrap_or_default()
                } else {
                    String::new()
                };
                let mut new = render(key, v, b.indent, &comment);
                if !comment.is_empty() && new.len() == 1 {
                    // keep the comment where it was
                    let col = self.lines[i].find(&comment).unwrap_or(0);
                    let base = render(key, v, b.indent, "").remove(0);
                    new[0] = if base.len() + 1 <= col { format!("{base:<col$}{comment}") } else { format!("{base}  {comment}") };
                }
                if b.dash_first && i == b.start {
                    let d = b.indent - 2;
                    new[0] = format!("{}- {}", " ".repeat(d), &new[0][b.indent..]);
                }
                self.lines.splice(i..end, new);
            }
            None if v.is_null() => {}
            None => {
                // after the last line of the block's content
                let mut at = b.end;
                while at > b.start && is_blank_or_comment(&self.lines[at - 1]) && indent_of(&self.lines[at - 1]) < b.indent.max(1) {
                    at -= 1;
                }
                while at > b.start && self.lines[at - 1].trim().is_empty() {
                    at -= 1;
                }
                let new = render(key, v, b.indent, "");
                self.lines.splice(at..at, new);
            }
        }
    }

    /// Set only if the value differs from what's in the file (or the default when it's not there),
    /// so saving from the window doesn't rewrite settings you didn't touch.
    pub fn set_if_changed(&mut self, dotted: &str, v: Value, default: &Value) -> Result<bool> {
        let current = self.get(dotted).unwrap_or_else(|| default.clone());
        if same_value(&current, &v) {
            return Ok(false);
        }
        self.set(dotted, v)?;
        Ok(true)
    }

    pub fn set(&mut self, dotted: &str, v: Value) -> Result<()> {
        let parts: Vec<&str> = dotted.split('.').collect();
        let mut b = self.root();
        for (n, part) in parts.iter().enumerate() {
            if n == parts.len() - 1 {
                self.set_in(b, part, &v);
                return Ok(());
            }
            match self.find_key(&b, part) {
                Some(i) => {
                    let line = self.lines[i].clone();
                    let after = line.splitn(2, ':').nth(1).unwrap_or("").trim();
                    if !after.is_empty() && !after.starts_with('#') {
                        // e.g. "ollama: {}" - turn it into a block
                        self.lines[i] = format!("{}{part}:", " ".repeat(b.indent));
                    }
                    b = self.child_block(i, &b);
                }
                None => {
                    // add the section at the end of its parent (top level: just above "podcasts:")
                    let mut at = b.end;
                    while at > b.start && self.lines[at - 1].trim().is_empty() {
                        at -= 1;
                    }
                    if b.indent == 0 {
                        if let Some(p) = self.find_key(&b, "podcasts") {
                            at = p;
                            while at > 0 && self.lines[at - 1].trim().starts_with('#') {
                                at -= 1;
                            }
                            while at > 0 && self.lines[at - 1].trim().is_empty() {
                                at -= 1;
                            }
                        }
                    }
                    let pad = " ".repeat(b.indent);
                    let mut new = vec![];
                    if b.indent == 0 {
                        new.push(String::new());
                    }
                    new.push(format!("{pad}{part}:"));
                    self.lines.splice(at..at, new.clone());
                    let i = at + new.len() - 1;
                    b = Block { start: i + 1, end: i + 1, indent: b.indent + 2, dash_first: false };
                }
            }
        }
        Ok(())
    }

    // ---------- podcasts ----------

    fn podcasts_block(&mut self) -> Result<(usize, Block)> {
        let root = self.root();
        let i = match self.find_key(&root, "podcasts") {
            Some(i) => i,
            None => {
                self.lines.push(String::new());
                self.lines.push("podcasts:".into());
                self.lines.len() - 1
            }
        };
        let after = self.lines[i].splitn(2, ':').nth(1).unwrap_or("").to_string();
        let inline = after.split(" #").next().unwrap_or("").trim().to_string();
        if inline == "[]" {
            self.lines[i] = "podcasts:".into();
        } else if !inline.is_empty() && !inline.starts_with('#') {
            bail!("config.yaml lists podcasts in a style this window can't edit - please edit the file by hand");
        }
        Ok((i, self.child_block(i, &root)))
    }

    /// (start, end) line ranges of each "- " item, in order.
    fn items(&mut self) -> Result<(usize, Vec<Block>)> {
        let (_, b) = self.podcasts_block()?;
        let starts: Vec<usize> = (b.start..b.end)
            .filter(|&j| !is_blank_or_comment(&self.lines[j]) && indent_of(&self.lines[j]) == b.indent && self.lines[j].trim_start().starts_with('-'))
            .collect();
        let mut out = vec![];
        for (n, &s) in starts.iter().enumerate() {
            let mut end = starts.get(n + 1).copied().unwrap_or(b.end);
            while end > s + 1 && is_blank_or_comment(&self.lines[end - 1]) && indent_of(&self.lines[end - 1]) <= b.indent + 2 {
                end -= 1;
            }
            out.push(Block { start: s, end, indent: b.indent + 2, dash_first: true });
        }
        let parsed = self.parsed();
        let count = parsed.get("podcasts").and_then(Value::as_array).map(Vec::len).unwrap_or(0);
        if count != out.len() {
            bail!("config.yaml's podcast list is laid out in a way this window can't edit - please edit the file by hand");
        }
        Ok((b.indent, out))
    }

    pub fn podcasts(&self) -> Vec<Map<String, Value>> {
        self.parsed().get("podcasts").and_then(Value::as_array).cloned().unwrap_or_default()
            .into_iter().filter_map(|v| v.as_object().cloned()).collect()
    }

    fn index_of(&self, name: &str) -> Option<usize> {
        self.podcasts().iter().position(|p| {
            crate::config::scalar_text(p.get("name").unwrap_or(&Value::Null)).trim().to_lowercase() == name.trim().to_lowercase()
        })
    }

    pub fn find(&self, name: &str) -> Option<Map<String, Value>> {
        self.index_of(name).map(|i| self.podcasts()[i].clone())
    }

    pub fn add_podcast(&mut self, fields: &Map<String, Value>) -> Result<()> {
        let name = fields.get("name").and_then(Value::as_str).unwrap_or("").to_string();
        if self.find(&name).is_some() {
            bail!("A podcast named '{name}' already exists");
        }
        let (item_indent, items) = self.items()?;
        let (_, block) = self.podcasts_block()?;
        let at = items.last().map(|b| b.end).unwrap_or(block.start);
        let mut keys: Vec<&String> = fields.keys().collect();
        let order = ["name", "rss", "search"];
        keys.sort_by_key(|k| order.iter().position(|o| o == k).unwrap_or(order.len()));
        let mut new = vec![];
        if !items.is_empty() {
            new.push(String::new());
        }
        for (n, k) in keys.iter().enumerate() {
            let v = &fields[*k];
            if v.is_null() || v.as_str() == Some("") {
                continue;
            }
            let mut lines = render(k, v, item_indent + 2, "");
            if n == 0 {
                lines[0] = format!("{}- {}", " ".repeat(item_indent), lines[0].trim_start());
            }
            new.extend(lines);
        }
        self.lines.splice(at..at, new);
        Ok(())
    }

    pub fn update_podcast(&mut self, name: &str, fields: &Map<String, Value>) -> Result<()> {
        let idx = self.index_of(name).ok_or_else(|| anyhow!("No podcast named '{name}'"))?;
        if let Some(new_name) = fields.get("name").and_then(Value::as_str) {
            if new_name.to_lowercase() != name.to_lowercase() && self.find(new_name).is_some() {
                bail!("A podcast named '{new_name}' already exists");
            }
        }
        // "name" first so the item's first line stays the name line
        let mut keys: Vec<&String> = fields.keys().collect();
        keys.sort_by_key(|k| *k != "name");
        for k in keys {
            let (_, items) = self.items()?;
            let b = items[idx];
            let v = &fields[k];
            let v = if v.as_str() == Some("") { Value::Null } else { v.clone() };
            self.set_in(b, k, &v);
        }
        Ok(())
    }

    pub fn remove_podcast(&mut self, name: &str) -> Result<()> {
        let idx = self.index_of(name).ok_or_else(|| anyhow!("No podcast named '{name}'"))?;
        let (_, items) = self.items()?;
        let b = items[idx];
        // also drop one blank line that separated it from the previous item
        let start = if b.start > 0 && self.lines[b.start - 1].trim().is_empty() && idx > 0 { b.start - 1 } else { b.start };
        self.lines.drain(start..b.end);
        Ok(())
    }

    /// Validate, keep config.yaml.bak, then replace the file.
    pub fn save(&self) -> Result<()> {
        let text = self.text();
        parse_config_text(&text, &self.path)?;
        let backup = PathBuf::from(format!("{}.bak", self.path.display()));
        let _ = std::fs::copy(&self.path, backup);
        atomic_write_text(&self.path, &text)?;
        Ok(())
    }
}

fn same_value(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => x.as_f64() == y.as_f64(),
        (Value::Number(x), Value::String(y)) | (Value::String(y), Value::Number(x)) => y.trim().parse::<f64>().ok() == x.as_f64(),
        (Value::Array(x), Value::Array(y)) => x.len() == y.len() && x.iter().zip(y).all(|(a, b)| same_value(a, b)),
        (Value::String(x), Value::String(y)) => x.trim_end() == y.trim_end(),
        _ => a == b,
    }
}

/// Removing the first key of a sequence item: make the next key carry the "- ".
fn bail_remove_first(lines: &mut Vec<String>, i: usize) {
    let dash_indent = indent_of(&lines[i]);
    if i + 1 < lines.len() && indent_of(&lines[i + 1]) == dash_indent + 2 && !is_blank_or_comment(&lines[i + 1]) {
        lines[i + 1] = format!("{}- {}", " ".repeat(dash_indent), lines[i + 1].trim_start());
        lines.remove(i);
    } else {
        lines.remove(i);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn ed(text: &str) -> (tempfile::TempDir, ConfigEditor) {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("config.yaml");
        std::fs::write(&p, text).unwrap();
        let e = ConfigEditor::open(&p).unwrap();
        (d, e)
    }

    fn obj(v: Value) -> Map<String, Value> {
        v.as_object().unwrap().clone()
    }

    #[test]
    fn settings_keep_comments() {
        let (_d, mut e) = ed(include_str!("../config.example.yaml"));
        e.set("ollama.model", json!("qwen2.5:7b")).unwrap();
        e.set("check_interval_minutes", json!(30)).unwrap();
        e.set("ollama.watch_sponsors", json!(["Acme", "Beta Co"])).unwrap();
        e.set("ollama.extra_instructions", json!("Line one\nLine two\n")).unwrap();
        e.set("server.port", json!(9000)).unwrap();
        e.set("output_dir", json!("D:\\Podcasts")).unwrap();
        e.set("whisper.new_key", json!(true)).unwrap();
        e.set("brand_new.section", json!("x")).unwrap();
        let t = e.text();
        assert!(t.contains("  model: qwen2.5:7b        # bigger models"), "{t}");
        assert!(t.contains("# Where cleaned podcasts go"));
        let c = parse_config_text(&t, Path::new("c.yaml")).unwrap();
        assert_eq!(c.ollama.model, "qwen2.5:7b");
        assert_eq!(c.check_interval_minutes, 30.0);
        assert_eq!(c.ollama.watch_sponsors, vec!["Acme", "Beta Co"]);
        assert_eq!(c.ollama.extra_instructions, "Line one\nLine two\n");
        assert_eq!(c.server.port, 9000.0);
        assert_eq!(c.output_dir, PathBuf::from("D:\\Podcasts").to_path_buf().pipe_abs());
        assert_eq!(c.whisper.model, "small");
        // editing again replaces the block cleanly
        e.set("ollama.extra_instructions", json!("short")).unwrap();
        e.set("ollama.watch_sponsors", json!([])).unwrap();
        let c = parse_config_text(&e.text(), Path::new("c.yaml")).unwrap();
        assert_eq!(c.ollama.extra_instructions, "short");
        assert!(c.ollama.watch_sponsors.is_empty());
        assert_eq!(c.ollama.max_known_sponsors, 25.0);
    }

    trait PipeAbs {
        fn pipe_abs(self) -> PathBuf;
    }
    impl PipeAbs for PathBuf {
        fn pipe_abs(self) -> PathBuf {
            crate::config::expand_path(&self.to_string_lossy(), &crate::utils::home_dir())
        }
    }

    #[test]
    fn podcasts() {
        let (_d, mut e) = ed(include_str!("../config.example.yaml"));
        e.add_podcast(&obj(json!({"name": "New: Show", "rss": "https://x/feed", "auto_download": false, "sponsors": ["A"]}))).unwrap();
        e.update_podcast("hard fork", &obj(json!({"name": "Hard Fork 2", "rss": "https://h/f", "search": null, "keep_episodes": null}))).unwrap();
        e.update_podcast("The Daily", &obj(json!({"enabled": false}))).unwrap();
        let c = parse_config_text(&e.text(), Path::new("c.yaml")).unwrap();
        assert_eq!(c.podcasts.len(), 3);
        assert_eq!(c.podcasts[1].name, "Hard Fork 2");
        assert_eq!(c.podcasts[1].search, None);
        assert_eq!(c.podcasts[1].keep_episodes, 0.0);
        assert_eq!(c.podcasts[1].sponsors, vec!["Squarespace"]);
        assert!(!c.podcasts[0].enabled);
        assert_eq!(c.podcasts[2].name, "New: Show");
        assert!(!c.podcasts[2].auto_download);
        assert!(e.text().contains("# - name: \"Some Other Show\""));
        e.remove_podcast("the daily").unwrap();
        e.update_podcast("Hard Fork 2", &obj(json!({"name": null}))).ok();
        let c = parse_config_text(&e.text(), Path::new("c.yaml"));
        assert!(c.is_err()); // name removed -> invalid, save() would refuse
        e.update_podcast("Hard Fork 2", &obj(json!({}))).ok();
    }

    #[test]
    fn empty_list() {
        let (_d, mut e) = ed("output_dir: x\npodcasts: []\n");
        e.add_podcast(&obj(json!({"name": "A", "rss": "https://a"}))).unwrap();
        e.add_podcast(&obj(json!({"name": "B", "rss": "https://b"}))).unwrap();
        let c = parse_config_text(&e.text(), Path::new("c.yaml")).unwrap();
        assert_eq!(c.podcasts.len(), 2);
        e.remove_podcast("A").unwrap();
        let c = parse_config_text(&e.text(), Path::new("c.yaml")).unwrap();
        assert_eq!(c.podcasts[0].name, "B");
        let (_d, mut e) = ed("output_dir: x\n");
        e.add_podcast(&obj(json!({"name": "A", "rss": "https://a"}))).unwrap();
        assert_eq!(parse_config_text(&e.text(), Path::new("c.yaml")).unwrap().podcasts.len(), 1);
    }
}
