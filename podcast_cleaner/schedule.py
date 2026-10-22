"""Learn when each podcast usually publishes, and only check its feed around those times.

Learned from the publish dates of the episodes in the feed itself (so it works from the first check,
no waiting to collect history) and saved in the show's .podcast-state.json under "schedule".

A show is checked when ANY of these is true:
  - smart checking is off (globally or for the show), or there's too little / too irregular history
  - it's one of the show's usual release days, inside its usual release window (with margins)
  - an expected episode hasn't appeared yet (late) - keep checking for a while after the window
  - it hasn't been checked for max_hours_between_checks (0 = no limit)
  - you asked for something from it (download, skip, re-analyze), or clicked Check now, or it's new

Nothing is ever missed by skipping a check: the feed lists every episode, so the next check finds
anything published in between. Skipping only delays it.
"""
from __future__ import annotations

from datetime import datetime, timedelta

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY = 24 * 60


def _local(dt: datetime) -> datetime:
    return dt.astimezone().replace(tzinfo=None)


def learn(published: list[datetime], scfg: dict, now: datetime | None = None) -> dict:
    """Work out the usual release days and time window from publish dates (any timezone)."""
    now = now or datetime.now()
    horizon = now - timedelta(days=int(scfg.get("history_days", 120)))
    times = sorted({_local(p) for p in published if p}, reverse=True)
    times = [t for t in times if horizon <= t <= now + timedelta(days=1)][: int(scfg.get("max_samples", 40))]
    min_n = int(scfg.get("min_episodes", 6))
    out: dict = {"samples": len(times), "updated": now.isoformat(timespec="minutes"),
                 "days": None, "window": None, "latest": times[0].isoformat(timespec="minutes") if times else None}
    if len(times) < min_n:
        out["summary"] = f"any time (only {len(times)} recent episode(s) to learn from)"
        return out

    # ---- days: a weekday with no release in >= 4 weeks of history is skipped
    span_days = (times[0] - times[-1]).days
    counts = [0] * 7
    for t in times:
        counts[t.weekday()] += 1
    if span_days >= 28:
        days = [d for d in range(7) if counts[d] > 0]
        if len(days) < 7:
            out["days"] = days

    # ---- time window: the shortest stretch of the day holding most releases
    share = float(scfg.get("window_share", 0.8))
    mins = sorted(t.hour * 60 + t.minute for t in times)
    need = max(1, int(round(share * len(mins) + 0.4999)))
    best = None
    n = len(mins)
    for i in range(n):            # circular: a window may cross midnight
        j = i + need - 1
        end = mins[j % n] + (DAY if j >= n else 0)
        width = end - mins[i]
        if best is None or width < best[1]:
            best = (mins[i], width)
    max_width = float(scfg.get("max_window_hours", 10)) * 60
    if best and best[1] <= max_width:
        out["window"] = [best[0], best[1]]          # start minute of day, length in minutes
    out["summary"] = describe(out, scfg)
    return out


def _hm(m: int) -> str:
    m %= DAY
    h, mm = divmod(m, 60)
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{mm:02d} {suffix}" if mm else f"{h12} {suffix}"


def describe(sched: dict, scfg: dict) -> str:
    days = sched.get("days")
    if days is None:
        d = "every day"
    elif days == [0, 1, 2, 3, 4]:
        d = "weekdays"
    elif days == [5, 6]:
        d = "weekends"
    else:
        d = ", ".join(DAY_NAMES[x] for x in days)
    w = sched.get("window")
    if not w:
        return f"{d}, any time" if days is not None else "any time (no regular pattern)"
    before, after = int(scfg.get("before_minutes", 60)), int(scfg.get("after_minutes", 180))
    return f"{d}, {_hm(w[0] - before)} - {_hm(w[0] + w[1] + after)}"


def _windows(sched: dict, scfg: dict, around: datetime):
    """Check windows (start, end) on days near `around`, newest first."""
    days = sched.get("days")
    w = sched.get("window")
    before, after = int(scfg.get("before_minutes", 60)), int(scfg.get("after_minutes", 180))
    base = around.replace(hour=0, minute=0, second=0, microsecond=0)
    for back in range(-1, 9):
        day = base - timedelta(days=back)
        if days is not None and day.weekday() not in days:
            continue
        if w:
            start = day + timedelta(minutes=w[0] - before)
            yield start, start + timedelta(minutes=w[1] + before + after)
        else:
            yield day, day + timedelta(days=1)


def is_due(sched: dict | None, scfg: dict, last_checked: str | None, now: datetime | None = None
           ) -> tuple[bool, str]:
    """Should the show be checked now? Returns (due, reason)."""
    now = now or datetime.now()
    if not sched or (sched.get("days") is None and not sched.get("window")):
        return True, "no pattern"
    try:
        last = datetime.fromisoformat(last_checked) if last_checked else None
    except ValueError:
        last = None
    limit = float(scfg.get("max_hours_between_checks", 0) or 0)
    if last is None or (limit and now - last >= timedelta(hours=limit)):
        return True, "safety check"
    windows = list(_windows(sched, scfg, now))
    if any(a <= now <= b for a, b in windows):
        return True, "release window"
    # Late: the latest expected release window has passed with no new episode - keep looking for a while.
    late_hours = float(scfg.get("late_hours", 24))
    past = [(a, b) for a, b in windows if b < now]
    if past and late_hours:
        a, b = max(past)
        try:
            latest = datetime.fromisoformat(sched["latest"]) if sched.get("latest") else None
        except ValueError:
            latest = None
        if (latest is None or latest < a) and now - b <= timedelta(hours=late_hours):
            return True, "episode late"
    return False, "outside usual release times"


# ---------- hours when low-priority shows may be processed ----------
def _hm(text: str) -> tuple[int, int]:
    h, _, m = str(text or "0:00").strip().partition(":")
    return max(0, min(23, int(h or 0))), max(0, min(59, int(m or 0)))


def low_window_open(win: dict | None, now: datetime | None = None) -> bool:
    """Is it inside the low-priority hours? (Always open when the window is off.) Windows may cross
    midnight, e.g. 22:00-06:00."""
    if not win or not win.get("enabled"):
        return True
    now = now or datetime.now()
    a, b = _hm(win.get("start", "01:00")), _hm(win.get("end", "06:00"))
    t = (now.hour, now.minute)
    return (a <= t < b) if a <= b else (t >= a or t < b)


def seconds_until_low_window(win: dict | None, now: datetime | None = None) -> float:
    if low_window_open(win, now):
        return 0.0
    now = now or datetime.now()
    h, m = _hm(win.get("start", "01:00"))
    start = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if start <= now:
        start += timedelta(days=1)
    return (start - now).total_seconds()
