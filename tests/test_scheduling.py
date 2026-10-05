from datetime import date, time, timedelta

import pytest

from helpers import utc
from nodemeet.scheduling import Availability, find_slots, group_by_local_date, is_free, parse_days

NOW = utc(2026, 10, 4, 0, 0)  # Sunday


def av(**kw):
    kw.setdefault("hours", {"mon-fri": "09:00-12:00"})
    return Availability.from_hours("h1", kw.pop("tz", "UTC"), kw.pop("hours"), **kw)


def test_parse_days_and_windows():
    assert parse_days("mon-fri") == [0, 1, 2, 3, 4]
    assert parse_days("sat,sun") == [5, 6]
    assert parse_days("fri-mon") == [4, 5, 6, 0]
    a = av(hours={"mon": "13:00-17:00, 09:00-12:00"})
    assert a.weekly[0] == [(time(9), time(12)), (time(13), time(17))]
    with pytest.raises(ValueError):
        av(hours={"mon": "12:00-09:00"})
    with pytest.raises(ValueError):
        Availability("h", timezone="Mars/Olympus")


def test_basic_slots_and_weekends():
    slots = find_slots(av(), date(2026, 10, 4), date(2026, 10, 5), now=NOW)
    assert len(slots) == 6  # Sunday closed, Monday 09-12 in 30-min slots
    assert slots[0].start == utc(2026, 10, 5, 9) and slots[-1].end == utc(2026, 10, 5, 12)


def test_timezone_conversion():
    a = av(tz="Asia/Kolkata")
    s = find_slots(a, date(2026, 10, 5), date(2026, 10, 5), now=NOW)[0]
    assert s.start == utc(2026, 10, 5, 3, 30)  # 09:00 IST
    assert s.to_dict("America/New_York")["local_start"].startswith("2026-10-04T23:30")


def test_dst_transition_keeps_wall_clock_hours():
    a = av(tz="Europe/London", hours={"sun": "09:00-10:00"})
    before = find_slots(a, date(2026, 10, 18), date(2026, 10, 18), now=NOW)
    after = find_slots(a, date(2026, 10, 25), date(2026, 10, 25), now=NOW)  # BST ends Oct 25
    assert before[0].start == utc(2026, 10, 18, 8)  # BST = UTC+1
    assert after[0].start == utc(2026, 10, 25, 9)  # GMT = UTC+0


def test_buffers_block_neighbours():
    a = av(buffer_before=15, buffer_after=15)
    busy = [(utc(2026, 10, 5, 10), utc(2026, 10, 5, 10, 30))]
    starts = [s.start.time() for s in find_slots(a, date(2026, 10, 5), date(2026, 10, 5), busy=busy, now=NOW)]
    assert starts == [time(9), time(11), time(11, 30)]
    assert is_free(utc(2026, 10, 5, 11), utc(2026, 10, 5, 11, 30), busy)


def test_min_notice_horizon_and_blackouts():
    now = utc(2026, 10, 5, 9, 10)
    a = av(min_notice_minutes=60, max_days_ahead=2, blackout_dates=["2026-10-06"])
    slots = find_slots(a, date(2026, 10, 5), date(2026, 10, 9), now=now)
    days = {s.start.date() for s in slots}
    assert slots[0].start == utc(2026, 10, 5, 10, 30)
    assert date(2026, 10, 6) not in days  # blackout
    assert all(s.start <= now + timedelta(days=2) for s in slots)


def test_overrides_step_and_duration():
    a = av(overrides={"2026-10-05": "14:00-15:00"}, step_minutes=15)
    slots = find_slots(a, date(2026, 10, 5), date(2026, 10, 5), now=NOW)
    assert [s.start.time() for s in slots] == [time(14), time(14, 15), time(14, 30)]
    long = find_slots(av(), date(2026, 10, 5), date(2026, 10, 5), now=NOW, duration_minutes=60)
    assert len(long) == 3


def test_until_midnight_and_grouping():
    a = av(hours={"mon": "23:00-24:00"})
    slots = find_slots(a, date(2026, 10, 5), date(2026, 10, 5), now=NOW)
    assert slots[-1].end == utc(2026, 10, 6, 0)
    grouped = group_by_local_date(slots, "Asia/Tokyo")
    assert list(grouped) == ["2026-10-06"]


def test_availability_serialisation_roundtrip():
    a = av(tz="Asia/Kolkata", buffer_after=10, blackout_dates=["2026-12-25"],
           overrides={"2026-10-10": "10:00-11:00"}, host_email="h@example.com")
    assert Availability.from_dict(a.to_dict()) == a
