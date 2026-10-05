from urllib.parse import parse_qs, urlparse

from helpers import utc
from nodemeet import Booking, BookingStatus, build_ics, calendar_links


def booking(**kw):
    kw.setdefault("title", "Demo, with; specials")
    return Booking("h", utc(2026, 10, 5, 9), utc(2026, 10, 5, 9, 30), "Ann, B", "ann@x.test", **kw)


def test_ics_structure_and_escaping():
    text = build_ics(booking(), organizer_email="o@x.test", organizer_name="Org",
                     url="https://x.test/r/1")
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert "DTSTART:20261005T090000Z" in text and "DTEND:20261005T093000Z" in text
    assert "SUMMARY:Demo\\, with\\; specials" in text
    assert 'ATTENDEE;CN="Ann, B"' in text
    assert "METHOD:REQUEST" in text and "BEGIN:VALARM" in text
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))


def test_ics_cancel_and_folding():
    b = booking(status=BookingStatus.CANCELLED, title="x" * 200)
    text = build_ics(b)
    assert "METHOD:CANCEL" in text and "STATUS:CANCELLED" in text and "VALARM" not in text
    unfolded = text.replace("\r\n ", "")
    assert "SUMMARY:" + "x" * 200 in unfolded


def test_calendar_links():
    links = calendar_links(booking(), join_url="https://x.test/r/1")
    g = parse_qs(urlparse(links["google"]).query)
    assert g["dates"] == ["20261005T090000Z/20261005T093000Z"]
    assert g["location"] == ["https://x.test/r/1"]
    o = parse_qs(urlparse(links["outlook"]).query)
    assert o["startdt"] == ["2026-10-05T09:00:00Z"] and "outlook.live.com" in links["outlook"]
    assert "outlook.office.com" in links["office365"]
