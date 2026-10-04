"""
check_and_send_reminders' actual delivery path - as opposed to the pure
compute_next_trigger date math in test_scheduler_reminders.py. Covers what is sent
and, when a send to a contact fails (they never started the bot, or blocked
it), who is told.
"""
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src.db import models
from src.scheduler import check_and_send_reminders

NOW = datetime.now(ZoneInfo("UTC"))
_DUE = (NOW - timedelta(minutes=1)).isoformat()


def _seed_reminder(user_id, content, recipient_contact_id=None):
    conn = models.get_connection()
    try:
        conn.execute(
            "INSERT INTO reminders (user_id, content, schedule_type, schedule_time, schedule_days, "
            "next_trigger_at, recipient_contact_id) VALUES (?, ?, 'once', '09:00', NULL, ?, ?)",
            (user_id, content, _DUE, recipient_contact_id),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_contact(owner_user_id, name, chat_id):
    conn = models.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO contacts (owner_user_id, name, chat_id) VALUES (?, ?, ?)",
            (owner_user_id, name, chat_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _set_kid_facing_role(user_id: int, role: str) -> None:
    conn = models.get_connection()
    try:
        conn.execute("UPDATE users SET kid_facing_role = ? WHERE id = ?", (role, user_id))
        conn.commit()
    finally:
        conn.close()


def test_kid_facing_reminder_prefix_uses_kid_facing_role_not_display_name(db_path, make_user):
    """2026-09-25: Yossi asked for a split - a message TO a kid should say
    "אבא"/"אימא", never the parent's own name, even though the bot still
    addresses the parent themselves by their real name everywhere else."""
    owner_id = make_user(chat_id="972500000001", display_name="יוסי")
    _set_kid_facing_role(owner_id, "אבא")
    contact_id = _seed_contact(owner_id, "דני", "972500000099")
    _seed_reminder(owner_id, "לקנות חלב", recipient_contact_id=contact_id)

    with patch("src.integrations.telegram.send_text_message", return_value=True) as mock_send:
        check_and_send_reminders()

    body = mock_send.call_args.kwargs["body"]
    assert "מ-אבא" in body
    assert "יוסי" not in body


def test_kid_facing_reminder_prefix_falls_back_to_display_name_when_role_unset(db_path, make_user):
    """A user with no kid_facing_role (everyone except Yossi/Ronit) keeps
    the old behavior - their real display_name in the prefix."""
    owner_id = make_user(chat_id="972500000001", display_name="Gil")
    contact_id = _seed_contact(owner_id, "דני", "972500000099")
    _seed_reminder(owner_id, "לקנות חלב", recipient_contact_id=contact_id)

    with patch("src.integrations.telegram.send_text_message", return_value=True) as mock_send:
        check_and_send_reminders()

    assert "מ-Gil" in mock_send.call_args.kwargs["body"]


def test_due_reminder_is_sent_as_plain_text(db_path, make_user):
    user_id = make_user(chat_id="972500000001")
    _seed_reminder(user_id, "לקחת תרופה")

    with patch("src.integrations.telegram.send_text_message", return_value=True) as mock_send:
        check_and_send_reminders()

    mock_send.assert_called_once()
    kwargs = mock_send.call_args.kwargs
    assert kwargs["to"] == "972500000001"
    assert "לקחת תרופה" in kwargs["body"]
    assert set(kwargs) == {"to", "body"}  # no template arguments exist any more

    assert models.list_active_reminders(user_id) == []  # "once" deactivated after a successful send


def test_multiple_due_reminders_are_consolidated_into_one_message(db_path, make_user):
    user_id = make_user(chat_id="972500000001")
    _seed_reminder(user_id, "לקחת תרופה")
    _seed_reminder(user_id, "להתקשר לרופא")

    with patch("src.integrations.telegram.send_text_message", return_value=True) as mock_send:
        check_and_send_reminders()

    mock_send.assert_called_once()
    body = mock_send.call_args.kwargs["body"]
    assert "לקחת תרופה" in body
    assert "להתקשר לרופא" in body


def _fail_only_for(contact_number):
    """A send_text_message stand-in: the contact's chat refuses the message, everyone else's works."""
    return lambda to, body: to != contact_number


def test_contact_reminder_still_notifies_owner_when_delivery_to_the_contact_fails(db_path, make_user):
    """When the contact cannot be reached (never pressed Start, or blocked the bot) the owner must
    still be told and the reminder must still be retired."""
    user_id = make_user(chat_id="972500000001", display_name="יוסי")
    contact_id = _seed_contact(user_id, "רונית", "972500000099")
    _seed_reminder(user_id, "לקנות חלב", recipient_contact_id=contact_id)

    with patch("src.integrations.telegram.send_text_message", side_effect=_fail_only_for("972500000099")) as mock_send:
        check_and_send_reminders()

    owner_calls = [c for c in mock_send.call_args_list if c.kwargs["to"] == "972500000001"]
    assert len(owner_calls) == 1
    assert "רונית" in owner_calls[0].kwargs["body"]

    assert models.list_active_reminders(user_id) == []  # deactivated even though delivery failed


def test_list_reminder_delivery_failure_notification_numbers_only_returns_opted_in_active_users(db_path, make_user):
    opted_in_id = make_user(chat_id="972500000001")
    make_user(chat_id="972500000002")  # not opted in
    opted_in_but_inactive_id = make_user(chat_id="972500000003", is_active=False)
    _set_notify_on_delivery_failure(opted_in_id)
    _set_notify_on_delivery_failure(opted_in_but_inactive_id)

    numbers = models.list_reminder_delivery_failure_notification_numbers()
    assert numbers == ["972500000001"]


def _set_notify_on_delivery_failure(user_id: int) -> None:
    conn = models.get_connection()
    try:
        conn.execute("UPDATE users SET notify_on_reminder_delivery_failure = 1 WHERE id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def test_delivery_failure_also_notifies_the_other_opted_in_parent(db_path, make_user):
    """Both parents should hear about a delivery failure to a kid, not just whichever of them
    created the reminder."""
    owner_id = make_user(chat_id="972500000001", display_name="יוסי")
    other_parent_id = make_user(chat_id="972500000002", display_name="רונית")
    _set_notify_on_delivery_failure(other_parent_id)
    contact_id = _seed_contact(owner_id, "דני", "972500000099")
    _seed_reminder(owner_id, "לקנות חלב", recipient_contact_id=contact_id)

    with patch("src.integrations.telegram.send_text_message", side_effect=_fail_only_for("972500000099")) as mock_send:
        check_and_send_reminders()

    notified_numbers = {call.kwargs["to"] for call in mock_send.call_args_list} - {"972500000099"}
    assert notified_numbers == {"972500000001", "972500000002"}


def test_delivery_failure_does_not_double_notify_when_owner_is_the_opted_in_parent(db_path, make_user):
    owner_id = make_user(chat_id="972500000001", display_name="יוסי")
    _set_notify_on_delivery_failure(owner_id)
    contact_id = _seed_contact(owner_id, "דני", "972500000099")
    _seed_reminder(owner_id, "לקנות חלב", recipient_contact_id=contact_id)

    with patch("src.integrations.telegram.send_text_message", side_effect=_fail_only_for("972500000099")) as mock_send:
        check_and_send_reminders()

    owner_calls = [c for c in mock_send.call_args_list if c.kwargs["to"] == "972500000001"]
    assert len(owner_calls) == 1  # not sent twice to the same chat
