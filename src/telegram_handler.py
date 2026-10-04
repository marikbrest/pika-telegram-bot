"""
Telegram intake: turns Bot API updates into the bot's internal message shape and runs the pipeline.

Two ways to receive updates (TELEGRAM_MODE in .env):
- "polling" (default): a background thread long-polls getUpdates. Needs no public URL.
- "webhook": Telegram POSTs to /telegram/webhook, authenticated by the secret token header.

Both end in handle_update(). Safety properties kept from the original design:
- allowlist: a chat that is not a user is ignored - no model call, no media download. The one
  exception is /start and /id, which only answer with the chat id (what the admin needs to add
  the person), at most once an hour per chat.
- idempotency: an update delivered twice is processed once (the message id is stored).
- only private chats are served; groups and channels are ignored.
- one failing update never stops the others.

Internal message shape (what webhook_handler._process_single_message consumes):
    {"id": "<chat_id>:<message_id>", "from": "<chat_id>", "type": "text" | "audio" | "image" | "document",
     "text": {"body": ...}, "audio": {"id": file_id}, "image"/"document": {"id", "caption", "mime_type"},
     "context": {"forwarded": True}}
"""
import hmac
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Header, Request, Response

from src import config
from src.db.models import get_user_by_chat_id
from src.i18n import t
from src.integrations import telegram
from src.webhook_handler import _handle_explain_capabilities, _process_single_message

router = APIRouter()

_CHAT_ID_REPLY_COOLDOWN_SECONDS = 3600
_last_chat_id_reply: dict[str, float] = {}


def normalize_update(update: dict) -> dict | None:
    """The internal message for an update, or None if it is not something the bot serves."""
    message = update.get("message") if isinstance(update, dict) else None
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") or {}
    if chat.get("type") != "private" or "id" not in chat or "message_id" not in message:
        return None

    chat_id = str(chat["id"])
    normalized: dict = {"id": f"{chat_id}:{message['message_id']}", "from": chat_id}
    if message.get("forward_origin") or message.get("forward_date"):
        normalized["context"] = {"forwarded": True}

    caption = message.get("caption") or ""
    if message.get("text") is not None:
        normalized["type"] = "text"
        normalized["text"] = {"body": message["text"]}
    elif message.get("voice") or message.get("audio"):
        media = message.get("voice") or message.get("audio")
        normalized["type"] = "audio"
        normalized["audio"] = {"id": media.get("file_id"), "mime_type": media.get("mime_type", "audio/ogg")}
    elif message.get("photo"):
        largest = message["photo"][-1]  # sizes are listed smallest to largest
        normalized["type"] = "image"
        normalized["image"] = {"id": largest.get("file_id"), "caption": caption, "mime_type": "image/jpeg"}
    elif message.get("document"):
        document = message["document"]
        normalized["type"] = "document"
        normalized["document"] = {"id": document.get("file_id"), "caption": caption, "mime_type": document.get("mime_type", "")}
    else:
        return None  # stickers, locations, contacts, polls ... are not supported
    return normalized


def _answer_stranger(chat_id: str, text: str) -> None:
    """/start or /id from someone who is not a user yet: only tell them the id the admin needs."""
    if text.split("@")[0].strip().lower() not in ("/start", "/id"):
        print(f"[telegram] message from unknown chat ({chat_id}) - ignoring")
        return
    now = time.monotonic()
    if now - _last_chat_id_reply.get(chat_id, -_CHAT_ID_REPLY_COOLDOWN_SECONDS) < _CHAT_ID_REPLY_COOLDOWN_SECONDS:
        return
    _last_chat_id_reply[chat_id] = now
    telegram.send_text_message(to=chat_id, body=t("telegram.not_a_user", chat_id=chat_id))


def handle_update(update: dict) -> None:
    """Processes one update. Never raises."""
    try:
        message = normalize_update(update)
        if message is None:
            return
        chat_id = message["from"]
        text = (message.get("text") or {}).get("body", "").strip()
        user = get_user_by_chat_id(chat_id)
        if user is None:
            if message["type"] == "text":
                _answer_stranger(chat_id, text)
            else:
                print(f"[telegram] message from unknown chat ({chat_id}) - ignoring")
            return

        command = text.split("@")[0].strip().lower() if text.startswith("/") else ""
        if command == "/start":
            telegram.send_text_message(to=chat_id, body=t("telegram.start", name=f', {user["display_name"]}' if user["display_name"] else ""))
            return
        if command == "/id":
            telegram.send_text_message(to=chat_id, body=t("telegram.your_chat_id", chat_id=chat_id))
            return
        if command == "/help":
            telegram.send_text_message(to=chat_id, body=_handle_explain_capabilities(user))
            return

        _process_single_message(message)
    except Exception as e:
        print(f"[telegram] unexpected error handling an update: {e}")


@router.post("/telegram/webhook")
async def receive_update(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    secret = config.TELEGRAM_WEBHOOK_SECRET
    if not secret or not x_telegram_bot_api_secret_token or not hmac.compare_digest(x_telegram_bot_api_secret_token, secret):
        return Response(status_code=403)
    try:
        update = await request.json()
    except ValueError:
        return Response(status_code=200)
    handle_update(update)
    return Response(status_code=200)


# ---- long polling ----------------------------------------------------------------------------------------------

_WORKERS = 4
_executors: list[ThreadPoolExecutor] = []
_stop = threading.Event()


def _executor_for(chat_id: str) -> ThreadPoolExecutor:
    """One single-thread executor per shard: a chat's updates stay in order, different chats run in parallel."""
    return _executors[int(chat_id) % _WORKERS if chat_id.lstrip("-").isdigit() else 0]


def _poll_forever() -> None:
    offset: int | None = None
    telegram.delete_webhook()  # getUpdates is refused while a webhook is registered
    while not _stop.is_set():
        updates = telegram.get_updates(offset)
        if updates is None:
            _stop.wait(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            message = normalize_update(update)
            executor = _executor_for(message["from"]) if message else _executors[0]
            executor.submit(handle_update, update)


def start_telegram_intake() -> None:
    """Starts long polling or registers the webhook, depending on TELEGRAM_MODE. Called once at startup."""
    if not config.TELEGRAM_BOT_TOKEN:
        print("[telegram] TELEGRAM_BOT_TOKEN is not set - the bot will not receive messages")
        return
    if config.TELEGRAM_MODE == "webhook":
        if not (config.PUBLIC_BASE_URL and config.TELEGRAM_WEBHOOK_SECRET):
            print("[telegram] webhook mode needs PUBLIC_BASE_URL and TELEGRAM_WEBHOOK_SECRET - not registered")
            return
        ok = telegram.set_webhook(f"{config.PUBLIC_BASE_URL}/telegram/webhook", config.TELEGRAM_WEBHOOK_SECRET)
        print(f"[telegram] webhook {'registered' if ok else 'registration FAILED'}")
        return
    _executors.extend(ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"telegram-{i}") for i in range(_WORKERS))
    threading.Thread(target=_poll_forever, name="telegram-poller", daemon=True).start()
    print("[telegram] long polling started")
