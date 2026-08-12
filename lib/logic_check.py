"""
Cron sweep — the background half of the bot (Mode B).

Each run:

  1. Loop every user's active watches.
  2. Fetch the current sessions.
  3. Filter by:
       - cinema
       - date
       - time filter
       - selected experience
       - availability
  4. Compare matching sessions against the sessions that existed when the
     watcher was created / last processed.
  5. Alert ONLY when a NEW matching session appears.
  6. Keep the watcher active after an alert.
  7. Send optional hourly status.
  8. Disable cron when there are no active watches.

IMPORTANT:

A movie having ANY showtime is NOT enough.

For example:

  Watch = The Odyssey
  Cinema = VOX Almaza
  Experience = IMAX
  Date = next 7 days

If Gold + Standard are open but IMAX is not:

    -> NO ALERT

If IMAX opens later:

    -> ALERT

Existing Gold/Standard sessions are ignored completely.
"""

import os
import time

from lib import store, telegram, vox, scene, cronjob  # noqa: E402


ALERT_REPEAT = int(
    os.getenv("ALERT_REPEAT", "5")
)

ALERT_INTERVAL = int(
    os.getenv("ALERT_INTERVAL", "3")
)

TZ_OFFSET = int(
    os.getenv("TZ_OFFSET", "3")
)

STATUS_EVERY = int(
    os.getenv("STATUS_EVERY_SEC", "3600")
)


# ---------------------------------------------------------------------------
# Experience matching
# ---------------------------------------------------------------------------

def _experience_matches(session_experience, wanted_experiences):
    """
    Return True only when the session's experience matches the watch.

    Supported watch values:

      "any"
      "IMAX"
      "Gold"
      "MAX"
      "4DX"
      "Kids"
      "Standard"

    Also accepts a list for future/multi-select support.
    """

    # Backwards compatibility for old watches created before experience
    # selection existed.
    if wanted_experiences is None:
        return True

    if wanted_experiences == "any":
        return True

    if isinstance(wanted_experiences, str):
        wanted = {wanted_experiences}
    else:
        wanted = set(wanted_experiences)

    if not wanted or "any" in wanted:
        return True

    return session_experience in wanted


# ---------------------------------------------------------------------------
# Session identity
# ---------------------------------------------------------------------------

def _session_key(chain, session):
    """
    Stable identity for a showtime.

    The identity is intentionally independent of experience filtering.
    The session itself is unique, so an IMAX session and a Gold session are
    separate session IDs/URLs.
    """

    if chain == "vox":
        return (
            f"vox:"
            f"{session.get('id') or session.get('bookingUrl')}"
        )

    return (
        f"scene:"
        f"{session.get('showtime_url')}"
    )


# ---------------------------------------------------------------------------
# Watch's selected experience
# ---------------------------------------------------------------------------

def _watch_experiences(watch):
    """
    Read the new field while remaining compatible with older watches.
    """

    if "experiences" in watch:
        return watch["experiences"]

    # Compatibility with an intermediate version if it existed.
    if "experience" in watch:
        return watch["experience"]

    # Old watches had no experience restriction.
    return "any"


# ---------------------------------------------------------------------------
# Date matching
# ---------------------------------------------------------------------------

def _date_set(watch):
    """
    Convert the watch's date value into a set.

    "any" -> None
    int/string YYYYMMDD -> set containing that date
    list -> set of dates
    """

    want_date = watch.get(
        "date",
        "any",
    )

    if want_date == "any":
        return None

    if isinstance(want_date, list):
        return {
            str(d)
            for d in want_date
        }

    return {
        str(want_date)
    }


# ---------------------------------------------------------------------------
# Watch session lookup
# ---------------------------------------------------------------------------

def _matching_sessions(bundle_cache, watch):
    """
    Return ONLY currently available sessions matching the complete watcher.

    Filters:

      chain
      movie
      cinema
      date
      time
      experience
      seats available
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

    experiences = _watch_experiences(watch)
    dates = _date_set(watch)

    # -----------------------------------------------------------------------
    # VOX
    # -----------------------------------------------------------------------
    if chain == "vox":
        bundle = bundle_cache.get("vox")

        if bundle is None:
            bundle = vox.fetch_bundle()
            bundle_cache["vox"] = bundle

        sessions = []

        if dates is None:
            candidates = vox.sessions_for(
                bundle,
                movie_slug=slug,
                cinemas=cinemas,
                time_filter=time_filter,
                only_available=True,
            )

            sessions.extend(candidates)

        else:
            for date_value in dates:
                try:
                    display_date = int(date_value)
                except Exception:
                    continue

                candidates = vox.sessions_for(
                    bundle,
                    movie_slug=slug,
                    cinemas=cinemas,
                    display_date=display_date,
                    time_filter=time_filter,
                    only_available=True,
                )

                sessions.extend(candidates)

        # ---------------------------------------------------------------
        # CRITICAL FILTER:
        #
        # A Gold/Standard session must NOT satisfy an IMAX watcher.
        # ---------------------------------------------------------------
        sessions = [
            s
            for s in sessions
            if _experience_matches(
                s.get("experience"),
                experiences,
            )
        ]

        sessions.sort(
            key=lambda x: (
                str(x.get("displayDate")),
                str(x.get("showtime")),
            )
        )

        return sessions

    # -----------------------------------------------------------------------
    # Scene
    # -----------------------------------------------------------------------

    try:
        if not scene.is_bookable(slug):
            return []

        open_days = sorted(
            scene.open_days(slug)
        )

        if not open_days:
            return []

        if dates is None:
            target_days = open_days

        else:
            target_days = [
                d
                for d in open_days
                if scene.to_ddmmyyyy(d)
                in {
                    scene.to_ddmmyyyy(int(x))
                    for x in dates
                }
            ]

        sessions = []

        for d in target_days:
            candidates = scene.sessions_for(
                slug,
                d,
                time_filter=time_filter,
            )

            # Scene currently doesn't expose a seats field in the same way
            # as VOX. Availability is therefore based on the showtime being
            # present in Scene's open-day showtime data.
            sessions.extend(candidates)

        # ---------------------------------------------------------------
        # Scene experience filtering.
        #
        # ScreenX / Premiere / Standard are treated exactly like the VOX
        # experiences above.
        # ---------------------------------------------------------------
        sessions = [
            s
            for s in sessions
            if _experience_matches(
                s.get("experience"),
                experiences,
            )
        ]

        return sessions

    except Exception:
        raise


# ---------------------------------------------------------------------------
# Format alert button
# ---------------------------------------------------------------------------

def _session_label(chain, session):
    if chain == "vox":
        date_value = session.get(
            "displayDate"
        )

        if date_value:
            date_value = str(date_value)

            if len(date_value) == 8:
                date_label = (
                    f"{date_value[6:8]}/"
                    f"{date_value[4:6]}"
                )
            else:
                date_label = date_value

        else:
            date_label = ""

        base = (
            f"{session.get('cinema', '')[:16]}"
            f" · {session.get('experience', '')}"
            f" · {session.get('time', '')}"
        )

        if date_label:
            base += f" · {date_label}"

        return base

    return (
        f"{session.get('cinema', 'Scene CFC')}"
        f" · {session.get('experience', '')}"
        f" · {session.get('time', '')}"
    )


# ---------------------------------------------------------------------------
# Watch summary
# ---------------------------------------------------------------------------

def _watch_summary(wl, open_ids):
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

        experiences = _watch_experiences(w)

        if experiences == "any":
            experience_label = "any experience"
        elif isinstance(experiences, list):
            experience_label = ", ".join(experiences)
        else:
            experience_label = str(experiences)

        flag = (
            " — ⏰ OPEN NOW"
            if w["id"] in open_ids
            else ""
        )

        lines.append(
            f"{i}. {w['movieTitle']} [{w['chain']}] "
            f"@ {cine} · 🎭 {experience_label} "
            f"· {dlabel} · {w['timeFilter']}{flag}"
        )

    lines.append(
        "\n/list to manage · "
        "/booked &lt;n&gt; or /remove &lt;n&gt; to stop."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def _maybe_send_status(
    chat_id,
    wl,
    open_ids,
    summary,
):
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
# Main sweep
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

        for watch in wl:
            any_active = True

            summary["checked"] += 1

            try:
                current_sessions = _matching_sessions(
                    bundle_cache,
                    watch,
                )

            except Exception as e:
                summary["errors"].append(
                    f"{watch.get('movieSlug')}: {e}"
                )

                continue

            # ---------------------------------------------------------------
            # Existing sessions snapshot.
            #
            # Older watches may not have this field. In that case we create
            # the snapshot now rather than alerting on every existing showtime.
            # ---------------------------------------------------------------
            has_baseline = "seenSessions" in watch
            old_seen = watch.get(
                "seenSessions"
            )

            if old_seen is None:
                old_seen = []

            old_seen = set(old_seen)

            # ---------------------------------------------------------------
            # Migration safety for watches created by older versions.
            #
            # An old watch may have no seenSessions field at all. We MUST NOT
            # alert on every currently-open matching showtime in that case.
            # Instead, the first cron run establishes the baseline, exactly
            # like the current webhook does when the user chooses IGNORE.
            # Future matching sessions can then be detected normally.
            # ---------------------------------------------------------------
            if not has_baseline:
                current_keys = {
                    _session_key(
                        watch["chain"],
                        session,
                    )
                    for session in current_sessions
                }

                watch["seenSessions"] = list(current_keys)

                current_wl = store.get_watchlist(
                    chat_id
                )

                for stored_watch in current_wl:
                    if stored_watch.get("id") == watch.get("id"):
                        stored_watch["seenSessions"] = list(current_keys)

                store._set(
                    f"watchlist:{chat_id}",
                    current_wl,
                )

                continue

            # ---------------------------------------------------------------
            # Find ONLY newly appearing matching sessions.
            #
            # This is the key behavior:
            #
            # Existing Gold session + new IMAX watcher:
            #   Gold is ignored.
            #
            # Existing IMAX session at watcher creation:
            #   already in seenSessions -> ignored.
            #
            # Later new IMAX session:
            #   not in seenSessions -> alert.
            # ---------------------------------------------------------------
            new_sessions = []

            current_keys = set()

            for session in current_sessions:
                key = _session_key(
                    watch["chain"],
                    session,
                )

                current_keys.add(key)

                if key not in old_seen:
                    new_sessions.append(
                        session
                    )

            # ---------------------------------------------------------------
            # Update seenSessions.
            #
            # We keep the current matching sessions as the baseline.
            # This prevents the same session from alerting every cron run.
            # ---------------------------------------------------------------
            watch["seenSessions"] = list(
                current_keys
            )

            if new_sessions:
                title = watch[
                    "movieTitle"
                ]

                experiences = _watch_experiences(
                    watch
                )

                if experiences == "any":
                    experience_label = (
                        "any experience"
                    )

                elif isinstance(
                    experiences,
                    list,
                ):
                    experience_label = ", ".join(
                        experiences
                    )

                else:
                    experience_label = str(
                        experiences
                    )

                buttons = []

                for session in new_sessions[:10]:
                    if watch["chain"] == "vox":
                        url = session.get(
                            "bookingUrl"
                        )

                    else:
                        url = session.get(
                            "showtime_url"
                        )

                    if not url:
                        continue

                    buttons.append([
                        (
                            _session_label(
                                watch["chain"],
                                session,
                            ),
                            url,
                        )
                    ])

                if buttons:
                    telegram.alert_burst(
                        chat_id,
                        (
                            f"🎬🔔 <b>{title}</b> "
                            f"has a NEW matching showtime!\n"
                            f"🎭 Experience: "
                            f"<b>{experience_label}</b>\n"
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

            # ---------------------------------------------------------------
            # Persist seen-session changes.
            #
            # set_alerted() also reloads and saves the watchlist, but if there
            # was no alert we still need to save the updated seenSessions.
            # ---------------------------------------------------------------
            current_wl = store.get_watchlist(
                chat_id
            )

            for stored_watch in current_wl:
                if (
                    stored_watch.get("id")
                    == watch.get("id")
                ):
                    stored_watch[
                        "seenSessions"
                    ] = list(current_keys)

            store._set(
                f"watchlist:{chat_id}",
                current_wl,
            )

        # ---------------------------------------------------------------
        # Optional heartbeat
        # ---------------------------------------------------------------
        if _maybe_send_status(
            chat_id,
            store.get_watchlist(chat_id),
            open_ids,
            summary,
        ):
            summary["status_sent"] += 1

    # -----------------------------------------------------------------------
    # Disable cron if no watches remain.
    # -----------------------------------------------------------------------

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
