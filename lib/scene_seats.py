"""
Scene Cinemas seat VIEW (read-only) — fetches the live seat plan for a showtime
and renders it as a text grid for Telegram, so the user can SEE which seats are
free before deciding to book.

READ-ONLY BY DESIGN: this module only fetches the booking page (to scrape the
order_id + CSRF the seat-plan call needs) and POSTs to /seat-plan, which merely
returns the seat layout. It NEVER calls /movies/orders/lock_seat, shopping_cart,
or any payment step. Viewing the plan holds nothing. The user still books on
Scene's own site.

Flow:
  GET /booking-<showtimeId>  -> scrape order_id, hall_id, CSRF token
  POST /seat-plan (showtime_id, order_id, hall_id, _token/csrf) -> seat JSON
  parse grid-<row>-<col> + st/st_txt -> render

Seat statuses: 'Standard' = free, 'Occupied' = taken, 'SeatRowTitle' = row label,
'Blank' = aisle/gap.
"""
import re
import json
import urllib.request
import urllib.parse

BASE = "https://cfc.scenecinemas.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "X-Requested-With": "XMLHttpRequest",
}


def _get(url, cookies=None):
    req = urllib.request.Request(url, headers=dict(HEADERS))
    if cookies:
        req.add_header("Cookie", cookies)
    with urllib.request.urlopen(req, timeout=25) as r:
        set_cookie = r.headers.get("Set-Cookie", "")
        return r.read().decode("utf-8", "replace"), set_cookie


def _post(url, data, cookies=None, csrf=None):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=dict(HEADERS))
    req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
    if cookies:
        req.add_header("Cookie", cookies)
    if csrf:
        req.add_header("X-CSRF-TOKEN", csrf)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def _scrape(html, *patterns):
    for p in patterns:
        m = re.search(p, html)
        if m:
            return m.group(1)
    return None


def fetch_seat_plan(showtime_id):
    """
    Return parsed seats for a showtime, or None if unavailable.
    Result: {"rows": {rowLabel: [ {label, free} ... ]}, "free": n, "taken": n}
    """
    # 1. load the booking page to get order_id, hall_id, csrf (+ cookies)
    page, cookie = _get(f"{BASE}/booking-{showtime_id}")
    jar = ";".join(c.split(";")[0] for c in cookie.split(",") if "=" in c)

    order_id = _scrape(page, r'order_id["\']?\s*[:=]\s*["\']([^"\']+)',
                       r'name=["\']order_id["\']\s+value=["\']([^"\']+)')
    hall_id = _scrape(page, r'hall_id["\']?\s*[:=]\s*["\']([^"\']+)',
                      r'name=["\']hall_id["\']\s+value=["\']([^"\']+)')
    csrf = _scrape(page, r'name=["\']csrf-token["\']\s+content=["\']([^"\']+)',
                   r'_token["\']?\s*[:=]\s*["\']([^"\']+)',
                   r'csrf["\']?\s*[:=]\s*["\']([^"\']+)')
    if not (order_id and hall_id):
        return None

    # 2. POST /seat-plan (read-only — returns the layout, holds nothing)
    resp = _post(f"{BASE}/seat-plan",
                 {"showtime_id": showtime_id, "order_id": order_id,
                  "hall_id": hall_id},
                 cookies=jar, csrf=csrf)
    try:
        data = json.loads(resp).get("data", [])
    except Exception:
        return None
    return _parse_grid(data)


def _parse_grid(cells):
    """Turn the flat cell list into rows of seats with free/taken status."""
    # group by row number from grid-<row>-<col>
    rows = {}          # rowLabel -> list of (col, label, free)
    row_label = {}     # row number -> letter
    seatcells = []
    for c in cells:
        aid = c.get("app_id", "")
        m = re.match(r"grid-(\d+)-(\d+)", aid)
        if not m:
            continue
        r, col = int(m.group(1)), int(m.group(2))
        st = c.get("st", "")
        if st == "SeatRowTitle":
            row_label[r] = (c.get("st_txt") or "").strip()
        elif st in ("Standard", "Occupied"):
            seatcells.append((r, col, c.get("st_txt", ""), st == "Standard"))

    free = taken = 0
    for r, col, label, is_free in seatcells:
        letter = row_label.get(r, str(r))
        rows.setdefault(letter, []).append((col, label, is_free))
        if is_free:
            free += 1
        else:
            taken += 1
    # sort each row by column
    for letter in rows:
        rows[letter].sort(key=lambda x: x[0])
    return {"rows": rows, "free": free, "taken": taken}


def render_text(plan, max_rows=16):
    """Render the plan as a compact text grid: 🟩 free, 🟥 taken."""
    if not plan or not plan["rows"]:
        return "Couldn't load the seat map."
    lines = ["<b>Seat map</b>  🟩 free · 🟥 taken", "<i>SCREEN THIS WAY</i>", ""]
    # order rows by their letter (front to back is usually A..N)
    for letter in sorted(plan["rows"].keys())[:max_rows]:
        seats = plan["rows"][letter]
        strip = "".join("🟩" if free else "🟥" for _, _, free in seats)
        lines.append(f"<b>{letter:>2}</b> {strip}")
    lines.append("")
    lines.append(f"🟩 {plan['free']} free · 🟥 {plan['taken']} taken")
    lines.append("\nPick your seats on Scene when you book.")
    return "\n".join(lines)
