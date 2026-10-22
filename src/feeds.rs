//! RSS feed fetching/parsing, iTunes search, publisher chapters, and episode downloads.

use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{anyhow, bail, Result};
use chrono::{DateTime, FixedOffset, NaiveDateTime, TimeZone, Utc};
use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::Value;

use crate::utils::{parse_timestamp, parse_timestamp_value, Cancelled};

pub const USER_AGENT: &str = "PodcastAdCleaner/2.0 (+https://github.com/adamc199/podcast-ad-cleaner)";

pub static AD_TITLE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)^\s*(\[\s*ads?\s*\]|ads?\s*[:\-]|ads?\s*$|ad break|advert|sponsor|paid promo)").unwrap()
});

#[derive(Debug, Clone, PartialEq)]
pub struct Chapter {
    pub start: f64,
    pub title: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Ad {
    pub start: f64,
    pub end: f64,
    pub label: String,
    pub source: String,
}

#[derive(Debug, Clone, Default)]
pub struct Episode {
    pub guid: String,
    pub title: String,
    pub audio_url: String,
    pub published: Option<DateTime<Utc>>,
    pub description: String,
    pub image_url: Option<String>,
    pub feed_chapters: Vec<Chapter>,
    pub chapters_url: Option<String>,
    /// as given by the feed (seconds or h:mm:ss)
    pub duration: String,
}

impl Episode {
    pub fn published_iso(&self) -> Option<String> {
        self.published.map(|d| crate::utils::iso_utc(&d.fixed_offset()))
    }
}

#[derive(Debug, Clone, Default)]
pub struct Feed {
    pub url: String,
    pub title: String,
    pub author: String,
    pub image_url: Option<String>,
    /// newest first
    pub episodes: Vec<Episode>,
    pub description: String,
    pub link: String,
}

// ---------------- HTTP ----------------

/// An HTTP agent. `read_timeout` is how long the server may stay silent (not a total limit).
pub fn agent(read_timeout: Duration) -> ureq::Agent {
    ureq::AgentBuilder::new()
        .user_agent(USER_AGENT)
        .timeout_connect(Duration::from_secs(30))
        .timeout_read(read_timeout)
        .redirects(10)
        .build()
}

pub fn make_session() -> ureq::Agent {
    agent(Duration::from_secs(60))
}

/// GET returning an error for HTTP failures, with the status in the message.
pub fn http_get(agent: &ureq::Agent, url: &str, headers: &[(&str, &str)]) -> Result<ureq::Response> {
    let mut req = agent.get(url);
    for (k, v) in headers {
        req = req.set(k, v);
    }
    match req.call() {
        Ok(r) => Ok(r),
        Err(ureq::Error::Status(code, r)) => bail!("HTTP {code} {} for {}", r.status_text(), host_of(url)),
        Err(e) => Err(anyhow!("{}", e)),
    }
}

pub fn read_all(resp: ureq::Response, limit: usize) -> Result<Vec<u8>> {
    let mut buf = Vec::new();
    resp.into_reader().take(limit as u64).read_to_end(&mut buf)?;
    Ok(buf)
}

pub fn host_of(url: &str) -> String {
    url::Url::parse(url).ok().and_then(|u| u.host_str().map(String::from)).unwrap_or_else(|| url.to_string())
}

#[derive(Debug, Clone)]
pub struct SearchResult {
    pub name: String,
    pub author: String,
    pub feed_url: String,
}

pub fn itunes_search(term: &str, agent: &ureq::Agent, limit: usize) -> Result<Vec<SearchResult>> {
    let r = agent
        .get("https://itunes.apple.com/search")
        .query("term", term)
        .query("media", "podcast")
        .query("limit", &limit.to_string())
        .timeout(Duration::from_secs(30))
        .call()
        .map_err(|e| anyhow!("iTunes search failed: {e}"))?;
    let v: Value = serde_json::from_slice(&read_all(r, 20 << 20)?)?;
    Ok(v["results"]
        .as_array()
        .cloned()
        .unwrap_or_default()
        .iter()
        .filter_map(|x| {
            let feed = x["feedUrl"].as_str()?;
            Some(SearchResult {
                name: x["collectionName"].as_str().unwrap_or("").into(),
                author: x["artistName"].as_str().unwrap_or("").into(),
                feed_url: feed.into(),
            })
        })
        .collect())
}

/// Decompress gzip/zlib data if that's what it is; otherwise return it unchanged.
/// Handles servers that label plain text as gzip, compress twice, or cut the stream short.
pub fn undo_compression(mut data: Vec<u8>) -> Vec<u8> {
    for _ in 0..3 {
        let mut out = Vec::new();
        let res = if data.starts_with(&[0x1f, 0x8b]) {
            flate2::read::MultiGzDecoder::new(&data[..]).read_to_end(&mut out)
        } else if data.first() == Some(&0x78) {
            flate2::read::ZlibDecoder::new(&data[..]).read_to_end(&mut out)
        } else {
            return data;
        };
        // a truncated stream errors at the end, but keeps what it could decode
        if out.is_empty() {
            let _ = res;
            return data;
        }
        data = out;
    }
    data
}

/// Fetch a feed's bytes. Compression is never requested, and anything that arrives compressed
/// anyway (or mislabelled) is decoded here.
pub fn get_feed_bytes(url: &str, agent: &ureq::Agent) -> Result<Vec<u8>> {
    let r = http_get(agent, url, &[("Accept-Encoding", "identity")])?;
    Ok(undo_compression(read_all(r, 200 << 20)?))
}

// ---------------- XML parsing ----------------

fn decode_xml(bytes: &[u8]) -> String {
    if let Some((enc, bom_len)) = encoding_rs::Encoding::for_bom(bytes) {
        return enc.decode_without_bom_handling(&bytes[bom_len..]).0.into_owned();
    }
    let head = String::from_utf8_lossy(&bytes[..bytes.len().min(200)]).to_lowercase();
    static ENC: Lazy<Regex> = Lazy::new(|| Regex::new(r#"encoding\s*=\s*["']([a-z0-9_\-]+)["']"#).unwrap());
    if let Some(c) = ENC.captures(&head) {
        if let Some(enc) = encoding_rs::Encoding::for_label(c[1].as_bytes()) {
            if enc != encoding_rs::UTF_8 {
                return enc.decode_without_bom_handling(bytes).0.into_owned();
            }
        }
    }
    String::from_utf8_lossy(bytes).into_owned()
}

type Node<'a, 'i> = roxmltree::Node<'a, 'i>;

fn ns_is(n: &Node, key: &str) -> bool {
    n.tag_name().namespace().map(|u| u.to_lowercase().contains(key)).unwrap_or(false)
}

fn plain(n: &Node, name: &str) -> bool {
    n.is_element() && n.tag_name().name() == name && n.tag_name().namespace().is_none()
}

fn child<'a, 'i>(n: &Node<'a, 'i>, pred: impl Fn(&Node) -> bool) -> Option<Node<'a, 'i>> {
    n.children().find(|c| c.is_element() && pred(c))
}

fn text_of(n: Option<Node>) -> String {
    n.map(|n| n.descendants().filter(|d| d.is_text()).map(|d| d.text().unwrap_or("")).collect::<String>())
        .unwrap_or_default()
        .trim()
        .to_string()
}

fn itunes<'a, 'i>(n: &Node<'a, 'i>, name: &str) -> Option<Node<'a, 'i>> {
    child(n, |c| c.tag_name().name() == name && ns_is(c, "itunes"))
}

static TZ_ABBR: &[(&str, &str)] = &[
    ("UT", "+0000"), ("UTC", "+0000"), ("GMT", "+0000"), ("Z", "+0000"),
    ("EST", "-0500"), ("EDT", "-0400"), ("CST", "-0600"), ("CDT", "-0500"),
    ("MST", "-0700"), ("MDT", "-0600"), ("PST", "-0800"), ("PDT", "-0700"),
    ("BST", "+0100"), ("CET", "+0100"), ("CEST", "+0200"), ("AEST", "+1000"), ("AEDT", "+1100"),
];

/// Tolerant RSS/Atom date parser (RFC 822 with the usual mistakes, or ISO 8601).
pub fn parse_feed_date(s: &str) -> Option<DateTime<Utc>> {
    let s = s.trim();
    if s.is_empty() {
        return None;
    }
    if let Ok(d) = DateTime::parse_from_rfc2822(s) {
        return Some(d.with_timezone(&Utc));
    }
    if let Ok(d) = DateTime::parse_from_rfc3339(s) {
        return Some(d.with_timezone(&Utc));
    }
    if let Some(crate::utils::IsoTime::Aware(d)) = crate::utils::parse_iso(s) {
        return Some(d.with_timezone(&Utc));
    }
    // "Tue, 8 Oct 2024 10:00 GMT", "08 Oct 2024 10:00:00 EST", "Thurs, 10 Oct 2024 ..."
    static RE: Lazy<Regex> = Lazy::new(|| {
        Regex::new(r"(?i)(\d{1,2})\s+([a-z]{3})[a-z]*\.?\s+(\d{2,4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([+\-]\d{2}:?\d{2}|[a-z]{1,4})?").unwrap()
    });
    let c = RE.captures(s)?;
    let months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
    let mon = months.iter().position(|m| c[2].eq_ignore_ascii_case(m))? + 1;
    let mut year: i32 = c[3].parse().ok()?;
    if year < 100 {
        year += if year < 70 { 2000 } else { 1900 };
    }
    let naive = NaiveDateTime::parse_from_str(
        &format!("{year}-{mon:02}-{:0>2} {}:{}:{}", &c[1], &c[4], &c[5], c.get(6).map(|m| m.as_str()).unwrap_or("00")),
        "%Y-%m-%d %H:%M:%S",
    )
    .ok()?;
    let tz = c.get(7).map(|m| m.as_str().to_uppercase()).unwrap_or_else(|| "+0000".into());
    let tz = TZ_ABBR.iter().find(|(a, _)| *a == tz).map(|(_, o)| o.to_string()).unwrap_or(tz);
    let tz = tz.replace(':', "");
    let secs = if tz.len() == 5 && (tz.starts_with('+') || tz.starts_with('-')) {
        let h: i32 = tz[1..3].parse().ok()?;
        let m: i32 = tz[3..5].parse().ok()?;
        let v = h * 3600 + m * 60;
        if tz.starts_with('-') { -v } else { v }
    } else {
        0
    };
    let off = FixedOffset::east_opt(secs)?;
    off.from_local_datetime(&naive).single().map(|d| d.with_timezone(&Utc))
}

fn item_audio_url(item: &Node) -> Option<String> {
    for enc in item.children().filter(|c| plain(c, "enclosure")) {
        let href = enc.attribute("url").unwrap_or("").trim();
        let ty = enc.attribute("type").unwrap_or("");
        if !href.is_empty() && (ty.starts_with("audio") || ty.is_empty()) {
            return Some(href.to_string());
        }
    }
    // Atom: <link rel="enclosure" href="...">
    for l in item.children().filter(|c| c.is_element() && c.tag_name().name() == "link") {
        if l.attribute("rel") == Some("enclosure") {
            if let Some(h) = l.attribute("href").filter(|h| !h.is_empty()) {
                return Some(h.to_string());
            }
        }
    }
    None
}

pub fn parse_feed(content: &[u8], url: &str) -> Result<Feed> {
    let text = decode_xml(content);
    let opts = roxmltree::ParsingOptions { allow_dtd: true, ..Default::default() };
    let doc = roxmltree::Document::parse_with_options(&text, opts)
        .map_err(|e| anyhow!("Could not parse feed {url}: {e}"))?;
    let root = doc.root_element();
    let is_atom = root.tag_name().name() == "feed";
    let channel = if is_atom {
        root
    } else {
        root.descendants()
            .find(|n| n.is_element() && n.tag_name().name() == "channel")
            .ok_or_else(|| anyhow!("Could not parse feed {url}: no <channel>"))?
    };

    let feed_image = itunes(&channel, "image")
        .and_then(|n| n.attribute("href").map(String::from))
        .or_else(|| {
            child(&channel, |c| plain(c, "image")).map(|i| text_of(child(&i, |c| plain(c, "url"))))
        })
        .or_else(|| if is_atom { Some(text_of(child(&channel, |c| c.tag_name().name() == "logo"))) } else { None })
        .filter(|s| !s.is_empty());

    let author = {
        let a = text_of(itunes(&channel, "author"));
        if !a.is_empty() {
            a
        } else if is_atom {
            text_of(child(&channel, |c| c.tag_name().name() == "author").and_then(|a| child(&a, |c| c.tag_name().name() == "name")))
        } else {
            text_of(child(&channel, |c| plain(c, "author") || plain(c, "managingEditor")))
        }
    };
    let link = if is_atom {
        channel
            .children()
            .find(|c| c.is_element() && c.tag_name().name() == "link" && c.attribute("rel").unwrap_or("alternate") == "alternate")
            .and_then(|l| l.attribute("href").map(String::from))
            .unwrap_or_default()
    } else {
        text_of(child(&channel, |c| plain(c, "link")))
    };
    let mut description = text_of(itunes(&channel, "subtitle"));
    if description.is_empty() {
        description = text_of(child(&channel, |c| plain(c, "description") || c.tag_name().name() == "subtitle"));
    }
    if description.is_empty() {
        description = text_of(itunes(&channel, "summary"));
    }

    let item_name = if is_atom { "entry" } else { "item" };
    let mut episodes = vec![];
    for item in channel.children().filter(|c| c.is_element() && c.tag_name().name() == item_name) {
        let Some(audio) = item_audio_url(&item) else { continue };
        let mut guid = text_of(child(&item, |c| plain(c, "guid") || (is_atom && c.tag_name().name() == "id")));
        if guid.is_empty() {
            guid = audio.trim().to_string();
        }
        let date = text_of(child(&item, |c| {
            plain(c, "pubDate") || (is_atom && matches!(c.tag_name().name(), "published" | "updated"))
                || (c.tag_name().name() == "date" && ns_is(c, "purl.org/dc"))
        }));
        let mut desc = text_of(child(&item, |c| plain(c, "description") || (is_atom && c.tag_name().name() == "summary")));
        if desc.is_empty() {
            desc = text_of(itunes(&item, "summary"));
        }
        if desc.is_empty() {
            desc = text_of(child(&item, |c| c.tag_name().name() == "encoded" && ns_is(c, "content")));
        }
        let mut psc = vec![];
        if let Some(chs) = child(&item, |c| c.tag_name().name() == "chapters" && ns_is(c, "podlove")) {
            for ch in chs.children().filter(|c| c.is_element() && c.tag_name().name() == "chapter") {
                if let Some(start) = ch.attribute("start").and_then(parse_timestamp) {
                    psc.push(Chapter { start, title: ch.attribute("title").unwrap_or("").trim().to_string() });
                }
            }
        }
        psc.sort_by(|a, b| a.start.total_cmp(&b.start));
        let chapters_url = child(&item, |c| c.tag_name().name() == "chapters" && ns_is(c, "podcastindex"))
            .and_then(|c| c.attribute("url").map(String::from))
            .filter(|u| !u.is_empty());
        let title = text_of(child(&item, |c| plain(c, "title") || (is_atom && c.tag_name().name() == "title")));
        episodes.push(Episode {
            guid,
            title: if title.is_empty() { "Untitled episode".into() } else { title },
            audio_url: audio,
            published: parse_feed_date(&date),
            description: desc,
            image_url: itunes(&item, "image")
                .and_then(|n| n.attribute("href").map(String::from))
                .filter(|s| !s.is_empty())
                .or_else(|| feed_image.clone()),
            feed_chapters: psc,
            chapters_url,
            duration: text_of(itunes(&item, "duration")),
        });
    }
    if episodes.is_empty() && channel.children().all(|c| c.tag_name().name() != item_name) && !is_atom && root.tag_name().name() != "rss" {
        bail!("Could not parse feed {url}: not an RSS or Atom feed");
    }
    // newest first (stable, like Python's sort)
    episodes.sort_by(|a, b| b.published.cmp(&a.published));
    let title = text_of(child(&channel, |c| plain(c, "title") || (is_atom && c.tag_name().name() == "title")));
    Ok(Feed { url: url.into(), title, author, image_url: feed_image, episodes, description, link })
}

pub fn fetch_feed(url: &str, agent: &ureq::Agent) -> Result<Feed> {
    let content = get_feed_bytes(url, agent)?;
    parse_feed(&content, url)
}

/// Publisher chapters from <psc:chapters> or a <podcast:chapters> JSON file.
pub fn load_publisher_chapters(ep: &Episode, agent: &ureq::Agent) -> Vec<Chapter> {
    if !ep.feed_chapters.is_empty() {
        return ep.feed_chapters.clone();
    }
    let Some(url) = &ep.chapters_url else { return vec![] };
    let res = (|| -> Result<Vec<Chapter>> {
        let r = http_get(agent, url, &[])?;
        let data: Value = serde_json::from_slice(&undo_compression(read_all(r, 20 << 20)?))?;
        let mut out: Vec<Chapter> = data["chapters"]
            .as_array()
            .cloned()
            .unwrap_or_default()
            .iter()
            .filter(|c| c.get("startTime").is_some() && c.get("toc") != Some(&Value::Bool(false)))
            .filter_map(|c| {
                Some(Chapter {
                    start: parse_timestamp_value(&c["startTime"])?,
                    title: crate::config::scalar_text(c.get("title").unwrap_or(&Value::Null)).trim().to_string(),
                })
            })
            .collect();
        out.sort_by(|a, b| a.start.total_cmp(&b.start));
        Ok(out)
    })();
    res.unwrap_or_else(|e| {
        log::warn!("Could not load chapters from {url}: {e}");
        vec![]
    })
}

/// Separate publisher chapters titled like ads into ad ranges; return (content_chapters, ads).
pub fn split_ad_chapters(chapters: &[Chapter], duration: f64) -> (Vec<Chapter>, Vec<Ad>) {
    let mut content = vec![];
    let mut ads = vec![];
    for (i, ch) in chapters.iter().enumerate() {
        if AD_TITLE.is_match(&ch.title) {
            let end = chapters.get(i + 1).map(|c| c.start).unwrap_or(duration);
            ads.push(Ad { start: ch.start, end, label: ch.title.clone(), source: "feed".into() });
        } else {
            content.push(ch.clone());
        }
    }
    (content, ads)
}

// ---------------- downloads ----------------

static HOST_SEGMENT: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)^([a-z0-9-]+\.)+[a-z]{2,}$").unwrap());
const AUDIO_EXT: &[&str] = &[".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac", ".mp4"];

/// Podcast links are often chains of tracking prefixes, each holding the next address:
///     https://arttrk.com/p/X/verifi.podscribe.com/rss/p/.../traffic.megaphone.fm/ABC.mp3
/// Return the addresses inside the chain, innermost (the real host) first.
pub fn unwrap_tracking(url: &str) -> Vec<String> {
    let Ok(u) = url::Url::parse(url) else { return vec![] };
    let path = u.path();
    let segs: Vec<&str> = path.split('/').collect();
    let mut out = vec![];
    for (i, seg) in segs.iter().enumerate() {
        let lower = seg.to_lowercase();
        if i == 0 || seg.is_empty() || AUDIO_EXT.iter().any(|e| lower.ends_with(e))
            || !(4..=253).contains(&seg.len()) || !HOST_SEGMENT.is_match(seg)
        {
            continue;
        }
        let rest = segs[i..].join("/");
        let q = u.query().map(|q| format!("?{q}")).unwrap_or_default();
        out.push(format!("{}://{rest}{q}", u.scheme()));
    }
    out.reverse();
    out
}

/// Download url to dest. Returns the address that worked.
///
/// With skip_trackers, the real host inside a tracking-prefix chain is tried first (more private,
/// and works when tracking domains are blocked); if that fails, the next addresses in the chain,
/// and finally the original link, are tried.
pub fn download(url: &str, dest: &Path, should_continue: &dyn Fn() -> bool, skip_trackers: bool) -> Result<String> {
    let inner = unwrap_tracking(url);
    let mut candidates: Vec<String> = if skip_trackers {
        inner.iter().cloned().chain([url.to_string()]).collect()
    } else {
        [url.to_string()].into_iter().chain(inner.iter().cloned()).collect()
    };
    let mut seen = std::collections::HashSet::new();
    candidates.retain(|c| seen.insert(c.clone()));
    download_from(&candidates, url, &inner, dest, should_continue)
}

/// Download exactly this address (no tracking-chain handling), e.g. model files.
pub fn download_plain(url: &str, dest: &Path, should_continue: &dyn Fn() -> bool) -> Result<String> {
    download_from(&[url.to_string()], url, &[], dest, should_continue)
}

fn download_from(candidates: &[String], url: &str, inner: &[String], dest: &Path, should_continue: &dyn Fn() -> bool) -> Result<String> {
    let tmp = PathBuf::from(format!("{}.part", dest.display()));
    let agent = agent(Duration::from_secs(300));
    let mut errors = vec![];
    for candidate in candidates {
        let res = (|| -> Result<()> {
            let r = http_get(&agent, candidate, &[("Accept-Encoding", "identity")])?;
            let ctype = r.header("Content-Type").unwrap_or("").to_string();
            if ctype.starts_with("text/html") {
                bail!("got a web page ({ctype}), not audio");
            }
            let encoded = !matches!(r.header("Content-Encoding").unwrap_or("").to_lowercase().as_str(), "" | "identity");
            let mut reader = r.into_reader();
            let mut f = File::create(&tmp)?;
            let mut buf = vec![0u8; 1 << 20];
            loop {
                if !should_continue() {
                    return Err(Cancelled.into());
                }
                let n = reader.read(&mut buf)?;
                if n == 0 {
                    break;
                }
                f.write_all(&buf[..n])?;
            }
            drop(f);
            if encoded {
                // genuinely compressed audio (rare) - unpack it
                let mut head = [0u8; 2];
                File::open(&tmp)?.read_exact(&mut head).ok();
                if head == [0x1f, 0x8b] {
                    let d = PathBuf::from(format!("{}.d", tmp.display()));
                    let mut src = flate2::read::MultiGzDecoder::new(File::open(&tmp)?);
                    std::io::copy(&mut src, &mut File::create(&d)?)?;
                    fs::rename(&d, &tmp)?;
                }
            }
            fs::rename(&tmp, dest)?;
            Ok(())
        })();
        match res {
            Ok(()) => {
                if candidate != url {
                    let hops = inner.iter().position(|c| c == candidate).map(|i| inner.len() - i).unwrap_or(0);
                    log::info!("Downloaded via {} (skipped {hops} tracking hop(s))", host_of(candidate));
                }
                return Ok(candidate.clone());
            }
            Err(e) => {
                let _ = fs::remove_file(&tmp);
                if e.is::<Cancelled>() {
                    return Err(e);
                }
                let host = host_of(candidate);
                let msg = crate::utils::prefix(&e.to_string(), 200);
                log::warn!("Download from {host} failed: {msg}");
                errors.push(format!("{host}: {msg}"));
            }
        }
    }
    Err(anyhow!("Could not download {}. Tried: {}", if inner.is_empty() && candidates.len() == 1 { "the file" } else { "the episode" }, errors.join("; ")))
}

#[cfg(test)]
mod tests {
    use super::*;

    const RSS: &str = r#"<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:psc="http://podlove.org/simple-chapters" xmlns:podcast="https://podcastindex.org/namespace/1.0">
<channel><title>Show &amp; Tell</title><link>https://ex.com</link><itunes:author>Me</itunes:author>
<itunes:image href="https://ex.com/a.jpg"/><description>Desc</description>
<item><title>Old</title><guid>g1</guid><pubDate>Mon, 07 Oct 2024 10:00:00 GMT</pubDate>
<enclosure url="https://ex.com/1.mp3" type="audio/mpeg" length="1"/></item>
<item><title>New</title><guid> g2 </guid><pubDate>Tue, 8 Oct 2024 10:00 EST</pubDate>
<enclosure url="https://ex.com/2.mp3" type="audio/mpeg"/><itunes:duration>1:02:03</itunes:duration>
<podcast:chapters url="https://ex.com/c.json" type="application/json+chapters"/>
<psc:chapters><psc:chapter start="00:00:00" title="Intro"/><psc:chapter start="00:05:00.5" title="Ad: Acme"/><psc:chapter start="00:06:00" title="Talk"/></psc:chapters>
</item>
<item><title>No audio</title><guid>g3</guid></item>
</channel></rss>"#;

    #[test]
    fn parse_rss() {
        let f = parse_feed(RSS.as_bytes(), "u").unwrap();
        assert_eq!(f.title, "Show & Tell");
        assert_eq!(f.author, "Me");
        assert_eq!(f.episodes.len(), 2);
        assert_eq!(f.episodes[0].guid, "g2");
        assert_eq!(f.episodes[0].duration, "1:02:03");
        assert_eq!(f.episodes[0].feed_chapters.len(), 3);
        assert_eq!(f.episodes[0].chapters_url.as_deref(), Some("https://ex.com/c.json"));
        assert_eq!(f.episodes[1].image_url.as_deref(), Some("https://ex.com/a.jpg"));
        let (content, ads) = split_ad_chapters(&f.episodes[0].feed_chapters, 1000.0);
        assert_eq!(content.len(), 2);
        assert_eq!(ads[0].end, 360.0);
        assert_eq!(f.episodes[0].published_iso().unwrap(), "2024-10-08T15:00:00+00:00");
    }

    #[test]
    fn trackers() {
        let v = unwrap_tracking("https://arttrk.com/p/ABC/verifi.podscribe.com/rss/p/traffic.megaphone.fm/X.mp3?a=1");
        assert_eq!(v[0], "https://traffic.megaphone.fm/X.mp3?a=1");
        assert_eq!(v.len(), 2);
    }

    #[test]
    fn compression() {
        use flate2::write::GzEncoder;
        let mut e = GzEncoder::new(Vec::new(), flate2::Compression::default());
        e.write_all(b"<rss/>").unwrap();
        assert_eq!(undo_compression(e.finish().unwrap()), b"<rss/>");
        assert_eq!(undo_compression(b"<rss/>".to_vec()), b"<rss/>");
    }

    #[test]
    fn dates() {
        assert!(parse_feed_date("Thu, 10 Oct 2024 09:00:00 +0000").is_some());
        assert!(parse_feed_date("Thurs, 10 Oct 2024 09:00:00 PDT").is_some());
        assert!(parse_feed_date("2024-10-10T09:00:00Z").is_some());
    }
}
