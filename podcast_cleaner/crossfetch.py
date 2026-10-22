"""Second download: find dynamically inserted ads by downloading an episode twice.

Ad servers splice ads into the audio when it is downloaded, so two downloads - made to look like
different podcast apps - often carry different ads. The audio of both is compared; parts of the
first download that can't be found anywhere in the second are inserted ads.
Idea from MinusPod (github.com/ttlequals0/minuspod, MIT licence).
"""
from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from . import feeds
from .utils import NO_WINDOW, Cancelled

log = logging.getLogger(__name__)

# hosts that insert ads at download time (also matched inside tracking-prefix chains)
DAI_HOSTS = (
    "megaphone.fm", "art19.com", "omny.fm", "omnycontent.com", "simplecast", "pdst.fm", "podtrac.com",
    "chtbl.com", "chrt.fm", "claritaspod.com", "arttrk.com", "mgln.ai", "acast.com", "spreaker.com",
    "audioboom.com", "iheart.com", "ihrt.com", "adswizz.com", "flightcast", "pscrb.fm", "byspotify.com",
    "anchor.fm", "spotifycdn", "podscribe", "veritone", "swap.fm", "libsyn.com", "podbean.com",
    "wondery", "stitcher", "barstool", "audioboom", "redcircle.com", "captivate.fm", "buzzsprout.com",
    "transistor.fm", "sounder.fm", "pinecast", "dts.podtrac", "tracking.feedpress", "afp.megaphone",
)
SECOND_AGENT = "AppleCoreMedia/1.0.0.21G93 (iPhone; U; CPU OS 17_6 like Mac OS X; en_us)"

RATE = 8000
HOP = 400            # 50 ms frames
FRAME = 800
WIN = 60             # 3 s comparison windows
STEP = 30
MATCH = 0.85


def norm_mode(mode) -> str:
    """YAML reads on/off as true/false."""
    if mode is True:
        return "on"
    if mode is False:
        return "off"
    m = str(mode or "auto").strip().lower()
    return {"true": "on", "yes": "on", "always": "on", "false": "off", "no": "off", "never": "off"}.get(m, m)


def wanted(mode, url: str) -> bool:
    mode = norm_mode(mode)
    if mode in ("off", "false", "never", "no"):
        return False
    if mode in ("on", "true", "always", "yes"):
        return True
    hosts = [urlsplit(u).netloc.lower() for u in [url] + feeds.unwrap_tracking(url)]
    joined = url.lower() + " " + " ".join(hosts)
    return any(h in joined for h in DAI_HOSTS)


def refetch(url: str, dest: Path, session, should_continue, skip_trackers: bool) -> Path:
    fresh = feeds.make_session()              # no cookies from the first download
    fresh.headers["User-Agent"] = SECOND_AGENT
    fresh.proxies.update(getattr(session, "proxies", {}) or {})
    try:
        feeds.download(url, dest, fresh, should_continue, skip_trackers=skip_trackers)
    finally:
        fresh.close()
    return dest


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _features(ffmpeg: str, path: Path, should_continue):
    """8 log-energy bands per 50 ms frame."""
    import numpy as np
    proc = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(RATE), "-f", "s16le",
                           "-"], capture_output=True, creationflags=NO_WINDOW, timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg couldn't decode {path.name}: {proc.stderr.decode(errors='replace')[-300:]}")
    x = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    n = max(0, (len(x) - FRAME) // HOP + 1)
    edges = np.geomspace(100, 3800, 9)
    freqs = np.fft.rfftfreq(FRAME, 1 / RATE)
    bands = [(freqs >= edges[k]) & (freqs < edges[k + 1]) for k in range(8)]
    win = np.hanning(FRAME).astype(np.float32)
    out = np.empty((n, 8), dtype=np.float32)
    for c in range(0, n, 8000):
        if not should_continue():
            raise Cancelled()
        idx = np.arange(c, min(n, c + 8000))[:, None] * HOP + np.arange(FRAME)[None, :]
        spec = np.abs(np.fft.rfft(x[idx] * win, axis=1)) ** 2
        out[c:c + len(idx)] = np.stack([spec[:, b].sum(axis=1) for b in bands], axis=1)
    return np.log10(out + 1e-7)


def _corr(a, b) -> float:
    import numpy as np
    # centre each band on its own: otherwise every pair of windows looks alike (bass is always louder)
    a, b = (a - a.mean(axis=0)).ravel(), (b - b.mean(axis=0)).ravel()
    d = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / d) if d > 1e-9 else 0.0


def _search(ea, eb, fa_win, fb):
    """Find fa_win (WIN x 8) anywhere in fb: candidates from the loudness curve, checked on all bands."""
    import numpy as np
    n = len(ea)
    if len(eb) < n:
        return 0.0, None
    q = (ea - ea.mean())
    qn = float(np.sqrt((q * q).sum()))
    if qn < 1e-6:
        return 0.0, None
    size = 1 << int(np.ceil(np.log2(len(eb) + n)))
    num = np.fft.irfft(np.fft.rfft(eb, size) * np.conj(np.fft.rfft(q, size)), size)[:len(eb) - n + 1]
    cs, cs2 = np.cumsum(np.r_[0, eb]), np.cumsum(np.r_[0, eb * eb])
    s, s2 = cs[n:] - cs[:-n], cs2[n:] - cs2[:-n]
    var = np.maximum(s2 - s * s / n, 1e-9)
    score = num / (np.sqrt(var) * qn)
    best, where = 0.0, None
    for p in np.argsort(score)[::-1][:6]:
        c = _corr(fa_win, fb[p:p + n])
        if c > best:
            best, where = c, int(p)
    return best, where


def compare(ffmpeg: str, first: Path, second: Path, should_continue=lambda: True, ceiling: float = 0.8,
            min_seconds: float = 10.0) -> list[dict]:
    """Parts of `first` not found in `second`: [{"start", "end", "similarity"}] in seconds."""
    import numpy as np
    if _sha1(first) == _sha1(second):
        log.info("Second download: identical to the first - this episode's ads aren't changed per download")
        return []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as ex:         # decode both at once
        ja, jb = (ex.submit(_features, ffmpeg, p, should_continue) for p in (first, second))
        fa, fb = ja.result(), jb.result()
    if len(fa) < WIN or len(fb) < WIN:
        return []
    ea, eb = fa.mean(axis=1), fb.mean(axis=1)
    loud = float(np.percentile(ea, 50))
    verdicts = []           # per window: "same", "diff", "unsure", "quiet"
    worst = []
    offs = []               # where each window was found in the second download (frames), if it was
    offset = 0
    for i, a0 in enumerate(range(0, len(fa) - WIN + 1, STEP)):
        if i % 200 == 0 and not should_continue():
            raise Cancelled()
        wa = fa[a0:a0 + WIN]
        if ea[a0:a0 + WIN].std() < 0.05 or ea[a0:a0 + WIN].mean() < loud - 2.0:
            verdicts.append("quiet")
            worst.append(1.0)
            offs.append(None)
            continue
        best = 0.0
        for d in (0, -1, 1, -2, 2):             # where it should be if nothing changed since the last match
            b0 = a0 + offset + d
            if 0 <= b0 <= len(fb) - WIN:
                best = max(best, _corr(wa, fb[b0:b0 + WIN]))
                if best >= MATCH:
                    offset += d
                    break
        if best < MATCH:
            c, where = _search(ea[a0:a0 + WIN], eb, wa, fb)
            if c > best:
                best = c
                if c >= MATCH and where is not None:
                    offset = where - a0
        verdicts.append("same" if best >= MATCH else "diff" if best <= ceiling else "unsure")
        worst.append(best)
        offs.append(offset if best >= MATCH else None)

    floor = loud - 3.0

    def frame_edge(off, lo, hi, forward):
        """First frame (walking forward from lo, or backward from hi) where the two stop matching."""
        bad, rng = 0, (range(lo, hi) if forward else range(hi - 1, lo - 1, -1))
        for f in rng:
            g = f + off
            if not (0 <= f < len(fa)) or not (0 <= g < len(fb)):
                return None
            d = float(np.abs(np.maximum(fa[f], floor) - np.maximum(fb[g], floor)).mean())
            bad = bad + 1 if d > 0.4 else 0
            if bad >= 4:
                return f - 3 if forward else f + 3
        return None

    def near_same(k, step):
        while 0 <= k < len(offs):
            if verdicts[k] == "same":
                return offs[k]
            k += step
        return None

    sec = HOP / RATE
    edge = STEP * sec / 2                     # a window overlapping an ad's edge by a little still matches
    regions: list[dict] = []
    run: list[int] = []
    for i, v in enumerate(verdicts + ["same"]):
        if v != "same":
            run.append(i)
            continue
        loud_ones = [k for k in run if verdicts[k] != "quiet"]
        diffs = [k for k in loud_ones if verdicts[k] == "diff"]
        if len(diffs) >= 2 and len(diffs) >= 0.4 * len(loud_ones):
            start = diffs[0] * STEP * sec + edge          # unclear windows at the edges only half overlap
            end = (diffs[-1] * STEP + WIN) * sec - edge
            # exact edges, frame by frame, against the matching audio on either side
            off = near_same(run[0] - 1, -1)
            if off is not None:
                f = frame_edge(off, max(0, diffs[0] * STEP - STEP), diffs[0] * STEP + WIN, True)
                if f is not None:
                    start = f * sec
            off = near_same(run[-1] + 1, 1)
            if off is not None:
                f = frame_edge(off, diffs[-1] * STEP, diffs[-1] * STEP + WIN + STEP, False)
                if f is not None:
                    end = (f + 1) * sec + (FRAME - HOP) / RATE
            if end - start >= min_seconds:
                regions.append({"start": round(start, 2), "end": round(end, 2),
                                "similarity": round(float(np.mean([worst[k] for k in loud_ones])), 2)})
        run = []
    log.info("Second download: %d part(s) differ from the first%s", len(regions),
             (" - " + ", ".join(f"{r['start']:.0f}-{r['end']:.0f}s" for r in regions[:8])) if regions else "")
    return regions
