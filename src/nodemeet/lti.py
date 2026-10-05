"""LTI 1.3 / LTI Advantage: plug nodemeet into Moodle, Canvas, Blackboard, Brightspace,
Schoology, Sakai, Open edX... once, permanently.

    lti = meet.add_lti(moodle=("https://moodle.school.edu", CLIENT_ID, DEPLOYMENT_ID))
    print(lti.registration_url())    # or: paste this into the LMS (dynamic registration)

What you get (all routes live under your nodemeet mount):

* ``/lti/login`` + ``/lti/launch``  OIDC launch. Students arrive signed in, teachers become hosts,
  every course link is its own permanent room (or the room an instructor picked).
* ``/lti/jwks``                     the tool's public keys (the key pair is generated once and saved).
* Deep linking                      instructors click "add activity" -> pick live meeting / booking page.
* ``/lti/register``                 LTI Dynamic Registration (Moodle 4.x, Canvas, Brightspace, Sakai...).
* ``/lti/canvas.json``              paste-in JSON config for a Canvas developer key.
* Names & Roles                     ``await lti.sync_roster(room)`` -> enrolled people skip the waiting room.
* Assignment & Grades               ``await lti.send_grade(room, user, 100)`` or ``grade_attendance=True``.
* Events                            lti.launch, lti.deep_link, lti.registered, lti.roster_synced,
                                    lti.grade_sent, lti.failed (webhooks + ``meet.on_event``).
"""
from __future__ import annotations

import base64
import hashlib
import inspect
import json
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html import escape
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

from ._http import HTTPError, Request, Response, Router, json_response, text_response
from .integrations.http_client import HTTPClient
from .tokens import sign_blob, verify_blob

if TYPE_CHECKING:
    from .server import NodeMeet

log = logging.getLogger("nodemeet.lti")

C = "https://purl.imsglobal.org/spec/lti/claim/"
DL = "https://purl.imsglobal.org/spec/lti-dl/claim/"
NRPS = "https://purl.imsglobal.org/spec/lti-nrps/claim/namesroleservice"
AGS = "https://purl.imsglobal.org/spec/lti-ags/claim/endpoint"
TOOL_CONF = "https://purl.imsglobal.org/spec/lti-tool-configuration"
PLATFORM_CONF = "https://purl.imsglobal.org/spec/lti-platform-configuration"
S_LINEITEM = "https://purl.imsglobal.org/spec/lti-ags/scope/lineitem"
S_SCORE = "https://purl.imsglobal.org/spec/lti-ags/scope/score"
S_RESULT = "https://purl.imsglobal.org/spec/lti-ags/scope/result.readonly"
S_NRPS = "https://purl.imsglobal.org/spec/lti-nrps/scope/contextmembership.readonly"
SCOPES = [S_LINEITEM, S_SCORE, S_RESULT, S_NRPS]

DEFAULT_ROLE_MAP = {  # LMS role (the part after '#' or the last '/') -> nodemeet role
    "Administrator": "host", "Instructor": "host", "TeachingAssistant": "host",
    "ContentDeveloper": "host", "Manager": "host",
    "Learner": "participant", "Student": "participant", "Mentor": "participant",
    "Member": "participant", "Guest": "viewer", "Observer": "viewer",
}
ROLE_RANK = {"host": 3, "participant": 2, "viewer": 1}
STAFF = {"Administrator", "Instructor", "TeachingAssistant", "ContentDeveloper", "Manager"}


class LTIError(HTTPError):
    def __init__(self, code: str, message: str, status: int = 401) -> None:
        super().__init__(status, code, message)


# -- tiny RS256 JWT (cryptography is already a dependency) ---------------------------------------
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _int(s: str) -> int:
    return int.from_bytes(_b64d(s), "big")


def jwk_public_key(jwk: Dict[str, Any]) -> Any:
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
    if jwk.get("kty") != "RSA":
        raise LTIError("bad_key", "only RSA keys are supported")
    return RSAPublicNumbers(_int(jwk["e"]), _int(jwk["n"])).public_key()


def public_jwk(private_key: Any, kid: str) -> Dict[str, str]:
    nums = private_key.public_key().public_numbers()
    size = (nums.n.bit_length() + 7) // 8
    return {"kty": "RSA", "alg": "RS256", "use": "sig", "kid": kid,
            "n": _b64e(nums.n.to_bytes(size, "big")), "e": _b64e(nums.e.to_bytes(3, "big").lstrip(b"\0"))}


def jwt_encode(claims: Dict[str, Any], private_key: Any, kid: str) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    head = _b64e(json.dumps({"alg": "RS256", "typ": "JWT", "kid": kid}, separators=(",", ":")).encode())
    body = _b64e(json.dumps(claims, separators=(",", ":")).encode())
    sig = private_key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64e(sig)}"


def jwt_parts(token: str) -> Tuple[Dict[str, Any], Dict[str, Any], bytes, bytes]:
    try:
        h, b, s = str(token).split(".")
        return json.loads(_b64d(h)), json.loads(_b64d(b)), f"{h}.{b}".encode(), _b64d(s)
    except Exception:  # noqa: BLE001
        raise LTIError("bad_token", "the LMS sent a malformed id_token") from None


def jwt_verify_signature(signing_input: bytes, sig: bytes, public_key: Any) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    try:
        public_key.verify(sig, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise LTIError("bad_signature", "the LMS token signature is invalid") from None


def generate_key() -> Any:
    from cryptography.hazmat.primitives.asymmetric import rsa
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _load_pem(pem: str) -> Any:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    if "BEGIN" not in pem and os.path.exists(pem):
        with open(pem, encoding="utf-8") as fh:
            pem = fh.read()
    return load_pem_private_key(pem.encode(), password=None)


def _pem(key: Any) -> str:
    from cryptography.hazmat.primitives import serialization as s
    return key.private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption()).decode()


def short_role(uri: str) -> str:
    return uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


# -- platforms ----------------------------------------------------------------------------------
@dataclass
class LTIPlatform:
    """One LMS registration. Presets fill in the standard URLs; check them against the LMS's
    tool registration page (self-hosted installs sometimes differ)."""
    issuer: str
    client_id: str
    auth_login_url: str
    auth_token_url: str
    jwks_url: str
    deployment_ids: List[str] = field(default_factory=list)  # empty = accept any deployment
    name: str = ""
    tenant_id: Optional[str] = None
    auth_server: Optional[str] = None  # 'aud' for the client assertion (default: token URL)
    active: bool = True

    @property
    def id(self) -> str:
        return hashlib.sha256(f"{self.issuer}|{self.client_id}".encode()).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, **asdict(self)}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "LTIPlatform":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    @staticmethod
    def _deps(dep: Any) -> List[str]:
        return [] if not dep else [str(d) for d in (dep if isinstance(dep, (list, tuple)) else [dep])]

    @classmethod
    def moodle(cls, url: str, client_id: str, deployment_id: Any = None, **kw: Any) -> "LTIPlatform":
        u = url.rstrip("/")
        return cls(u, client_id, f"{u}/mod/lti/auth.php", f"{u}/mod/lti/token.php", f"{u}/mod/lti/certs.php",
                   cls._deps(deployment_id), kw.pop("name", "moodle"), **kw)

    @classmethod
    def canvas(cls, client_id: str, deployment_id: Any = None, *, host: str = "https://sso.canvaslms.com",
               issuer: str = "https://canvas.instructure.com", **kw: Any) -> "LTIPlatform":
        h = host.rstrip("/")
        return cls(issuer, client_id, f"{h}/api/lti/authorize_redirect", f"{h}/login/oauth2/token",
                   f"{h}/api/lti/security/jwks", cls._deps(deployment_id), kw.pop("name", "canvas"), **kw)

    @classmethod
    def blackboard(cls, client_id: str, deployment_id: Any = None, **kw: Any) -> "LTIPlatform":
        g = "https://developer.blackboard.com/api/v1"
        return cls("https://blackboard.com", client_id, f"{g}/gateway/oidcauth", f"{g}/gateway/oauth2/jwttoken",
                   f"{g}/management/applications/{client_id}/jwks.json", cls._deps(deployment_id),
                   kw.pop("name", "blackboard"), **kw)

    @classmethod
    def brightspace(cls, host: str, client_id: str, deployment_id: Any = None, **kw: Any) -> "LTIPlatform":
        h = host.rstrip("/") if "://" in host else f"https://{host.rstrip('/')}"
        kw.setdefault("auth_server", "https://api.brightspace.com/auth/token")
        return cls(h, client_id, f"{h}/d2l/lti/authenticate", "https://auth.brightspace.com/core/connect/token",
                   f"{h}/d2l/.well-known/jwks", cls._deps(deployment_id), kw.pop("name", "brightspace"), **kw)

    @classmethod
    def schoology(cls, client_id: str, deployment_id: Any = None, **kw: Any) -> "LTIPlatform":
        s = "https://lti-service.svc.schoology.com/lti-service"
        return cls("https://schoology.schoology.com", client_id, f"{s}/authorize-redirect", f"{s}/access-token",
                   f"{s}/.well-known/jwks", cls._deps(deployment_id), kw.pop("name", "schoology"), **kw)


@dataclass
class LTILaunch:
    """A verified launch. ``user_id`` is the email when the LMS shares it, else ``lti:<sub>``."""
    platform: LTIPlatform
    claims: Dict[str, Any]
    message_type: str
    sub: str
    email: str
    name: str
    roles: List[str]
    context: Dict[str, Any]
    resource_link: Dict[str, Any]
    custom: Dict[str, Any]
    deployment_id: str

    @property
    def user_id(self) -> str:
        return self.email or f"lti:{self.sub}"

    @property
    def is_staff(self) -> bool:
        return bool(STAFF & set(self.roles))

    @property
    def is_learner(self) -> bool:
        return "Learner" in self.roles or "Student" in self.roles


# -- the service --------------------------------------------------------------------------------
class LTIService:
    def __init__(self, meet: "NodeMeet", platforms: Iterable[LTIPlatform] = (), *, key: Optional[str] = None,
                 role_map: Optional[Dict[str, str]] = None, role_for: Optional[Callable[..., Any]] = None,
                 join_list: bool = True, grade_attendance: bool = False, attendance_minutes: float = 0,
                 room_for: Optional[Callable[..., Any]] = None, on_launch: Optional[Callable[..., Any]] = None,
                 registration: Optional[str] = "invite", title: str = "Live classes",
                 description: str = "Live video classes and office-hour booking", http: Optional[HTTPClient] = None,
                 allow_http: bool = False, grades: bool = True, roster: bool = True) -> None:
        self.meet = meet
        self.static = {p.id: p for p in platforms}
        self._key_src = key or os.environ.get("NODEMEET_LTI_KEY")
        self._key: Any = None
        self.kid = ""
        self.role_map = {**DEFAULT_ROLE_MAP, **(role_map or {})}
        self.role_for, self.room_for, self.on_launch = role_for, room_for, on_launch
        self.join_list, self.grade_attendance, self.attendance_minutes = join_list, grade_attendance, attendance_minutes
        if registration not in (None, "invite", "open"):
            raise ValueError("registration must be 'invite', 'open' or None")
        self.registration, self.title, self.description = registration, title, description
        self.http = http or HTTPClient()
        self.allow_http = allow_http
        self.grades, self.roster_on = grades, roster
        if not grades and grade_attendance:
            raise ValueError("grade_attendance=True needs grades=True")
        self._jwks: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._tokens: Dict[str, Tuple[float, str]] = {}
        self._mem: Dict[str, Dict[str, Any]] = {}  # fallback when storage has no records
        meet.lti = self
        meet.use(self)
        meet.on_event("meeting.ended")(self._meeting_ended)

    # -- plumbing ---------------------------------------------------------------------------
    def register(self, r: Router) -> None:
        r.add("GET", "/lti/jwks", self.http_jwks)
        r.add("GET", "/lti/login", self.http_login)
        r.add("POST", "/lti/login", self.http_login)
        r.add("POST", "/lti/launch", self.http_launch)
        r.add("POST", "/lti/deeplink", self.http_deeplink)
        r.add("GET", "/lti/register", self.http_register)
        r.add("GET", "/lti/canvas.json", self.http_canvas)
        r.add("GET", "/api/lti/platforms", self.api_list)
        r.add("POST", "/api/lti/platforms", self.api_add)
        r.add("DELETE", "/api/lti/platforms/{pid}", self.api_delete)
        r.add("GET", "/api/lti/registration-url", self.api_reg_url)
        r.add("GET", "/api/lti/rooms/{room}/roster", self.api_roster)
        r.add("POST", "/api/lti/rooms/{room}/sync-roster", self.api_sync)
        r.add("POST", "/api/lti/rooms/{room}/grades", self.api_grade)

    @property
    def scopes(self) -> List[str]:
        """Services this tool asks the LMS for (grades / roster can be switched off)."""
        return [s for s in SCOPES if (self.grades or "lti-ags" not in s) and (self.roster_on or s != S_NRPS)]

    @property
    def base(self) -> str:
        return self.meet.base_url

    async def _get(self, kind: str, key: str) -> Optional[Dict[str, Any]]:
        try:
            return await self.meet.storage.get_record(kind, key)
        except NotImplementedError:
            return self._mem.get(f"{kind}:{key}")

    async def _put(self, kind: str, key: str, value: Dict[str, Any]) -> None:
        try:
            await self.meet.storage.put_record(kind, key, value)
        except NotImplementedError:
            self._mem[f"{kind}:{key}"] = value

    async def _emit(self, event: str, data: Dict[str, Any], tenant: Optional[str] = None) -> None:
        await self.meet.emit_webhook(event, data, tenant_id=tenant)

    async def _call(self, fn: Optional[Callable[..., Any]], *args: Any) -> Any:
        if fn is None:
            return None
        out = fn(*args)
        return await out if inspect.isawaitable(out) else out

    async def key(self) -> Any:
        """The tool's RSA key: ``key=`` / NODEMEET_LTI_KEY (PEM or path), else generated once and saved."""
        if self._key is None:
            if self._key_src:
                self._key = _load_pem(self._key_src)
            else:
                rec = await self._get("lti", "tool_key")
                if rec:
                    self._key = _load_pem(rec["pem"])
                else:
                    self._key = generate_key()
                    await self._put("lti", "tool_key", {"pem": _pem(self._key), "created": time.time()})
            self.kid = public_jwk(self._key, "x")["n"][:16]
        return self._key

    async def jwks(self) -> Dict[str, Any]:
        k = await self.key()
        return {"keys": [public_jwk(k, self.kid)]}

    # -- platform registry ----------------------------------------------------------------------
    async def add_platform(self, p: LTIPlatform) -> LTIPlatform:
        """Save a registration in your database (permanent; survives restarts)."""
        await self._put("lti_platform", p.id, p.to_dict())
        return p

    async def remove_platform(self, pid: str) -> None:
        self.static.pop(pid, None)
        try:
            await self.meet.storage.delete_record("lti_platform", pid)
        except NotImplementedError:
            self._mem.pop(f"lti_platform:{pid}", None)

    async def platforms(self) -> List[LTIPlatform]:
        out = dict(self.static)
        try:
            rows = await self.meet.storage.list_records("lti_platform")
        except NotImplementedError:
            rows = [(k.split(":", 1)[1], v) for k, v in self._mem.items() if k.startswith("lti_platform:")]
        for _, rec in rows:
            p = LTIPlatform.from_dict(rec)
            out.setdefault(p.id, p)
        return list(out.values())

    async def find(self, issuer: str, client_id: Optional[str] = None) -> LTIPlatform:
        hits = [p for p in await self.platforms() if p.active and p.issuer == issuer
                and (not client_id or p.client_id == client_id)]
        if len(hits) != 1:
            raise LTIError("unknown_platform", "this LMS is not registered with nodemeet" if not hits
                           else "several registrations for this LMS: it must send client_id", 400)
        return hits[0]

    # -- 1. OIDC login initiation ---------------------------------------------------------------
    async def http_login(self, req: Request) -> Response:
        q = dict(req.query)
        if req.body:
            q.update({k: v[0] for k, v in parse_qs(req.body.decode()).items()})
        if not q.get("iss") or not q.get("login_hint"):
            raise LTIError("bad_login", "missing iss or login_hint", 400)
        p = await self.find(q["iss"], q.get("client_id"))
        nonce = secrets.token_urlsafe(24)
        state = sign_blob(self.meet.tokens, {"pid": p.id, "nonce": nonce}, ttl=600, purpose="lti")
        params = {"scope": "openid", "response_type": "id_token", "response_mode": "form_post",
                  "prompt": "none", "client_id": p.client_id, "redirect_uri": f"{self.base}/lti/launch",
                  "login_hint": q["login_hint"], "state": state, "nonce": nonce}
        if q.get("lti_message_hint"):
            params["lti_message_hint"] = q["lti_message_hint"]
        sep = "&" if "?" in p.auth_login_url else "?"
        return Response(302, b"", "text/plain", {"Location": p.auth_login_url + sep + urlencode(params)})

    # -- 2. launch ------------------------------------------------------------------------------
    async def _platform_keys(self, p: LTIPlatform, refresh: bool = False) -> List[Dict[str, Any]]:
        hit = self._jwks.get(p.id)
        if hit and hit[0] > time.monotonic() and not refresh:
            return hit[1]
        res = await self.http.request("GET", p.jwks_url, expect_ok=True, what="LMS JWKS")
        keys = (res.json() or {}).get("keys") or []
        self._jwks[p.id] = (time.monotonic() + 600, keys)
        return keys

    async def verify(self, id_token: str, state: str) -> LTILaunch:
        """Check everything LTI 1.3 requires: signature (platform JWKS), iss, aud/azp, exp/iat,
        nonce (single use, bound to the signed state), version, deployment."""
        try:
            st = verify_blob(self.meet.tokens, state, purpose="lti")
        except Exception:  # noqa: BLE001
            raise LTIError("bad_state", "this launch expired, open the activity again") from None
        head, claims, signing_input, sig = jwt_parts(id_token)
        if head.get("alg") != "RS256":
            raise LTIError("bad_alg", "only RS256 tokens are accepted")
        p = next((x for x in await self.platforms() if x.id == st["pid"] and x.active), None)
        if p is None or claims.get("iss") != p.issuer:
            raise LTIError("bad_issuer", "token issuer does not match the LMS that started the login")
        keys = await self._platform_keys(p)
        match = [k for k in keys if not head.get("kid") or k.get("kid") == head.get("kid")]
        if not match:
            keys = await self._platform_keys(p, refresh=True)  # the LMS rotated its keys
            match = [k for k in keys if not head.get("kid") or k.get("kid") == head.get("kid")]
        if not match:
            raise LTIError("unknown_key", "the LMS signed with a key it does not publish")
        jwt_verify_signature(signing_input, sig, jwk_public_key(match[0]))
        now = time.time()
        aud = claims.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        if p.client_id not in auds or (len(auds) > 1 and claims.get("azp") != p.client_id):
            raise LTIError("bad_audience", "token was not issued for this tool")
        if float(claims.get("exp", 0)) < now - 60 or float(claims.get("iat", now)) > now + 60:
            raise LTIError("expired", "the LMS token expired, open the activity again")
        nonce = claims.get("nonce")
        if not nonce or nonce != st["nonce"]:
            raise LTIError("bad_nonce", "nonce mismatch")
        if await self._get("lti_nonce", nonce):
            raise LTIError("replay", "this launch was already used")
        await self._put("lti_nonce", nonce, {"at": now})
        if claims.get(C + "version") != "1.3.0":
            raise LTIError("bad_version", "only LTI 1.3.0 is supported", 400)
        dep = str(claims.get(C + "deployment_id") or "")
        if not dep or (p.deployment_ids and dep not in p.deployment_ids):
            raise LTIError("bad_deployment", "this LMS deployment is not allowed", 403)
        return LTILaunch(
            platform=p, claims=claims, message_type=claims.get(C + "message_type", ""),
            sub=str(claims.get("sub") or ""), email=str(claims.get("email") or "").lower(),
            name=claims.get("name") or " ".join(x for x in (claims.get("given_name"), claims.get("family_name")) if x)
            or claims.get("email") or "Student",
            roles=sorted({short_role(r) for r in claims.get(C + "roles") or []}),
            context=claims.get(C + "context") or {}, resource_link=claims.get(C + "resource_link") or {},
            custom=claims.get(C + "custom") or {}, deployment_id=dep)

    def role(self, launch: LTILaunch) -> str:
        roles = [self.role_map[r] for r in launch.roles if r in self.role_map] or ["participant"]
        return max(roles, key=lambda r: ROLE_RANK.get(r, 2))

    def room_id(self, launch: LTILaunch) -> str:
        raw = "|".join([launch.platform.issuer, launch.deployment_id, str(launch.context.get("id", "")),
                        str(launch.resource_link.get("id", ""))])
        return "lti-" + hashlib.sha256(raw.encode()).hexdigest()[:16]

    async def http_launch(self, req: Request) -> Response:
        form = {k: v[0] for k, v in parse_qs(req.body.decode()).items()} if req.body else {}
        if form.get("error"):
            raise LTIError("lms_error", form.get("error_description") or form["error"], 400)
        try:
            launch = await self.verify(form.get("id_token", ""), form.get("state", ""))
        except LTIError as exc:
            await self._emit("lti.failed", {"step": "launch", "error": exc.code, "message": exc.message})
            raise
        if launch.message_type == "LtiDeepLinkingRequest":
            return await self._picker(launch)
        if launch.message_type != "LtiResourceLinkRequest":
            raise LTIError("unsupported_message", f"{launch.message_type} is not supported", 400)
        return await self.launch_response(launch)

    async def launch_response(self, launch: LTILaunch) -> Response:
        """Turn a verified resource-link launch into a redirect (room or booking page)."""
        custom = await self._call(self.on_launch, launch)
        if isinstance(custom, Response):
            return custom
        if isinstance(custom, str):
            return _redirect(custom)
        over = custom if isinstance(custom, dict) else {}
        p = launch.platform
        if launch.custom.get("booking") and not over.get("room"):
            q = urlencode({"name": launch.name, "email": launch.email}) if launch.email else ""
            await self._emit("lti.launch", self._launch_event(launch, None, "booking"), p.tenant_id)
            return _redirect(f"{self.base}/book/{launch.custom['booking']}" + (f"?{q}" if q else ""))
        room = over.get("room") or await self._call(self.room_for, launch)
        cfg = None
        if not room and launch.custom.get("room"):
            cfg = await self.meet.storage.get_room(str(launch.custom["room"]))
            owner = (cfg.metadata.get("lti") or {}).get("platform") if cfg else None
            if cfg is not None and owner == p.id:  # only rooms this LMS created via deep linking
                room = cfg.id
            else:
                cfg = None
        room = room or self.room_id(launch)
        cfg = cfg or await self.meet.storage.get_room(room)
        if cfg is None:
            title = launch.resource_link.get("title") or launch.context.get("title") or "Class"
            cfg = await self.meet.create_room(room_id=room, name=title, tenant_id=p.tenant_id, metadata={
                "lti": {"platform": p.id, "context": launch.context.get("id"),
                        "context_title": launch.context.get("title"), "resource_link": launch.resource_link.get("id")}})
        role = over.get("role") or await self._call(self.role_for, launch) or self.role(launch)
        if not role:
            raise LTIError("not_allowed", "your LMS role may not join this meeting", 403)
        await self._remember(room, launch)
        if self.join_list and cfg.lobby and not cfg.on_join_list(launch.user_id):
            await self.meet.add_to_join_list(room, launch.user_id)
        token = self.meet.create_token(room, launch.user_id, str(role), name=over.get("name") or launch.name,
                                       tenant=cfg.tenant_id, meta={"lti": p.id, "lti_sub": launch.sub,
                                                                   "email": launch.email, "lms_roles": launch.roles})
        await self._emit("lti.launch", self._launch_event(launch, room, str(role)), p.tenant_id)
        return _redirect(self.meet.join_url(room, token))

    def _launch_event(self, launch: LTILaunch, room: Optional[str], role: str) -> Dict[str, Any]:
        return {"room": room, "platform": launch.platform.name or launch.platform.issuer, "role": role,
                "user": {"id": launch.user_id, "email": launch.email, "name": launch.name, "lms_roles": launch.roles},
                "course": {"id": launch.context.get("id"), "title": launch.context.get("title")},
                "link": {"id": launch.resource_link.get("id"), "title": launch.resource_link.get("title")}}

    async def _remember(self, room: str, launch: LTILaunch) -> None:
        old = await self._get("lti_context", room) or {}
        ags = launch.claims.get(AGS) or {}
        ctx = {**old, "platform": launch.platform.id, "deployment_id": launch.deployment_id,
               "context": launch.context, "resource_link": launch.resource_link}
        if launch.claims.get(NRPS):
            ctx["nrps"] = launch.claims[NRPS].get("context_memberships_url")
        if ags:
            ctx["ags"] = {**(old.get("ags") or {}), **{k: ags[k] for k in ("lineitem", "lineitems", "scope") if k in ags}}
        await self._put("lti_context", room, ctx)
        await self._put("lti_user", f"{room}|{launch.user_id}", {"sub": launch.sub, "roles": launch.roles})

    # -- 3. deep linking (instructor picks what to add to the course) ------------------------------
    async def _picker(self, launch: LTILaunch) -> Response:
        if not launch.is_staff:
            raise LTIError("not_allowed", "only instructors can add meetings", 403)
        settings = launch.claims.get(DL + "deep_linking_settings") or {}
        if not settings.get("deep_link_return_url"):
            raise LTIError("bad_request", "deep linking settings missing", 400)
        can_grade = self.can_grade(launch)
        state = sign_blob(self.meet.tokens, {"pid": launch.platform.id, "dep": launch.deployment_id,
                                             "s": settings, "ctx": launch.context, "u": launch.user_id,
                                             "g": can_grade}, ttl=3600, purpose="lti-dl")
        default = escape(launch.context.get("title") or "Live class")
        grade_box = ("<label><input type=checkbox name=grade value=1> Send attendance to the gradebook</label>\n"
                     if can_grade else "")
        html = f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Add a live class</title><style>body{{font:15px system-ui;max-width:460px;margin:32px auto;padding:0 16px}}
label{{display:block;margin:12px 0 4px}}input[type=text]{{width:100%;padding:8px}}button{{margin-top:16px;padding:10px 18px}}</style>
<h2>Add to {default}</h2><form method=post action="{escape(self.base)}/lti/deeplink">
<input type=hidden name=state value="{escape(state)}">
<label><input type=radio name=kind value=meeting checked> Live meeting room</label>
<label><input type=radio name=kind value=booking> Booking page (office hours)</label>
<label>Title</label><input type=text name=title value="{default} - live class">
<label>Booking page id (only for booking pages)</label><input type=text name=booking placeholder="host id">
<label><input type=checkbox name=waiting_room value=1 checked> Waiting room for people not enrolled</label>
{grade_box}<button>Add</button></form>"""
        return text_response(html, "text/html")

    async def http_deeplink(self, req: Request) -> Response:
        form = {k: v[0] for k, v in parse_qs(req.body.decode()).items()} if req.body else {}
        try:
            st = verify_blob(self.meet.tokens, form.get("state", ""), purpose="lti-dl")
        except Exception:  # noqa: BLE001
            raise LTIError("bad_state", "this page expired, start again from the LMS") from None
        p = next((x for x in await self.platforms() if x.id == st["pid"]), None)
        if p is None:
            raise LTIError("unknown_platform", "LMS registration was removed", 400)
        title = (form.get("title") or "Live class")[:200]
        if form.get("kind") == "booking":
            if not form.get("booking"):
                raise LTIError("bad_request", "enter the booking page id", 400)
            item = self.booking_item(form["booking"], title)
        else:
            item = await self.meeting_item(p, title, waiting_room=form.get("waiting_room") == "1",
                                           grade=bool(st.get("g")) and form.get("grade") == "1", course=st.get("ctx") or {})
        await self._emit("lti.deep_link", {"platform": p.name or p.issuer, "item": item,
                                           "course": (st.get("ctx") or {}).get("id"), "by": st.get("u")}, p.tenant_id)
        return await self.deep_link_response(p, st["dep"], st["s"], [item])

    async def meeting_item(self, p: LTIPlatform, title: str, *, waiting_room: bool = True, grade: bool = False,
                           course: Optional[Dict[str, Any]] = None, room_id: Optional[str] = None) -> Dict[str, Any]:
        """Create a room owned by this LMS and return its deep-linking content item."""
        rid = room_id or "lti-" + secrets.token_hex(8)
        await self.meet.create_room(room_id=rid, name=title, tenant_id=p.tenant_id, waiting_room=waiting_room,
                                    metadata={"lti": {"platform": p.id, "context": (course or {}).get("id"),
                                                      "context_title": (course or {}).get("title"), "grade": grade}})
        item: Dict[str, Any] = {"type": "ltiResourceLink", "title": title, "url": f"{self.base}/lti/launch",
                                "custom": {"room": rid}}
        if grade:
            item["lineItem"] = {"scoreMaximum": 100, "label": f"{title} (attendance)", "resourceId": rid}
        return item

    def can_grade(self, launch: LTILaunch) -> bool:
        """Offer gradebook options only when grades are on here AND the LMS offers a gradebook for
        this course (it sends the Assignment and Grade Services claim; e.g. not for a course with
        grades turned off)."""
        return bool(self.grades and launch.claims.get(AGS))

    def booking_item(self, host_id: str, title: str) -> Dict[str, Any]:
        return {"type": "ltiResourceLink", "title": title, "url": f"{self.base}/lti/launch",
                "custom": {"booking": host_id}}

    async def deep_link_response(self, p: LTIPlatform, deployment_id: str, settings: Dict[str, Any],
                                 items: List[Dict[str, Any]]) -> Response:
        """Signed LtiDeepLinkingResponse, auto-posted back to the LMS."""
        key = await self.key()
        now = int(time.time())
        claims = {"iss": p.client_id, "aud": p.issuer, "iat": now, "exp": now + 600,
                  "nonce": secrets.token_urlsafe(16), C + "deployment_id": deployment_id,
                  C + "message_type": "LtiDeepLinkingResponse", C + "version": "1.3.0",
                  DL + "content_items": items}
        if settings.get("data"):
            claims[DL + "data"] = settings["data"]
        jwt = jwt_encode(claims, key, self.kid)
        return _autopost(settings["deep_link_return_url"], {"JWT": jwt})

    # -- 4. LTI Advantage services --------------------------------------------------------------
    async def access_token(self, p: LTIPlatform, scopes: Iterable[str]) -> str:
        sc = " ".join(sorted(set(scopes)))
        hit = self._tokens.get(f"{p.id}|{sc}")
        if hit and hit[0] > time.monotonic():
            return hit[1]
        key = await self.key()
        now = int(time.time())
        assertion = jwt_encode({"iss": p.client_id, "sub": p.client_id, "aud": p.auth_server or p.auth_token_url,
                                "iat": now, "exp": now + 300, "jti": secrets.token_urlsafe(16)}, key, self.kid)
        res = await self.http.request("POST", p.auth_token_url, form={
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion, "scope": sc}, what="LMS token")
        data = res.json() or {}
        if not res.ok or not data.get("access_token"):
            err = data.get("error") or f"HTTP {res.status}"
            raise LTIError("lms_refused", f"the LMS refused access ({err}): "
                           + ("this service may be turned off for this course or school"
                              if err in ("invalid_scope", "unauthorized_client") or res.status in (403, 404)
                              else (data.get("error_description") or "check the tool registration")), 502)
        self._tokens[f"{p.id}|{sc}"] = (time.monotonic() + int(data.get("expires_in", 3600)) - 60, data["access_token"])
        return data["access_token"]

    async def _ctx(self, room: str) -> Tuple[Dict[str, Any], LTIPlatform]:
        ctx = await self._get("lti_context", room)
        if not ctx:
            raise LTIError("no_lti_context", "nobody has launched this room from an LMS yet", 404)
        p = next((x for x in await self.platforms() if x.id == ctx["platform"]), None)
        if p is None:
            raise LTIError("unknown_platform", "LMS registration was removed", 404)
        return ctx, p

    async def roster(self, room: str) -> List[Dict[str, Any]]:
        """Everyone enrolled in the course (Names and Role Provisioning Services)."""
        if not self.roster_on:
            raise LTIError("roster_off", "roster sync is turned off (add_lti(roster=False))", 400)
        ctx, p = await self._ctx(room)
        url = ctx.get("nrps")
        if not url:
            raise LTIError("no_roster", "the LMS did not share the course roster (enable Names and Roles)", 400)
        try:
            token = await self.access_token(p, [S_NRPS])
        except LTIError as exc:
            await self._emit("lti.failed", {"step": "roster", "room": room, "error": exc.message}, p.tenant_id)
            raise
        members: List[Dict[str, Any]] = []
        while url:
            res = await self.http.request("GET", url, bearer=token, what="LMS roster", headers={
                "Accept": "application/vnd.ims.lti-nrps.v2.membershipcontainer+json"})
            if not res.ok:
                msg = ("the LMS doesn't share this course's roster (roster sync may be turned off)"
                       if res.status in (401, 403, 404) else f"the LMS roster request failed (HTTP {res.status})")
                await self._emit("lti.failed", {"step": "roster", "room": room, "status": res.status, "error": msg},
                                 p.tenant_id)
                raise LTIError("roster_refused", msg, 502)
            for m in (res.json() or {}).get("members") or []:
                members.append({"sub": str(m.get("user_id")), "email": str(m.get("email") or "").lower(),
                                "name": m.get("name") or "", "status": m.get("status", "Active"),
                                "roles": sorted({short_role(r) for r in m.get("roles") or []})})
            url = _next_link(res.headers.get("link", ""))
        return members

    async def sync_roster(self, room: str, *, remove_missing: bool = False) -> Dict[str, Any]:
        """Put every active course member on the room's join list (they skip the waiting room)."""
        ctx, p = await self._ctx(room)
        members = [m for m in await self.roster(room) if m["status"] == "Active"]
        ids = [m["email"] or f"lti:{m['sub']}" for m in members]
        for m, uid in zip(members, ids):
            await self._put("lti_user", f"{room}|{uid}", {"sub": m["sub"], "roles": m["roles"]})
        cfg = await self.meet.storage.get_room(room)
        before = set(cfg.allowed_users) if cfg else set()
        await self.meet.add_to_join_list(room, *ids)
        removed: List[str] = []
        if remove_missing and cfg:
            removed = [u for u in before if u.lower() not in {i.lower() for i in ids}]
            if removed:
                await self.meet.remove_from_join_list(room, *removed)
        out = {"room": room, "members": len(members), "added": len([i for i in ids if i not in before]),
               "removed": len(removed)}
        await self._emit("lti.roster_synced", out, p.tenant_id)
        return out

    async def send_grade(self, room: str, user: str, score: float, maximum: float = 100, *,
                         comment: Optional[str] = None, activity: str = "Completed",
                         grading: str = "FullyGraded") -> Dict[str, Any]:
        """Post a score to the course gradebook (Assignment and Grade Services). ``user`` is the
        nodemeet user id (email / lti:sub) or the LMS user id."""
        if not self.grades:
            raise LTIError("grades_off", "grades are turned off (add_lti(grades=False))", 400)
        ctx, p = await self._ctx(room)
        ags = ctx.get("ags") or {}
        rec = await self._get("lti_user", f"{room}|{user}")
        sub = rec["sub"] if rec else (user[4:] if user.startswith("lti:") else user)
        try:
            lineitem = ags.get("lineitem") or await self._lineitem(room, ctx, p, maximum)
            token = await self.access_token(p, [S_SCORE, S_LINEITEM])
            url, _, q = lineitem.partition("?")
            body = {"userId": sub, "scoreGiven": score, "scoreMaximum": maximum, "activityProgress": activity,
                    "gradingProgress": grading,
                    "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
            if comment:
                body["comment"] = comment
            await self.http.request("POST", f"{url.rstrip('/')}/scores" + (f"?{q}" if q else ""), bearer=token,
                                    data=json.dumps(body).encode(), expect_ok=True, what="LMS score",
                                    headers={"Content-Type": "application/vnd.ims.lis.v1.score+json"})
        except LTIError as exc:
            await self._emit("lti.failed", {"step": "grade", "room": room, "user": user, "error": exc.message}, p.tenant_id)
            raise
        except Exception as exc:  # noqa: BLE001
            from .integrations.http_client import HTTPError as CallFailed
            status = exc.result.status if isinstance(exc, CallFailed) else None
            msg = ("the LMS refused the score (grades may be turned off for this course)"
                   if status in (401, 403, 404) else f"sending the score failed: {exc}")
            await self._emit("lti.failed", {"step": "grade", "room": room, "user": user, "status": status,
                                            "error": msg}, p.tenant_id)
            raise LTIError("grade_refused", msg, 502) from None
        out = {"room": room, "user": user, "lms_user": sub, "score": score, "maximum": maximum}
        await self._emit("lti.grade_sent", out, p.tenant_id)
        return out

    async def _lineitem(self, room: str, ctx: Dict[str, Any], p: LTIPlatform, maximum: float) -> str:
        ags = ctx.get("ags") or {}
        if not ags.get("lineitems"):
            raise LTIError("no_gradebook", "the LMS did not share a gradebook for this link", 400)
        token = await self.access_token(p, [S_LINEITEM])
        title = (ctx.get("resource_link") or {}).get("title") or "Live class"
        res = await self.http.request("POST", ags["lineitems"], bearer=token, expect_ok=True, what="LMS line item",
                                      data=json.dumps({"scoreMaximum": maximum, "label": f"{title} (attendance)",
                                                       "resourceId": room,
                                                       "resourceLinkId": (ctx.get("resource_link") or {}).get("id")}).encode(),
                                      headers={"Content-Type": "application/vnd.ims.lis.v2.lineitem+json"})
        ags["lineitem"] = (res.json() or {})["id"]
        ctx["ags"] = ags
        await self._put("lti_context", room, ctx)
        return ags["lineitem"]

    def attendance_score(self, seconds: float) -> float:
        need = self.attendance_minutes * 60
        return 100.0 if not need or seconds >= need else round(100 * seconds / need, 1)

    async def _meeting_ended(self, data: Dict[str, Any]) -> None:
        room = data.get("room")
        if not self.grades:
            return
        ctx = await self._get("lti_context", room) if room else None
        if not ctx:
            return
        cfg = await self.meet.storage.get_room(room)
        if not (self.grade_attendance or (cfg and (cfg.metadata.get("lti") or {}).get("grade"))):
            return
        for a in data.get("attendees") or []:
            rec = await self._get("lti_user", f"{room}|{a['user_id']}")
            if not rec or STAFF & set(rec.get("roles") or []):
                continue
            secs = a.get("seconds", 0)
            try:
                await self.send_grade(room, a["user_id"], self.attendance_score(secs),
                                      comment=f"Attended {round(secs / 60)} min")
            except LTIError as exc:
                log.warning("attendance grade for %s not sent: %s", a["user_id"], exc.message)
            except Exception:  # noqa: BLE001
                log.exception("attendance grade failed for %s", a["user_id"])

    # -- 5. dynamic registration ------------------------------------------------------------------
    def registration_url(self, ttl_days: int = 7, tenant_id: Optional[str] = None) -> str:
        """The URL an LMS admin pastes into "register tool by URL" (Moodle, Canvas, Brightspace...)."""
        invite = sign_blob(self.meet.tokens, {"t": tenant_id}, ttl=ttl_days * 86400, purpose="lti-reg")
        return f"{self.base}/lti/register?" + urlencode({"invite": invite})

    def tool_config(self) -> Dict[str, Any]:
        host = urlparse(self.base).netloc
        return {"application_type": "web", "response_types": ["id_token"],
                "grant_types": ["implicit", "client_credentials"], "client_name": self.title,
                "initiate_login_uri": f"{self.base}/lti/login", "redirect_uris": [f"{self.base}/lti/launch"],
                "jwks_uri": f"{self.base}/lti/jwks", "token_endpoint_auth_method": "private_key_jwt",
                "scope": " ".join(self.scopes),
                TOOL_CONF: {"domain": host, "description": self.description,
                            "target_link_uri": f"{self.base}/lti/launch",
                            "claims": ["iss", "sub", "name", "given_name", "family_name", "email"],
                            "messages": [{"type": "LtiDeepLinkingRequest", "target_link_uri": f"{self.base}/lti/launch",
                                          "label": f"Add {self.title}"},
                                         {"type": "LtiResourceLinkRequest", "target_link_uri": f"{self.base}/lti/launch",
                                          "label": self.title}]}}

    async def http_register(self, req: Request) -> Response:
        tenant = None
        if self.registration is None:
            raise LTIError("registration_off", "dynamic registration is turned off", 403)
        if self.registration == "invite":
            try:
                tenant = verify_blob(self.meet.tokens, req.query.get("invite", ""), purpose="lti-reg").get("t")
            except Exception:  # noqa: BLE001
                raise LTIError("bad_invite", "ask the nodemeet admin for a fresh registration link", 403) from None
        conf_url = req.query.get("openid_configuration", "")
        if not conf_url.startswith("https://") and not (self.allow_http and conf_url.startswith("http://")):
            raise LTIError("bad_request", "openid_configuration must be an https URL", 400)
        reg_token = req.query.get("registration_token")
        from .integrations.http_client import HTTPError as CallFailed
        try:
            conf = (await self.http.request("GET", conf_url, bearer=reg_token, expect_ok=True,
                                            what="LMS configuration")).json() or {}
            if not conf.get("registration_endpoint") or not conf.get("issuer"):
                raise LTIError("bad_config", "the LMS configuration is incomplete", 400)
            res = await self.http.request("POST", conf["registration_endpoint"], json_body=self.tool_config(),
                                          bearer=reg_token, expect_ok=True, what="LMS registration")
        except CallFailed as exc:
            await self._emit("lti.failed", {"step": "register", "error": str(exc)}, tenant)
            raise LTIError("lms_refused", f"the LMS refused the registration: {exc}", 502) from None
        out = res.json() or {}
        dep = (out.get(TOOL_CONF) or {}).get("deployment_id")
        family = (conf.get(PLATFORM_CONF) or {}).get("product_family_code") or "lms"
        p = await self.add_platform(LTIPlatform(
            conf["issuer"], out["client_id"], conf["authorization_endpoint"], conf["token_endpoint"],
            conf["jwks_uri"], [str(dep)] if dep else [], family, tenant,
            conf.get("authorization_server")))
        await self._emit("lti.registered", {"platform": family, "issuer": p.issuer, "client_id": p.client_id,
                                            "deployment_id": dep}, tenant)
        return text_response("<!doctype html><p>Connected. You can close this window.</p><script>"
                             "(window.opener||window.parent).postMessage({subject:'org.imsglobal.lti.close'},'*')"
                             "</script>", "text/html")

    async def http_canvas(self, req: Request) -> Response:
        b = self.base
        return json_response({
            "title": self.title, "description": self.description, "oidc_initiation_url": f"{b}/lti/login",
            "target_link_uri": f"{b}/lti/launch", "public_jwk_url": f"{b}/lti/jwks", "scopes": self.scopes,
            "extensions": [{"platform": "canvas.instructure.com", "privacy_level": "public", "settings": {
                "placements": [
                    {"placement": "course_navigation", "message_type": "LtiResourceLinkRequest", "text": self.title},
                    {"placement": "link_selection", "message_type": "LtiDeepLinkingRequest"},
                    {"placement": "assignment_selection", "message_type": "LtiDeepLinkingRequest"}]}}],
            "custom_fields": {}})

    async def http_jwks(self, req: Request) -> Response:
        return json_response(await self.jwks())

    # -- admin REST (master API key) ----------------------------------------------------------
    async def api_list(self, req: Request) -> Response:
        await self.meet.api.require_master(req)
        return json_response({"platforms": [p.to_dict() for p in await self.platforms()]})

    async def api_add(self, req: Request) -> Response:
        await self.meet.api.require_master(req)
        body = req.json()
        preset = body.pop("preset", None)
        try:
            if preset:
                p = getattr(LTIPlatform, preset)(*body.pop("args", []), **body)
            else:
                p = LTIPlatform.from_dict(body)
        except (AttributeError, TypeError, KeyError) as exc:
            raise HTTPError(400, "bad_request", f"bad platform: {exc}") from None
        await self.add_platform(p)
        return json_response(p.to_dict(), 201)

    async def api_delete(self, req: Request) -> Response:
        await self.meet.api.require_master(req)
        await self.remove_platform(req.match_info["pid"])
        return json_response({"deleted": True})

    async def api_reg_url(self, req: Request) -> Response:
        await self.meet.api.require_master(req)
        return json_response({"url": self.registration_url(tenant_id=req.query.get("tenant"))})

    async def _room_access(self, req: Request) -> str:
        who = await self.meet.api.require(req, "rooms:write")
        room = req.match_info["room"]
        cfg = await self.meet.storage.get_room(room)
        if cfg is None or (not who.is_master and cfg.tenant_id != who.tenant_id):
            raise HTTPError(404, "not_found", "no such room")
        return room

    async def api_roster(self, req: Request) -> Response:
        return json_response({"members": await self.roster(await self._room_access(req))})

    async def api_sync(self, req: Request) -> Response:
        room = await self._room_access(req)
        body = req.json()
        return json_response(await self.sync_roster(room,
                                                    remove_missing=bool(body.get("remove_missing"))))

    async def api_grade(self, req: Request) -> Response:
        room = await self._room_access(req)
        b = req.json()
        if "user" not in b or "score" not in b:
            raise HTTPError(400, "bad_request", "send user and score")
        return json_response(await self.send_grade(room, str(b["user"]), float(b["score"]),
                                                   float(b.get("maximum", 100)), comment=b.get("comment")))


def _redirect(url: str) -> Response:
    return Response(302, b"", "text/plain", {"Location": url})


def _autopost(url: str, fields: Dict[str, str]) -> Response:
    inputs = "".join(f'<input type=hidden name="{escape(k)}" value="{escape(v)}">' for k, v in fields.items())
    return text_response(f'<!doctype html><form id=f method=post action="{escape(url)}">{inputs}'
                         '<noscript><button>Continue</button></noscript></form>'
                         '<script>document.getElementById("f").submit()</script>', "text/html")


def _next_link(header: str) -> Optional[str]:
    for part in header.split(","):
        if 'rel="next"' in part or "rel=next" in part:
            return part.split(";")[0].strip().strip("<>")
    return None
