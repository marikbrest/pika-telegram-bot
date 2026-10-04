"""
Privacy audit (2026-09-26) - three print() sites found to be echoing full
user content (or Gemini's reconstruction of it) into the shared process
log, readable via the admin panel's /admin/logs. Regression coverage so
none of these silently comes back.
"""
from unittest.mock import MagicMock, patch

from src.integrations.gemini import _truncate_for_log


def test_truncate_for_log_leaves_short_text_untouched():
    assert _truncate_for_log("short") == "short"


def test_truncate_for_log_truncates_long_text_and_notes_the_original_length():
    text = "א" * 500
    result = _truncate_for_log(text, max_chars=200)
    assert len(result) < len(text)
    assert result.startswith("א" * 200)
    assert "500 chars total" in result


def test_telegram_missing_token_log_does_not_include_the_message_body(capsys):
    from src.integrations import telegram

    with patch.object(telegram, "TELEGRAM_BOT_TOKEN", ""):
        assert telegram.send_text_message(to="972500000001", body="תוכן פרטי שאסור שיודלף ללוג") is False

    assert "תוכן פרטי שאסור שיודלף ללוג" not in capsys.readouterr().out


def test_gemini_adapter_no_function_call_log_does_not_repr_the_response(capsys):
    from src.tools.gemini_adapter import classify_with_tools

    response = MagicMock()
    response.function_calls = []
    response.candidates = [MagicMock(finish_reason="STOP")]
    response.text = "תוכן פרטי שהמשתמש כתב, לא אמור להופיע בלוג"
    client = MagicMock()
    client.models.generate_content.return_value = response

    with patch("src.tools.gemini_adapter._get_client", return_value=client), \
         patch("src.tools.gemini_adapter._log_usage_safe"):
        classify_with_tools("some contents", [])

    captured = capsys.readouterr()
    assert "תוכן פרטי שהמשתמש כתב" not in captured.out
    assert "finish_reason" in captured.out
