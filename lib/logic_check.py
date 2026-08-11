"""
Cron sweep — the background half of the bot (Mode B).

Pinged on a schedule by cron-job.org (only while enabled). Each run:
  1. Loop every user's active watches (both chains).
  2. For each, check if the watched movie has NEW showtimes matching filters.
  3. If a new showtime appears -> LOUD repeated alert with book buttons;
     keep alerting each run until the user sends /booked or /remove
     (we do NOT auto-clear).
  4. Send each user a QUIET hourly status ("still watching …") so they know the
     watcher is alive — gated by a stored timestamp so a fast cron doesn't spam.
  5. If no user has any active watch left -> auto-disable the cron.

Returns JSON summary (also handy for manual GET tests / heartbeat).

IMPORTANT:
  seenSessions contains the showtimes that already existed when the user
  created the watch.

  The watcher must NOT alert for those existing showtimes.

  Only showtimes that appear after the watch was created are considered NEW.

  Once a NEW showtime is discovered, it is stored in alertedSessions so it
  continues to trigger the repeated alert on subsequent cron runs while it
  remains available.
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


def _session_key(chain, session):
    """
    Return the stable identifier used to determine whether a showtime is
    already known to this watch.

    VOX:
        Use the session id when available, otherwise bookingUrl.

    Scene:
        Use the showtime URL because Scene does not expose a separate
        persistent session id in the current data layer.
    """
    if chain == "vox":
        session_id = session.get("id")
        if session_id:
            return f"vox:{session_id}"

        return f"vox:{session.get('bookingUrl', '')}"

    return f"scene:{session.get('showtime_url', '')}"


def _current_sessions(bundle_cache, watch):
    """
    Return the currently matching AVAILABLE sessions for a watch.

    This function only performs filtering/fetching.

    The NEW-vs-existing comparison happens separately in check_watch().
    """
    chain = watch["chain"]
    slug = watch["movieSlug"]
    cinemas = watch.get("cinemas", "any")
    tf = watch.get("timeFilter", "any")

    # date filter: "any", a single YYYYMMDD int, or a list of them
    want_date = watch.get("date", "any")

    date_set = None

    if want_date != "any":
        date_set = (
            set(want_date)
            if isinstance(want_date, list)
            else {want_date}
        )

    # ------------------------------------------------------------
    # VOX
    # ------------------------------------------------------------

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

        return sess

    # ------------------------------------------------------------
    # SCENE
    # ------------------------------------------------------------

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
            d
            for d in open_days
            if d in want_ddmm
        ]

        if not target_days:
            return []

    else:
        # IMPORTANT:
        #
        # The old code used only:
        #
        #     target_days = [open_days[0]]
        #
        # which meant that Scene watches without a specific date only
        # checked the first open day, commonly today's date.
        #
        # For a watcher looking for NEW future showtimes, we need to inspect
        # all currently open days.
        target_days = open_days

    sessions = []

    for d in target_days:
        for x in scene.sessions_for(
            slug,
            d,
            time_filter=tf,
        ):
            sessions.append(x)

    return sessions


def check_watch(bundle_cache, watch):
    """
    Return a list of (label, bookingUrl) tuples for NEW showtimes.

    A showtime is NEW if its stable session key does not exist in
    watch["seenSessions"].

    Once a showtime is discovered as NEW, its key is stored in
    watch["alertedSessions"].

    On later cron runs, that showtime remains eligible for alerting as long
    as it is still currently available.

    Existing showtimes from when the watch was created are ignored.

    For old watches that do not have seenSessions, the current sessions are
    established as the baseline and nothing is alerted on that first run.
    """

    chain = watch["chain"]

    current = _current_sessions(
        bundle_cache,
        watch,
    )

    # Build current session map while preserving the order returned by the
    # cinema data layer.
    current_by_key = {}

    for session in current:
        key = _session_key(
            chain,
            session,
        )

        if not key:
            continue

        if key not in current_by_key:
            current_by_key[key] = session

    current_keys = set(current_by_key.keys())

    # ------------------------------------------------------------
    # Existing baseline
    # ------------------------------------------------------------

    seen_exists = "seenSessions" in watch

    seen_sessions = watch.get(
        "seenSessions",
        [],
    )

    if not isinstance(seen_sessions, list):
        seen_sessions = []

    seen = set(seen_sessions)

    # ------------------------------------------------------------
    # Migration safety for watches created before seenSessions existed.
    #
    # We must NOT suddenly alert the user about every showtime that already
    # exists. The first run simply establishes the baseline.
    # ------------------------------------------------------------

    if not seen_exists:
        watch["seenSessions"] = list(current_keys)

        if "alertedSessions" not in watch:
            watch["alertedSessions"] = []

        return []

    # ------------------------------------------------------------
    # Sessions that appeared AFTER the watch was created.
    # ------------------------------------------------------------

    new_keys = current_keys - seen

    # ------------------------------------------------------------
    # Previously discovered new sessions.
    #
    # alertedSessions is intentionally persisted so a newly added showtime
    # continues to alert on every cron run until the watch is removed/booked.
    # ------------------------------------------------------------

    alerted_sessions = watch.get(
        "alertedSessions",
        [],
    )

    if not isinstance(alerted_sessions, list):
        alerted_sessions = []

    alerted = set(alerted_sessions)

    # Any newly discovered sessions are now marked as alerted.
    if new_keys:
        alerted.update(new_keys)

    watch["alertedSessions"] = list(alerted)

    # ------------------------------------------------------------
    # We want:
    #
    #   NEW session
    #       OR
    #   previously detected NEW session
    #
    # but NEVER a session that was already in seenSessions.
    #
    # Because seenSessions is the original baseline and does not get updated
    # with newly discovered sessions, the same newly added show remains in
    # this set on subsequent cron runs.
    # ------------------------------------------------------------

    eligible_keys = (
        alerted
        - seen
    )

    # Only sessions that are currently available should be returned.
    eligible_keys &= current_keys

    hits = []

    for session in current:
        key = _session_key(
            chain,
            session,
        )

        if key not in eligible_keys:
            continue

        if chain == "vox":
            # VOX `seats` is a binary available/sold-out flag, not a count —
            # so we just say the show is bookable, no fake "(N left)".
            label = (
                f"{session['cinema'][:16]} · "
                f"{session['experience']} · "
                f"{session['time']}"
            )

            booking_url = session["bookingUrl"]

        else:
            label = (
                f"{session['experience']} · "
                f"{session['time']}"
            )

            booking_url = session["showtime_url"]

        hits.append(
            (
                label,
                booking_url,
            )
        )

    return hits[:10]


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

        flag = (
            " — ⏰ OPEN NOW"
            if w["id"] in open_ids
            else ""
        )

        lines.append(
            f"{i}. {w['movieTitle']} [{w['chain']}] @ {cine} "
            f"· {dlabel} · {w['timeFilter']}{flag}"
        )

    lines.append(
        "\n/list to manage · /booked &lt;n&gt; or /remove &lt;n&gt; to stop."
    )

    return "\n".join(lines)


def _maybe_send_status(chat_id, wl, open_ids, summary):
    """
    Send the status at most once per the user's chosen interval.

    Interval set from Telegram via /status (key status_interval:<chat_id>):
    seconds, 0 = off, unset = STATUS_EVERY default.

    Records why it did/didn't send into summary['status_debug'] so /api/check
    reveals the gate state.
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
        # Small tolerance to avoid drift.
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

        # notify — this is the ping
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
                # NEW SHOWTIME -> loud alert.
                #
                # Every cron run will continue to alert for a newly added
                # showtime while it remains available, until /booked or
                # /remove clears the watch.

                title = w["movieTitle"]

                buttons = [
                    [h]
                    for h in hits
                ]

                telegram.alert_burst(
                    chat_id,
                    f"🎬🔔 <b>{title}</b> has a NEW showtime!\n"
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

        # check_watch() may have updated seenSessions / alertedSessions.
        #
        # Persist the complete watchlist so those changes survive the next
        # cron invocation.
        store._set(
            f"watchlist:{chat_id}",
            wl,
        )

        # Quiet hourly heartbeat of what we're watching for this user.
        if _maybe_send_status(
            chat_id,
            wl,
            open_ids,
            summary,
        ):
            summary["status_sent"] += 1

    # Auto-disable cron if nothing left to watch anywhere.
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
