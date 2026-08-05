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
ALERT_REPEAT = int(os.getenv("ALERT_REPEAT", "3"))
ALERT_INTERVAL = int(os.getenv("ALERT_INTERVAL", "3"))

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
