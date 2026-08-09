"""
Toggle the cron-job.org scheduler on/off via its REST API.

The bot calls enable() when the first active watch is added, and disable()
when the last active watch clears — so the job only pings while there's
something to watch, and is TRULY off otherwise.

Env vars (set on Vercel):
  CRONJOB_API_KEY   API key from cron-job.org Settings
  CRONJOB_JOB_ID    numeric id of the watcher job (from the job's URL/dashboard)

Docs: PATCH https://api.cron-job.org/jobs/<id>  body {"job":{"enabled":bool}}
Quota: 100 requests/day default — we only call this on state changes, not per run.
"""
import os
import json
import urllib.request

API_KEY = os.getenv("CRONJOB_API_KEY", "").strip()
JOB_ID = os.getenv("CRONJOB_JOB_ID", "").strip()
_BASE = "https://api.cron-job.org"


def _patch_enabled(enabled: bool) -> dict:
    if not (API_KEY and JOB_ID):
        return {"ok": False, "skipped": "CRONJOB_API_KEY / CRONJOB_JOB_ID not set"}
    body = json.dumps({"job": {"enabled": bool(enabled)}}).encode()
    req = urllib.request.Request(
        f"{_BASE}/jobs/{JOB_ID}",
        data=body,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return {"ok": r.status in (200, 204), "status": r.status}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def enable() -> dict:
    """Turn the watcher cron ON (job starts pinging)."""
    return _patch_enabled(True)


def disable() -> dict:
    """Turn the watcher cron OFF (no pings at all)."""
    return _patch_enabled(False)


def sync_to_watches(has_active_watches: bool, last_known_state) -> dict:
    """
    Enable/disable only when the desired state differs from last_known_state,
    to avoid burning API quota on redundant calls.
    Returns {"changed": bool, "state": bool, "result": {...}}.
    Caller persists the returned 'state' (e.g. in KV) as the new last_known_state.
    """
    desired = bool(has_active_watches)
    if last_known_state is not None and bool(last_known_state) == desired:
        return {"changed": False, "state": desired, "result": {"noop": True}}
    result = enable() if desired else disable()
    return {"changed": True, "state": desired, "result": result}
