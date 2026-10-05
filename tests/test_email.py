from unittest import mock

from helpers import run, utc
from nodemeet import Booking, EmailContext, EmailMessage, EmailTemplates, SMTPMailer


def test_smtp_build_includes_calendar_part():
    m = SMTPMailer("smtp.test", sender="noreply@x.test", sender_name="Acme")
    msg = m.build(EmailMessage(["a@x.test"], "Hi", "plain", "<b>html</b>",
                               [("invite.ics", "BEGIN:VCALENDAR", "text/calendar")],
                               calendar_method="REQUEST"))
    raw = msg.as_string()
    assert "From: Acme <noreply@x.test>" in raw
    assert 'text/calendar; method="REQUEST"' in raw
    assert "multipart/alternative" in raw


def test_smtp_send_uses_starttls_and_login():
    m = SMTPMailer("smtp.test", 587, username="u", password="p", sender="n@x.test")
    with mock.patch("smtplib.SMTP") as smtp:
        run(m.send(EmailMessage(["a@x.test"], "Hi", "body")))
    server = smtp.return_value
    server.starttls.assert_called_once()
    server.login.assert_called_once_with("u", "p")
    server.send_message.assert_called_once()


def test_templates_render_in_recipient_timezone():
    b = Booking("h", utc(2026, 10, 5, 9), utc(2026, 10, 5, 9, 30), "Ann", "a@x.test", title="Sync")
    t = EmailTemplates()
    ctx = EmailContext(join_url="https://x.test/j", timezone="Asia/Kolkata",
                       calendar_links={"google": "https://g"}, manage_url="https://m")
    subject, text, html = t.confirmation(b, ctx)
    assert "14:30 Asia/Kolkata" in subject
    assert "https://x.test/j" in text and "https://m" in html
    subject, _, _ = t.reminder(b, EmailContext(minutes_before=1440, timezone="UTC"))
    assert subject.endswith("in 1 day")
    subject, text, _ = t.cancellation(b, EmailContext(reason="sick", timezone="UTC"))
    assert subject.startswith("Cancelled") and "sick" in text
