"""Every event nodemeet can send to your webhooks / taps, with a one-line description.

Subscribe to exact names or patterns (``booking.*``, ``*.failed``, ``*``). Events marked
*noisy* (high-volume: votes, reactions, caption lines, mic toggles...) are only delivered to
endpoints that name them (or their pattern) explicitly - ``*`` / "everything" skips them.

    GET /api/webhooks/events   -> this catalogue as JSON
"""
from __future__ import annotations

import fnmatch
from typing import Dict, Iterable, List, Optional, Tuple

# name -> (description, noisy)
CATALOG: Dict[str, Tuple[str, bool]] = {
    # rooms & meetings
    "room.created": ("A room was created", False),
    "room.updated": ("Room settings changed (waiting room, join list, lock, branding...)", False),
    "room.closed": ("A room closed (everyone left / ended)", False),
    "room.locked": ("A moderator locked the room", False),
    "room.unlocked": ("A moderator unlocked the room", False),
    "meeting.started": ("First person joined a room: the meeting is live", False),
    "meeting.ended": ("Last person left: duration, peak size, everyone who attended", False),
    "meeting.ended_by_host": ("A moderator ended the meeting for everyone", False),
    # people
    "participant.joined": ("Someone joined a meeting", False),
    "participant.left": ("Someone left (with how long they stayed)", False),
    "participant.waiting": ("Someone is in the waiting room", False),
    "participant.admitted": ("Someone was let in from the waiting room", False),
    "participant.denied": ("Someone was turned away from the waiting room", False),
    "participant.blocked_attempt": ("A blocked person tried to rejoin", False),
    "participant.kicked": ("A moderator removed someone", False),
    "participant.banned": ("A moderator blocked someone", False),
    "participant.unbanned": ("Someone was unblocked", False),
    "participant.role_changed": ("Someone's role changed", False),
    "participant.muted": ("A moderator muted someone", False),
    "participant.renamed": ("Someone changed their display name", False),
    "participant.hand_raised": ("Someone raised their hand", False),
    "participant.hand_lowered": ("Someone lowered their hand", True),
    "participant.reaction": ("Someone sent an emoji reaction", True),
    "participant.media_changed": ("Someone turned mic / camera on or off", True),
    "participant.moderated": ("Any moderator action (raw)", False),
    "screen_share.started": ("Someone started sharing their screen", False),
    "screen_share.stopped": ("Someone stopped sharing their screen", False),
    # chat & collaboration
    "chat.message": ("A chat message was sent", False),
    "chat.deleted": ("A chat message was deleted", False),
    "chat.pinned": ("A chat message was pinned / unpinned", False),
    "poll.created": ("A poll was created", False),
    "poll.voted": ("Someone voted in a poll", True),
    "poll.closed": ("A poll closed (with results)", False),
    "poll.deleted": ("A poll was deleted", False),
    "qa.asked": ("A question was asked in Q&A", False),
    "qa.upvoted": ("A question was upvoted", True),
    "qa.answered": ("A question was answered", False),
    "whiteboard.cleared": ("The whiteboard was cleared", False),
    "whiteboard.drawn": ("Someone drew on the whiteboard", True),
    "breakout.configured": ("Breakout rooms were set up", False),
    "breakout.opened": ("Breakout rooms opened", False),
    "breakout.closed": ("Breakout rooms closed", False),
    "breakout.moved": ("Someone was moved to a breakout room", False),
    "breakout.announced": ("A message was broadcast to all breakout rooms", False),
    "recording.started": ("Recording started", False),
    "recording.stopped": ("Recording stopped", False),
    "recording.ready": ("A recording finished processing (with download URL)", False),
    "captions.started": ("Live captions were turned on", False),
    "captions.stopped": ("Live captions were turned off", False),
    "caption.line": ("A final caption line (live transcript)", True),
    # scheduling
    "booking.created": ("A meeting was booked", False),
    "booking.rescheduled": ("A booking moved", False),
    "booking.cancelled": ("A booking was cancelled", False),
    "booking.reminder": ("A reminder went out", False),
    "booking.pending_payment": ("A paid booking is waiting for payment (slot held)", False),
    "booking.paid": ("A booking was paid and confirmed", False),
    "booking.payment_late": ("Payment arrived after the hold expired", False),
    "booking.payment_expired": ("An unpaid hold expired and the slot was released", False),
    "booking.attendee_joined": ("The person who booked joined the meeting", False),
    "booking.host_joined": ("The host joined a booked meeting", False),
    "booking.no_show": ("Nobody (or only one side) turned up to a booked meeting", False),
    "booking.completed": ("A booked meeting took place (both sides joined)", False),
    "booking.attendee_confirmed": ("The attendee replied YES / CONFIRM by SMS or WhatsApp", False),
    "availability.updated": ("A host's bookable hours changed", False),
    # payments
    "payment.checkout_created": ("A checkout / payment link was created", False),
    "payment.refunded": ("A payment was refunded (full or partial)", False),
    "payment.failed": ("A payment attempt failed or the checkout expired", False),
    "payment.disputed": ("A customer opened a dispute / chargeback", False),
    # integrations: outgoing
    "email.sent": ("An email was sent", True),
    "email.failed": ("An email could not be sent", False),
    "sms.sent": ("An SMS / WhatsApp message was sent", True),
    "sms.failed": ("An SMS / WhatsApp message failed", False),
    "sms.delivered": ("The carrier / WhatsApp confirmed delivery (or read)", True),
    "sms.received": ("Someone texted back (SMS)", False),
    "whatsapp.received": ("Someone replied on WhatsApp", False),
    "push.sent": ("A push notification was delivered", True),
    "push.failed": ("A push notification failed", False),
    "push.subscribed": ("A device turned on push notifications", False),
    "push.unsubscribed": ("A device turned off / lost push notifications", False),
    "chat_app.sent": ("A Slack / Discord / Teams... notification was posted", True),
    "chat_app.failed": ("Posting to a chat app failed", False),
    "chat_app.command": ("Someone used a /meet command in Slack, Discord or Telegram", False),
    "crm.synced": ("A booking was written to the CRM", False),
    "crm.failed": ("Writing to the CRM failed", False),
    "crm.contact_updated": ("The CRM reported a contact change (inbound webhook)", False),
    "crm.contact_deleted": ("The CRM reported a contact deletion (inbound webhook)", False),
    "crm.event": ("Any other inbound CRM webhook (raw)", False),
    "calendar.connected": ("A host connected a calendar", False),
    "calendar.disconnected": ("A host disconnected a calendar", False),
    "calendar.synced": ("A booking was written to a connected calendar", True),
    "calendar.failed": ("Writing to a calendar failed", False),
    "calendar.changed": ("A connected calendar changed (busy times refreshed)", False),
    "conference.created": ("A Zoom / Teams / Meet / Webex / Jitsi meeting was created for a booking", False),
    "conference.updated": ("The external meeting moved", False),
    "conference.deleted": ("The external meeting was deleted", False),
    "conference.failed": ("Creating / updating the external meeting failed", False),
    "conference.started": ("The external meeting started (Zoom / Webex / Teams webhook)", False),
    "conference.ended": ("The external meeting ended", False),
    "conference.participant_joined": ("Someone joined the external meeting", False),
    "conference.participant_left": ("Someone left the external meeting", False),
    "conference.recording_ready": ("The external meeting's cloud recording is ready", False),
    "sso.login": ("Someone signed in with SSO", False),
    "sso.failed": ("An SSO sign-in failed", False),
    "lti.launch": ("Someone opened a class from an LMS (Moodle, Canvas...): who, course, role", False),
    "lti.deep_link": ("An instructor added a meeting or booking page to a course", False),
    "lti.registered": ("An LMS connected itself via dynamic registration", False),
    "lti.roster_synced": ("Course roster copied to a room's join list", False),
    "lti.grade_sent": ("A score (e.g. attendance) was sent to the LMS gradebook", False),
    "lti.failed": ("An LMS launch, roster sync or grade passback failed", False),
    # admin
    "role.saved": ("A role was created or edited", False),
    "role.deleted": ("A role was deleted", False),
    "branding.updated": ("Branding changed", False),
    "api_key.created": ("An API key was created", False),
    "api_key.revoked": ("An API key was revoked", False),
    "inbound.received": ("An inbound action / webhook was accepted", False),
    "inbound.rejected": ("An inbound webhook failed its signature check", False),
    "integration.error": ("Any integration failed (catch-all)", False),
    "webhook.test": ("A test event you triggered", False),
}

EVENTS: Tuple[str, ...] = tuple(CATALOG)


def is_custom(name: str) -> bool:
    return name.startswith("custom.")


def known(name: str) -> bool:
    """Exact event, pattern that matches at least one event, or custom.* event."""
    if name in CATALOG or is_custom(name) or name == "*":
        return True
    return any(fnmatch.fnmatchcase(e, name) for e in CATALOG)


def wants(subscribed: Optional[Iterable[str]], event: str) -> bool:
    """Does a subscription list want this event?

    ``None`` / ``"*"`` = everything except noisy events. Any other pattern that matches
    (``poll.*``, ``*.failed``) or the exact name includes noisy events too.
    """
    noisy = CATALOG.get(event, ("", False))[1]
    if subscribed is None:
        return not noisy
    for pat in subscribed:
        if pat == "*":
            if not noisy:
                return True
        elif pat == event or fnmatch.fnmatchcase(event, pat):
            return True
    return False


def catalog() -> List[Dict[str, object]]:
    return [{"event": k, "description": d, "noisy": n, "group": k.split(".")[0]} for k, (d, n) in CATALOG.items()]
