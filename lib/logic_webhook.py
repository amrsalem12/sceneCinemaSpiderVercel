"""
Telegram webhook — the interactive surface of the bot.

MODE A
  /showing
      -> browse currently showing movies
      -> choose a day
      -> choose a showtime
      -> deep-link to booking

MODE B
  /upcoming
      -> browse coming-soon movies
      -> choose cinema
      -> choose time filter
      -> choose date
      -> create a watcher

Watcher behavior
  When a watch is created, all currently available matching showtimes are
  recorded as the baseline.

  The cron watcher will NOT alert for those existing showtimes.

  If a new showtime appears later, cron detects it and alerts the user.

  Newly discovered showtimes remain in alertSessions so repeated cron runs
  can continue alerting until /booked or /remove.

Manage
  /list
  /remove <n>
  /booked <n>
  /status

Access
  JOIN_CODE can be used to restrict access.

Conversation state
  Stored in KV through store.get_convo()/set_convo().
"""

import os
import re

from lib import (
    store,
    telegram,
    vox,
    scene,
    scene_seats,
    cronjob,
)


JOIN_CODE = os.getenv(
    "JOIN_CODE",
    "",
).strip()


# ---------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------

CINEMA_CHOICES = [
    (
        "VOX Almaza",
        "vox:000047",
    ),
    (
        "Scene CFC",
        "scene:cfc",
    ),
    (
        "Any (Almaza or Scene)",
        "any:any",
    ),
]


TIME_CHOICES = [
    (
        "After 5pm",
        "after5",
    ),
    (
        "Any time",
        "any",
    ),
    (
        "First showtime",
        "first",
    ),
]


DATE_CHOICES = [
    (
        "Any date",
        "any",
    ),
    (
        "Today",
        "today",
    ),
    (
        "Tomorrow",
        "tomorrow",
    ),
    (
        "This Friday",
        "friday",
    ),
    (
        "This weekend",
        "weekend",
    ),
    (
        "Within 7 days",
        "week",
    ),
]


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

def is_member(user_id):
    return str(user_id) in set(
        store._get(
            "allowlist",
            [],
        )
    )


def add_member(user_id):
    ids = set(
        store._get(
            "allowlist",
            [],
        )
    )

    ids.add(
        str(user_id)
    )

    store._set(
        "allowlist",
        list(ids),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def handle_update(update):
    ev = telegram.parse_update(
        update
    )

    if not ev:
        return

    chat_id = ev["chat_id"]
    user_id = ev["user_id"]

    # -----------------------------------------------------------------------
    # Access gate
    # -----------------------------------------------------------------------

    if not is_member(user_id):
        if (
            ev["kind"] == "message"
            and JOIN_CODE
            and ev["text"] == JOIN_CODE
        ):
            add_member(
                user_id
            )

            telegram.send_message(
                chat_id,
                "✅ You're in! Try /showing or /upcoming.",
            )

        else:
            telegram.send_message(
                chat_id,
                "🔒 This bot is private. Send the access code to join.",
            )

        return

    # -----------------------------------------------------------------------
    # Callback
    # -----------------------------------------------------------------------

    if ev["kind"] == "callback":
        toast = (
            "Sold out"
            if ev["data"] == "soldout"
            else None
        )

        telegram.answer_callback(
            ev["callback_id"],
            text=toast,
        )

        return handle_callback(
            chat_id,
            ev["data"],
        )

    # -----------------------------------------------------------------------
    # Commands
    # -----------------------------------------------------------------------

    text = ev["text"]

    if text.startswith("/start"):
        telegram.send_message(
            chat_id,
            "🎬 Cinema bot.\n\n"
            "/showing — what's on now (book)\n"
            "/upcoming — watch a coming-soon movie\n"
            "/list — your watches\n"
            "/status — how often I ping what I'm watching\n"
            "/remove &lt;n&gt; — stop a watch\n"
            "/booked &lt;n&gt; — mark booked, stop alerts",
        )

    elif text.startswith("/showing"):
        cmd_showing(
            chat_id
        )

    elif text.startswith("/upcoming"):
        cmd_upcoming(
            chat_id
        )

    elif text.startswith("/list"):
        cmd_list(
            chat_id
        )

    elif text.startswith("/status"):
        cmd_status(
            chat_id
        )

    elif text.startswith("/remove"):
        cmd_stop(
            chat_id,
            text,
            booked=False,
        )

    elif text.startswith("/booked"):
        cmd_stop(
            chat_id,
            text,
            booked=True,
        )

    else:
        telegram.send_message(
            chat_id,
            "Try /showing or /upcoming.",
        )


# ---------------------------------------------------------------------------
# MODE A — now showing
# ---------------------------------------------------------------------------

def cmd_showing(chat_id):
    telegram.send_message(
        chat_id,
        "Fetching what's on…",
    )

    rows = []

    # VOX
    try:
        bundle = vox.fetch_bundle()

        for movie in vox.now_showing(
            bundle
        ):
            rows.append([
                (
                    f"🎬 {movie['title'][:40]}",
                    f"show:vox:{movie['slug']}",
                )
            ])

    except Exception as exc:
        telegram.send_message(
            chat_id,
            f"(VOX unavailable: {exc})",
        )

    # Scene
    try:
        for movie in scene.now_showing()[:15]:
            rows.append([
                (
                    f"🎬 {movie['title'][:40]} (Scene)",
                    f"show:scene:{movie['slug']}",
                )
            ])

    except Exception:
        pass

    if not rows:
        telegram.send_message(
            chat_id,
            "Couldn't load movies right now.",
        )
        return

    telegram.send_message(
        chat_id,
        "Now showing — pick a movie:",
        buttons=rows,
    )


# ---------------------------------------------------------------------------
# MODE B — coming soon
# ---------------------------------------------------------------------------

def cmd_upcoming(chat_id):
    telegram.send_message(
        chat_id,
        "Fetching coming soon…",
    )

    rows = []

    # VOX
    try:
        bundle = vox.fetch_bundle()

        for movie in vox.coming_soon(
            bundle
        ):
            rows.append([
                (
                    f"🔜 {movie['title'][:40]}",
                    f"mark:vox:{movie['slug']}",
                )
            ])

    except Exception as exc:
        telegram.send_message(
            chat_id,
            f"(VOX unavailable: {exc})",
        )

    # Scene
    try:
        for movie in scene.coming_soon()[:15]:
            rows.append([
                (
                    f"🔜 {movie['title'][:40]} (Scene)",
                    f"mark:scene:{movie['slug']}",
                )
            ])

    except Exception:
        pass

    if not rows:
        telegram.send_message(
            chat_id,
            "No coming-soon movies found.",
        )
        return

    telegram.send_message(
        chat_id,
        "Coming soon — tap one to watch it:",
        buttons=rows,
    )


# ---------------------------------------------------------------------------
# Callback router
# ---------------------------------------------------------------------------

def handle_callback(chat_id, data):
    parts = data.split(":")
    action = parts[0]

    # -----------------------------------------------------------------------
    # Show a movie -> day picker
    # -----------------------------------------------------------------------

    if action == "show":
        return show_showtimes(
            chat_id,
            parts[1],
            parts[2],
        )

    # -----------------------------------------------------------------------
    # Pick day -> showtimes
    # -----------------------------------------------------------------------

    if action == "day":
        return show_day_showtimes(
            chat_id,
            parts[1],
            parts[2],
            parts[3],
        )

    # -----------------------------------------------------------------------
    # Status interval
    # -----------------------------------------------------------------------

    if action == "statusiv":
        return set_status_interval(
            chat_id,
            int(parts[1]),
        )

    # -----------------------------------------------------------------------
    # Scene seat map
    # -----------------------------------------------------------------------

    if action == "seatmap":
        return show_seatmap(
            chat_id,
            ":".join(parts[1:]),
        )

    if action == "soldout":
        return

    # -----------------------------------------------------------------------
    # Start watch
    # -----------------------------------------------------------------------

    if action == "mark":
        store.set_convo(
            chat_id,
            {
                "chain": parts[1],
                "slug": parts[2],
                "step": "cinema",
            },
        )

        return telegram.send_message(
            chat_id,
            "Which cinema?",
            buttons=[
                [
                    (
                        name,
                        f"mc:{value}",
                    )
                ]
                for name, value in CINEMA_CHOICES
            ],
        )

    # -----------------------------------------------------------------------
    # Cinema
    # -----------------------------------------------------------------------

    if action == "mc":
        convo = store.get_convo(
            chat_id
        )

        convo["cinemaChoice"] = ":".join(
            parts[1:]
        )

        convo["step"] = "time"

        store.set_convo(
            chat_id,
            convo,
        )

        return telegram.send_message(
            chat_id,
            "Which showtimes?",
            buttons=[
                [
                    (
                        name,
                        f"mt:{value}",
                    )
                ]
                for name, value in TIME_CHOICES
            ],
        )

    # -----------------------------------------------------------------------
    # Time filter
    # -----------------------------------------------------------------------

    if action == "mt":
        convo = store.get_convo(
            chat_id
        )

        convo["timeFilter"] = parts[1]
        convo["step"] = "date"

        store.set_convo(
            chat_id,
            convo,
        )

        return telegram.send_message(
            chat_id,
            "Which date?",
            buttons=[
                [
                    (
                        name,
                        f"md:{value}",
                    )
                ]
                for name, value in DATE_CHOICES
            ],
        )

    # -----------------------------------------------------------------------
    # Date
    # -----------------------------------------------------------------------

    if action == "md":
        return save_watch(
            chat_id,
            parts[1],
        )

    return telegram.send_message(
        chat_id,
        f"(unhandled tap: {data!r})",
    )


# ---------------------------------------------------------------------------
# Date formatting
# ---------------------------------------------------------------------------

def _daylabel(yyyymmdd):
    """
    Convert:
        20260810

    to:
        Mon 10/08
    """

    from datetime import datetime

    try:
        return datetime.strptime(
            str(yyyymmdd),
            "%Y%m%d",
        ).strftime(
            "%a %d/%m"
        )

    except Exception:
        return str(yyyymmdd)


# ---------------------------------------------------------------------------
# Showtimes — day picker
# ---------------------------------------------------------------------------

def show_showtimes(chat_id, chain, slug):
    """
    After a movie is picked, show only days that currently have showtimes.

    The "Watch for another date" option enters the watcher flow.
    """

    telegram.send_message(
        chat_id,
        "Checking available days…",
    )

    try:
        days = []

        if chain == "vox":
            bundle = vox.fetch_bundle()

            movie = vox.find_movie(
                bundle,
                slug=slug,
            )

            name = (
                movie["title"]
                if movie
                else slug.replace(
                    "-",
                    " ",
                ).title()
            )

            cinema = "VOX Almaza"

            seen = set()

            sessions = vox.sessions_for(
                bundle,
                movie_slug=slug,
                time_filter="any",
                only_available=False,
            )

            for session in sorted(
                sessions,
                key=lambda x: x["displayDate"],
            ):
                display_date = str(
                    session["displayDate"]
                )

                if display_date not in seen:
                    seen.add(
                        display_date
                    )
                    days.append(
                        display_date
                    )

        else:
            name = slug.replace(
                "-",
                " ",
            ).title()

            cinema = "Scene CFC"

            for ddmm in scene.open_days(
                slug
            ):
                dd, mm, yyyy = ddmm.split(
                    "-"
                )

                days.append(
                    f"{yyyy}{mm}{dd}"
                )

            days.sort()

        if not days:
            return telegram.send_message(
                chat_id,
                f"🎬 <b>{name}</b> — {cinema}\n"
                f"No showtimes yet. Use /upcoming "
                f"to be pinged when they open.",
            )

        rows = [
            [
                (
                    _daylabel(day),
                    f"day:{chain}:{slug}:{day}",
                )
            ]
            for day in days[:10]
        ]

        rows.append([
            (
                "⏰ Watch for another date",
                f"mark:{chain}:{slug}",
            )
        ])

        return telegram.send_message(
            chat_id,
            f"🎬 <b>{name}</b> — {cinema}\n"
            f"Which day do you want to go?",
            buttons=rows,
        )

    except Exception as exc:
        return telegram.send_message(
            chat_id,
            f"Couldn't load days: {exc}",
        )


# ---------------------------------------------------------------------------
# Showtimes — selected day
# ---------------------------------------------------------------------------

def show_day_showtimes(
    chat_id,
    chain,
    slug,
    yyyymmdd,
):
    """
    Show one day's showtimes.

    VOX:
      Available -> booking button.
      Sold out -> disabled-looking callback.

    Scene:
      Seat-map button + booking button.
    """

    telegram.send_message(
        chat_id,
        "Loading showtimes…",
    )

    day_label = _daylabel(
        yyyymmdd
    )

    try:
        # -------------------------------------------------------------------
        # VOX
        # -------------------------------------------------------------------

        if chain == "vox":
            bundle = vox.fetch_bundle()

            movie = vox.find_movie(
                bundle,
                slug=slug,
            )

            name = (
                movie["title"]
                if movie
                else slug.replace(
                    "-",
                    " ",
                ).title()
            )

            sessions = vox.sessions_for(
                bundle,
                movie_slug=slug,
                display_date=int(
                    yyyymmdd
                ),
                time_filter="any",
                only_available=False,
            )

            if not sessions:
                return telegram.send_message(
                    chat_id,
                    f"🎬 <b>{name}</b> — "
                    f"VOX Almaza · {day_label}\n"
                    f"No showtimes for that day.",
                )

            rows = []

            for session in sorted(
                sessions,
                key=lambda x: x["showtime"],
            ):
                free = (
                    session["seats"]
                    and session["seats"] > 0
                )

                experience = session[
                    "experience"
                ]

                if free:
                    rows.append([
                        (
                            f"{session['time']} · "
                            f"{experience}",
                            session["bookingUrl"],
                        )
                    ])

                else:
                    rows.append([
                        (
                            f"🔴 {session['time']} · "
                            f"{experience} — sold out",
                            "soldout",
                        )
                    ])

            return telegram.send_message(
                chat_id,
                f"🎬 <b>{name}</b> — "
                f"VOX Almaza · {day_label}\n"
                f"Tap a time to book:",
                buttons=rows,
            )

        # -------------------------------------------------------------------
        # Scene
        # -------------------------------------------------------------------

        name = slug.replace(
            "-",
            " ",
        ).title()

        ddmm = scene.to_ddmmyyyy(
            int(yyyymmdd)
        )

        sessions = scene.sessions_for(
            slug,
            ddmm,
            time_filter="any",
        )

        if not sessions:
            return telegram.send_message(
                chat_id,
                f"🎬 <b>{name}</b> — "
                f"Scene CFC · {day_label}\n"
                f"No showtimes for that day.",
            )

        rows = []

        for session in sessions[:10]:
            raw = (
                session["showtime_url"]
                .split("?")[0]
                .rstrip("/")
            )

            match = re.search(
                r"(?:showtime|booking)-([0-9a-f]{24})",
                raw,
            )

            showtime_id = (
                match.group(1)
                if match
                else None
            )

            seat_button = (
                (
                    f"🗺 {session['time']} · "
                    f"{session['experience']}",
                    (
                        f"seatmap:{showtime_id}"
                        if showtime_id
                        else "seatmap:BADID"
                    ),
                )
            )

            rows.append([
                seat_button,
                (
                    "Book",
                    session["showtime_url"],
                ),
            ])

        return telegram.send_message(
            chat_id,
            f"🎬 <b>{name}</b> — "
            f"Scene CFC · {day_label}\n"
            f"🗺 = see seats · Book = go to Scene:",
            buttons=rows,
        )

    except Exception as exc:
        return telegram.send_message(
            chat_id,
            f"Couldn't load showtimes: {exc}",
        )


# ---------------------------------------------------------------------------
# Date choice
# ---------------------------------------------------------------------------

def _resolve_date_choice(choice):
    """
    Convert a UI date choice into:

        dateValue
        humanLabel

    dateValue:
        "any"
        YYYYMMDD integer
        list of YYYYMMDD integers
    """

    from datetime import (
        datetime,
        timedelta,
    )

    now = (
        datetime.utcnow()
        + timedelta(
            hours=int(
                os.getenv(
                    "TZ_OFFSET",
                    "3",
                )
            )
        )
    )

    def ymd(value):
        return int(
            value.strftime(
                "%Y%m%d"
            )
        )

    if choice == "any":
        return (
            "any",
            "any date",
        )

    if choice == "today":
        return (
            ymd(now),
            now.strftime(
                "%a %d/%m"
            ),
        )

    if choice == "tomorrow":
        day = now + timedelta(
            days=1
        )

        return (
            ymd(day),
            day.strftime(
                "%a %d/%m"
            ),
        )

    if choice == "friday":
        ahead = (
            4 - now.weekday()
        ) % 7

        day = now + timedelta(
            days=ahead
        )

        return (
            ymd(day),
            day.strftime(
                "Fri %d/%m"
            ),
        )

    if choice == "weekend":
        friday = now + timedelta(
            days=(
                4 - now.weekday()
            ) % 7
        )

        saturday = friday + timedelta(
            days=1
        )

        return (
            [
                ymd(friday),
                ymd(saturday),
            ],
            "this weekend",
        )

    if choice == "week":
        return (
            [
                ymd(
                    now + timedelta(
                        days=i
                    )
                )
                for i in range(7)
            ],
            "within 7 days",
        )

    return (
        "any",
        "any date",
    )


# ---------------------------------------------------------------------------
# Existing open-date check
# ---------------------------------------------------------------------------

def _dates_already_open(
    chain,
    slug,
    cinemas,
    dates,
):
    """
    Return dates that already have matching showtimes.

    Used only to avoid creating a watcher for a specific date that is already
    bookable.
    """

    open_now = []

    try:
        if chain == "vox":
            bundle = vox.fetch_bundle()

            for display_date in dates:
                if vox.sessions_for(
                    bundle,
                    movie_slug=slug,
                    cinemas=cinemas,
                    display_date=display_date,
                    time_filter="any",
                ):
                    open_now.append(
                        display_date
                    )

        else:
            open_days = scene.open_days(
                slug
            )

            for display_date in dates:
                if (
                    scene.to_ddmmyyyy(
                        display_date
                    )
                    in open_days
                ):
                    open_now.append(
                        display_date
                    )

    except Exception:
        pass

    return open_now


# ---------------------------------------------------------------------------
# Stable session identity
# ---------------------------------------------------------------------------

def _session_key(chain, session):
    """
    Same identity format used by the cron watcher.
    """

    if chain == "vox":
        return (
            f"vox:{session.get('id') or session.get('bookingUrl')}"
        )

    return (
        f"scene:{session.get('showtime_url')}"
    )


# ---------------------------------------------------------------------------
# Initial snapshot
# ---------------------------------------------------------------------------

def _initial_seen_sessions(
    chain,
    slug,
    cinemas,
    time_filter,
    date_val,
):
    """
    Snapshot every currently matching showtime.

    These are the baseline.

    They are deliberately NOT considered new by cron.
    """

    seen = []

    try:
        # -------------------------------------------------------------------
        # VOX
        # -------------------------------------------------------------------

        if chain == "vox":
            bundle = vox.fetch_bundle()

            if date_val == "any":
                sessions = vox.sessions_for(
                    bundle,
                    movie_slug=slug,
                    cinemas=cinemas,
                    time_filter=time_filter,
                    only_available=True,
                )

                seen.extend(
                    _session_key(
                        chain,
                        session,
                    )
                    for session in sessions
                )

            else:
                dates = (
                    date_val
                    if isinstance(
                        date_val,
                        list,
                    )
                    else [date_val]
                )

                for display_date in dates:
                    sessions = vox.sessions_for(
                        bundle,
                        movie_slug=slug,
                        cinemas=cinemas,
                        display_date=display_date,
                        time_filter=time_filter,
                        only_available=True,
                    )

                    seen.extend(
                        _session_key(
                            chain,
                            session,
                        )
                        for session in sessions
                    )

        # -------------------------------------------------------------------
        # Scene
        # -------------------------------------------------------------------

        else:
            open_days = scene.open_days(
                slug
            )

            if date_val == "any":
                dates = open_days

            else:
                wanted = (
                    date_val
                    if isinstance(
                        date_val,
                        list,
                    )
                    else [date_val]
                )

                wanted_ddmm = {
                    scene.to_ddmmyyyy(
                        d
                    )
                    for d in wanted
                }

                dates = [
                    d
                    for d in open_days
                    if d in wanted_ddmm
                ]

            for display_date in dates:
                sessions = scene.sessions_for(
                    slug,
                    display_date,
                    time_filter=time_filter,
                )

                seen.extend(
                    _session_key(
                        chain,
                        session,
                    )
                    for session in sessions
                )

    except Exception:
        # A temporary failure while creating a watch should not prevent
        # the watch from being created.
        return []

    return list(
        dict.fromkeys(
            seen
        )
    )


# ---------------------------------------------------------------------------
# Scene seat map
# ---------------------------------------------------------------------------

def show_seatmap(
    chat_id,
    showtime_id,
):
    """
    Scene only.

    Fetches and renders a read-only seat map.
    """

    telegram.send_message(
        chat_id,
        f"Loading seat map… (id={showtime_id})",
    )

    try:
        plan = scene_seats.fetch_seat_plan(
            showtime_id
        )

    except Exception:
        import traceback

        return telegram.send_message(
            chat_id,
            "seatmap crash:\n"
            + traceback.format_exc()[-600:],
        )

    if (
        isinstance(plan, dict)
        and plan.get("error")
    ):
        return telegram.send_message(
            chat_id,
            f"seatmap debug: {plan['error']}",
        )

    if (
        not plan
        or not plan.get("rows")
    ):
        return telegram.send_message(
            chat_id,
            f"seatmap debug: empty plan -> {plan!r}",
        )

    caption = (
        f"🟩 {plan['free']} free · "
        f"⬛ {plan['taken']} taken — "
        f"pick your seats on Scene."
    )

    debug = ""

    try:
        png = scene_seats.render_png(
            plan.get("cells") or []
        )

        if isinstance(png, str):
            debug = png

        elif not png:
            debug = (
                "render_png returned None "
                "(no seats parsed)"
            )

        else:
            result = telegram.send_photo(
                chat_id,
                png,
                caption=caption,
            )

            if (
                isinstance(result, dict)
                and result.get("ok")
            ):
                return

            debug = (
                f"send_photo failed: {result}"
            )

    except Exception:
        import traceback

        debug = (
            "render/upload crash: "
            + traceback.format_exc()[-400:]
        )

    telegram.send_message(
        chat_id,
        f"[img debug] {debug}",
    )

    telegram.send_message(
        chat_id,
        scene_seats.render_text(
            plan
        ),
    )


# ---------------------------------------------------------------------------
# Save watcher
# ---------------------------------------------------------------------------

def save_watch(
    chat_id,
    date_choice,
):
    convo = store.get_convo(
        chat_id
    )

    if not convo.get("slug"):
        return telegram.send_message(
            chat_id,
            "Something expired — try /upcoming again.",
        )

    chain = convo["chain"]

    cinema_choice = convo.get(
        "cinemaChoice",
        "any:any",
    )

    if cinema_choice.startswith(
        "any"
    ):
        cinemas = "any"

    else:
        cinemas = [
            cinema_choice.split(
                ":"
            )[1]
        ]

    time_filter = convo.get(
        "timeFilter",
        "any",
    )

    date_val, date_label = (
        _resolve_date_choice(
            date_choice
        )
    )

    slug = convo["slug"]

    # -----------------------------------------------------------------------
    # Specific date already open?
    #
    # If yes, don't create a pointless watch.
    # -----------------------------------------------------------------------

    if date_val != "any":
        dates = (
            date_val
            if isinstance(
                date_val,
                list,
            )
            else [date_val]
        )

        already = _dates_already_open(
            chain,
            slug,
            cinemas,
            dates,
        )

        if already:
            store.clear_convo(
                chat_id
            )

            telegram.send_message(
                chat_id,
                f"📅 <b>{date_label}</b> is already open "
                f"for booking — here are the showtimes "
                f"(no watch needed):",
            )

            return show_showtimes(
                chat_id,
                chain,
                slug,
            )

    # -----------------------------------------------------------------------
    # IMPORTANT:
    #
    # Snapshot all showtimes that exist NOW.
    #
    # These are the baseline and will NOT trigger the cron watcher.
    # -----------------------------------------------------------------------

    seen_sessions = (
        _initial_seen_sessions(
            chain,
            slug,
            cinemas,
            time_filter,
            date_val,
        )
    )

    entry = {
        "chain": chain,
        "movieSlug": slug,

        # Preserve the existing title behavior.
        "movieTitle": slug.replace(
            "-",
            " ",
        ).title(),

        "cinemas": cinemas,

        "mode": "release",

        "date": date_val,

        "dateLabel": date_label,

        "timeFilter": time_filter,

        # -------------------------------------------------------------------
        # NEW WATCHER STATE
        # -------------------------------------------------------------------

        # Everything available at watch creation time is baseline.
        "seenSessions": seen_sessions,

        # Nothing is newly alertable yet.
        "alertSessions": [],

        # Existing compatibility flag.
        "alerted": False,
    }

    store.add_watch(
        chat_id,
        entry,
    )

    store.clear_convo(
        chat_id
    )

    _sync_cron()

    when = (
        ""
        if date_val == "any"
        else f" for <b>{date_label}</b>"
    )

    telegram.send_message(
        chat_id,
        f"👀 Watching <b>{entry['movieTitle']}</b>{when}. "
        f"I'll alert you when a <b>new showtime</b> opens.",
    )


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

DEFAULT_STATUS_SEC = 3600


def _fmt_interval(secs):
    """
    Human-readable heartbeat interval.
    """

    if secs is None:
        return "1 hour (default)"

    secs = int(secs)

    if secs <= 0:
        return "off"

    if secs % 3600 == 0:
        hours = secs // 3600

        return (
            f"{hours} hour"
            + (
                "s"
                if hours != 1
                else ""
            )
        )

    return f"{secs // 60} min"


def cmd_status(chat_id):
    current = store._get(
        f"status_interval:{chat_id}",
        None,
    )

    telegram.send_message(
        chat_id,
        f"⏱ <b>Watcher updates</b>\n"
        f"I quietly ping you what I'm watching every "
        f"<b>{_fmt_interval(current)}</b>.\n\n"
        f"How often would you like them?",
        buttons=[
            [
                (
                    "Every 30 min",
                    "statusiv:1800",
                ),
                (
                    "Every 1 hour",
                    "statusiv:3600",
                ),
            ],
            [
                (
                    "Every 3 hours",
                    "statusiv:10800",
                ),
                (
                    "Off",
                    "statusiv:0",
                ),
            ],
        ],
    )


def set_status_interval(
    chat_id,
    secs,
):
    store._set(
        f"status_interval:{chat_id}",
        int(secs),
    )

    store._set(
        f"status_ts:{chat_id}",
        0,
    )

    if secs <= 0:
        message = (
            "🔕 Watcher updates turned <b>off</b>. "
            "You'll still get loud alerts when a movie opens."
        )

    else:
        message = (
            f"✅ Watcher updates set to every "
            f"<b>{_fmt_interval(secs)}</b>."
        )

    telegram.send_message(
        chat_id,
        message,
    )


# ---------------------------------------------------------------------------
# List watches
# ---------------------------------------------------------------------------

def cmd_list(chat_id):
    watchlist = store.get_watchlist(
        chat_id
    )

    if not watchlist:
        return telegram.send_message(
            chat_id,
            "No active watches. /upcoming to add one.",
        )

    lines = [
        "<b>Your watches:</b>"
    ]

    for i, watch in enumerate(
        watchlist,
        1,
    ):
        cinemas = watch.get(
            "cinemas",
            "any",
        )

        cine = (
            "any"
            if cinemas == "any"
            else ",".join(cinemas)
        )

        flag = (
            " ⏰ NEW SHOWTIME"
            if watch.get("alerted")
            else ""
        )

        date_label = watch.get(
            "dateLabel",
            "any date",
        )

        lines.append(
            f"{i}. {watch['movieTitle']} "
            f"[{watch['chain']}] @ {cine} "
            f"· {date_label} "
            f"· {watch.get('timeFilter', 'any')}"
            f"{flag}"
        )

    lines.append(
        "\n/booked &lt;n&gt; or /remove &lt;n&gt; to stop one."
    )

    telegram.send_message(
        chat_id,
        "\n".join(lines),
    )


# ---------------------------------------------------------------------------
# Remove / booked
# ---------------------------------------------------------------------------

def cmd_stop(
    chat_id,
    text,
    booked,
):
    parts = text.split()

    if (
        len(parts) < 2
        or not parts[1].isdigit()
    ):
        return telegram.send_message(
            chat_id,
            "Usage: /%s &lt;number from /list&gt;"
            % (
                "booked"
                if booked
                else "remove"
            ),
        )

    index = int(
        parts[1]
    ) - 1

    watchlist = store.get_watchlist(
        chat_id
    )

    if (
        index < 0
        or index >= len(watchlist)
    ):
        return telegram.send_message(
            chat_id,
            "No watch with that number.",
        )

    title = watchlist[index][
        "movieTitle"
    ]

    store.remove_watch(
        chat_id,
        watchlist[index]["id"],
    )

    _sync_cron()

    verb = (
        "Booked — alerts stopped for"
        if booked
        else "Removed"
    )

    telegram.send_message(
        chat_id,
        f"✅ {verb} <b>{title}</b>.",
    )


# ---------------------------------------------------------------------------
# Cron synchronization
# ---------------------------------------------------------------------------

def _sync_cron():
    """
    Enable cron when at least one user has an active watch.

    Disable it when nobody has watches.
    """

    any_active = False

    for chat_id in store.all_chat_ids():
        if store.get_watchlist(
            chat_id
        ):
            any_active = True
            break

    last = store._get(
        "cron_state",
        None,
    )

    result = cronjob.sync_to_watches(
        any_active,
        last,
    )

    if result.get("changed"):
        store._set(
            "cron_state",
            result["state"],
        )


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def handle_http_request(request):
    """
    Compatibility wrapper for Vercel / api handlers.

    If the project already calls handle_update() directly, this function
    does not interfere with that flow.
    """

    try:
        update = request.get_json()

    except Exception:
        return {
            "ok": False,
            "error": "invalid JSON",
        }

    try:
        handle_update(
            update
        )

        return {
            "ok": True,
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": repr(exc),
        }
