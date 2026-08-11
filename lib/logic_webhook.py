"""
Telegram webhook — the interactive surface of the bot.

Supports:

MODE A
  /showing
    -> browse now-showing
    -> movie
    -> available days
    -> showtimes
    -> book

  A movie from /showing can also be turned into a watcher.

MODE B
  /upcoming
    -> browse coming-soon
    -> movie
    -> watcher setup

WATCHER
  A watcher can be created from either /showing or /upcoming.

  Watcher filters:
    - cinema
    - experience
    - time
    - date/date range

  The watcher snapshots matching sessions that already exist.

  Cron only alerts when a NEW session appears that matches ALL
  selected filters.

Experience examples:

  VOX:
    any
    gd   = Gold
    imx  = IMAX
    mx   = MAX
    fx   = 4DX
    kd   = Kids
    st   = Standard

  Scene:
    any
    imax  = ScreenX
    vip   = Premiere
    stand = Standard
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
# Watcher setup choices
# ---------------------------------------------------------------------------

CINEMA_CHOICES = [
    ("VOX Almaza", "vox:000047"),
    ("Scene CFC", "scene:cfc"),
    ("Any (Almaza or Scene)", "any:any"),
]


TIME_CHOICES = [
    ("After 5pm", "after5"),
    ("Any time", "any"),
    ("First showtime", "first"),
]


DATE_CHOICES = [
    ("Any date", "any"),
    ("Today", "today"),
    ("Tomorrow", "tomorrow"),
    ("This Friday", "friday"),
    ("This weekend", "weekend"),
    ("Within 7 days", "week"),
]


VOX_EXPERIENCE_CHOICES = [
    ("Any experience", "any"),
    ("IMAX", "imx"),
    ("Gold", "gd"),
    ("MAX", "mx"),
    ("4DX", "fx"),
    ("Kids", "kd"),
    ("Standard", "st"),
]


SCENE_EXPERIENCE_CHOICES = [
    ("Any experience", "any"),
    ("ScreenX", "imax"),
    ("Premiere", "vip"),
    ("Standard", "stand"),
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
    ev = telegram.parse_update(update)

    if not ev:
        return

    chat_id = ev["chat_id"]
    user_id = ev["user_id"]

    if not is_member(user_id):
        if (
            ev["kind"] == "message"
            and JOIN_CODE
            and ev["text"] == JOIN_CODE
        ):
            add_member(user_id)

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

    text = ev["text"]

    if text.startswith("/start"):
        telegram.send_message(
            chat_id,
            "🎬 Cinema bot.\n\n"
            "/showing — what's on now (book/watch)\n"
            "/upcoming — watch a coming-soon movie\n"
            "/list — your watches\n"
            "/status — how often I ping what I'm watching\n"
            "/remove &lt;n&gt; — stop a watch\n"
            "/booked &lt;n&gt; — mark booked, stop alerts",
        )

    elif text.startswith("/showing"):
        cmd_showing(chat_id)

    elif text.startswith("/upcoming"):
        cmd_upcoming(chat_id)

    elif text.startswith("/list"):
        cmd_list(chat_id)

    elif text.startswith("/status"):
        cmd_status(chat_id)

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

    try:
        bundle = vox.fetch_bundle()

        for movie in vox.now_showing(bundle):
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
# MODE B — upcoming
# ---------------------------------------------------------------------------

def cmd_upcoming(chat_id):
    telegram.send_message(
        chat_id,
        "Fetching coming soon…",
    )

    rows = []

    try:
        bundle = vox.fetch_bundle()

        for movie in vox.coming_soon(bundle):
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

    if action == "show":
        return show_showtimes(
            chat_id,
            parts[1],
            parts[2],
        )

    if action == "day":
        return show_day_showtimes(
            chat_id,
            parts[1],
            parts[2],
            parts[3],
        )

    if action == "statusiv":
        return set_status_interval(
            chat_id,
            int(parts[1]),
        )

    if action == "seatmap":
        return show_seatmap(
            chat_id,
            ":".join(parts[1:]),
        )

    if action == "soldout":
        return

    # -------------------------------------------------------------------
    # WATCHER ENTRY FROM /UPCOMING
    # -------------------------------------------------------------------

    if action == "mark":
        chain = parts[1]
        slug = ":".join(parts[2:])

        store.set_convo(
            chat_id,
            {
                "chain": chain,
                "slug": slug,
                "step": "cinema",
                "source": "upcoming",
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
                if _cinema_choice_allowed(
                    chain,
                    value,
                )
            ],
        )

    # -------------------------------------------------------------------
    # WATCHER ENTRY FROM /SHOWING
    # -------------------------------------------------------------------

    if action == "watch":
        chain = parts[1]
        slug = ":".join(parts[2:])

        store.set_convo(
            chat_id,
            {
                "chain": chain,
                "slug": slug,
                "step": "cinema",
                "source": "showing",
            },
        )

        return telegram.send_message(
            chat_id,
            "Which cinema should I watch?",
            buttons=[
                [
                    (
                        name,
                        f"mc:{value}",
                    )
                ]
                for name, value in CINEMA_CHOICES
                if _cinema_choice_allowed(
                    chain,
                    value,
                )
            ],
        )

    # -------------------------------------------------------------------
    # Cinema selected
    # -------------------------------------------------------------------

    if action == "mc":
        convo = store.get_convo(
            chat_id
        )

        convo["cinemaChoice"] = ":".join(
            parts[1:]
        )

        convo["step"] = "experience"

        store.set_convo(
            chat_id,
            convo,
        )

        chain = convo.get(
            "chain"
        )

        choices = (
            VOX_EXPERIENCE_CHOICES
            if chain == "vox"
            else SCENE_EXPERIENCE_CHOICES
        )

        return telegram.send_message(
            chat_id,
            "Which cinema experience?",
            buttons=[
                [
                    (
                        name,
                        f"me:{value}",
                    )
                ]
                for name, value in choices
            ],
        )

    # -------------------------------------------------------------------
    # Experience selected
    # -------------------------------------------------------------------

    if action == "me":
        convo = store.get_convo(
            chat_id
        )

        convo["experience"] = parts[1]
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

    # -------------------------------------------------------------------
    # Time selected
    # -------------------------------------------------------------------

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

    # -------------------------------------------------------------------
    # Date selected
    # -------------------------------------------------------------------

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
# Cinema validation
# ---------------------------------------------------------------------------

def _cinema_choice_allowed(chain, choice):
    if choice == "any:any":
        return True

    if chain == "vox":
        return choice.startswith("vox:")

    if chain == "scene":
        return choice.startswith("scene:")

    return True


# ---------------------------------------------------------------------------
# Day helpers
# ---------------------------------------------------------------------------

def _daylabel(yyyymmdd):
    from datetime import datetime

    try:
        return datetime.strptime(
            str(yyyymmdd),
            "%Y%m%d",
        ).strftime("%a %d/%m")

    except Exception:
        return str(yyyymmdd)


def show_showtimes(chat_id, chain, slug):
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
                else slug.replace("-", " ").title()
            )

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
                day = str(
                    session["displayDate"]
                )

                if day not in seen:
                    seen.add(day)
                    days.append(day)

            cinema = "VOX Almaza"

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
                "No showtimes yet.",
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

        # IMPORTANT:
        # A movie that is already showing can still become a watcher.
        rows.append([
            (
                "👀 Watch new showtimes",
                f"watch:{chain}:{slug}",
            )
        ])

        return telegram.send_message(
            chat_id,
            f"🎬 <b>{name}</b> — {cinema}\n"
            "Which day do you want to go?",
            buttons=rows,
        )

    except Exception as exc:
        return telegram.send_message(
            chat_id,
            f"Couldn't load days: {exc}",
        )


# ---------------------------------------------------------------------------
# Show one day's showtimes
# ---------------------------------------------------------------------------

def show_day_showtimes(
    chat_id,
    chain,
    slug,
    yyyymmdd,
):
    telegram.send_message(
        chat_id,
        "Loading showtimes…",
    )

    daylbl = _daylabel(
        yyyymmdd
    )

    try:
        if chain == "vox":
            bundle = vox.fetch_bundle()

            movie = vox.find_movie(
                bundle,
                slug=slug,
            )

            name = (
                movie["title"]
                if movie
                else slug.replace("-", " ").title()
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
                    f"🎬 <b>{name}</b> — VOX Almaza · {daylbl}\n"
                    "No showtimes for that day.",
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

                exp = session["experience"]

                if free:
                    rows.append([
                        (
                            f"{session['time']} · {exp}",
                            session["bookingUrl"],
                        )
                    ])
                else:
                    rows.append([
                        (
                            f"🔴 {session['time']} · {exp} — sold out",
                            "soldout",
                        )
                    ])

            rows.append([
                (
                    "👀 Watch new showtimes",
                    f"watch:{chain}:{slug}",
                )
            ])

            return telegram.send_message(
                chat_id,
                f"🎬 <b>{name}</b> — VOX Almaza · {daylbl}\n"
                "Tap a time to book or watch future showtimes:",
                buttons=rows,
            )

        # ---------------------------------------------------------------
        # Scene
        # ---------------------------------------------------------------

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
                f"🎬 <b>{name}</b> — Scene CFC · {daylbl}\n"
                "No showtimes for that day.",
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

            seat_btn = (
                f"🗺 {session['time']} · "
                f"{session['experience']}",
                (
                    f"seatmap:{showtime_id}"
                    if showtime_id
                    else "seatmap:BADID"
                ),
            )

            rows.append([
                seat_btn,
                (
                    "Book",
                    session["showtime_url"],
                ),
            ])

        rows.append([
            (
                "👀 Watch new showtimes",
                f"watch:{chain}:{slug}",
            )
        ])

        return telegram.send_message(
            chat_id,
            f"🎬 <b>{name}</b> — Scene CFC · {daylbl}\n"
            "🗺 = see seats · Book = go to Scene:",
            buttons=rows,
        )

    except Exception as exc:
        return telegram.send_message(
            chat_id,
            f"Couldn't load showtimes: {exc}",
        )


# ---------------------------------------------------------------------------
# Date resolution
# ---------------------------------------------------------------------------

def _resolve_date_choice(choice):
    from datetime import datetime, timedelta

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

    def ymd(day):
        return int(
            day.strftime("%Y%m%d")
        )

    if choice == "any":
        return (
            "any",
            "any date",
        )

    if choice == "today":
        return (
            ymd(now),
            now.strftime("%a %d/%m"),
        )

    if choice == "tomorrow":
        day = now + timedelta(
            days=1
        )

        return (
            ymd(day),
            day.strftime("%a %d/%m"),
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
            day.strftime("Fri %d/%m"),
        )

    if choice == "weekend":
        fri = now + timedelta(
            days=(
                4 - now.weekday()
            ) % 7
        )

        sat = fri + timedelta(
            days=1
        )

        return (
            [
                ymd(fri),
                ymd(sat),
            ],
            "this weekend",
        )

    if choice == "week":
        return (
            [
                ymd(
                    now + timedelta(days=i)
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
# Existing sessions snapshot
# ---------------------------------------------------------------------------

def _session_key(chain, session):
    if chain == "vox":
        return (
            f"vox:"
            f"{session.get('id') or session.get('bookingUrl')}"
        )

    return (
        f"scene:"
        f"{session.get('showtime_url')}"
    )


def _initial_seen_sessions(
    chain,
    slug,
    cinemas,
    time_filter,
    date_val,
    experience,
):
    """
    Snapshot ONLY sessions matching the watcher's filters.

    This is crucial.

    Example:
      IMAX watcher is created while Gold + Standard are open.

    Gold and Standard are NOT put into the watcher's baseline.

    Later:
      Gold opens again -> irrelevant
      Standard opens -> irrelevant
      IMAX opens -> new matching session -> alert
    """

    seen = []

    try:
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

                sessions = [
                    s for s in sessions
                    if _experience_matches(
                        chain,
                        s,
                        experience,
                    )
                ]

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

                for day in dates:
                    sessions = vox.sessions_for(
                        bundle,
                        movie_slug=slug,
                        cinemas=cinemas,
                        display_date=day,
                        time_filter=time_filter,
                        only_available=True,
                    )

                    sessions = [
                        s for s in sessions
                        if _experience_matches(
                            chain,
                            s,
                            experience,
                        )
                    ]

                    seen.extend(
                        _session_key(
                            chain,
                            session,
                        )
                        for session in sessions
                    )

        else:
            if date_val == "any":
                dates = scene.open_days(
                    slug
                )

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
                    scene.to_ddmmyyyy(day)
                    for day in wanted
                }

                dates = [
                    day
                    for day in scene.open_days(
                        slug
                    )
                    if day in wanted_ddmm
                ]

            for day in dates:
                sessions = scene.sessions_for(
                    slug,
                    day,
                    time_filter=time_filter,
                )

                sessions = [
                    s for s in sessions
                    if _experience_matches(
                        chain,
                        s,
                        experience,
                    )
                ]

                seen.extend(
                    _session_key(
                        chain,
                        session,
                    )
                    for session in sessions
                )

    except Exception:
        # A temporary snapshot failure should not prevent the watch
        # from being created.
        return []

    return list(
        dict.fromkeys(seen)
    )


def _experience_normalise(value):
    if value is None:
        return "any"

    value = str(
        value
    ).strip().lower()

    aliases = {
        "any": "any",
        "all": "any",

        "gold": "gd",
        "gd": "gd",

        "imax": "imx",
        "imx": "imx",

        "max": "mx",
        "mx": "mx",

        "4dx": "fx",
        "fx": "fx",

        "kids": "kd",
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

    return aliases.get(
        value,
        value,
    )


def _experience_matches(
    chain,
    session,
    wanted,
):
    wanted = _experience_normalise(
        wanted
    )

    if wanted == "any":
        return True

    if chain == "vox":
        actual = session.get(
            "experienceCode"
        )

        if not actual:
            actual = session.get(
                "experience"
            )

        return (
            _experience_normalise(actual)
            == wanted
        )

    actual = session.get(
        "experience"
    )

    return (
        _experience_normalise(actual)
        == wanted
    )


# ---------------------------------------------------------------------------
# Save watcher
# ---------------------------------------------------------------------------

def save_watch(chat_id, date_choice):
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

    cinemas = (
        "any"
        if cinema_choice.startswith(
            "any"
        )
        else [
            cinema_choice.split(":")[1]
        ]
    )

    time_filter = convo.get(
        "timeFilter",
        "any",
    )

    experience = convo.get(
        "experience",
        "any",
    )

    date_val, date_label = (
        _resolve_date_choice(
            date_choice
        )
    )

    slug = convo["slug"]

    # ---------------------------------------------------------------
    # IMPORTANT:
    #
    # If a specifically requested date already has a MATCHING
    # experience available, don't create a pointless watcher.
    #
    # We deliberately check the experience too.
    #
    # If the user chose IMAX and only Gold is open, this does NOT
    # count as already open.
    # ---------------------------------------------------------------

    if date_val != "any":
        dates = (
            date_val
            if isinstance(
                date_val,
                list,
            )
            else [date_val]
        )

        already = []

        try:
            if chain == "vox":
                bundle = vox.fetch_bundle()

                for day in dates:
                    sessions = vox.sessions_for(
                        bundle,
                        movie_slug=slug,
                        cinemas=cinemas,
                        display_date=day,
                        time_filter=time_filter,
                        only_available=True,
                    )

                    for session in sessions:
                        if _experience_matches(
                            chain,
                            session,
                            experience,
                        ):
                            already.append(day)
                            break

            else:
                open_days = scene.open_days(
                    slug
                )

                for day in dates:
                    ddmm = scene.to_ddmmyyyy(
                        day
                    )

                    if ddmm not in open_days:
                        continue

                    sessions = scene.sessions_for(
                        slug,
                        ddmm,
                        time_filter=time_filter,
                    )

                    if any(
                        _experience_matches(
                            chain,
                            session,
                            experience,
                        )
                        for session in sessions
                    ):
                        already.append(day)

        except Exception:
            already = []

        if already:
            store.clear_convo(
                chat_id
            )

            return telegram.send_message(
                chat_id,
                f"📅 <b>{date_label}</b> already has a "
                f"matching <b>{_experience_label(chain, experience)}</b> "
                "showtime.\n"
                "Use /showing to book it.",
            )

    # ---------------------------------------------------------------
    # Snapshot ONLY matching sessions.
    # ---------------------------------------------------------------

    seen_sessions = _initial_seen_sessions(
        chain=chain,
        slug=slug,
        cinemas=cinemas,
        time_filter=time_filter,
        date_val=date_val,
        experience=experience,
    )

    # ---------------------------------------------------------------
    # Get the real movie title.
    # ---------------------------------------------------------------

    movie_title = slug.replace(
        "-",
        " ",
    ).title()

    try:
        if chain == "vox":
            bundle = vox.fetch_bundle()

            movie = vox.find_movie(
                bundle,
                slug=slug,
            )

            if movie and movie.get("title"):
                movie_title = movie["title"]

        else:
            for movie in (
                scene.now_showing()
                + scene.coming_soon()
            ):
                if movie.get("slug") == slug:
                    movie_title = movie.get(
                        "title",
                        movie_title,
                    )
                    break

    except Exception:
        pass

    entry = {
        "chain": chain,
        "movieSlug": slug,
        "movieTitle": movie_title,
        "cinemas": cinemas,

        # Same watcher model regardless of whether it came from
        # /showing or /upcoming.
        "mode": "release",

        "date": date_val,
        "dateLabel": date_label,
        "timeFilter": time_filter,

        # NEW:
        # Exact experience the watcher cares about.
        "experience": experience,

        # Existing matching sessions are baseline.
        "seenSessions": seen_sessions,
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

    exp_label = _experience_label(
        chain,
        experience,
    )

    telegram.send_message(
        chat_id,
        f"👀 Watching <b>{movie_title}</b>{when}\n"
        f"🎭 Experience: <b>{exp_label}</b>\n"
        f"⏰ Time: <b>{time_filter}</b>\n\n"
        "I'll alert you only when a NEW matching showtime opens.",
    )


def _experience_label(
    chain,
    experience,
):
    value = _experience_normalise(
        experience
    )

    choices = (
        VOX_EXPERIENCE_CHOICES
        if chain == "vox"
        else SCENE_EXPERIENCE_CHOICES
    )

    for label, code in choices:
        if code == value:
            return label

    return "Any experience"


# ---------------------------------------------------------------------------
# Seat map
# ---------------------------------------------------------------------------

def show_seatmap(
    chat_id,
    showtime_id,
):
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
            f"seatmap crash:\n"
            f"{traceback.format_exc()[-600:]}",
        )

    if (
        isinstance(plan, dict)
        and plan.get("error")
    ):
        return telegram.send_message(
            chat_id,
            f"seatmap debug: {plan['error']}",
        )

    if not plan or not plan.get("rows"):
        return telegram.send_message(
            chat_id,
            f"seatmap debug: empty plan -> {plan!r}",
        )

    caption = (
        f"🟩 {plan['free']} free · "
        f"⬛ {plan['taken']} taken — "
        "pick your seats on Scene."
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
        scene_seats.render_text(plan),
    )


# ---------------------------------------------------------------------------
# Manage
# ---------------------------------------------------------------------------

DEFAULT_STATUS_SEC = 3600


def _fmt_interval(secs):
    if secs is None:
        return "1 hour (default)"

    secs = int(secs)

    if secs <= 0:
        return "off"

    if secs % 3600 == 0:
        hours = secs // 3600

        return (
            f"{hours} hour"
            + ("s" if hours != 1 else "")
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
        "I quietly ping you what I'm watching every "
        f"<b>{_fmt_interval(current)}</b>.\n\n"
        "How often would you like them?",
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
            "You'll still get loud alerts when a matching "
            "new showtime opens."
        )

    else:
        message = (
            "✅ Watcher updates set to every "
            f"<b>{_fmt_interval(secs)}</b>."
        )

    telegram.send_message(
        chat_id,
        message,
    )


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
            " ⏰ NEW MATCH"
            if watch.get("alerted")
            else ""
        )

        date_label = watch.get(
            "dateLabel",
            "any date",
        )

        experience = _experience_label(
            watch["chain"],
            watch.get(
                "experience",
                watch.get(
                    "experienceFilter",
                    "any",
                ),
            ),
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
        "\n/booked &lt;n&gt; or /remove &lt;n&gt; to stop one."
    )

    telegram.send_message(
        chat_id,
        "\n".join(lines),
    )


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

    index = int(parts[1]) - 1

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
