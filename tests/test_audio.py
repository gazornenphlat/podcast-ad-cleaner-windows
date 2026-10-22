"""Audio: comparing two downloads, cutting with tones, loudness, artwork - with real ffmpeg on synthetic audio."""
from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from conftest import FFMPEG, needs_ffmpeg
from podcast_cleaner import crossfetch, media

pytestmark = needs_ffmpeg


def _near(regions, expected, tol=1.0):
    assert len(regions) == len(expected), regions
    for r, (a, b) in zip(regions, expected):
        assert abs(r["start"] - a) <= tol and abs(r["end"] - b) <= tol, (r, a, b)


def test_second_download_finds_swapped_ad(audio):
    _near(crossfetch.compare("ffmpeg", audio["a"], audio["b"]), [(100, 130)])


def test_second_download_finds_ads_missing_from_other_copy(audio):
    _near(crossfetch.compare("ffmpeg", audio["a"], audio["c"]), [(100, 130), (230, 250)])


def test_second_download_nothing_extra_in_first(audio):
    assert crossfetch.compare("ffmpeg", audio["c"], audio["a"]) == []


@pytest.mark.parametrize("args", [["-b:a", "24k", "-ar", "16000"], ["-c:a", "aac", "-b:a", "48k"]])
def test_second_download_ignores_reencoding(audio, tmp_path, args):
    out = tmp_path / ("re.m4a" if "aac" in args else "re.mp3")
    subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(audio["a"]), *args, str(out)], check=True)
    assert crossfetch.compare("ffmpeg", audio["a"], out) == []


def test_auto_mode_only_for_ad_inserting_hosts():
    assert crossfetch.wanted("auto", "https://www.podtrac.com/pts/redirect.mp3/traffic.megaphone.fm/ABC.mp3")
    assert not crossfetch.wanted("auto", "https://example.org/x.mp3")
    assert crossfetch.wanted(True, "https://example.org/x.mp3")      # YAML "on"
    assert not crossfetch.wanted(False, "https://megaphone.fm/x.mp3")  # YAML "off"


def test_cut_with_tones_keeps_timing_exact(audio, tmp_path):
    src = audio["a"]
    dur = media.probe_duration("ffprobe", src)
    ads = [{"start": 0, "end": 8}, {"start": 100, "end": 130}, {"start": 230, "end": 250},
           {"start": dur - 10, "end": dur}]
    plan = media.tone_plan(ads, dur)
    assert [k for _, k in plan] == ["start", "middle", "middle", "end"]
    out = tmp_path / "cut.mp3"
    media.cut_ads("ffmpeg", src, out, ads, "128k", dur, 40, normalize={"enabled": True}, tones=plan)
    heard = media.timing_ads(ads, plan)
    expect = dur - sum(a["end"] - a["start"] for a in heard)
    assert abs(media.probe_duration("ffprobe", out) - expect) < 0.1
    # the audio after the first middle cut is where the chapters/transcript say it is
    fa = crossfetch._features("ffmpeg", src, lambda: True)
    fb = crossfetch._features("ffmpeg", out, lambda: True)
    a0 = int(131 / 0.05)
    corr, where = crossfetch._search(fa[a0:a0 + 60].mean(1), fb.mean(1), fa[a0:a0 + 60], fb)
    assert corr > 0.9 and abs(where * 0.05 - media.remap(131, heard)) < 0.1


def test_cut_without_tones(audio, tmp_path):
    dur = media.probe_duration("ffprobe", audio["a"])
    out = tmp_path / "cut.mp3"
    media.cut_ads("ffmpeg", audio["a"], out, [{"start": 100, "end": 130}], "128k", dur, 0)
    assert abs(media.probe_duration("ffprobe", out) - (dur - 30)) < 0.1


def test_volume_is_evened_out(audio, tmp_path):
    dur = media.probe_duration("ffprobe", audio["short"])
    out = tmp_path / "loud.mp3"
    m = media.cut_ads("ffmpeg", audio["short"], out, [], "128k", dur, 0,
                      normalize={"enabled": True, "loudness": -16, "true_peak": -1.5})
    assert m is not None
    after = media._measure_loudness("ffmpeg", out, "[0:a:0]anull[cut]", {"loudness": -16})
    assert abs(after["input_i"] - -16) < 1.0


def test_artwork_badge():
    from PIL import Image
    from podcast_cleaner import artwork
    buf = io.BytesIO()
    Image.new("RGB", (1400, 1400), (230, 120, 40)).save(buf, "PNG")
    data, mime = artwork.badge(buf.getvalue())
    img = Image.open(io.BytesIO(data))
    assert mime == "image/jpeg" and img.size == (1400, 1400)
    corner = img.crop((900, 1150, 1400, 1400)).getdata()
    green = sum(1 for r, g, b in corner if g > r + 30 and g > b + 30)
    assert green > 5000                                # the green badge is in the bottom-right corner
    top_left = img.crop((0, 0, 400, 400)).getdata()
    assert sum(1 for r, g, b in top_left if g > r + 30) == 0      # the rest of the picture is untouched


def test_feed_reader_helper_process():
    """Reading a feed happens in a helper process that can be stopped if it gets stuck."""
    from podcast_cleaner import feedproc
    feedproc._disabled = False
    try:
        small = (b"<rss version='2.0'><channel><title>S</title><item><title>A</title>"
                 b"<enclosure url='http://x/a.mp3'/></item></channel></rss>")
        assert len(feedproc.parse(small, "u", 60).episodes) == 1
        big = ("<rss version='2.0'><channel><title>B</title>" + "".join(
            f"<item><title>E{i}</title><guid>g{i}</guid><enclosure url='http://x/{i}.mp3'/>"
            f"<description>{'<p>words</p>' * 40}</description></item>" for i in range(1500)) + "</channel></rss>").encode()
        with pytest.raises(TimeoutError):
            feedproc.parse(big, "u", 0.3)
        assert feedproc._helper is None                       # the stuck helper was stopped
        assert feedproc.parse(small, "u", 60).title == "S"     # and a new one took over
        with pytest.raises(ValueError):
            feedproc.parse(b"not a feed", "u", 60)
    finally:
        feedproc.stop()
