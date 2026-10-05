"""Exception hierarchy for nodemeet."""
from __future__ import annotations


class NodeMeetError(Exception):
    """Base class for every error raised by nodemeet."""


class InvalidToken(NodeMeetError):
    """A join token is malformed, has a bad signature, or targets another room."""


class ExpiredToken(InvalidToken):
    """A join token was valid but has expired."""


class JoinRejected(NodeMeetError):
    """Raise from a ``before_join`` hook to refuse a participant.

    The ``reason`` is sent to the client.
    """

    def __init__(self, reason: str = "join rejected", code: str = "rejected") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


class RoomNotFound(NodeMeetError):
    """The requested room does not exist."""


class RoomFull(NodeMeetError):
    """The room has an explicit ``max_participants`` and it is reached."""


class SchedulingError(NodeMeetError):
    """Base class for scheduling problems."""


class SlotUnavailable(SchedulingError):
    """The requested time is not bookable (taken, outside hours, too soon...)."""


class BookingNotFound(SchedulingError):
    """No booking with that id."""


class AvailabilityNotFound(SchedulingError):
    """The host has no availability configured."""


class SFUUnavailable(NodeMeetError):
    """The SFU was requested but ``aiortc`` is not installed."""
