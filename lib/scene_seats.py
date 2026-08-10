"""
Scene Cinemas seat VIEW (read-only) — fetches the live seat plan for a showtime
and renders it as a text grid for Telegram, so the user can SEE which seats are
free before deciding to book.

READ-ONLY BY DESIGN: this only fetches the booking page (to read the order_id +
hall_id + CSRF the seat-plan call needs) and POSTs to /seat-plan, which merely
returns the seat layout. It NEVER calls lock_seat, shopping_cart, or any payment
step. Viewing the plan holds nothing.

Flow (validated against a real HAR capture of a full Scene booking):
  GET  /booking-<showtimeId>
       -> order_id : hidden input #shopping_center_market_order_id  value="..."
       -> hall_id  : JS literal  var hall_id = "...";
       -> csrf     : X-CSRF-TOKEN inside $.ajaxSetup({headers:{...}}) on that page
  POST /seat-plan   body: showtime_id, order_id, hall_id   header: X-CSRF-TOKEN
       -> JSON {"status":1,"data":[ {app_id:"grid-<r>-<c>", st:..., st_txt:...}, ... ]}

Seat status is the `st` field (NOT st_name, which is a decoy that reads "Free"
for almost everything):
    st == "Standard"     -> a free/available seat
    st == "Occupied"     -> taken
    st == "Blank"        -> aisle / gap (skip)
    st == "SeatRowTitle" -> row label; the letter is in st_txt (e.g. "L")
"""
import re
import json
import socket
import urllib.request
import urllib.error
import urllib.parse

BASE = "https://cfc.scenecinemas.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "*/*",
}
TIMEOUT = 7  # seconds per call — keep under the Vercel function limit


def _get(url, cookies=None):
    req = urllib.request.Request(url, headers=dict(HEADERS))
    if cookies:
        req.add_header("Cookie", cookies)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace"), (r.headers.get_all("Set-Cookie") or [])


def _post(url, data, cookies=None, csrf=None, referer=None):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=dict(HEADERS))
    req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
    req.add_header("Origin", BASE)
    if referer:
        req.add_header("Referer", referer)
    if cookies:
        req.add_header("Cookie", cookies)
    if csrf:
        req.add_header("X-CSRF-TOKEN", csrf)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _jar(set_cookies):
    """Build a Cookie header from a list of Set-Cookie headers (name=value only)."""
    pairs = [c.split(";", 1)[0].strip() for c in set_cookies]
    return "; ".join(p for p in pairs if "=" in p)


def _scrape(html, *patterns):
    for p in patterns:
        m = re.search(p, html)
        if m:
            return m.group(1)
    return None


def fetch_seat_plan(showtime_id):
    """
    Return parsed seats for a showtime, or {"error": "..."} on failure.
    Success: {"rows": {rowLabel: [(col,label,free)...]}, "free": n, "taken": n}
    """
    try:
        # 1. load the booking page — it carries order_id (hidden input),
        #    hall_id (JS literal), and the CSRF token (in $.ajaxSetup).
        page, set_cookies = _get(f"{BASE}/booking-{showtime_id}")
        jar = _jar(set_cookies)

        order_id = _scrape(
            page,
            # hidden input, value attr can be before or after id — try both orders
            r'id=["\']shopping_center_market_order_id["\'][^>]*value=["\']([^"\']+)',
            r'name=["\']shopping_center_market_order_id["\'][^>]*value=["\']([^"\']+)',
            r'value=["\']([^"\']+)["\'][^>]*id=["\']shopping_center_market_order_id',
        )
        hall_id = _scrape(
            page,
            r'var\s+hall_id\s*=\s*["\']([^"\']+)["\']',
            r'hall_id["\']?\s*[:=]\s*["\']([0-9a-f]{16,})["\']',
        )
        csrf = _scrape(
            page,
            r"X-CSRF-TOKEN['\"]\s*:\s*['\"]([A-Za-z0-9]+)",
            r'name=["\']csrf-token["\']\s+content=["\']([^"\']+)',
        )
        if not (order_id and hall_id):
            return {"error": f"missing ids (order_id={order_id!r} hall_id={hall_id!r} "
                             f"pagelen={len(page)})"}

        # 2. POST /seat-plan — body is exactly these three; token goes in the header.
        resp = _post(
            f"{BASE}/seat-plan",
            {"showtime_id": showtime_id, "order_id": order_id, "hall_id": hall_id},
            cookies=jar, csrf=csrf,
            referer=f"{BASE}/showtime-{showtime_id}",
        )
        try:
            payload = json.loads(resp)
        except ValueError:
            return {"error": f"seat-plan returned non-JSON (len={len(resp)}, "
                             f"head={resp[:80]!r})"}
        return _parse_grid(payload.get("data", []))
    except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout,
            TimeoutError) as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _parse_grid(cells):
    """Turn the flat cell list into rows of seats with free/taken status."""
    rows = {}          # rowLabel -> list of (col, label, free)
    row_label = {}     # row number -> letter
    seatcells = []
    for c in cells:
        m = re.match(r"grid-(\d+)-(\d+)", c.get("app_id", ""))
        if not m:
            continue
        r, col = int(m.group(1)), int(m.group(2))
        st = c.get("st", "")
        if st == "SeatRowTitle":
            lbl = (c.get("st_txt") or "").strip()
            if lbl:
                row_label[r] = lbl
        elif st in ("Standard", "Occupied"):
            seatcells.append((r, col, c.get("st_txt", ""), st == "Standard"))
        # "Blank" and anything else => aisle/gap, skip

    if cells and not seatcells:
        seen = sorted({c.get("st", "") for c in cells})
        return {"error": f"0 seats from {len(cells)} cells; st values seen: {seen}"}

    free = taken = 0
    for r, col, label, is_free in seatcells:
        letter = row_label.get(r, str(r))
        rows.setdefault(letter, []).append((col, label, is_free))
        if is_free:
            free += 1
        else:
            taken += 1
    for letter in rows:
        rows[letter].sort(key=lambda x: x[0])
    return {"rows": rows, "free": free, "taken": taken}


def render_text(plan, max_rows=20):
    """Render the plan as a compact text grid: 🟩 free, 🟥 taken."""
    if not plan or not plan.get("rows"):
        return "Couldn't load the seat map."
    lines = ["<b>Seat map</b>  🟩 free · 🟥 taken", "<i>— SCREEN —</i>", ""]
    # rows come back with letter labels; sort A..N (front to back)
    for letter in sorted(plan["rows"].keys())[:max_rows]:
        seats = plan["rows"][letter]
        strip = "".join("🟩" if free else "🟥" for _, _, free in seats)
        lines.append(f"<b>{letter:>2}</b> {strip}")
    lines.append("")
    lines.append(f"🟩 {plan['free']} free · 🟥 {plan['taken']} taken")
    lines.append("\nPick your seats on Scene when you book.")
    return "\n".join(lines)
