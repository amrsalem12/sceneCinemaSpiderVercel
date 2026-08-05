"""
Vercel serverless endpoint for the Scene CFC watcher.
Deploy this repo to Vercel; cron-job.org pings https://<your-app>.vercel.app/api/check
Set TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (and optionally TARGET_DATE, ONLY_AFTER_5PM)
as Environment Variables in the Vercel project settings.
"""
import json
from http.server import BaseHTTPRequestHandler

# reuse the exact same logic as the CLI script
from _watcher import target_day, fetch, open_days, parse_showtimes, build_message, notify, MOVIE_URL


def run_check() -> dict:
    target = target_day()
    days = open_days(fetch(MOVIE_URL))
    if target not in days:
        return {"open": False, "target": target, "open_days": sorted(days)}

    by_exp = parse_showtimes(fetch(f"{MOVIE_URL}?business_day={target}&ajax=1"))
    if not by_exp:
        return {"open": True, "target": target, "showtimes": {}, "note": "no shows matched filter"}

    text, keyboard = build_message(target, by_exp)
    notify(text, keyboard)
    return {"open": True, "target": target,
            "showtimes": {k: [t for t, _ in v] for k, v in by_exp.items()}}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            result = run_check()
            code = 200
        except Exception as e:
            result = {"error": str(e)}
            code = 500
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())
