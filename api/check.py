#!/usr/bin/env python3
"""
Shared logic for the Scene CFC watcher (importable by CLI and serverless).

Checks whether a target day is open for booking yet and, if so, sends a
Telegram alert with tappable "Book" buttons (one per showtime). A day
counts as "open" the moment it appears in the site's calendar strip.
Showtimes are grouped by experience (ScreenX / Premiere / Standard).

Config via environment variables:
  TELEGRAM_TOKEN    bot token from @BotFather (leave empty to just print)
  TELEGRAM_CHAT_ID  your chat id (from getUpdates)
  TARGET_DATE       optional DD-MM-YYYY to lock a day; default = next Thursday
  ONLY_AFTER_5PM    "1" (default) keep only shows at/after 5pm (+ 12-4am late shows)
  ALERT_REPEAT      how many times to buzz per run (default "3")
  ALERT_INTERVAL    seconds between those buzzes (default "3")

Exit code 0 = open, 1 = not open yet.
"""

import os
import re
import sys
import time
from datetime import date, datetime, timedelta

import requests

MOVIE_URL = "https://cfc.scenecinemas.com/movie-details/spider-man-brand-new-day.html"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
}

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TARGET_DATE = os.getenv("TARGET_DATE", "").strip()
ONLY_AFTER_5PM = os.getenv("ONLY_AFTER_5PM", "1") == "1"
ALERT_REPEAT = int(os.getenv("ALERT_REPEAT", "5"))
ALERT_INTERVAL = int(os.getenv("ALERT_INTERVAL", "3"))
HEARTBEAT = os.getenv("HEARTBEAT", "1") == "1"          # hourly "still alive" ping
TZ_OFFSET = int(os.getenv("TZ_OFFSET", "3"))            # Cairo = UTC+3 (summer)


# Each experience is a <div class="ex_<key>_content"> block on the page.
EXPERIENCES = [
    ("ScreenX", "ex_imax_content"),
    ("Premiere", "ex_vip_content"),
    ("Standard", "ex_stand_content"),
]


def target_day() -> str:
    if TARGET_DATE:
        return TARGET_DATE
    today = date.today()                       # Thursday = weekday 3
    thu = today + timedelta(days=(3 - today.weekday()) % 7)
    return thu.strftime("%d-%m-%Y")


def fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.text


def open_days(page_html: str) -> set:
    return set(re.findall(r"data-(\d{2}-\d{2}-\d{4})", page_html))


def _shows_in(block_html: str) -> list:
    """Return [(time, absolute_url), ...] for one experience block."""
    pairs = re.findall(
        r'href="([^"]*showtime-[a-f0-9]+)">\s*(\d{1,2}:\d{2}\s*[AP]M)', block_html)
    out = []
    for url, t in pairs:
        if ONLY_AFTER_5PM and not after_5pm(t):
            continue
        out.append((t.strip(), requests.compat.urljoin(MOVIE_URL, url)))
    return out


def parse_showtimes(day_html: str) -> dict:
    """Return {experience_name: [(time, url), ...]} for experiences with shows."""
    if "no showtimes" in day_html.lower():
        return {}
    result = {}
    for name, css_class in EXPERIENCES:
        m = re.search(
            rf'{css_class}(.*?)(?=ex_(?:imax|vip|stand)_content|$)',
            day_html, re.DOTALL,
        )
        shows = _shows_in(m.group(1)) if m else []
        if shows:
            result[name] = shows
    return result


def after_5pm(t: str) -> bool:
    """Keep evening + late-night shows. Midnight-4 AM count as the same night."""
    try:
        hour = datetime.strptime(t.strip(), "%I:%M %p").hour
    except ValueError:
        return False
    return hour >= 17 or hour < 5   # 5 PM onward, plus 12-4:59 AM late shows


def build_message(target: str, by_experience: dict):
    """Return (text, inline_keyboard rows)."""
    lines = [f"🎬 Scene CFC — {target} is OPEN", "Spider-Man: Brand New Day", ""]
    keyboard = []
    for name, shows in by_experience.items():
        lines.append(f"{name}: " + ", ".join(t for t, _ in shows))
        row = []
        for t, url in shows:
            row.append({"text": f"{name} · {t}", "url": url})
            if len(row) == 2:               # 2 buttons per row
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
    return "\n".join(lines), keyboard


def notify(text: str, keyboard) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_notification": False,       # play a sound
    }
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    for i in range(max(1, ALERT_REPEAT)):
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload, timeout=20,
        )
        if i < ALERT_REPEAT - 1:
            time.sleep(ALERT_INTERVAL)


def send_plain(text: str) -> None:
    """One-off Telegram message, no buttons, no burst (for the heartbeat)."""
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
              "disable_notification": True},   # quiet: it's just a status ping
        timeout=20,
    )


def local_now():
    return datetime.utcnow() + timedelta(hours=TZ_OFFSET)


def maybe_heartbeat(is_open: bool, target: str) -> bool:
    """Send an hourly status ping. Returns True if it sent one this run."""
    if not HEARTBEAT:
        return False
    now = local_now()
    # only the run that lands in the first 5 min of the hour sends it
    if now.minute >= 5:
        return False
    stamp = now.strftime("%H:%M")
    if is_open:
        send_plain(f"\u2705 {stamp} - checked: {target} is OPEN (see the alert above)")
    else:
        send_plain(f"\U0001F50D {stamp} - checked: {target} not open yet, still watching")
    return True


# ------------------------- VOX read-endpoint probe (temporary) ----------------
import gzip as _gzip, json as _json2
from urllib.request import Request as _Req, urlopen as _urlopen

_VOX_APP_HEADERS = {
    "Application-Name": "VOX Android Application",
    "Application-Version": "2.22.3",
    "Device-Identifier": "00000000-5e1f-f519-ffff-ffffef05ac4a",
    "User-Agent": "okhttp/4.10.0",
    "Accept-Encoding": "gzip",
}
_VOX_BULK = ("https://egy.voxcinemas.com/ar/api/bulk/location"
             "?region=EG&language=ar&version=2")


def probe_vox() -> dict:
    out = {"step1_bulk_location": {}, "step2_cdn_bundle": {}}
    # Step 1: bulk/location -> returns the current CDN bundle URL (plain text)
    try:
        r = requests.get(_VOX_BULK, headers=_VOX_APP_HEADERS, timeout=25)
        bundle_url = r.text.strip()
        out["step1_bulk_location"] = {
            "status_code": r.status_code,
            "bundle_url": bundle_url[:200],
        }
    except Exception as e:
        out["step1_bulk_location"] = {"error": repr(e)}
        return out
    # Step 2: fetch + gunzip the CDN bundle, report top-level keys + counts
    try:
        rr = requests.get(bundle_url, headers={"User-Agent": "okhttp/4.10.0"},
                          timeout=25)
        raw = rr.content
        try:
            text = _gzip.decompress(raw).decode("utf-8", "replace")
        except OSError:
            text = raw.decode("utf-8", "replace")   # already decompressed
        data = _json2.loads(text)
        out["step2_cdn_bundle"] = {
            "status_code": rr.status_code,
            "bytes": len(raw),
            "top_level_keys": list(data.keys())[:30],
            "movies": len(data.get("movies", [])),
            "sessions": len(data.get("sessions", [])),
            "cinemas": len(data.get("cinemas", [])),
        }
    except Exception as e:
        out["step2_cdn_bundle"] = {"error": repr(e), "raw_head": raw[:120].hex()}
    return out


# ------------------------- diagnostic URL probe (temporary) -------------------
import time as _time
from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs

_VOX_TEST_URL = "https://egy.voxcinemas.com/ar/movies/spider-man-brand-new-day?d=20260808"

_PROBE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def probe(url: str) -> dict:
    t0 = _time.time()
    r = requests.get(url, headers=_PROBE_HEADERS, timeout=25, allow_redirects=True)
    body = r.text
    return {
        "requested_url": url,
        "final_url": r.url,
        "status_code": r.status_code,
        "elapsed_sec": round(_time.time() - t0, 2),
        "length": len(body),
        "looks_blocked": any(s in body.lower() for s in
                             ["access denied", "captcha", "cloudflare", "are you a robot"]),
        "no_showtimes_marker": "no showtimes could be found" in body.lower(),
        "snippet": body[:600],
    }


# ------------------------- Vercel serverless endpoint -------------------------
import json
import traceback
from http.server import BaseHTTPRequestHandler


def run_check() -> dict:
    target = target_day()
    days = open_days(fetch(MOVIE_URL))
    if target not in days:
        beat = maybe_heartbeat(False, target)
        return {"open": False, "target": target, "open_days": sorted(days), "heartbeat": beat}

    by_exp = parse_showtimes(fetch(f"{MOVIE_URL}?business_day={target}&ajax=1"))
    if not by_exp:
        return {"open": True, "target": target, "showtimes": {}, "note": "no shows matched filter"}

    text, keyboard = build_message(target, by_exp)
    notify(text, keyboard)
    beat = maybe_heartbeat(True, target)
    return {"open": True, "target": target,
            "showtimes": {k: [t for t, _ in v] for k, v in by_exp.items()},
            "heartbeat": beat}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = _parse_qs(_urlparse(self.path).query)
        test_url = (qs.get("url") or [""])[0]
        want_vox = (qs.get("vox") or [""])[0]
        try:
            if (qs.get("vox_api") or [""])[0]: # ?vox_api=1 -> test VOX read endpoints
                body = probe_vox()
            elif want_vox:                     # ?vox=1 -> probe the hardcoded VOX url
                body = probe(_VOX_TEST_URL)
            elif test_url:                     # ?url=<encoded target> -> probe that
                body = probe(test_url)
            else:
                body = run_check()
            code = 200
        except Exception:
            body = {"error": traceback.format_exc()}
            code = 500
        payload = json.dumps(body, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)
