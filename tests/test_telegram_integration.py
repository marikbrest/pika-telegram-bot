"""src/integrations/telegram.py against a faked Bot API (no network)."""
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.integrations import telegram


def _resp(status=200, body=None, content=b"", headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body if body is not None else {"ok": status == 200}
    r.content = content
    r.headers = headers or {}
    return r


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("src.integrations.telegram.time.sleep"):
        yield


def test_to_html_converts_bold_and_escapes_markup():
    assert telegram.to_html("*hi* a < b & c") == "<b>hi</b> a &lt; b &amp; c"


def test_chunks_leave_short_text_alone_and_split_long_text_on_line_boundaries():
    assert telegram._chunks("short") == ["short"]
    text = "\n".join(f"line {i} " + "x" * 90 for i in range(100))
    parts = telegram._chunks(text)
    assert len(parts) > 1
    assert all(len(p) <= telegram.MAX_MESSAGE_CHARS for p in parts)
    assert "\n".join(parts).replace("\n\n", "\n") == text


def test_chunks_hard_cut_a_single_endless_line():
    parts = telegram._chunks("y" * (telegram.MAX_MESSAGE_CHARS * 2 + 5))
    assert [len(p) for p in parts] == [telegram.MAX_MESSAGE_CHARS, telegram.MAX_MESSAGE_CHARS, 5]


def test_send_text_message_posts_html_to_the_chat():
    with patch.object(telegram._client, "post", return_value=_resp()) as post:
        assert telegram.send_text_message("111", "*hello*") is True
    payload = post.call_args.kwargs["json"]
    assert payload == {"chat_id": "111", "text": "<b>hello</b>", "parse_mode": "HTML"}
    assert post.call_args.args[0].endswith("/sendMessage")


def test_send_text_message_falls_back_to_plain_text_when_markup_is_refused():
    refused = _resp(400, {"ok": False, "description": "Bad Request: can't parse entities"})
    with patch.object(telegram._client, "post", side_effect=[refused, _resp()]) as post:
        assert telegram.send_text_message("111", "a *b*") is True
    assert post.call_args_list[1].kwargs["json"] == {"chat_id": "111", "text": "a *b*"}


def test_send_text_message_does_not_retry_a_permanent_client_error():
    blocked = _resp(403, {"ok": False, "description": "Forbidden: bot was blocked by the user"})
    with patch.object(telegram._client, "post", return_value=blocked) as post:
        assert telegram.send_text_message("111", "hi") is False
    assert post.call_count == 1


def test_send_text_message_retries_rate_limits_and_server_errors():
    limited = _resp(429, {"ok": False, "parameters": {"retry_after": 1}})
    broken = _resp(502, {"ok": False})
    with patch.object(telegram._client, "post", side_effect=[limited, broken, _resp()]) as post:
        assert telegram.send_text_message("111", "hi") is True
    assert post.call_count == 3


def test_send_text_message_gives_up_after_the_retries_and_never_raises():
    with patch.object(telegram._client, "post", side_effect=httpx.ConnectError("down")) as post:
        assert telegram.send_text_message("111", "hi") is False
    assert post.call_count == 1 + len(telegram._RETRY_DELAYS_SECONDS)


def test_send_text_message_reports_failure_if_any_chunk_fails():
    long_text = "a" * (telegram.MAX_MESSAGE_CHARS + 10)
    with patch.object(telegram._client, "post", side_effect=[_resp(), _resp(400, {"ok": False, "description": "nope"})]):
        assert telegram.send_text_message("111", long_text) is False


def test_missing_token_sends_nothing_and_logs_no_content(capsys):
    with patch.object(telegram, "TELEGRAM_BOT_TOKEN", ""), patch.object(telegram._client, "post") as post:
        assert telegram.send_text_message("111", "secret body") is False
    post.assert_not_called()
    assert "secret body" not in capsys.readouterr().out


def test_send_reaction_uses_the_raw_message_id_from_the_internal_id():
    with patch.object(telegram._client, "post", return_value=_resp()) as post:
        assert telegram.send_reaction("111", "111:42", "👍") is True
    assert post.call_args.kwargs["json"] == {
        "chat_id": "111", "message_id": 42, "reaction": [{"type": "emoji", "emoji": "👍"}],
    }


def test_send_reaction_rejects_a_malformed_id_without_calling_the_api():
    with patch.object(telegram._client, "post") as post:
        assert telegram.send_reaction("111", "garbage", "👍") is False
    post.assert_not_called()


def test_send_image_bytes_uploads_a_photo():
    with patch.object(telegram._client, "post", return_value=_resp()) as post:
        assert telegram.send_image_bytes("111", b"\x89PNG", "image/png") is True
    assert post.call_args.args[0].endswith("/sendPhoto")
    assert post.call_args.kwargs["files"]["photo"][1] == b"\x89PNG"


def test_download_media_fetches_via_getfile_and_guesses_the_mime_from_the_path():
    get_file = _resp(200, {"ok": True, "result": {"file_path": "voice/file_1.oga"}})
    with patch.object(telegram._client, "post", return_value=get_file), \
         patch.object(telegram._client, "get", return_value=_resp(200, content=b"AUDIO", headers={"content-type": "application/octet-stream"})):
        assert telegram.download_media("FILEID") == (b"AUDIO", "audio/ogg")


def test_download_media_returns_none_when_telegram_has_no_file():
    with patch.object(telegram._client, "post", return_value=_resp(400, {"ok": False, "description": "file is too big"})):
        assert telegram.download_media("FILEID") is None


def test_get_updates_passes_the_offset_and_returns_none_on_failure():
    with patch.object(telegram._client, "post", return_value=_resp(200, {"ok": True, "result": [{"update_id": 5}]})) as post:
        assert telegram.get_updates(5) == [{"update_id": 5}]
    assert post.call_args.kwargs["json"]["offset"] == 5
    with patch.object(telegram._client, "post", return_value=_resp(401, {"ok": False, "description": "Unauthorized"})):
        assert telegram.get_updates(None) is None


def test_set_webhook_sends_the_secret_token():
    with patch.object(telegram._client, "post", return_value=_resp()) as post:
        assert telegram.set_webhook("https://x.example/telegram/webhook", "s3cret") is True
    assert post.call_args.kwargs["json"]["secret_token"] == "s3cret"
