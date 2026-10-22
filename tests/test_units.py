"""Fast checks of single pieces - no audio, no servers."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from podcast_cleaner import analyze, config, cues, opml, phone, pubtranscript, schedule
from podcast_cleaner.learned import Learned, key_for, text_between, words
from podcast_cleaner.state import QueueOrder, StartNow, priority_tier
from podcast_cleaner.utils import Segment

READ = ("This episode is brought to you by Northwind Mattress. Northwind makes the comfiest mattress you will ever "
        "sleep on, with a hundred night trial and free shipping to your door. Go to northwind dot com slash show and "
        "use code SHOW for fifty percent off your first order today.")


def episode(variant="a", with_read=True):
    segs, t = [], 0
    for i in range(30):
        segs.append(Segment(t, t + 10, f"talk number {i} about subject {variant} and other lovely things {i}"))
        t += 10
    if with_read:
        w = READ.split()
        if variant == "b":           # small transcription differences
            w[10], w[20] = "comfiest", "nights"
        for k in range(0, len(w), 12):
            segs.append(Segment(t, t + 5, " ".join(w[k:k + 12])))
            t += 5
    for i in range(30, 40):
        segs.append(Segment(t, t + 10, f"talk number {i} about subject {variant} and other lovely things {i}"))
        t += 10
    return segs


# ---------------------------------------------------------------- learned ad reads
def test_learned_read_found_on_another_show(tmp_path):
    L = Learned(tmp_path)
    a = episode("a")
    assert L.learn("Show A", [{"text": text_between(a, 300, 325), "sponsor": "Northwind", "key": "k1"}]) == 1
    assert L.learn("Show A", [{"text": text_between(a, 300, 325), "sponsor": "Northwind"}]) == 0   # known already
    found, nots = L.find(episode("b"), "Show B")
    assert [(round(x["start"]), round(x["end"]), x["label"]) for x in found] == [(300, 325, "Northwind")]
    assert nots == []


def test_learned_nothing_in_episode_without_the_read(tmp_path):
    L = Learned(tmp_path)
    a = episode("a")
    L.learn("Show A", [{"text": text_between(a, 300, 325), "sponsor": "Northwind"}])
    assert L.find(episode("a", with_read=False), "Show A") == ([], [])


def test_learned_not_an_ad_flips(tmp_path):
    L = Learned(tmp_path)
    a = episode("a")
    L.learn("Show A", [{"text": text_between(a, 300, 325), "sponsor": "Northwind", "key": key_for("Show A", "E1", 300)}])
    assert L.set_kind({key_for("Show A", "E1", 300.0)}, "not_ad") == 1
    found, nots = L.find(episode("b"), "Show B")
    assert found == [] and len(nots) == 1


def test_learned_ignores_phrases_repeated_all_over():
    """Generic phrases said in every line mustn't chain the whole episode into one 'ad'."""
    from podcast_cleaner import learned as lmod
    segs = [Segment(t, t + 10, f"segment {t} where the hosts talk about topic {t} and share stories {t}")
            for t in range(0, 300, 10)]
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        L = Learned(Path(d))
        L.learn("S", [{"text": text_between(segs, 100, 140), "sponsor": "X"}])
        found, _ = L.find(segs, "S")
        assert all(a["end"] - a["start"] < 60 for a in found)


def test_learned_share_export_import(tmp_path):
    from podcast_cleaner.learned import export_file, import_file
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    Learned(a).learn("S", [{"text": text_between(episode(), 300, 325), "sponsor": "Northwind", "key": "secret|episode"}])
    assert export_file(a, tmp_path / "share.json") == 1
    assert "secret" not in (tmp_path / "share.json").read_text()      # no episode names in the shared file
    assert import_file(b, tmp_path / "share.json") == (1, 0)
    assert import_file(b, tmp_path / "share.json") == (0, 1)


# ---------------------------------------------------------------- cues: ad edges and joining
def test_complete_tails_adds_lines_naming_the_sponsor():
    segs = episode()
    out = cues.complete_tails([{"start": 300, "end": 310, "label": "Northwind", "source": "ai"}], segs)
    assert out[0]["end"] == 320


def test_merge_breaks_joins_ads_with_little_talk_between():
    segs = [Segment(30, 45, "stinger music"), Segment(60, 200, " ".join(["word"] * 400))]
    ads = [{"start": 0, "end": 30, "label": "A", "source": "ai"}, {"start": 45, "end": 60, "label": "B", "source": "ai"},
           {"start": 200, "end": 230, "label": "C", "source": "ai"}]
    out = cues.merge_breaks(ads, segs)
    assert [(a["start"], a["end"]) for a in out] == [(0, 60), (200, 230)]


def test_merge_breaks_respects_not_an_ad():
    segs = [Segment(30, 45, "stinger")]
    ads = [{"start": 0, "end": 30, "label": "A", "source": "ai"}, {"start": 45, "end": 60, "label": "B", "source": "ai"}]
    assert len(cues.merge_breaks(ads, segs, protect=[(31, 44)])) == 2


def test_extend_pulls_in_lead_in():
    segs = [Segment(0, 10, "We'll be right back after this."), Segment(10, 20, "Acme is great, visit acme.com"),
            Segment(20, 30, "Now back to the show."), Segment(30, 40, "So anyway, the interview.")]
    out = cues.extend([{"start": 10, "end": 20, "label": "Acme", "source": "ai"}], segs)
    assert (out[0]["start"], out[0]["end"]) == (0, 30)


# ---------------------------------------------------------------- publisher's transcript
def test_publisher_transcript_gap_found():
    segs = [Segment(i * 10, i * 10 + 10, f"talk number {i} about subject and other lovely things we discuss {i}")
            for i in range(60)]
    pub = [w for s in segs if not 200 <= s.start < 260 for w in words(s.text)]
    gaps = pubtranscript.gaps(segs, pub)
    assert len(gaps) == 1 and 195 <= gaps[0]["start"] <= 205 and gaps[0]["end"] == 260


def test_publisher_transcript_identical_has_no_gaps():
    segs = [Segment(i * 10, i * 10 + 10, f"talk number {i} about things {i}") for i in range(60)]
    assert pubtranscript.gaps(segs, [w for s in segs for w in words(s.text)]) == []


@pytest.mark.parametrize("body,kind,expect", [
    ("WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\n<v Host>Hello there\n", "text/vtt", "Hello there"),
    ('{"segments":[{"body":"hi"},{"body":"you"}]}', "application/json", "hi you"),
    ("<p>Hi &amp; bye</p>", "text/html", "Hi & bye"),
])
def test_publisher_transcript_formats(body, kind, expect):
    assert " ".join(pubtranscript._to_text(body, kind).split()) == expect


# ---------------------------------------------------------------- queue order and priority
def test_priority_four_levels_order():
    items = [("a", "low"), ("b", "medium"), ("c", "high"), ("d", "highest"), ("e", "medium"), ("f", "high"),
             ("g", None)]
    out = QueueOrder.sort(items, lambda x: x[0], ["e", "b"], tier=lambda x: priority_tier({"priority": x[1]}))
    assert [k for k, _ in out] == ["d", "c", "f", "e", "b", "g", "a"]


def test_start_now_store(tmp_path):
    s = StartNow(tmp_path)
    s.add(["x|1", "y|2"])
    s.remove("x|1")
    assert s.load() == {"y|2"}


# ---------------------------------------------------------------- low-priority hours
@pytest.mark.parametrize("now,open_,hours_left", [("21:59", False, 0.02), ("22:00", True, 0), ("03:00", True, 0),
                                                  ("06:00", False, 16.0), ("12:00", False, 10.0)])
def test_low_priority_window_crosses_midnight(now, open_, hours_left):
    w = {"enabled": True, "start": "22:00", "end": "06:00"}
    d = datetime(2026, 10, 20, *map(int, now.split(":")))
    assert schedule.low_window_open(w, d) is open_
    assert round(schedule.seconds_until_low_window(w, d) / 3600, 2) == hours_left


def test_low_priority_window_off_is_always_open():
    assert schedule.low_window_open({"enabled": False}) is True


# ---------------------------------------------------------------- OPML
def test_opml_round_trip():
    text = opml.build([{"name": "Show & Co", "rss": "https://x/feed"}, {"name": "B", "rss": "https://y/rss"}], "Mine")
    assert opml.parse(text) == [{"name": "Show & Co", "rss": "https://x/feed"}, {"name": "B", "rss": "https://y/rss"}]


def test_opml_nested_and_duplicates():
    text = ('<opml version="1.0"><body><outline text="feeds"><outline text="A" xmlUrl="https://a/f"/>'
            '<outline text="A again" xmlUrl="https://a/f"/><outline text="No url"/></outline></body></opml>')
    assert opml.parse(text) == [{"name": "A", "rss": "https://a/f"}]


# ---------------------------------------------------------------- config
def test_config_defaults_and_old_self_promo_setting(make_cfg):
    cfg = make_cfg("ad_removal: {self_promo_is_ad: true}")
    assert cfg["ad_removal"]["remove"]["self_promo"] is True
    assert cfg["ad_removal"]["remove"]["intro"] is False
    assert cfg["processing"]["low_priority_hours"]["enabled"] is False
    assert cfg["download"]["feed_timeout_seconds"] == 60


def test_config_rejects_bad_priority(make_cfg):
    with pytest.raises(config.ConfigError):
        make_cfg(podcasts="podcasts:\n  - {name: X, rss: 'http://x', priority: urgent}")


def test_removal_choices_per_show(make_cfg):
    from podcast_cleaner.pipeline import removal_choices
    cfg = make_cfg()
    r = removal_choices(cfg, {"remove": {"intro": True, "sponsor": False}})
    assert r["intro"] is True and r["sponsor"] is True and r["outro"] is False


# ---------------------------------------------------------------- phone notifications
@pytest.mark.parametrize("title,msg,kind", [("Cleaned: X", "Ep\n1 ad(s)", "episodes"),
                                            ("Cleaned: X", "Ep\n2 held for review", "held"),
                                            ("Failed: X", "boom", "problems"),
                                            ("Feed keeps failing: X", "", "problems"), ("Something else", "", None)])
def test_phone_notification_kinds(title, msg, kind):
    assert phone.kind_of(title, msg) == kind


def test_phone_needs_a_topic():
    assert phone.wanted({"ntfy_topic": ""}, "episodes") is False
    assert phone.wanted({"ntfy_topic": "t", "episodes": False}, "episodes") is False
    assert phone.wanted({"ntfy_topic": "t", "episodes": True}, "held") is True


# ---------------------------------------------------------------- AI answers
def test_ai_answer_must_be_usable():
    assert analyze._usable({"ads": []})
    assert analyze._usable({"chapters": []})
    assert not analyze._usable({})
    assert not analyze._usable(None)
    assert not analyze._usable({"ads": "none"})


def test_section_percentage():
    from podcast_cleaner.pipeline import _section_pct
    assert _section_pct("Analyzing: 3/11 sections done on PC2") == 27
    assert _section_pct("writing files") is None


def test_config_typed_errors_are_plain(make_cfg):
    with pytest.raises(config.ConfigError) as e:
        make_cfg("ad_removal: {min_confidence: high}\nai: {service: chatgpt}",
                 "podcasts:\n  - {name: X, rss: 'http://x', max_retries: lots}")
    msg = str(e.value)
    assert "ad_removal.min_confidence: must be a number (you wrote 'high')" in msg
    assert "ai.service: must be ollama" in msg
    assert "podcasts #1 (X).max_retries: must be a number" in msg


def test_config_typed_keeps_unknown_and_yaml_off(make_cfg):
    cfg = make_cfg("second_download: {mode: off}\nsomething_new: 1",
                   "podcasts:\n  - {name: X, rss: 'http://x', custom: 2}")
    assert cfg["second_download"]["mode"] is False and cfg["something_new"] == 1
    assert cfg["podcasts"][0]["custom"] == 2
