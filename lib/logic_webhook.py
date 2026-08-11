"""
Telegram webhook — the interactive surface of the bot.

Receives commands + button taps from Telegram and drives both modes:
  MODE A  /showing   -> browse now-showing (both chains) -> showtimes -> deep-link book
  MODE B  /upcoming  -> browse coming-soon -> mark flow
                         (cinema, experience, time filter, date) -> watch
  Manage  /list, /remove <n>, /booked <n>
  Access  send secret code once to join; /start explains.

Stateless per request: conversation state (the /upcoming mark flow) lives in KV
via store.get_convo/set_convo. Watchlists are per-user (keyed by chat id).
"""

import os
import re
import sys
import json


from lib import store, telegram, vox, scene, scene_seats, cronjob  # noqa: E402


JOIN_CODE = os.getenv("JOIN_CODE", "").strip()


# ---------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------

CINEMA_CHOICES = [
    ("VOX Almaza", "vox:000047"),
    ("Scene CFC", "scene:cfc"),
    ("Any (Almaza or Scene)", "any:any"),
]


# Experience choices.
#
# The stored value is normalized by logic_check.py.
#
# VOX:
#   imx = IMAX
#   gd  = Gold
#   mx  = MAX
#   fx  = 4DX
#   kd  = Kids
#   st  = Standard
#
# Scene:
#   imax  = ScreenX
#   vip   = Premiere
#   stand = Standard
#
# "any" works on both chains.

VOX_EXPERIENCE_CHOICES = [
    ("Any experience", "any"),
    ("IMAX", "imax"),
    ("Gold", "gold"),
    ("MAX", "max"),
    ("4DX", "4dx"),
    ("Kids", "kids"),
    ("Standard", "standard"),
]


SCENE_EXPERIENCE_CHOICES = [
    ("Any experience", "any"),
    ("ScreenX", "screenx"),
    ("Premiere", "premiere"),
    ("Standard", "standard"),
]


ANY_EXPERIENCE_CHOICES = [
    ("Any experience", "any"),
    ("IMAX", "imax"),
    ("Gold", "gold"),
    ("MAX", "max"),
    ("4DX", "4dx"),
    ("Kids", "kids"),
    ("ScreenX", "screenx"),
    ("Premiere", "premiere"),
    ("Standard", "standard"),
]


TIME_CHOICES = [
    ("After 5pm", "after5"),
    ("Any time", "any"),
    ("First showtime", "first"),
]


# Date choices resolve to concrete YYYYMMDD (or "any") at save time.
DATE_CHOICES = [
    ("Any date", "any"),
    ("Today", "today"),
    ("Tomorrow", "tomorrow"),
    ("This Friday", "friday"),
    ("This weekend", "weekend"),
    ("Within 7 days", "week"),
]


# ---------------------------------------------------------------------------
# Experience helpers
# ---------------------------------------------------------------------------

def _experience_label(value):
    """Human-readable experience label."""

    if value is None:
        return "Any experience"

    value = str(value).strip().lower()

    labels = {
        "any": "Any experience",

        "imx": "IMAX",
        "imax": "IMAX",

        "gd": "Gold",
        "gold": "Gold",

        "mx": "MAX",
        "max": "MAX",

        "fx": "4DX",
        "4dx": "4DX",

        "kd": "Kids",
        "kids": "Kids",

        "st": "Standard",
        "standard": "Standard",

        "screenx": "ScreenX",

        "vip": "Premiere",
        "premiere": "Premiere",

        "stand": "Standard",
    }

    return labels.get(
        value,
        str(value),
    )


def _experience_choices_for(
    chain,
    cinema_choice,
):
    """
    Return the experience buttons appropriate for the current selection.

    If a specific chain/cinema is selected, keep the choices clean.

    If the user chose the combined "Any (Almaza or Scene)" option, show the
    union so they can still request a precise experience. logic_check.py
    will only match experiences that actually exist on each chain.
    """

    if cinema_choice.startswith("vox:"):
        return VOX_EXPERIENCE_CHOICES

    if cinema_choice.startswith("scene:"):
        return SCENE_EXPERIENCE_CHOICES

    return ANY_EXPERIENCE_CHOICES


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

    # gate: non-members must send the join code first
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
            "/showing — what's on now (book)\n"
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
# MODE A: browse now-showing
# ---------------------------------------------------------------------------

def cmd_showing(chat_id):

    telegram.send_message(
        chat_id,
        "Fetching what's on…",
    )

    rows = []

    try:

        b = vox.fetch_bundle()

        for m in vox.now_showing(b):

            rows.append([
                (
                    f"🎬 {m['title'][:40]}",
                    f"show:vox:{m['slug']}",
                )
            ])

    except Exception as e:

        telegram.send_message(
            chat_id,
            f"(VOX unavailable: {e})",
        )

    # Scene now-showing is best-effort
    try:

        for m in scene.now_showing()[:15]:

            rows.append([
                (
                    f"🎬 {m['title'][:40]} (Scene)",
                    f"show:scene:{m['slug']}",
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
# MODE B: browse coming-soon
# ---------------------------------------------------------------------------

def cmd_upcoming(chat_id):

    telegram.send_message(
        chat_id,
        "Fetching coming soon…",
    )

    rows = []

    try:

        b = vox.fetch_bundle()

        for m in vox.coming_soon(b):

            rows.append([
                (
                    f"🔜 {m['title'][:40]}",
                    f"mark:vox:{m['slug']}",
                )
            ])

    except Exception as e:

        telegram.send_message(
            chat_id,
            f"(VOX unavailable: {e})",
        )

    try:

        for m in scene.coming_soon()[:15]:

            rows.append([
                (
                    f"🔜 {m['title'][:40]} (Scene)",
                    f"mark:scene:{m['slug']}",
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

        # show:<chain>:<slug> -> day picker

        return show_showtimes(
            chat_id,
            parts[1],
            parts[2],
        )

    if action == "day":

        # day:<chain>:<slug>:<yyyymmdd>

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

    # -----------------------------------------------------------------------
    # START WATCH
    # -----------------------------------------------------------------------

    if action == "mark":

        chain = parts[1]
        slug = parts[2]

        store.set_convo(
            chat_id,
            {
                "chain": chain,
                "slug": slug,
                "step": "cinema",
            },
        )

        return telegram.send_message(
            chat_id,
            "Which cinema?",
            buttons=[
                [
                    (name, f"mc:{value}")
                ]
                for name, value
                in CINEMA_CHOICES
            ],
        )

    # -----------------------------------------------------------------------
    # CINEMA
    # -----------------------------------------------------------------------

    if action == "mc":

        convo = store.get_convo(
            chat_id
        )

        cinema_choice = ":".join(
            parts[1:]
        )

        convo["cinemaChoice"] = (
            cinema_choice
        )

        convo["step"] = "experience"

        store.set_convo(
            chat_id,
            convo,
        )

        choices = _experience_choices_for(
            convo.get("chain"),
            cinema_choice,
        )

        return telegram.send_message(
            chat_id,
            "Which cinema experience?",
            buttons=[
                [
                    (name, f"me:{value}")
                ]
                for name, value
                in choices
            ],
        )

    # -----------------------------------------------------------------------
    # EXPERIENCE
    # -----------------------------------------------------------------------

    if action == "me":

        convo = store.get_convo(
            chat_id
        )

        experience = parts[1]

        convo["experience"] = (
            experience
        )

        convo["experienceLabel"] = (
            _experience_label(
                experience
            )
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
                    (name, f"mt:{value}")
                ]
                for name, value
                in TIME_CHOICES
            ],
        )

    # -----------------------------------------------------------------------
    # TIME
    # -----------------------------------------------------------------------

    if action == "mt":

        convo = store.get_convo(
            chat_id
        )

        convo["timeFilter"] = (
            parts[1]
        )

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
                    (name, f"md:{value}")
                ]
                for name, value
                in DATE_CHOICES
            ],
        )

    # -----------------------------------------------------------------------
    # DATE
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
# Showing helpers
# ---------------------------------------------------------------------------

def _daylabel(yyyymmdd):

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


def show_showtimes(chat_id, chain, slug):

    telegram.send_message(
        chat_id,
        "Checking available days…",
    )

    try:

        days = []

        if chain == "vox":

            b = vox.fetch_bundle()

            movie = vox.find_movie(
                b,
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

            for x in sorted(
                vox.sessions_for(
                    b,
                    movie_slug=slug,
                    time_filter="any",
                    only_available=False,
                ),
                key=lambda z: z["displayDate"],
            ):

                d = str(
                    x["displayDate"]
                )

                if d not in seen:

                    seen.add(d)
                    days.append(d)

        else:

            name = slug.replace(
                "-",
                " ",
            ).title()

            cinema = "Scene CFC"

            for ddmm in scene.open_days(
                slug
            ):

                dd, mm, yyyy = (
                    ddmm.split("-")
                )

                days.append(
                    f"{yyyy}{mm}{dd}"
                )

            days.sort()

        if not days:

            return telegram.send_message(
                chat_id,
                f"🎬 <b>{name}</b> — {cinema}\n"
                f"No showtimes yet. "
                f"Use /upcoming to be pinged when they open.",
            )

        rows = [
            [
                (
                    _daylabel(d),
                    f"day:{chain}:{slug}:{d}",
                )
            ]
            for d in days[:10]
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

    except Exception as e:

        return telegram.send_message(
            chat_id,
            f"Couldn't load days: {e}",
        )


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

            b = vox.fetch_bundle()

            movie = vox.find_movie(
                b,
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

            sess = vox.sessions_for(
                b,
                movie_slug=slug,
                display_date=int(
                    yyyymmdd
                ),
                time_filter="any",
                only_available=False,
            )

            if not sess:

                return telegram.send_message(
                    chat_id,
                    f"🎬 <b>{name}</b> — "
                    f"VOX Almaza · {daylbl}\n"
                    f"No showtimes for that day.",
                )

            rows = []

            for x in sorted(
                sess,
                key=lambda z: z["showtime"],
            ):

                free = (
                    x["seats"]
                    and x["seats"] > 0
                )

                exp = x["experience"]

                if free:

                    rows.append([
                        (
                            f"{x['time']} · {exp}",
                            x["bookingUrl"],
                        )
                    ])

                else:

                    rows.append([
                        (
                            f"🔴 {x['time']} · "
                            f"{exp} — sold out",
                            "soldout",
                        )
                    ])

            return telegram.send_message(
                chat_id,
                f"🎬 <b>{name}</b> — "
                f"VOX Almaza · {daylbl}\n"
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

        sess = scene.sessions_for(
            slug,
            ddmm,
            time_filter="any",
        )

        if not sess:

            return telegram.send_message(
                chat_id,
                f"🎬 <b>{name}</b> — "
                f"Scene CFC · {daylbl}\n"
                f"No showtimes for that day.",
            )

        rows = []

        for x in sess[:10]:

            raw = (
                x["showtime_url"]
                .split("?")[0]
                .rstrip("/")
            )

            m = re.search(
                r"(?:showtime|booking)-([0-9a-f]{24})",
                raw,
            )

            stid = (
                m.group(1)
                if m
                else None
            )

            seat_btn = (
                f"🗺 {x['time']} · "
                f"{x['experience']}",
                f"seatmap:{stid}"
                if stid
                else "seatmap:BADID",
            )

            rows.append([
                seat_btn,
                (
                    "Book",
                    x["showtime_url"],
                ),
            ])

        return telegram.send_message(
            chat_id,
            f"🎬 <b>{name}</b> — "
            f"Scene CFC · {daylbl}\n"
            f"🗺 = see seats · "
            f"Book = go to Scene:",
            buttons=rows,
        )

    except Exception as e:

        return telegram.send_message(
            chat_id,
            f"Couldn't load showtimes: {e}",
        )


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _resolve_date_choice(choice):

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

    def ymd(d):

        return int(
            d.strftime("%Y%m%d")
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

        d = now + timedelta(
            days=1
        )

        return (
            ymd(d),
            d.strftime(
                "%a %d/%m"
            ),
        )

    if choice == "friday":

        ahead = (
            4 - now.weekday()
        ) % 7

        d = now + timedelta(
            days=ahead
        )

        return (
            ymd(d),
            d.strftime(
                "Fri %d/%m"
            ),
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
                    now
                    + timedelta(days=i)
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
# Existing matching sessions
# ---------------------------------------------------------------------------

def _normalize_experience(value):

    if value is None:
        return "any"

    s = str(value).strip().lower()

    aliases = {
        "any": "any",

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

        "screenx": "screenx",

        "vip": "premiere",
        "premiere": "premiere",

        "stand": "standard",
    }

    return aliases.get(
        s,
        s,
    )


def _experience_matches(
    actual,
    wanted,
):

    wanted = _normalize_experience(
        wanted
    )

    if wanted == "any":
        return True

    return (
        _normalize_experience(actual)
        == wanted
    )


def _dates_already_open(
    chain,
    slug,
    cinemas,
    experience,
    dates,
):

    """
    Return the subset of dates that already contain at least one
    AVAILABLE matching session.

    IMPORTANT:

    We do NOT use Scene.open_days() by itself because that only proves
    that the day has some experience available.

    Example:

      IMAX watcher
      Standard is open
      IMAX is not open

    Result must be FALSE.
    """

    open_now = []

    try:

        if chain == "vox":

            b = vox.fetch_bundle()

            for d in dates:

                sessions = vox.sessions_for(
                    b,
                    movie_slug=slug,
                    cinemas=cinemas,
                    display_date=d,
                    time_filter="any",
                    only_available=True,
                )

                sessions = [
                    s
                    for s in sessions
                    if _experience_matches(
                        s.get("experience"),
                        experience,
                    )
                ]

                if sessions:
                    open_now.append(d)

        else:

            opendays = scene.open_days(
                slug
            )

            for d in dates:

                ddmm = scene.to_ddmmyyyy(
                    d
                )

                if ddmm not in opendays:
                    continue

                sessions = scene.sessions_for(
                    slug,
                    ddmm,
                    time_filter="any",
                )

                sessions = [
                    s
                    for s in sessions
                    if _experience_matches(
                        s.get("experience"),
                        experience,
                    )
                ]

                if sessions:
                    open_now.append(d)

    except Exception:

        # If the check fails, fall through and create the watch.
        pass

    return open_now


# ---------------------------------------------------------------------------
# Session identity
# ---------------------------------------------------------------------------

def _session_key(
    chain,
    session,
):

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
# Initial watcher snapshot
# ---------------------------------------------------------------------------

def _initial_seen_sessions(
    chain,
    slug,
    cinemas,
    experience,
    time_filter,
    date_val,
):

    """
    Snapshot ALL currently existing matching showtimes.

    These are deliberately NOT considered new by cron.

    This is what prevents:

        "I created the watcher while IMAX was already open"

    from immediately generating an alert.

    It also prevents:

        "Standard was already open when I created an IMAX watcher"

    from interfering with the IMAX watcher.
    """

    seen = []

    try:

        if chain == "vox":

            b = vox.fetch_bundle()

            if date_val == "any":

                sessions = vox.sessions_for(
                    b,
                    movie_slug=slug,
                    cinemas=cinemas,
                    time_filter=time_filter,
                    only_available=True,
                )

                sessions = [
                    s
                    for s in sessions
                    if _experience_matches(
                        s.get("experience"),
                        experience,
                    )
                ]

                seen.extend(
                    _session_key(
                        chain,
                        s,
                    )
                    for s in sessions
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

                for d in dates:

                    sessions = vox.sessions_for(
                        b,
                        movie_slug=slug,
                        cinemas=cinemas,
                        display_date=d,
                        time_filter=time_filter,
                        only_available=True,
                    )

                    sessions = [
                        s
                        for s in sessions
                        if _experience_matches(
                            s.get("experience"),
                            experience,
                        )
                    ]

                    seen.extend(
                        _session_key(
                            chain,
                            s,
                        )
                        for s in sessions
                    )

        else:

            # Scene
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
                    scene.to_ddmmyyyy(d)
                    for d in wanted
                }

                dates = [
                    d
                    for d in scene.open_days(
                        slug
                    )
                    if d in wanted_ddmm
                ]

            for d in dates:

                sessions = scene.sessions_for(
                    slug,
                    d,
                    time_filter=time_filter,
                )

                sessions = [
                    s
                    for s in sessions
                    if _experience_matches(
                        s.get("experience"),
                        experience,
                    )
                ]

                seen.extend(
                    _session_key(
                        chain,
                        s,
                    )
                    for s in sessions
                )

    except Exception:

        # Don't prevent the watch from being created if the initial
        # snapshot temporarily fails.
        return []

    return list(
        dict.fromkeys(seen)
    )


# ---------------------------------------------------------------------------
# Seat map
# ---------------------------------------------------------------------------

def show_seatmap(
    chat_id,
    showtime_id,
):

    telegram.send_message(
        chat_id,
        f"Loading seat map… "
        f"(id={showtime_id})",
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
            f"seatmap debug: "
            f"{plan['error']}",
        )

    if (
        not plan
        or not plan.get("rows")
    ):

        return telegram.send_message(
            chat_id,
            f"seatmap debug: "
            f"empty plan -> {plan!r}",
        )

    caption = (
        f"🟩 {plan['free']} free · "
        f"⬛ {plan['taken']} taken — "
        f"pick your seats on Scene."
    )

    dbg = ""

    try:

        png = scene_seats.render_png(
            plan.get("cells") or []
        )

        if isinstance(
            png,
            str,
        ):

            dbg = png

        elif not png:

            dbg = (
                "render_png returned None "
                "(no seats parsed)"
            )

        else:

            res = telegram.send_photo(
                chat_id,
                png,
                caption=caption,
            )

            if (
                isinstance(res, dict)
                and res.get("ok")
            ):

                return

            dbg = (
                f"send_photo failed: "
                f"{res}"
            )

    except Exception:

        import traceback

        dbg = (
            "render/upload crash: "
            + traceback.format_exc()[-400:]
        )

    telegram.send_message(
        chat_id,
        f"[img debug] {dbg}",
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
            "Something expired — "
            "try /upcoming again.",
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

    experience = convo.get(
        "experience",
        "any",
    )

    experience_label = convo.get(
        "experienceLabel",
        _experience_label(
            experience
        ),
    )

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
    # Specific-date shortcut
    # -----------------------------------------------------------------------
    #
    # ONLY skip creating a watcher when the selected date itself already has
    # a matching experience.
    #
    # This fixes:
    #
    #   Watch IMAX
    #   Standard is already open
    #   -> should STILL create an IMAX watcher
    #
    # We also only use this shortcut for a single explicit date.
    #
    # For "within 7 days" / "weekend", we KEEP the watcher because some dates
    # may already be open while other dates may still receive new showtimes.

    if (
        date_val != "any"
        and not isinstance(
            date_val,
            list,
        )
    ):

        dates = [date_val]

        already = _dates_already_open(
            chain,
            slug,
            cinemas,
            experience,
            dates,
        )

        if already:

            store.clear_convo(
                chat_id
            )

            telegram.send_message(
                chat_id,
                f"📅 <b>{date_label}</b> "
                f"already has "
                f"<b>{experience_label}</b> "
                f"showtimes open for booking — "
                f"here are the showtimes "
                f"(no watch needed):",
            )

            return show_showtimes(
                chat_id,
                chain,
                slug,
            )

    # -----------------------------------------------------------------------
    # Snapshot currently existing matching showtimes
    # -----------------------------------------------------------------------

    seen_sessions = (
        _initial_seen_sessions(
            chain,
            slug,
            cinemas,
            experience,
            time_filter,
            date_val,
        )
    )

    entry = {

        "chain": chain,

        "movieSlug": convo["slug"],

        "movieTitle": (
            convo["slug"]
            .replace("-", " ")
            .title()
        ),

        "cinemas": cinemas,

        "mode": "release",

        "date": date_val,

        "dateLabel": date_label,

        "experience": experience,

        "experienceLabel": experience_label,

        "timeFilter": time_filter,

        # Showtimes that existed when this watch was created.
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

    telegram.send_message(
        chat_id,
        f"👀 Watching "
        f"<b>{entry['movieTitle']}</b>"
        f"{when}\n"
        f"🎭 Experience: "
        f"<b>{experience_label}</b>\n"
        f"⏰ Time: "
        f"<b>{time_filter}</b>\n\n"
        f"I'll ping you loudly only when "
        f"a <b>new matching showtime</b> "
        f"opens.",
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

        h = secs // 3600

        return (
            f"{h} hour"
            + (
                "s"
                if h != 1
                else ""
            )
        )

    return f"{secs // 60} min"


def cmd_status(chat_id):

    cur = store._get(
        f"status_interval:{chat_id}",
        None,
    )

    telegram.send_message(
        chat_id,
        f"⏱ <b>Watcher updates</b>\n"
        f"I quietly ping you what I'm watching every "
        f"<b>{_fmt_interval(cur)}</b>.\n\n"
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

        msg = (
            "🔕 Watcher updates turned "
            "<b>off</b>. "
            "You'll still get loud alerts "
            "when a movie opens."
        )

    else:

        msg = (
            f"✅ Watcher updates set to every "
            f"<b>{_fmt_interval(secs)}</b>."
        )

    telegram.send_message(
        chat_id,
        msg,
    )


def cmd_list(chat_id):

    wl = store.get_watchlist(
        chat_id
    )

    if not wl:

        return telegram.send_message(
            chat_id,
            "No active watches. "
            "/upcoming to add one.",
        )

    lines = [
        "<b>Your watches:</b>"
    ]

    for i, w in enumerate(
        wl,
        1,
    ):

        cine = (
            "any"
            if w["cinemas"] == "any"
            else ",".join(
                w["cinemas"]
            )
        )

        flag = (
            " ⏰ OPEN"
            if w.get("alerted")
            else ""
        )

        dlabel = w.get(
            "dateLabel",
            "any date",
        )

        experience = w.get(
            "experienceLabel",
            _experience_label(
                w.get(
                    "experience",
                    "any",
                )
            ),
        )

        lines.append(
            f"{i}. "
            f"{w['movieTitle']} "
            f"[{w['chain']}] "
            f"@ {cine} "
            f"· {experience} "
            f"· {dlabel} "
            f"· {w['timeFilter']}"
            f"{flag}"
        )

    lines.append(
        "\n/booked &lt;n&gt; "
        "or /remove &lt;n&gt; to stop one."
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
            "Usage: /%s "
            "&lt;number from /list&gt;"
            % (
                "booked"
                if booked
                else "remove"
            ),
        )

    idx = (
        int(parts[1])
        - 1
    )

    wl = store.get_watchlist(
        chat_id
    )

    if (
        idx < 0
        or idx >= len(wl)
    ):

        return telegram.send_message(
            chat_id,
            "No watch with that number.",
        )

    title = wl[idx][
        "movieTitle"
    ]

    store.remove_watch(
        chat_id,
        wl[idx]["id"],
    )

    _sync_cron()

    verb = (
        "Booked — alerts stopped for"
        if booked
        else "Removed"
    )

    telegram.send_message(
        chat_id,
        f"✅ {verb} "
        f"<b>{title}</b>.",
    )


def _sync_cron():

    """Enable/disable cron based on whether ANY user has an active watch."""

    any_active = False

    for cid in store.all_chat_ids():

        if store.get_watchlist(cid):

            any_active = True
            break

    last = store._get(
        "cron_state",
        None,
    )

    res = cronjob.sync_to_watches(
        any_active,
        last,
    )

    if res.get("changed"):

        store._set(
            "cron_state",
            res["state"],
        )


# ---------------- HTTP handler ----------------
