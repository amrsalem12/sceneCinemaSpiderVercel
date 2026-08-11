"""
Cron sweep — the background half of the bot (Mode B).

The watcher checks each active watch and alerts only when a genuinely
NEW matching showtime appears.

Important watcher behavior:

1. A date becoming open for the first time is NOT an alert.
   Its currently published showtimes become the baseline.

2. A showtime added later to an already-open date IS an alert.

3. Experience/theatre filters are respected:
      IMAX + Gold + Standard
   means any of those three experiences can trigger the watch.

4. Existing watches created before the experience/date-baseline fields
   were introduced are migrated safely on their first sweep.

5. Once a watch has triggered, loud alerts continue on later cron runs
   until /booked or /remove, preserving the existing behavior.

6. VOX seats are availability flags, not counts.
"""

import os
import sys
import json
import time
from datetime import datetime, timedelta


from lib import store, telegram, vox, scene, cronjob  # noqa: E402


ALERT_REPEAT = int(
    os.getenv(
        "ALERT_REPEAT",
        "5",
    )
)

ALERT_INTERVAL = int(
    os.getenv(
        "ALERT_INTERVAL",
        "3",
    )
)

TZ_OFFSET = int(
    os.getenv(
        "TZ_OFFSET",
        "3",
    )
)

STATUS_EVERY = int(
    os.getenv(
        "STATUS_EVERY_SEC",
        "3600",
    )
)


def _local_now():
    return (
        datetime.utcnow()
        + timedelta(
            hours=TZ_OFFSET
        )
    )


# -------------------------------------------------------------------
# Generic helpers
# -------------------------------------------------------------------

def _experience_matches(session, experiences):
    """
    Empty experiences = any experience.

    Otherwise the session must have one of the selected experiences.
    """
    if not experiences:
        return True

    return (
        session.get("experience")
        in set(experiences)
    )


def _session_key(chain, session):
    """
    Stable identity of a showtime.

    We intentionally do NOT include seats/availability in this key.
    A seat availability change is not a new showtime.
    """
    if chain == "vox":
        return (
            f"vox:{session.get('id') or session.get('bookingUrl')}"
        )

    return (
        f"scene:{session.get('showtime_url')}"
    )


def _session_is_available(chain, session):
    """
    Whether this showtime is currently bookable.

    VOX:
      seats is a binary available/sold-out flag.

    Scene:
      sessions returned by Scene are considered bookable here;
      the actual seat map is separate and read-only.
    """
    if chain == "vox":
        return bool(
            session.get("seats")
        )

    return True


def _date_from_session(session):
    """
    Return a normalized YYYYMMDD date for a VOX session.

    Scene dates are handled separately because Scene's session
    objects do not expose the same displayDate field.
    """
    value = session.get(
        "displayDate"
    )

    if value is None:
        return None

    try:
        return int(value)
    except Exception:
        return None


def _save_watchlist(chat_id, wl):
    """
    Persist a modified watchlist.

    store.py intentionally exposes the generic _set used elsewhere
    by this project, so use the same storage layer rather than
    introducing another persistence API.
    """
    store._set(
        f"watchlist:{chat_id}",
        wl,
    )


# -------------------------------------------------------------------
# VOX / Scene session collection
# -------------------------------------------------------------------

def _vox_sessions(
    bundle,
    watch,
):
    """
    Return ALL VOX sessions matching:
      movie
      cinema
      date
      time filter

    Experience filtering happens afterward.
    Availability filtering also happens afterward.

    This is important because the watcher needs the complete
    published session set to establish a baseline.
    """

    slug = watch["movieSlug"]

    cinemas = watch.get(
        "cinemas",
        "any",
    )

    tf = watch.get(
        "timeFilter",
        "any",
    )

    want_date = watch.get(
        "date",
        "any",
    )

    if want_date == "any":
        return vox.sessions_for(
            bundle,
            movie_slug=slug,
            cinemas=cinemas,
            time_filter=tf,
            only_available=False,
        )

    dates = (
        want_date
        if isinstance(want_date, list)
        else [want_date]
    )

    out = []

    for d in dates:
        out.extend(
            vox.sessions_for(
                bundle,
                movie_slug=slug,
                cinemas=cinemas,
                display_date=d,
                time_filter=tf,
                only_available=False,
            )
        )

    return out


def _scene_open_dates(watch):
    """
    Return the Scene dates that are currently open for this watch.

    Date values are normalized to YYYYMMDD integers.
    """
    slug = watch["movieSlug"]

    open_days = sorted(
        scene.open_days(slug)
    )

    want_date = watch.get(
        "date",
        "any",
    )

    if want_date == "any":
        wanted = None

    else:
        wanted = set(
            want_date
            if isinstance(want_date, list)
            else [want_date]
        )

    out = []

    for ddmm in open_days:
        try:
            dd, mm, yyyy = ddmm.split("-")
            ymd = int(
                f"{yyyy}{mm}{dd}"
            )
        except Exception:
            continue

        if wanted is None or ymd in wanted:
            out.append(
                (ymd, ddmm)
            )

    return out


def _scene_sessions_for_date(
    watch,
    ddmm,
):
    """
    Return all Scene sessions for one open date.
    """
    return scene.sessions_for(
        watch["movieSlug"],
        ddmm,
        time_filter=watch.get(
            "timeFilter",
            "any",
        ),
    )


# -------------------------------------------------------------------
# Date/session state
# -------------------------------------------------------------------

def _watch_date_targets(
    bundle_cache,
    watch,
):
    """
    Return:

      {
        date_key: {
          "sessions": [...],
          "open": True
        }
      }

    Only dates currently open/published are returned.

    For VOX, a date is considered open when VOX has sessions.

    For Scene, the calendar strip is authoritative for whether the
    date itself is open.
    """

    chain = watch["chain"]

    result = {}

    if chain == "vox":
        bundle = bundle_cache.get(
            "vox"
        )

        if bundle is None:
            bundle = vox.fetch_bundle()
            bundle_cache["vox"] = bundle

        sessions = _vox_sessions(
            bundle,
            watch,
        )

        for session in sessions:
            d = _date_from_session(
                session
            )

            if d is None:
                continue

            result.setdefault(
                d,
                [],
            ).append(session)

        return result

    # Scene
    for ymd, ddmm in _scene_open_dates(
        watch
    ):
        try:
            sessions = _scene_sessions_for_date(
                watch,
                ddmm,
            )
        except Exception:
            sessions = []

        result[ymd] = sessions

    return result


def _matching_sessions(
    chain,
    sessions,
    experiences,
):
    """
    Apply experience filter to sessions.
    """
    return [
        s
        for s in sessions
        if _experience_matches(
            s,
            experiences,
        )
    ]


def _available_matching_sessions(
    chain,
    sessions,
    experiences,
):
    """
    Apply BOTH:
      experience filter
      availability filter
    """
    return [
        s
        for s in sessions
        if _experience_matches(
            s,
            experiences,
        )
        and _session_is_available(
            chain,
            s,
        )
    ]


# -------------------------------------------------------------------
# Core watcher
# -------------------------------------------------------------------

def check_watch(
    bundle_cache,
    watch,
):
    """
    Check one watch.

    Returns:
      (hits, state_changed)

    hits:
      currently bookable matching sessions that should be included
      in a loud alert.

    state_changed:
      whether seenSessions / initializedDates / alertedSessions
      were changed and therefore need to be persisted.
    """

    chain = watch["chain"]

    experiences = list(
        watch.get(
            "experiences",
            [],
        )
    )

    # ----------------------------------------------------------------
    # Migration for old watches
    # ----------------------------------------------------------------

    seen_sessions = set(
        watch.get(
            "seenSessions",
            [],
        )
    )

    initialized_dates = set(
        str(x)
        for x in watch.get(
            "initializedDates",
            [],
        )
    )

    alerted_sessions = set(
        watch.get(
            "alertedSessions",
            [],
        )
    )

    state_changed = False

    # ----------------------------------------------------------------
    # Get currently open dates + all sessions
    # ----------------------------------------------------------------

    date_targets = _watch_date_targets(
        bundle_cache,
        watch,
    )

    # No open date currently.
    if not date_targets:
        return [], state_changed

    new_session_hits = []

    # ----------------------------------------------------------------
    # Process each open date independently.
    #
    # This is the key fix for the "date just opened" bug.
    # ----------------------------------------------------------------

    for date_key, all_sessions in date_targets.items():

        date_key = str(
            date_key
        )

        current_keys = {
            _session_key(
                chain,
                s,
            )
            for s in all_sessions
        }

        # ------------------------------------------------------------
        # First time this date has ever been observed by this watch.
        #
        # IMPORTANT:
        # Do NOT alert.
        #
        # Everything currently published on this date becomes
        # baseline, regardless of experience selection.
        # ------------------------------------------------------------

        if date_key not in initialized_dates:
            initialized_dates.add(
                date_key
            )

            before = len(
                seen_sessions
            )

            seen_sessions.update(
                current_keys
            )

            if (
                len(seen_sessions)
                != before
            ):
                state_changed = True

            state_changed = True

            # Do not alert for anything on this first observation.
            continue

        # ------------------------------------------------------------
        # Date has already been initialized.
        #
        # Now detect genuinely NEW showtimes.
        # ------------------------------------------------------------

        for session in all_sessions:
            key = _session_key(
                chain,
                session,
            )

            if key in seen_sessions:
                continue

            # It is a genuinely new showtime.
            seen_sessions.add(
                key
            )

            state_changed = True

            # It must also satisfy the selected experience.
            if not _experience_matches(
                session,
                experiences,
            ):
                continue

            # It must currently be bookable.
            if not _session_is_available(
                chain,
                session,
            ):
                continue

            new_session_hits.append(
                session
            )

    # ----------------------------------------------------------------
    # Persist the complete current session set.
    #
    # This also makes the state resilient if a session disappears
    # temporarily from a cinema feed.
    # ----------------------------------------------------------------

    all_current_keys = set()

    for sessions in date_targets.values():
        all_current_keys.update(
            _session_key(
                chain,
                s,
            )
            for s in sessions
        )

    before_count = len(
        seen_sessions
    )

    seen_sessions.update(
        all_current_keys
    )

    if len(seen_sessions) != before_count:
        state_changed = True

    # ----------------------------------------------------------------
    # Save state back into the watch object.
    # ----------------------------------------------------------------

    watch["seenSessions"] = list(
        seen_sessions
    )

    watch["initializedDates"] = list(
        initialized_dates
    )

    # ----------------------------------------------------------------
    # New session(s) found.
    #
    # Store them as alert-triggering sessions.
    # ----------------------------------------------------------------

    if new_session_hits:
        for session in new_session_hits:
            alerted_sessions.add(
                _session_key(
                    chain,
                    session,
                )
            )

        state_changed = True

    watch["alertedSessions"] = list(
        alerted_sessions
    )

    # ----------------------------------------------------------------
    # Repeated alert behavior.
    #
    # Once a genuinely new matching showtime has triggered the watch,
    # keep sending loud alerts until the watch is removed/booked.
    #
    # We intentionally do NOT use the old "alerted" boolean as the
    # trigger because old watches may have been falsely alerted by the
    # previous buggy logic.
    # ----------------------------------------------------------------

    should_repeat = bool(
        alerted_sessions
    )

    if not should_repeat:
        return [], state_changed

    # Current matching/bookable sessions for the alert buttons.
    current_hits = []

    for sessions in date_targets.values():
        current_hits.extend(
            _available_matching_sessions(
                chain,
                sessions,
                experiences,
            )
        )

    # Deduplicate by showtime key.
    unique = []
    seen_hit_keys = set()

    for session in current_hits:
        key = _session_key(
            chain,
            session,
        )

        if key in seen_hit_keys:
            continue

        seen_hit_keys.add(
            key
        )

        unique.append(
            session
        )

    # If the watch triggered before but all current sessions are gone,
    # don't send an empty alert.
    if not unique:
        return [], state_changed

    return unique, state_changed


# -------------------------------------------------------------------
# Formatting
# -------------------------------------------------------------------

def _session_label(chain, session):
    """
    Convert a session to the label used in Telegram alert buttons.
    """

    if chain == "vox":
        cinema = session.get(
            "cinema",
            "VOX",
        )

        experience = session.get(
            "experience",
            "Standard",
        )

        time = session.get(
            "time",
            "",
        )

        return (
            f"{cinema[:16]} · "
            f"{experience} · "
            f"{time}"
        )

    experience = session.get(
        "experience",
        "Standard",
    )

    time = session.get(
        "time",
        "",
    )

    return (
        f"{experience} · "
        f"{time}"
    )


def _watch_summary(
    wl,
    open_ids,
):
    """
    Build the quiet hourly 'still watching' status text.
    """

    lines = [
        "👀 <b>Still watching</b> — hourly update"
    ]

    for i, w in enumerate(
        wl,
        1,
    ):
        cine = (
            "any"
            if w.get("cinemas") == "any"
            else ",".join(
                w.get(
                    "cinemas",
                    [],
                )
            )
        )

        dlabel = w.get(
            "dateLabel",
            "any date",
        )

        experiences = w.get(
            "experiences",
            [],
        )

        experience_label = (
            "any"
            if not experiences
            else " / ".join(
                experiences
            )
        )

        flag = (
            " — ⏰ OPEN NOW"
            if w["id"] in open_ids
            else ""
        )

        lines.append(
            f"{i}. {w['movieTitle']} "
            f"[{w['chain']}] @ {cine} "
            f"· {experience_label} "
            f"· {dlabel} "
            f"· {w.get('timeFilter', 'any')}"
            f"{flag}"
        )

    lines.append(
        "\n/list to manage · "
        "/booked &lt;n&gt; or /remove &lt;n&gt; to stop."
    )

    return "\n".join(
        lines
    )


# -------------------------------------------------------------------
# Hourly status
# -------------------------------------------------------------------

def _maybe_send_status(
    chat_id,
    wl,
    open_ids,
    summary,
):
    """
    Send the status at most once per the user's chosen interval.
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

    except (
        TypeError,
        ValueError,
    ):
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
    ).append(
        dbg
    )

    return (
        dbg.get("decision")
        == "SENT"
    )


# -------------------------------------------------------------------
# Main sweep
# -------------------------------------------------------------------

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

        watchlist_changed = False

        for w in wl:
            any_active = True

            summary["checked"] += 1

            try:
                hits, changed = check_watch(
                    bundle_cache,
                    w,
                )

                if changed:
                    watchlist_changed = True

            except Exception as e:
                summary["errors"].append(
                    f"{w.get('movieSlug')}: {e}"
                )

                continue

            if not hits:
                continue

            # --------------------------------------------------------
            # Loud repeated alert.
            # --------------------------------------------------------

            title = w[
                "movieTitle"
            ]

            buttons = [
                [
                    (
                        _session_label(
                            w["chain"],
                            h,
                        ),
                        (
                            h["bookingUrl"]
                            if w["chain"] == "vox"
                            else h["showtime_url"]
                        ),
                    )
                ]
                for h in hits
            ]

            telegram.alert_burst(
                chat_id,
                f"🎬🔔 <b>{title}</b> has new "
                f"matching showtimes!\n"
                f"Tap a showtime to book:",
                buttons=buttons,
                repeat=ALERT_REPEAT,
                interval=ALERT_INTERVAL,
            )

            # Keep the existing /list OPEN indicator.
            w["alerted"] = True

            watchlist_changed = True

            open_ids.add(
                w["id"]
            )

            summary["alerts"] += 1

        # Persist watcher state changes.
        if watchlist_changed:
            _save_watchlist(
                chat_id,
                wl,
            )

        # Quiet hourly heartbeat.
        if _maybe_send_status(
            chat_id,
            wl,
            open_ids,
            summary,
        ):
            summary["status_sent"] += 1

    # ---------------------------------------------------------------
    # Auto-disable cron if there are no active watches anywhere.
    # ---------------------------------------------------------------

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
