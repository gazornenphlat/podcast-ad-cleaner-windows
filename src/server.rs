//! Small HTTP server that publishes the cleaned episodes as podcast RSS feeds.
//!
//! Subscribe from a phone podcast app on the same network (or over Tailscale/VPN):
//!     http://<pc-address>:8585/feed/<podcast>.xml
//! Supports HTTP Range requests so apps can seek and resume downloads.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::net::UdpSocket;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

use chrono::{DateTime, Local, Utc};
use percent_encoding::{percent_decode_str, utf8_percent_encode, AsciiSet, NON_ALPHANUMERIC};
use serde_json::{Map, Value};
use tiny_http::{Header, Method, Request, Response, Server, StatusCode};

use crate::config::Config;
use crate::state::{peek_state, s, PodcastState, STATE_FILENAME};
use crate::utils::parse_iso;

/// Python's urllib.parse.quote (safe="/")
const QUOTE: &AsciiSet = &NON_ALPHANUMERIC.remove(b'_').remove(b'.').remove(b'-').remove(b'~').remove(b'/');

pub fn quote(s: &str) -> String {
    utf8_percent_encode(s, QUOTE).to_string()
}

fn media_type(ext: &str) -> Option<&'static str> {
    match ext {
        "mp3" => Some("audio/mpeg"),
        "json" => Some("application/json+chapters"),
        "vtt" => Some("text/vtt"),
        "txt" => Some("text/plain; charset=utf-8"),
        _ => None,
    }
}

/// Best guess at this PC's LAN IP (no packets are sent).
pub fn lan_address() -> String {
    UdpSocket::bind("0.0.0.0:0")
        .and_then(|s| {
            s.connect("10.255.255.255:1")?;
            s.local_addr()
        })
        .map(|a| a.ip().to_string())
        .unwrap_or_else(|_| "127.0.0.1".into())
}

pub fn slugify(name: &str) -> String {
    let re = regex::Regex::new(r"[^a-z0-9]+").unwrap();
    let s = re.replace_all(&name.to_lowercase(), "-").trim_matches('-').to_string();
    if s.is_empty() { "podcast".into() } else { s }
}

pub fn xml_escape(v: &str) -> String {
    v.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

fn attr(v: &str) -> String {
    xml_escape(v).replace('"', "&quot;")
}

fn html_escape(v: &str) -> String {
    attr(v).replace('\'', "&#x27;")
}

pub struct Show {
    pub folder: PathBuf,
    pub slug: String,
    pub state: Arc<PodcastState>,
    pub name: String,
}

pub struct ShowEpisode {
    pub guid: String,
    pub info: Map<String, Value>,
    pub path: PathBuf,
}

impl Show {
    fn new(folder: PathBuf) -> Self {
        let name = folder.file_name().unwrap_or_default().to_string_lossy().to_string();
        Self { slug: slugify(&name), state: peek_state(&folder), folder, name }
    }

    pub fn meta(&self, k: &str) -> String {
        self.state.data.get("feed_meta").and_then(|m| m.get(k)).and_then(Value::as_str).unwrap_or("").to_string()
    }

    pub fn episodes(&self) -> Vec<ShowEpisode> {
        let mut eps: Vec<ShowEpisode> = self.state.episodes().iter()
            .filter_map(|(g, e)| {
                let e = e.as_object()?;
                if s(e, "status") != "done" || s(e, "file").is_empty() {
                    return None;
                }
                let path = self.folder.join(s(e, "file"));
                path.is_file().then(|| ShowEpisode { guid: g.clone(), info: e.clone(), path })
            })
            .collect();
        eps.sort_by_key(|e| std::cmp::Reverse(sort_date(&e.info)));
        eps
    }
}

fn sort_date(e: &Map<String, Value>) -> String {
    let p = s(e, "published");
    if p.is_empty() { s(e, "updated").into() } else { p.into() }
}

pub fn find_shows(output_dir: &Path) -> BTreeMap<String, Show> {
    let mut shows = BTreeMap::new();
    let Ok(rd) = std::fs::read_dir(output_dir) else { return shows };
    let mut dirs: Vec<PathBuf> = rd.flatten().map(|d| d.path()).filter(|p| p.is_dir() && p.join(STATE_FILENAME).exists()).collect();
    dirs.sort();
    for d in dirs {
        let show = Show::new(d);
        shows.entry(show.slug.clone()).or_insert(show);
    }
    shows
}

fn rfc822(iso: &str) -> String {
    let dt = parse_iso(iso).map(|t| t.aware()).unwrap_or_else(|| Local::now().fixed_offset());
    dt.to_rfc2822()
}

#[allow(clippy::too_many_arguments)]
pub fn build_rss(title: &str, items: &[(&Show, ShowEpisode)], base: &str, image: &str, author: &str, description: &str,
                 link: &str, self_url: &str, prefix_show: bool) -> String {
    let link = if link.is_empty() { format!("{base}/") } else { link.to_string() };
    let mut out = vec![
        r#"<?xml version="1.0" encoding="UTF-8"?>"#.to_string(),
        r#"<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:podcast="https://podcastindex.org/namespace/1.0" xmlns:atom="http://www.w3.org/2005/Atom">"#.into(),
        "<channel>".into(),
        format!("<title>{}</title>", xml_escape(title)),
        format!(r#"<atom:link href="{}" rel="self" type="application/rss+xml"/>"#, attr(self_url)),
        format!("<link>{}</link>", xml_escape(&link)),
        format!("<description>{}</description>", xml_escape(if description.is_empty() { title } else { description })),
        format!("<itunes:author>{}</itunes:author>", xml_escape(author)),
        "<itunes:block>Yes</itunes:block>".into(), // keep private feeds out of directories
        format!("<lastBuildDate>{}</lastBuildDate>", Utc::now().to_rfc2822()),
    ];
    if !image.is_empty() {
        out.push(format!(r#"<itunes:image href="{}"/>"#, attr(image)));
        out.push(format!("<image><url>{}</url><title>{}</title><link>{}</link></image>", xml_escape(image), xml_escape(title), xml_escape(&link)));
    }
    for (show, ep) in items {
        let media = format!("{base}/media/{}", show.slug);
        let t = s(&ep.info, "title");
        let ep_title = if prefix_show { format!("{}: {t}", show.name) } else { t.to_string() };
        let size = std::fs::metadata(&ep.path).map(|m| m.len()).unwrap_or(0);
        out.extend([
            "<item>".into(),
            format!("<title>{}</title>", xml_escape(&ep_title)),
            format!(r#"<guid isPermaLink="false">{}</guid>"#, xml_escape(&ep.guid)),
            format!("<pubDate>{}</pubDate>", rfc822(s(&ep.info, "published"))),
            format!(r#"<enclosure url="{}" length="{size}" type="audio/mpeg"/>"#, attr(&format!("{media}/{}", quote(s(&ep.info, "file"))))),
            format!("<description>{}</description>", xml_escape(s(&ep.info, "description"))),
        ]);
        let dur = crate::state::num(&ep.info, "duration");
        if dur > 0.0 {
            out.push(format!("<itunes:duration>{}</itunes:duration>", dur as i64));
        }
        let base_name = s(&ep.info, "assets_base");
        if !base_name.is_empty() {
            let chap = show.folder.join("assets").join(format!("{base_name}.chapters.json"));
            if chap.is_file() {
                out.push(format!(r#"<podcast:chapters url="{}" type="application/json+chapters"/>"#,
                    attr(&format!("{media}/assets/{}", quote(&format!("{base_name}.chapters.json"))))));
            }
            for sub in ["transcripts", "assets"] {
                // archive has every episode; assets only the latest
                if show.folder.join(sub).join(format!("{base_name}.vtt")).is_file() {
                    out.push(format!(r#"<podcast:transcript url="{}" type="text/vtt"/>"#,
                        attr(&format!("{media}/{sub}/{}", quote(&format!("{base_name}.vtt"))))));
                    break;
                }
            }
        }
        out.push("</item>".into());
    }
    out.push("</channel>".into());
    out.push("</rss>".into());
    out.join("\n")
}

type CfgGetter = Arc<dyn Fn() -> Option<Config> + Send + Sync>;

fn header(k: &str, v: &str) -> Header {
    Header::from_bytes(k.as_bytes(), v.as_bytes()).unwrap()
}

fn send(req: Request, code: u16, body: Vec<u8>, ctype: &str, head: bool) {
    let len = body.len();
    let resp = Response::new(StatusCode(code), vec![header("Content-Type", ctype), header("Cache-Control", "no-cache")],
        if head { &[][..] } else { &body[..] }, Some(len), None);
    let _ = req.respond(resp);
}

fn error(req: Request, code: u16, head: bool) {
    send(req, code, format!("{code}\n").into_bytes(), "text/plain", head);
}

fn send_file(req: Request, path: &Path, ctype: &str, head: bool) {
    let Ok(meta) = std::fs::metadata(path) else { return error(req, 404, head) };
    let size = meta.len();
    let (mut start, mut end) = (0u64, size.saturating_sub(1));
    let mut partial = false;
    let range = req.headers().iter().find(|h| h.field.equiv("Range")).map(|h| h.value.as_str().trim().to_string());
    let bad = |req: Request| {
        let _ = req.respond(Response::empty(416).with_header(header("Content-Range", &format!("bytes */{size}"))));
    };
    if let Some(r) = range {
        let re = regex::Regex::new(r"^bytes=(\d*)-(\d*)$").unwrap();
        let Some(c) = re.captures(&r) else { return bad(req) };
        let (a, b) = (&c[1], &c[2]);
        if a.is_empty() && b.is_empty() {
            return bad(req);
        }
        if !a.is_empty() {
            start = a.parse().unwrap_or(u64::MAX);
            end = if b.is_empty() { size.saturating_sub(1) } else { b.parse::<u64>().unwrap_or(u64::MAX).min(size.saturating_sub(1)) };
        } else {
            start = size.saturating_sub(b.parse().unwrap_or(0));
        }
        if start > end || start >= size {
            return bad(req);
        }
        partial = true;
    }
    let length = if size == 0 { 0 } else { end - start + 1 };
    let modified: DateTime<Utc> = meta.modified().map(DateTime::from).unwrap_or_else(|_| Utc::now());
    let mut headers = vec![
        header("Content-Type", ctype), header("Accept-Ranges", "bytes"),
        header("Last-Modified", &modified.format("%a, %d %b %Y %H:%M:%S GMT").to_string()),
    ];
    if partial {
        headers.push(header("Content-Range", &format!("bytes {start}-{end}/{size}")));
    }
    let code = if partial { 206 } else { 200 };
    if head {
        let _ = req.respond(Response::new(StatusCode(code), headers, std::io::empty(), Some(length as usize), None));
        return;
    }
    let Ok(mut f) = File::open(path) else { return error(req, 404, head) };
    if f.seek(SeekFrom::Start(start)).is_err() {
        return error(req, 500, head);
    }
    let _ = req.respond(Response::new(StatusCode(code), headers, f.take(length), Some(length as usize), None));
}

fn base_url(req: &Request, cfg: &Config) -> String {
    let srv = &cfg.server;
    let root = srv.public_url.trim_end_matches('/').to_string();
    let root = if root.is_empty() {
        let host = req.headers().iter().find(|h| h.field.equiv("Host")).map(|h| h.value.to_string())
            .unwrap_or_else(|| format!("{}:{}", lan_address(), srv.port as u16));
        format!("http://{host}")
    } else {
        root
    };
    if srv.token.is_empty() { root } else { format!("{root}/{}", quote(&srv.token)) }
}

fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

fn handle(req: Request, get_cfg: &CfgGetter) {
    let head = *req.method() == Method::Head;
    if !head && *req.method() != Method::Get {
        return error(req, 405, false);
    }
    let Some(cfg) = get_cfg() else { return error(req, 503, head) };
    let path = req.url().split('?').next().unwrap_or("").to_string();
    let mut parts: Vec<String> = path.split('/').filter(|p| !p.is_empty())
        .map(|p| percent_decode_str(p).decode_utf8_lossy().to_string()).collect();
    if !cfg.server.token.is_empty() {
        if parts.is_empty() || !ct_eq(parts[0].as_bytes(), cfg.server.token.as_bytes()) {
            return error(req, 404, head);
        }
        parts.remove(0);
    }
    let shows = find_shows(&cfg.output_dir);
    let base = base_url(&req, &cfg);

    if parts.is_empty() {
        let body = index_page(&shows, &base, &cfg);
        return send(req, 200, body.into_bytes(), "text/html; charset=utf-8", head);
    }
    if parts[0] == "feed" && parts.len() == 2 && parts[1].ends_with(".xml") {
        let slug = &parts[1][..parts[1].len() - 4];
        let self_url = format!("{base}/feed/{}", quote(&parts[1]));
        let xml = if slug == "all" && cfg.server.include_combined_feed {
            let mut items: Vec<(&Show, ShowEpisode)> = shows.values().flat_map(|s| s.episodes().into_iter().map(move |e| (s, e))).collect();
            items.sort_by_key(|x| std::cmp::Reverse(s(&x.1.info, "published").to_string()));
            build_rss("All podcasts (Ad-Free)", &items, &base, "", "", "Every cleaned episode", "", &self_url, true)
        } else if let Some(sh) = shows.get(slug) {
            let items: Vec<(&Show, ShowEpisode)> = sh.episodes().into_iter().map(|e| (sh, e)).collect();
            let t = sh.meta("title");
            build_rss(&format!("{} (Ad-Free)", if t.is_empty() { &sh.name } else { &t }), &items, &base, &sh.meta("image"),
                &sh.meta("author"), &sh.meta("description"), &sh.meta("link"), &self_url, false)
        } else {
            return error(req, 404, head);
        };
        return send(req, 200, xml.into_bytes(), "application/rss+xml; charset=utf-8", head);
    }
    if parts[0] == "media" && (parts.len() == 3 || parts.len() == 4) {
        let Some(sh) = shows.get(&parts[1]) else { return error(req, 404, head) };
        let mut folder = sh.folder.clone();
        if parts.len() == 4 {
            if parts[2] != "assets" && parts[2] != "transcripts" {
                return error(req, 404, head);
            }
            folder = folder.join(&parts[2]);
        }
        let name = parts.last().unwrap();
        let ext = Path::new(name).extension().map(|e| e.to_string_lossy().to_lowercase()).unwrap_or_default();
        // only direct children of the podcast (or assets) folder, only known file types
        let ok_name = !name.starts_with('.') && !name.contains(['/', '\\']) && name != ".." && !name.contains(':');
        let Some(ctype) = media_type(&ext).filter(|_| ok_name) else { return error(req, 404, head) };
        let p = folder.join(name);
        let inside = p.canonicalize().ok().zip(folder.canonicalize().ok()).is_some_and(|(p, f)| p.parent() == Some(f.as_path()));
        if !inside || !p.is_file() || (parts.len() == 3 && ext != "mp3") {
            return error(req, 404, head);
        }
        return send_file(req, &p, ctype, head);
    }
    error(req, 404, head)
}

fn index_page(shows: &BTreeMap<String, Show>, base: &str, cfg: &Config) -> String {
    let mut feeds: Vec<(String, String, usize, String)> = shows.iter().map(|(slug, s)| {
        let t = s.meta("title");
        (if t.is_empty() { s.name.clone() } else { t }, format!("{base}/feed/{slug}.xml"), s.episodes().len(), s.meta("image"))
    }).collect();
    if cfg.server.include_combined_feed && !shows.is_empty() {
        let total = feeds.iter().map(|f| f.2).sum();
        feeds.insert(0, ("All podcasts (combined)".into(), format!("{base}/feed/all.xml"), total, String::new()));
    }
    let rows: String = feeds.iter().map(|(title, url, count, img)| {
        let pic = if img.is_empty() { r#"<div class="ph"></div>"#.to_string() } else { format!(r#"<img src="{}" alt="">"#, html_escape(img)) };
        format!(r#"<li>{pic}<div class="t"><b>{}</b><span>{count} episode{}</span><code>{}</code></div><button onclick="navigator.clipboard.writeText({});this.textContent='Copied'">Copy</button></li>"#,
            html_escape(title), if *count != 1 { "s" } else { "" }, html_escape(url), html_escape(&serde_json::to_string(url).unwrap()))
    }).collect();
    let empty = if shows.is_empty() { "<p>No cleaned episodes yet. Feeds appear here after the first episode is processed.</p>" } else { "" };
    format!(r#"<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ad-Free Podcast Feeds</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mut:#667085;--acc:#1f6feb;--line:#e4e7ec}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1115;--card:#181b22;--fg:#e6e8ec;--mut:#98a2b3;--acc:#5b9dff;--line:#2a2f3a}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,Segoe UI,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:24px 16px}}
h1{{font-size:1.4rem;margin:0 0 4px}} p{{color:var(--mut)}}
ul{{list-style:none;padding:0;margin:20px 0}}
li{{display:flex;gap:14px;align-items:center;background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:12px;margin-bottom:10px}}
img,.ph{{width:56px;height:56px;border-radius:8px;object-fit:cover;flex:none;background:var(--line)}}
.t{{flex:1;min-width:0;display:flex;flex-direction:column}} .t span{{color:var(--mut);font-size:.85rem}}
code{{font-size:.78rem;color:var(--mut);overflow-wrap:anywhere}}
button{{flex:none;border:0;border-radius:8px;padding:8px 14px;background:var(--acc);color:#fff;font:inherit;cursor:pointer}}
</style></head><body><main>
<h1>Ad-Free Podcast Feeds</h1>
<p>In your podcast app choose <i>Add by URL / Add RSS feed</i> and paste a feed address.
Your phone must be on the same network as this PC (or connected by VPN / Tailscale).</p>
<ul>{rows}</ul>{empty}
</main></body></html>"#)
}

pub struct FeedServer {
    get_cfg: CfgGetter,
    server: Mutex<Option<Arc<Server>>>,
    threads: Mutex<Vec<JoinHandle<()>>>,
    port: Mutex<u16>,
}

impl FeedServer {
    pub fn new(get_cfg: CfgGetter) -> Arc<Self> {
        Arc::new(Self { get_cfg, server: Mutex::new(None), threads: Mutex::new(vec![]), port: Mutex::new(0) })
    }

    pub fn running(&self) -> bool {
        self.server.lock().unwrap().is_some()
    }

    pub fn start(&self, cfg: &Config) -> bool {
        let port = cfg.server.port as u16;
        let addr = format!("{}:{port}", cfg.server.host);
        let server = match Server::http(&addr) {
            Ok(s) => Arc::new(s),
            Err(e) => {
                log::error!("Feed server could not listen on {addr} - {e}");
                return false;
            }
        };
        *self.port.lock().unwrap() = port;
        let mut threads = self.threads.lock().unwrap();
        for _ in 0..6 {
            let (srv, get_cfg) = (server.clone(), self.get_cfg.clone());
            threads.push(std::thread::Builder::new().name("feed-server".into()).spawn(move || {
                for req in srv.incoming_requests() {
                    let url = req.url().to_string();
                    if std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| handle(req, &get_cfg))).is_err() {
                        log::error!("Feed server error for {url}");
                    }
                }
            }).unwrap());
        }
        *self.server.lock().unwrap() = Some(server);
        log::info!("Feed server listening on {}", self.base_url(false));
        true
    }

    pub fn base_url(&self, local: bool) -> String {
        let cfg = (self.get_cfg)();
        let (public, token) = cfg.as_ref().map(|c| (c.server.public_url.clone(), c.server.token.clone())).unwrap_or_default();
        let root = if !public.is_empty() && !local {
            public.trim_end_matches('/').to_string()
        } else {
            format!("http://{}:{}", if local { "localhost".into() } else { lan_address() }, self.port.lock().unwrap())
        };
        format!("{root}{}/", if token.is_empty() { String::new() } else { format!("/{}", quote(&token)) })
    }

    /// Block until the server stops (for `serve`).
    pub fn join(&self) {
        let threads = std::mem::take(&mut *self.threads.lock().unwrap());
        for t in threads {
            let _ = t.join();
        }
    }

    pub fn stop(&self) {
        if let Some(s) = self.server.lock().unwrap().take() {
            for _ in 0..6 {
                s.unblock();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn helpers() {
        assert_eq!(slugify("The Daily!"), "the-daily");
        assert_eq!(slugify("!!"), "podcast");
        assert_eq!(quote("a b/c&d.mp3"), "a%20b/c%26d.mp3");
    }
}
