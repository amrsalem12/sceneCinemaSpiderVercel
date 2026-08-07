"""
Throwaway diagnostic endpoint — checks whether Vercel's IP can fetch a URL.
Hit:  https://<your-app>.vercel.app/api/testfetch?url=<URL-ENCODED-TARGET>
Returns status code, size, timing, and a short snippet so you can see if the
real content came back or a block/timeout happened.
Delete this file once you've finished testing.
"""
import json
import time
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def probe(url: str) -> dict:
    t0 = time.time()
    r = requests.get(url, headers=HEADERS, timeout=25, allow_redirects=True)
    body = r.text
    return {
        "requested_url": url,
        "final_url": r.url,
        "status_code": r.status_code,
        "elapsed_sec": round(time.time() - t0, 2),
        "length": len(body),
        "looks_blocked": any(s in body.lower() for s in
                             ["access denied", "captcha", "cloudflare", "are you a robot"]),
        "no_showtimes_marker": "no showtimes could be found" in body.lower(),
        "snippet": body[:600],
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        url = (qs.get("url") or [""])[0]
        try:
            if not url:
                result = {"error": "pass ?url=<url-encoded target>"}
                code = 400
            else:
                result = probe(url)
                code = 200
        except Exception:
            result = {"error": traceback.format_exc(), "requested_url": url}
            code = 502
        payload = json.dumps(result, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)
