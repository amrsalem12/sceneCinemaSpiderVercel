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


from . import store, telegram, vox, scene, cronjob  # noqa: E402

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

    if action == "mt":                         # mt:<timeFilter> -> save watch
        return save_watch(chat_id, parts[1])


def show_showtimes(chat_id, chain, slug):
    telegram.send_message(chat_id, "Loading showtimes…")
    try:
        if chain == "vox":
            b = vox.fetch_bundle()
            sess = vox.sessions_for(b, movie_slug=slug, time_filter="any",
                                    only_available=True)
            if not sess:
                return telegram.send_message(chat_id, "No available showtimes found.")
            # show soonest ~12, each a deep-link book button
            rows = [[(f"{s['cinema'][:16]} · {s['experience'][:8]} · {s['time']} "
                      f"({s['seats']} seats)", s["bookingUrl"])] for s in sess[:12]]
            return telegram.send_message(chat_id,
                f"🎬 Showtimes (tap to book on VOX):", buttons=rows)
        else:
            # Scene: needs a date; show today's open days -> simplest: next open day
            days = sorted(scene.open_days(slug))
            if not days:
                return telegram.send_message(chat_id, "No open showtimes yet.")
            sess = scene.sessions_for(slug, days[0], time_filter="any")
            rows = [[(f"{s['experience']} · {s['time']}", s["showtime_url"])]
                    for s in sess[:12]]
            return telegram.send_message(chat_id,
                f"🎬 Scene showtimes {days[0]} (tap to book):", buttons=rows)
    except Exception as e:
        return telegram.send_message(chat_id, f"Couldn't load showtimes: {e}")


def save_watch(chat_id, time_filter):
    convo = store.get_convo(chat_id)
    if not convo.get("slug"):
        return telegram.send_message(chat_id, "Something expired — try /upcoming again.")
    chain = convo["chain"]
    cinema_choice = convo.get("cinemaChoice", "any:any")  # "<chain>:<id>" or "any:any"
    cinemas = "any" if cinema_choice.startswith("any") else [cinema_choice.split(":")[1]]

    entry = {
        "chain": chain,
        "movieSlug": convo["slug"],
        "movieTitle": convo["slug"].replace("-", " ").title(),
        "cinemas": cinemas,
        "mode": "release",
        "date": "any",
        "timeFilter": time_filter,
    }
    store.add_watch(chat_id, entry)
    store.clear_convo(chat_id)

    # auto-enable cron since there's now an active watch
    _sync_cron()
    telegram.send_message(chat_id,
        f"👀 Watching <b>{entry['movieTitle']}</b>. "
        f"I'll ping you (loudly) the moment it opens for booking.")


# ---------------- manage ----------------
def cmd_list(chat_id):
    wl = store.get_watchlist(chat_id)
    if not wl:
        return telegram.send_message(chat_id, "No active watches. /upcoming to add one.")
    lines = ["<b>Your watches:</b>"]
    for i, w in enumerate(wl, 1):
        cine = "any" if w["cinemas"] == "any" else ",".join(w["cinemas"])
        flag = " ⏰ OPEN" if w.get("alerted") else ""
        lines.append(f"{i}. {w['movieTitle']} [{w['chain']}] @ {cine} "
                     f"({w['timeFilter']}){flag}")
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
