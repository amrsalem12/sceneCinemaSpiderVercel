"""
Telegram helpers — send messages, inline-button keyboards, photos, and answer
callback taps. Shared by api/webhook.py (interactive) and api/check.py (alerts).

Env vars:
  TELEGRAM_TOKEN   bot token from @BotFather
  (chat ids are passed per-call, since the bot is multi-user)
"""
import os
import json
import uuid
import urllib.request

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
_API = f"https://api.telegram.org/bot{TOKEN}"


def _post(method: str, payload: dict) -> dict:
    if not TOKEN:
        return {"ok": False, "skipped": "TELEGRAM_TOKEN not set"}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{_API}/{method}", data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def _post_multipart(method: str, fields: dict, files: dict) -> dict:
    """POST as multipart/form-data. `files` = {name: (filename, bytes, mime)}.
    Needed to upload raw image bytes (sendPhoto with a file, not a URL)."""
    if not TOKEN:
        return {"ok": False, "skipped": "TELEGRAM_TOKEN not set"}
    boundary = "----cinemabot" + uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += (str(value) + "\r\n").encode()
    for name, (filename, content, mime) in files.items():
        body += f"--{boundary}\r\n".encode()
        body += (f'Content-Disposition: form-data; name="{name}"; '
                 f'filename="{filename}"\r\n').encode()
        body += f"Content-Type: {mime}\r\n\r\n".encode()
        body += content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{_API}/{method}", data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def send_message(chat_id, text, buttons=None, silent=False):
    """
    Send a text message. `buttons` = list of rows, each row a list of
    (label, callback_data_or_url) tuples. A value starting with http(s):// is
    rendered as a URL button; anything else as a callback button.
    """
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_notification": bool(silent),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": _kb(buttons)}
    return _post("sendMessage", payload)


def _kb(buttons):
    kb = []
    for row in buttons:
        kb_row = []
        for label, value in row:
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                kb_row.append({"text": label, "url": value})
            else:
                kb_row.append({"text": label, "callback_data": value})
        kb.append(kb_row)
    return kb


def send_photo(chat_id, photo, caption=None, buttons=None):
    """Send a photo. `photo` may be:
      - a URL or Telegram file_id (str)  -> JSON sendPhoto
      - raw image bytes                  -> multipart upload
    Optional caption + inline buttons."""
    if isinstance(photo, (bytes, bytearray)):
        fields = {"chat_id": chat_id}
        if caption:
            fields["caption"] = caption
            fields["parse_mode"] = "HTML"
        if buttons:
            fields["reply_markup"] = json.dumps({"inline_keyboard": _kb(buttons)})
        return _post_multipart("sendPhoto", fields,
                               {"photo": ("seatmap.png", bytes(photo), "image/png")})

    payload = {"chat_id": chat_id, "photo": photo}
    if caption:
        payload["caption"] = caption
        payload["parse_mode"] = "HTML"
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": _kb(buttons)}
    return _post("sendPhoto", payload)


def answer_callback(callback_query_id, text=None):
    """Acknowledge a button tap (stops Telegram's loading spinner)."""
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    return _post("answerCallbackQuery", payload)


def alert_burst(chat_id, text, buttons=None, repeat=5, interval=3):
    """Loud, repeated alert (Mode B 'it opened' notification). Sound on."""
    import time
    last = None
    for i in range(max(1, repeat)):
        last = send_message(chat_id, text, buttons=buttons, silent=False)
        if i < repeat - 1:
            time.sleep(interval)
    return last


def parse_update(update: dict):
    """
    Normalize a Telegram update into a simple dict:
      {"kind":"message"|"callback", "chat_id":..., "user_id":..., "text":...,
       "data":..., "callback_id":...}
    Returns None for updates we don't handle.
    """
    if "message" in update:
        m = update["message"]
        return {
            "kind": "message",
            "chat_id": m["chat"]["id"],
            "user_id": m["from"]["id"],
            "text": (m.get("text") or "").strip(),
        }
    if "callback_query" in update:
        c = update["callback_query"]
        return {
            "kind": "callback",
            "chat_id": c["message"]["chat"]["id"],
            "user_id": c["from"]["id"],
            "data": c.get("data", ""),
            "callback_id": c["id"],
        }
    return None
