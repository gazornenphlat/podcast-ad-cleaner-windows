"""ffmpeg cutting, timeline remapping, chapter JSON and ID3 tagging."""
from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

from mutagen.id3 import APIC, CHAP, COMM, CTOC, ID3, TALB, TDRC, TIT2, TPE1, TCON, CTOCFlags
from mutagen.mp3 import MP3

from .utils import Segment, run

log = logging.getLogger(__name__)


# ---------- ffmpeg ----------

def probe_duration(ffprobe: str, path: Path) -> float:
    proc = run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path)], text=True)
    try:
        return float(proc.stdout.strip())
    except ValueError as e:
        raise RuntimeError(f"ffprobe could not read {path.name}: {proc.stderr[-300:]}") from e


def check_tool(path: str) -> bool:
    return bool(shutil.which(path) or Path(path).is_file())


def resolve_tool(name: str) -> str:
    """Use the configured tool if it exists, else an .exe shipped next to the app (packaged build)."""
    if check_tool(name):
        return name
    from .config import APP_DIR
    for candidate in (APP_DIR / f"{Path(name).stem}.exe", APP_DIR / "ffmpeg" / f"{Path(name).stem}.exe"):
        if candidate.is_file():
            return str(candidate)
    return name


def keep_ranges(ads: list[dict], duration: float, min_len: float = 0.25) -> list[tuple[float, float]]:
    """Complement of the ad ranges within [0, duration]."""
    out, pos = [], 0.0
    for a in ads:
        if a["start"] - pos >= min_len:
            out.append((pos, a["start"]))
        pos = max(pos, a["end"])
    if duration - pos >= min_len:
        out.append((pos, duration))
    return out


# ---------- cut tones: a soft "wooden" sound where something was removed ----------
# Idea from Podcast Ad Remover (github.com/jdcb4/podcast-ad-remover, MIT licence); the sounds here are our own.
TONES = {"start": ((659.3, 0.0), (880.0, 0.2)),      # two rising notes: something was cut at the very start
         "end": ((880.0, 0.0), (659.3, 0.2)),        # two falling notes: ...at the very end
         "middle": ((392.0, 0.0),)}                  # one low note: ...in the middle
TONE_SECONDS = {"start": 0.47, "end": 0.47, "middle": 0.25}


def write_tone(path: Path, kind: str, rate: int = 44100) -> Path:
    """Short damped harmonic taps, like a wood block. Written as a mono 16-bit WAV."""
    import math
    import struct
    import wave
    n = int(TONE_SECONDS[kind] * rate)
    buf = [0.0] * n
    for freq, at in TONES[kind]:
        first = int(at * rate)
        for i in range(first, n):
            t = (i - first) / rate
            env = math.exp(-t / 0.045) * min(1.0, t / 0.003)             # 3 ms attack, quick decay
            buf[i] += env * (math.sin(2 * math.pi * freq * t) + 0.35 * math.sin(2 * math.pi * freq * 2.76 * t)
                             * math.exp(-t / 0.02) + 0.15 * math.sin(2 * math.pi * freq * 5.4 * t)
                             * math.exp(-t / 0.01))
    peak = max(1e-9, max(abs(x) for x in buf))
    scale = 0.2 / peak                                                   # about -14 dBFS
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(x * scale * 32767)) for x in buf))
    return path


def tone_plan(ads: list[dict], duration: float) -> list[tuple[int, str]]:
    """Which cut gets which tone: [(index of the ad, kind)]. A tone takes the place of the first part of
    its cut, so the episode's timing (chapters, transcript) stays exact."""
    plan = []
    for i, a in enumerate(ads):
        kind = "start" if a["start"] < 0.25 else "end" if a["end"] > duration - 0.25 else "middle"
        if a["end"] - a["start"] > TONE_SECONDS[kind] + 0.3:
            plan.append((i, kind))
    return plan


def timing_ads(ads: list[dict], plan: list[tuple[int, str]]) -> list[dict]:
    """The removed time as the listener hears it: each toned cut is shorter by its tone."""
    out = [dict(a) for a in ads]
    for i, kind in plan:
        if kind == "end":
            out[i]["end"] -= TONE_SECONDS[kind]
        else:
            out[i]["start"] += TONE_SECONDS[kind]
    return out


def _loudnorm_args(norm: dict) -> str:
    return (f"I={float(norm.get('loudness', -16)):.1f}:TP={float(norm.get('true_peak', -1.5)):.1f}"
            f":LRA={float(norm.get('lra', 11)):.1f}")


def _measure_loudness(ffmpeg: str, src: Path, graph: str, norm: dict, extra_inputs: list | None = None
                      ) -> dict | None:
    """Pass 1: run the same cuts through the EBU R128 meter (no encoding) and read loudness and true peak."""
    more = [x for p in (extra_inputs or []) for x in ("-i", str(p))]
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-i", str(src), *more, "-filter_complex",
           f"{graph};[cut]ebur128=peak=true:framelog=quiet[o]", "-map", "[o]", "-f", "null", "-"]
    proc = run(cmd)
    text = proc.stderr.decode(errors="replace")
    if proc.returncode != 0:
        log.warning("Loudness measurement failed - using one-pass normalization: %s", text[-300:])
        return None
    summary = text[text.rfind("Summary:"):] if "Summary:" in text else ""
    i = re.search(r"\bI:\s*(-?[\d.]+|-inf)\s*LUFS", summary)
    pk = re.search(r"True peak:\s*\n\s*Peak:\s*(-?[\d.]+|-inf)\s*dBFS", summary)
    if not i or i.group(1) == "-inf":
        return None
    loud = float(i.group(1))
    peak = float(pk.group(1)) if pk and pk.group(1) != "-inf" else loud + 20
    if loud < -70:                 # silence / no audio: nothing sensible to do
        return None
    return {"input_i": loud, "input_tp": peak}


def cut_ads(ffmpeg: str, src: Path, dst: Path, ads: list[dict], bitrate: str,
            duration: float = 0.0, crossfade_ms: float = 0, normalize: dict | None = None,
            tones: list[tuple[int, str]] | None = None) -> dict | None:
    """Write dst as MP3 with ad ranges removed (one re-encode, sample-accurate cuts).

    With crossfade_ms > 0 the kept pieces are joined with a short crossfade so there is
    no click or abrupt jump where an ad was removed.

    With normalize (e.g. {"enabled": True, "loudness": -16, "true_peak": -1.5, "lra": 11}) the result is
    brought to a standard loudness: the cut audio is measured first, then one fixed (linear) gain is applied
    while encoding - the show's own dynamics are kept, nothing is pumped. Returns the measurement, if any.
    """
    fade = max(0.0, float(crossfade_ms)) / 1000.0
    pieces = keep_ranges(ads, duration, min_len=max(0.25, fade * 3)) if ads and duration else []
    norm = normalize if normalize and normalize.get("enabled", True) else None

    # the cut, as a filter graph ending in [cut]
    extra_inputs: list[Path] = []
    if ads and tones and duration:
        # pieces of the show and tones, joined in order; each piece fades in/out over 15 ms (no clicks)
        files = {k: write_tone(dst.parent / f"tone-{k}.wav", k) for k in {k for _, k in tones}}
        toned = dict(tones)
        fmt = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
        parts, labels = [], []
        events = sorted([(a, "piece", (a, b)) for a, b in pieces] +
                        [(ads[i]["start"] if k != "end" else ads[i]["end"] - TONE_SECONDS[k], "tone", k)
                         for i, k in toned.items()])
        for n, (_, what, x) in enumerate(events):
            if what == "piece":
                a, b = x
                ln = b - a
                f = min(0.015, ln / 4)
                parts.append(f"[0:a:0]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,{fmt},"
                             f"afade=t=in:d={f:.3f},afade=t=out:st={max(0.0, ln - f):.3f}:d={f:.3f}[q{n}]")
            else:
                extra_inputs.append(files[x])
                parts.append(f"[{len(extra_inputs)}:a:0]{fmt}[q{n}]")
            labels.append(f"[q{n}]")
        graph = ";".join(parts) + f";{''.join(labels)}concat=n={len(labels)}:v=0:a=1[cut]"
    elif ads and fade > 0 and len(pieces) > 1:
        graph = [f"[0:a:0]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[p{i}]"
                 for i, (a, b) in enumerate(pieces)]
        prev = "p0"
        for i in range(1, len(pieces)):
            out = "cut" if i == len(pieces) - 1 else f"x{i}"
            graph.append(f"[{prev}][p{i}]acrossfade=d={fade:.3f}:c1=tri:c2=tri[{out}]")
            prev = out
        graph = ";".join(graph)
    elif ads:
        expr = "+".join(f"between(t,{a['start']:.3f},{a['end']:.3f})" for a in ads)
        graph = f"[0:a:0]aselect='not({expr})',asetpts=N/SR/TB[cut]"
    elif src.suffix.lower() == ".mp3" and not norm:
        shutil.copyfile(src, dst)        # nothing to change: keep the original bytes
        return None
    else:
        graph = "[0:a:0]anull[cut]"

    measured = None
    if norm:
        measured = _measure_loudness(ffmpeg, src, graph, norm, extra_inputs)
        target, ceiling = float(norm.get("loudness", -16)), float(norm.get("true_peak", -1.5))
        if measured:
            # One fixed gain to reach the target (keeps the show's own dynamics). Only if that would push
            # peaks past the ceiling, a fast limiter catches just those peaks.
            gain = target - measured["input_i"]
            chain = f"volume={gain:.2f}dB"
            if measured["input_tp"] + gain > ceiling:
                limit = 10 ** ((ceiling - 0.3) / 20)      # a little margin: the limiter works on samples
                chain += f",alimiter=limit={limit:.4f}:attack=2:release=50:level=0"
            measured["gain"] = round(gain, 1)
        else:
            chain = f"loudnorm={_loudnorm_args(norm)},aresample=44100"   # fallback: one adaptive pass
        graph += f";[cut]{chain}[o]"
        out_label = "[o]"
    else:
        out_label = "[cut]"

    more = [x for p in extra_inputs for x in ("-i", str(p))]
    cmd = [ffmpeg, "-nostdin", "-y", "-i", str(src), *more, "-filter_complex", graph, "-map", out_label,
           "-vn", "-c:a", "libmp3lame", "-b:a", bitrate, "-map_metadata", "-1", "-id3v2_version", "3", str(dst)]
    proc = run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode(errors='replace')[-800:]}")
    return measured


def read_embedded_chapters(path: Path) -> list[dict]:
    """Chapters the publisher put inside the MP3 itself (ID3 CHAP frames). Idea from Podly (MIT licence)."""
    if path.suffix.lower() != ".mp3":
        return []
    try:
        tags = ID3(str(path))
    except Exception:
        return []
    out = []
    for frame in tags.getall("CHAP"):
        title = ""
        for sub in (frame.sub_frames or {}).values():
            if getattr(sub, "FrameID", "") == "TIT2":
                title = str(sub.text[0]) if sub.text else ""
        out.append({"start": frame.start_time / 1000.0, "title": title.strip()[:100]})
    out.sort(key=lambda c: c["start"])
    return [c for c in out if c["title"]] if len(out) >= 2 else []


# ---------- timeline remapping (original -> cleaned) ----------

def remap(t: float, ads: list[dict]) -> float:
    removed = 0.0
    for a in ads:
        if t <= a["start"]:
            break
        removed += min(t, a["end"]) - a["start"]
    return max(0.0, t - removed)


def in_ad(t: float, ads: list[dict]) -> bool:
    return any(a["start"] <= t < a["end"] for a in ads)


def remap_segments(segments: list[Segment], ads: list[dict]) -> list[Segment]:
    out = []
    for s in segments:
        if in_ad((s.start + s.end) / 2, ads):
            continue
        out.append(Segment(remap(s.start, ads), remap(s.end, ads), s.text))
    return out


def build_chapters(chapters: list[dict], ads: list[dict], clean_duration: float,
                   min_gap: float, fallback_minutes: float) -> list[dict]:
    """Remap chapters to the cleaned timeline, de-duplicate, and guarantee one at 0:00."""
    mapped = sorted(
        ({"start": remap(c["start"], ads), "title": c["title"]} for c in chapters if c.get("title")),
        key=lambda c: c["start"],
    )
    result: list[dict] = []
    for c in mapped:
        if c["start"] >= clean_duration - 5:
            continue
        if result and c["start"] - result[-1]["start"] < min_gap:
            continue
        result.append(c)

    if not result:
        step = max(60.0, fallback_minutes * 60)
        t, n = 0.0, 1
        while t < clean_duration - 30:
            result.append({"start": t, "title": f"Part {n}"})
            t, n = t + step, n + 1
    elif result[0]["start"] > 0:
        if result[0]["start"] < min_gap:
            result[0]["start"] = 0.0
        else:
            result.insert(0, {"start": 0.0, "title": "Introduction"})
    return [{"start": round(c["start"], 2), "title": c["title"]} for c in result]


# ---------- outputs ----------

def write_chapters_json(path: Path, chapters: list[dict], episode_title: str,
                        podcast_name: str, author: str) -> None:
    data = {
        "version": "1.2.0",
        "title": episode_title,
        "podcastName": podcast_name,
        "author": author,
        "chapters": [{"startTime": c["start"], "title": c["title"]} for c in chapters],
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def tag_mp3(path: Path, chapters: list[dict], *, title: str, podcast: str, author: str,
            date: str | None, comment: str, artwork: bytes | None, artwork_mime: str) -> None:
    audio = MP3(path, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    for frame in ("CHAP", "CTOC", "TIT2", "TALB", "TPE1", "TDRC", "TCON", "COMM", "APIC"):
        tags.delall(frame)

    tags.add(TIT2(encoding=3, text=title))
    tags.add(TALB(encoding=3, text=podcast))
    if author:
        tags.add(TPE1(encoding=3, text=author))
    if date:
        tags.add(TDRC(encoding=3, text=date))
    tags.add(TCON(encoding=3, text="Podcast"))
    if comment:
        tags.add(COMM(encoding=3, lang="eng", desc="", text=comment))
    if artwork:
        tags.add(APIC(encoding=3, mime=artwork_mime, type=3, desc="Cover", data=artwork))

    duration_ms = int(audio.info.length * 1000)
    ids = []
    for i, ch in enumerate(chapters):
        cid = f"chp{i}"
        ids.append(cid)
        start = int(ch["start"] * 1000)
        end = int(chapters[i + 1]["start"] * 1000) if i + 1 < len(chapters) else duration_ms
        tags.add(CHAP(element_id=cid, start_time=start, end_time=max(start, end),
                      start_offset=0xFFFFFFFF, end_offset=0xFFFFFFFF,
                      sub_frames=[TIT2(encoding=3, text=ch["title"])]))
    if ids:
        tags.add(CTOC(element_id="toc", flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
                      child_element_ids=ids, sub_frames=[TIT2(encoding=3, text="Chapters")]))
    audio.save(v2_version=3)
