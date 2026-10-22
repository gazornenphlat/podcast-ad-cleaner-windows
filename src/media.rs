//! ffmpeg cutting, timeline remapping (original -> cleaned), chapter JSON and ID3 tagging.

use std::path::{Path, PathBuf};

use anyhow::{anyhow, bail, Result};
use id3::frame::{Chapter as ChapFrame, Comment, Picture, PictureType, TableOfContents};
use id3::{Frame, Tag, TagLike, Version};
use serde_json::json;

use crate::config::APP_DIR;
use crate::feeds::{Ad, Chapter};
use crate::utils::{run, suffix, Segment};

// ---------- tools ----------

/// Find a program the way a shell would (PATH, plus .exe on Windows).
pub fn which(name: &str) -> Option<PathBuf> {
    let p = Path::new(name);
    let exts: &[&str] = if cfg!(windows) { &["", ".exe", ".cmd", ".bat"] } else { &[""] };
    if p.components().count() > 1 || p.is_absolute() {
        return exts.iter().map(|e| PathBuf::from(format!("{name}{e}"))).find(|c| c.is_file());
    }
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        for e in exts {
            let c = dir.join(format!("{name}{e}"));
            if c.is_file() {
                return Some(c);
            }
        }
    }
    None
}

pub fn check_tool(path: &str) -> bool {
    which(path).is_some() || Path::new(path).is_file()
}

/// Use the configured tool if it exists, else an .exe shipped next to the app.
pub fn resolve_tool(name: &str) -> String {
    if check_tool(name) {
        return name.to_string();
    }
    let stem = Path::new(name).file_stem().map(|s| s.to_string_lossy().to_string()).unwrap_or_else(|| name.into());
    let exe = if cfg!(windows) { format!("{stem}.exe") } else { stem };
    for c in [APP_DIR.join(&exe), APP_DIR.join("ffmpeg").join(&exe)] {
        if c.is_file() {
            return c.to_string_lossy().into();
        }
    }
    name.to_string()
}

pub fn probe_duration(ffprobe: &str, path: &Path) -> Result<f64> {
    let out = run(ffprobe, &[
        "-v".into(), "error".into(), "-show_entries".into(), "format=duration".into(),
        "-of".into(), "default=noprint_wrappers=1:nokey=1".into(), path.to_string_lossy().into(),
    ])
    .map_err(|e| anyhow!("could not run {ffprobe}: {e}"))?;
    String::from_utf8_lossy(&out.stdout).trim().parse::<f64>().map_err(|_| {
        anyhow!(
            "ffprobe could not read {}: {}",
            path.file_name().unwrap_or_default().to_string_lossy(),
            suffix(&String::from_utf8_lossy(&out.stderr), 300)
        )
    })
}

/// Complement of the ad ranges within [0, duration].
pub fn keep_ranges(ads: &[Ad], duration: f64, min_len: f64) -> Vec<(f64, f64)> {
    let mut out = vec![];
    let mut pos: f64 = 0.0;
    for a in ads {
        if a.start - pos >= min_len {
            out.push((pos, a.start));
        }
        pos = pos.max(a.end);
    }
    if duration - pos >= min_len {
        out.push((pos, duration));
    }
    out
}

/// Write dst as MP3 with ad ranges removed (one re-encode, sample-accurate cuts).
/// With crossfade_ms > 0 the kept pieces are joined with a short crossfade.
pub fn cut_ads(ffmpeg: &str, src: &Path, dst: &Path, ads: &[Ad], bitrate: &str, duration: f64, crossfade_ms: f64) -> Result<()> {
    let mut cmd: Vec<String> = vec!["-nostdin".into(), "-y".into(), "-i".into(), src.to_string_lossy().into()];
    let fade = crossfade_ms.max(0.0) / 1000.0;
    let pieces = if !ads.is_empty() && duration > 0.0 { keep_ranges(ads, duration, (fade * 3.0).max(0.25)) } else { vec![] };

    if !ads.is_empty() && fade > 0.0 && pieces.len() > 1 {
        let mut graph: Vec<String> = pieces
            .iter()
            .enumerate()
            .map(|(i, (a, b))| format!("[0:a:0]atrim=start={a:.3}:end={b:.3},asetpts=PTS-STARTPTS[p{i}]"))
            .collect();
        let mut prev = "p0".to_string();
        for i in 1..pieces.len() {
            let out = if i == pieces.len() - 1 { "out".to_string() } else { format!("x{i}") };
            graph.push(format!("[{prev}][p{i}]acrossfade=d={fade:.3}:c1=tri:c2=tri[{out}]"));
            prev = out;
        }
        cmd.extend(["-filter_complex".into(), graph.join(";"), "-map".into(), "[out]".into()]);
    } else if !ads.is_empty() {
        let expr: Vec<String> = ads.iter().map(|a| format!("between(t,{:.3},{:.3})", a.start, a.end)).collect();
        cmd.extend(["-map".into(), "0:a:0".into(), "-af".into(), format!("aselect='not({})',asetpts=N/SR/TB", expr.join("+"))]);
    } else if src.extension().map(|e| e.eq_ignore_ascii_case("mp3")).unwrap_or(false) {
        std::fs::copy(src, dst)?;
        return Ok(());
    } else {
        cmd.extend(["-map".into(), "0:a:0".into()]);
    }
    cmd.extend(["-vn", "-c:a", "libmp3lame", "-b:a", bitrate, "-map_metadata", "-1", "-id3v2_version", "3"].map(String::from));
    cmd.push(dst.to_string_lossy().into());
    let out = run(ffmpeg, &cmd).map_err(|e| anyhow!("could not run {ffmpeg}: {e}"))?;
    if !out.status.success() {
        bail!("ffmpeg failed: {}", suffix(&String::from_utf8_lossy(&out.stderr), 800));
    }
    Ok(())
}

// ---------- timeline remapping ----------

pub fn remap(t: f64, ads: &[Ad]) -> f64 {
    let mut removed = 0.0;
    for a in ads {
        if t <= a.start {
            break;
        }
        removed += t.min(a.end) - a.start;
    }
    (t - removed).max(0.0)
}

pub fn in_ad(t: f64, ads: &[Ad]) -> bool {
    ads.iter().any(|a| a.start <= t && t < a.end)
}

pub fn remap_segments(segments: &[Segment], ads: &[Ad]) -> Vec<Segment> {
    segments
        .iter()
        .filter(|s| !in_ad((s.start + s.end) / 2.0, ads))
        .map(|s| Segment { start: remap(s.start, ads), end: remap(s.end, ads), text: s.text.clone() })
        .collect()
}

/// Remap chapters to the cleaned timeline, de-duplicate, and guarantee one at 0:00.
pub fn build_chapters(chapters: &[Chapter], ads: &[Ad], clean_duration: f64, min_gap: f64, fallback_minutes: f64) -> Vec<Chapter> {
    let mut mapped: Vec<Chapter> = chapters
        .iter()
        .filter(|c| !c.title.is_empty())
        .map(|c| Chapter { start: remap(c.start, ads), title: c.title.clone() })
        .collect();
    mapped.sort_by(|a, b| a.start.total_cmp(&b.start));
    let mut result: Vec<Chapter> = vec![];
    for c in mapped {
        if c.start >= clean_duration - 5.0 {
            continue;
        }
        if let Some(last) = result.last() {
            if c.start - last.start < min_gap {
                continue;
            }
        }
        result.push(c);
    }
    if result.is_empty() {
        let step = (fallback_minutes * 60.0).max(60.0);
        let (mut t, mut n) = (0.0, 1);
        while t < clean_duration - 30.0 {
            result.push(Chapter { start: t, title: format!("Part {n}") });
            t += step;
            n += 1;
        }
    } else if result[0].start > 0.0 {
        if result[0].start < min_gap {
            result[0].start = 0.0;
        } else {
            result.insert(0, Chapter { start: 0.0, title: "Introduction".into() });
        }
    }
    result.into_iter().map(|c| Chapter { start: (c.start * 100.0).round() / 100.0, title: c.title }).collect()
}

// ---------- outputs ----------

pub fn write_chapters_json(path: &Path, chapters: &[Chapter], episode_title: &str, podcast: &str, author: &str) -> Result<()> {
    let data = json!({
        "version": "1.2.0", "title": episode_title, "podcastName": podcast, "author": author,
        "chapters": chapters.iter().map(|c| json!({"startTime": c.start, "title": c.title})).collect::<Vec<_>>(),
    });
    std::fs::write(path, crate::state::to_json_pretty(&data, 2))?;
    Ok(())
}

pub struct TagInfo<'a> {
    pub title: &'a str,
    pub podcast: &'a str,
    pub author: &'a str,
    pub date: Option<&'a str>,
    pub comment: &'a str,
    pub artwork: Option<(Vec<u8>, String)>,
    pub duration: f64,
}

pub fn tag_mp3(path: &Path, chapters: &[Chapter], info: &TagInfo) -> Result<()> {
    let mut tag = Tag::read_from_path(path).unwrap_or_else(|_| Tag::new());
    for id in ["CHAP", "CTOC", "TIT2", "TALB", "TPE1", "TDRC", "TYER", "TDAT", "TCON", "COMM", "APIC"] {
        tag.remove(id);
    }
    tag.set_title(info.title);
    tag.set_album(info.podcast);
    if !info.author.is_empty() {
        tag.set_artist(info.author);
    }
    if let Some(d) = info.date {
        if let Ok(ts) = d.parse::<id3::Timestamp>() {
            tag.set_date_recorded(ts);
            if let Ok(y) = d[..4].parse::<i32>() {
                tag.set_year(y);
            }
        }
    }
    tag.set_genre("Podcast");
    if !info.comment.is_empty() {
        tag.add_frame(Comment { lang: "eng".into(), description: String::new(), text: info.comment.into() });
    }
    if let Some((data, mime)) = &info.artwork {
        tag.add_frame(Picture { mime_type: mime.clone(), picture_type: PictureType::CoverFront, description: "Cover".into(), data: data.clone() });
    }
    let duration_ms = (info.duration * 1000.0) as u32;
    let mut ids = vec![];
    for (i, ch) in chapters.iter().enumerate() {
        let cid = format!("chp{i}");
        let start = (ch.start * 1000.0) as u32;
        let end = chapters.get(i + 1).map(|n| (n.start * 1000.0) as u32).unwrap_or(duration_ms);
        tag.add_frame(ChapFrame {
            element_id: cid.clone(),
            start_time: start,
            end_time: end.max(start),
            start_offset: 0xFFFF_FFFF,
            end_offset: 0xFFFF_FFFF,
            frames: vec![Frame::text("TIT2", ch.title.clone())],
        });
        ids.push(cid);
    }
    if !ids.is_empty() {
        tag.add_frame(TableOfContents {
            element_id: "toc".into(),
            top_level: true,
            ordered: true,
            elements: ids,
            frames: vec![Frame::text("TIT2", "Chapters")],
        });
    }
    tag.write_to_path(path, Version::Id3v23)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ad(s: f64, e: f64) -> Ad {
        Ad { start: s, end: e, label: "x".into(), source: "ai".into() }
    }

    #[test]
    fn remapping() {
        let ads = [ad(10.0, 20.0), ad(50.0, 60.0)];
        assert_eq!(remap(5.0, &ads), 5.0);
        assert_eq!(remap(15.0, &ads), 10.0);
        assert_eq!(remap(70.0, &ads), 50.0);
        assert_eq!(keep_ranges(&ads, 100.0, 0.25), vec![(0.0, 10.0), (20.0, 50.0), (60.0, 100.0)]);
        let ch = build_chapters(&[Chapter { start: 30.0, title: "A".into() }, Chapter { start: 40.0, title: "B".into() }], &ads, 80.0, 60.0, 10.0);
        assert_eq!(ch, vec![Chapter { start: 0.0, title: "A".into() }]);
        let fb = build_chapters(&[], &[], 1300.0, 60.0, 10.0);
        assert_eq!(fb.len(), 3);
    }
}
