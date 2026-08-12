"""
Telegram webhook — the interactive surface of the bot.

Receives commands + button taps from Telegram and drives both modes:

MODE A
  /showing -> browse now-showing -> showtimes -> deep-link book
  -> "Watch for another date" -> watcher setup

MODE B
  /upcoming -> browse coming-soon -> watcher setup

Watcher setup:
  movie -> cinema -> experience -> time filter -> date -> watch

The watcher stores the selected experience(s) so the cron sweep only alerts
when a matching experience gets a NEW showtime.

Manage:
  /list
  /remove <n>
  /booked <n>
  /status

Conversation state lives in KV via store.get_convo/set_convo.
Watchlists are per-user.
"""

import os
import re
import sys
import json


from lib import store, telegram, vox, scene, scene_seats, cronjob  # noqa: E402


JOIN_CODE = os.getenv("JOIN_CODE", "").strip()


# ---------------------------------------------------------------------------
# Cinema choices
# ---------------------------------------------------------------------------

CINEMA_CHOICES = [
    ("VOX Almaza", "vox:000047"),
    ("Scene CFC", "scene:cfc"),
    ("Any (Almaza or Scene)", "any:any"),
]


# ---------------------------------------------------------------------------
# Experience choices
#
# The values are the friendly names returned by vox._session_brief() and
# scene.sessions_for().
#
# "Any experience" means the watcher accepts any experience at that cinema.
# ---------------------------------------------------------------------------

VOX_EXPERIENCES = [
    ("Any experience", "any"),
    ("IMAX", "IMAX"),
    ("Gold", "Gold"),
    ("MAX", "MAX"),
    ("4DX", "4DX"),
    ("Kids", "Kids"),
    ("Standard", "Standard"),
]

SCENE_EXPERIENCES = [
    ("Any experience", "any"),
    ("ScreenX", "ScreenX"),
    ("Premiere", "Premiere"),
    ("Standard", "Standard"),
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


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

def is_member(user_id):
    return str(user_id) in set(
        store._get("allowlist", [])
    )


def add_member(user_id):
    ids = set(
        store._get("allowlist", [])
    )

    ids.add(str(user_id))

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

    chat_id, user_id = ev["chat_id"], ev["user_id"]

    # Private-bot gate
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
        # Telegram may retry a callback while the webhook is still busy
        # fetching VOX/Scene data. Claim it before doing any work so the same
        # tap cannot create duplicate watches or hit a conversation that the
        # first request has already cleared.
        if not store.claim_callback(ev["callback_id"]):
            telegram.answer_callback(ev["callback_id"])
            return

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

    # Scene now-showing is best-effort.
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
        # show:<chain>:<slug>
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
    # Start watcher
    #
    # mark:<chain>:<slug>
    #
    # This is used by /upcoming and by the "Watch for another date" button
    # from /showing.
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
                    c
                ]
                for c in [
                    (n, f"mc:{v}")
                    for n, v in CINEMA_CHOICES
                ]
            ],
        )

    # -----------------------------------------------------------------------
    # Cinema selected -> experience
    # -----------------------------------------------------------------------
    if action == "mc":
        convo = store.get_convo(chat_id)

        cinema_choice = ":".join(parts[1:])

        convo["cinemaChoice"] = cinema_choice
        convo["step"] = "experience"

        store.set_convo(
            chat_id,
            convo,
        )

        chain = convo.get("chain")

        # If user selected a concrete cinema, show only its experiences.
        # If "Any" was selected, show all supported experiences.
        if chain == "vox":
            experiences = VOX_EXPERIENCES
        elif chain == "scene":
            experiences = SCENE_EXPERIENCES
        else:
            experiences = [
                ("Any experience", "any"),
                ("IMAX", "IMAX"),
                ("Gold", "Gold"),
                ("MAX", "MAX"),
                ("4DX", "4DX"),
                ("Kids", "Kids"),
                ("Standard", "Standard"),
                ("ScreenX", "ScreenX"),
                ("Premiere", "Premiere"),
            ]

        return telegram.send_message(
            chat_id,
            "Which theatre experience?",
            buttons=[
                [
                    (name, f"me:{value}")
                ]
                for name, value in experiences
            ],
        )

    # -----------------------------------------------------------------------
    # Experience selected -> time
    # -----------------------------------------------------------------------
    if action == "me":
        convo = store.get_convo(chat_id)

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
                    (n, f"mt:{v}")
                ]
                for n, v in TIME_CHOICES
            ],
        )

    if action == "mt":
        convo = store.get_convo(chat_id)

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
                    (n, f"md:{v}")
                ]
                for n, v in DATE_CHOICES
            ],
        )

    if action == "md":
        return save_watch(
            chat_id,
            parts[1],
        )

    # An existing matching showtime was found while creating the watcher.
    # The user must explicitly choose whether to take it now or ignore it
    # and keep watching for a NEW matching showtime.
    if action == "existing":
        choice = parts[1] if len(parts) > 1 else ""

        if choice == "take":
            return take_existing_showtimes(chat_id)

        if choice == "ignore":
            return ignore_existing_showtimes_and_save(chat_id)

        return telegram.send_message(
            chat_id,
            "Invalid choice. Please start the watcher again.",
        )

    return telegram.send_message(
        chat_id,
        f"(unhandled tap: {data!r})",
    )


# ---------------------------------------------------------------------------
# Date helpers
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


# ---------------------------------------------------------------------------
# Showing -> available days
# ---------------------------------------------------------------------------

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
                else slug.replace("-", " ").title()
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
                d = str(x["displayDate"])

                if d not in seen:
                    seen.add(d)
                    days.append(d)

        else:
            name = slug.replace("-", " ").title()
            cinema = "Scene CFC"

            for ddmm in scene.open_days(slug):
                dd, mm, yyyy = ddmm.split("-")
                days.append(f"{yyyy}{mm}{dd}")

            days.sort()

        if not days:
            return telegram.send_message(
                chat_id,
                f"🎬 <b>{name}</b> — {cinema}\n"
                f"No showtimes yet. Use /upcoming to be pinged when they open.",
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


# ---------------------------------------------------------------------------
# Showing -> one day's showtimes
# ---------------------------------------------------------------------------

def show_day_showtimes(chat_id, chain, slug, yyyymmdd):
    telegram.send_message(
        chat_id,
        "Loading showtimes…",
    )

    daylbl = _daylabel(yyyymmdd)

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
                else slug.replace("-", " ").title()
            )

            sess = vox.sessions_for(
                b,
                movie_slug=slug,
                display_date=int(yyyymmdd),
                time_filter="any",
                only_available=False,
            )

            if not sess:
                return telegram.send_message(
                    chat_id,
                    f"🎬 <b>{name}</b> — VOX Almaza · {daylbl}\n"
                    f"No showtimes for that day.",
                )

            rows = []

            for x in sorted(
                sess,
                key=lambda z: z["showtime"],
            ):
                free = x["seats"] and x["seats"] > 0
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
                            f"🔴 {x['time']} · {exp} — sold out",
                            "soldout",
                        )
                    ])

            return telegram.send_message(
                chat_id,
                f"🎬 <b>{name}</b> — VOX Almaza · {daylbl}\n"
                f"Tap a time to book:",
                buttons=rows,
            )

        # Scene
        name = slug.replace("-", " ").title()

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
                f"🎬 <b>{name}</b> — Scene CFC · {daylbl}\n"
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

            stid = m.group(1) if m else None

            seat_btn = (
                f"🗺 {x['time']} · {x['experience']}",
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
            f"🎬 <b>{name}</b> — Scene CFC · {daylbl}\n"
            f"🗺 = see seats · Book = go to Scene:",
            buttons=rows,
        )

    except Exception as e:
        return telegram.send_message(
            chat_id,
            f"Couldn't load showtimes: {e}",
        )


# ---------------------------------------------------------------------------
# Date choice
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

    def ymd(d):
        return int(
            d.strftime("%Y%m%d")
        )

    if choice == "any":
        return "any", "any date"

    if choice == "today":
        return (
            ymd(now),
            now.strftime("%a %d/%m"),
        )

    if choice == "tomorrow":
        d = now + timedelta(days=1)

        return (
            ymd(d),
            d.strftime("%a %d/%m"),
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
            d.strftime("Fri %d/%m"),
        )

    if choice == "weekend":
        fri = now + timedelta(
            days=(4 - now.weekday()) % 7
        )

        sat = fri + timedelta(days=1)

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

    return "any", "any date"


# ---------------------------------------------------------------------------
# Experience matching
# ---------------------------------------------------------------------------

def _experience_matches(session_experience, wanted_experiences):
    """
    Match a session against the watch's selected experience.

    Backwards compatibility:
      - missing experience field -> any
      - "any" -> any experience
      - string -> single selected experience
      - list -> any selected experience
    """
    if not wanted_experiences:
        return True

    if wanted_experiences == "any":
        return True

    if isinstance(wanted_experiences, str):
        wanted = {wanted_experiences}
    else:
        wanted = set(wanted_experiences)

    if "any" in wanted:
        return True

    return session_experience in wanted


# ---------------------------------------------------------------------------
# Check whether selected date is already open
# ---------------------------------------------------------------------------

def _dates_already_open(
    chain,
    slug,
    cinemas,
    dates,
    experiences="any",
    time_filter="any",
):
    """
    Return only dates that already have a matching showtime.

    IMPORTANT:
    A date being open is NOT enough.

    The date must contain a showtime matching:
      - selected cinema
      - selected experience
      - selected time filter
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
                    time_filter=time_filter,
                    only_available=True,
                )

                if any(
                    _experience_matches(
                        s.get("experience"),
                        experiences,
                    )
                    for s in sessions
                ):
                    open_now.append(d)

        else:
            opendays = scene.open_days(slug)

            for d in dates:
                if scene.to_ddmmyyyy(d) not in opendays:
                    continue

                sessions = scene.sessions_for(
                    slug,
                    scene.to_ddmmyyyy(d),
                    time_filter=time_filter,
                )

                if any(
                    _experience_matches(
                        s.get("experience"),
                        experiences,
                    )
                    for s in sessions
                ):
                    open_now.append(d)

    except Exception:
        pass

    return open_now


# ---------------------------------------------------------------------------
# Stable session ID
# ---------------------------------------------------------------------------

def _session_key(chain, session):
    if chain == "vox":
        return f"vox:{session.get('id') or session.get('bookingUrl')}"

    return f"scene:{session.get('showtime_url')}"


# ---------------------------------------------------------------------------
# Initial snapshot
# ---------------------------------------------------------------------------

def _initial_seen_sessions(
    chain,
    slug,
    cinemas,
    time_filter,
    date_val,
    experiences="any",
):
    """
    Snapshot matching showtimes that ALREADY EXIST when the watch is created.

    A session is considered existing only if it matches:
      - cinema
      - experience
      - time filter
      - date filter

    Therefore an IMAX watcher does NOT remember Gold/Standard sessions as
    matching sessions, and those sessions cannot accidentally satisfy the
    watcher later.
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
                    s for s in sessions
                    if _experience_matches(
                        s.get("experience"),
                        experiences,
                    )
                ]

                seen.extend(
                    _session_key(chain, s)
                    for s in sessions
                )

            else:
                dates = (
                    date_val
                    if isinstance(date_val, list)
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
                        s for s in sessions
                        if _experience_matches(
                            s.get("experience"),
                            experiences,
                        )
                    ]

                    seen.extend(
                        _session_key(chain, s)
                        for s in sessions
                    )

        else:
            if date_val == "any":
                dates = scene.open_days(slug)

            else:
                wanted = (
                    date_val
                    if isinstance(date_val, list)
                    else [date_val]
                )

                wanted_ddmm = {
                    scene.to_ddmmyyyy(d)
                    for d in wanted
                }

                dates = [
                    d
                    for d in scene.open_days(slug)
                    if d in wanted_ddmm
                ]

            for d in dates:
                sessions = scene.sessions_for(
                    slug,
                    d,
                    time_filter=time_filter,
                )

                sessions = [
                    s for s in sessions
                    if _experience_matches(
                        s.get("experience"),
                        experiences,
                    )
                ]

                seen.extend(
                    _session_key(chain, s)
                    for s in sessions
                )

    except Exception:
        return []

    return list(dict.fromkeys(seen))


# ---------------------------------------------------------------------------
# Seat map
# ---------------------------------------------------------------------------

def show_seatmap(chat_id, showtime_id):
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
            f"seatmap crash:\n{traceback.format_exc()[-600:]}",
        )

    if isinstance(plan, dict) and plan.get("error"):
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
        f"pick your seats on Scene."
    )

    dbg = ""

    try:
        png = scene_seats.render_png(
            plan.get("cells") or []
        )

        if isinstance(png, str):
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

            if isinstance(res, dict) and res.get("ok"):
                return

            dbg = f"send_photo failed: {res}"

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
        scene_seats.render_text(plan),
    )


# ---------------------------------------------------------------------------
# Save watcher
# ---------------------------------------------------------------------------

def _experience_label(experiences):
    if experiences == "any":
        return "any experience"

    if isinstance(experiences, list):
        return ", ".join(str(x) for x in experiences)

    return str(experiences)


def _watch_context_from_convo(convo, date_val=None, date_label=None):
    """Build the normalized watcher filters from conversation state."""
    cinema_choice = convo.get(
        "cinemaChoice",
        "any:any",
    )

    cinemas = (
        "any"
        if cinema_choice.startswith("any")
        else [cinema_choice.split(":", 1)[1]]
    )

    return {
        "chain": convo["chain"],
        "slug": convo["slug"],
        "cinemas": cinemas,
        "experiences": convo.get("experience", "any"),
        "timeFilter": convo.get("timeFilter", "any"),
        "date": date_val if date_val is not None else convo.get("dateVal", "any"),
        "dateLabel": date_label if date_label is not None else convo.get("dateLabel", "any date"),
    }


def _matching_sessions_for_setup(ctx):
    """
    Fetch currently available sessions matching the COMPLETE watcher setup.

    This is deliberately shared by the "already open" check, the Take button,
    and the Ignore button. That guarantees all three paths use exactly the
    same cinema + experience + time + date rules.
    """
    chain = ctx["chain"]
    slug = ctx["slug"]
    cinemas = ctx["cinemas"]
    experiences = ctx["experiences"]
    time_filter = ctx["timeFilter"]
    date_val = ctx["date"]

    sessions = []

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
        else:
            dates = (
                date_val
                if isinstance(date_val, list)
                else [date_val]
            )

            for d in dates:
                sessions.extend(
                    vox.sessions_for(
                        bundle,
                        movie_slug=slug,
                        cinemas=cinemas,
                        display_date=int(d),
                        time_filter=time_filter,
                        only_available=True,
                    )
                )

    else:
        if not scene.is_bookable(slug):
            return []

        open_days = scene.open_days(slug)

        if date_val == "any":
            target_days = sorted(open_days)
        else:
            dates = (
                date_val
                if isinstance(date_val, list)
                else [date_val]
            )

            wanted_ddmm = {
                scene.to_ddmmyyyy(d)
                for d in dates
            }

            target_days = [
                d for d in sorted(open_days)
                if d in wanted_ddmm
            ]

        for d in target_days:
            sessions.extend(
                scene.sessions_for(
                    slug,
                    d,
                    time_filter=time_filter,
                )
            )

    return [
        s for s in sessions
        if _experience_matches(
            s.get("experience"),
            experiences,
        )
    ]


def _session_sort_key(chain, session):
    if chain == "vox":
        return (
            str(session.get("displayDate", "")),
            str(session.get("showtime", "")),
        )

    return (
        str(session.get("date", "")),
        str(session.get("time", "")),
    )


def _existing_buttons(chain, sessions):
    """Turn matching existing sessions into normal booking buttons."""
    rows = []

    for session in sorted(
        sessions,
        key=lambda x: _session_sort_key(chain, x),
    )[:10]:
        if chain == "vox":
            date_value = session.get("displayDate")
            date_label = (
                _daylabel(date_value)
                if date_value
                else ""
            )
            label = (
                f"{date_label} · "
                f"{session['time']} · "
                f"{session['experience']}"
            )
            url = session.get("bookingUrl")
        else:
            label = (
                f"{session['time']} · "
                f"{session['experience']}"
            )
            url = session.get("showtime_url")

        if url:
            rows.append([(label, url)])

    return rows


def _make_watch_entry(ctx, seen_sessions):
    return {
        "chain": ctx["chain"],
        "movieSlug": ctx["slug"],
        "movieTitle": ctx["slug"].replace("-", " ").title(),
        "cinemas": ctx["cinemas"],
        "mode": "release",
        "date": ctx["date"],
        "dateLabel": ctx["dateLabel"],
        "timeFilter": ctx["timeFilter"],
        "experiences": ctx["experiences"],
        "seenSessions": list(dict.fromkeys(seen_sessions)),
    }


def _save_watch_from_convo(chat_id, convo, ctx, matching_sessions):
    """Persist the watcher using the current matching sessions as baseline."""
    seen_sessions = [
        _session_key(ctx["chain"], session)
        for session in matching_sessions
    ]

    entry = _make_watch_entry(
        ctx,
        seen_sessions,
    )

    store.add_watch(
        chat_id,
        entry,
    )

    store.clear_convo(chat_id)
    _sync_cron()

    experience_label = _experience_label(
        ctx["experiences"]
    )

    when = (
        ""
        if ctx["date"] == "any"
        else f" for <b>{ctx['dateLabel']}</b>"
    )

    return telegram.send_message(
        chat_id,
        f"👀 Watching <b>{entry['movieTitle']}</b>{when}.\n"
        f"🎭 Experience: <b>{experience_label}</b>\n"
        f"⏰ Time: <b>{ctx['timeFilter']}</b>\n\n"
        f"I'll ping you loudly only when a NEW matching showtime opens."
    )


def _pending_existing_context(chat_id):
    convo = store.get_convo(chat_id)

    if not convo.get("slug"):
        return None, telegram.send_message(
            chat_id,
            "Something expired — try /upcoming again.",
        )

    if not convo.get("pendingExisting"):
        return None, telegram.send_message(
            chat_id,
            "There is no pending existing-showtime choice. Start the watcher again.",
        )

    ctx = _watch_context_from_convo(
        convo,
        convo.get("dateVal", "any"),
        convo.get("dateLabel", "any date"),
    )

    return (convo, ctx), None


def take_existing_showtimes(chat_id):
    """
    User chose TAKE on an already-open matching showtime.

    We fetch again because the showtime could have disappeared between the
    initial check and the button tap. If it is still there, show only sessions
    matching the exact watcher filters.
    """
    pending, error = _pending_existing_context(chat_id)
    if error:
        return error

    convo, ctx = pending

    try:
        sessions = _matching_sessions_for_setup(ctx)
    except Exception as e:
        return telegram.send_message(
            chat_id,
            f"Couldn't refresh the matching showtimes: {e}",
        )

    if not sessions:
        return telegram.send_message(
            chat_id,
            "Those matching showtimes are no longer open. "
            "Choose Ignore if you still want to keep watching.",
            buttons=[
                [("👀 Ignore & keep watching", "existing:ignore")]
            ],
        )

    store.clear_convo(chat_id)

    rows = _existing_buttons(
        ctx["chain"],
        sessions,
    )

    experience_label = _experience_label(
        ctx["experiences"]
    )

    return telegram.send_message(
        chat_id,
        f"🎟 <b>{ctx['slug'].replace('-', ' ').title()}</b> already has "
        f"matching <b>{experience_label}</b> showtimes open.\n"
        f"Tap one to book:",
        buttons=rows,
    )


def ignore_existing_showtimes_and_save(chat_id):
    """
    User chose IGNORE on currently-open matching sessions.

    Those exact matching sessions become the watcher's baseline, so they do
    NOT alert immediately. A later newly-created matching session will alert.
    """
    pending, error = _pending_existing_context(chat_id)
    if error:
        return error

    convo, ctx = pending

    try:
        matching_sessions = _matching_sessions_for_setup(ctx)
    except Exception as e:
        return telegram.send_message(
            chat_id,
            f"Couldn't refresh the watcher state: {e}",
        )

    return _save_watch_from_convo(
        chat_id,
        convo,
        ctx,
        matching_sessions,
    )


def save_watch(chat_id, date_choice):
    convo = store.get_convo(chat_id)

    if not convo.get("slug"):
        return telegram.send_message(
            chat_id,
            "Something expired — try /upcoming again.",
        )

    date_val, date_label = _resolve_date_choice(
        date_choice
    )

    ctx = _watch_context_from_convo(
        convo,
        date_val,
        date_label,
    )

    try:
        matching_sessions = _matching_sessions_for_setup(ctx)
    except Exception as e:
        return telegram.send_message(
            chat_id,
            f"Couldn't check the current matching showtimes: {e}",
        )

    # ---------------------------------------------------------------
    # IMPORTANT:
    # If a matching showtime is ALREADY open, do NOT automatically clear
    # the conversation and do NOT automatically create a watcher.
    #
    # Ask the user:
    #   TAKE   -> show those matching showtimes now
    #   IGNORE -> save them as baseline and keep watching for NEW ones
    #
    # This applies to every experience, not just IMAX, and every date
    # selection, including "within 7 days" and "any date".
    # ---------------------------------------------------------------
    if matching_sessions:
        convo.update({
            "pendingExisting": True,
            "dateVal": date_val,
            "dateLabel": date_label,
        })
        store.set_convo(
            chat_id,
            convo,
        )

        experience_label = _experience_label(
            ctx["experiences"]
        )

        count = len(matching_sessions)
        noun = "showtime" if count == 1 else "showtimes"

        return telegram.send_message(
            chat_id,
            f"📅 <b>{date_label}</b> already has "
            f"{count} matching <b>{experience_label}</b> {noun} "
            f"open for booking.\n\n"
            f"Do you want to take one of those now, or ignore them "
            f"and keep the watcher active for a NEW matching showtime?",
            buttons=[
                [("🎟 Take existing showtimes", "existing:take")],
                [("👀 Ignore & keep watching", "existing:ignore")],
            ],
        )

    # Nothing matching is open right now, so the watcher can be created
    # immediately with an empty baseline.
    return _save_watch_from_convo(
        chat_id,
        convo,
        ctx,
        [],
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
            + ("s" if h != 1 else "")
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
                ("Every 30 min", "statusiv:1800"),
                ("Every 1 hour", "statusiv:3600"),
            ],
            [
                ("Every 3 hours", "statusiv:10800"),
                ("Off", "statusiv:0"),
            ],
        ],
    )


def set_status_interval(chat_id, secs):
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
            "🔕 Watcher updates turned <b>off</b>. "
            "You'll still get loud alerts when a matching showtime opens."
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


def _experience_label(w):
    experiences = w.get(
        "experiences",
        w.get("experience", "any"),
    )

    if experiences == "any":
        return "any experience"

    if isinstance(experiences, list):
        return ", ".join(experiences)

    return str(experiences)


def cmd_list(chat_id):
    wl = store.get_watchlist(chat_id)

    if not wl:
        return telegram.send_message(
            chat_id,
            "No active watches. /upcoming to add one.",
        )

    lines = [
        "<b>Your watches:</b>"
    ]

    for i, w in enumerate(wl, 1):
        cine = (
            "any"
            if w["cinemas"] == "any"
            else ",".join(w["cinemas"])
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

        lines.append(
            f"{i}. {w['movieTitle']} [{w['chain']}] @ {cine} "
            f"· 🎭 {_experience_label(w)} "
            f"· {dlabel} · {w['timeFilter']}{flag}"
        )

    lines.append(
        "\n/booked &lt;n&gt; or /remove &lt;n&gt; to stop one."
    )

    telegram.send_message(
        chat_id,
        "\n".join(lines),
    )


def cmd_stop(chat_id, text, booked):
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

    idx = int(parts[1]) - 1

    wl = store.get_watchlist(chat_id)

    if idx < 0 or idx >= len(wl):
        return telegram.send_message(
            chat_id,
            "No watch with that number.",
        )

    title = wl[idx]["movieTitle"]

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
        f"✅ {verb} <b>{title}</b>.",
    )


# ---------------------------------------------------------------------------
# Cron synchronization
# ---------------------------------------------------------------------------

def _sync_cron():
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


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def handler(request):
    """
    Vercel entry point.

    Kept intentionally compatible with the existing project structure.
    """
    try:
        body = request.get_json(silent=True)

        if body:
            handle_update(body)

        return {
            "statusCode": 200,
            "body": "ok",
        }

    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": repr(e),
            }),
        }
