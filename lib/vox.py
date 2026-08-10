"""
VOX Cinemas Egypt — data access (READ-ONLY, cloud-reachable).

Flow (all confirmed working from Vercel):
  1. GET bulk/location (app headers) -> current CDN bundle URL
  2. GET that .json.gz from assets.voxcinemas.com -> gunzip -> full Egypt dataset
  3. filter movies (now-showing / coming-soon) and sessions (by movie/cinema/date/time)

No auth key needed; gated only by app headers. Booking = deep link (bookingUrl);
we never start a VOX booking session.
"""
import gzip
import json
import urllib.request
from datetime import datetime

BULK_URL = ("https://egy.voxcinemas.com/en/api/bulk/location"
            "?region=EG&language=en&version=2")
APP_HEADERS = {
    "Application-Name": "VOX Android Application",
    "Application-Version": "2.22.3",
    "Device-Identifier": "00000000-5e1f-f519-ffff-ffffef05ac4a",
    "User-Agent": "okhttp/4.10.0",
    "Accept-Encoding": "gzip",
}

# Egypt cinemas we care about (id -> friendly name)
EG_CINEMAS = {
    "000047": "City Centre Almaza",   # the only VOX cinema Amr cares about
}
EXPERIENCE = {"gd": "Gold", "imx": "IMAX", "mx": "MAX", "fx": "4DX",
              "kd": "Kids", "st": "Standard"}


def _get(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _decode_maybe_gzip(raw: bytes) -> str:
    """Decode bytes that may or may not be gzip-compressed."""
    if raw[:2] == b"\x1f\x8b":              # gzip magic number
        return gzip.decompress(raw).decode("utf-8", "replace")
    return raw.decode("utf-8", "replace")


def fetch_bundle():
    """Return the parsed Egypt dataset dict (fresh each call)."""
    bundle_url = _decode_maybe_gzip(_get(BULK_URL, APP_HEADERS)).strip()
    raw = _get(bundle_url, {"User-Agent": "okhttp/4.10.0"})
    return json.loads(_decode_maybe_gzip(raw))


# ---------- movie helpers ----------
def now_showing(bundle):
    return [_movie_brief(m) for m in bundle.get("movies", []) if m.get("nowShowing")]


def coming_soon(bundle):
    return [_movie_brief(m) for m in bundle.get("movies", []) if m.get("comingSoon")]


def _movie_brief(m):
    return {"id": m.get("id"), "slug": m.get("slug"), "title": m.get("title"),
            "genre": m.get("genre"), "rating": m.get("rating"),
            "runtime": m.get("runtime"), "releaseDate": m.get("releaseDate"),
            "posterUrl": m.get("posterUrl"), "trailerUrl": m.get("trailerUrl"),
            "language": m.get("language"), "subtitles": m.get("subtitles"),
            "synopsis": m.get("synopsis"),
            "nowShowing": bool(m.get("nowShowing")),
            "comingSoon": bool(m.get("comingSoon"))}


def find_movie(bundle, slug=None, movie_id=None):
    for m in bundle.get("movies", []):
        if (slug and m.get("slug") == slug) or (movie_id and m.get("id") == movie_id):
            return _movie_brief(m)
    return None


def is_bookable(bundle, slug):
    """A coming-soon movie has 'opened' once it has >=1 Egypt session."""
    m = find_movie(bundle, slug=slug)
    if not m:
        return False
    return any(s.get("movieId") == m["id"] and s.get("cinemaId") in EG_CINEMAS
               for s in bundle.get("sessions", []))


# ---------- session (showtime) helpers ----------
def _hour(showtime_iso):
    try:
        return datetime.fromisoformat(showtime_iso).hour
    except Exception:
        return None


def _after5(showtime_iso):
    h = _hour(showtime_iso)
    return h is not None and (h >= 17 or h < 5)   # evening + late-night


def sessions_for(bundle, *, movie_slug=None, movie_id=None,
                 cinemas=None, display_date=None, time_filter="any",
                 only_available=False):
    """
    Filter Egypt sessions. Returns list of clean dicts sorted by showtime.
      cinemas: iterable of cinema ids, or None/"any" for all Egypt cinemas.
      display_date: int like 20260813, or None for any date.
      time_filter: "after5" | "any" | "first".
      only_available: if True, drop sessions with seats==0.
    """
    if movie_slug and not movie_id:
        m = find_movie(bundle, slug=movie_slug)
        movie_id = m["id"] if m else None
    if not movie_id:
        return []

    allowed = set(EG_CINEMAS) if (not cinemas or cinemas == "any") else set(cinemas)
    out = []
    for s in bundle.get("sessions", []):
        if s.get("movieId") != movie_id:
            continue
        if s.get("cinemaId") not in allowed:
            continue
        if display_date and s.get("displayDate") != display_date:
            continue
        if time_filter == "after5" and not _after5(s.get("showtime", "")):
            continue
        if only_available and not s.get("seats"):
            continue
        out.append(_session_brief(s))

    out.sort(key=lambda x: x["showtime"])
    if time_filter == "first" and out:
        out = out[:1]
    return out


def _session_brief(s):
    exp_code = (s.get("experience") or "").strip(";")
    return {
        "id": s.get("id"),
        "cinemaId": s.get("cinemaId"),
        "cinema": EG_CINEMAS.get(s.get("cinemaId"), s.get("cinemaId")),
        "experience": EXPERIENCE.get(exp_code, exp_code or "Standard"),
        "seats": s.get("seats", 0),
        "showtime": s.get("showtime"),
        "time": _fmt_time(s.get("showtime")),
        "displayDate": s.get("displayDate"),
        "bookingUrl": s.get("bookingUrl"),
    }


def _fmt_time(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%I:%M %p").lstrip("0")
    except Exception:
        return iso
