"""Use Zoom, Microsoft Teams, Google Meet, Webex or Jitsi for booked meetings.

nodemeet still does the scheduling, payments, reminders, CRM and calendar sync; the meeting
link in emails, invites and the booking page becomes the external one.

    meet.add_conferencing("zoom", ACCOUNT_ID, CLIENT_ID, CLIENT_SECRET)   # Server-to-Server OAuth app
    meet.add_conferencing("teams", TENANT_ID, CLIENT_ID, CLIENT_SECRET, organizer="host@acme.com")
    meet.add_conferencing("google_meet", CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN)
    meet.add_conferencing("webex", ACCESS_TOKEN)
    meet.add_conferencing("jitsi")                                          # free, no account

``mode="also"`` keeps the nodemeet room as the main link and adds the external one to the
booking (``booking.metadata["conference"]``) instead.

Note: this hands meetings *to* those apps. Bridging audio/video between a nodemeet room and
a Zoom/Teams call needs their proprietary SDKs or paid gateways, so nodemeet doesn't do it.
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from datetime import timezone
from typing import TYPE_CHECKING, Any, Dict, Optional

from .http_client import HTTPClient

if TYPE_CHECKING:
    from ..models import Booking
    from ..server import NodeMeet

log = logging.getLogger("nodemeet.conferencing")


def _iso(dt: Any) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Token:
    def __init__(self) -> None:
        self.value: Optional[str] = None
        self.expires = 0.0

    def valid(self) -> bool:
        return bool(self.value) and time.time() < self.expires - 60

    def set(self, data: Dict[str, Any]) -> str:
        self.value, self.expires = data["access_token"], time.time() + int(data.get("expires_in", 3600))
        return self.value


class ConferencingProvider:
    name = "conference"

    def __init__(self, http: Optional[HTTPClient] = None) -> None:
        self.http = http or HTTPClient()

    async def create(self, booking: "Booking", host_email: str) -> Dict[str, Any]:
        """-> {"id", "join_url", "host_url"?, "password"?}"""
        raise NotImplementedError

    async def update(self, info: Dict[str, Any], booking: "Booking", host_email: str) -> Dict[str, Any]:
        return info

    async def delete(self, info: Dict[str, Any], booking: "Booking") -> None:
        return None


class ZoomMeetings(ConferencingProvider):
    """Zoom Server-to-Server OAuth app (scopes: meeting:write:meeting:admin or meeting:write:admin)."""

    name = "zoom"

    def __init__(self, account_id: str, client_id: str, client_secret: str, *, user: Optional[str] = None,
                 settings: Optional[Dict[str, Any]] = None, http: Optional[HTTPClient] = None) -> None:
        super().__init__(http)
        self.account_id, self.client_id, self.client_secret = account_id, client_id, client_secret
        self.user, self.settings, self.tok = user, settings or {}, _Token()

    async def _auth(self) -> str:
        if self.tok.valid():
            return self.tok.value or ""
        res = await self.http.request("POST", "https://zoom.us/oauth/token", basic=(self.client_id, self.client_secret),
                                      form={"grant_type": "account_credentials", "account_id": self.account_id},
                                      expect_ok=True, what="zoom token")
        return self.tok.set(res.json())

    def _body(self, b: "Booking") -> Dict[str, Any]:
        return {"topic": b.title[:200], "type": 2, "start_time": _iso(b.start), "timezone": "UTC",
                "duration": max(1, int((b.end - b.start).total_seconds() // 60)), "agenda": (b.notes or "")[:2000],
                "settings": {"join_before_host": False, "waiting_room": True, "meeting_invitees": [{"email": b.attendee_email}],
                             **self.settings}}

    async def create(self, booking: "Booking", host_email: str) -> Dict[str, Any]:
        user = self.user or host_email or "me"
        res = await self.http.request("POST", f"https://api.zoom.us/v2/users/{user}/meetings", bearer=await self._auth(),
                                      json_body=self._body(booking), expect_ok=True, what="zoom meeting")
        d = res.json()
        return {"id": str(d["id"]), "join_url": d["join_url"], "host_url": d.get("start_url"), "password": d.get("password")}

    async def update(self, info: Dict[str, Any], booking: "Booking", host_email: str) -> Dict[str, Any]:
        await self.http.request("PATCH", f"https://api.zoom.us/v2/meetings/{info['id']}", bearer=await self._auth(),
                                json_body=self._body(booking), expect_ok=True, what="zoom update")
        return info

    async def delete(self, info: Dict[str, Any], booking: "Booking") -> None:
        res = await self.http.request("DELETE", f"https://api.zoom.us/v2/meetings/{info['id']}", bearer=await self._auth())
        if not res.ok and res.status != 404:
            raise RuntimeError(f"zoom delete: HTTP {res.status}")


class TeamsMeetings(ConferencingProvider):
    """Microsoft Teams online meetings via Graph (app permission OnlineMeetings.ReadWrite.All +
    an application access policy for the organizer)."""

    name = "teams"

    def __init__(self, tenant_id: str, client_id: str, client_secret: str, *, organizer: Optional[str] = None,
                 http: Optional[HTTPClient] = None) -> None:
        super().__init__(http)
        self.tenant, self.client_id, self.client_secret = tenant_id, client_id, client_secret
        self.organizer, self.tok = organizer, _Token()

    async def _auth(self) -> str:
        if self.tok.valid():
            return self.tok.value or ""
        res = await self.http.request("POST", f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token", form={
            "grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret,
            "scope": "https://graph.microsoft.com/.default"}, expect_ok=True, what="microsoft token")
        return self.tok.set(res.json())

    def _url(self, host_email: str, mid: str = "") -> str:
        user = self.organizer or host_email
        if not user:
            raise ValueError("Teams needs organizer= (a user id or email) or a host_email on the availability")
        return f"https://graph.microsoft.com/v1.0/users/{user}/onlineMeetings" + (f"/{mid}" if mid else "")

    async def create(self, booking: "Booking", host_email: str) -> Dict[str, Any]:
        res = await self.http.request("POST", self._url(host_email), bearer=await self._auth(), json_body={
            "subject": booking.title, "startDateTime": _iso(booking.start), "endDateTime": _iso(booking.end),
            "lobbyBypassSettings": {"scope": "organization"}}, expect_ok=True, what="teams meeting")
        d = res.json()
        return {"id": d["id"], "join_url": d["joinWebUrl"], "organizer": self.organizer or host_email}

    async def update(self, info: Dict[str, Any], booking: "Booking", host_email: str) -> Dict[str, Any]:
        await self.http.request("PATCH", self._url(info.get("organizer") or host_email, info["id"]), bearer=await self._auth(),
                                json_body={"startDateTime": _iso(booking.start), "endDateTime": _iso(booking.end),
                                           "subject": booking.title}, expect_ok=True, what="teams update")
        return info

    async def delete(self, info: Dict[str, Any], booking: "Booking") -> None:
        await self.http.request("DELETE", self._url(info.get("organizer") or "", info["id"]), bearer=await self._auth())


class GoogleMeet(ConferencingProvider):
    """Google Meet REST API (spaces). OAuth client + a refresh token with the
    ``https://www.googleapis.com/auth/meetings.space.created`` scope."""

    name = "google_meet"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, *, http: Optional[HTTPClient] = None) -> None:
        super().__init__(http)
        self.client_id, self.client_secret, self.refresh_token, self.tok = client_id, client_secret, refresh_token, _Token()

    async def _auth(self) -> str:
        if self.tok.valid():
            return self.tok.value or ""
        res = await self.http.request("POST", "https://oauth2.googleapis.com/token", form={
            "grant_type": "refresh_token", "refresh_token": self.refresh_token, "client_id": self.client_id,
            "client_secret": self.client_secret}, expect_ok=True, what="google token")
        return self.tok.set(res.json())

    async def create(self, booking: "Booking", host_email: str) -> Dict[str, Any]:
        res = await self.http.request("POST", "https://meet.googleapis.com/v2/spaces", bearer=await self._auth(),
                                      json_body={"config": {"accessType": "TRUSTED"}}, expect_ok=True, what="google meet")
        d = res.json()
        return {"id": d["name"], "join_url": d["meetingUri"], "code": d.get("meetingCode")}

    async def delete(self, info: Dict[str, Any], booking: "Booking") -> None:
        # Spaces can't be deleted; end any running conference so the link stops being useful.
        await self.http.request("POST", f"https://meet.googleapis.com/v2/{info['id']}:endActiveConference",
                                bearer=await self._auth(), json_body={})


class WebexMeetings(ConferencingProvider):
    name = "webex"

    def __init__(self, access_token: Optional[str] = None, *, client_id: Optional[str] = None,
                 client_secret: Optional[str] = None, refresh_token: Optional[str] = None,
                 http: Optional[HTTPClient] = None) -> None:
        super().__init__(http)
        self.static, self.client_id, self.client_secret, self.refresh_token = access_token, client_id, client_secret, refresh_token
        self.tok = _Token()
        if not access_token and not refresh_token:
            raise ValueError("Webex needs an access token or client_id/client_secret/refresh_token")

    async def _auth(self) -> str:
        if self.static:
            return self.static
        if self.tok.valid():
            return self.tok.value or ""
        res = await self.http.request("POST", "https://webexapis.com/v1/access_token", form={
            "grant_type": "refresh_token", "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh_token}, expect_ok=True, what="webex token")
        return self.tok.set(res.json())

    def _body(self, b: "Booking", host_email: str) -> Dict[str, Any]:
        body = {"title": b.title[:128], "start": _iso(b.start), "end": _iso(b.end), "timezone": "UTC",
                "agenda": (b.notes or "")[:1300], "invitees": [{"email": b.attendee_email, "displayName": b.attendee_name}],
                "sendEmail": False}
        if host_email:
            body["hostEmail"] = host_email
        return body

    async def create(self, booking: "Booking", host_email: str) -> Dict[str, Any]:
        res = await self.http.request("POST", "https://webexapis.com/v1/meetings", bearer=await self._auth(),
                                      json_body=self._body(booking, host_email), expect_ok=True, what="webex meeting")
        d = res.json()
        return {"id": d["id"], "join_url": d["webLink"], "password": d.get("password")}

    async def update(self, info: Dict[str, Any], booking: "Booking", host_email: str) -> Dict[str, Any]:
        body = self._body(booking, host_email)
        body.pop("invitees", None)
        if info.get("password"):
            body["password"] = info["password"]  # Webex PUT replaces the whole meeting
        await self.http.request("PUT", f"https://webexapis.com/v1/meetings/{info['id']}", bearer=await self._auth(),
                                json_body=body, expect_ok=True, what="webex update")
        return info

    async def delete(self, info: Dict[str, Any], booking: "Booking") -> None:
        await self.http.request("DELETE", f"https://webexapis.com/v1/meetings/{info['id']}", bearer=await self._auth())


class JitsiMeetings(ConferencingProvider):
    """Jitsi Meet (meet.jit.si or your own server). No API: an unguessable room name."""

    name = "jitsi"

    def __init__(self, domain: str = "meet.jit.si", *, http: Optional[HTTPClient] = None) -> None:
        super().__init__(http)
        self.domain = domain.replace("https://", "").rstrip("/")

    async def create(self, booking: "Booking", host_email: str) -> Dict[str, Any]:
        slug = re.sub(r"[^A-Za-z0-9]", "", booking.title.title())[:30] or "Meeting"
        room = f"{slug}-{secrets.token_hex(8)}"  # 64 random bits: unguessable
        return {"id": room, "join_url": f"https://{self.domain}/{room}"}


class ConferencingService:
    def __init__(self, meet: "NodeMeet", provider: ConferencingProvider, *, mode: str = "replace") -> None:
        if mode not in ("replace", "also"):
            raise ValueError("mode must be 'replace' or 'also'")
        self.meet, self.provider, self.mode = meet, provider, mode
        meet.conferencing = self
        meet.bookings.listeners.insert(0, self.on_booking)  # first: CRM/calendar see the final link

    async def on_booking(self, event: str, booking: "Booking", extra: Dict[str, Any]) -> None:
        if event not in ("booking.created", "booking.rescheduled", "booking.cancelled"):
            return
        try:
            done = await self._sync(event, booking)
        except Exception as exc:
            info = {"provider": self.provider.name, "booking_id": booking.id, "action": event, "error": str(exc)[:300]}
            await self.meet.emit_webhook("conference.failed", info, tenant_id=booking.tenant_id)
            await self.meet.emit_webhook("integration.error", {"source": "conference", **info}, tenant_id=booking.tenant_id)
            raise
        if done:
            name, conf = done
            if conf.get("id"):
                await self.meet.storage.put_record("conf_ref", f"{self.provider.name}|{conf['id']}", {"booking_id": booking.id})
            await self.meet.emit_webhook(name, {"booking_id": booking.id, "host_id": booking.host_id,
                                                "conference": {k: v for k, v in conf.items() if k != "host_url"}},
                                         tenant_id=booking.tenant_id)

    async def _sync(self, event: str, booking: "Booking") -> Any:
        info = dict(booking.metadata.get("conference") or {})
        av = await self.meet.storage.get_availability(booking.host_id)
        host_email = (av.host_email if av else "") or ""
        if event == "booking.created" or (event == "booking.rescheduled" and not info.get("id")):
            info = {"provider": self.provider.name, "mode": self.mode, **await self.provider.create(booking, host_email)}
            name = "conference.created"
        elif event == "booking.rescheduled":
            info.update(await self.provider.update(info, booking, host_email))
            name = "conference.updated"
        elif event == "booking.cancelled" and info.get("id"):
            await self.provider.delete(info, booking)
            info["deleted"] = True
            name = "conference.deleted"
        else:
            return None
        booking.metadata["conference"] = info
        await self.meet.storage.save_booking(booking)
        return name, info


def make_conferencing(kind: str, *args: Any, **kw: Any) -> ConferencingProvider:
    kinds = {"zoom": ZoomMeetings, "teams": TeamsMeetings, "msteams": TeamsMeetings, "google_meet": GoogleMeet,
             "meet": GoogleMeet, "googlemeet": GoogleMeet, "webex": WebexMeetings, "jitsi": JitsiMeetings}
    key = kind.lower().replace("-", "_").replace(" ", "_")
    if key not in kinds:
        from ..easy import did_you_mean
        raise ValueError(f"unknown conferencing app {kind!r}." + did_you_mean(key, kinds))
    return kinds[key](*args, **kw)
