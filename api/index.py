"""
Single Vercel entrypoint. The newer Python runtime wants ONE entrypoint, so we
route internally by path:
  /api/webhook  (POST from Telegram)  -> webhook logic
  /api/check    (GET from cron-job.org) -> cron sweep
  anything else -> health check
"""
import os
import sys
import json
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import logic_webhook, logic_check


class handler(BaseHTTPRequestHandler):
    def _route(self):
        path = self.path.split("?")[0].rstrip("/")
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""

        if path.endswith("/check"):
            body = logic_check.run_sweep()
            return 200, body
        if path.endswith("/webhook"):
            try:
                update = json.loads(raw.decode() or "{}")
                logic_webhook.handle_update(update)
            except Exception:
                pass
            return 200, {"ok": True}
        return 200, {"status": "cinema bot up", "path": path}

    def do_GET(self):
        self._send(*self._route())

    def do_POST(self):
        self._send(*self._route())

    def _send(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)
