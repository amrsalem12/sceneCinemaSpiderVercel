"""
Storage layer for the cinema bot — backed by Vercel KV (Upstash Redis REST API),
with a local-file fallback so logic can be tested before KV is configured.

Vercel KV injects these env vars when you create a KV store:
  KV_REST_API_URL, KV_REST_API_TOKEN
(If absent, we fall back to a local JSON file so nothing crashes in dev.)

Data model
----------
watchlist:<chat_id>  -> JSON list of watch entries
convo:<chat_id>      -> JSON dict: transient state of a /upcoming mark flow

Watch entry:
  {
    "id": "w3",                       # short unique id within the user's list
    "chain": "vox" | "scene",
    "movieSlug": "spider-man-brand-new-day",
    "movieTitle": "Spider-Man: Brand New Day",
    "movieId": "00HO00013065",        # vox only; scene uses slug
    "cinemas": ["000028","000047"] | "any",
    "mode": "release" | "date",
    "date": "any" | "20260813",
    "timeFilter": "after5" | "any" | "first",
    "alerted": false
  }
"""
import os
import json
import time
import urllib.request

KV_URL = os.getenv("KV_REST_API_URL", "").rstrip("/")
KV_TOKEN = os.getenv("KV_REST_API_TOKEN", "").strip()
_LOCAL = "/tmp/bot_store.json"          # fallback only


# ---------- low-level KV (Upstash REST) ----------
def _kv(*command):
    """Run one Redis command via Upstash REST. Returns the 'result' field."""
    body = json.dumps(list(command)).encode()
    req = urllib.request.Request(
        KV_URL, data=body,
        headers={"Authorization": f"Bearer {KV_TOKEN}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode()).get("result")


def _kv_available():
    return bool(KV_URL and KV_TOKEN)


# ---------- local fallback ----------
def _local_all():
    try:
        with open(_LOCAL) as f:
            return json.load(f)
    except Exception:
        return {}


def _local_save(d):
    with open(_LOCAL, "w") as f:
        json.dump(d, f)


# ---------- generic get/set of a JSON value at a key ----------
def _get(key, default):
    if _kv_available():
        raw = _kv("GET", key)
        return json.loads(raw) if raw else default
    return _local_all().get(key, default)


def _set(key, value):
    if _kv_available():
        _kv("SET", key, json.dumps(value))
        return
    d = _local_all()
    d[key] = value
    _local_save(d)


# ---------- watchlist API ----------
def get_watchlist(chat_id):
    return _get(f"watchlist:{chat_id}", [])


def add_watch(chat_id, entry):
    wl = get_watchlist(chat_id)
    entry["id"] = "w%d" % (int(time.time() * 1000) % 100000)
    entry.setdefault("alerted", False)
    wl.append(entry)
    _set(f"watchlist:{chat_id}", wl)
    return entry


def remove_watch(chat_id, watch_id):
    wl = get_watchlist(chat_id)
    new = [w for w in wl if w.get("id") != watch_id]
    _set(f"watchlist:{chat_id}", new)
    return len(new) != len(wl)


def set_alerted(chat_id, watch_id, value=True):
    wl = get_watchlist(chat_id)
    for w in wl:
        if w.get("id") == watch_id:
            w["alerted"] = value
    _set(f"watchlist:{chat_id}", wl)


def all_chat_ids():
    """Every chat that has a watchlist — used by the cron sweep."""
    if _kv_available():
        keys = _kv("KEYS", "watchlist:*") or []
        return [k.split(":", 1)[1] for k in keys]
    return [k.split(":", 1)[1] for k in _local_all() if k.startswith("watchlist:")]


# ---------- transient conversation state (for the /upcoming mark flow) ----------
def get_convo(chat_id):
    return _get(f"convo:{chat_id}", {})


def set_convo(chat_id, state):
    _set(f"convo:{chat_id}", state)


def clear_convo(chat_id):
    _set(f"convo:{chat_id}", {})
