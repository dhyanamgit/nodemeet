"""REST API (JSON), framework-neutral.

Auth: ``Authorization: Bearer <key>`` (or ``X-API-Key``). The key is either
the *master* key (``NodeMeet(api_key=...)``) or a tenant key (``nmk_...``)
created through ``POST /api/keys``. Tenant keys only see their own tenant's
rooms, hosts, bookings and webhooks.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .exceptions import (AvailabilityNotFound, BookingNotFound, InvalidToken, RoomFull,
                         RoomNotFound, SlotUnavailable)
from ._http import HTTPError, Request, Response, Router, json_response, text_response
from .integrations.webhooks import WebhookEndpoint
from .models import Booking, BookingStatus, RoomConfig, parse_dt, utcnow
from .scheduling.availability import Availability, ZoneInfo
from .tenancy import MASTER, ApiKey, Principal, constant_time_equals, generate_key, hash_key
from .tokens import Role

if TYPE_CHECKING:
    from .server import NodeMeet

MAX_RANGE_DAYS = 62

ERRORS = [
    (BookingNotFound, 404, "booking_not_found"), (RoomNotFound, 404, "room_not_found"),
    (AvailabilityNotFound, 404, "availability_not_found"), (SlotUnavailable, 409, "slot_unavailable"),
    (InvalidToken, 401, "invalid_token"), (RoomFull, 403, "room_full"),
    (NotImplementedError, 501, "not_supported"),
    (ValueError, 400, "bad_request"), (KeyError, 400, "missing_field"),
]


def map_exception(exc: Exception) -> Optional[Response]:
    for cls, status, code in ERRORS:
        if isinstance(exc, cls):
            msg = f"missing field {exc}" if type(exc) is KeyError else str(exc)
            return json_response({"error": code, "message": msg}, status)
    return None


def cors_headers(origin: Optional[str], allowed: List[str]) -> Dict[str, str]:
    if not origin or not allowed or ("*" not in allowed and origin not in allowed):
        return {}
    return {"Access-Control-Allow-Origin": "*" if "*" in allowed else origin, "Vary": "Origin",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, X-API-Key, X-Manage-Token",
            "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS"}


def _unauthorized() -> HTTPError:
    return HTTPError(401, "unauthorized", "missing or invalid API key")


class API:
    def __init__(self, meet: "NodeMeet") -> None:
        self.meet = meet
        self._touch: Dict[str, float] = {}

    # -- auth --------------------------------------------------------------
    def _given_key(self, req: Request) -> str:
        auth = req.header("authorization")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return req.header("x-api-key").strip()

    async def principal(self, req: Request) -> Optional[Principal]:
        """Resolve the caller, or None for anonymous requests."""
        cache = req.match_info.get("__principal__")
        if cache is not None:
            return cache  # type: ignore[return-value]
        given = self._given_key(req)
        if not given:
            return None
        master = self.meet.api_key
        if master and constant_time_equals(given, master):
            return MASTER
        if given.startswith("nms1."):  # admin dashboard session (SSO sign-in)
            from .tokens import verify_blob
            try:
                data = verify_blob(self.meet.tokens, given, purpose="admin")
            except Exception:  # noqa: BLE001
                raise _unauthorized() from None
            return MASTER if not data.get("tenant") else Principal(tenant_id=data["tenant"])
        if not given.startswith("nmk_"):
            raise _unauthorized()
        try:
            key = await self.meet.storage.get_api_key_by_hash(hash_key(given))
        except NotImplementedError:
            key = None
        if key is None or not key.active:
            raise _unauthorized()
        if not await self.meet.rate_limiter.hit(key.id, key.rate_limit_per_minute):
            raise HTTPError(429, "rate_limited", "too many requests for this API key",
                            headers={"Retry-After": "60"})
        now = time.time()
        if now - self._touch.get(key.id, 0) > 60:  # record last use at most once a minute
            self._touch[key.id] = now
            key.last_used_at = utcnow()
            await self.meet.storage.save_api_key(key)
        p = Principal(tenant_id=key.tenant_id, scopes=frozenset(key.scopes), key_id=key.id)
        req.match_info["__principal__"] = p  # type: ignore[assignment]
        return p

    async def require(self, req: Request, scope: str) -> Principal:
        if not self.meet.api_key and not self._given_key(req).startswith(("nmk_", "nms1.")):
            raise HTTPError(403, "admin_disabled", "set api_key to enable admin endpoints")
        p = await self.principal(req)
        if p is None:
            raise _unauthorized()
        if not p.can(scope):
            raise HTTPError(403, "forbidden", f"this key lacks the {scope!r} scope")
        return p

    async def require_master(self, req: Request) -> Principal:
        p = await self.require(req, "*")
        if not p.is_master:
            raise HTTPError(403, "forbidden", "only the master key can do this")
        return p

    async def owned_room(self, p: Principal, room_id: str) -> RoomConfig:
        config = await self.meet.storage.get_room(room_id)
        if config is None or not p.owns(config.tenant_id):
            raise RoomNotFound(room_id)  # don't reveal other tenants' rooms
        return config

    async def booking_for(self, req: Request, token: Optional[str] = None,
                          scope: str = "bookings:read") -> Booking:
        booking = await self.meet.bookings.get(req.match_info["booking_id"])
        try:
            p = await self.principal(req)
        except HTTPError:
            p = None
        if p is not None and p.can(scope) and p.owns(booking.tenant_id):
            return booking
        token = token or req.query.get("t") or req.header("x-manage-token")
        if not token or not constant_time_equals(token, booking.manage_token):
            raise _unauthorized()
        return booking

    def booking_payload(self, booking: Booking, *, secrets: bool) -> Dict[str, Any]:
        d = booking.to_dict(include_secrets=secrets)
        if secrets:
            d["join_url"] = self.meet.booking_join_url(booking, "attendee")
            d["manage_url"] = self.meet.manage_url(booking)
            d["calendar_links"] = self.meet.bookings.calendar_links(booking)
        return d

    def register(self, r: Router) -> None:
        r.add("GET", "/api/health", self.health)
        r.add("GET", "/api/me", self.me)
        r.add("POST", "/api/rooms", self.create_room)
        r.add("GET", "/api/rooms", self.list_rooms)
        r.add("GET", "/api/rooms/{room_id}", self.get_room)
        r.add("PATCH", "/api/rooms/{room_id}", self.update_room)
        r.add("DELETE", "/api/rooms/{room_id}", self.delete_room)
        r.add("POST", "/api/tokens", self.create_token)
        r.add("GET", "/api/rooms/{room_id}/join-list", self.get_join_list)
        r.add("POST", "/api/rooms/{room_id}/join-list", self.add_join_list)
        r.add("POST", "/api/rooms/{room_id}/join-list/remove", self.remove_join_list)
        r.add("GET", "/api/rooms/{room_id}/bans", self.list_bans)
        r.add("DELETE", "/api/rooms/{room_id}/bans/{user_id}", self.unban)
        r.add("PUT", "/api/hosts/{host_id}/availability", self.put_availability)
        r.add("GET", "/api/hosts/{host_id}/availability", self.get_availability)
        r.add("GET", "/api/hosts/{host_id}/slots", self.slots)
        r.add("POST", "/api/bookings", self.create_booking)
        r.add("GET", "/api/bookings", self.list_bookings)
        r.add("GET", "/api/bookings/{booking_id}", self.get_booking)
        r.add("POST", "/api/bookings/{booking_id}/cancel", self.cancel_booking)
        r.add("POST", "/api/bookings/{booking_id}/reschedule", self.reschedule_booking)
        r.add("GET", "/api/bookings/{booking_id}/invite.ics", self.booking_ics)
        r.add("POST", "/api/keys", self.create_key)
        r.add("GET", "/api/keys", self.list_keys)
        r.add("DELETE", "/api/keys/{key_id}", self.revoke_key)
        r.add("GET", "/api/permissions", self.permissions_catalog)
        r.add("GET", "/api/roles", self.list_roles)
        r.add("POST", "/api/roles", self.create_role)
        r.add("GET", "/api/roles/{name}", self.get_role)
        r.add("PATCH", "/api/roles/{name}", self.update_role)
        r.add("DELETE", "/api/roles/{name}", self.delete_role)
        r.add("POST", "/api/roles/{name}/reset", self.reset_role)
        r.add("GET", "/api/branding", self.get_branding)
        r.add("PUT", "/api/branding", self.put_branding)
        r.add("PATCH", "/api/branding", self.patch_branding)
        r.add("GET", "/api/branding/resolve", self.resolve_branding)
        r.add("POST", "/api/webhooks", self.create_webhook)
        r.add("GET", "/api/webhooks", self.list_webhooks)
        r.add("DELETE", "/api/webhooks/{webhook_id}", self.delete_webhook)

    # -- health / me ---------------------------------------------------------
    async def health(self, req: Request) -> Response:
        from ._version import __version__
        return json_response({"ok": True, "version": __version__, "sfu": self.meet.sfu.available,
                              "node": self.meet.broker.node_id, "clustered": self.meet.bus is not None,
                              "live_rooms": len(self.meet.rooms.live)})

    async def me(self, req: Request) -> Response:
        p = await self.principal(req)
        if p is None:
            raise _unauthorized()
        return json_response({"master": p.is_master, "tenant_id": p.tenant_id,
                              "scopes": sorted(p.scopes), "key_id": p.key_id})

    # -- rooms -------------------------------------------------------------------
    async def create_room(self, req: Request) -> Response:
        p = await self.require(req, "rooms:write")
        body = req.json()
        tenant = body.get("tenant_id") if p.is_master else p.tenant_id
        if body.get("id"):
            existing = await self.meet.storage.get_room(str(body["id"]))
            if existing is not None and not p.owns(existing.tenant_id):
                raise HTTPError(409, "room_exists", "that room id is taken")
        config = await self.meet.create_room(
            room_id=body.get("id"), name=body.get("name", ""), mode=body.get("mode", "auto"),
            max_participants=body.get("max_participants"), metadata=body.get("metadata"),
            tenant_id=tenant, waiting_room=body.get("waiting_room", body.get("lobby")),
            join_list=body.get("join_list") or body.get("allowed_users") or [],
            chat_enabled=body.get("chat_enabled", True) is not False, locked=bool(body.get("locked")),
            default_role=body.get("default_role"), role_overrides=body.get("role_overrides"),
            branding=body.get("branding"))
        return json_response(config.to_dict(), 201)

    async def list_rooms(self, req: Request) -> Response:
        p = await self.require(req, "rooms:read")
        rooms = [c for c in await self.meet.storage.list_rooms() if p.owns(c.tenant_id)]
        live = {r["id"]: r for r in self.meet.rooms.stats()}
        return json_response({"rooms": [{**c.to_dict(), "live": live.get(c.id)} for c in rooms]})

    async def get_room(self, req: Request) -> Response:
        p = await self.require(req, "rooms:read")
        config = await self.owned_room(p, req.match_info["room_id"])
        live = self.meet.rooms.get(config.id)
        return json_response({**config.to_dict(), "live": live.snapshot() if live else None})

    async def update_room(self, req: Request) -> Response:
        p = await self.require(req, "rooms:write")
        config = await self.owned_room(p, req.match_info["room_id"])
        body = req.json()
        data = config.to_dict()
        data.update({k: body[k] for k in ("name", "mode", "locked", "max_participants", "metadata",
                                          "lobby", "chat_enabled", "banned_users", "role_overrides",
                                          "allowed_users",
                                          "branding", "default_role") if k in body})
        new = RoomConfig.from_dict(data)
        await self.meet.rooms.update_config(new)
        return json_response(new.to_dict())

    async def delete_room(self, req: Request) -> Response:
        p = await self.require(req, "rooms:write")
        config = await self.owned_room(p, req.match_info["room_id"])
        await self.meet.rooms.delete(config.id)
        return Response(status=204)

    async def create_token(self, req: Request) -> Response:
        p = await self.require(req, "tokens:create")
        body = req.json()
        room, user_id = str(body["room"]), str(body["user_id"])
        config = await self.meet.storage.get_room(room)
        if config is not None and not p.owns(config.tenant_id):
            raise RoomNotFound(room)
        tenant = (config.tenant_id if config else body.get("tenant_id")) if p.is_master else p.tenant_id
        if config is None and tenant:  # claim the room for this tenant up front
            await self.meet.create_room(room_id=room, tenant_id=tenant)
        ttl = int(body["ttl"]) if body.get("ttl") else None
        role = Role.parse(body.get("role") or (config.default_role if config else None)
                          or self.meet.roles.default_role)
        if await self.meet.roles.get(role, tenant) is None:
            raise ValueError(f"role {role!r} does not exist")
        token = self.meet.create_token(room, user_id, role, name=body.get("name"), ttl=ttl,
                                       meta=body.get("meta"), tenant=tenant,
                                       grant=list(body.get("grant") or ()) + (
                                           ["room.bypass_lobby"] if body.get("skip_waiting_room") else []),
                                       revoke=body.get("revoke") or ())
        claims = self.meet.tokens.verify(token)
        return json_response({"token": token, "join_url": self.meet.join_url(room, token),
                              "expires_at": claims.exp, "role": claims.role,
                              "tenant_id": tenant}, 201)

    # -- availability & slots ----------------------------------------------------
    async def put_availability(self, req: Request) -> Response:
        p = await self.require(req, "availability:write")
        host_id = req.match_info["host_id"]
        existing = await self.meet.storage.get_availability(host_id)
        if existing is not None and not p.owns(existing.tenant_id):
            raise HTTPError(409, "host_exists", "that host id belongs to another tenant")
        body = req.json()
        body["host_id"] = host_id
        body["tenant_id"] = body.get("tenant_id") if p.is_master else p.tenant_id
        av = await self.meet.bookings.set_availability(Availability.from_dict(body))
        return json_response(av.to_dict())

    async def get_availability(self, req: Request) -> Response:
        av = await self.meet.bookings.get_availability(req.match_info["host_id"])
        try:
            p = await self.principal(req)
        except HTTPError:
            p = None
        if p is not None and p.owns(av.tenant_id):
            return json_response(av.to_dict())
        return json_response({"host_id": av.host_id, "timezone": av.timezone, "title": av.title,
                              "host_name": av.host_name, "duration_minutes": av.duration_minutes,
                              "max_days_ahead": av.max_days_ahead})

    async def slots(self, req: Request) -> Response:
        host_id = req.match_info["host_id"]
        av = await self.meet.bookings.get_availability(host_id)
        q = req.query
        viewer_tz = q.get("tz") or av.timezone
        ZoneInfo(viewer_tz)  # validate
        today = datetime.now(ZoneInfo(av.timezone)).date()
        start = date.fromisoformat(q["start"]) if q.get("start") else today
        end = date.fromisoformat(q["end"]) if q.get("end") else start + timedelta(days=13)
        if (end - start).days > MAX_RANGE_DAYS or end < start:
            raise ValueError(f"date range must be 0-{MAX_RANGE_DAYS} days")
        duration = None
        if q.get("duration"):
            p = await self.principal(req)
            if p is not None and p.owns(av.tenant_id):
                duration = int(q["duration"])
        slots = await self.meet.bookings.find_slots(host_id, start, end, duration_minutes=duration)
        return json_response({"host_id": host_id, "timezone": viewer_tz,
                              "host_timezone": av.timezone,
                              "duration_minutes": duration or av.duration_minutes,
                              "slots": [s.to_dict(viewer_tz) for s in slots]})

    # -- bookings ----------------------------------------------------------------------
    async def create_booking(self, req: Request) -> Response:
        body = req.json()
        host_id = str(body["host_id"])
        av = await self.meet.bookings.get_availability(host_id)
        try:
            p = await self.principal(req)
        except HTTPError:
            p = None
        admin = p is not None and p.can("bookings:write") and p.owns(av.tenant_id)
        start = parse_dt(body["start"])
        if start is None:
            raise ValueError("start is required")
        tz = body.get("timezone")
        if tz:
            ZoneInfo(tz)
        booking = await self.meet.bookings.book(
            host_id, start, attendee_name=str(body.get("name", "")),
            attendee_email=str(body.get("email", "")), attendee_timezone=tz,
            notes=str(body.get("notes", ""))[:2000],
            duration_minutes=int(body["duration"]) if admin and body.get("duration") else None,
            title=body.get("title") if admin else None,
            metadata=body.get("metadata") if admin else None, tenant_id=av.tenant_id,
            attendee_phone=str(body.get("phone") or "")[:32] or None)
        if av.collect_phone and not booking.attendee_phone and not admin:
            pass  # the widget asks for it; enforcement is the integrator's choice
        payload = self.booking_payload(booking, secrets=True)
        if booking.awaiting_payment:
            payload["payment_url"] = booking.payment.get("url")
            payload["payment_expires_at"] = booking.payment.get("expires_at")
        return json_response(payload, 201)

    async def list_bookings(self, req: Request) -> Response:
        p = await self.require(req, "bookings:read")
        q = req.query
        items = await self.meet.bookings.list(
            host_id=q.get("host_id"), start=parse_dt(q.get("start")), end=parse_dt(q.get("end")),
            status=BookingStatus(q["status"]) if q.get("status") else None)
        return json_response({"bookings": [b.to_dict() for b in items if p.owns(b.tenant_id)]})

    async def get_booking(self, req: Request) -> Response:
        booking = await self.booking_for(req)
        return json_response(self.booking_payload(booking, secrets=True))

    async def cancel_booking(self, req: Request) -> Response:
        body = req.json()
        booking = await self.booking_for(req, body.get("t"), "bookings:write")
        booking = await self.meet.bookings.cancel(booking.id, reason=body.get("reason"))
        return json_response(self.booking_payload(booking, secrets=False))

    async def reschedule_booking(self, req: Request) -> Response:
        body = req.json()
        booking = await self.booking_for(req, body.get("t"), "bookings:write")
        start = parse_dt(body["start"])
        if start is None:
            raise ValueError("start is required")
        booking = await self.meet.bookings.reschedule(booking.id, start)
        return json_response(self.booking_payload(booking, secrets=True))

    async def booking_ics(self, req: Request) -> Response:
        booking = await self.booking_for(req)
        who = "host" if req.query.get("as") == "host" and not req.query.get("t") else "attendee"
        return text_response(self.meet.bookings.ics(booking, who), "text/calendar",
                             headers={"Content-Disposition": f'attachment; filename="{booking.id}.ics"'})

    # -- API keys ---------------------------------------------------------------------
    async def create_key(self, req: Request) -> Response:
        await self.require_master(req)
        body = req.json()
        plaintext, prefix, digest = generate_key()
        key = ApiKey(tenant_id=str(body["tenant_id"]), key_hash=digest, prefix=prefix,
                     name=str(body.get("name", "")), scopes=list(body.get("scopes") or ["*"]),
                     rate_limit_per_minute=body.get("rate_limit_per_minute"),
                     expires_at=parse_dt(body.get("expires_at")))
        await self.meet.storage.save_api_key(key)
        return json_response({**key.to_dict(include_hash=False), "key": plaintext}, 201)

    async def list_keys(self, req: Request) -> Response:
        p = await self.require(req, "*")
        tenant = req.query.get("tenant_id") if p.is_master else p.tenant_id
        keys = await self.meet.storage.list_api_keys(tenant)
        return json_response({"keys": [k.to_dict(include_hash=False) for k in keys]})

    async def revoke_key(self, req: Request) -> Response:
        p = await self.require(req, "*")
        key = await self.meet.storage.get_api_key(req.match_info["key_id"])
        if key is None or not p.owns(key.tenant_id):
            raise HTTPError(404, "key_not_found")
        key.revoked = True
        await self.meet.storage.save_api_key(key)
        return json_response(key.to_dict(include_hash=False))

    # -- stored webhooks -------------------------------------------------------------------
    async def create_webhook(self, req: Request) -> Response:
        import secrets as _secrets
        p = await self.require(req, "webhooks:manage")
        body = req.json()
        url = str(body["url"])
        if not url.startswith(("https://", "http://")):
            raise ValueError("url must be http(s)")
        events = body.get("events")
        from .events import EVENTS as _ALL, known
        from .easy import did_you_mean
        for ev in events or []:
            if not known(ev):
                raise ValueError(f"unknown event {ev!r}." + did_you_mean(ev, _ALL))
        ep = WebhookEndpoint(url=url, secret=str(body.get("secret") or _secrets.token_urlsafe(24)),
                             events=set(events) if events else None,
                             tenant_id=body.get("tenant_id") if p.is_master else p.tenant_id)
        await self.meet.storage.save_webhook(ep)
        self.meet.invalidate_webhook_cache()
        return json_response(ep.to_dict(), 201)

    async def list_webhooks(self, req: Request) -> Response:
        p = await self.require(req, "webhooks:manage")
        items = await self.meet.storage.list_webhooks(None if p.is_master else p.tenant_id)
        return json_response({"webhooks": [w.to_dict(include_secret=False) for w in items
                                           if p.owns(w.tenant_id)]})

    async def delete_webhook(self, req: Request) -> Response:
        p = await self.require(req, "webhooks:manage")
        wid = req.match_info["webhook_id"]
        items = await self.meet.storage.list_webhooks(None if p.is_master else p.tenant_id)
        if not any(w.id == wid and p.owns(w.tenant_id) for w in items):
            raise HTTPError(404, "webhook_not_found")
        await self.meet.storage.delete_webhook(wid)
        self.meet.invalidate_webhook_cache()
        return Response(status=204)

    # -- permissions & roles ------------------------------------------------------------
    def _tenant_arg(self, req: Request, p: Principal, body: Optional[Dict[str, Any]] = None) -> Optional[str]:
        if not p.is_master:
            return p.tenant_id
        return (body or {}).get("tenant_id") or req.query.get("tenant_id") or None

    async def permissions_catalog(self, req: Request) -> Response:
        from .permissions import KNOWN_ATTRIBUTES, PERMISSIONS
        return json_response({"permissions": PERMISSIONS, "attributes": KNOWN_ATTRIBUTES,
                              "wildcards": ["*", "<group>.*  e.g. chat.*, moderate.*"]})

    async def list_roles(self, req: Request) -> Response:
        p = await self.require(req, "rooms:read")
        tenant = self._tenant_arg(req, p)
        include_deleted = req.query.get("include_deleted") == "1"
        roles = await self.meet.roles.all(tenant, include_deleted=include_deleted)
        return json_response({"roles": [r.to_dict() for r in roles.values()]})

    async def get_role(self, req: Request) -> Response:
        p = await self.require(req, "rooms:read")
        role = await self.meet.roles.get(req.match_info["name"], self._tenant_arg(req, p))
        if role is None:
            raise HTTPError(404, "role_not_found")
        return json_response(role.to_dict())

    async def create_role(self, req: Request) -> Response:
        p = await self.require(req, "roles:manage")
        body = req.json()
        tenant = self._tenant_arg(req, p, body)
        name = str(body["name"])
        existing = await self.meet.roles.get(name, tenant)
        if existing is not None and not existing.builtin:
            raise HTTPError(409, "role_exists", f"role {name!r} already exists; use PATCH")
        role = await self.meet.roles.create(name, body.get("permissions") or [],
                                            attributes=body.get("attributes"), tenant_id=tenant,
                                            based_on=body.get("based_on"))
        return json_response(role.to_dict(), 201)

    async def update_role(self, req: Request) -> Response:
        p = await self.require(req, "roles:manage")
        body = req.json()
        tenant = self._tenant_arg(req, p, body)
        name = req.match_info["name"]
        if await self.meet.roles.get(name, tenant) is None:
            raise HTTPError(404, "role_not_found")
        role = await self.meet.roles.update(
            name, tenant_id=tenant, permissions=body.get("permissions"),
            grant=body.get("grant") or (), revoke=body.get("revoke") or (),
            attributes=body.get("attributes"), replace_attributes=bool(body.get("replace_attributes")))
        return json_response(role.to_dict())

    async def delete_role(self, req: Request) -> Response:
        p = await self.require(req, "roles:manage")
        tenant = self._tenant_arg(req, p)
        name = req.match_info["name"]
        if await self.meet.roles.get(name, tenant) is None:
            raise HTTPError(404, "role_not_found")
        if name == self.meet.roles.default_role:
            raise HTTPError(409, "default_role", "change NodeMeet(default_role=...) before deleting it")
        await self.meet.roles.delete(name, tenant)
        return Response(status=204)

    async def reset_role(self, req: Request) -> Response:
        p = await self.require(req, "roles:manage")
        await self.meet.roles.reset(req.match_info["name"], self._tenant_arg(req, p))
        role = await self.meet.roles.get(req.match_info["name"], self._tenant_arg(req, p))
        return json_response(role.to_dict() if role else {"reset": True})

    # -- branding ---------------------------------------------------------------------------
    def _brand_key(self, req: Request, p: Principal) -> str:
        return self._tenant_arg(req, p) or "_default"

    async def get_branding(self, req: Request) -> Response:
        p = await self.require(req, "branding:manage")
        stored = await self.meet.storage.get_branding(self._brand_key(req, p))
        return json_response({"branding": stored or {}})

    async def put_branding(self, req: Request) -> Response:
        from .branding import validate
        p = await self.require(req, "branding:manage")
        body = req.json()
        branding = validate(body.get("branding", body))
        await self.meet.storage.save_branding(self._brand_key(req, p), branding)
        return json_response({"branding": branding})

    async def patch_branding(self, req: Request) -> Response:
        from .branding import deep_merge, validate
        p = await self.require(req, "branding:manage")
        key = self._brand_key(req, p)
        body = req.json()
        merged = validate(deep_merge(await self.meet.storage.get_branding(key) or {},
                                     validate(body.get("branding", body))))
        await self.meet.storage.save_branding(key, merged)
        return json_response({"branding": merged})

    async def resolve_branding(self, req: Request) -> Response:
        """Public: the effective branding for a room or booking host (used by embeds)."""
        room_id, host = req.query.get("room"), req.query.get("host")
        if room_id:
            cfg = await self.meet.storage.get_room(room_id)
            return json_response(await self.meet.branding_for(cfg))
        if host:
            av = await self.meet.storage.get_availability(host)
            return json_response(await self.meet.branding_for(None, av.tenant_id if av else None))
        return json_response(await self.meet.branding_for())

    # -- waiting room join list & bans --------------------------------------------------
    async def get_join_list(self, req: Request) -> Response:
        p = await self.require(req, "rooms:read")
        cfg = await self.owned_room(p, req.match_info["room_id"])
        return json_response({"waiting_room": cfg.lobby, "join_list": cfg.allowed_users})

    async def add_join_list(self, req: Request) -> Response:
        p = await self.require(req, "rooms:write")
        cfg = await self.owned_room(p, req.match_info["room_id"])
        users = [str(u) for u in (req.json().get("users") or [])]
        return json_response({"join_list": await self.meet.add_to_join_list(cfg.id, *users)})

    async def remove_join_list(self, req: Request) -> Response:
        p = await self.require(req, "rooms:write")
        cfg = await self.owned_room(p, req.match_info["room_id"])
        users = [str(u) for u in (req.json().get("users") or [])]
        return json_response({"join_list": await self.meet.remove_from_join_list(cfg.id, *users)})

    async def list_bans(self, req: Request) -> Response:
        p = await self.require(req, "rooms:read")
        cfg = await self.owned_room(p, req.match_info["room_id"])
        return json_response({"banned": [{"user_id": u, **cfg.banned_info.get(u, {})}
                                         for u in cfg.banned_users]})

    async def unban(self, req: Request) -> Response:
        p = await self.require(req, "rooms:write")
        cfg = await self.owned_room(p, req.match_info["room_id"])
        await self.meet.unban(cfg.id, req.match_info["user_id"])
        return Response(status=204)
