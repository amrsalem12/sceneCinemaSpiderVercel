"""
Telegram webhook — the interactive surface of the bot.

Receives commands + button taps from Telegram and drives both modes:
  MODE A  /showing   -> browse now-showing (both chains) -> showtimes -> deep-link book
  MODE B  /upcoming  -> browse coming-soon -> mark flow (cinema, time filter) -> watch
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

# Date choices resolve to concrete YYYYMMDD (or "any") at save time.
DATE_CHOICES = [
    ("Any date", "any"),
    ("Today", "today"),
    ("Tomorrow", "tomorrow"),
    ("This Friday", "friday"),
    ("This weekend", "weekend"),
    ("Within 7 days", "week"),
]


# ---------------- access control ----------------
def is_member(user_id):
    return str(user_id) in set(store._get("allowlist", []))


def add_member(user_id):
    ids = set(store._get("allowlist", []))
    ids.add(str(user_id))
    store._set("allowlist", list(ids))


# ---------------- entry point ----------------
def handle_update(update):
    ev = telegram.parse_update(update)
    if not ev:
        return
    chat_id, user_id = ev["chat_id"], ev["user_id"]

    # gate: non-members must send the join code first
    if not is_member(user_id):
        if ev["kind"] == "message" and JOIN_CODE and ev["text"] == JOIN_CODE:
            add_member(user_id)
            telegram.send_message(chat_id, "✅ You're in! Try /showing or /upcoming.")
        else:
            telegram.send_message(chat_id,
                "🔒 This bot is private. Send the access code to join.")
        return

    if ev["kind"] == "callback":
        toast = "Sold out" if ev["data"] == "soldout" else None
        telegram.answer_callback(ev["callback_id"], text=toast)
        return handle_callback(chat_id, ev["data"])

    text = ev["text"]
    if text.startswith("/start"):
        telegram.send_message(chat_id,
            "🎬 Cinema bot.\n\n"
            "/showing — what's on now (book)\n"
            "/upcoming — watch a coming-soon movie\n"
            "/list — your watches\n"
            "/status — how often I ping what I'm watching\n"
            "/remove &lt;n&gt; — stop a watch\n"
            "/booked &lt;n&gt; — mark booked, stop alerts")
    elif text.startswith("/showing"):
        cmd_showing(chat_id)
    elif text.startswith("/upcoming"):
        cmd_upcoming(chat_id)
    elif text.startswith("/list"):
        cmd_list(chat_id)
    elif text.startswith("/status"):
        cmd_status(chat_id)
    elif text.startswith("/remove"):
        cmd_stop(chat_id, text, booked=False)
    elif text.startswith("/booked"):
        cmd_stop(chat_id, text, booked=True)
    else:
        telegram.send_message(chat_id, "Try /showing or /upcoming.")


# ---------------- MODE A: browse now-showing ----------------
def cmd_showing(chat_id):
    telegram.send_message(chat_id, "Fetching what's on…")
    rows = []
    try:
        b = vox.fetch_bundle()
        for m in vox.now_showing(b):
            rows.append([(f"🎬 {m['title'][:40]}", f"show:vox:{m['slug']}")])
    except Exception as e:
        telegram.send_message(chat_id, f"(VOX unavailable: {e})")
    # Scene now-showing is best-effort (listing page structure unverified)
    try:
        for m in scene.now_showing()[:15]:
            rows.append([(f"🎬 {m['title'][:40]} (Scene)", f"show:scene:{m['slug']}")])
    except Exception:
        pass
    if not rows:
        telegram.send_message(chat_id, "Couldn't load movies right now.")
        return
    telegram.send_message(chat_id, "Now showing — pick a movie:", buttons=rows)


# ---------------- MODE B: browse coming-soon ----------------
def cmd_upcoming(chat_id):
    telegram.send_message(chat_id, "Fetching coming soon…")
    rows = []
    try:
        b = vox.fetch_bundle()
        for m in vox.coming_soon(b):
            rows.append([(f"🔜 {m['title'][:40]}", f"mark:vox:{m['slug']}")])
    except Exception as e:
        telegram.send_message(chat_id, f"(VOX unavailable: {e})")
    try:
        for m in scene.coming_soon()[:15]:
            rows.append([(f"🔜 {m['title'][:40]} (Scene)", f"mark:scene:{m['slug']}")])
    except Exception:
        pass
    if not rows:
        telegram.send_message(chat_id, "No coming-soon movies found.")
        return
    telegram.send_message(chat_id, "Coming soon — tap one to watch it:", buttons=rows)


# ---------------- callback router ----------------
def handle_callback(chat_id, data):
    parts = data.split(":")
    action = parts[0]

    if action == "show":                       # show:<chain>:<slug> -> day picker
        return show_showtimes(chat_id, parts[1], parts[2])

    if action == "day":                        # day:<chain>:<slug>:<yyyymmdd>
        return show_day_showtimes(chat_id, parts[1], parts[2], parts[3])

    if action == "statusiv":                   # statusiv:<seconds> (0 = off)
        return set_status_interval(chat_id, int(parts[1]))

    if action == "seatmap":                    # seatmap:<showtimeId> (Scene, read-only)
        # tolerate ids that themselves contain ':' by rejoining the tail
        return show_seatmap(chat_id, ":".join(parts[1:]))

    if action == "soldout":                    # tapped a sold-out time — do nothing
        return

    if action == "mark":                       # mark:<chain>:<slug> -> ask cinema
        store.set_convo(chat_id, {"chain": parts[1], "slug": parts[2],
                                  "step": "cinema"})
        return telegram.send_message(chat_id, "Which cinema?",
                                     buttons=[[c] for c in
                                              [(n, f"mc:{v}") for n, v in CINEMA_CHOICES]])

    if action == "mc":                         # mc:<chain>:<cinemaId> -> ask time
        convo = store.get_convo(chat_id)
        convo["cinemaChoice"] = ":".join(parts[1:])
        convo["step"] = "time"
        store.set_convo(chat_id, convo)
        return telegram.send_message(chat_id, "Which showtimes?",
                                     buttons=[[(n, f"mt:{v}")] for n, v in TIME_CHOICES])

    if action == "mt":                         # mt:<timeFilter> -> ask date
        convo = store.get_convo(chat_id)
        convo["timeFilter"] = parts[1]
        convo["step"] = "date"
        store.set_convo(chat_id, convo)
        return telegram.send_message(chat_id, "Which date?",
                                     buttons=[[(n, f"md:{v}")] for n, v in DATE_CHOICES])

    if action == "md":                         # md:<dateChoice> -> save watch
        return save_watch(chat_id, parts[1])

    # nothing matched -> say so instead of silently doing nothing
    return telegram.send_message(chat_id, f"(unhandled tap: {data!r})")


def _daylabel(yyyymmdd):
    """'20260810' -> 'Sun 10/08'."""
    from datetime import datetime
    try:
        return datetime.strptime(str(yyyymmdd), "%Y%m%d").strftime("%a %d/%m")
    except Exception:
        return str(yyyymmdd)


def show_showtimes(chat_id, chain, slug):
    """Step 1: after a film is picked, ask WHICH DAY — offering only days that
    actually have showtimes. Picking a day fires day:<chain>:<slug>:<yyyymmdd>."""
    telegram.send_message(chat_id, "Checking available days…")
    try:
        days = []  # list of yyyymmdd strings, ascending
        if chain == "vox":
            b = vox.fetch_bundle()
            movie = vox.find_movie(b, slug=slug)
            name = movie["title"] if movie else slug.replace("-", " ").title()
            cinema = "VOX Almaza"
            seen = set()
            for x in sorted(vox.sessions_for(b, movie_slug=slug, time_filter="any",
                                             only_available=False),
                            key=lambda z: z["displayDate"]):
                d = str(x["displayDate"])
                if d not in seen:
                    seen.add(d)
                    days.append(d)
        else:  # scene — open_days returns 'DD-MM-YYYY'
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
                f"No showtimes yet. Use /upcoming to be pinged when they open.")

        rows = [[(_daylabel(d), f"day:{chain}:{slug}:{d}")] for d in days[:10]]
        rows.append([("⏰ Watch for another date", f"mark:{chain}:{slug}")])
        return telegram.send_message(
            chat_id, f"🎬 <b>{name}</b> — {cinema}\nWhich day do you want to go?",
            buttons=rows)
    except Exception as e:
        return telegram.send_message(chat_id, f"Couldn't load days: {e}")


def show_day_showtimes(chat_id, chain, slug, yyyymmdd):
    """Step 2: show one day's showtimes as tap-to-book buttons
    (VOX marks sold-out; Scene adds a 🗺 seat-map button per show)."""
    telegram.send_message(chat_id, "Loading showtimes…")
    daylbl = _daylabel(yyyymmdd)
    try:
        if chain == "vox":
            b = vox.fetch_bundle()
            movie = vox.find_movie(b, slug=slug)
            name = movie["title"] if movie else slug.replace("-", " ").title()
            sess = vox.sessions_for(b, movie_slug=slug, display_date=int(yyyymmdd),
                                    time_filter="any", only_available=False)
            if not sess:
                return telegram.send_message(
                    chat_id, f"🎬 <b>{name}</b> — VOX Almaza · {daylbl}\n"
                             f"No showtimes for that day.")
            rows = []
            for x in sorted(sess, key=lambda z: z["showtime"]):
                free = x["seats"] and x["seats"] > 0
                exp = x["experience"]
                if free:
                    rows.append([(f"{x['time']} · {exp}", x["bookingUrl"])])
                else:
                    rows.append([(f"🔴 {x['time']} · {exp} — sold out", "soldout")])
            return telegram.send_message(
                chat_id,
                f"🎬 <b>{name}</b> — VOX Almaza · {daylbl}\nTap a time to book:",
                buttons=rows)

        else:  # scene
            name = slug.replace("-", " ").title()
            ddmm = scene.to_ddmmyyyy(int(yyyymmdd))          # -> 'DD-MM-YYYY'
            sess = scene.sessions_for(slug, ddmm, time_filter="any")
            if not sess:
                return telegram.send_message(
                    chat_id, f"🎬 <b>{name}</b> — Scene CFC · {daylbl}\n"
                             f"No showtimes for that day.")
            rows = []
            for x in sess[:10]:
                raw = x["showtime_url"].split("?")[0].rstrip("/")
                m = re.search(r"(?:showtime|booking)-([0-9a-f]{24})", raw)
                stid = m.group(1) if m else None
                seat_btn = (f"🗺 {x['time']} · {x['experience']}",
                            f"seatmap:{stid}" if stid else "seatmap:BADID")
                rows.append([seat_btn, ("Book", x["showtime_url"])])
            return telegram.send_message(
                chat_id,
                f"🎬 <b>{name}</b> — Scene CFC · {daylbl}\n"
                f"🗺 = see seats · Book = go to Scene:",
                buttons=rows)
    except Exception as e:
        return telegram.send_message(chat_id, f"Couldn't load showtimes: {e}")


def _resolve_date_choice(choice):
    """Turn a date choice into (dateValue, humanLabel).
    dateValue is 'any', a single YYYYMMDD int, or a list of YYYYMMDD ints
    (for weekend / within-7-days ranges)."""
    from datetime import datetime, timedelta
    now = datetime.utcnow() + timedelta(hours=int(os.getenv("TZ_OFFSET", "3")))
    def ymd(d): return int(d.strftime("%Y%m%d"))
    if choice == "any":
        return "any", "any date"
    if choice == "today":
        return ymd(now), now.strftime("%a %d/%m")
    if choice == "tomorrow":
        d = now + timedelta(days=1)
        return ymd(d), d.strftime("%a %d/%m")
    if choice == "friday":
        # next Friday (weekday 4), including today if it's Friday
        ahead = (4 - now.weekday()) % 7
        d = now + timedelta(days=ahead)
        return ymd(d), d.strftime("Fri %d/%m")
    if choice == "weekend":
        # upcoming Fri+Sat (Egypt weekend)
        fri = now + timedelta(days=(4 - now.weekday()) % 7)
        sat = fri + timedelta(days=1)
        return [ymd(fri), ymd(sat)], "this weekend"
    if choice == "week":
        return [ymd(now + timedelta(days=i)) for i in range(7)], "within 7 days"
    return "any", "any date"


def _dates_already_open(chain, slug, cinemas, dates):
    """Return the subset of `dates` (YYYYMMDD ints) that already have showtimes."""
    open_now = []
    try:
        if chain == "vox":
            b = vox.fetch_bundle()
            for d in dates:
                if vox.sessions_for(b, movie_slug=slug, cinemas=cinemas,
                                    display_date=d, time_filter="any"):
                    open_now.append(d)
        else:  # scene
            opendays = scene.open_days(slug)
            for d in dates:
                if scene.to_ddmmyyyy(d) in opendays:
                    open_now.append(d)
    except Exception:
        pass          # if the check fails, fall through and just set the watch
    return open_now


def show_seatmap(chat_id, showtime_id):
    """Scene only: fetch + render the live seat map (read-only, holds nothing).
    Sends a PNG (phone-friendly); falls back to the text grid if image render fails.

    While debugging: reports WHY it failed instead of a generic message.
    Once it works, trim the debug branches back to a plain fallback.
    """
    telegram.send_message(chat_id, f"Loading seat map… (id={showtime_id})")
    try:
        plan = scene_seats.fetch_seat_plan(showtime_id)
    except Exception:
        import traceback
        return telegram.send_message(
            chat_id, f"seatmap crash:\n{traceback.format_exc()[-600:]}")

    if isinstance(plan, dict) and plan.get("error"):
        return telegram.send_message(chat_id, f"seatmap debug: {plan['error']}")

    if not plan or not plan.get("rows"):
        return telegram.send_message(chat_id, f"seatmap debug: empty plan -> {plan!r}")

    caption = f"🟩 {plan['free']} free · ⬛ {plan['taken']} taken — pick your seats on Scene."
    # try the image first; fall back to the text grid if PIL/render/upload fails.
    # (debug: report the reason so we can see why the image didn't send)
    dbg = ""
    try:
        png = scene_seats.render_png(plan.get("cells") or [])
        if isinstance(png, str):            # 'ERR: ...' diagnostic
            dbg = png
        elif not png:
            dbg = "render_png returned None (no seats parsed)"
        else:
            res = telegram.send_photo(chat_id, png, caption=caption)
            if isinstance(res, dict) and res.get("ok"):
                return
            dbg = f"send_photo failed: {res}"
    except Exception:
        import traceback
        dbg = "render/upload crash: " + traceback.format_exc()[-400:]

    telegram.send_message(chat_id, f"[img debug] {dbg}")
    telegram.send_message(chat_id, scene_seats.render_text(plan))


def save_watch(chat_id, date_choice):
    convo = store.get_convo(chat_id)
    if not convo.get("slug"):
        return telegram.send_message(chat_id, "Something expired — try /upcoming again.")
    chain = convo["chain"]
    cinema_choice = convo.get("cinemaChoice", "any:any")  # "<chain>:<id>" or "any:any"
    cinemas = "any" if cinema_choice.startswith("any") else [cinema_choice.split(":")[1]]
    time_filter = convo.get("timeFilter", "any")
    date_val, date_label = _resolve_date_choice(date_choice)
    slug = convo["slug"]

    # If a SPECIFIC date is chosen and it's ALREADY open, don't set a pointless
    # watch — just show those showtimes now.
    if date_val != "any":
        dates = date_val if isinstance(date_val, list) else [date_val]
        already = _dates_already_open(chain, slug, cinemas, dates)
        if already:
            store.clear_convo(chat_id)
            telegram.send_message(chat_id,
                f"📅 <b>{date_label}</b> is already open for booking — "
                f"here are the showtimes (no watch needed):")
            return show_showtimes(chat_id, chain, slug)

    entry = {
        "chain": chain,
        "movieSlug": convo["slug"],
        "movieTitle": convo["slug"].replace("-", " ").title(),
        "cinemas": cinemas,
        "mode": "release",
        "date": date_val,
        "dateLabel": date_label,
        "timeFilter": time_filter,
    }
    store.add_watch(chat_id, entry)
    store.clear_convo(chat_id)

    _sync_cron()
    when = "" if date_val == "any" else f" for <b>{date_label}</b>"
    telegram.send_message(chat_id,
        f"👀 Watching <b>{entry['movieTitle']}</b>{when}. "
        f"I'll ping you (loudly) the moment those showtimes open.")


# ---------------- manage ----------------
DEFAULT_STATUS_SEC = 3600   # matches logic_check default when unset


def _fmt_interval(secs):
    """Human label for a status interval in seconds. None -> default; 0 -> off."""
    if secs is None:
        return "1 hour (default)"
    secs = int(secs)
    if secs <= 0:
        return "off"
    if secs % 3600 == 0:
        h = secs // 3600
        return f"{h} hour" + ("s" if h != 1 else "")
    return f"{secs // 60} min"


def cmd_status(chat_id):
    """Show current heartbeat interval + tappable options to change it."""
    cur = store._get(f"status_interval:{chat_id}", None)
    telegram.send_message(
        chat_id,
        f"⏱ <b>Watcher updates</b>\n"
        f"I quietly ping you what I'm watching every <b>{_fmt_interval(cur)}</b>.\n\n"
        f"How often would you like them?",
        buttons=[
            [("Every 30 min", "statusiv:1800"), ("Every 1 hour", "statusiv:3600")],
            [("Every 3 hours", "statusiv:10800"), ("Off", "statusiv:0")],
        ])


def set_status_interval(chat_id, secs):
    store._set(f"status_interval:{chat_id}", int(secs))
    store._set(f"status_ts:{chat_id}", 0)      # reset so the change takes effect next sweep
    if secs <= 0:
        msg = "🔕 Watcher updates turned <b>off</b>. You'll still get loud alerts when a movie opens."
    else:
        msg = f"✅ Watcher updates set to every <b>{_fmt_interval(secs)}</b>."
    telegram.send_message(chat_id, msg)


def cmd_list(chat_id):
    wl = store.get_watchlist(chat_id)
    if not wl:
        return telegram.send_message(chat_id, "No active watches. /upcoming to add one.")
    lines = ["<b>Your watches:</b>"]
    for i, w in enumerate(wl, 1):
        cine = "any" if w["cinemas"] == "any" else ",".join(w["cinemas"])
        flag = " ⏰ OPEN" if w.get("alerted") else ""
        dlabel = w.get("dateLabel", "any date")
        lines.append(f"{i}. {w['movieTitle']} [{w['chain']}] @ {cine} "
                     f"· {dlabel} · {w['timeFilter']}{flag}")
    lines.append("\n/booked &lt;n&gt; or /remove &lt;n&gt; to stop one.")
    telegram.send_message(chat_id, "\n".join(lines))


def cmd_stop(chat_id, text, booked):
    parts = text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return telegram.send_message(chat_id, "Usage: /%s &lt;number from /list&gt;"
                                     % ("booked" if booked else "remove"))
    idx = int(parts[1]) - 1
    wl = store.get_watchlist(chat_id)
    if idx < 0 or idx >= len(wl):
        return telegram.send_message(chat_id, "No watch with that number.")
    title = wl[idx]["movieTitle"]
    store.remove_watch(chat_id, wl[idx]["id"])
    _sync_cron()
    verb = "Booked — alerts stopped for" if booked else "Removed"
    telegram.send_message(chat_id, f"✅ {verb} <b>{title}</b>.")


def _sync_cron():
    """Enable/disable cron based on whether ANY user has an active watch."""
    any_active = False
    for cid in store.all_chat_ids():
        if store.get_watchlist(cid):
            any_active = True
            break
    last = store._get("cron_state", None)
    res = cronjob.sync_to_watches(any_active, last)
    if res.get("changed"):
        store._set("cron_state", res["state"])


# ---------------- HTTP handler ----------------
