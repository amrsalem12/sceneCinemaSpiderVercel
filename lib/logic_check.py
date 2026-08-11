"""
Cron sweep — the background half of the bot (Mode B).

Each run:
  1. Loop every user's active watches.
  2. Find sessions matching ALL watcher filters:
       - chain
       - movie
       - cinema
       - date
       - time filter
       - experience
  3. Compare those sessions with the watcher's seenSessions snapshot.
  4. Only NEW matching sessions trigger an alert.
  5. Existing sessions are never alerted just because the cron ran.
  6. Keep alerting new/open matching sessions until /booked or /remove.
  7. Send the optional heartbeat status.
  8. Disable cron when there are no active watches.
"""

import os
import time

from lib import store, telegram, vox, scene, cronjob


ALERT_REPEAT = int(os.getenv("ALERT_REPEAT", "5"))
ALERT_INTERVAL = int(os.getenv("ALERT_INTERVAL", "3"))
STATUS_EVERY = int(os.getenv("STATUS_EVERY_SEC", "3600"))


# ---------------------------------------------------------------------------
# Experience helpers
# ---------------------------------------------------------------------------

def _normalise_experience(value):
    """
    Convert an experience value into a stable lowercase representation.

    Watchers store one of:
      "any"
      VOX codes: gd, imx, mx, fx, kd, st
      Scene names/codes: imax, vip, stand

    We also accept friendly names so older/newer watcher data remains usable.
    """
    if value is None:
        return "any"

    value = str(value).strip().lower()

    aliases = {
        "any": "any",
        "all": "any",

        # VOX
        "gold": "gd",
        "gd": "gd",

        "imax": "imx",
        "imx": "imx",

        "max": "mx",
        "mx": "mx",

        "4dx": "fx",
        "fx": "fx",

        "kids": "kd",
        "kid": "kd",
        "kd": "kd",

        "standard": "st",
        "std": "st",
        "st": "st",

        # Scene
        "screenx": "imax",
        "screen x": "imax",

        "premiere": "vip",
        "vip": "vip",

        "stand": "stand",
    }

    return aliases.get(value, value)


def _session_experience(chain, session):
    """
    Return a stable experience identifier for a session.

    VOX sessions contain the raw experience code and the friendly name.

    Scene sessions currently expose the friendly name, so map that back to
    the same stable identifier used by watcher filters.
    """
    if chain == "vox":
        raw = session.get("experienceCode")

        if raw:
            return _normalise_experience(raw)

        raw = session.get("experience")

        return _normalise_experience(raw)

    # Scene
    raw = session.get("experience")

    return _normalise_experience(raw)


def _experience_matches(chain, session, wanted):
    """
    Return True when a session belongs to the watcher's requested experience.

    "any" accepts every experience.
    """
    wanted = _normalise_experience(wanted)

    if wanted == "any":
        return True

    actual = _session_experience(chain, session)

    return actual == wanted


# ---------------------------------------------------------------------------
# Session identity
# ---------------------------------------------------------------------------

def _session_key(chain, session):
    """
    Stable identity for a showtime.

    This is intentionally based on the cinema's actual session/showtime
    identifier rather than its experience/time text.
    """
    if chain == "vox":
        return f"vox:{session.get('id') or session.get('bookingUrl')}"

    return f"scene:{session.get('showtime_url')}"


# ---------------------------------------------------------------------------
# Watch matching
# ---------------------------------------------------------------------------

def _date_set(watch):
    """
    Return the watcher's requested dates.

    None means "any date".
    """
    want_date = watch.get("date", "any")

    if want_date == "any" or want_date is None:
        return None

    if isinstance(want_date, list):
        return {int(x) for x in want_date}

    return {int(want_date)}


def _get_matching_sessions(bundle_cache, watch):
    """
    Fetch all currently available sessions matching the watcher's filters.

    IMPORTANT:
    Experience filtering happens BEFORE the caller decides whether a session
    is new. This prevents a Gold/Standard session from triggering an IMAX
    watcher, etc.
    """
    chain = watch["chain"]
    slug = watch["movieSlug"]

    cinemas = watch.get("cinemas", "any")
    time_filter = watch.get("timeFilter", "any")
    experience = watch.get(
        "experience",
        watch.get("experienceFilter", "any"),
    )

    dates = _date_set(watch)

    sessions = []

    if chain == "vox":
        bundle = bundle_cache.get("vox")

        if bundle is None:
            bundle = vox.fetch_bundle()
            bundle_cache["vox"] = bundle

        if dates is None:
            sessions = vox.sessions_for(
                bundle,
                movie_slug=slug,
                cinemas=cinemas,
                time_filter=time_filter,
                only_available=True,
            )

        else:
            for display_date in sorted(dates):
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

    elif chain == "scene":
        # Scene is date-driven. Only query dates that the watcher cares about.
        open_days = sorted(scene.open_days(slug))

        if dates is None:
            target_days = open_days
        else:
            wanted_ddmm = {
                scene.to_ddmmyyyy(d)
                for d in dates
            }

            target_days = [
                d
                for d in open_days
                if d in wanted_ddmm
            ]

        for day in target_days:
            sessions.extend(
                scene.sessions_for(
                    slug,
                    day,
                    time_filter=time_filter,
                )
            )

    else:
        return []

    # ---------------------------------------------------------------
    # Experience filter
    # ---------------------------------------------------------------

    matching = []

    for session in sessions:
        if _experience_matches(
            chain,
            session,
            experience,
        ):
            matching.append(session)

    return matching


def _format_session(chain, session):
    """
    Convert a matching session into the button label + booking URL.
    """
    if chain == "vox":
        cinema = session.get("cinema", "VOX")
        experience = session.get("experience", "Standard")
        time = session.get("time", "?")

        label = (
            f"{cinema[:16]} · "
            f"{experience} · "
            f"{time}"
        )

        return label, session.get("bookingUrl")

    experience = session.get(
        "experience",
        "Standard",
    )

    time = session.get(
        "time",
        "?",
    )

    day = session.get(
        "displayDate",
        "",
    )

    if day:
        label = f"{experience} · {time} ({day})"
    else:
        label = f"{experience} · {time}"

    return label, session.get("showtime_url")


# ---------------------------------------------------------------------------
# Check one watcher
# ---------------------------------------------------------------------------

def check_watch(bundle_cache, watch):
    """
    Return only NEW, currently-bookable sessions matching the watcher.

    Existing sessions are removed using watch["seenSessions"].

    This is the important distinction:

        currently available != newly opened

    A watcher should only alert for the second case.
    """
    current = _get_matching_sessions(
        bundle_cache,
        watch,
    )

    if not current:
        return []

    seen = set(
        watch.get("seenSessions", [])
    )

    new_sessions = []

    for session in current:
        key = _session_key(
            watch["chain"],
            session,
        )

        if key not in seen:
            new_sessions.append(
                session
            )

    return [
        _format_session(
            watch["chain"],
            session,
        )
        for session in new_sessions
    ]


# ---------------------------------------------------------------------------
# Watch summary
# ---------------------------------------------------------------------------

def _watch_summary(wl, open_ids):
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

        experience = watch.get(
            "experience",
            watch.get(
                "experienceFilter",
                "any",
            ),
        )

        flag = (
            " — ⏰ NEW MATCH"
            if watch["id"] in open_ids
            else ""
        )

        lines.append(
            f"{i}. {watch['movieTitle']} "
            f"[{watch['chain']}] "
            f"@ {cine} "
            f"· {experience} "
            f"· {date_label} "
            f"· {watch.get('timeFilter', 'any')}"
            f"{flag}"
        )

    lines.append(
        "\n/list to manage · "
        "/booked &lt;n&gt; or /remove &lt;n&gt; to stop."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Optional heartbeat
# ---------------------------------------------------------------------------

def _maybe_send_status(
    chat_id,
    wl,
    open_ids,
    summary,
):
    """
    Send the status at most once per the user's configured interval.
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
                wl,
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
# Main cron sweep
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
                hits = check_watch(
                    bundle_cache,
                    watch,
                )

            except Exception as exc:
                summary["errors"].append(
                    f"{watch.get('movieSlug')}: {exc}"
                )
                continue

            if not hits:
                continue

            # -------------------------------------------------------
            # NEW MATCH
            # -------------------------------------------------------

            title = watch["movieTitle"]

            buttons = [
                [hit]
                for hit in hits
            ]

            experience = watch.get(
                "experience",
                watch.get(
                    "experienceFilter",
                    "any",
                ),
            )

            telegram.alert_burst(
                chat_id,
                (
                    f"🎬🔔 <b>{title}</b> "
                    f"has a NEW "
                    f"<b>{experience}</b> "
                    f"showtime!\n"
                    f"Tap a showtime to book:"
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

            # -------------------------------------------------------
            # IMPORTANT:
            #
            # Add the sessions we just detected to seenSessions.
            #
            # This prevents the exact same new showtime from being
            # reported as "new" on every cron run.
            # -------------------------------------------------------

            existing_seen = set(
                watch.get(
                    "seenSessions",
                    [],
                )
            )

            # We only have formatted hits here, so retrieve the
            # matching sessions again to get their stable IDs.
            try:
                current_matching = _get_matching_sessions(
                    bundle_cache,
                    watch,
                )

                for session in current_matching:
                    key = _session_key(
                        watch["chain"],
                        session,
                    )

                    existing_seen.add(key)

                watch["seenSessions"] = list(
                    existing_seen
                )

                # Persist the updated snapshot.
                store._set(
                    f"watchlist:{chat_id}",
                    watchlist,
                )

            except Exception as exc:
                summary["errors"].append(
                    f"seenSessions {watch.get('movieSlug')}: {exc}"
                )

        if _maybe_send_status(
            chat_id,
            watchlist,
            open_ids,
            summary,
        ):
            summary["status_sent"] += 1

    # -------------------------------------------------------------------
    # Disable cron when no watches remain.
    # -------------------------------------------------------------------

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
