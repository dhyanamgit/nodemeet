"""Scheduling: availability, slot finding, bookings, invites and reminders."""
from .availability import Availability, parse_days, parse_windows
from .calendar_links import calendar_links, google_calendar_link, outlook_calendar_link
from .ics import build_ics
from .slots import Slot, find_slots, group_by_local_date, is_free

__all__ = [
    "Availability", "parse_days", "parse_windows", "Slot", "find_slots", "group_by_local_date",
    "is_free", "build_ics", "calendar_links", "google_calendar_link", "outlook_calendar_link",
]
from .booking import BookingService  # noqa: E402
from .reminders import ReminderScheduler  # noqa: E402

__all__ += ["BookingService", "ReminderScheduler"]
