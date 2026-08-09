"""
Single Vercel entrypoint (newer Python runtime wants ONE entrypoint).
Routes by path, falling back to method/payload so it works even if the
platform rewrites the visible path:
  - POST carrying a Telegram update   -> webhook logic
  - path contains 'check'             -> cron sweep
  - anything else                     -> health check
"""
import os
import sys
import json
from http.server import BaseHTTPRequestHandler

# Make both the project root AND the api/ dir importable, whatever the CWD.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_IMPORT_ERROR = None
logic_webhook = logic_check = None
try:
    from lib import logic_webhook, logic_check
except Exception:
    import traceback as _tb
    _IMPORT_ERROR = _tb.format_exc()


class handler(BaseHTTPRequestHandler):
    def _route(self, method):
        if _IMPORT_ERROR:
            return 200, {"import_error": _IMPORT_ERROR[-1500:]}
        path = self.path.split("?")[0].rstrip("/").lower()
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""

        if "check" in path:
            return 200, logic_check.run_sweep()

        looks_like_telegram = False
        update = None
        if raw:
            try:
                update = json.loads(raw.decode() or "{}")
                looks_like_telegram = isinstance(update, dict) and (
                    "message" in update or "callback_query" in update
                    or "edited_message" in update or "update_id" in update)
            except Exception:
                update = None

        if "webhook" in path or (method == "POST" and looks_like_telegram):
            if update is not None:
                try:
                    logic_webhook.handle_update(update)
                except Exception:
                    import traceback
                    return 200, {"ok": False, "err": traceback.format_exc()[-1200:]}
            return 200, {"ok": True}

        return 200, {"status": "cinema bot up"}

    def do_GET(self):
        self._send(*self._route("GET"))

    def do_POST(self):
        self._send(*self._route("POST"))

    def _send(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)
