"""
Cron sweep — the background half of the bot (Mode B).

Pinged on a schedule by cron-job.org (only while enabled). Each run:
  1. Loop every user's active watches (both chains).
  2. For each, check if the watched movie is now bookable (matching filters).
  3. If open -> LOUD repeated alert with book buttons; keep alerting each run
     until the user sends /booked or /remove (we do NOT auto-clear).
  4. Send each user a QUIET hourly status ("still watching …") so they know the
     watcher is alive — gated by a stored timestamp so a fast cron doesn't spam.
  5. If no user has any active watch left -> auto-disable the cron.

Returns JSON summary (also handy for manual GET tests / heartbeat).
"""
import os
import sys
import json
import time
from datetime import datetime, timedelta


from lib import store, telegram, vox, scene, cronjob  # noqa: E402

ALERT_REPEAT = int(os.getenv("ALERT_REPEAT", "5"))
ALERT_INTERVAL = int(os.getenv("ALERT_INTERVAL", "3"))
TZ_OFFSET = int(os.getenv("TZ_OFFSET", "3"))               # Cairo
STATUS_EVERY = int(os.getenv("STATUS_EVERY_SEC", "3600"))  # hourly status ping


def _local_now():
    return datetime.utcnow() + timedelta(hours=TZ_OFFSET)


def check_watch(bundle_cache, watch):
    """
    Return a list of (label, bookingUrl) tuples if the watch is OPEN now
    (matching its filters, available seats), else [].
    bundle_cache: dict to memoize the VOX bundle within one run.
    """
    chain = watch["chain"]
    slug = watch["movieSlug"]
    cinemas = watch.get("cinemas", "any")
    tf = watch.get("timeFilter", "any")
    # date filter: "any", a single YYYYMMDD int, or a list of them
    want_date = watch.get("date", "any")
    date_set = None
    if want_date != "any":
        date_set = set(want_date) if isinstance(want_date, list) else {want_date}

    if chain == "vox":
        b = bundle_cache.get("vox")
        if b is None:
            b = bundle_cache["vox"] = vox.fetch_bundle()
        if date_set:
            sess = []
            for d in date_set:
                sess += vox.sessions_for(b, movie_slug=slug, cinemas=cinemas,
                                         display_date=d, time_filter=tf,
                                         only_available=True)
        else:
            sess = vox.sessions_for(b, movie_slug=slug, cinemas=cinemas,
                                    time_filter=tf, only_available=True)
        # VOX `seats` is a binary available/sold-out flag, not a count — so we
        # just say the show is bookable, no fake "(N left)".
        return [(f"{s['cinema'][:16]} · {s['experience']} · {s['time']}",
                 s["bookingUrl"]) for s in sess[:10]]

    # scene
    if not scene.is_bookable(slug):
        return []
    open_days = sorted(scene.open_days(slug))
    if not open_days:
        return []
    if date_set:
        want_ddmm = {scene.to_ddmmyyyy(d) for d in date_set}
        target_days = [d for d in open_days if d in want_ddmm]
        if not target_days:
            return []
    else:
        target_days = [open_days[0]]
    hits = []
    for d in target_days:
        for x in scene.sessions_for(slug, d, time_filter=tf):
            hits.append((f"{x['experience']} · {x['time']} ({d})", x["showtime_url"]))
    return hits[:10]


def _watch_summary(wl, open_ids):
    """Build the quiet hourly 'still watching' status text."""
    lines = ["👀 <b>Still watching</b> — hourly update"]
    for i, w in enumerate(wl, 1):
        cine = "any" if w["cinemas"] == "any" else ",".join(w["cinemas"])
        dlabel = w.get("dateLabel", "any date")
        flag = " — ⏰ OPEN NOW" if w["id"] in open_ids else ""
        lines.append(f"{i}. {w['movieTitle']} [{w['chain']}] @ {cine} "
                     f"· {dlabel} · {w['timeFilter']}{flag}")
    lines.append("\n/list to manage · /booked &lt;n&gt; or /remove &lt;n&gt; to stop.")
    return "\n".join(lines)


def _maybe_send_status(chat_id, wl, open_ids, summary):
    """Send the status at most once per the user's chosen interval.
    Interval set from Telegram via /status (key status_interval:<chat_id>):
    seconds, 0 = off, unset = STATUS_EVERY default. Records why it did/didn't
    send into summary['status_debug'] so /api/check reveals the gate state."""
    iv = store._get(f"status_interval:{chat_id}", None)
    iv = STATUS_EVERY if iv is None else int(iv)
    now_ts = time.time()
    try:
        last = float(store._get(f"status_ts:{chat_id}", 0) or 0)
    except (TypeError, ValueError):
        last = 0
    since = round(now_ts - last)
    dbg = {"chat": str(chat_id)[-4:], "interval_s": iv, "since_last_s": since}

    if iv <= 0:
        dbg["decision"] = "off"
    elif since < iv - 60:                       # small tolerance to avoid drift
        dbg["decision"] = f"wait {iv - 60 - since}s"
    else:
        res = telegram.send_message(chat_id, _watch_summary(wl, open_ids),
                                    silent=False)     # notify — this is the ping
        ok = isinstance(res, dict) and res.get("ok")
        if ok:
            store._set(f"status_ts:{chat_id}", now_ts)
            dbg["decision"] = "SENT"
        else:
            dbg["decision"] = f"send_failed: {res}"
    summary.setdefault("status_debug", []).append(dbg)
    return dbg.get("decision") == "SENT"


def run_sweep():
    summary = {"checked": 0, "alerts": 0, "users": 0, "status_sent": 0, "errors": []}
    bundle_cache = {}
    any_active = False

    for chat_id in store.all_chat_ids():
        wl = store.get_watchlist(chat_id)
        if not wl:
            continue
        summary["users"] += 1
        open_ids = set()
        for w in wl:
            any_active = True
            summary["checked"] += 1
            try:
                hits = check_watch(bundle_cache, w)
            except Exception as e:
                summary["errors"].append(f"{w.get('movieSlug')}: {e}")
                continue
            if hits:
                # OPEN -> loud alert (every run until /booked or /remove)
                title = w["movieTitle"]
                buttons = [[h] for h in hits]
                telegram.alert_burst(
                    chat_id,
                    f"🎬🔔 <b>{title}</b> is OPEN for booking!\n"
                    f"Tap a showtime to book on the cinema site:",
                    buttons=buttons,
                    repeat=ALERT_REPEAT, interval=ALERT_INTERVAL,
                )
                store.set_alerted(chat_id, w["id"], True)
                open_ids.add(w["id"])
                summary["alerts"] += 1

        # quiet hourly heartbeat of what we're watching for this user
        if _maybe_send_status(chat_id, wl, open_ids, summary):
            summary["status_sent"] += 1

    # auto-disable cron if nothing left to watch anywhere
    if not any_active:
        last = store._get("cron_state", None)
        res = cronjob.sync_to_watches(False, last)
        if res.get("changed"):
            store._set("cron_state", res["state"])
        summary["cron"] = "disabled (no active watches)"
    else:
        summary["cron"] = "active"

    return summary
