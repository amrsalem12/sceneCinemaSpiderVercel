"""
Single Vercel entrypoint (the newer Python runtime wants ONE entrypoint).
Routes by path AND falls back to method/payload so it works even if the
platform rewrites the visible path:

  - POST carrying a Telegram update   -> webhook logic
  - path contains 'check' (GET/POST)  -> cron sweep
  - anything else                     -> health check
"""
import os
import sys
import json
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import logic_webhook, logic_check


class handler(BaseHTTPRequestHandler):
    def _route(self, method):
        path = self.path.split("?")[0].rstrip("/").lower()
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""

        # TEMP debug route: /api/debug -> exercise KV + report any error
        if "debug" in path:
            import traceback
            out = {}
            try:
                from lib import store
                out["kv_available"] = store._kv_available()
                out["kv_ping"] = store._kv("SET", "diag", "ok") if store._kv_available() else "no-kv"
                out["kv_read"] = store._kv("GET", "diag") if store._kv_available() else "no-kv"
                out["allowlist"] = store._get("allowlist", [])
                import os
                out["has_telegram_token"] = bool(os.getenv("TELEGRAM_TOKEN"))
                jc = os.getenv("JOIN_CODE")
                out["join_code_is_none"] = jc is None
                out["join_code_len"] = (len(jc) if jc is not None else -1)
                out["join_code_repr"] = repr(jc)[:40]
                # list env keys that look related, to catch a name typo
                out["env_keys_with_join_or_code"] = [k for k in os.environ
                                                     if "JOIN" in k.upper() or "CODE" in k.upper()]
                # all NON-secret-looking custom env var names (helps spot wrong project)
                sysprefixes = ("PATH","PYTHON","LANG","LC_","HOME","HOSTNAME","PWD",
                               "SHLVL","TERM","AWS","LAMBDA","VERCEL","NOW_","_","TZ")
                out["custom_env_names"] = sorted(
                    k for k in os.environ
                    if not k.startswith(sysprefixes) and "TOKEN" not in k
                    and "SECRET" not in k and "KEY" not in k and "URL" not in k)
            except Exception:
                out["error"] = traceback.format_exc()
            return 200, out

        # explicit cron path
        if "check" in path:
            return 200, logic_check.run_sweep()

        # webhook: explicit path OR any POST that looks like a Telegram update
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
                    pass
            return 200, {"ok": True}

        return 200, {"status": "cinema bot up", "path": path or "/"}

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
