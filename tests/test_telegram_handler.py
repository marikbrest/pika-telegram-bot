"""src/telegram_handler.py: update normalization, the allowlist, commands, webhook auth, polling."""
from unittest.mock import patch

import pytest

from src import telegram_handler
from src.db import models
from tests.conftest import post_update, telegram_text_update


@pytest.fixture(autouse=True)
def _reset_stranger_cooldown():
    telegram_handler._last_chat_id_reply.clear()


def _base(**message):
    return {"update_id": 1, "message": {"message_id": 7, "chat": {"id": 555, "type": "private"}, **message}}


def test_normalize_text():
    assert telegram_handler.normalize_update(_base(text="שלום")) == {
        "id": "555:7", "from": "555", "type": "text", "text": {"body": "שלום"},
    }


def test_normalize_voice_photo_document():
    voice = telegram_handler.normalize_update(_base(voice={"file_id": "V1", "mime_type": "audio/ogg"}))
    assert voice["type"] == "audio" and voice["audio"]["id"] == "V1"
    photo = telegram_handler.normalize_update(_base(photo=[{"file_id": "S"}, {"file_id": "L"}], caption="cap"))
    assert photo["type"] == "image" and photo["image"] == {"id": "L", "caption": "cap", "mime_type": "image/jpeg"}
    doc = telegram_handler.normalize_update(_base(document={"file_id": "D", "mime_type": "application/pdf"}))
    assert doc["type"] == "document" and doc["document"]["mime_type"] == "application/pdf"


def test_normalize_marks_forwarded_messages():
    assert telegram_handler.normalize_update(_base(text="x", forward_origin={"type": "user"}))["context"] == {"forwarded": True}


@pytest.mark.parametrize("update", [
    {}, {"update_id": 1}, {"message": "nope"},
    {"message": {"message_id": 1, "chat": {"id": 1, "type": "group"}, "text": "x"}},
    {"message": {"message_id": 1, "chat": {"id": 1, "type": "private"}, "sticker": {}}},
    {"message": {"chat": {"id": 1, "type": "private"}, "text": "x"}},
])
def test_normalize_ignores_what_it_does_not_serve(update):
    assert telegram_handler.normalize_update(update) is None


def test_webhook_rejects_a_missing_or_wrong_secret(client):
    update = telegram_text_update("555", "hi")
    assert client.post("/telegram/webhook", json=update).status_code == 403
    assert post_update(client, update, secret="wrong").status_code == 403


def test_webhook_rejects_everything_when_no_secret_is_configured(client):
    with patch.object(telegram_handler.config, "TELEGRAM_WEBHOOK_SECRET", ""):
        assert post_update(client, telegram_text_update("555", "hi"), secret="").status_code == 403


def test_a_user_message_goes_through_the_pipeline(client, make_user):
    make_user(chat_id="555")
    with patch("src.telegram_handler._process_single_message") as process:
        assert post_update(client, telegram_text_update("555", "מה המצב")).status_code == 200
    process.assert_called_once()
    assert process.call_args.args[0]["text"]["body"] == "מה המצב"


def test_an_unknown_chat_is_ignored_without_a_model_call(client):
    with patch("src.telegram_handler._process_single_message") as process, \
         patch("src.integrations.telegram.send_text_message") as send:
        post_update(client, telegram_text_update("999", "hello there"))
    process.assert_not_called()
    send.assert_not_called()


def test_an_unknown_chat_asking_start_is_told_its_id_once_per_hour(client):
    with patch("src.integrations.telegram.send_text_message") as send:
        post_update(client, telegram_text_update("999", "/start"))
        post_update(client, telegram_text_update("999", "/id", message_id=2))
    send.assert_called_once()
    assert "999" in send.call_args.kwargs["body"]


def test_start_greets_a_known_user_by_name(client, make_user):
    make_user(chat_id="555", display_name="Dana")
    with patch("src.integrations.telegram.send_text_message") as send, \
         patch("src.telegram_handler._process_single_message") as process:
        post_update(client, telegram_text_update("555", "/start"))
    process.assert_not_called()
    assert "Dana" in send.call_args.kwargs["body"]


def test_id_command_replies_with_the_chat_id(client, make_user):
    make_user(chat_id="555")
    with patch("src.integrations.telegram.send_text_message") as send:
        post_update(client, telegram_text_update("555", "/id"))
    assert "555" in send.call_args.kwargs["body"]


def test_help_lists_the_capabilities(client, make_user):
    make_user(chat_id="555")
    with patch("src.integrations.telegram.send_text_message") as send:
        post_update(client, telegram_text_update("555", "/help"))
    assert send.call_args.kwargs["body"]


def test_a_failing_update_never_raises_to_telegram(client, make_user):
    make_user(chat_id="555")
    with patch("src.telegram_handler._process_single_message", side_effect=RuntimeError("boom")):
        assert post_update(client, telegram_text_update("555", "hi")).status_code == 200


def test_a_malformed_body_is_acknowledged(client):
    r = client.post("/telegram/webhook", content=b"not json", headers={"X-Telegram-Bot-Api-Secret-Token": telegram_handler.config.TELEGRAM_WEBHOOK_SECRET})
    assert r.status_code == 200


def test_the_same_update_delivered_twice_is_answered_once(client, make_user, db_path):
    make_user(chat_id="555")
    with patch("src.webhook_handler._classify_text_with_cutover", return_value={"intent": "chat", "reply": "hi"}), \
         patch("src.webhook_handler.send_text_message", return_value=True) as send:
        post_update(client, telegram_text_update("555", "hello"))
        post_update(client, telegram_text_update("555", "hello"))
    assert send.call_count == 1


def test_the_reply_is_sent_to_the_senders_chat(client, make_user, db_path):
    make_user(chat_id="555")
    with patch("src.webhook_handler._classify_text_with_cutover", return_value={"intent": "chat", "reply": "hi there"}), \
         patch("src.webhook_handler.send_text_message", return_value=True) as send:
        post_update(client, telegram_text_update("555", "hello"))
    assert send.call_args.kwargs["to"] == "555"
    assert models.get_user_by_chat_id("555") is not None


def test_start_intake_without_a_token_starts_nothing(capsys):
    with patch.object(telegram_handler.config, "TELEGRAM_BOT_TOKEN", ""), \
         patch("src.telegram_handler.threading.Thread") as thread:
        telegram_handler.start_telegram_intake()
    thread.assert_not_called()
    assert "TELEGRAM_BOT_TOKEN is not set" in capsys.readouterr().out


def test_webhook_mode_registers_the_webhook(monkeypatch):
    monkeypatch.setattr(telegram_handler.config, "TELEGRAM_MODE", "webhook")
    monkeypatch.setattr(telegram_handler.config, "PUBLIC_BASE_URL", "https://bot.example.org")
    with patch("src.telegram_handler.telegram.set_webhook", return_value=True) as set_webhook:
        telegram_handler.start_telegram_intake()
    set_webhook.assert_called_once_with("https://bot.example.org/telegram/webhook", telegram_handler.config.TELEGRAM_WEBHOOK_SECRET)


def test_webhook_mode_without_a_public_url_registers_nothing(monkeypatch):
    monkeypatch.setattr(telegram_handler.config, "TELEGRAM_MODE", "webhook")
    monkeypatch.setattr(telegram_handler.config, "PUBLIC_BASE_URL", "")
    with patch("src.telegram_handler.telegram.set_webhook") as set_webhook:
        telegram_handler.start_telegram_intake()
    set_webhook.assert_not_called()


def test_polling_loop_advances_the_offset_and_dispatches_each_update():
    handled = []
    batches = [[{"update_id": 10}, {"update_id": 11}], None, []]
    offsets = []

    def fake_get_updates(offset):
        offsets.append(offset)
        if not batches:
            telegram_handler._stop.set()
            return []
        return batches.pop(0)

    class Inline:
        def submit(self, fn, *a):
            fn(*a)

    telegram_handler._stop.clear()
    try:
        with patch("src.telegram_handler.telegram.delete_webhook"), \
             patch("src.telegram_handler.telegram.get_updates", side_effect=fake_get_updates), \
             patch.object(telegram_handler._stop, "wait"), \
             patch("src.telegram_handler.handle_update", side_effect=handled.append), \
             patch.object(telegram_handler, "_executors", [Inline()] * telegram_handler._WORKERS):
            telegram_handler._poll_forever()
    finally:
        telegram_handler._stop.clear()
    assert [u["update_id"] for u in handled] == [10, 11]
    assert offsets[:2] == [None, 12]
