"""
Cron sweep — the background half of the bot (Mode B).

Each run:
  1. Loop every user's active watches.
  2. Check the current matching showtimes.
  3. Compare them against the showtimes that existed when the watch was created.
  4. Alert ONLY when a genuinely new showtime appears.
  5. Once a new showtime is detected, keep alerting on subsequent runs while
     that showtime remains available, until the user sends /booked or /remove.
  6. Send each user a quiet status update according to their chosen interval.
  7. Disable cron automatically when no watches remain.

Important watcher behavior:

A watch created while:
    7:00 PM, 8:00 PM, 9:00 PM

already exist will NOT alert for those times.

If later:
    10:00 PM

appears, only the 10:00 PM showtime becomes a new alert.

The same behavior applies to both VOX and Scene.
"""

import os
import time

from lib import store, telegram, vox, scene, cronjob


ALERT_REPEAT = int(os.getenv("ALERT_REPEAT", "5"))
ALERT_INTERVAL = int(os.getenv("ALERT_INTERVAL", "3"))

TZ_OFFSET = int(os.getenv("TZ_OFFSET", "3"))

STATUS_EVERY = int(
    os.getenv("STATUS_EVERY_SEC", "3600")
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session_key(chain, session):
    """
    Return a stable identity for a showtime.

    VOX:
      Prefer the session ID, falling back to booking URL.

    Scene:
      The showtime URL identifies the actual showtime page.
    """
    if chain == "vox":
        return (
            f"vox:{session.get('id') or session.get('bookingUrl')}"
        )

    return f"scene:{session.get('showtime_url')}"


def _normalize_seen(value):
    """
    Normalize old/missing watch data.

    Older watches may not have seenSessions or alertSessions.
    """
    if not isinstance(value, list):
        return []

    return list(
        dict.fromkeys(
            str(x)
            for x in value
            if x
        )
    )


def _matching_sessions(bundle_cache, watch):
    """
    Return ALL currently available sessions matching the watch filters.

    This function does NOT decide whether a session is new.

    It only answers:
        "What bookable showtimes exist right now that match this watch?"
    """
    chain = watch["chain"]
    slug = watch["movieSlug"]

    cinemas = watch.get(
        "cinemas",
        "any",
    )

    time_filter = watch.get(
        "timeFilter",
        "any",
    )

    want_date = watch.get(
        "date",
        "any",
    )

    if want_date != "any":
        date_set = (
            set(want_date)
            if isinstance(want_date, list)
            else {want_date}
        )
    else:
        date_set = None

    # -----------------------------------------------------------------------
    # VOX
    # -----------------------------------------------------------------------

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

        return sessions

    # -----------------------------------------------------------------------
    # Scene
    # -----------------------------------------------------------------------

    if chain == "scene":
        try:
            open_days = sorted(
                scene.open_days(slug)
            )
        except Exception:
            return []

        if not open_days:
            return []

        # Specific date(s)
        if date_set:
            wanted_days = {
                scene.to_ddmmyyyy(d)
                for d in date_set
            }

            target_days = [
                d
                for d in open_days
                if d in wanted_days
            ]

        # "Any date" means ALL currently open days.
        #
        # The old code only checked open_days[0], which meant:
        #   - it could watch only the earliest open day
        #   - a new showtime on another already-open day was missed
        #
        else:
            target_days = open_days

        sessions = []

        for display_date in target_days:
            sessions.extend(
                scene.sessions_for(
                    slug,
                    display_date,
                    time_filter=time_filter,
                )
            )

        return sessions

    return []


def _session_label(chain, session):
    """
    Human-readable label for Telegram alert buttons.
    """
    if chain == "vox":
        return (
            f"{session['cinema'][:16]} · "
            f"{session['experience']} · "
            f"{session['time']}"
        )

    return (
        f"{session['experience']} · "
        f"{session['time']}"
    )


def _session_url(chain, session):
    """
    Return the booking URL for a session.
    """
    if chain == "vox":
        return session["bookingUrl"]

    return session["showtime_url"]


def _detect_new_sessions(watch, current_sessions):
    """
    Compare current sessions with the watch's baseline.

    Returns:
        new_sessions
        alert_sessions
        changed

    Data stored on the watch:

      seenSessions:
          Every matching showtime that has been observed.

      alertSessions:
          Newly discovered showtimes that are currently being alerted.

    Why two lists?

    Suppose the watch was created with:

        7 PM
        8 PM

    Those go into seenSessions.

    Later 9 PM appears.

    9 PM is added to:
        seenSessions
        alertSessions

    On the next cron run, 9 PM is already in seenSessions, but remains
    in alertSessions, so the user continues receiving the loud alert.

    This continues until /booked or /remove deletes the watch.

    Returns the current alertable sessions, not merely the newly discovered
    ones, so alert repetition works correctly.
    """

    seen = set(
        _normalize_seen(
            watch.get("seenSessions", [])
        )
    )

    alerting = set(
        _normalize_seen(
            watch.get("alertSessions", [])
        )
    )

    current_by_key = {}

    for session in current_sessions:
        key = _session_key(
            watch["chain"],
            session,
        )

        if not key:
            continue

        current_by_key[key] = session

    current_keys = set(
        current_by_key.keys()
    )

    # -----------------------------------------------------------------------
    # Detect genuinely new showtimes
    # -----------------------------------------------------------------------

    newly_discovered = (
        current_keys - seen
    )

    # Every currently observed session becomes part of the baseline.
    #
    # This is important:
    # once a session has been seen, it must never suddenly become "new"
    # merely because it temporarily sold out/disappeared and later returns.
    seen.update(current_keys)

    # Newly discovered sessions become alert sessions.
    alerting.update(
        newly_discovered
    )

    # -----------------------------------------------------------------------
    # Stop alerting sessions that are no longer currently bookable.
    # -----------------------------------------------------------------------

    alerting.intersection_update(
        current_keys
    )

    # Store the updated state on the watch.
    watch["seenSessions"] = sorted(
        seen
    )

    watch["alertSessions"] = sorted(
        alerting
    )

    # Only sessions that are currently available AND are in alertSessions
    # should generate an alert.
    alert_sessions = [
        current_by_key[key]
        for key in sorted(alerting)
        if key in current_by_key
    ]

    new_sessions = [
        current_by_key[key]
        for key in sorted(newly_discovered)
        if key in current_by_key
    ]

    changed = (
        set(watch.get("seenSessions", [])) != seen
        or set(watch.get("alertSessions", [])) != alerting
    )

    return (
        new_sessions,
        alert_sessions,
        changed,
    )


def check_watch(bundle_cache, watch):
    """
    Return:

        {
            "new": [...],
            "alert": [...],
        }

    where:

      new:
          Showtimes discovered for the first time after the watch was created.

      alert:
          Currently available newly-discovered showtimes that should continue
          producing alerts.

    Existing showtimes from watch creation are never returned as alertable.
    """

    current_sessions = _matching_sessions(
        bundle_cache,
        watch,
    )

    new_sessions, alert_sessions, changed = (
        _detect_new_sessions(
            watch,
            current_sessions,
        )
    )

    return {
        "new": new_sessions,
        "alert": alert_sessions,
        "changed": changed,
    }


# ---------------------------------------------------------------------------
# Quiet status
# ---------------------------------------------------------------------------

def _watch_summary(wl, open_ids):
    """
    Build the quiet status message.
    """

    lines = [
        "👀 <b>Still watching</b> — hourly update"
    ]

    for i, watch in enumerate(wl, 1):
        cinemas = watch.get(
            "cinemas",
            "any",
        )

        cine = (
            "any"
            if cinemas == "any"
            else ",".join(cinemas)
        )

        date_label = watch.get(
            "dateLabel",
            "any date",
        )

        flag = (
            " — ⏰ NEW SHOWTIME"
            if watch["id"] in open_ids
            else ""
        )

        lines.append(
            f"{i}. {watch['movieTitle']} "
            f"[{watch['chain']}] @ {cine} "
            f"· {date_label} "
            f"· {watch.get('timeFilter', 'any')}"
            f"{flag}"
        )

    lines.append(
        "\n/list to manage · "
        "/booked &lt;n&gt; or /remove &lt;n&gt; to stop."
    )

    return "\n".join(lines)


def _maybe_send_status(
    chat_id,
    watchlist,
    open_ids,
    summary,
):
    """
    Send the status at most once per configured interval.

    /status stores:
        status_interval:<chat_id>

    and the last successful send:
        status_ts:<chat_id>
    """

    interval = store._get(
        f"status_interval:{chat_id}",
        None,
    )

    interval = (
        STATUS_EVERY
        if interval is None
        else int(interval)
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
    except (
        TypeError,
        ValueError,
    ):
        last = 0

    since = round(
        now_ts - last
    )

    debug = {
        "chat": str(chat_id)[-4:],
        "interval_s": interval,
        "since_last_s": since,
    }

    if interval <= 0:
        debug["decision"] = "off"

    elif since < interval - 60:
        debug["decision"] = (
            f"wait {interval - 60 - since}s"
        )

    else:
        result = telegram.send_message(
            chat_id,
            _watch_summary(
                watchlist,
                open_ids,
            ),
            silent=False,
        )

        ok = (
            isinstance(result, dict)
            and result.get("ok")
        )

        if ok:
            store._set(
                f"status_ts:{chat_id}",
                now_ts,
            )

            debug["decision"] = "SENT"

        else:
            debug["decision"] = (
                f"send_failed: {result}"
            )

    summary.setdefault(
        "status_debug",
        [],
    ).append(debug)

    return (
        debug.get("decision")
        == "SENT"
    )


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def run_sweep():
    summary = {
        "checked": 0,
        "alerts": 0,
        "new_showtimes": 0,
        "users": 0,
        "status_sent": 0,
        "errors": [],
    }

    bundle_cache = {}

    any_active = False

    for chat_id in store.all_chat_ids():
        watchlist = store.get_watchlist(
            chat_id
        )

        if not watchlist:
            continue

        summary["users"] += 1

        open_ids = set()

        for watch in watchlist:
            any_active = True

            summary["checked"] += 1

            try:
                result = check_watch(
                    bundle_cache,
                    watch,
                )

                # Persist watcher state changes.
                #
                # This is crucial because seenSessions / alertSessions
                # are part of the watch itself.
                if result.get("changed"):
                    store._set(
                        f"watchlist:{chat_id}",
                        watchlist,
                    )

            except Exception as exc:
                summary["errors"].append(
                    f"{watch.get('movieSlug')}: {exc}"
                )
                continue

            new_sessions = result.get(
                "new",
                [],
            )

            alert_sessions = result.get(
                "alert",
                [],
            )

            summary["new_showtimes"] += len(
                new_sessions
            )

            if not alert_sessions:
                continue

            # ---------------------------------------------------------------
            # Loud alert
            # ---------------------------------------------------------------

            buttons = []

            for session in alert_sessions[:10]:
                buttons.append([
                    (
                        _session_label(
                            watch["chain"],
                            session,
                        ),
                        _session_url(
                            watch["chain"],
                            session,
                        ),
                    )
                ])

            title = watch["movieTitle"]

            if new_sessions:
                message = (
                    f"🎬🔔 <b>{title}</b> — "
                    f"NEW showtime opened!\n"
                    f"These showtimes were not available "
                    f"when you started watching:"
                )
            else:
                message = (
                    f"🎬🔔 <b>{title}</b> — "
                    f"new showtime is still available!\n"
                    f"Tap a showtime to book:"
                )

            telegram.alert_burst(
                chat_id,
                message,
                buttons=buttons,
                repeat=ALERT_REPEAT,
                interval=ALERT_INTERVAL,
            )

            # Keep the old watch-level alerted flag for /list compatibility.
            store.set_alerted(
                chat_id,
                watch["id"],
                True,
            )

            open_ids.add(
                watch["id"]
            )

            summary["alerts"] += 1

        # -------------------------------------------------------------------
        # Quiet heartbeat
        # -------------------------------------------------------------------

        if _maybe_send_status(
            chat_id,
            watchlist,
            open_ids,
            summary,
        ):
            summary["status_sent"] += 1

    # -----------------------------------------------------------------------
    # Cron state
    # -----------------------------------------------------------------------

    if not any_active:
        last = store._get(
            "cron_state",
            None,
        )

        result = cronjob.sync_to_watches(
            False,
            last,
        )

        if result.get("changed"):
            store._set(
                "cron_state",
                result["state"],
            )

        summary["cron"] = (
            "disabled (no active watches)"
        )

    else:
        summary["cron"] = "active"

    return summary
