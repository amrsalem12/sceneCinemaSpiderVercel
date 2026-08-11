"""
Cron sweep — the background half of the bot (Mode B).

Pinged on a schedule by cron-job.org (only while enabled). Each run:
  1. Loop every user's active watches (both chains).
  2. For each, check if the watched movie has NEW bookable showtimes
     matching ALL of the user's filters:
       - cinema
       - experience
       - date
       - time filter
  3. Existing showtimes that were already present when the watch was created
     are ignored.
  4. If a NEW matching showtime appears -> LOUD repeated alert with book
     buttons; keep alerting each run until the user sends /booked or /remove.
  5. Send each user a QUIET hourly status ("still watching …") so they know
     the watcher is alive — gated by a stored timestamp so a fast cron
     doesn't spam.
  6. If no user has any active watch left -> auto-disable the cron.

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


# ---------------------------------------------------------------------------
# Experience matching
# ---------------------------------------------------------------------------

def _normalize_experience(value):
    """
    Normalize experience names/codes so old/new watcher data can be compared.

    VOX:
      imx -> IMAX
      gd  -> Gold
      mx  -> MAX
      fx  -> 4DX
      kd  -> Kids
      st  -> Standard

    Scene:
      imax -> ScreenX
      vip  -> Premiere
      stand -> Standard
    """
    if value is None:
        return "any"

    s = str(value).strip().lower()

    aliases = {
        "any": "any",

        # VOX
        "imx": "imax",
        "imax": "imax",
        "gd": "gold",
        "gold": "gold",
        "mx": "max",
        "max": "max",
        "fx": "4dx",
        "4dx": "4dx",
        "kd": "kids",
        "kids": "kids",
        "st": "standard",
        "standard": "standard",

        # Scene
        "screenx": "screenx",
        "premiere": "premiere",
        "vip": "premiere",
        "stand": "standard",
    }

    return aliases.get(s, s)


def _experience_matches(session_experience, wanted_experience):
    """
    True only when the session has the experience requested by the watch.

    A missing/old watch experience means "any experience" for backwards
    compatibility.
    """
    wanted = _normalize_experience(wanted_experience)

    if wanted == "any":
        return True

    actual = _normalize_experience(session_experience)

    return actual == wanted


# ---------------------------------------------------------------------------
# Session identity / new-session detection
# ---------------------------------------------------------------------------

def _session_key(chain, session):
    """
    Stable identifier for a showtime.

    VOX session ids are preferred. Booking URL is the fallback.

    Scene uses the showtime URL.
    """
    if chain == "vox":
        return f"vox:{session.get('id') or session.get('bookingUrl')}"

    return f"scene:{session.get('showtime_url')}"


def _get_seen_sessions(watch):
    """
    Return the watcher snapshot as a set.

    Older watches created before seenSessions existed are treated as having
    an empty snapshot. New watches created by the corrected webhook always
    have this field populated.
    """
    raw = watch.get("seenSessions", [])

    if not isinstance(raw, list):
        return set()

    return set(str(x) for x in raw)


# ---------------------------------------------------------------------------
# Watch matching
# ---------------------------------------------------------------------------

def check_watch(bundle_cache, watch):
    """
    Return a list of (label, bookingUrl) tuples if the watch has NEW matching
    showtimes now.

    A showtime must satisfy ALL of:
      - movie
      - selected cinema
      - selected experience
      - selected date(s)
      - selected time filter
      - available seats
      - NOT already present in seenSessions when the watch was created

    bundle_cache: dict to memoize the VOX bundle within one run.
    """

    chain = watch["chain"]
    slug = watch["movieSlug"]

    cinemas = watch.get("cinemas", "any")
    tf = watch.get("timeFilter", "any")
    wanted_experience = watch.get("experience", "any")

    # date filter:
    # "any", a single YYYYMMDD int, or a list of YYYYMMDD ints
    want_date = watch.get("date", "any")

    date_set = None

    if want_date != "any":
        date_set = (
            set(want_date)
            if isinstance(want_date, list)
            else {want_date}
        )

    seen = _get_seen_sessions(watch)

    # -----------------------------------------------------------------------
    # VOX
    # -----------------------------------------------------------------------

    if chain == "vox":

        b = bundle_cache.get("vox")

        if b is None:
            b = bundle_cache["vox"] = vox.fetch_bundle()

        if date_set:

            sess = []

            for d in date_set:
                sess += vox.sessions_for(
                    b,
                    movie_slug=slug,
                    cinemas=cinemas,
                    display_date=d,
                    time_filter=tf,
                    only_available=True,
                )

        else:

            sess = vox.sessions_for(
                b,
                movie_slug=slug,
                cinemas=cinemas,
                time_filter=tf,
                only_available=True,
            )

        # IMPORTANT:
        # Filter experience AFTER retrieving sessions.
        #
        # This is the bug that caused:
        #
        #   User wanted IMAX
        #   Gold/Standard opened
        #   watcher said OPEN
        #
        # We now require the exact requested experience.
        sess = [
            s for s in sess
            if _experience_matches(
                s.get("experience"),
                wanted_experience,
            )
        ]

        # Only NEW sessions should trigger the watcher.
        new_sess = [
            s for s in sess
            if _session_key("vox", s) not in seen
        ]

        return [
            (
                f"{s['cinema'][:16]} · "
                f"{s['experience']} · "
                f"{s['time']}",
                s["bookingUrl"],
            )
            for s in new_sess[:10]
        ]

    # -----------------------------------------------------------------------
    # Scene
    # -----------------------------------------------------------------------

    # Scene's open_days() only tells us that SOME experience has opened.
    #
    # That is NOT sufficient for a watcher.
    #
    # We therefore inspect the actual sessions and apply the exact same
    # experience filtering used by VOX.

    if not scene.is_bookable(slug):
        return []

    open_days = sorted(scene.open_days(slug))

    if not open_days:
        return []

    if date_set:

        want_ddmm = {
            scene.to_ddmmyyyy(d)
            for d in date_set
        }

        target_days = [
            d for d in open_days
            if d in want_ddmm
        ]

        if not target_days:
            return []

    else:
        target_days = open_days

    hits = []

    for d in target_days:

        sessions = scene.sessions_for(
            slug,
            d,
            time_filter=tf,
        )

        # EXACT experience filtering.
        sessions = [
            x for x in sessions
            if _experience_matches(
                x.get("experience"),
                wanted_experience,
            )
        ]

        # Only sessions that did not exist when the watch was created.
        sessions = [
            x for x in sessions
            if _session_key("scene", x) not in seen
        ]

        for x in sessions:

            hits.append(
                (
                    f"{x['experience']} · "
                    f"{x['time']} ({d})",
                    x["showtime_url"],
                )
            )

    return hits[:10]


# ---------------------------------------------------------------------------
# Status message
# ---------------------------------------------------------------------------

def _watch_summary(wl, open_ids):
    """Build the quiet hourly 'still watching' status text."""

    lines = [
        "👀 <b>Still watching</b> — hourly update"
    ]

    for i, w in enumerate(wl, 1):

        cine = (
            "any"
            if w["cinemas"] == "any"
            else ",".join(w["cinemas"])
        )

        dlabel = w.get(
            "dateLabel",
            "any date",
        )

        experience = w.get(
            "experienceLabel",
            w.get("experience", "any"),
        )

        flag = (
            " — ⏰ OPEN NOW"
            if w["id"] in open_ids
            else ""
        )

        lines.append(
            f"{i}. {w['movieTitle']} "
            f"[{w['chain']}] "
            f"@ {cine} "
            f"· {experience} "
            f"· {dlabel} "
            f"· {w['timeFilter']}"
            f"{flag}"
        )

    lines.append(
        "\n/list to manage · "
        "/booked &lt;n&gt; or /remove &lt;n&gt; to stop."
    )

    return "\n".join(lines)


def _maybe_send_status(chat_id, wl, open_ids, summary):
    """
    Send the status at most once per the user's chosen interval.

    Interval set from Telegram via /status
    (key status_interval:<chat_id>):
      seconds
      0 = off
      unset = STATUS_EVERY default

    Records why it did/didn't send into summary['status_debug'] so
    /api/check reveals the gate state.
    """

    iv = store._get(
        f"status_interval:{chat_id}",
        None,
    )

    iv = (
        STATUS_EVERY
        if iv is None
        else int(iv)
    )

    now_ts = time.time()

    try:
        last = float(
            store._get(
                f"status_ts:{chat_id}",
                0,
            )
            or 0
        )

    except (TypeError, ValueError):
        last = 0

    since = round(
        now_ts - last
    )

    dbg = {
        "chat": str(chat_id)[-4:],
        "interval_s": iv,
        "since_last_s": since,
    }

    if iv <= 0:

        dbg["decision"] = "off"

    elif since < iv - 60:

        dbg["decision"] = (
            f"wait {iv - 60 - since}s"
        )

    else:

        res = telegram.send_message(
            chat_id,
            _watch_summary(
                wl,
                open_ids,
            ),
            silent=False,
        )

        ok = (
            isinstance(res, dict)
            and res.get("ok")
        )

        if ok:

            store._set(
                f"status_ts:{chat_id}",
                now_ts,
            )

            dbg["decision"] = "SENT"

        else:

            dbg["decision"] = (
                f"send_failed: {res}"
            )

    summary.setdefault(
        "status_debug",
        [],
    ).append(dbg)

    return (
        dbg.get("decision")
        == "SENT"
    )


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

def run_sweep():

    summary = {
        "checked": 0,
        "alerts": 0,
        "users": 0,
        "status_sent": 0,
        "errors": [],
    }

    bundle_cache = {}

    any_active = False

    for chat_id in store.all_chat_ids():

        wl = store.get_watchlist(
            chat_id
        )

        if not wl:
            continue

        summary["users"] += 1

        open_ids = set()

        for w in wl:

            any_active = True

            summary["checked"] += 1

            try:

                hits = check_watch(
                    bundle_cache,
                    w,
                )

            except Exception as e:

                summary["errors"].append(
                    f"{w.get('movieSlug')}: {e}"
                )

                continue

            if hits:

                # OPEN -> loud alert.
                #
                # Deliberately do NOT clear the watch and do NOT mark the
                # sessions as seen here. The user asked for repeated alerts
                # until /booked or /remove.

                title = w["movieTitle"]

                buttons = [
                    [h]
                    for h in hits
                ]

                experience = w.get(
                    "experienceLabel",
                    w.get("experience", "any"),
                )

                telegram.alert_burst(
                    chat_id,

                    f"🎬🔔 <b>{title}</b> "
                    f"is OPEN for booking!\n"
                    f"Experience: <b>{experience}</b>\n"
                    f"Tap a showtime to book on the cinema site:",

                    buttons=buttons,

                    repeat=ALERT_REPEAT,
                    interval=ALERT_INTERVAL,
                )

                store.set_alerted(
                    chat_id,
                    w["id"],
                    True,
                )

                open_ids.add(
                    w["id"]
                )

                summary["alerts"] += 1

        # quiet hourly heartbeat
        if _maybe_send_status(
            chat_id,
            wl,
            open_ids,
            summary,
        ):
            summary["status_sent"] += 1

    # auto-disable cron if nothing left to watch anywhere
    if not any_active:

        last = store._get(
            "cron_state",
            None,
        )

        res = cronjob.sync_to_watches(
            False,
            last,
        )

        if res.get("changed"):

            store._set(
                "cron_state",
                res["state"],
            )

        summary["cron"] = (
            "disabled (no active watches)"
        )

    else:

        summary["cron"] = "active"

    return summary
