"""The :class:`NodeMeet` facade: one object that wires everything together.

It is framework-neutral. Adapters:

* ``meet.app()`` / ``meet.mount(aiohttp_app, "/meet")`` -- aiohttp
* ``meet.asgi()`` -- any ASGI host: FastAPI/Starlette ``app.mount("/meet", meet.asgi())``,
  Django (``nodemeet.asgi.route``), Quart, Litestar, uvicorn, hypercorn...
"""
from __future__ import annotations

import html
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Union
from urllib.parse import quote

from ._version import __version__
from .api import API, cors_headers, map_exception
from .broker import Broker, MemoryBroker, RedisBroker
from .cluster import ClusterBus
from .hooks import Hooks
from ._http import HTTPError, Request, Response, Router, static_response, text_response
from .integrations.email import Mailer
from .integrations.email_templates import EmailTemplates
from .integrations.webhooks import WebhookDispatcher, WebhookEndpoint
from .models import Booking, RoomConfig
from .rooms import RoomManager
from .scheduling.booking import BookingService, BusyProvider
from .scheduling.reminders import ReminderScheduler
from .sfu import MediaBackend, NullBackend, default_backend
from .signaling import WSTransport, moderate, serve_session
from .storage.base import Storage
from .branding import BrandingResolver, head_html, public as public_branding
from .permissions import RoleDefinition, RoleRegistry
from .tenancy import RateLimiter
from .tokens import Role, TokenSigner

log = logging.getLogger("nodemeet")
PKG_DIR = Path(__file__).parent
STATIC_DIR = PKG_DIR / "static"
TEMPLATE_DIR = PKG_DIR / "templates"
DEFAULT_ICE_SERVERS: List[Dict[str, Any]] = [{"urls": "stun:stun.l.google.com:19302"}]
WEBHOOK_CACHE_SECONDS = 5.0


from .quick import EasyMixin  # noqa: E402


class NodeMeet(EasyMixin):
    """Embed video meetings + scheduling in your app, or run standalone.

    Everything has a default, so ``NodeMeet()`` works. The common knobs take plain strings::

        meet = NodeMeet(db="postgres://u:p@host/app", email="smtp://u:p@smtp.host:587")

    >>> meet = NodeMeet(secret="long-random-secret", base_url="https://example.com/meet")
    >>> fastapi_app.mount("/meet", meet.asgi())   # ASGI (FastAPI, Starlette, Django...)
    >>> meet.mount(aiohttp_app, "/meet")          # or aiohttp
    >>> meet.run(port=8080)                        # or standalone
    """

    def __init__(self, secret: Optional[str] = None, *, storage: Union[None, str, Storage] = None,
                 base_url: Optional[str] = None, api_key: Optional[str] = None,
                 mailer: Optional[Mailer] = None, email_templates: Optional[EmailTemplates] = None,
                 webhooks: Optional[WebhookDispatcher] = None,
                 broker: Union[None, str, Broker] = None,
                 sfu: Union[bool, MediaBackend] = True, p2p_max: int = 4,
                 ice_servers: Optional[List[Dict[str, Any]]] = None,
                 cors_origins: Iterable[str] = (), token_ttl: int = 3600,
                 auto_create_rooms: bool = True, chat_history: int = 200,
                 reminders: bool = True, reminder_offsets: Iterable[int] = (1440, 15),
                 reminder_interval: float = 60.0, organizer_email: str = "",
                 organizer_name: str = "", busy_provider: Optional[BusyProvider] = None,
                 serve_ui: bool = True, title: str = "nodemeet", trust_proxy: bool = False,
                 branding: Optional[Dict[str, Any]] = None,
                 roles: Iterable[Union[RoleDefinition, Dict[str, Any]]] = (),
                 default_role: str = "participant", waiting_room: bool = True,
                 recording_store: Optional[Any] = None, transcriber: Optional[Any] = None,
                 ban_scope: Iterable[str] = ("user", "device"),
                 db: Union[None, str, Storage] = None, email: Union[None, str, Mailer] = None,
                 redis: Optional[str] = None, webhook_secret: Optional[str] = None,
                 recordings: Union[None, str, Any] = None, inbound_secret: Optional[str] = None) -> None:
        from .easy import mailer_from_url, recording_store_from_url, storage_from_url
        secret = secret or os.environ.get("NODEMEET_SECRET")
        self.secret_generated = not secret
        if not secret:
            import secrets as _secrets
            secret = _secrets.token_urlsafe(32)
            log.warning("nodemeet: no secret given, using a random one. Links stop working when the "
                        "app restarts - set NODEMEET_SECRET (or NodeMeet(secret=...)) for production.")
        self.tokens = TokenSigner(secret, default_ttl=token_ttl)
        self.hooks = Hooks()
        db = db if db is not None else storage
        if db is None:
            db = os.environ.get("NODEMEET_DB") or os.environ.get("NODEMEET_DB_URL") or None
        self.storage: Storage = storage_from_url(db)
        mailer = mailer_from_url(email if email is not None else
                                 (mailer if mailer is not None else os.environ.get("NODEMEET_EMAIL")))
        broker = broker if broker is not None else (redis or os.environ.get("NODEMEET_REDIS_URL") or None)
        if isinstance(webhooks, (str, list, tuple)):
            urls = [webhooks] if isinstance(webhooks, str) else list(webhooks)
            wh_secret = webhook_secret or os.environ.get("NODEMEET_WEBHOOK_SECRET")
            if urls and not wh_secret:
                raise ValueError("webhooks need a signing secret: NodeMeet(webhooks=[...], webhook_secret='...')")
            webhooks = WebhookDispatcher()
            for u in urls:
                webhooks.add(u, wh_secret)
        recording_store = recording_store_from_url(recordings if recordings is not None else recording_store)
        base_url = base_url or os.environ.get("NODEMEET_BASE_URL")
        self._base_url_fixed = bool(base_url)
        self.base_url = (base_url or "http://localhost:8080").rstrip("/")
        self.trust_proxy = trust_proxy
        self.api_key = api_key or os.environ.get("NODEMEET_API_KEY") or None
        # cluster
        if isinstance(broker, str):
            broker = RedisBroker(broker)
        self.broker: Broker = broker or MemoryBroker()
        self.bus: Optional[ClusterBus] = ClusterBus(self.broker) if self.broker.clustered else None
        self.rate_limiter = RateLimiter(self.broker if self.bus else None)
        # webhooks: static endpoints + per-tenant endpoints stored in the database
        self.webhooks = webhooks or WebhookDispatcher()
        if self.webhooks.resolver is None:
            self.webhooks.resolver = self._stored_webhooks
        self._wh_cache: Dict[Optional[str], Any] = {}
        # media
        self.ice_servers = DEFAULT_ICE_SERVERS if ice_servers is None else list(ice_servers)
        if isinstance(sfu, MediaBackend):
            self.sfu: MediaBackend = sfu
        else:
            self.sfu = default_backend(self.ice_servers) if sfu else NullBackend()
        self.rooms = RoomManager(self.storage, self.hooks, p2p_max=p2p_max,
                                 sfu_available=self.sfu.available, chat_history=chat_history,
                                 auto_create=auto_create_rooms, bus=self.bus,
                                 waiting_room=waiting_room)
        self.waiting_room = waiting_room
        self.ban_scope = tuple(ban_scope)
        from .captions import CaptionService
        from .features import BreakoutManager
        from .recordings import RecordingService
        self.recordings = RecordingService(self, recording_store)
        self.transcriber = transcriber
        self.captions = CaptionService(self, transcriber)
        self.breakouts = BreakoutManager(self)
        self.sso: Any = None  # nodemeet.sso.SSOService when configured
        self.lti: Any = None  # nodemeet.lti.LTIService when configured
        self.push: Any = None  # nodemeet.integrations.push.PushService (add_push)
        self.conferencing: Any = None  # add_conferencing
        self._easy_init()
        if self.bus is not None:
            self.bus.manager = self.rooms
            self.bus.on_control = lambda room, target, action, data: moderate(self, room, target, action, data)
        self.bookings = BookingService(
            self.storage, mailer=mailer, templates=email_templates, webhooks=self.webhooks,
            hooks=self.hooks, join_url=self.booking_join_url, manage_url=self.manage_url,
            busy_provider=busy_provider, organizer_email=organizer_email,
            organizer_name=organizer_name)
        self.reminders = ReminderScheduler(self.bookings, reminder_offsets, reminder_interval)
        self.enable_reminders = reminders
        self.cors_origins = list(cors_origins)
        self.serve_ui = serve_ui
        self.title = title
        self.prefix = ""
        self._started = False
        self._aiohttp_app: Any = None
        self._sessions: Set[Any] = set()
        # roles & branding (stored per tenant; library defaults as the base layer)
        self.roles = RoleRegistry(self.storage, default_role=default_role)
        self._initial_roles = [r if isinstance(r, RoleDefinition) else RoleDefinition.from_dict(r)
                               for r in roles]
        self.branding = BrandingResolver({"name": title, **(branding or {})}, self.storage)
        self.bookings.branding_provider = self._email_branding
        self._route_providers: List[Any] = []  # calendars, payments, SSO... add their routes
        from .event_bridge import EventBridge
        self.events = EventBridge(self)
        self.api = API(self)
        self.router = self._build_router()
        from .inbound import InboundService
        self.inbound = InboundService(self)
        self.inbound.configure("actions", secret=inbound_secret or os.environ.get("NODEMEET_INBOUND_SECRET"))
        self.use(self.inbound)
        from .webhook_api import WebhookTools
        self.webhook_tools = WebhookTools(self)
        self.use(self.webhook_tools)
        self._wire_room_webhooks()

    # -- webhooks ------------------------------------------------------------
    def _wire_room_webhooks(self) -> None:
        async def created(config: RoomConfig) -> None:
            await self.emit_webhook("room.created", config.to_dict(), tenant_id=config.tenant_id)

        async def closed(room: Any) -> None:
            await self.emit_webhook("room.closed", {"room": room.id}, tenant_id=room.config.tenant_id)

        self.hooks.on("on_room_created", created)
        self.hooks.on("on_room_closed", closed)

    async def emit_webhook(self, event: str, data: Dict[str, Any], *,
                           tenant_id: Optional[str] = None) -> None:
        await self.webhooks.emit(event, data, tenant_id=tenant_id)

    async def _stored_webhooks(self, event: str, tenant_id: Optional[str]) -> List[WebhookEndpoint]:
        if not tenant_id:
            return []
        hit = self._wh_cache.get(tenant_id)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        try:
            items = await self.storage.list_webhooks(tenant_id)
        except NotImplementedError:
            items = []
        self._wh_cache[tenant_id] = (time.monotonic() + WEBHOOK_CACHE_SECONDS, items)
        return items

    def invalidate_webhook_cache(self) -> None:
        self._wh_cache.clear()

    # -- hooks (decorators) ----------------------------------------------------
    def on(self, event: str, fn: Optional[Callable[..., Any]] = None) -> Any:
        return self.hooks.on(event, fn)

    def before_join(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        return self.hooks.on("before_join", fn)

    def on_join(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        return self.hooks.on("on_join", fn)

    def on_leave(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        return self.hooks.on("on_leave", fn)

    def on_chat(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        return self.hooks.on("on_chat", fn)

    # -- rooms, tokens & links ---------------------------------------------------
    async def create_room(self, *, room_id: Optional[str] = None, name: str = "",
                          mode: str = "auto", max_participants: Optional[int] = None,
                          metadata: Optional[Dict[str, Any]] = None,
                          tenant_id: Optional[str] = None, waiting_room: Optional[bool] = None,
                          join_list: Iterable[str] = (), chat_enabled: bool = True,
                          locked: bool = False, default_role: Optional[str] = None,
                          role_overrides: Optional[Dict[str, Any]] = None,
                          branding: Optional[Dict[str, Any]] = None) -> RoomConfig:
        """Create (or replace) a room.

        ``waiting_room`` defaults to ``NodeMeet(waiting_room=...)`` (on). People in
        ``join_list`` (user ids or emails) skip it; everyone else waits to be admitted.
        """
        kwargs: Dict[str, Any] = dict(
            name=name, mode=mode, max_participants=max_participants, metadata=dict(metadata or {}),
            tenant_id=tenant_id, lobby=self.waiting_room if waiting_room is None else bool(waiting_room),
            allowed_users=list(join_list), chat_enabled=chat_enabled, locked=locked,
            default_role=default_role, role_overrides=dict(role_overrides or {}),
            branding=dict(branding or {}))
        if room_id:
            kwargs["id"] = room_id
        return await self.rooms.create(RoomConfig(**kwargs))

    def use(self, provider: Any) -> Any:
        """Plug in an integration that adds REST routes (calendars, payments, SSO...)."""
        if provider not in self._route_providers:
            self._route_providers.append(provider)
            provider.register(self.router)
        return provider

    async def _room_config(self, room: str) -> RoomConfig:
        live = self.rooms.get(room)
        cfg = live.config if live else await self.storage.get_room(room)
        if cfg is None:
            cfg = await self.create_room(room_id=room)
        return cfg

    async def add_to_join_list(self, room: str, *users: str) -> List[str]:
        """Let these user ids/emails skip the waiting room of ``room``."""
        cfg = await self._room_config(room)
        for u in users:
            if not cfg.on_join_list(u):
                cfg.allowed_users.append(str(u).strip())
        await self._config_saved(cfg)
        return list(cfg.allowed_users)

    async def remove_from_join_list(self, room: str, *users: str) -> List[str]:
        cfg = await self._room_config(room)
        drop = {str(u).strip().lower() for u in users}
        cfg.allowed_users = [u for u in cfg.allowed_users if u.lower() not in drop]
        await self._config_saved(cfg)
        return list(cfg.allowed_users)

    async def set_waiting_room(self, room: str, enabled: bool) -> None:
        cfg = await self._room_config(room)
        cfg.lobby = bool(enabled)
        await self._config_saved(cfg)

    async def unban(self, room: str, user_id: str) -> None:
        cfg = await self._room_config(room)
        cfg.banned_users = [u for u in cfg.banned_users if u.lower() != str(user_id).lower()]
        info = cfg.banned_info.pop(user_id, None)
        await self._config_saved(cfg)
        await self.emit_webhook("participant.unbanned", {"room": room, "user_id": user_id, "ban": info},
                                tenant_id=cfg.tenant_id)

    async def _config_saved(self, cfg: RoomConfig) -> None:
        await self.rooms.update_config(cfg)
        await self.emit_webhook("room.updated", cfg.to_dict(), tenant_id=cfg.tenant_id)
        live = self.rooms.get(cfg.id)
        if live is not None:
            await live.broadcast({"type": "room-updated", "room": live.snapshot()})

    def create_token(self, room: str, user_id: str, role: Union[str, Role] = Role.PARTICIPANT, *,
                     name: Optional[str] = None, ttl: Optional[int] = None,
                     meta: Optional[Dict[str, Any]] = None, tenant: Optional[str] = None,
                     grant: Iterable[str] = (), revoke: Iterable[str] = ()) -> str:
        """Mint a join token. ``role`` may be any built-in or custom role name;
        ``grant``/``revoke`` adjust permissions for this one person."""
        return self.tokens.create(room, user_id, role, name=name, ttl=ttl, meta=meta, tenant=tenant,
                                  grant=grant, revoke=revoke)

    def join_url(self, room: str, token: str) -> str:
        # The token lives in the URL fragment so it never reaches server logs.
        return f"{self.base_url}/r/{quote(room, safe='')}#token={token}"

    def invite(self, room: str, user_id: str, role: Union[str, Role] = Role.PARTICIPANT, *,
               name: Optional[str] = None, ttl: Optional[int] = None,
               tenant: Optional[str] = None, skip_waiting_room: bool = False) -> str:
        """Shortcut: mint a token and return a ready-to-share join URL.

        ``skip_waiting_room=True`` lets this one link bypass the waiting room."""
        grant = ["room.bypass_lobby"] if skip_waiting_room else []
        return self.join_url(room, self.create_token(room, user_id, role, name=name, ttl=ttl,
                                                     tenant=tenant, grant=grant))

    def booking_join_url(self, booking: Booking, who: str = "attendee") -> str:
        conf = booking.metadata.get("conference") or {}
        if conf.get("join_url") and conf.get("mode", "replace") == "replace" and not conf.get("deleted"):
            return str(conf["join_url"])  # Zoom / Teams / Meet / Webex / Jitsi (add_conferencing)
        return self._nodemeet_join_url(booking, who)

    def _nodemeet_join_url(self, booking: Booking, who: str = "attendee") -> str:
        host = who == "host"
        ttl = max(3600, int(booking.end.timestamp() - time.time()) + 86400)
        token = self.create_token(
            booking.room, booking.host_id if host else booking.attendee_email,
            Role.HOST if host else Role.PARTICIPANT,
            name=None if host else booking.attendee_name, ttl=ttl,
            meta={"booking_id": booking.id}, tenant=booking.tenant_id)
        return self.join_url(booking.room, token)

    def manage_url(self, booking: Booking) -> str:
        return f"{self.base_url}/book/manage/{booking.id}#t={booking.manage_token}"

    def booking_page_url(self, host_id: str) -> str:
        return f"{self.base_url}/book/{quote(host_id, safe='')}"

    # -- embed snippets (for server-side templates) --------------------------------
    def embed_room(self, room: str, token: str, *, width: str = "100%", height: str = "600px") -> str:
        src = html.escape(self.join_url(room, token), quote=True)
        return (f'<iframe src="{src}" style="width:{width};height:{height};border:0;'
                f'border-radius:12px" allow="camera; microphone; display-capture; autoplay; '
                f'fullscreen; clipboard-write" allowfullscreen></iframe>')

    def embed_booking(self, host_id: str, *, inline: bool = True) -> str:
        base = html.escape(self.base_url, quote=True)
        hid = html.escape(host_id, quote=True)
        if not inline:
            return (f'<iframe src="{base}/book/{hid}" style="width:100%;height:640px;border:0">'
                    f'</iframe>')
        return (f'<script src="{base}/static/embed.js" async></script>\n'
                f'<nodemeet-booking base="{base}" host="{hid}"></nodemeet-booking>')

    # -- HTTP (framework-neutral) ------------------------------------------------------
    def _build_router(self) -> Router:
        r = Router()
        self.api.register(r)
        from .extras_api import ExtrasAPI
        ExtrasAPI(self).register(r)
        for extra in self._route_providers:
            extra.register(r)
        if self.serve_ui:
            r.add("GET", "/", self._page_index)
            r.add("GET", "/r/{room_id}", self._page_room)
            r.add("GET", "/book/manage/{booking_id}", self._page_manage)
            r.add("GET", "/book/{host_id}", self._page_book)
        r.add("GET", "/static/{file}", self._static)
        return r

    # -- branding ---------------------------------------------------------------------
    async def resolve_branding(self, *, tenant_id: Optional[str] = None,
                               room: Optional[RoomConfig] = None) -> Dict[str, Any]:
        """Full branding dict for a tenant and/or room (defaults < tenant < room)."""
        return await self.branding.resolve(tenant_id=tenant_id or (room.tenant_id if room else None),
                                           room_branding=room.branding if room else None)

    async def branding_for(self, room: Optional[RoomConfig] = None,
                           tenant_id: Optional[str] = None) -> Dict[str, Any]:
        return public_branding(await self.resolve_branding(tenant_id=tenant_id, room=room))

    async def _email_branding(self, tenant_id: Optional[str]) -> Dict[str, Any]:
        return await self.resolve_branding(tenant_id=tenant_id)

    async def _render(self, req: Request, template: str, config: Dict[str, Any],
                      branding: Dict[str, Any], page: str = "") -> Response:
        text = (TEMPLATE_DIR / template).read_text("utf-8")
        cfg = json.dumps({"base": req.prefix, **config, "branding": public_branding(branding)}
                         ).replace("</", "<\\/")
        title = str(branding.get("page_title") or "{room} · {name}").replace(
            "{room}", page or "").replace("{name}", str(branding.get("name") or self.title)).strip(" ·")
        text = (text.replace("{{TITLE}}", html.escape(title or self.title))
                .replace("{{HEAD}}", head_html(branding))
                .replace("{{THEME}}", html.escape(str(branding.get("theme", "dark"))))
                .replace("{{BASE}}", html.escape(req.prefix, quote=True))
                .replace("{{VERSION}}", __version__).replace("{{CONFIG}}", cfg))
        return text_response(text, "text/html", headers={"Cache-Control": "no-store",
                                                         "Referrer-Policy": "no-referrer"})

    async def _page_room(self, req: Request) -> Response:
        room_id = req.match_info["room_id"]
        config = await self.storage.get_room(room_id)
        branding = await self.resolve_branding(room=config) if config else await self.resolve_branding()
        name = (config.name if config and config.name else room_id)
        page = {"room": room_id}
        if config and config.sso_required:
            page["sso"] = f"{req.prefix}/sso/{config.sso_required}/login?room={room_id}"
        return await self._render(req, "meeting.html", page, branding, name)

    async def _page_book(self, req: Request) -> Response:
        host = req.match_info["host_id"]
        av = await self.storage.get_availability(host)
        branding = await self.resolve_branding(tenant_id=av.tenant_id if av else None)
        return await self._render(req, "booking.html", {"host": host}, branding,
                                  (av.title if av else "") or "Book a time")

    async def _page_manage(self, req: Request) -> Response:
        booking = await self.storage.get_booking(req.match_info["booking_id"])
        branding = await self.resolve_branding(tenant_id=booking.tenant_id if booking else None)
        return await self._render(req, "booking.html", {"manage": req.match_info["booking_id"]},
                                  branding, "Your booking")

    async def _page_index(self, req: Request) -> Response:
        return await self._render(req, "index.html", {}, await self.resolve_branding(), "")

    async def _static(self, req: Request) -> Response:
        return static_response(STATIC_DIR, req.match_info["file"])

    async def handle(self, req: Request) -> Response:
        """Serve one HTTP request (used by every framework adapter)."""
        if not self._started:
            await self.startup()
        if not self._base_url_fixed and req.host:
            self.base_url = req.base_url(self.trust_proxy).rstrip("/")
            self._base_url_fixed = True
            log.info("nodemeet base_url learned from first request: %s", self.base_url)
        cors = cors_headers(req.header("origin") or None, self.cors_origins)
        if req.method == "OPTIONS" and cors:
            return Response(status=204, headers=cors)
        try:
            handler, params = self.router.match(req.method, req.path or "/")
            req.match_info.update(params)
            resp = await handler(req)
        except HTTPError as exc:
            resp = exc.response()
        except Exception as exc:  # noqa: BLE001
            mapped = map_exception(exc)
            if mapped is None:
                log.exception("unhandled error for %s %s", req.method, req.path)
                mapped = HTTPError(500, "server_error", "internal error").response()
            resp = mapped
        resp.headers.update(cors)
        try:
            await self.events.after_http(req, resp)
        except Exception:  # noqa: BLE001
            log.exception("event bridge failed for %s %s", req.method, req.path)
        return resp

    async def handle_websocket(self, ws: WSTransport) -> None:
        """Run a signaling session over any WebSocket adapter."""
        if not self._started:
            await self.startup()
        await serve_session(self, ws)

    # -- lifecycle ----------------------------------------------------------------------
    async def startup(self) -> None:
        """Idempotent. Adapters call it automatically (lifespan or first request)."""
        if self._started:
            return
        self._started = True
        await self.storage.setup()
        for role in self._initial_roles:
            if await self.roles.get(role.name, role.tenant_id) is None:
                await self.roles.save(role)
        await self._easy_apply()
        if self.bus is not None:
            await self.bus.start()
        if self.enable_reminders:
            self.reminders.start()
        log.info("nodemeet %s ready (sfu=%s, node=%s, clustered=%s)", __version__,
                 self.sfu.available, self.broker.node_id, self.bus is not None)

    async def shutdown(self) -> None:
        if not self._started:
            return
        self._started = False
        for session in list(self._sessions):
            try:
                await session.ws.close(1001)
            except Exception:  # noqa: BLE001
                pass
        await self.reminders.stop()
        await self.sfu.close()
        await self.webhooks.close()
        if self.bus is not None:
            await self.bus.close()
        await self.storage.close()

    # -- adapters ---------------------------------------------------------------------------
    def asgi(self) -> Any:
        """An ASGI 3 application (HTTP + WebSocket + lifespan)."""
        from .asgi import ASGIApp
        return ASGIApp(self)

    def app(self) -> Any:
        """The aiohttp application (built once, then cached). Needs ``nodemeet[aiohttp]``."""
        if self._aiohttp_app is None:
            from .aiohttp_app import build_app
            self._aiohttp_app = build_app(self)
        return self._aiohttp_app

    def mount(self, parent: Any, prefix: str = "/meet") -> Any:
        """Mount under ``prefix`` in an existing **aiohttp** app.

        For FastAPI/Starlette use ``app.mount("/meet", meet.asgi())`` instead.
        """
        prefix = "/" + prefix.strip("/")
        self.prefix = prefix
        if self._base_url_fixed and not self.base_url.endswith(prefix):
            log.warning("base_url %s does not end with mount prefix %s", self.base_url, prefix)
        sub = self.app()
        parent.add_subapp(prefix, sub)
        return sub

    def run(self, host: str = "127.0.0.1", port: int = 8080, **kwargs: Any) -> None:
        """Run standalone (blocking): aiohttp if installed, else uvicorn, else the built-in
        zero-dependency server (``server="dev"`` forces it)."""
        server = kwargs.pop("server", "auto")
        if not self._base_url_fixed:
            self.base_url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
        if kwargs.pop("banner", True):
            print(f"nodemeet {__version__} running on http://{host}:{port}  (Ctrl+C to stop)", flush=True)
        web = None
        if server in ("auto", "aiohttp"):
            try:
                from aiohttp import web
            except ImportError:
                web = None
        if web is not None:
            web.run_app(self.app(), host=host, port=port, **kwargs)
            return
        if server in ("auto", "uvicorn"):
            try:
                import uvicorn
                uvicorn.run(self.asgi(), host=host, port=port, log_level="info")
                return
            except ImportError:
                if server == "uvicorn":
                    raise RuntimeError("pip install uvicorn") from None
        from .devserver import run as dev_run
        log.info("using the built-in server (pip install uvicorn for heavy production traffic)")
        dev_run(self.asgi(), host=host, port=port)


def create_app(**kwargs: Any) -> Any:
    """aiohttp factory: ``gunicorn 'nodemeet.server:create_app()' -k aiohttp.GunicornWebWorker``."""
    return NodeMeet(**kwargs).app()


def create_asgi(**kwargs: Any) -> Any:
    """ASGI factory: ``uvicorn --factory nodemeet.server:create_asgi``."""
    return NodeMeet(**kwargs).asgi()
