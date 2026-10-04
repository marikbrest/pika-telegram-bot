"""
Telegram Bot API integration: everything the rest of the bot needs from the channel.

    send_text_message(to, body)            -> bool     (chunked, bold markers converted, retried)
    send_reaction(to, message_id, emoji)   -> bool
    send_image_bytes(to, bytes, mime)      -> bool
    download_media(file_id)                -> (bytes, mime) | None
    get_updates / set_webhook / ...        -> raw API calls used by src/telegram_handler.py

`to` is a Telegram chat id (a string of digits; users get theirs from @userinfobot). A
bot can only message people who have started it, but there is no 24-hour
window and no template approval: a reminder or alert is always plain text.

Message ids inside the bot are "<chat_id>:<message_id>" (Telegram message ids are only
unique per chat); send_reaction splits that back apart.
"""
import html
import re
import time

import httpx

from src.config import TELEGRAM_API_BASE, TELEGRAM_BOT_TOKEN

# Retries with backoff for rate limits (429), server errors (5xx) and network errors: immediately, then 1s, 5s, 20s.
_RETRY_DELAYS_SECONDS = [1, 5, 20]

# Telegram rejects a single text message longer than 4096 characters.
MAX_MESSAGE_CHARS = 4096

# One pooled client; a bot's messages are sparse, so keep connections alive for two minutes instead of httpx's 5 seconds.
_client = httpx.Client(timeout=10.0, limits=httpx.Limits(max_keepalive_connections=5, keepalive_expiry=120.0))

_BOLD = re.compile(r"\*([^*\n]+)\*")


def _url(method: str) -> str:
    return f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/{method}"


def _api(method: str, payload: dict | None = None, files: dict | None = None, timeout: float | None = None) -> tuple[bool, dict | None]:
    """
    Calls one Bot API method with retries. Returns (ok, parsed JSON body). A 4xx other than 429 is
    returned straight away (it will not fix itself); the body of the last failed attempt is returned
    on failure so a caller can read Telegram's description.
    """
    if not TELEGRAM_BOT_TOKEN:
        # Never print the payload: it can carry message text, and this log is readable from the admin panel.
        print(f"[telegram] TELEGRAM_BOT_TOKEN missing - {method} not sent")
        return False, None

    attempts = 1 + len(_RETRY_DELAYS_SECONDS)
    error_json = None
    for attempt in range(attempts):
        try:
            if files:
                resp = _client.post(_url(method), data=payload or {}, files=files, timeout=timeout or 30.0)
            else:
                resp = _client.post(_url(method), json=payload or {}, timeout=timeout or 10.0)
            try:
                body = resp.json()
            except ValueError:
                body = None
            if resp.status_code == 200 and body is not None and body.get("ok"):
                return True, body
            error_json = body
            retryable = resp.status_code == 429 or resp.status_code >= 500
            print(f"[telegram] {method} failed ({resp.status_code}), attempt {attempt + 1}/{attempts}: {(body or {}).get('description')}")
            if not retryable:
                return False, error_json
            delay = ((body or {}).get("parameters") or {}).get("retry_after")
            if attempt < len(_RETRY_DELAYS_SECONDS):
                time.sleep(min(float(delay), 30.0) if delay else _RETRY_DELAYS_SECONDS[attempt])
            continue
        except httpx.HTTPError as e:
            print(f"[telegram] network error on {method}, attempt {attempt + 1}/{attempts}: {e}")
        if attempt < len(_RETRY_DELAYS_SECONDS):
            time.sleep(_RETRY_DELAYS_SECONDS[attempt])

    print(f"[telegram] giving up on {method} after {attempts} attempts")
    return False, error_json


def to_html(text: str) -> str:
    """The bot's messages use *bold* (chat-style markup); Telegram needs HTML entities for that."""
    return _BOLD.sub(r"<b>\1</b>", html.escape(text, quote=False))


def _chunks(text: str) -> list[str]:
    """Splits at paragraph/line boundaries where possible so a long list is not cut mid-line."""
    if len(text) <= MAX_MESSAGE_CHARS:
        return [text]
    parts: list[str] = []
    rest = text
    while len(rest) > MAX_MESSAGE_CHARS:
        cut = rest.rfind("\n\n", 0, MAX_MESSAGE_CHARS)
        if cut < MAX_MESSAGE_CHARS // 2:
            cut = rest.rfind("\n", 0, MAX_MESSAGE_CHARS)
        if cut < MAX_MESSAGE_CHARS // 2:
            cut = MAX_MESSAGE_CHARS
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        parts.append(rest)
    return parts


def send_text_message(to: str, body: str) -> bool:
    """
    Sends a text message (split in chunks if it is longer than Telegram allows). The bold markers are
    sent as HTML; if Telegram refuses the markup the plain text is sent instead, so a message is never
    lost to formatting. Returns True only if every chunk was delivered; never raises to the caller.
    """
    all_sent = True
    for chunk in _chunks(body):
        ok, response = _api("sendMessage", {"chat_id": to, "text": to_html(chunk), "parse_mode": "HTML"})
        if not ok and response is not None and "parse entities" in str(response.get("description", "")):
            ok, response = _api("sendMessage", {"chat_id": to, "text": chunk})
        all_sent = all_sent and ok
    return all_sent


def send_reaction(to: str, message_id: str, emoji: str) -> bool:
    """
    Reacts to a message the bot received (with emoji="" the reaction is removed). Best-effort: a reaction
    failing must never affect the real reply. `message_id` is the bot-internal "<chat_id>:<message_id>".
    """
    raw_id = str(message_id).rsplit(":", 1)[-1]
    if not raw_id.isdigit():
        return False
    reaction = [{"type": "emoji", "emoji": emoji}] if emoji else []
    ok, _ = _api("setMessageReaction", {"chat_id": to, "message_id": int(raw_id), "reaction": reaction})
    return ok


def send_image_bytes(to: str, image_bytes: bytes, mime_type: str) -> bool:
    """Sends generated/edited image bytes as a photo. Returns True/False; never raises."""
    ok, _ = _api("sendPhoto", {"chat_id": to}, files={"photo": ("image", image_bytes, mime_type)}, timeout=30.0)
    return ok


def download_media(file_id: str) -> tuple[bytes, str] | None:
    """
    Downloads a file the user sent (voice message, photo, document): getFile gives a path, the content
    is then fetched from the file endpoint. Returns (bytes, mime_type) or None. Bots can only download
    files up to 20 MB; larger ones fail here and are answered as "download failed".
    """
    if not TELEGRAM_BOT_TOKEN:
        print("[telegram] TELEGRAM_BOT_TOKEN missing - cannot download media")
        return None

    ok, body = _api("getFile", {"file_id": file_id})
    path = ((body or {}).get("result") or {}).get("file_path") if ok else None
    if not path:
        return None
    try:
        resp = _client.get(f"{TELEGRAM_API_BASE}/file/bot{TELEGRAM_BOT_TOKEN}/{path}", timeout=30.0)
    except httpx.HTTPError as e:
        print(f"[telegram] network error downloading media: {e}")
        return None
    if resp.status_code != 200:
        print(f"[telegram] media download failed ({resp.status_code})")
        return None
    return resp.content, _guess_mime_type(path, resp.headers.get("content-type"))


_MIME_BY_EXTENSION = {
    ".oga": "audio/ogg", ".ogg": "audio/ogg", ".opus": "audio/ogg", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".pdf": "application/pdf",
}


def _guess_mime_type(path: str, header: str | None) -> str:
    """Telegram serves files as application/octet-stream, so the extension of the stored path is the better hint."""
    extension = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if extension in _MIME_BY_EXTENSION:
        return _MIME_BY_EXTENSION[extension]
    return (header or "application/octet-stream").split(";")[0].strip()


def get_updates(offset: int | None, timeout_seconds: int = 30) -> list[dict] | None:
    """Long-polls for updates. Returns the list (possibly empty) or None on a failed call."""
    payload: dict = {"timeout": timeout_seconds, "allowed_updates": ["message"]}
    if offset is not None:
        payload["offset"] = offset
    ok, body = _api("getUpdates", payload, timeout=timeout_seconds + 10.0)
    return (body or {}).get("result", []) if ok else None


def set_webhook(url: str, secret_token: str) -> bool:
    ok, _ = _api("setWebhook", {"url": url, "secret_token": secret_token, "allowed_updates": ["message"]})
    return ok


def delete_webhook() -> bool:
    ok, _ = _api("deleteWebhook", {"drop_pending_updates": False})
    return ok


def get_me() -> dict | None:
    ok, body = _api("getMe", {})
    return (body or {}).get("result") if ok else None
