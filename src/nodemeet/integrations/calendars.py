"""Two-way calendar sync: Google Calendar, Microsoft Outlook / 365, and CalDAV
(Apple iCloud, Fastmail, Nextcloud, Zoho, Yahoo, Radicale...).

* **Read**: busy times from every connected calendar block booking slots.
* **Write**: bookings are added to the host's calendar, moved on reschedule and
  removed on cancel.

Setup (Google example)::

    from nodemeet.integrations.calendars import CalendarService, GoogleCalendar, OutlookCalendar, CalDAVCalendar
    cal = CalendarService(meet, [GoogleCalendar(CLIENT_ID, CLIENT_SECRET),
                                 OutlookCalendar(MS_CLIENT_ID, MS_SECRET), CalDAVCalendar()])
    # host connects:  GET /api/calendars/google/connect?host_id=dr-lee  -> {"url": "https://accounts.google.com/..."}

Register ``{base_url}/api/calendars/<provider>/callback`` as the OAuth redirect URI.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from html import escape
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .._http import HTTPError as APIError, Request, Response, Router, json_response, text_response
from ..models import Booking, to_utc
from ..tokens import sign_blob, verify_blob
from .http_client import HTTPClient

if TYPE_CHECKING:
    from ..server import NodeMeet

log = logging.getLogger("nodemeet.calendars")
Interval = Tuple[datetime, datetime]


def _iso(dt: datetime) -> str:
    return to_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


_ISO = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:?\d\d)?")


def _parse(value: str) -> datetime:
    """ISO 8601 incl. Graph's 7-digit fractions; naive values are treated as UTC."""
    m = _ISO.match(value.strip())
    if not m:
        raise ValueError(f"bad datetime {value!r}")
    dt = datetime.fromisoformat(m.group(1))
    if m.group(2):
        dt = dt.replace(microsecond=int(m.group(2)[:6].ljust(6, "0")))
    tz = m.group(3)
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        hh, mm = int(tz[1:3]), int(tz[-2:])
        dt = dt.replace(tzinfo=timezone(sign * timedelta(hours=hh, minutes=mm)))
    else:
        dt = dt.replace(tzinfo=timezone.utc)
    return to_utc(dt)


class CalendarProvider:
    name = ""
    oauth = True

    def __init__(self, http: Optional[HTTPClient] = None) -> None:
        self.http = http or HTTPClient()

    def authorize_url(self, state: str, redirect_uri: str) -> str: raise NotImplementedError
    async def exchange(self, code: str, redirect_uri: str) -> Dict[str, Any]: raise NotImplementedError
    async def refresh(self, conn: Dict[str, Any]) -> Dict[str, Any]: return conn
    async def busy(self, conn: Dict[str, Any], start: datetime, end: datetime) -> List[Interval]: raise NotImplementedError
    async def create_event(self, conn: Dict[str, Any], b: Booking, details: Dict[str, str]) -> str: raise NotImplementedError
    async def update_event(self, conn: Dict[str, Any], event_id: str, b: Booking, details: Dict[str, str]) -> None: raise NotImplementedError
    async def delete_event(self, conn: Dict[str, Any], event_id: str) -> None: raise NotImplementedError


class _OAuthProvider(CalendarProvider):
    auth_endpoint = token_endpoint = ""
    scopes: Sequence[str] = ()

    def __init__(self, client_id: str, client_secret: str, *, http: Optional[HTTPClient] = None) -> None:
        super().__init__(http)
        self.client_id, self.client_secret = client_id, client_secret

    def _auth_params(self) -> Dict[str, str]:
        return {}

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        q = {"client_id": self.client_id, "redirect_uri": redirect_uri, "response_type": "code",
             "scope": " ".join(self.scopes), "state": state, **self._auth_params()}
        return f"{self.auth_endpoint}?{urlencode(q)}"

    async def _token(self, form: Dict[str, str]) -> Dict[str, Any]:
        res = await self.http.request("POST", self.token_endpoint, form={
            "client_id": self.client_id, "client_secret": self.client_secret, **form},
            expect_ok=True, what=f"{self.name} token")
        data = res.json() or {}
        data["expires_at"] = time.time() + int(data.get("expires_in", 3600)) - 60
        return data

    async def exchange(self, code: str, redirect_uri: str) -> Dict[str, Any]:
        tok = await self._token({"code": code, "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
        conn = {"provider": self.name, "access_token": tok["access_token"],
                "refresh_token": tok.get("refresh_token"), "expires_at": tok["expires_at"]}
        conn["account"] = await self.account(conn)
        return conn

    async def account(self, conn: Dict[str, Any]) -> str:
        return ""

    async def refresh(self, conn: Dict[str, Any]) -> Dict[str, Any]:
        if conn.get("expires_at", 0) > time.time() or not conn.get("refresh_token"):
            return conn
        tok = await self._token({"refresh_token": conn["refresh_token"], "grant_type": "refresh_token"})
        conn = dict(conn, access_token=tok["access_token"], expires_at=tok["expires_at"])
        if tok.get("refresh_token"):
            conn["refresh_token"] = tok["refresh_token"]
        conn["_changed"] = True
        return conn


class GoogleCalendar(_OAuthProvider):
    name = "google"
    auth_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint = "https://oauth2.googleapis.com/token"
    api = "https://www.googleapis.com/calendar/v3"
    scopes = ("openid", "email", "https://www.googleapis.com/auth/calendar.events",
              "https://www.googleapis.com/auth/calendar.freebusy")

    def __init__(self, client_id: str, client_secret: str, *, calendar_id: str = "primary",
                 http: Optional[HTTPClient] = None) -> None:
        super().__init__(client_id, client_secret, http=http)
        self.calendar_id = calendar_id

    def _auth_params(self) -> Dict[str, str]:
        return {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"}

    async def account(self, conn: Dict[str, Any]) -> str:
        res = await self.http.request("GET", "https://openidconnect.googleapis.com/v1/userinfo",
                                      bearer=conn["access_token"])
        return (res.json() or {}).get("email", "") if res.ok else ""

    async def busy(self, conn: Dict[str, Any], start: datetime, end: datetime) -> List[Interval]:
        cal = conn.get("calendar_id") or self.calendar_id
        res = await self.http.request("POST", f"{self.api}/freeBusy", bearer=conn["access_token"],
                                      json_body={"timeMin": _iso(start), "timeMax": _iso(end), "items": [{"id": cal}]},
                                      expect_ok=True, what="google freeBusy")
        busy = ((res.json() or {}).get("calendars", {}).get(cal, {}) or {}).get("busy", [])
        return [(_parse(b["start"]), _parse(b["end"])) for b in busy]

    def _event(self, b: Booking, d: Dict[str, str]) -> Dict[str, Any]:
        return {"summary": d["title"], "description": d["description"], "location": d["join_url"],
                "start": {"dateTime": _iso(b.start)}, "end": {"dateTime": _iso(b.end)},
                "attendees": [{"email": b.attendee_email, "displayName": b.attendee_name}],
                "source": {"title": d["title"], "url": d["join_url"]} if d["join_url"].startswith("http") else None,
                "extendedProperties": {"private": {"nodemeet_booking": b.id}}}

    async def create_event(self, conn: Dict[str, Any], b: Booking, d: Dict[str, str]) -> str:
        cal = conn.get("calendar_id") or self.calendar_id
        body = {k: v for k, v in self._event(b, d).items() if v is not None}
        res = await self.http.request("POST", f"{self.api}/calendars/{cal}/events", bearer=conn["access_token"],
                                      params={"sendUpdates": "none"}, json_body=body, expect_ok=True,
                                      what="google create event")
        return str((res.json() or {}).get("id"))

    async def update_event(self, conn: Dict[str, Any], event_id: str, b: Booking, d: Dict[str, str]) -> None:
        cal = conn.get("calendar_id") or self.calendar_id
        body = {k: v for k, v in self._event(b, d).items() if v is not None}
        await self.http.request("PATCH", f"{self.api}/calendars/{cal}/events/{event_id}", bearer=conn["access_token"],
                                params={"sendUpdates": "none"}, json_body=body, expect_ok=True, what="google update")

    async def delete_event(self, conn: Dict[str, Any], event_id: str) -> None:
        cal = conn.get("calendar_id") or self.calendar_id
        res = await self.http.request("DELETE", f"{self.api}/calendars/{cal}/events/{event_id}",
                                      bearer=conn["access_token"], params={"sendUpdates": "none"})
        if not res.ok and res.status not in (404, 410):
            raise RuntimeError(f"google delete failed: {res.status}")


class OutlookCalendar(_OAuthProvider):
    """Microsoft 365 / Outlook.com via Microsoft Graph."""

    name = "outlook"
    api = "https://graph.microsoft.com/v1.0"
    scopes = ("offline_access", "User.Read", "Calendars.ReadWrite")

    def __init__(self, client_id: str, client_secret: str, *, tenant: str = "common",
                 http: Optional[HTTPClient] = None) -> None:
        super().__init__(client_id, client_secret, http=http)
        base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
        self.auth_endpoint, self.token_endpoint = base + "/authorize", base + "/token"

    def _auth_params(self) -> Dict[str, str]:
        return {"response_mode": "query"}

    async def _token(self, form: Dict[str, str]) -> Dict[str, Any]:
        return await super()._token({**form, "scope": " ".join(self.scopes)})

    async def account(self, conn: Dict[str, Any]) -> str:
        res = await self.http.request("GET", f"{self.api}/me", bearer=conn["access_token"])
        me = res.json() or {}
        return me.get("mail") or me.get("userPrincipalName") or ""

    async def busy(self, conn: Dict[str, Any], start: datetime, end: datetime) -> List[Interval]:
        out: List[Interval] = []
        url: Optional[str] = f"{self.api}/me/calendarView"
        params: Optional[Dict[str, Any]] = {"startDateTime": _iso(start), "endDateTime": _iso(end),
                                            "$select": "start,end,showAs,isCancelled", "$top": 200}
        while url:
            res = await self.http.request("GET", url, params=params, bearer=conn["access_token"],
                                          headers={"Prefer": 'outlook.timezone="UTC"'}, expect_ok=True,
                                          what="graph calendarView")
            data = res.json() or {}
            for ev in data.get("value", []):
                if ev.get("isCancelled") or ev.get("showAs") == "free":
                    continue
                out.append((_parse(ev["start"]["dateTime"] + "+00:00"), _parse(ev["end"]["dateTime"] + "+00:00")))
            url, params = data.get("@odata.nextLink"), None
        return out

    def _event(self, b: Booking, d: Dict[str, str]) -> Dict[str, Any]:
        return {"subject": d["title"], "body": {"contentType": "HTML", "content": d["html"]},
                "start": {"dateTime": _iso(b.start)[:-1], "timeZone": "UTC"},
                "end": {"dateTime": _iso(b.end)[:-1], "timeZone": "UTC"},
                "location": {"displayName": d["join_url"]},
                "attendees": [{"emailAddress": {"address": b.attendee_email, "name": b.attendee_name},
                               "type": "required"}],
                "isOnlineMeeting": False, "transactionId": b.id}

    async def create_event(self, conn: Dict[str, Any], b: Booking, d: Dict[str, str]) -> str:
        res = await self.http.request("POST", f"{self.api}/me/events", bearer=conn["access_token"],
                                      json_body=self._event(b, d), expect_ok=True, what="graph create event")
        return str((res.json() or {}).get("id"))

    async def update_event(self, conn: Dict[str, Any], event_id: str, b: Booking, d: Dict[str, str]) -> None:
        body = self._event(b, d)
        body.pop("transactionId", None)
        await self.http.request("PATCH", f"{self.api}/me/events/{event_id}", bearer=conn["access_token"],
                                json_body=body, expect_ok=True, what="graph update event")

    async def delete_event(self, conn: Dict[str, Any], event_id: str) -> None:
        res = await self.http.request("DELETE", f"{self.api}/me/events/{event_id}", bearer=conn["access_token"])
        if not res.ok and res.status != 404:
            raise RuntimeError(f"graph delete failed: {res.status}")


# -- CalDAV (Apple iCloud, Fastmail, Nextcloud, Zoho, Yahoo, Radicale, Baïkal...) -----------
def _ics_times(ics: str) -> List[Interval]:
    """Very small iCalendar reader: DTSTART/DTEND (UTC, TZID or floating) of each VEVENT."""
    text = re.sub(r"\r?\n[ \t]", "", ics)  # unfold
    out: List[Interval] = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        if re.search(r"^TRANSP:TRANSPARENT", block, re.M) or re.search(r"^STATUS:CANCELLED", block, re.M):
            continue
        vals = {}
        for key in ("DTSTART", "DTEND"):
            m = re.search(rf"^{key}((?:;[^:\r\n]*)?):([0-9TZ]+)", block, re.M)
            if m:
                vals[key] = (m.group(1), m.group(2))
        if "DTSTART" not in vals:
            continue

        def conv(params: str, raw: str) -> datetime:
            if len(raw) == 8:  # all-day
                return datetime.strptime(raw, "%Y%m%d").replace(tzinfo=timezone.utc)
            dt = datetime.strptime(raw.rstrip("Z"), "%Y%m%dT%H%M%S")
            tzm = re.search(r"TZID=([^;:]+)", params)
            if raw.endswith("Z") or not tzm:
                return dt.replace(tzinfo=timezone.utc)
            try:
                return to_utc(dt.replace(tzinfo=ZoneInfo(tzm.group(1).strip('"'))))
            except Exception:  # noqa: BLE001 - unknown Windows zone names etc.
                return dt.replace(tzinfo=timezone.utc)
        start = conv(*vals["DTSTART"])
        end = conv(*vals["DTEND"]) if "DTEND" in vals else start + (
            timedelta(days=1) if len(vals["DTSTART"][1]) == 8 else timedelta(hours=1))
        out.append((start, end))
    return out


class CalDAVCalendar(CalendarProvider):
    """Connect with a calendar collection URL + username + app password (no OAuth)."""

    name = "caldav"
    oauth = False

    def _auth(self, conn: Dict[str, Any]) -> tuple:
        return (conn["username"], conn["password"])

    async def busy(self, conn: Dict[str, Any], start: datetime, end: datetime) -> List[Interval]:
        fmt = "%Y%m%dT%H%M%SZ"
        body = ('<?xml version="1.0" encoding="utf-8"?><C:calendar-query xmlns:D="DAV:" '
                'xmlns:C="urn:ietf:params:xml:ns:caldav"><D:prop><C:calendar-data/></D:prop>'
                '<C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT">'
                f'<C:time-range start="{to_utc(start).strftime(fmt)}" end="{to_utc(end).strftime(fmt)}"/>'
                '</C:comp-filter></C:comp-filter></C:filter></C:calendar-query>')
        res = await self.http.request("REPORT", conn["url"], data=body.encode(), basic=self._auth(conn),
                                      headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8",
                                               "Accept": "application/xml"})
        if res.status not in (200, 207):
            raise RuntimeError(f"caldav REPORT failed: {res.status}")
        out = []
        for chunk in re.findall(r"<(?:\w+:)?calendar-data[^>]*>(.*?)</(?:\w+:)?calendar-data>", res.text, re.S):
            import html as _html
            out += _ics_times(_html.unescape(chunk))
        return [(s, e) for s, e in out if e > to_utc(start) and s < to_utc(end)]

    def _href(self, conn: Dict[str, Any], uid: str) -> str:
        return conn["url"].rstrip("/") + f"/{uid}.ics"

    def _ics(self, b: Booking, d: Dict[str, str], uid: str) -> str:
        from ..scheduling.ics import build_ics
        ics = build_ics(b, url=d["join_url"] or None)
        ics = re.sub(r"METHOD:[A-Z]+\r?\n", "", ics)
        return re.sub(r"UID:[^\r\n]+", f"UID:{uid}", ics, count=1)

    async def create_event(self, conn: Dict[str, Any], b: Booking, d: Dict[str, str]) -> str:
        uid = f"nodemeet-{b.id}-{uuid.uuid4().hex[:6]}"
        res = await self.http.request("PUT", self._href(conn, uid), data=self._ics(b, d, uid).encode(),
                                      basic=self._auth(conn), headers={"Content-Type": "text/calendar; charset=utf-8",
                                                                       "If-None-Match": "*"})
        if res.status not in (200, 201, 204):
            raise RuntimeError(f"caldav PUT failed: {res.status}")
        return uid

    async def update_event(self, conn: Dict[str, Any], event_id: str, b: Booking, d: Dict[str, str]) -> None:
        res = await self.http.request("PUT", self._href(conn, event_id), data=self._ics(b, d, event_id).encode(),
                                      basic=self._auth(conn), headers={"Content-Type": "text/calendar; charset=utf-8"})
        if res.status not in (200, 201, 204):
            raise RuntimeError(f"caldav PUT failed: {res.status}")

    async def delete_event(self, conn: Dict[str, Any], event_id: str) -> None:
        res = await self.http.request("DELETE", self._href(conn, event_id), basic=self._auth(conn))
        if res.status not in (200, 204, 404):
            raise RuntimeError(f"caldav DELETE failed: {res.status}")


class CalendarService:
    """Connects hosts' calendars, feeds busy times into slot finding and keeps
    booked meetings in their calendars.

    ``cipher`` (optional) is any object with ``encrypt(str)->str`` / ``decrypt(str)->str``
    (e.g. a ``cryptography.fernet.Fernet`` wrapper) used for OAuth tokens at rest.
    """

    SECRET_FIELDS = ("access_token", "refresh_token", "password")

    def __init__(self, meet: "NodeMeet", providers: Sequence[CalendarProvider], *,
                 write_events: bool = True, cache_seconds: float = 60.0, cipher: Any = None) -> None:
        self.meet = meet
        self.providers = {p.name: p for p in providers}
        self.write_events = write_events
        self.cache_seconds = cache_seconds
        self.cipher = cipher
        self._cache: Dict[Tuple[str, str, str], Tuple[float, List[Interval]]] = {}
        meet.bookings.busy_sources.append(self.busy)
        meet.bookings.listeners.append(self.on_booking)
        meet.use(self)

    # -- connection storage ---------------------------------------------------------------
    def _seal(self, conn: Dict[str, Any]) -> Dict[str, Any]:
        out = {k: v for k, v in conn.items() if not k.startswith("_")}
        if self.cipher is not None:
            for f in self.SECRET_FIELDS:
                if out.get(f):
                    out[f] = "enc:" + self.cipher.encrypt(out[f])
        return out

    def _open(self, conn: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(conn)
        if self.cipher is not None:
            for f in self.SECRET_FIELDS:
                if isinstance(out.get(f), str) and out[f].startswith("enc:"):
                    out[f] = self.cipher.decrypt(out[f][4:])
        return out

    async def save(self, host_id: str, conn: Dict[str, Any]) -> None:
        conn = dict(conn, host_id=host_id, connected_at=conn.get("connected_at") or time.time())
        await self.meet.storage.put_record("calendar", f"{host_id}|{conn['provider']}", self._seal(conn))
        self._cache.clear()

    async def connections(self, host_id: str) -> List[Dict[str, Any]]:
        return [self._open(v) for _, v in await self.meet.storage.list_records("calendar", host_id + "|")]

    async def disconnect(self, host_id: str, provider: str) -> None:
        await self.meet.storage.delete_record("calendar", f"{host_id}|{provider}")
        self._cache.clear()

    async def connect_caldav(self, host_id: str, url: str, username: str, password: str) -> Dict[str, Any]:
        conn = {"provider": "caldav", "url": url, "username": username, "password": password, "account": username}
        await self.providers["caldav"].busy(conn, datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(hours=1))
        await self.save(host_id, conn)
        return conn

    async def _fresh(self, host_id: str, conn: Dict[str, Any]) -> Dict[str, Any]:
        conn = await self.providers[conn["provider"]].refresh(conn)
        if conn.pop("_changed", False):
            await self.save(host_id, conn)
        return conn

    # -- busy times (read) ------------------------------------------------------------------
    async def busy(self, host_id: str, start: datetime, end: datetime) -> List[Interval]:
        key = (host_id, _iso(start), _iso(end))
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        out: List[Interval] = []
        for conn in await self.connections(host_id):
            provider = self.providers.get(conn["provider"])
            if provider is None or conn.get("read") is False:
                continue
            try:
                conn = await self._fresh(host_id, conn)
                out += await provider.busy(conn, start, end)
            except Exception:  # noqa: BLE001
                log.exception("could not read %s calendar of %s", conn["provider"], host_id)
        self._cache[key] = (time.monotonic() + self.cache_seconds, out)
        return out

    # -- events (write) -----------------------------------------------------------------------
    def _details(self, b: Booking) -> Dict[str, str]:
        join = self.meet.booking_join_url(b, "host")
        manage = self.meet.manage_url(b)
        desc = f"Booked with {self.meet.title}.\nJoin: {join}\nAttendee: {b.attendee_name} <{b.attendee_email}>"
        if b.notes:
            desc += f"\nNotes: {b.notes}"
        html = (f"<p>Join: <a href=\"{escape(join)}\">{escape(join)}</a></p>"
                f"<p>Attendee: {escape(b.attendee_name)} &lt;{escape(b.attendee_email)}&gt;</p>"
                + (f"<p>{escape(b.notes)}</p>" if b.notes else ""))
        return {"title": f"{b.title} with {b.attendee_name}", "description": desc, "html": html,
                "join_url": join, "manage_url": manage}

    async def on_booking(self, event: str, booking: Booking, extra: Dict[str, Any]) -> None:
        if not self.write_events or event not in ("booking.created", "booking.rescheduled", "booking.cancelled"):
            return
        self._cache.clear()
        events: Dict[str, str] = dict(booking.metadata.get("calendar_events") or {})
        changed = False
        for conn in await self.connections(booking.host_id):
            provider = self.providers.get(conn["provider"])
            if provider is None or conn.get("write") is False:
                continue
            try:
                conn = await self._fresh(booking.host_id, conn)
                eid = events.get(conn["provider"])
                if event == "booking.cancelled":
                    if eid:
                        await provider.delete_event(conn, eid)
                        events.pop(conn["provider"], None)
                        changed = True
                elif eid:
                    await provider.update_event(conn, eid, booking, self._details(booking))
                else:
                    events[conn["provider"]] = await provider.create_event(conn, booking, self._details(booking))
                    changed = True
                await self.meet.emit_webhook("calendar.synced", {"booking_id": booking.id, "host_id": booking.host_id,
                                                                 "provider": conn["provider"], "action": event},
                                             tenant_id=booking.tenant_id)
            except Exception as exc:  # noqa: BLE001
                log.exception("calendar %s sync failed for booking %s", conn["provider"], booking.id)
                info = {"booking_id": booking.id, "host_id": booking.host_id, "provider": conn["provider"],
                        "action": event, "error": str(exc)[:300]}
                await self.meet.emit_webhook("calendar.failed", info, tenant_id=booking.tenant_id)
                await self.meet.emit_webhook("integration.error", {"source": "calendar", **info}, tenant_id=booking.tenant_id)
        if changed:
            fresh = await self.meet.storage.get_booking(booking.id) or booking
            fresh.metadata["calendar_events"] = events
            await self.meet.storage.save_booking(fresh)
            booking.metadata["calendar_events"] = events

    # -- change notifications --------------------------------------------------------------------
    async def watch(self, host_id: str, *, hours: int = 70) -> List[Dict[str, Any]]:
        """Ask Google / Microsoft to ping /api/inbound/... when this host's calendar changes
        (busy times refresh instantly, ``calendar.changed`` fires). Re-run before ``hours`` pass."""
        import secrets as _s
        from datetime import timedelta as _td
        inbound = self.meet.inbound.secrets
        out = []
        for conn in await self.connections(host_id):
            conn = await self._fresh(host_id, conn)
            if conn["provider"] == "google":
                cid = f"nm-{_s.token_hex(8)}"
                prov = self.providers["google"]
                res = await prov.http.request("POST", f"{prov.api}/calendars/{getattr(prov, 'calendar_id', 'primary')}/events/watch",
                                              bearer=conn["access_token"], json_body={
                                                  "id": cid, "type": "web_hook", "address": f"{self.meet.base_url}/api/inbound/google-calendar",
                                                  "token": inbound["google-calendar"]["channel_token"],
                                                  "params": {"ttl": str(hours * 3600)}}, expect_ok=True, what="google watch")
                ref = {"host_id": host_id, "provider": "google", "resource_id": (res.json() or {}).get("resourceId")}
                await self.meet.storage.put_record("calendar_watch", cid, ref)
                out.append({"id": cid, **ref})
            elif conn["provider"] == "outlook":
                prov = self.providers["outlook"]
                exp = (datetime.now(timezone.utc) + _td(hours=min(hours, 70))).strftime("%Y-%m-%dT%H:%M:%SZ")
                res = await prov.http.request("POST", f"{prov.api}/subscriptions", bearer=conn["access_token"], json_body={
                    "changeType": "created,updated,deleted", "resource": "me/events",
                    "notificationUrl": f"{self.meet.base_url}/api/inbound/microsoft",
                    "expirationDateTime": exp, "clientState": inbound["microsoft"]["client_state"]},
                    expect_ok=True, what="graph subscription")
                sid = (res.json() or {}).get("id", "")
                await self.meet.storage.put_record("calendar_watch", sid, {"host_id": host_id, "provider": "outlook"})
                out.append({"id": sid, "host_id": host_id, "provider": "outlook"})
        return out

    # -- REST --------------------------------------------------------------------------------------
    def register(self, r: Router) -> None:
        r.add("GET", "/api/calendars/{provider}/connect", self.http_connect)
        r.add("GET", "/api/calendars/{provider}/callback", self.http_callback)
        r.add("GET", "/api/hosts/{host_id}/calendars", self.http_list)
        r.add("POST", "/api/hosts/{host_id}/calendars/caldav", self.http_caldav)
        r.add("DELETE", "/api/hosts/{host_id}/calendars/{provider}", self.http_disconnect)

    def redirect_uri(self, provider: str) -> str:
        return f"{self.meet.base_url}/api/calendars/{provider}/callback"

    async def _host_ok(self, req: Request, host_id: str) -> None:
        p = await self.meet.api.require(req, "bookings:write")
        av = await self.meet.storage.get_availability(host_id)
        if av is not None and not p.owns(av.tenant_id):
            raise APIError(404, "not_found")

    async def http_connect(self, req: Request) -> Response:
        name = req.match_info["provider"]
        provider = self.providers.get(name)
        if provider is None or not provider.oauth:
            raise APIError(404, "unknown_provider", f"no OAuth calendar provider {name!r}")
        host_id = req.query.get("host_id", "")
        await self._host_ok(req, host_id)
        state = sign_blob(self.meet.tokens, {"host": host_id, "provider": name,
                                             "return_to": req.query.get("return_to", "")}, ttl=900, purpose="calendar")
        url = provider.authorize_url(state, self.redirect_uri(name))
        if req.query.get("redirect") == "1":
            return Response(302, b"", "text/plain", {"Location": url})
        return json_response({"url": url})

    async def http_callback(self, req: Request) -> Response:
        name = req.match_info["provider"]
        provider = self.providers.get(name)
        if provider is None:
            raise APIError(404, "unknown_provider")
        if req.query.get("error"):
            return text_response(f"Calendar connection cancelled: {escape(req.query['error'])}", "text/html", 400)
        try:
            state = verify_blob(self.meet.tokens, req.query.get("state", ""), purpose="calendar")
        except Exception:  # noqa: BLE001
            raise APIError(400, "bad_state", "this link expired, start again") from None
        conn = await provider.exchange(req.query.get("code", ""), self.redirect_uri(name))
        await self.save(state["host"], conn)
        await self.meet.emit_webhook("calendar.connected", {"host_id": state["host"], "provider": name,
                                                            "account": conn.get("account")})
        if state.get("return_to", "").startswith(("https://", "http://", "/")):
            return Response(302, b"", "text/plain", {"Location": state["return_to"]})
        return text_response(f"<p>{escape(name.title())} calendar connected"
                             f"{' (' + escape(conn.get('account', '')) + ')' if conn.get('account') else ''}."
                             " You can close this tab.</p>", "text/html")

    async def http_list(self, req: Request) -> Response:
        host_id = req.match_info["host_id"]
        await self._host_ok(req, host_id)
        safe = [{k: v for k, v in c.items() if k not in self.SECRET_FIELDS} for c in await self.connections(host_id)]
        return json_response({"calendars": safe, "providers": sorted(self.providers)})

    async def http_caldav(self, req: Request) -> Response:
        host_id = req.match_info["host_id"]
        await self._host_ok(req, host_id)
        body = req.json()
        try:
            conn = await self.connect_caldav(host_id, body["url"], body["username"], body["password"])
        except KeyError:
            raise APIError(400, "bad_request", "url, username and password are required") from None
        except Exception as exc:  # noqa: BLE001
            raise APIError(400, "caldav_failed", f"could not read that calendar: {exc}") from None
        return json_response({"provider": "caldav", "account": conn["account"]}, 201)

    async def http_disconnect(self, req: Request) -> Response:
        host_id = req.match_info["host_id"]
        await self._host_ok(req, host_id)
        await self.disconnect(host_id, req.match_info["provider"])
        return Response(status=204)
