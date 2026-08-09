"""
Scene Cinemas Egypt (Cairo Festival City / CFC) — data access (READ-ONLY).

Showtimes are server-rendered HTML on the movie-details page; a day is "open"
when its date appears in the calendar strip (data-DD-MM-YYYY). Per-day showtimes
come from the ?business_day=DD-MM-YYYY&ajax=1 partial, grouped by experience.

Booking = deep link to each showtime's page (we never start a Scene session).

NOTE: Scene has ONE physical branch we track (CFC). Movies are identified by the
slug in their movie-details URL. Coming-soon movies live on /next_releasing.
"""
import re
import urllib.request
import urllib.parse
import html as _html
from datetime import date, datetime, timedelta

BASE = "https://cfc.scenecinemas.com"
CINEMA_NAME = "Scene CFC (Cairo Festival City)"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "X-Requested-With": "XMLHttpRequest",
}
# experience block class -> friendly name
EXPERIENCE = {"imax": "ScreenX", "vip": "Premiere", "stand": "Standard"}


def _get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def movie_url(slug):
    return f"{BASE}/movie-details/{slug}.html"


# ---------- date helpers ----------
def ddmmyyyy(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def to_ddmmyyyy(display_date):
    """Accept 20260813 (int/str) or a date; return 'DD-MM-YYYY'."""
    if isinstance(display_date, date):
        return ddmmyyyy(display_date)
    s = str(display_date)
    if len(s) == 8 and s.isdigit():                 # YYYYMMDD
        return f"{s[6:8]}-{s[4:6]}-{s[0:4]}"
    return s                                        # assume already DD-MM-YYYY


# ---------- open-day detection ----------
def open_days(slug):
    """Dates currently bookable for this movie = dates in the calendar strip."""
    html = _get(movie_url(slug))
    return set(re.findall(r"data-(\d{2}-\d{2}-\d{4})", html))


def is_open(slug, display_date):
    """Is a given day open for booking for this movie?"""
    return to_ddmmyyyy(display_date) in open_days(slug)


def is_bookable(slug):
    """A coming-soon movie has 'opened' once it has ANY open day."""
    try:
        return len(open_days(slug)) > 0
    except Exception:
        return False


# ---------- showtimes for a day ----------
def _after5(iso_hour):
    return iso_hour >= 17 or iso_hour < 5


def _hour(t):
    try:
        return datetime.strptime(t.strip(), "%I:%M %p").hour
    except ValueError:
        return None


def sessions_for(slug, display_date, *, time_filter="any"):
    """
    Return showtimes for a movie on a day, grouped-flat list of dicts:
      {cinema, experience, time, showtime_url}
    time_filter: "after5" | "any" | "first".
    """
    day = to_ddmmyyyy(display_date)
    html = _get(f"{movie_url(slug)}?business_day={day}&ajax=1")
    if "no showtimes" in html.lower():
        return []

    out = []
    for css, name in EXPERIENCE.items():
        m = re.search(rf'ex_{css}_content(.*?)(?=ex_(?:imax|vip|stand)_content|$)',
                      html, re.DOTALL)
        if not m:
            continue
        block = m.group(1)
        for url, t in re.findall(
                r'href="([^"]*showtime-[a-f0-9]+)">\s*(\d{1,2}:\d{2}\s*[AP]M)', block):
            h = _hour(t)
            if time_filter == "after5" and (h is None or not _after5(h)):
                continue
            out.append({
                "cinema": CINEMA_NAME,
                "experience": name,
                "time": t.strip(),
                "showtime_url": urllib.parse.urljoin(BASE, url),
                "_hour": h if h is not None else 99,
            })

    out.sort(key=lambda x: x["_hour"])
    for x in out:
        x.pop("_hour", None)
    if time_filter == "first" and out:
        out = out[:1]
    return out


# ---------- now-showing / coming-soon listings ----------
def _list_movies(path):
    """Scrape movie slugs + real titles from a listing page (home or next_releasing).
    Titles come from the Book-Now link's title="..." attribute; falls back to slug."""
    html = _get(f"{BASE}/{path}")
    # capture: href=".../movie-details/<slug>.html" title="<title>"
    pairs = re.findall(
        r'movie-details/([a-z0-9-]+)\.html"\s+title="([^"]*)"', html)
    seen, out = set(), []
    for slug, title in pairs:
        if slug in seen:
            continue
        seen.add(slug)
        out.append({"slug": slug,
                    "title": _html.unescape(re.sub(r"\s+", " ", title).strip())
                             or slug.replace("-", " ").title()})
    # also catch any slug without a title attribute nearby
    for slug in re.findall(r'movie-details/([a-z0-9-]+)\.html', html):
        if slug not in seen:
            seen.add(slug)
            out.append({"slug": slug, "title": slug.replace("-", " ").title()})
    return out


def now_showing():
    return _list_movies("")            # home page


def coming_soon():
    return _list_movies("next_releasing")
