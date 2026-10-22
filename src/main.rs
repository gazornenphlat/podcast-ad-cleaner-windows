//! Podcast Ad Cleaner - command line entry point.
//!
//!     PodcastAdCleaner                 # window + system tray (default)
//!     PodcastAdCleaner tray [--minimized] [--no-gui]
//!     PodcastAdCleaner once            # one check in the console, then exit
//!     PodcastAdCleaner search "name"   # find a podcast's RSS feed on iTunes
//!     PodcastAdCleaner check           # validate config, ffmpeg, Ollama, Whisper
//!     PodcastAdCleaner serve           # only the RSS feed server
//!     PodcastAdCleaner list            # episodes you can choose (auto_download: false shows)
//!     PodcastAdCleaner get 3 5         # queue those for download
//!     PodcastAdCleaner skip 4          # skip one (never download it)

// No console window when double-clicked; command-line use re-attaches to the terminal (below).
#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

use std::collections::{HashMap, HashSet};
use std::io::Write;
use std::path::PathBuf;

use clap::{Parser, Subcommand};
use podcast_ad_cleaner::config::{config_path, load_config, Config, ConfigError};
use podcast_ad_cleaner::state::{peek_state, s, DownloadRequests};
#[allow(unused_imports)]
use podcast_ad_cleaner::{analyze, app, feeds, logging, media, pipeline, server, transcribe, utils};
use serde_json::{Map, Value};

#[derive(Parser)]
#[command(name = "PodcastAdCleaner", about = "Ad-free podcast processor", version)]
struct Cli {
    /// path to config.yaml
    #[arg(short, long, global = true)]
    config: Option<PathBuf>,
    #[command(subcommand)]
    cmd: Option<Cmd>,
}

#[derive(Subcommand)]
enum Cmd {
    /// run the app: window + system tray (default)
    Tray {
        /// start hidden in the tray (used at Windows startup)
        #[arg(long)]
        minimized: bool,
        /// tray icon only, no window
        #[arg(long = "no-gui")]
        no_gui: bool,
    },
    /// check all podcasts once in the console
    Once,
    /// search iTunes for a podcast RSS feed
    Search { term: Vec<String> },
    /// verify config and dependencies
    Check,
    /// run only the feed server in the console
    Serve,
    /// list episodes you can choose (shows with auto_download: false)
    List,
    /// queue episodes from 'list' for download
    Get { numbers: Vec<String> },
    /// skip episodes from 'list' (never download them)
    Skip { numbers: Vec<String> },
}

fn cmd_once(cfg_file: Option<&PathBuf>) -> Result<i32, ConfigError> {
    let cfg = load_config(cfg_file.map(|p| p.as_path()))?;
    logging::setup_logging(Some(&cfg), true);
    let hooks = pipeline::Hooks { status: std::sync::Arc::new(|m| println!("  > {m}")), ..Default::default() };
    let res = pipeline::run_cycle(&cfg, &hooks);
    println!("\nCleaned: {}  Failed: {}  Errors: {}", res.processed.len(), res.failed.len(), res.errors.len());
    for e in &res.errors {
        println!("  ! {e}");
    }
    Ok(if res.failed.is_empty() && res.errors.is_empty() { 0 } else { 1 })
}

fn cmd_search(term: &[String]) -> i32 {
    let results = match feeds::itunes_search(&term.join(" "), &feeds::make_session(), 10) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("{e}");
            return 1;
        }
    };
    if results.is_empty() {
        println!("No podcasts found.");
        return 1;
    }
    for (i, r) in results.iter().enumerate() {
        println!("{:2}. {}  (by {})\n    {}", i + 1, r.name, r.author, r.feed_url);
    }
    print!("\nNumber to print a config.yaml entry for (Enter to skip): ");
    let _ = std::io::stdout().flush();
    let mut line = String::new();
    if std::io::stdin().read_line(&mut line).is_err() {
        return 0;
    }
    if let Ok(n) = line.trim().parse::<usize>() {
        if (1..=results.len()).contains(&n) {
            let r = &results[n - 1];
            println!("\nAdd this under 'podcasts:' in config.yaml:\n");
            println!("  - name: \"{}\"\n    rss: \"{}\"\n", r.name.replace('"', "'"), r.feed_url);
        }
    }
    0
}

fn cmd_check(cfg_file: Option<&PathBuf>) -> i32 {
    let mut ok = true;
    let mut report = |good: bool, msg: String| {
        ok &= good;
        println!("  [{}] {msg}", if good { "OK" } else { "!!" });
    };
    let cfg = match load_config(cfg_file.map(|p| p.as_path())) {
        Ok(c) => {
            report(true, format!("config: {} ({} podcasts)", c.path.display(), c.podcasts.len()));
            c
        }
        Err(e) => {
            report(false, e.to_string());
            return 1;
        }
    };
    for (tool, path) in [("ffmpeg", &cfg.tools.ffmpeg), ("ffprobe", &cfg.tools.ffprobe)] {
        let resolved = media::resolve_tool(path);
        report(media::check_tool(&resolved), format!("{tool}: {resolved}"));
    }
    let (good, msg) = analyze::check_ollama(&cfg.ollama);
    report(good, format!("ollama: {}", if good { cfg.ollama.model.clone() } else { msg }));
    if cfg!(feature = "whisper") {
        let model = transcribe::model_dir(&cfg.whisper).join(transcribe::model_file_name(&cfg.whisper));
        let have = if model.is_file() { "downloaded" } else { "downloads on first use" };
        report(true, format!("whisper.cpp: model {} ({have}), device {}", cfg.whisper.model, transcribe::resolve_device(&cfg.whisper)));
    } else {
        report(false, "this build has no Whisper support".into());
    }
    if cfg.server.enabled {
        report(true, format!("feed server: http://{}:{}/", server::lan_address(), cfg.server.port as u16));
    }
    let agent = feeds::make_session();
    for p in &cfg.podcasts {
        let Some(rss) = p.rss() else {
            report(true, format!("{}: will be looked up on iTunes ('{}')", p.name, p.search().unwrap_or("")));
            continue;
        };
        match feeds::fetch_feed(rss, &agent) {
            Ok(f) => report(true, format!("{}: {} episodes, newest '{}'", p.name, f.episodes.len(),
                f.episodes.first().map(|e| e.title.as_str()).unwrap_or("-"))),
            Err(e) => report(false, format!("{}: {e}", p.name)),
        }
    }
    if ok { 0 } else { 1 }
}

fn cmd_serve(cfg_file: Option<&PathBuf>) -> Result<i32, ConfigError> {
    let cfg = load_config(cfg_file.map(|p| p.as_path()))?;
    logging::setup_logging(Some(&cfg), true);
    let c2 = cfg.clone();
    let srv = server::FeedServer::new(std::sync::Arc::new(move || Some(c2.clone())));
    if !srv.start(&cfg) {
        return Ok(1);
    }
    println!("Serving feeds at {}  (Ctrl+C to stop)", srv.base_url(false));
    srv.join();
    Ok(0)
}

type Choices = Vec<(String, PathBuf, Vec<(String, Map<String, Value>)>)>;

fn manual_choices(cfg: &Config) -> Choices {
    cfg.podcasts.iter().filter(|p| p.enabled && !p.auto_download).map(|p| {
        let folder = cfg.podcast_folder(&p.name);
        let eps = if folder.is_dir() { peek_state(&folder).choosable(50) } else { vec![] };
        (p.name.clone(), folder, eps)
    }).collect()
}

fn fname(p: &std::path::Path) -> String {
    p.file_name().unwrap_or_default().to_string_lossy().into()
}

fn cmd_list(cfg_file: Option<&PathBuf>) -> Result<i32, ConfigError> {
    let cfg = load_config(cfg_file.map(|p| p.as_path()))?;
    let reqs: HashMap<(String, String), String> = DownloadRequests::new(&cfg.output_dir).all().into_iter()
        .map(|r| ((r.folder, r.guid), r.action)).collect();
    let shows = manual_choices(&cfg);
    if shows.is_empty() {
        println!("No podcasts have auto_download: false.");
        return Ok(0);
    }
    let mut n = 0;
    for (name, folder, eps) in shows {
        println!("\n{name}");
        let eps: Vec<_> = eps.into_iter().filter(|(g, _)| reqs.get(&(fname(&folder), g.clone())).map(String::as_str) != Some("skip")).collect();
        if eps.is_empty() {
            println!("   (nothing new)");
        }
        for (g, ep) in eps {
            n += 1;
            let mut flag = match s(&ep, "status") { "queued" => " [queued]", "failed" => " [failed]", _ => "" };
            if reqs.get(&(fname(&folder), g)).map(String::as_str) == Some("download") {
                flag = " [queued]";
            }
            println!("  {n:3}. {}  {}{flag}", utils::prefix(s(&ep, "published"), 10), s(&ep, "title"));
        }
    }
    println!("\nDownload:  PodcastAdCleaner get <number> [<number> ...]   (or 'get all')");
    println!("Skip:      PodcastAdCleaner skip <number> [<number> ...]  (or 'skip all')");
    Ok(0)
}

fn cmd_get(cfg_file: Option<&PathBuf>, numbers: &[String], action: &str) -> Result<i32, ConfigError> {
    let cfg = load_config(cfg_file.map(|p| p.as_path()))?;
    let req = DownloadRequests::new(&cfg.output_dir);
    let skipped: HashSet<(String, String)> = req.skips().into_iter().map(|r| (r.folder, r.guid)).collect();
    let flat: Vec<(String, String, Map<String, Value>)> = manual_choices(&cfg).into_iter()
        .flat_map(|(_, folder, eps)| { let f = fname(&folder); eps.into_iter().map(move |(g, e)| (f.clone(), g, e)) })
        .filter(|(f, g, _)| !skipped.contains(&(f.clone(), g.clone())))
        .collect();
    let picks: Vec<usize> = if numbers == ["all"] {
        (1..=flat.len()).collect()
    } else {
        match numbers.iter().map(|x| x.parse::<usize>()).collect::<Result<Vec<_>, _>>() {
            Ok(v) if !v.is_empty() => v,
            _ => {
                println!("Give episode numbers from 'list', or 'all'.");
                return Ok(2);
            }
        }
    };
    for i in picks {
        if i < 1 || i > flat.len() {
            println!("  ! no episode #{i}");
            continue;
        }
        let (folder, guid, ep) = &flat[i - 1];
        if let Err(e) = req.add(folder, guid, s(ep, "title"), action) {
            println!("  ! {e}");
            continue;
        }
        println!("  {} {}", if action == "skip" { "-" } else { "+" }, s(ep, "title"));
    }
    if action == "skip" {
        println!("Skipped. They won't be downloaded and disappear from the list.");
    } else {
        println!("Queued. The tray app downloads them at its next check (or right-click > Check now).");
    }
    Ok(0)
}

/// A windowed exe has no console; when run from cmd/PowerShell with a command, reuse that console.
fn attach_console() {
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::System::Console::{AttachConsole, ATTACH_PARENT_PROCESS};
        AttachConsole(ATTACH_PARENT_PROCESS);
    }
}

fn main() {
    let cli = Cli::parse_from(std::env::args_os());
    if !matches!(cli.cmd, None | Some(Cmd::Tray { .. })) {
        attach_console();
    }
    let cfg = cli.config.as_ref();
    let code = match &cli.cmd {
        None => run_tray(cfg, false, false),
        Some(Cmd::Tray { minimized, no_gui }) => run_tray(cfg, *minimized, *no_gui),
        Some(Cmd::Once) => cmd_once(cfg).unwrap_or_else(config_error),
        Some(Cmd::Search { term }) => cmd_search(term),
        Some(Cmd::Check) => cmd_check(cfg),
        Some(Cmd::Serve) => cmd_serve(cfg).unwrap_or_else(config_error),
        Some(Cmd::List) => cmd_list(cfg).unwrap_or_else(config_error),
        Some(Cmd::Get { numbers }) => cmd_get(cfg, numbers, "download").unwrap_or_else(config_error),
        Some(Cmd::Skip { numbers }) => cmd_get(cfg, numbers, "skip").unwrap_or_else(config_error),
    };
    log::logger().flush();
    std::process::exit(code);
}

fn config_error(e: ConfigError) -> i32 {
    log::error!("{e}");
    eprintln!("Config error: {e}");
    2
}

fn run_tray(cfg: Option<&PathBuf>, minimized: bool, no_gui: bool) -> i32 {
    let path = config_path(cfg.map(|p| p.as_path()));
    #[cfg(feature = "gui")]
    {
        podcast_ad_cleaner::gui::run_tray(path, minimized, !no_gui)
    }
    #[cfg(not(feature = "gui"))]
    {
        let _ = (minimized, no_gui);
        app::create_config_if_missing(&path, true);
        app::run_headless(path)
    }
}
