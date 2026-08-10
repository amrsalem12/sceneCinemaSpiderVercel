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
        telegram.answer_callback(ev["callback_id"])
        return handle_callback(chat_id, ev["data"])

    text = ev["text"]
    if text.startswith("/start"):
        telegram.send_message(chat_id,
            "🎬 Cinema bot.\n\n"
            "/showing — what's on now (book)\n"
            "/upcoming — watch a coming-soon movie\n"
            "/list — your watches\n"
            "/remove &lt;n&gt; — stop a watch\n"
            "/booked &lt;n&gt; — mark booked, stop alerts")
    elif text.startswith("/showing"):
        cmd_showing(chat_id)
    elif text.startswith("/upcoming"):
        cmd_upcoming(chat_id)
    elif text.startswith("/list"):
        cmd_list(chat_id)
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

    if action == "show":                       # show:<chain>:<slug>
        return show_showtimes(chat_id, parts[1], parts[2])

    if action == "seatmap":                    # seatmap:<showtimeId> (Scene, read-only)
        return show_seatmap(chat_id, parts[1])

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


def _send_movie_card(chat_id, m):
    """Poster + details caption + trailer button."""
    bits = []
    if m.get("rating"): bits.append(m["rating"])
    if m.get("genre"): bits.append(m["genre"])
    if m.get("runtime"): bits.append(f"{m['runtime']} min")
    meta = " · ".join(bits)
    lang = ""
    if m.get("language"):
        lang = f"\n🗣 {m['language']}"
        if m.get("subtitles"): lang += f" · sub {m['subtitles']}"
    caption = f"🎬 <b>{m['title']}</b>"
    if meta: caption += f"\n{meta}"
    caption += lang
    buttons = None
    if m.get("trailerUrl"):
        buttons = [[("▶️ Trailer", m["trailerUrl"])]]
    try:
        telegram.send_photo(chat_id, m["posterUrl"], caption=caption, buttons=buttons)
    except Exception:
        telegram.send_message(chat_id, caption, buttons=buttons)


def _seat_label(seats):
    """Human seat-availability label shown under each showtime."""
    if seats is None:
        return ""
    if seats <= 0:
        return "🔴 SOLD OUT"
    if seats <= 5:
        return f"🟠 {seats} left"
    if seats <= 20:
        return f"🟡 {seats} seats"
    return f"🟢 {seats} seats"


def show_showtimes(chat_id, chain, slug):
    telegram.send_message(chat_id, "Loading showtimes…")
    try:
        if chain == "vox":
            b = vox.fetch_bundle()
            # include sold-out too (only_available=False) so we can SHOW them as booked
            sess = vox.sessions_for(b, movie_slug=slug, time_filter="any",
                                    only_available=False)
            movie = vox.find_movie(b, slug=slug)
            name = movie["title"] if movie else slug.replace("-", " ").title()

            # rich movie card (poster + details + trailer) before showtimes
            if movie and movie.get("posterUrl"):
                _send_movie_card(chat_id, movie)

            if not sess:
                # movie exists but no sessions at all
                return telegram.send_message(
                    chat_id,
                    f"🎬 <b>{name}</b>\n\nNo showtimes yet at VOX Almaza. "
                    f"Want me to watch it and ping you when they open? Use /upcoming.")

            # group by date for a cleaner read
            from collections import OrderedDict
            by_date = OrderedDict()
            for x in sess:
                by_date.setdefault(x["displayDate"], []).append(x)

            lines = [f"🎬 <b>{name}</b> — VOX Almaza", ""]
            rows = []
            shown = 0
            # experience display order
            EXP_ORDER = ["IMAX", "4DX", "MAX", "Gold", "Kids", "Standard"]
            for d, items in list(by_date.items())[:4]:          # up to 4 days
                ds = str(d)
                lines.append(f"📅 <b>{ds[6:8]}/{ds[4:6]}</b>")
                # group this date's sessions by experience (screen type)
                from collections import OrderedDict
                by_exp = OrderedDict()
                for x in items:
                    by_exp.setdefault(x["experience"], []).append(x)
                ordered = sorted(by_exp.items(),
                                 key=lambda kv: (EXP_ORDER.index(kv[0])
                                                 if kv[0] in EXP_ORDER else 99))
                for exp, xs in ordered:
                    lines.append(f"  <b>{exp}</b>")
                    for x in sorted(xs, key=lambda z: z["showtime"]):
                        lab = _seat_label(x["seats"])
                        lines.append(f"     • {x['time']} — {lab}")
                        if x["seats"] and x["seats"] > 0 and shown < 12:
                            rows.append([(f"Book {x['time']} · {exp[:6]}",
                                          x["bookingUrl"])])
                            shown += 1
                lines.append("")
            lines.append("Tap a button below to book, or watch for a date "
                         "that isn't open yet.")
            rows.append([("⏰ Watch for another date", f"mark:vox:{slug}")])
            return telegram.send_message(chat_id, "\n".join(lines),
                                         buttons=rows or None)

        else:  # scene
            days = sorted(scene.open_days(slug))
            name = slug.replace("-", " ").title()
            if not days:
                return telegram.send_message(
                    chat_id,
                    f"🎬 <b>{name}</b>\n\nNo showtimes open yet at Scene CFC. "
                    f"Use /upcoming to be notified when they open.")
            sess = scene.sessions_for(slug, days[0], time_filter="any")
            if not sess:
                return telegram.send_message(
                    chat_id, f"🎬 <b>{name}</b> — {days[0]}: no showtimes listed.")
            lines = [f"🎬 <b>{name}</b> — Scene CFC · {days[0]}", ""]
            rows = []
            for x in sess[:8]:
                lines.append(f"   • {x['time']} · {x['experience']}")
                # showtime id is the tail of the showtime_url (…/showtime-<id>)
                stid = x["showtime_url"].rstrip("/").split("-")[-1]
                rows.append([
                    (f"🗺 Seats {x['time']}", f"seatmap:{stid}"),
                    (f"Book {x['time'][:5]}", x["showtime_url"]),
                ])
            lines.append("\nTap 🗺 to see the seat map, or Book to go to Scene. "
                         "You can also watch for a date that isn't open yet.")
            rows.append([("⏰ Watch for another date", f"mark:scene:{slug}")])
            return telegram.send_message(chat_id, "\n".join(lines), buttons=rows)
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
    """Scene only: fetch + render the live seat map (read-only, holds nothing)."""
    telegram.send_message(chat_id, "Loading seat map…")
    try:
        plan = scene_seats.fetch_seat_plan(showtime_id)
        if not plan:
            return telegram.send_message(chat_id,
                "Couldn't load the seat map for that showtime.")
        telegram.send_message(chat_id, scene_seats.render_text(plan))
    except Exception as e:
        telegram.send_message(chat_id, f"Seat map unavailable: {e}")


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
