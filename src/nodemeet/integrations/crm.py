"""Keep your CRM in sync with bookings: HubSpot, Salesforce, Pipedrive, Zoho CRM.

On every booking the attendee becomes (or updates) a contact and the meeting is logged on
them; reschedules move it, cancellations mark it cancelled.

    meet.add_crm("hubspot", "pat-na1-...")                       # private app token
    meet.add_crm("salesforce", client_id, client_secret, domain="acme.my.salesforce.com")
    meet.add_crm("pipedrive", "api-token")
    meet.add_crm("zoho", client_id, client_secret, refresh_token, dc="in")
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from .http_client import HTTPClient

if TYPE_CHECKING:
    from ..models import Booking
    from ..server import NodeMeet

log = logging.getLogger("nodemeet.crm")


def _split_name(name: str) -> Tuple[str, str]:
    parts = (name or "").strip().split()
    if not parts:
        return "", "Unknown"
    return (" ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else ("", parts[0])


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CRMProvider:
    name = "crm"

    def __init__(self, http: Optional[HTTPClient] = None) -> None:
        self.http = http or HTTPClient()

    async def upsert_contact(self, booking: "Booking") -> str: raise NotImplementedError
    async def create_meeting(self, booking: "Booking", contact_id: str, join_url: str) -> str: raise NotImplementedError
    async def update_meeting(self, meeting_id: str, booking: "Booking", join_url: str) -> None: raise NotImplementedError
    async def cancel_meeting(self, meeting_id: str, booking: "Booking") -> None: raise NotImplementedError


class HubSpotCRM(CRMProvider):
    """HubSpot private-app token with crm.objects.contacts.write + meetings scopes."""

    name = "hubspot"
    API = "https://api.hubapi.com"

    def __init__(self, access_token: str, *, http: Optional[HTTPClient] = None) -> None:
        super().__init__(http)
        self.token = access_token

    async def upsert_contact(self, booking: "Booking") -> str:
        first, last = _split_name(booking.attendee_name)
        props = {"email": booking.attendee_email, "firstname": first, "lastname": last}
        if booking.attendee_phone:
            props["phone"] = booking.attendee_phone
        res = await self.http.request("POST", f"{self.API}/crm/v3/objects/contacts/batch/upsert", bearer=self.token,
                                      json_body={"inputs": [{"idProperty": "email", "id": booking.attendee_email,
                                                             "properties": props}]}, expect_ok=True, what="hubspot contact")
        return str(res.json()["results"][0]["id"])

    def _props(self, booking: "Booking", join_url: str, outcome: str = "SCHEDULED") -> Dict[str, Any]:
        return {"hs_timestamp": _iso(booking.start), "hs_meeting_title": booking.title,
                "hs_meeting_body": booking.notes or f"Booked via nodemeet ({booking.id})",
                "hs_meeting_start_time": _iso(booking.start), "hs_meeting_end_time": _iso(booking.end),
                "hs_meeting_outcome": outcome, "hs_meeting_external_url": join_url,
                "hs_meeting_location": join_url}

    async def create_meeting(self, booking: "Booking", contact_id: str, join_url: str) -> str:
        res = await self.http.request("POST", f"{self.API}/crm/v3/objects/meetings", bearer=self.token, json_body={
            "properties": self._props(booking, join_url),
            "associations": [{"to": {"id": contact_id}, "types": [
                {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 200}]}]},  # meeting -> contact
            expect_ok=True, what="hubspot meeting")
        return str(res.json()["id"])

    async def update_meeting(self, meeting_id: str, booking: "Booking", join_url: str) -> None:
        await self.http.request("PATCH", f"{self.API}/crm/v3/objects/meetings/{meeting_id}", bearer=self.token,
                                json_body={"properties": self._props(booking, join_url, "RESCHEDULED")},
                                expect_ok=True, what="hubspot meeting")

    async def cancel_meeting(self, meeting_id: str, booking: "Booking") -> None:
        await self.http.request("PATCH", f"{self.API}/crm/v3/objects/meetings/{meeting_id}", bearer=self.token,
                                json_body={"properties": {"hs_meeting_outcome": "CANCELED"}},
                                expect_ok=True, what="hubspot meeting")


class SalesforceCRM(CRMProvider):
    """Salesforce via OAuth client-credentials (an External Client App / Connected App with
    the client-credentials flow enabled), or a ready ``access_token`` + ``instance_url``.
    Contacts (or Leads with ``object="Lead"``) + Events."""

    name = "salesforce"

    def __init__(self, client_id: Optional[str] = None, client_secret: Optional[str] = None, *,
                 domain: Optional[str] = None, access_token: Optional[str] = None,
                 instance_url: Optional[str] = None, object: str = "Contact", company: str = "Unknown",
                 api_version: str = "v61.0", http: Optional[HTTPClient] = None) -> None:
        super().__init__(http)
        if not access_token and not (client_id and client_secret and domain):
            raise ValueError("Salesforce needs client_id, client_secret and domain (or access_token + instance_url)")
        self.client_id, self.client_secret = client_id, client_secret
        self.domain = (domain or "").replace("https://", "").rstrip("/")
        self.token, self.instance = access_token, (instance_url or (f"https://{self.domain}" if self.domain else "")).rstrip("/")
        self.object, self.company, self.v = object, company, api_version

    async def _auth(self) -> None:
        res = await self.http.request("POST", f"https://{self.domain}/services/oauth2/token", form={
            "grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret},
            expect_ok=True, what="salesforce token")
        data = res.json()
        self.token, self.instance = data["access_token"], data.get("instance_url", self.instance).rstrip("/")

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        if not self.token:
            await self._auth()
        url = f"{self.instance}/services/data/{self.v}{path}"
        res = await self.http.request(method, url, bearer=self.token, **kw)
        if res.status == 401 and self.client_id:  # token expired: re-auth once
            await self._auth()
            res = await self.http.request(method, url, bearer=self.token, **kw)
        if not res.ok:
            raise RuntimeError(f"salesforce {method} {path}: HTTP {res.status} {res.text[:300]}")
        return res.json()

    async def upsert_contact(self, booking: "Booking") -> str:
        email = booking.attendee_email.replace("\\", "\\\\").replace("'", "\\'")
        found = await self._call("GET", "/query", params={"q": f"SELECT Id FROM {self.object} WHERE Email = '{email}' LIMIT 1"})
        first, last = _split_name(booking.attendee_name)
        fields: Dict[str, Any] = {"FirstName": first, "LastName": last, "Email": booking.attendee_email}
        if booking.attendee_phone:
            fields["Phone"] = booking.attendee_phone
        if found.get("records"):
            cid = found["records"][0]["Id"]
            await self._call("PATCH", f"/sobjects/{self.object}/{cid}", json_body=fields)
            return cid
        if self.object == "Lead":
            fields.setdefault("Company", self.company)
        return (await self._call("POST", f"/sobjects/{self.object}", json_body=fields))["id"]

    def _event(self, booking: "Booking", join_url: str) -> Dict[str, Any]:
        return {"Subject": booking.title[:255], "StartDateTime": _iso(booking.start), "EndDateTime": _iso(booking.end),
                "Location": join_url[:255], "Description": (booking.notes or "") + f"\nJoin: {join_url}\nnodemeet {booking.id}"}

    async def create_meeting(self, booking: "Booking", contact_id: str, join_url: str) -> str:
        return (await self._call("POST", "/sobjects/Event", json_body={**self._event(booking, join_url),
                                                                        "WhoId": contact_id}))["id"]

    async def update_meeting(self, meeting_id: str, booking: "Booking", join_url: str) -> None:
        await self._call("PATCH", f"/sobjects/Event/{meeting_id}", json_body=self._event(booking, join_url))

    async def cancel_meeting(self, meeting_id: str, booking: "Booking") -> None:
        await self._call("PATCH", f"/sobjects/Event/{meeting_id}",
                         json_body={"Subject": f"[Cancelled] {booking.title}"[:255]})


class PipedriveCRM(CRMProvider):
    name = "pipedrive"

    def __init__(self, api_token: str, *, company_domain: Optional[str] = None, http: Optional[HTTPClient] = None) -> None:
        super().__init__(http)
        self.token = api_token
        self.base = f"https://{company_domain}.pipedrive.com/api/v1" if company_domain else "https://api.pipedrive.com/v1"

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        params = dict(kw.pop("params", {}) or {}, api_token=self.token)
        res = await self.http.request(method, self.base + path, params=params, expect_ok=True, what="pipedrive", **kw)
        data = res.json() or {}
        if data.get("success") is False:
            raise RuntimeError(f"pipedrive: {data.get('error')}")
        return data.get("data")

    async def upsert_contact(self, booking: "Booking") -> str:
        found = await self._call("GET", "/persons/search", params={"term": booking.attendee_email, "fields": "email",
                                                                   "exact_match": "true", "limit": 1})
        items = (found or {}).get("items") or []
        if items:
            return str(items[0]["item"]["id"])
        body: Dict[str, Any] = {"name": booking.attendee_name, "email": [{"value": booking.attendee_email, "primary": True}]}
        if booking.attendee_phone:
            body["phone"] = [{"value": booking.attendee_phone, "primary": True}]
        return str((await self._call("POST", "/persons", json_body=body))["id"])

    def _activity(self, booking: "Booking", join_url: str) -> Dict[str, Any]:
        mins = max(1, int((booking.end - booking.start).total_seconds() // 60))
        start = booking.start.astimezone(timezone.utc)
        return {"subject": booking.title, "type": "meeting", "due_date": start.strftime("%Y-%m-%d"),
                "due_time": start.strftime("%H:%M"), "duration": f"{mins // 60:02d}:{mins % 60:02d}",
                "note": f"{booking.notes or ''}<br>Join: {join_url}", "location": join_url}

    async def create_meeting(self, booking: "Booking", contact_id: str, join_url: str) -> str:
        return str((await self._call("POST", "/activities", json_body={**self._activity(booking, join_url),
                                                                         "person_id": int(contact_id)}))["id"])

    async def update_meeting(self, meeting_id: str, booking: "Booking", join_url: str) -> None:
        await self._call("PUT", f"/activities/{meeting_id}", json_body=self._activity(booking, join_url))

    async def cancel_meeting(self, meeting_id: str, booking: "Booking") -> None:
        await self._call("PUT", f"/activities/{meeting_id}", json_body={"subject": f"[Cancelled] {booking.title}",
                                                                        "done": 1})


class ZohoCRM(CRMProvider):
    """Zoho CRM with a self-client refresh token. ``dc`` = com, eu, in, com.au, jp, ca, sa."""

    name = "zoho"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, *, dc: str = "com",
                 http: Optional[HTTPClient] = None) -> None:
        super().__init__(http)
        self.client_id, self.client_secret, self.refresh_token, self.dc = client_id, client_secret, refresh_token, dc
        self.token: Optional[str] = None
        self.expires = 0.0

    async def _auth(self) -> str:
        if self.token and time.time() < self.expires - 60:
            return self.token
        res = await self.http.request("POST", f"https://accounts.zoho.{self.dc}/oauth/v2/token", params={
            "refresh_token": self.refresh_token, "client_id": self.client_id, "client_secret": self.client_secret,
            "grant_type": "refresh_token"}, expect_ok=True, what="zoho token")
        data = res.json()
        if "access_token" not in data:
            raise RuntimeError(f"zoho token: {data}")
        self.token, self.expires = data["access_token"], time.time() + int(data.get("expires_in", 3600))
        return self.token

    async def _call(self, method: str, path: str, body: Any = None) -> Any:
        tok = await self._auth()
        res = await self.http.request(method, f"https://www.zohoapis.{self.dc}/crm/v6{path}", json_body=body,
                                      headers={"Authorization": f"Zoho-oauthtoken {tok}"}, expect_ok=True, what="zoho")
        return res.json() or {}

    async def upsert_contact(self, booking: "Booking") -> str:
        first, last = _split_name(booking.attendee_name)
        rec: Dict[str, Any] = {"Email": booking.attendee_email, "First_Name": first, "Last_Name": last}
        if booking.attendee_phone:
            rec["Phone"] = booking.attendee_phone
        data = await self._call("POST", "/Contacts/upsert", {"data": [rec], "duplicate_check_fields": ["Email"]})
        return str(data["data"][0]["details"]["id"])

    def _event(self, booking: "Booking", join_url: str) -> Dict[str, Any]:
        fmt = "%Y-%m-%dT%H:%M:%S+00:00"
        return {"Event_Title": booking.title, "Start_DateTime": booking.start.astimezone(timezone.utc).strftime(fmt),
                "End_DateTime": booking.end.astimezone(timezone.utc).strftime(fmt), "Venue": join_url[:255],
                "Description": f"{booking.notes or ''}\nJoin: {join_url}"}

    async def create_meeting(self, booking: "Booking", contact_id: str, join_url: str) -> str:
        data = await self._call("POST", "/Events", {"data": [{**self._event(booking, join_url),
                                                             "Who_Id": {"id": contact_id}}]})
        return str(data["data"][0]["details"]["id"])

    async def update_meeting(self, meeting_id: str, booking: "Booking", join_url: str) -> None:
        await self._call("PUT", f"/Events/{meeting_id}", {"data": [self._event(booking, join_url)]})

    async def cancel_meeting(self, meeting_id: str, booking: "Booking") -> None:
        await self._call("PUT", f"/Events/{meeting_id}", {"data": [{"Event_Title": f"[Cancelled] {booking.title}"}]})


class CRMService:
    def __init__(self, meet: "NodeMeet", provider: CRMProvider) -> None:
        self.meet, self.provider = meet, provider
        meet.bookings.listeners.append(self.on_booking)

    async def on_booking(self, event: str, booking: "Booking", extra: Dict[str, Any]) -> None:
        if event not in ("booking.created", "booking.rescheduled", "booking.cancelled"):
            return
        try:
            await self._sync(event, booking)
        except Exception as exc:
            info = {"crm": self.provider.name, "booking_id": booking.id, "action": event, "error": str(exc)[:300]}
            await self.meet.emit_webhook("crm.failed", info, tenant_id=booking.tenant_id)
            await self.meet.emit_webhook("integration.error", {"source": "crm", **info}, tenant_id=booking.tenant_id)
            raise

    async def _sync(self, event: str, booking: "Booking") -> None:
        state = dict((booking.metadata.get("crm") or {}).get(self.provider.name) or {})
        join = self.meet.booking_join_url(booking).split("#", 1)[0] if event != "booking.cancelled" else ""
        if event == "booking.created" or (event == "booking.rescheduled" and not state.get("meeting_id")):
            state["contact_id"] = await self.provider.upsert_contact(booking)
            state["meeting_id"] = await self.provider.create_meeting(booking, state["contact_id"], join)
        elif event == "booking.rescheduled":
            await self.provider.update_meeting(state["meeting_id"], booking, join)
        elif event == "booking.cancelled" and state.get("meeting_id"):
            await self.provider.cancel_meeting(state["meeting_id"], booking)
            state["cancelled"] = True
        else:
            return
        crm = dict(booking.metadata.get("crm") or {})
        crm[self.provider.name] = state
        booking.metadata["crm"] = crm
        await self.meet.storage.save_booking(booking)
        await self.meet.emit_webhook("crm.synced", {"crm": self.provider.name, "booking_id": booking.id,
                                                    "action": event, **state}, tenant_id=booking.tenant_id)


def make_crm(kind: str, *args: Any, **kw: Any) -> CRMProvider:
    kinds = {"hubspot": HubSpotCRM, "salesforce": SalesforceCRM, "pipedrive": PipedriveCRM, "zoho": ZohoCRM}
    key = kind.lower()
    if key not in kinds:
        from ..easy import did_you_mean
        raise ValueError(f"unknown CRM {kind!r}." + did_you_mean(key, kinds))
    return kinds[key](*args, **kw)
