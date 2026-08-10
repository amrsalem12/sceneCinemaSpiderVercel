import re, json, socket
import urllib.request, urllib.error, urllib.parse

TIMEOUT = 7  # per call — must stay under your Vercel function limit

def _get(url, cookies=None):
    req = urllib.request.Request(url, headers=dict(HEADERS))
    if cookies:
        req.add_header("Cookie", cookies)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace"), (r.headers.get_all("Set-Cookie") or [])

def _post(url, data, cookies=None, csrf=None):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=dict(HEADERS))
    req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
    if cookies:
        req.add_header("Cookie", cookies)
    if csrf:
        req.add_header("X-CSRF-TOKEN", csrf)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")

def _jar(set_cookies):
    # each Set-Cookie header is ONE cookie; take the name=value before the first ';'
    pairs = [c.split(";", 1)[0].strip() for c in set_cookies]
    return "; ".join(p for p in pairs if "=" in p)

def fetch_seat_plan(showtime_id):
    try:
        page, set_cookies = _get(f"{BASE}/booking-{showtime_id}")
        jar = _jar(set_cookies)

        order_id = _scrape(page, r'order_id["\']?\s*[:=]\s*["\']([^"\']+)',
                           r'name=["\']order_id["\']\s+value=["\']([^"\']+)')
        hall_id = _scrape(page, r'hall_id["\']?\s*[:=]\s*["\']([^"\']+)',
                          r'name=["\']hall_id["\']\s+value=["\']([^"\']+)')
        csrf = _scrape(page, r'name=["\']csrf-token["\']\s+content=["\']([^"\']+)',
                       r'_token["\']?\s*[:=]\s*["\']([^"\']+)',
                       r'csrf["\']?\s*[:=]\s*["\']([^"\']+)')

        if not (order_id and hall_id):
            print(f"[scene] no order_id/hall_id for {showtime_id} "
                  f"order_id={order_id} hall_id={hall_id} pagelen={len(page)}")
            return None

        resp = _post(f"{BASE}/seat-plan",
                     {"showtime_id": showtime_id, "order_id": order_id, "hall_id": hall_id},
                     cookies=jar, csrf=csrf)
        return _parse_grid(json.loads(resp).get("data", []))
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout,
            TimeoutError, ValueError) as e:
        print(f"[scene] seat plan failed for {showtime_id}: {e!r}")
        return None
