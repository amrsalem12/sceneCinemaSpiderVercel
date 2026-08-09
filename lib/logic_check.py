"""
Cron sweep — the background half of the bot (Mode B).

Pinged on a schedule by cron-job.org (only while enabled). Each run:
  1. Loop every user's active watches (both chains).
  2. For each, check if the watched movie is now bookable (matching filters).
  3. If open -> LOUD repeated alert with book buttons; keep alerting each run
     until the user sends /booked or /remove (we do NOT auto-clear).
  4. If no user has any active watch left -> auto-disable the cron.

Returns JSON summary (also handy for manual GET tests / heartbeat).
"""
import os
import sys
import json
from datetime import datetime, timedelta


from lib import store, telegram, vox, scene, cronjob  # noqa: E402

ALERT_REPEAT = int(os.getenv("ALERT_REPEAT", "5"))
ALERT_INTERVAL = int(os.getenv("ALERT_INTERVAL", "3"))
TZ_OFFSET = int(os.getenv("TZ_OFFSET", "3"))       # Cairo


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

    if chain == "vox":
        b = bundle_cache.get("vox")
        if b is None:
            b = bundle_cache["vox"] = vox.fetch_bundle()
        sess = vox.sessions_for(b, movie_slug=slug, cinemas=cinemas,
                                time_filter=tf, only_available=True)
        return [(f"{s['cinema'][:16]} · {s['experience'][:8]} · {s['time']} "
                 f"({s['seats']} left)", s["bookingUrl"]) for s in sess[:10]]

    # scene
    if not scene.is_bookable(slug):
        return []
    days = sorted(scene.open_days(slug))
    if not days:
        return []
    sess = scene.sessions_for(slug, days[0], time_filter=tf)
    return [(f"{s['experience']} · {s['time']}", s["showtime_url"])
            for s in sess[:10]]


def run_sweep():
    summary = {"checked": 0, "alerts": 0, "users": 0, "errors": []}
    bundle_cache = {}
    any_active = False

    for chat_id in store.all_chat_ids():
        wl = store.get_watchlist(chat_id)
        if not wl:
            continue
        summary["users"] += 1
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
                summary["alerts"] += 1

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
