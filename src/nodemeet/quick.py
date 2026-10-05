"""Short, forgiving helpers that live directly on ``NodeMeet`` (mixed in).

The idea: everything a developer does day to day is ``meet.<verb>(...)`` with plain
values. Nothing to import, nothing to wire::

    meet = NodeMeet(db="meet.db", email="console")
    standup = meet.room("standup", preset="open")
    print(standup.host_link("Ada"))
    meet.hours("ada", "mon-fri 9-17", timezone="Asia/Kolkata")
    meet.run()
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .easy import (brand_settings, did_you_mean, friendly_permissions, mailer_from_url, need,
                   parse_hours, parse_minutes, parse_price, recording_store_from_url, room_settings)

if TYPE_CHECKING:  # pragma: no cover
    from .models import Booking, RoomConfig
    from .scheduling.slots import Slot

log = logging.getLogger("nodemeet")
BUILTIN_ROLES = ("host", "participant", "viewer")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return s[:40] or "guest"


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _when(value: Any, tz: Optional[str] = None) -> datetime:
    """datetime, Slot, ISO string ('2026-10-05 10:30', '2026-10-05T10:30+05:30')."""
    if hasattr(value, "start") and isinstance(getattr(value, "start"), datetime):
        value = value.start
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace(" ", "T", 1))
    if not isinstance(value, datetime):
        raise TypeError(f"expected a datetime, a slot or an ISO string, got {value!r}")
    if value.tzinfo is None:
        from zoneinfo import ZoneInfo
        value = value.replace(tzinfo=ZoneInfo(tz) if tz else dt_timezone.utc)
    return value


def _timezone(name: Optional[str]) -> str:
    name = name or os.environ.get("NODEMEET_TIMEZONE") or "UTC"
    from zoneinfo import ZoneInfo, available_timezones
    try:
        ZoneInfo(name)
    except Exception:  # noqa: BLE001
        raise ValueError(f"unknown time zone {name!r}." + did_you_mean(name, available_timezones())) from None
    return name


def _creds(value: Any, env: Sequence[str], what: str) -> List[str]:
    """(a, b) / [a, b] / {"client_id": a, ...} / True (= read env vars) -> [a, b]."""
    if value is True or value is None:
        got = [os.environ.get(e, "") for e in env]
        missing = [e for e, v in zip(env, got) if not v]
        if missing:
            raise ValueError(f"{what}: set {', '.join(missing)} (env vars) or pass the values directly")
        return got
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, str):
        return [value]
    return list(value)


class RoomHandle:
    """What ``meet.room(...)`` returns: a room id plus link helpers."""

    def __init__(self, meet: Any, room_id: str) -> None:
        self.meet, self.id = meet, room_id

    def __str__(self) -> str:
        return self.id

    def __repr__(self) -> str:
        return f"<room {self.id!r}>"

    def link(self, name: Optional[str] = None, role: str = "participant", **kw: Any) -> str:
        return self.meet.link(self.id, name, role, **kw)

    def host_link(self, name: str = "Host", **kw: Any) -> str:
        return self.meet.host_link(self.id, name, **kw)

    def guest_link(self, name: Optional[str] = None, **kw: Any) -> str:
        return self.meet.guest_link(self.id, name, **kw)

    def viewer_link(self, name: Optional[str] = None, **kw: Any) -> str:
        return self.meet.link(self.id, name, "viewer", **kw)

    def embed(self, name: Optional[str] = None, role: str = "participant", *, height: str = "600px",
              **kw: Any) -> str:
        """An ``<iframe>`` for your page template."""
        link = self.link(name, role, **kw)
        return self.meet.embed_room(self.id, link.split("#token=", 1)[1], height=height)

    async def update(self, preset: Optional[str] = None, **settings: Any) -> "RoomConfig":
        return await self.meet._apply_room(self.id, room_settings(preset, **settings))

    async def let_in(self, *users: str) -> List[str]:
        """Add people to the join list (they skip the waiting room)."""
        return await self.meet.add_to_join_list(self.id, *users)

    async def unblock(self, user_id: str) -> None:
        await self.meet.unban(self.id, user_id)


class BookingPage:
    """What ``meet.hours(...)`` returns."""

    def __init__(self, meet: Any, host_id: str) -> None:
        self.meet, self.host_id = meet, host_id

    @property
    def url(self) -> str:
        return self.meet.booking_page_url(self.host_id)

    def __str__(self) -> str:
        return self.url

    def __repr__(self) -> str:
        return f"<booking page {self.host_id!r} {self.url}>"

    def embed(self, inline: bool = True) -> str:
        return self.meet.embed_booking(self.host_id, inline=inline)

    async def slots(self, days: int = 7, **kw: Any) -> List["Slot"]:
        return await self.meet.slots(self.host_id, days=days, **kw)

    async def book(self, when: Any, *, name: str, email: str, **kw: Any) -> "Booking":
        return await self.meet.book(self.host_id, when, name=name, email=email, **kw)


class EasyMixin:
    """Mixed into NodeMeet. See the module docstring."""

    # -- bookkeeping for things declared before the server starts -------------------------
    def _easy_init(self) -> None:
        self._declared: Dict[str, Tuple[str, Dict[str, Any]]] = {}  # key -> (kind, spec)
        self._easy_tasks: set = set()
        self._integrations: List[str] = []

    def _declare(self, kind: str, key: str, spec: Dict[str, Any]) -> None:
        self._declared[f"{kind}:{key}"] = (kind, spec)
        if getattr(self, "_started", False):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # applied on the next startup
            task = loop.create_task(self._apply_one(f"{kind}:{key}", kind, spec))
            self._easy_tasks.add(task)
            task.add_done_callback(self._easy_tasks.discard)

    async def _easy_apply(self) -> None:
        for key, (kind, spec) in list(self._declared.items()):
            try:
                await self._apply_one(key, kind, spec)
            except Exception:  # noqa: BLE001
                log.exception("nodemeet: could not apply %s", key)
                raise

    async def _apply_one(self, key: str, kind: str, spec: Dict[str, Any]) -> None:
        """Code is the source of truth *when it changes*: edits made later through the REST
        API / admin UI survive restarts until you change the declaration itself."""
        digest = _digest(spec)
        try:
            seen = await self.storage.get_record("declared", key)
        except NotImplementedError:
            seen = None
        exists = await self._declared_exists(kind, spec)
        if exists and seen and seen.get("hash") == digest:
            return
        if kind == "room":
            await self._apply_room(spec["id"], spec["settings"])
        elif kind == "hours":
            from .scheduling.availability import Availability
            await self.storage.save_availability(Availability.from_dict(spec["availability"]))
        elif kind == "role":
            await self._apply_role(spec)
        try:
            await self.storage.put_record("declared", key, {"hash": digest})
        except NotImplementedError:
            pass

    async def _declared_exists(self, kind: str, spec: Dict[str, Any]) -> bool:
        if kind == "room":
            return await self.storage.get_room(spec["id"]) is not None
        if kind == "hours":
            return await self.storage.get_availability(spec["availability"]["host_id"]) is not None
        if kind == "role":
            got = await self.roles.get(spec["name"], spec.get("tenant"))
            return got is not None and not got.builtin
        return False

    # -- rooms ----------------------------------------------------------------------------------
    def room(self, room_id: Optional[str] = None, preset: Optional[str] = None, **settings: Any) -> RoomHandle:
        """Declare a room. Works before or after the server starts; returns link helpers.

        Presets: meeting (default), open, private, webinar, town-hall, classroom, interview,
        1on1, support, drop-in. Settings: name, waiting_room, join_list, max_participants,
        mode, chat_enabled, locked, default_role, role_overrides, branding, lobby_mode,
        lobby_message, sso_required, tenant_id, metadata.
        """
        rid = str(room_id or ("room-" + secrets.token_hex(3)))
        if preset is None and not settings and f"room:{rid}" in self._declared:
            return RoomHandle(self, rid)  # just looking it up
        merged = room_settings(preset, **settings)
        if "branding" in merged and merged["branding"]:
            merged["branding"] = brand_settings(**merged["branding"])
        self._declare("room", rid, {"id": rid, "settings": merged})
        return RoomHandle(self, rid)

    async def _apply_room(self, room_id: str, settings: Dict[str, Any]) -> "RoomConfig":
        from .models import ROOM_MODES
        s = dict(settings)
        if "mode" in s and s["mode"] not in ROOM_MODES:
            raise ValueError(f"unknown room mode {s['mode']!r}." + did_you_mean(s["mode"], ROOM_MODES))
        cfg = await self.storage.get_room(room_id)
        create_keys = ("name", "mode", "max_participants", "metadata", "tenant_id", "waiting_room",
                       "join_list", "chat_enabled", "locked", "default_role", "role_overrides", "branding")
        if cfg is None:
            cfg = await self.create_room(room_id=room_id, **{k: s.pop(k) for k in create_keys if k in s})
            if not s:
                return cfg
        rename = {"waiting_room": "lobby", "join_list": "allowed_users"}
        for k, v in s.items():
            attr = rename.get(k, k)
            if attr == "allowed_users":
                v = list(dict.fromkeys([*cfg.allowed_users, *v]))
            setattr(cfg, attr, v)
        await self._config_saved(cfg)
        return cfg

    # -- links ----------------------------------------------------------------------------------
    def link(self, room: Any, name: Optional[str] = None, role: str = "participant", *,
             user_id: Optional[str] = None, email: Optional[str] = None,
             skip_waiting_room: bool = False, ttl: Union[None, int, str] = None,
             tenant: Optional[str] = None, can: Any = (), cannot: Any = ()) -> str:
        """A ready-to-share join link. Only ``room`` is required.

        ``ttl`` takes seconds or '2h' / '1d'. ``can`` / ``cannot`` adjust permissions for this
        one person in plain words ("mic, camera, chat, screen, record, kick, moderate...").
        """
        room_id = str(room)
        role = str(role or "participant").strip().lower()
        known = set(BUILTIN_ROLES) | {s["name"] for k, s in self._declared.values() if k == "role"} | \
            {r.name for r in getattr(self, "_initial_roles", [])}
        if role not in known:
            hit = did_you_mean(role, known)
            if "Did you mean" in hit:  # probably a typo of a built-in/declared role
                log.warning("nodemeet: role %r is not declared in code.%s (fine if it was created "
                            "through the API)", role, hit)
        uid = user_id or email or (f"{_slug(name)}-{secrets.token_hex(2)}" if name else
                                   f"{role}-{secrets.token_hex(3)}")
        grant = friendly_permissions(can) + (["room.bypass_lobby"] if skip_waiting_room else [])
        ttl_s = parse_minutes(ttl) * 60 if isinstance(ttl, str) else ttl
        token = self.create_token(room_id, uid, role, name=name, ttl=ttl_s, tenant=tenant,
                                  grant=grant, revoke=friendly_permissions(cannot),
                                  meta={"email": email} if email else None)
        return self.join_url(room_id, token)

    def host_link(self, room: Any, name: str = "Host", **kw: Any) -> str:
        """Host link (full control, never waits)."""
        return self.link(room, name, "host", **kw)

    def guest_link(self, room: Any, name: Optional[str] = None, **kw: Any) -> str:
        """Participant link (waits in the waiting room unless on the join list)."""
        return self.link(room, name, "participant", **kw)

    # -- scheduling -----------------------------------------------------------------------------
    def hours(self, host_id: str, spec: Union[str, Dict[str, Any]] = "mon-fri 9-17", *,
              timezone: Optional[str] = None, minutes: Union[int, str] = 30,
              title: Optional[str] = None, name: str = "", email: str = "",
              buffer: Union[None, int, str] = None, buffer_before: Union[None, int, str] = None,
              notice: Union[None, int, str] = None, days_ahead: Optional[int] = None,
              every: Union[None, int, str] = None, blackout: Iterable[Any] = (),
              price: Union[None, int, float, str] = None, currency: Optional[str] = None,
              phone: bool = False, tenant: Optional[str] = None) -> BookingPage:
        """Make someone bookable: ``meet.hours("ada", "mon-fri 9-17", timezone="Asia/Kolkata")``.

        ``spec`` examples: ``"mon-fri 9-17"``, ``"mon-fri 9am-12pm, 2pm-6pm; sat 10-13"``.
        ``buffer``/``notice``/``minutes``/``every`` take minutes or '15m', '2h', '1d'.
        ``price`` takes '499 INR', '$19.99' or 19.99 (paid bookings need ``add_payments``).
        """
        from .scheduling.availability import Availability
        amount, cur = parse_price(price if not isinstance(price, float) else f"{price}", currency)
        av = Availability.from_hours(
            host_id, _timezone(timezone), parse_hours(spec),
            duration_minutes=parse_minutes(minutes, 30), title=title or "Meeting",
            host_name=name, host_email=email, buffer_after=parse_minutes(buffer),
            buffer_before=parse_minutes(buffer_before if buffer_before is not None else 0),
            min_notice_minutes=parse_minutes(notice), max_days_ahead=days_ahead or 60,
            step_minutes=parse_minutes(every) or None,
            blackout_dates={d if isinstance(d, date) else date.fromisoformat(str(d)) for d in blackout},
            tenant_id=tenant, price=amount, currency=cur or "USD", collect_phone=bool(phone))
        self._declare("hours", host_id, {"availability": av.to_dict()})
        return BookingPage(self, host_id)

    async def slots(self, host_id: str, *, days: int = 7, start: Any = None,
                    minutes: Union[None, int, str] = None, limit: Optional[int] = None) -> List["Slot"]:
        """Free slots for the next ``days`` days (or from ``start``)."""
        lo = _when(start).date() if start else datetime.now(dt_timezone.utc).date()
        return await self.bookings.find_slots(host_id, lo, lo + timedelta(days=days),
                                              duration_minutes=parse_minutes(minutes) or None, limit=limit)

    async def book(self, host_id: str, when: Any, *, name: str, email: str,
                   minutes: Union[None, int, str] = None, **kw: Any) -> "Booking":
        """Book a slot. ``when`` = a Slot, a datetime or '2026-10-05 10:30' (host's time zone)."""
        av = await self.storage.get_availability(host_id)
        start = _when(when, av.timezone if av else None)
        return await self.bookings.book(host_id, start, attendee_name=name, attendee_email=email,
                                        duration_minutes=parse_minutes(minutes) or None, **kw)

    async def cancel(self, booking_id: Any, reason: Optional[str] = None) -> "Booking":
        return await self.bookings.cancel(getattr(booking_id, "id", booking_id), reason=reason)

    async def reschedule(self, booking_id: Any, when: Any) -> "Booking":
        bid = getattr(booking_id, "id", booking_id)
        booking = await self.bookings.get(bid)
        av = await self.storage.get_availability(booking.host_id)
        return await self.bookings.reschedule(bid, _when(when, av.timezone if av else None))

    # -- roles & branding -----------------------------------------------------------------------
    def role(self, name: str, can: Any = (), cannot: Any = (), *, based_on: Optional[str] = None,
             label: Optional[str] = None, color: Optional[str] = None, badge: Optional[str] = None,
             rank: Optional[int] = None, tenant: Optional[str] = None, **attributes: Any) -> str:
        """Create or change a role in one line; returns the role name for ``meet.link``.

        ``meet.role("teacher", can="moderate, record", badge="🎓", rank=50)``
        ``meet.role("student", cannot="screen, dm", based_on="participant")``
        ``meet.role("viewer", can="chat")`` edits a built-in role; ``meet.delete_role(name)``.
        New roles start from ``participant`` unless ``based_on`` says otherwise (``based_on=""`` =
        start from nothing).
        Extra keyword arguments become role attributes (``auto_mute=True``, ``max_fps=15``...).
        """
        from .permissions import validate_permissions
        validate_permissions(friendly_permissions(can))  # fail fast on typos
        validate_permissions(friendly_permissions(cannot))
        attrs = {k: v for k, v in dict(label=label, color=color, badge=badge, rank=rank, **attributes).items()
                 if v is not None}
        role_name = str(name).strip().lower()
        self._declare("role", role_name, {"name": role_name, "can": friendly_permissions(can),
                                          "cannot": friendly_permissions(cannot),
                                          "based_on": based_on,
                                          "attributes": attrs, "tenant": tenant})
        return role_name

    async def _apply_role(self, spec: Dict[str, Any]) -> None:
        """Declared role = (its base role as shipped/stored) + can - cannot + attributes."""
        from .permissions import RoleDefinition, builtin_roles, validate_permissions
        name, tenant, shipped = spec["name"], spec.get("tenant"), builtin_roles()
        based_on = spec.get("based_on")
        if based_on is None:
            based_on = name if name in shipped else "participant"
        if not based_on:
            base = None
        elif based_on == name:
            base = shipped[name]
        else:
            base = await self.roles.get(based_on, tenant)
            if base is None:
                raise ValueError(f"role {name!r} is based on unknown role {based_on!r}")
        perms = set(base.permissions) if base else set()
        perms = (perms | validate_permissions(spec["can"])) - validate_permissions(spec["cannot"])
        attrs = {k: v for k, v in (base.attributes if base else {}).items()
                 if based_on == name or k not in ("label", "description")}
        await self.roles.save(RoleDefinition(name=name, permissions=perms,
                                             attributes={**attrs, **spec["attributes"]}, tenant_id=tenant))

    async def delete_role(self, name: str, tenant: Optional[str] = None) -> None:
        """Delete a custom role, or hide a built-in one."""
        self._declared.pop(f"role:{str(name).lower()}", None)
        await self.roles.delete(name, tenant_id=tenant)

    def brand(self, **options: Any) -> Any:
        """Brand every page and email: ``meet.brand(name="Acme", color="#ff5a00", logo=URL)``.

        Shortcuts: color, logo, favicon, font (a Google Font name or CSS URL), theme ('dark' /
        'light'), css, title, white_label=True, and any feature name set to False to hide it
        (whiteboard=False, polls=False...). Any key of ``nodemeet.branding.DEFAULTS`` works too.
        """
        from .branding import deep_merge, validate
        self.branding.base = deep_merge(self.branding.base, validate(brand_settings(**options)))
        return self

    def on_event(self, *patterns: str) -> Any:
        """In-process subscriber to the same events your webhooks get.

        ``@meet.on_event("meeting.ended", "booking.*")`` -> ``async def f(event, data)`` (or ``f(data)``).
        """
        import fnmatch
        import inspect
        pats = patterns or ("*",)

        def deco(fn: Any) -> Any:
            two = len(inspect.signature(fn).parameters) >= 2

            async def tap(event: str, data: Dict[str, Any], tenant_id: Optional[str]) -> None:
                if any(p == event or fnmatch.fnmatchcase(event, p) for p in pats):
                    out = fn(event, data) if two else fn(data)
                    if inspect.isawaitable(out):
                        await out
            self.webhooks.taps.append(tap)
            return fn
        return deco

    # -- integrations (all optional, all one line) ------------------------------------------------
    def add_email(self, url: Any) -> Any:
        """``meet.add_email("smtp://user:pass@smtp.host:587?from=me@x.com")`` or "console"."""
        self.bookings.mailer = mailer_from_url(url)
        self._integrations.append("email")
        return self.bookings.mailer

    def add_webhook(self, url: str, secret: Optional[str] = None, events: Optional[Iterable[str]] = None) -> Any:
        secret = secret or os.environ.get("NODEMEET_WEBHOOK_SECRET")
        if not secret:
            raise ValueError("add_webhook needs a signing secret (argument or NODEMEET_WEBHOOK_SECRET)")
        self._integrations.append("webhooks")
        return self.webhooks.add(url, secret, events)

    def add_calendars(self, *, google: Any = None, outlook: Any = None, caldav: bool = False,
                      **options: Any) -> Any:
        """Two-way calendar sync. ``google=(client_id, client_secret)`` or ``google=True`` to read
        GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET; same for outlook (MICROSOFT_CLIENT_ID/SECRET)."""
        from .integrations.calendars import CalDAVCalendar, CalendarService, GoogleCalendar, OutlookCalendar
        self.inbound.configure("google-calendar", channel_token=options.pop("channel_token", None)
                               or os.environ.get("GOOGLE_CHANNEL_TOKEN") or secrets.token_urlsafe(24))
        self.inbound.configure("microsoft", client_state=options.pop("client_state", None)
                               or os.environ.get("MICROSOFT_CLIENT_STATE") or secrets.token_urlsafe(24))
        providers: List[Any] = []
        if google:
            providers.append(GoogleCalendar(*_creds(google, ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"), "google")))
        if outlook:
            providers.append(OutlookCalendar(*_creds(outlook, ("MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET"),
                                                     "outlook")))
        if caldav:
            providers.append(CalDAVCalendar())
        if not providers:
            raise ValueError("add_calendars: pass google=..., outlook=... and/or caldav=True")
        self.calendars = CalendarService(self, providers, **options)
        self._integrations.append("calendars")
        return self.calendars

    def add_payments(self, provider: str = "stripe", *credentials: str, **options: Any) -> Any:
        """Paid bookings. ``meet.add_payments("stripe", SECRET_KEY, WEBHOOK_SECRET)`` or just
        ``meet.add_payments("stripe")`` with STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET set.
        Razorpay: key id, key secret, webhook secret (RAZORPAY_KEY_ID / _KEY_SECRET / _WEBHOOK_SECRET)."""
        from .integrations.payments import PaymentService, RazorpayPayments, StripePayments
        kind = provider.lower()
        service_opts = {k: options.pop(k) for k in ("success_url", "cancel_url", "cancel_on_refund") if k in options}
        if kind == "stripe":
            p: Any = StripePayments(*_creds(list(credentials) or None,
                                            ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"), "stripe"), **options)
        elif kind == "razorpay":
            p = RazorpayPayments(*_creds(list(credentials) or None, ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET",
                                                                    "RAZORPAY_WEBHOOK_SECRET"), "razorpay"), **options)
        elif kind == "paypal":
            c = _creds(list(credentials) or None, ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_WEBHOOK_ID"),
                       "paypal")
            options.setdefault("sandbox", _truthy(os.environ.get("PAYPAL_SANDBOX", "")))
            from .integrations.payments import PayPalPayments
            p = PayPalPayments(*c, **options)
        elif hasattr(provider, "create_checkout"):
            p = provider
        else:
            raise ValueError(f"unknown payment provider {provider!r}." + did_you_mean(kind, ["stripe", "razorpay", "paypal"]))
        self.payments = PaymentService(self, p, **service_opts)
        self._integrations.append(f"payments:{p.name}")
        return self.payments

    def add_sms(self, provider: str = "twilio", *credentials: str, **options: Any) -> Any:
        """Text confirmations/reminders.

        ``meet.add_sms("twilio", SID, TOKEN, "+15550001111")`` (a 'whatsapp:+1...' sender sends WhatsApp)
        ``meet.add_sms("whatsapp", PHONE_NUMBER_ID, ACCESS_TOKEN, template="booking_update")``
        ``meet.add_sms("webhook", "https://you.com/send-sms")`` for MSG91, Vonage, Plivo, SNS...
        """
        from .integrations import sms
        kind = provider.lower() if isinstance(provider, str) else ""
        self.inbound.configure("whatsapp", app_secret=options.pop("app_secret", None) or os.environ.get("WHATSAPP_APP_SECRET"),
                               verify_token=options.pop("verify_token", None) or os.environ.get("WHATSAPP_VERIFY_TOKEN"))
        if "keywords" in options:
            self.inbound.sms_keywords = bool(options.pop("keywords"))
        if kind == "twilio":
            c = _creds(list(credentials) or None, ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"), "twilio")
            sid, token, sender = (c + [""])[:3]
            if sender.startswith("whatsapp:"):
                options.setdefault("whatsapp_from", sender.split(":", 1)[1])
            elif sender:
                options.setdefault("sms_from", sender)
            n: Any = sms.TwilioNotifier(sid, token, **options)
        elif kind == "whatsapp":
            n = sms.WhatsAppCloudNotifier(*_creds(list(credentials) or None,
                                                  ("WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_ACCESS_TOKEN"), "whatsapp"),
                                          **options)
        elif kind == "webhook":
            n = sms.WebhookNotifier(*_creds(list(credentials) or None, ("NODEMEET_SMS_WEBHOOK",), "sms webhook"),
                                    **options)
        elif kind == "memory":
            n = sms.MemoryNotifier(**options)
        elif hasattr(provider, "send"):
            n = provider
        else:
            raise ValueError(f"unknown SMS provider {provider!r}." +
                             did_you_mean(kind, ["twilio", "whatsapp", "webhook", "memory"]))
        if kind == "twilio":  # delivery receipts come back to /api/inbound/twilio
            n.status_callback = lambda: f"{self.base_url}/api/inbound/twilio"
        self.bookings.notifier = n
        self._integrations.append(f"sms:{kind or type(n).__name__}")
        return n

    def add_sso(self, *, google: Any = None, microsoft: Any = None, github: Any = None, okta: Any = None,
                auth0: Any = None, keycloak: Any = None, oidc: Optional[Dict[str, Any]] = None,
                saml: Optional[Dict[str, Any]] = None, admins: Iterable[str] = (),
                domains: Iterable[str] = (), **options: Any) -> Any:
        """Single sign-on. ``meet.add_sso(google=(ID, SECRET), admins=["me@acme.com"], domains=["acme.com"])``.
        okta/auth0 = (domain, id, secret); keycloak = (base_url, realm, id, secret); ``True`` reads env
        vars like GOOGLE_CLIENT_ID / GITHUB_CLIENT_ID / OKTA_DOMAIN."""
        from .sso import GitHubProvider, OIDCProvider, SAMLProvider, SSOService
        ps: List[Any] = []
        if google:
            ps.append(OIDCProvider.google(*_creds(google, ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"), "google sso")))
        if microsoft:
            ps.append(OIDCProvider.microsoft(*_creds(microsoft, ("MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET"),
                                                     "microsoft sso")))
        if github:
            ps.append(GitHubProvider(*_creds(github, ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"), "github sso")))
        if okta:
            ps.append(OIDCProvider.okta(*_creds(okta, ("OKTA_DOMAIN", "OKTA_CLIENT_ID", "OKTA_CLIENT_SECRET"), "okta")))
        if auth0:
            ps.append(OIDCProvider.auth0(*_creds(auth0, ("AUTH0_DOMAIN", "AUTH0_CLIENT_ID", "AUTH0_CLIENT_SECRET"),
                                                 "auth0")))
        if keycloak:
            ps.append(OIDCProvider.keycloak(*_creds(keycloak, ("KEYCLOAK_URL", "KEYCLOAK_REALM", "KEYCLOAK_CLIENT_ID",
                                                               "KEYCLOAK_CLIENT_SECRET"), "keycloak")))
        if oidc:
            ps.append(OIDCProvider(**oidc))
        if saml:
            ps.append(SAMLProvider(saml.pop("name", "saml"), saml))
        if not ps:
            raise ValueError("add_sso: pass at least one of google, microsoft, github, okta, auth0, keycloak, oidc, saml")
        service = SSOService(self, ps, admin_emails=admins, allowed_domains=domains, **options)
        self._integrations.append("sso:" + ",".join(p.name for p in ps))
        return service

    def add_chat(self, app: str, *credentials: str, events: Optional[Iterable[str]] = None,
                 **options: Any) -> Any:
        """Post bookings, blocked rejoin attempts, recordings... to team chat. Call once per channel.

        ``meet.add_chat("slack", WEBHOOK_URL)``, ``("discord", URL)``, ``("teams", URL)``,
        ``("google_chat", URL)``, ``("telegram", BOT_TOKEN, CHAT_ID)``, ``("mattermost", URL)``,
        ``("webhook", URL)``. Without credentials it reads SLACK_WEBHOOK_URL, DISCORD_WEBHOOK_URL,
        TEAMS_WEBHOOK_URL, GOOGLE_CHAT_WEBHOOK_URL or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID.
        ``events`` takes patterns: ``["booking.*", "participant.waiting"]``.
        """
        from .integrations.chat import DEFAULT_EVENTS, ChatService, make_channel
        key = app.lower().replace("-", "_")
        env = {"slack": ("SLACK_WEBHOOK_URL",), "mattermost": ("MATTERMOST_WEBHOOK_URL",),
               "rocketchat": ("ROCKETCHAT_WEBHOOK_URL",), "discord": ("DISCORD_WEBHOOK_URL",),
               "teams": ("TEAMS_WEBHOOK_URL",), "google_chat": ("GOOGLE_CHAT_WEBHOOK_URL",),
               "telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"), "webhook": ("NODEMEET_CHAT_WEBHOOK",)}
        svc_opts = {k: options.pop(k) for k in ("tenant_id", "timezone", "format") if k in options}
        creds = list(credentials)
        if not creds and not (key == "slack" and options.get("token")):
            creds = _creds(None, env.get(key, ()), app) if key in env else []
        service = ChatService(self, make_channel(app, *creds, **options), events=events or DEFAULT_EVENTS, **svc_opts)
        self._integrations.append(f"chat:{key}")
        return service

    def add_crm(self, app: str, *credentials: str, **options: Any) -> Any:
        """Sync attendees + meetings into a CRM on every booking.

        ``meet.add_crm("hubspot", TOKEN)`` · ``("salesforce", CLIENT_ID, CLIENT_SECRET, domain="acme.my.salesforce.com")``
        · ``("pipedrive", API_TOKEN)`` · ``("zoho", CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, dc="in")``.
        Without credentials: HUBSPOT_TOKEN, SALESFORCE_CLIENT_ID/_CLIENT_SECRET/_DOMAIN,
        PIPEDRIVE_API_TOKEN, ZOHO_CLIENT_ID/_CLIENT_SECRET/_REFRESH_TOKEN (+ ZOHO_DC).
        """
        from .integrations.crm import CRMService, make_crm
        key = app.lower()
        env = os.environ.get
        if key == "hubspot":
            self.inbound.configure("hubspot", client_secret=options.pop("client_secret", None) or env("HUBSPOT_CLIENT_SECRET"))
        elif key == "pipedrive":
            self.inbound.configure("pipedrive", user=options.pop("webhook_user", None) or env("PIPEDRIVE_WEBHOOK_USER"),
                                   password=options.pop("webhook_password", None) or env("PIPEDRIVE_WEBHOOK_PASSWORD"))
        elif key == "salesforce":
            self.inbound.configure("salesforce", org_id=options.pop("org_id", None) or env("SALESFORCE_ORG_ID"))
        elif key == "zoho":
            self.inbound.configure("zoho", token=options.pop("webhook_token", None) or env("ZOHO_WEBHOOK_TOKEN"))
        creds = list(credentials)
        if not creds:
            if key == "hubspot":
                creds = _creds(None, ("HUBSPOT_TOKEN",), "hubspot")
            elif key == "salesforce":
                creds = _creds(None, ("SALESFORCE_CLIENT_ID", "SALESFORCE_CLIENT_SECRET"), "salesforce")
                options.setdefault("domain", _creds(None, ("SALESFORCE_DOMAIN",), "salesforce")[0])
            elif key == "pipedrive":
                creds = _creds(None, ("PIPEDRIVE_API_TOKEN",), "pipedrive")
            elif key == "zoho":
                creds = _creds(None, ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN"), "zoho")
                options.setdefault("dc", os.environ.get("ZOHO_DC", "com"))
        service = CRMService(self, make_crm(app, *creds, **options))
        self._integrations.append(f"crm:{key}")
        return service

    def add_push(self, app: str = "webpush", *credentials: str, **options: Any) -> Any:
        """Push notifications to phones and browsers.

        ``meet.add_push()`` = Web Push (VAPID keys are generated and stored for you, or set
        VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY) · ``("fcm", "service-account.json")`` ·
        ``("onesignal", APP_ID, API_KEY)`` · ``("ntfy", "https://ntfy.sh", topic_prefix="acme-")``.
        Then ``await meet.push.notify(user_id, title, body, url=...)``; in the browser
        ``NodeMeet.push.enable({ token })``.
        """
        from .integrations import push as P
        key = app.lower().replace("-", "").replace("_", "")
        events = options.pop("events", None)
        creds = list(credentials)
        if key in ("webpush", "web", "vapid"):
            priv = creds[0] if creds else os.environ.get("VAPID_PRIVATE_KEY")
            pub = creds[1] if len(creds) > 1 else os.environ.get("VAPID_PUBLIC_KEY")
            options.setdefault("subject", os.environ.get("VAPID_SUBJECT") or
                               f"mailto:{self.bookings.organizer_email or 'admin@localhost'}")
            need("cryptography", "push", "Web Push")
            provider: Any = P.WebPush(priv, pub, **options)
        elif key in ("fcm", "firebase"):
            provider = P.FCMPush(creds[0] if creds else _creds(None, ("FCM_SERVICE_ACCOUNT",), "fcm")[0], **options)
        elif key == "onesignal":
            provider = P.OneSignalPush(*(creds or _creds(None, ("ONESIGNAL_APP_ID", "ONESIGNAL_API_KEY"), "onesignal")),
                                       **options)
        elif key == "ntfy":
            provider = P.NtfyPush(*(creds or [os.environ.get("NTFY_SERVER", "https://ntfy.sh")]), **options)
        elif hasattr(app, "send"):
            provider = app
        else:
            raise ValueError(f"unknown push service {app!r}." + did_you_mean(key, ["webpush", "fcm", "onesignal", "ntfy"]))
        service = P.PushService(self, provider, **({"events": events} if events else {}))
        self._integrations.append(f"push:{provider.name}")
        return service

    def add_conferencing(self, app: str, *credentials: str, mode: str = "replace", **options: Any) -> Any:
        """Hand booked meetings to Zoom, Teams, Google Meet, Webex or Jitsi.

        ``("zoom", ACCOUNT_ID, CLIENT_ID, CLIENT_SECRET)`` · ``("teams", TENANT_ID, CLIENT_ID, CLIENT_SECRET,
        organizer="host@acme.com")`` · ``("google_meet", CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN)`` ·
        ``("webex", ACCESS_TOKEN)`` · ``("jitsi")``. Env vars: ZOOM_ACCOUNT_ID/_CLIENT_ID/_CLIENT_SECRET,
        TEAMS_TENANT_ID/_CLIENT_ID/_CLIENT_SECRET, GOOGLE_MEET_CLIENT_ID/_CLIENT_SECRET/_REFRESH_TOKEN, WEBEX_TOKEN.
        ``mode="also"`` keeps the nodemeet room as the main link.
        """
        from .integrations.conferencing import ConferencingService, make_conferencing
        key = app.lower().replace("-", "_")
        hook_secret = options.pop("webhook_secret", None) or os.environ.get(f"{key.upper()}_WEBHOOK_SECRET")
        client_state = options.pop("client_state", None) or os.environ.get("MICROSOFT_CLIENT_STATE")
        if key in ("zoom", "webex"):
            self.inbound.configure(key, webhook_secret=hook_secret)
        if key in ("teams", "msteams"):
            self.inbound.configure("microsoft", client_state=client_state)
        env = {"zoom": ("ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET"),
               "teams": ("TEAMS_TENANT_ID", "TEAMS_CLIENT_ID", "TEAMS_CLIENT_SECRET"),
               "google_meet": ("GOOGLE_MEET_CLIENT_ID", "GOOGLE_MEET_CLIENT_SECRET", "GOOGLE_MEET_REFRESH_TOKEN"),
               "webex": ("WEBEX_TOKEN",)}
        creds = list(credentials) or (_creds(None, env[key], app) if key in env else [])
        service = ConferencingService(self, make_conferencing(app, *creds, **options), mode=mode)
        self._integrations.append(f"conferencing:{key}")
        return service

    def add_bot(self, app: str, secret: Optional[str] = None, *, allow: Iterable[str] = ()) -> str:
        """``/meet`` commands from chat apps. Returns the URL to paste into the app's settings.

        ``meet.add_bot("slack", SIGNING_SECRET)`` -> Slash command Request URL
        ``meet.add_bot("telegram", SECRET_TOKEN)`` -> setWebhook(url, secret_token=...)
        ``meet.add_bot("discord", PUBLIC_KEY)``   -> Interactions Endpoint URL
        Env: SLACK_SIGNING_SECRET, TELEGRAM_WEBHOOK_SECRET, DISCORD_PUBLIC_KEY. ``allow`` = user names/ids
        allowed to use it (default: everyone in that workspace/chat). Commands: ``/meet [room]``,
        ``/meet book <host>``, ``/meet today [host]``, ``/meet help``.
        """
        key = app.lower()
        field, env = {"slack": ("signing_secret", "SLACK_SIGNING_SECRET"),
                      "telegram": ("secret_token", "TELEGRAM_WEBHOOK_SECRET"),
                      "discord": ("public_key", "DISCORD_PUBLIC_KEY")}.get(key, (None, None))
        if field is None:
            raise ValueError(f"unknown bot app {app!r}." + did_you_mean(key, ["slack", "telegram", "discord"]))
        if key == "discord":
            need("cryptography", "push", "Discord interactions (Ed25519)")
        value = secret or _creds(None, (env,), f"{app} bot")[0]
        self.inbound.configure(key, **{field: value})
        if allow:
            self.inbound.bot_allow[key] = list(allow)
        self._integrations.append(f"bot:{key}")
        return f"{self.base_url}/api/inbound/{key}"

    def add_lti(self, *platforms: Any, moodle: Any = None, canvas: Any = None, blackboard: Any = None,
                brightspace: Any = None, schoology: Any = None, **options: Any) -> Any:
        """Connect LMSs over LTI 1.3 (permanent; works in Moodle, Canvas, Blackboard, Brightspace,
        Schoology, Sakai, Open edX...). ``meet.add_lti(moodle=(URL, CLIENT_ID, DEPLOYMENT_ID))``,
        ``canvas=(CLIENT_ID, DEPLOYMENT_ID)``, ``brightspace=(HOST, CLIENT_ID)``, or no platform at all
        and hand the LMS admin ``lti.registration_url()``. Options: role_map, grade_attendance,
        attendance_minutes, join_list, registration ('invite' / 'open' / None), key (PEM or path),
        grades=False (no gradebook at all), roster=False (no roster access)."""
        from .lti import LTIPlatform, LTIService
        ps: List[Any] = []
        for p in platforms:
            if isinstance(p, dict) and p.get("preset"):
                d = dict(p)
                ps.append(getattr(LTIPlatform, d.pop("preset"))(*d.pop("args", []), **d))
            else:
                ps.append(p if isinstance(p, LTIPlatform) else LTIPlatform.from_dict(p))
        for name, args in (("moodle", moodle), ("canvas", canvas), ("blackboard", blackboard),
                           ("brightspace", brightspace), ("schoology", schoology)):
            if args:
                args = args if isinstance(args, (list, tuple)) else (args,)
                ps.append(getattr(LTIPlatform, name)(*args))
        service = LTIService(self, ps, **options)
        self._integrations.append("lti" + (":" + ",".join(p.name for p in ps) if ps else ""))
        return service

    def add_recordings(self, where: Any = "recordings") -> Any:
        """Where recordings go: a folder (default 'recordings') or 's3://bucket/prefix'."""
        self.recordings.store = recording_store_from_url(where)
        self._integrations.append("recordings")
        return self.recordings.store

    def add_captions(self, model: Any = "base", **options: Any) -> Any:
        """Live captions + transcripts with local Whisper (``pip install "nodemeet[captions]"``),
        or pass your own transcriber object."""
        if isinstance(model, str):
            need("faster_whisper", "captions", "Live captions")
            from .captions import WhisperTranscriber
            model = WhisperTranscriber(model, **options)
        self.transcriber = self.captions.transcriber = model
        self._integrations.append("captions")
        return model

    # -- configuration from the environment / a file ----------------------------------------------
    @classmethod
    def from_env(cls, *, dotenv: Union[None, str, bool] = ".env", **overrides: Any) -> Any:
        """Configure everything from environment variables (and a ``.env`` file if present).

        NODEMEET_SECRET, NODEMEET_BASE_URL, NODEMEET_DB, NODEMEET_EMAIL, NODEMEET_REDIS_URL,
        NODEMEET_API_KEY, NODEMEET_WAITING_ROOM (true/false), NODEMEET_TITLE,
        NODEMEET_WEBHOOKS (comma list) + NODEMEET_WEBHOOK_SECRET, NODEMEET_RECORDINGS,
        NODEMEET_PAYMENTS=stripe|razorpay, NODEMEET_CALENDARS=google,outlook,caldav,
        NODEMEET_SSO=google,github,..., NODEMEET_SMS=twilio|whatsapp|webhook
        (each reads its provider's usual env vars, e.g. STRIPE_SECRET_KEY).
        """
        if dotenv:
            load_dotenv(dotenv if isinstance(dotenv, str) else ".env")
        env = os.environ
        kw: Dict[str, Any] = {}
        for key, arg in (("NODEMEET_SECRET", "secret"), ("NODEMEET_BASE_URL", "base_url"),
                         ("NODEMEET_DB", "db"), ("NODEMEET_EMAIL", "email"), ("NODEMEET_REDIS_URL", "redis"),
                         ("NODEMEET_API_KEY", "api_key"), ("NODEMEET_TITLE", "title"),
                         ("NODEMEET_RECORDINGS", "recordings"), ("NODEMEET_WEBHOOK_SECRET", "webhook_secret")):
            if env.get(key):
                kw[arg] = env[key]
        if env.get("NODEMEET_WAITING_ROOM"):
            kw["waiting_room"] = _truthy(env["NODEMEET_WAITING_ROOM"])
        if env.get("NODEMEET_WEBHOOKS"):
            kw["webhooks"] = [u.strip() for u in env["NODEMEET_WEBHOOKS"].split(",") if u.strip()]
        if env.get("NODEMEET_CORS"):
            kw["cors_origins"] = [u.strip() for u in env["NODEMEET_CORS"].split(",") if u.strip()]
        kw.update(overrides)
        meet = cls(**kw)
        if env.get("NODEMEET_PAYMENTS"):
            meet.add_payments(env["NODEMEET_PAYMENTS"].strip())
        if env.get("NODEMEET_CALENDARS"):
            names = {n.strip().lower() for n in env["NODEMEET_CALENDARS"].split(",")}
            meet.add_calendars(google=True if "google" in names else None,
                               outlook=True if names & {"outlook", "microsoft"} else None, caldav="caldav" in names)
        if env.get("NODEMEET_SSO"):
            names = {n.strip().lower() for n in env["NODEMEET_SSO"].split(",")}
            admins = [a.strip() for a in env.get("NODEMEET_SSO_ADMINS", "").split(",") if a.strip()]
            domains = [d.strip() for d in env.get("NODEMEET_SSO_DOMAINS", "").split(",") if d.strip()]
            meet.add_sso(admins=admins, domains=domains, **{n: True for n in names})
        if env.get("NODEMEET_SMS"):
            meet.add_sms(env["NODEMEET_SMS"].strip())
        for app in [a.strip() for a in env.get("NODEMEET_CHAT", "").split(",") if a.strip()]:
            meet.add_chat(app)
        if env.get("NODEMEET_CRM"):
            meet.add_crm(env["NODEMEET_CRM"].strip())
        if env.get("NODEMEET_PUSH"):
            meet.add_push(env["NODEMEET_PUSH"].strip())
        if env.get("NODEMEET_CONFERENCING"):
            meet.add_conferencing(env["NODEMEET_CONFERENCING"].strip())
        for app in [a.strip() for a in env.get("NODEMEET_BOTS", "").split(",") if a.strip()]:
            meet.add_bot(app)
        return meet

    @classmethod
    def from_config(cls, path: Union[str, Path, Dict[str, Any]] = "nodemeet.toml", **overrides: Any) -> Any:
        """Build from a TOML / JSON file (or a dict). ``${VAR}`` and ``${VAR:-default}`` are
        replaced from the environment, so secrets stay out of the file. See ``nodemeet init``."""
        data = load_config(path) if not isinstance(path, dict) else _expand_env(path)
        return cls.from_dict({**data, **overrides})

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Any:
        data = dict(data)
        sections = {k: data.pop(k) for k in CONFIG_SECTIONS if k in data}
        import inspect
        params = set(inspect.signature(cls.__init__).parameters) - {"self"}
        unknown = [k for k in data if k not in params]
        if unknown:
            k = unknown[0]
            raise TypeError(f"unknown setting {k!r} in config." + did_you_mean(k, list(params) + list(CONFIG_SECTIONS)))
        meet = cls(**data)
        if sections.get("brand") or sections.get("branding"):
            meet.brand(**{**sections.get("branding", {}), **sections.get("brand", {})})
        for name, spec in (sections.get("roles") or {}).items():
            meet.role(name, **dict(spec))
        for rid, spec in (sections.get("rooms") or {}).items():
            meet.room(rid, **dict(spec))
        for host, spec in (sections.get("hours") or {}).items():
            spec = dict(spec) if isinstance(spec, dict) else {"spec": spec}
            meet.hours(host, spec.pop("spec", spec.pop("when", "mon-fri 9-17")), **spec)
        if sections.get("email"):
            e = sections["email"]
            meet.add_email(e if isinstance(e, str) else e.get("url"))
        wh = sections.get("webhooks")
        if wh:
            for url in (wh.get("urls") or [] if isinstance(wh, dict) else wh):
                meet.add_webhook(url, wh.get("secret") if isinstance(wh, dict) else None)
        if sections.get("payments"):
            p = dict(sections["payments"])
            provider = p.pop("provider", "stripe")
            meet.add_payments(provider, *p.pop("credentials", []), **p)
        if sections.get("calendars"):
            meet.add_calendars(**dict(sections["calendars"]))
        if sections.get("sso"):
            meet.add_sso(**dict(sections["sso"]))
        if sections.get("lti"):
            lti = dict(sections["lti"])
            meet.add_lti(*lti.pop("platforms", []), **lti)
        if sections.get("sms"):
            s = dict(sections["sms"])
            meet.add_sms(s.pop("provider", "twilio"), *s.pop("credentials", []), **s)
        chat = sections.get("chat")
        if chat:
            for item in (chat if isinstance(chat, list) else [chat]):
                item = dict(item)
                meet.add_chat(item.pop("app"), *item.pop("credentials", []), **item)
        for name, fn in (("crm", meet.add_crm), ("push", meet.add_push), ("conferencing", meet.add_conferencing)):
            if sections.get(name):
                item = sections[name]
                item = {"app": item} if isinstance(item, str) else dict(item)
                fn(item.pop("app", "webpush"), *item.pop("credentials", []), **item)
        if sections.get("recordings"):
            r = sections["recordings"]
            meet.add_recordings(r if isinstance(r, str) else r.get("where", "recordings"))
        if sections.get("captions"):
            c = sections["captions"]
            meet.add_captions(**(c if isinstance(c, dict) else {}))
        return meet

    # -- discoverability ----------------------------------------------------------------------
    def help(self) -> None:
        """Print the cheat sheet."""
        print(CHEATSHEET)

    def __repr__(self) -> str:
        bits = [f"storage={type(self.storage).__name__}",
                f"email={type(self.bookings.mailer).__name__ if self.bookings.mailer else 'off'}",
                f"waiting_room={'on' if self.waiting_room else 'off'}",
                f"cluster={'redis' if self.bus else 'off'}", f"sfu={'on' if self.sfu.available else 'off'}"]
        if self._integrations:
            bits.append("integrations=" + ",".join(self._integrations))
        declared = [k for k in self._declared]
        if declared:
            bits.append(f"declared={len(declared)}")
        return f"<NodeMeet {self.base_url} " + " ".join(bits) + ">"


CONFIG_SECTIONS = ("brand", "branding", "roles", "rooms", "hours", "email", "webhooks", "payments",
                   "calendars", "sso", "lti", "sms", "recordings", "captions", "chat", "crm", "push", "conferencing")


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on", "y")


def load_dotenv(path: str = ".env", override: bool = False) -> Dict[str, str]:
    """Tiny .env reader (KEY=VALUE, # comments, optional quotes). No dependency."""
    out: Dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return out
    for line in p.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.removeprefix("export ").partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
        if override or k not in os.environ:
            os.environ[k] = v
    return out


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(obj: Any) -> Any:
    if isinstance(obj, str):
        def sub(m: "re.Match[str]") -> str:
            val = os.environ.get(m.group(1))
            if val is None:
                if m.group(2) is None:
                    raise ValueError(f"config uses ${{{m.group(1)}}} but that environment variable is not set")
                return m.group(2)
            return val
        return _ENV_REF.sub(sub, obj)
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    return obj


def load_config(path: Union[str, Path]) -> Dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"config file {str(p)!r} not found. Create one with:  nodemeet init")
    load_dotenv(str(p.parent / ".env"))
    text = p.read_text("utf-8")
    if p.suffix.lower() == ".json":
        data = json.loads(text)
    elif p.suffix.lower() in (".yaml", ".yml"):
        data = need("yaml", "yaml", "YAML config").safe_load(text)
    else:
        try:
            import tomllib
        except ImportError:  # Python 3.10
            tomllib = need("tomli", "toml", "TOML config on Python 3.10")
        data = tomllib.loads(text)
    return _expand_env(data or {})


def quickstart(config: Union[None, str, Dict[str, Any]] = None, *, host: str = "127.0.0.1",
               port: int = 8080, room: str = "demo", open_browser: bool = False, **settings: Any) -> None:
    """One call: build from nodemeet.toml (if present) or env vars, print links, serve.

    >>> import nodemeet; nodemeet.quickstart()
    """
    from .server import NodeMeet
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings.setdefault("base_url", os.environ.get("NODEMEET_BASE_URL") or f"http://localhost:{port}")
    if not os.environ.get("NODEMEET_EMAIL"):
        settings.setdefault("email", "console")  # booking emails show up in the terminal
    if config is None and Path("nodemeet.toml").is_file():
        config = "nodemeet.toml"
    meet = NodeMeet.from_config(config, **settings) if config else NodeMeet.from_env(**settings)
    r = meet.room(room) if f"room:{room}" not in meet._declared else RoomHandle(meet, room)
    host_url, guest_url = r.host_link("Host", ttl="1d"), r.guest_link("Guest", ttl="1d")
    print(f"\n  nodemeet is running on {meet.base_url}\n")
    print(f"  host link : {host_url}")
    print(f"  guest link: {guest_url}  (waits until the host lets them in)\n")
    for key, (kind, spec) in meet._declared.items():
        if kind == "hours":
            print(f"  booking page: {meet.booking_page_url(spec['availability']['host_id'])}")
    if open_browser:
        import webbrowser
        webbrowser.open(host_url)
    meet.run(host=host, port=port)


CHEATSHEET = """\
nodemeet cheat sheet - everything is meet.<something>(...)

  meet = NodeMeet()                          # works as-is; add db=, email=, secret= when ready
  meet = NodeMeet(db="meet.db", email="console", secret="long-random")
  meet = NodeMeet.from_env()                 # or NodeMeet.from_config("nodemeet.toml")

ROOMS & LINKS
  room = meet.room("standup")                # presets: open, webinar, classroom, interview, 1on1...
  room.host_link("Ada")    room.guest_link("Bob")    meet.link("standup", "Cy", "viewer")
  meet.room("class", join_list=["a@x.com"])  # they skip the waiting room (on by default)
  meet.link("standup", "Dee", skip_waiting_room=True, ttl="2h", can="screen", cannot="chat")

BOOKING
  page = meet.hours("ada", "mon-fri 9-17", timezone="Asia/Kolkata", minutes=30, buffer=10)
  page.url                                   # share it, or page.embed() in your HTML
  await meet.slots("ada", days=7)   await meet.book("ada", "2026-10-05 10:30", name=.., email=..)

ROLES, PERMISSIONS & BRANDING
  meet.role("teacher", can="moderate, record", badge="T", rank=50)
  meet.role("student", cannot="screen, dm")  meet.role("viewer", can="chat")   # edit built-ins
  meet.brand(name="Acme", color="#ff5a00", logo="https://..", font="Inter", whiteboard=False)

EVENTS
  @meet.on("join")  @meet.on("leave")  @meet.on("chat")  @meet.on("booked")  @meet.on("blocked")

ADD-ONS (one line each)
  meet.add_email("smtp://user:pass@smtp.host:587")      meet.add_webhook(url, secret)
  meet.add_payments("stripe")   meet.add_calendars(google=True)   meet.add_sso(github=True)
  meet.add_lti(moodle=(URL, CLIENT_ID, DEPLOYMENT_ID))   # LMS: Moodle/Canvas/Blackboard/Brightspace
  meet.add_sms("twilio")        meet.add_recordings("s3://bucket/rec")   meet.add_captions()
  meet.add_chat("slack", URL)   meet.add_crm("hubspot", TOKEN)   meet.add_push()   meet.add_payments("paypal")
  meet.add_conferencing("zoom")  # or teams, google_meet, webex, jitsi: bookings get that link
  meet.add_bot("slack", SIGNING_SECRET)   # /meet in Slack, Telegram, Discord
WEBHOOKS
  meet.add_webhook(url, secret, events=["booking.*", "payment.*", "*.failed"])   # 120+ events
  @meet.on_event("meeting.ended")  async def f(data): ...     # in-process, same events
  POST /api/inbound/actions  {"action": "booking.create", "data": {...}}         # Zapier / n8n in

RUN
  meet.run()                                  # standalone
  app.mount("/meet", meet.asgi())             # FastAPI / Starlette / Django
  nodemeet init  |  nodemeet serve  |  nodemeet link standup Ada --host  |  nodemeet doctor
"""
