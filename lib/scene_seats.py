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
        cells = payload.get("data", [])
        plan = _parse_grid(cells)
        if isinstance(plan, dict) and "rows" in plan:
            plan["cells"] = cells        # raw cells kept for the PNG renderer
        return plan
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

    rows = plan["rows"]  # {letter: [(col, label, free), ...]}
    # global column span so every row lines up vertically (aisles included)
    all_cols = [col for seats in rows.values() for col, _, _ in seats]
    if not all_cols:
        return "Couldn't load the seat map."
    min_c, max_c = min(all_cols), max(all_cols)

    AISLE = "\u3000"  # fullwidth space: same width as an emoji, renders as a gap
    lines = ["<b>Seat map</b>   🟩 free · 🟥 taken",
             "<i>———— SCREEN ————</i>", ""]

    # rows run front(A, nearest screen) -> back; show A at the top, directly
    # under the SCREEN header, matching how Scene's own map is oriented
    for letter in sorted(rows.keys())[:max_rows]:
        seat_at = {col: free for col, _, free in rows[letter]}
        strip = "".join(
            ("🟩" if seat_at[c] else "🟥") if c in seat_at else AISLE
            for c in range(min_c, max_c + 1)
        )
        nfree = sum(1 for v in seat_at.values() if v)
        tag = f"  ({nfree})" if nfree else ""
        lines.append(f"<b>{letter:>2}</b> {strip}{tag}")

    lines.append("")
    lines.append(f"🟩 {plan['free']} free · 🟥 {plan['taken']} taken")
    lines.append("\nNumbers in () = free seats in that row. "
                 "Pick your seats on Scene when you book.")
    return "\n".join(lines)


# ---------------- image rendering (phone-friendly PNG) ----------------
# Text grids wrap on phones once a hall gets wide; a PNG scales and never wraps.
# render_png() returns raw PNG bytes for telegram.send_photo, or None on failure
# (callers should fall back to render_text).
_IMG_BG     = (14, 15, 18)
_IMG_FREE   = (46, 160, 67)     # green  = available
_IMG_TAKEN  = (60, 63, 70)      # gray   = occupied
_IMG_SCREEN = (120, 130, 150)
_IMG_SUBTLE = (120, 125, 135)
_IMG_WHITE  = (255, 255, 255)


def _img_font(size, bold=False):
    from PIL import ImageFont
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def render_png(cells):
    """Render the seat plan to PNG bytes. `cells` is the raw /seat-plan data list.
    Returns bytes on success, or a string starting with 'ERR:' describing why it
    couldn't (so callers can log it); returns None only if there are no seats."""
    try:
        import io
        from PIL import Image, ImageDraw
    except Exception as e:
        return f"ERR: PIL import failed: {type(e).__name__}: {e}"

    seats, rowletter, cols, rowset = {}, {}, set(), set()
    for c in cells:
        m = re.match(r"grid-(\d+)-(\d+)", c.get("app_id", ""))
        if not m:
            continue
        r, col = int(m.group(1)), int(m.group(2))
        st = c.get("st", "")
        if st == "SeatRowTitle":
            lbl = (c.get("st_txt") or "").strip()
            if lbl:
                rowletter[r] = lbl
        elif st in ("Standard", "Occupied"):
            seats[(r, col)] = (st == "Standard", (c.get("st_txt") or "").strip())
            cols.add(col)
            rowset.add(r)
    if not seats:
        return None

    min_c, max_c = min(cols), max(cols)
    # sort by the ROW LETTER (A=front,...) not the grid row number — Scene's grid
    # numbers run back-to-front, so sorting by number flips the map upside down.
    rows = sorted(rowset, key=lambda r: rowletter.get(r, chr(0x7f) + str(r)))
    ncol = max_c - min_c + 1

    CELL, GAP, PAD, LBL, TOP = 34, 6, 28, 26, 70
    W = PAD * 2 + LBL * 2 + ncol * CELL + (ncol - 1) * GAP
    H = TOP + PAD + len(rows) * CELL + (len(rows) - 1) * GAP + PAD
    img = Image.new("RGB", (W, H), _IMG_BG)
    d = ImageDraw.Draw(img)

    # screen bar + label
    d.rounded_rectangle([PAD + LBL, 26, W - PAD - LBL, 34], radius=4, fill=_IMG_SCREEN)
    fscreen = _img_font(18, bold=True)
    tw = d.textlength("SCREEN", font=fscreen)
    d.text(((W - tw) / 2, 6), "SCREEN", font=fscreen, fill=_IMG_SCREEN)

    fseat, flabel = _img_font(13), _img_font(16, bold=True)
    x0 = PAD + LBL
    for ri, r in enumerate(rows):
        y = TOP + ri * (CELL + GAP)
        letter = rowletter.get(r, str(r))
        d.text((PAD - 2, y + CELL / 2 - 9), letter, font=flabel, fill=_IMG_SUBTLE)
        d.text((W - PAD - LBL + 8, y + CELL / 2 - 9), letter, font=flabel, fill=_IMG_SUBTLE)
        # draw columns right-to-left: Scene's grid col numbers run opposite to the
        # visual layout (higher col = lower seat number), so mirror the placement.
        for ci, col in enumerate(range(max_c, min_c - 1, -1)):
            cell = seats.get((r, col))
            if not cell:
                continue                       # aisle / gap
            is_free, label = cell
            x = x0 + ci * (CELL + GAP)
            d.rounded_rectangle([x, y, x + CELL, y + CELL], radius=7,
                                fill=_IMG_FREE if is_free else _IMG_TAKEN)
            if is_free and label:
                num = "".join(ch for ch in label if ch.isdigit()) or label
                tw = d.textlength(num, font=fseat)
                d.text((x + (CELL - tw) / 2, y + CELL / 2 - 8), num,
                       font=fseat, fill=_IMG_WHITE)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
