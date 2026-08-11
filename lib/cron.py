"""
Cron sweep — the background half of the bot (Mode B).

Pinged on a schedule by cron-job.org (only while enabled). Each run:
  1. Loop every user's active watches (both chains).
  2. Find ONLY showtimes that are NEW compared with the showtimes that
     existed when the watch was created.
  3. A newly-added showtime is alerted loudly and repeatedly on every cron
     run until the user sends /booked or /remove.
  4. Existing showtimes are ignored, even if they are available today.
  5. Send each user a QUIET hourly status ("still watching …").
  6. If no user has any active watch left -> auto-disable the cron.

Important:
  `seenSessions` is the baseline snapshot captured when the watch is created.

  Example:
    Watch created:
      17:00 already exists
      20:00 already exists

    Later:
      17:00 exists
      20:00 exists
      22:00 appears

    Only 22:00 triggers the alert.

  The newly detected 22:00 remains "new" relative to the original
  `seenSessions`, so it continues to alert every cron run until the watch
  is removed/booked.

Returns JSON summary (also handy for manual GET tests / heartbeat).
"""

import os
import time

from lib import store, telegram, vox, scene, cronjob


ALERT_REPEAT = int(os.getenv("ALERT_REPEAT", "5"))
ALERT_INTERVAL = int(os.getenv("ALERT_INTERVAL", "3"))
TZ_OFFSET = int(os.getenv("TZ_OFFSET", "3"))               # Cairo
STATUS_EVERY = int(os.getenv("STATUS_EVERY_SEC", "3600"))  # hourly status ping


def _session_key(chain, session):
    """
    Build the same stable identifier used when the watch is created.

    VOX:
      Prefer the session id, then bookingUrl.

    Scene:
      Use the showtime URL.
    """
    if chain == "vox":
        return f"vox:{session.get('id') or session.get('bookingUrl')}"

    return f"scene:{session.get('showtime_url')}"


def _local_now():
    from datetime import datetime, timedelta

    return datetime.utcnow() + timedelta(hours=TZ_OFFSET)


def _date_set(watch):
    """
    Convert the watch's date field into a set.

    Supported:
      "any"
      YYYYMMDD
      [YYYYMMDD, YYYYMMDD, ...]
    """
    want_date = watch.get("date", "any")

    if want_date == "any":
        return None

    if isinstance(want_date, list):
        return set(want_date)

    return {want_date}


def _watch_seen_sessions(watch):
    """
    Return the baseline sessions captured when this watch was created.

    Old watches that don't have seenSessions are treated as having an empty
    baseline. That means their currently available sessions may be detected
    as new on the first sweep, which is preferable to silently missing them.
    """
    raw = watch.get("seenSessions", [])

    if not isinstance(raw, list):
        return set()

    return set(str(x) for x in raw)


def _new_available_sessions(watch, bundle_cache):
    """
    Return only sessions which:

      1. Match the watch's movie/cinema/date/time filters.
      2. Are currently bookable/available.
      3. Were NOT present when the watch was created.

    This is the key distinction between:
      "showtimes that exist"
    and:
      "new showtimes that appeared after the watch was created."
    """
    chain = watch["chain"]
    slug = watch["movieSlug"]
    cinemas = watch.get("cinemas", "any")
    time_filter = watch.get("timeFilter", "any")
    date_set = _date_set(watch)

    baseline = _watch_seen_sessions(watch)

    # ---------------------------------------------------------
    # VOX
    # ---------------------------------------------------------
    if chain == "vox":
        bundle = bundle_cache.get("vox")

        if bundle is None:
            bundle = vox.fetch_bundle()
            bundle_cache["vox"] = bundle

        sessions = []

        if date_set:
            for display_date in sorted(date_set):
                sessions.extend(
                    vox.sessions_for(
                        bundle,
                        movie_slug=slug,
                        cinemas=cinemas,
                        display_date=display_date,
                        time_filter=time_filter,
                        only_available=True,
                    )
                )
        else:
            sessions = vox.sessions_for(
                bundle,
                movie_slug=slug,
                cinemas=cinemas,
                time_filter=time_filter,
                only_available=True,
            )

        new_sessions = []

        for session in sessions:
            key = _session_key("vox", session)

            if key in baseline:
                continue

            new_sessions.append(session)

        return new_sessions

    # ---------------------------------------------------------
    # SCENE
    # ---------------------------------------------------------
    if not scene.is_bookable(slug):
        return []

    open_days = sorted(scene.open_days(slug))

    if not open_days:
        return []

    if date_set:
        target_days = []

        for d in open_days:
            try:
                # Scene open_days uses DD-MM-YYYY.
                ymd = int(
                    f"{d[6:10]}{d[3:5]}{d[0:2]}"
                )
            except Exception:
                continue

            if ymd in date_set:
                target_days.append(d)

    else:
        # IMPORTANT:
        # "any date" means ALL currently open days.
        #
        # The old watcher only checked open_days[0], which meant it could
        # completely miss a newly-added showtime on a later date.
        target_days = open_days

    new_sessions = []

    for display_date in target_days:
        sessions = scene.sessions_for(
            slug,
            display_date,
            time_filter=time_filter,
        )

        for session in sessions:
            key = _session_key("scene", session)

            if key in baseline:
                continue

            new_sessions.append(session)

    return new_sessions


def check_watch(bundle_cache, watch):
    """
    Return a list of (label, bookingUrl) tuples for NEW showtimes only.

    Existing showtimes are deliberately ignored.
    """
    chain = watch["chain"]

    sessions = _new_available_sessions(
        watch,
        bundle_cache,
    )

    hits = []

    if chain == "vox":
        for session in sessions:
            hits.append(
                (
                    (
                        f"{session['cinema'][:16]} · "
                        f"{session['experience']} · "
                        f"{session['time']}"
                    ),
                    session["bookingUrl"],
                )
            )

    else:
        for session in sessions:
            hits.append(
                (
                    (
                        f"{session['experience']} · "
                        f"{session['time']}"
                    ),
                    session["showtime_url"],
                )
            )

    return hits[:10]


def _watch_summary(wl, open_ids):
    """
    Build the quiet hourly 'still watching' status text.
    """
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

        flag = (
            " — ⏰ NEW SHOWTIME"
            if w["id"] in open_ids
            else ""
        )

        lines.append(
            f"{i}. {w['movieTitle']} [{w['chain']}] @ {cine} "
            f"· {dlabel} · {w['timeFilter']}{flag}"
        )

    lines.append(
        "\n/list to manage · "
        "/booked &lt;n&gt; or /remove &lt;n&gt; to stop."
    )

    return "\n".join(lines)


def _maybe_send_status(
    chat_id,
    wl,
    open_ids,
    summary,
):
    """
    Send the status at most once per the user's chosen interval.

    Interval set from Telegram via /status:
      status_interval:<chat_id>

    Values:
      seconds
      0 = off
      unset = STATUS_EVERY default
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

    since = round(now_ts - last)

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

    return dbg.get("decision") == "SENT"


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

        wl = store.get_watchlist(chat_id)

        if not wl:
            continue

        summary["users"] += 1

        open_ids = set()

        for watch in wl:

            any_active = True
            summary["checked"] += 1

            try:
                hits = check_watch(
                    bundle_cache,
                    watch,
                )

            except Exception as e:
                summary["errors"].append(
                    f"{watch.get('movieSlug')}: {e}"
                )
                continue

            if hits:

                # -------------------------------------------------
                # NEW SHOWTIME FOUND
                #
                # We intentionally DO NOT add these sessions to
                # seenSessions here.
                #
                # seenSessions is the original baseline:
                # "what existed when the user started watching?"
                #
                # Therefore the newly-added showtime remains new
                # and the alert repeats every cron run until the
                # user sends /booked or /remove.
                # -------------------------------------------------

                title = watch["movieTitle"]

                buttons = [
                    [hit]
                    for hit in hits
                ]

                telegram.alert_burst(
                    chat_id,
                    (
                        f"🎬🔔 <b>{title}</b> — "
                        f"NEW SHOWTIME OPENED!\n"
                        f"Tap a showtime to book on the cinema site:"
                    ),
                    buttons=buttons,
                    repeat=ALERT_REPEAT,
                    interval=ALERT_INTERVAL,
                )

                store.set_alerted(
                    chat_id,
                    watch["id"],
                    True,
                )

                open_ids.add(
                    watch["id"]
                )

                summary["alerts"] += 1

        # ---------------------------------------------------------
        # Quiet hourly heartbeat
        # ---------------------------------------------------------
        if _maybe_send_status(
            chat_id,
            wl,
            open_ids,
            summary,
        ):
            summary["status_sent"] += 1

    # -------------------------------------------------------------
    # Auto-disable cron if nothing is being watched.
    # -------------------------------------------------------------
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
