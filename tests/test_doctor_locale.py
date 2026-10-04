"""scripts/doctor.py: LOCALE and TELEGRAM_MODE validation."""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_check(monkeypatch, capsys, **env):
    for name in ("LOCALE", "TELEGRAM_MODE", "TELEGRAM_WEBHOOK_SECRET", "PUBLIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location("doctor", os.path.join(ROOT, "scripts", "doctor.py"))
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)
    doctor.check_required_env()
    return capsys.readouterr().out


def _line(out, name):
    return next((l for l in out.splitlines() if name in l), "")


def test_unsupported_locale_fails(monkeypatch, capsys):
    line = _line(_run_check(monkeypatch, capsys, LOCALE="fr"), "LOCALE")
    assert "FAIL" in line


def test_english_locale_is_ok(monkeypatch, capsys):
    line = _line(_run_check(monkeypatch, capsys, LOCALE="en"), "LOCALE")
    assert "OK" in line and line.endswith("en")


def test_polling_is_the_default_and_needs_no_public_url(monkeypatch, capsys):
    assert "polling (no public URL needed)" in _run_check(monkeypatch, capsys)


def test_webhook_mode_without_url_and_secret_fails(monkeypatch, capsys):
    assert "FAIL" in _line(_run_check(monkeypatch, capsys, TELEGRAM_MODE="webhook"), "TELEGRAM_MODE")
