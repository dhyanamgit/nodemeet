"""LTI 1.3 **platform** kit: make your own LMS speak LTI Advantage, the same way Moodle and
Canvas do. Then nodemeet (or any LTI 1.3 tool) connects to it exactly like a real customer would.

    from nodemeet.lti_platform import LTIPlatformKit
    kit = LTIPlatformKit("https://lms.example.com/lti-platform", db="postgresql://...",
                         authenticate=current_user_id,      # (request) -> logged-in user id
                         members=course_members,           # (course_id) -> [{"user_id","roles","email","name"}]
                         on_score=save_grade)               # (score dict) -> None
    app.mount("/lti-platform", kit.asgi())                  # FastAPI / Starlette / Django / Quart...

    url = kit.registration_url("https://meet.example.com/lti/register?invite=...")  # admin connects a tool
    url = await kit.launch(tool_id, user_id, ["Learner"], course, link)             # student clicks
    url = await kit.deep_link(tool_id, user_id, ["Instructor"], course)             # teacher adds activity

Endpoints (under the mount): /.well-known/openid-configuration, /lti/jwks, /lti/auth, /lti/token,
/lti/register, /lti/deep-link-return, /lti/nrps/{course}, /lti/ags/{course}/lineitems[/{id}[/scores|/results]].
"""
from __future__ import annotations

import inspect
import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass, field
from html import escape
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode

from ._http import HTTPError, Request, Response, Router, json_response, text_response
from .integrations.http_client import HTTPClient
from .lti import (AGS, C, DL, NRPS, PLATFORM_CONF, S_LINEITEM, S_NRPS, S_RESULT, S_SCORE, SCOPES, TOOL_CONF,
                  _autopost, _load_pem, _pem, _redirect, generate_key, jwk_public_key, jwt_encode, jwt_parts,
                  jwt_verify_signature, public_jwk)
from .tokens import TokenSigner, sign_blob, verify_blob

log = logging.getLogger("nodemeet.lti_platform")
S_LINEITEM_RO = "https://purl.imsglobal.org/spec/lti-ags/scope/lineitem.readonly"
LIS = "http://purl.imsglobal.org/vocab/lis/v2/"
ROLE_URIS = {
    "Administrator": [LIS + "institution/person#Administrator", LIS + "membership#Administrator"],
    "Instructor": [LIS + "membership#Instructor"],
    "TeachingAssistant": [LIS + "membership#Instructor", LIS + "membership/Instructor#TeachingAssistant"],
    "Learner": [LIS + "membership#Learner"], "Student": [LIS + "membership#Learner"],
    "ContentDeveloper": [LIS + "membership#ContentDeveloper"], "Mentor": [LIS + "membership#Mentor"],
    "Member": [LIS + "membership#Member"], "Manager": [LIS + "membership#Manager"],
    "Guest": [LIS + "institution/person#Guest"], "Observer": [LIS + "institution/person#Observer"],
}
CT_LINEITEM = "application/vnd.ims.lis.v2.lineitem+json"
CT_LINEITEMS = "application/vnd.ims.lis.v2.lineitemcontainer+json"
CT_RESULTS = "application/vnd.ims.lis.v2.resultcontainer+json"
CT_NRPS = "application/vnd.ims.lti-nrps.v2.membershipcontainer+json"


def role_uris(roles: Iterable[str]) -> List[str]:
    out: List[str] = []
    for r in roles:
        for u in ([r] if "://" in r else ROLE_URIS.get(r, [LIS + "membership#" + r])):
            if u not in out:
                out.append(u)
    return out


@dataclass
class Tool:
    """A tool connected to your LMS (nodemeet, or any LTI 1.3 tool)."""
    client_id: str
    deployment_id: str
    name: str
    login_url: str
    launch_url: str
    jwks_url: str = ""
    redirect_uris: List[str] = field(default_factory=list)
    scopes: List[str] = field(default_factory=lambda: list(SCOPES))
    deep_link_url: str = ""
    public_jwk: Optional[Dict[str, Any]] = None  # instead of jwks_url
    active: bool = True

    @property
    def id(self) -> str:
        return self.client_id

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Tool":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class LTIPlatformKit:
    def __init__(self, issuer: str, *, base_url: Optional[str] = None, secret: Optional[str] = None,
                 db: Any = None, storage: Any = None, key: Optional[str] = None,
                 authenticate: Optional[Callable[[Request], Any]] = None,
                 user: Optional[Callable[[str], Any]] = None,
                 members: Optional[Callable[[str], Any]] = None,
                 on_score: Optional[Callable[[Dict[str, Any]], Any]] = None,
                 on_content_items: Optional[Callable[..., Any]] = None,
                 on_tool_registered: Optional[Callable[[Tool], Any]] = None,
                 name: str = "My LMS", product: str = "custom-lms", http: Optional[HTTPClient] = None,
                 grades: bool = True, roster: bool = True) -> None:
        from .easy import storage_from_url
        self.issuer = issuer.rstrip("/")
        self.base = (base_url or issuer).rstrip("/")
        self.storage = storage or storage_from_url(db or "memory://")
        self.signer = TokenSigner(secret or secrets.token_urlsafe(32))
        if not secret:
            log.warning("LTIPlatformKit: no secret given; launch/registration links break on restart")
        self._key_src, self._key, self.kid = key, None, ""
        self.authenticate, self.user, self.members = authenticate, user, members
        self.on_score, self.on_content_items, self.on_tool_registered = on_score, on_content_items, on_tool_registered
        self.name, self.product = name, product
        self.grades, self.roster = grades, roster
        self.http = http or HTTPClient()
        self._jwks: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self.router = Router()
        self._register(self.router)
        self._started = False

    # -- hosting --------------------------------------------------------------------------------
    def asgi(self) -> Any:
        from .asgi import ASGIApp
        return ASGIApp(self)

    async def startup(self) -> None:
        if not self._started:
            self._started = True
            await self.storage.setup()

    async def shutdown(self) -> None:
        await self.storage.close()

    async def handle(self, req: Request) -> Response:
        """Serve one request (use this from Flask/Django views if you don't mount the ASGI app)."""
        await self.startup()
        try:
            handler, params = self.router.match(req.method, req.path or "/")
            req.match_info.update(params)
            return await handler(req)
        except HTTPError as exc:
            return exc.response()
        except Exception:  # noqa: BLE001
            log.exception("LTI platform error on %s %s", req.method, req.path)
            return HTTPError(500, "server_error", "internal error").response()

    @staticmethod
    def known(scopes: Iterable[str]) -> List[str]:
        ok = [*SCOPES, S_LINEITEM_RO]
        return [s for s in dict.fromkeys(scopes) if s in ok]

    def allowed(self, scopes: Iterable[str]) -> List[str]:
        """Drop services that are switched off *right now* (grades / roster). Tools keep the scopes
        they asked for, so turning grades back on gives them back with no re-registration."""
        return [s for s in self.known(scopes) if (self.grades or "lti-ags" not in s) and (self.roster or s != S_NRPS)]

    def _service_on(self, scopes: Iterable[str]) -> None:
        sc = list(scopes)
        if any("lti-ags" in s for s in sc) and not self.grades or S_NRPS in sc and not self.roster:
            raise HTTPError(404, "not_found", "this service is turned off")

    def _register(self, r: Router) -> None:
        r.add("GET", "/.well-known/openid-configuration", self.http_config)
        r.add("GET", "/lti/jwks", self.http_jwks)
        r.add("GET", "/lti/auth", self.http_auth)
        r.add("POST", "/lti/auth", self.http_auth)
        r.add("POST", "/lti/token", self.http_token)
        r.add("POST", "/lti/register", self.http_register)
        r.add("POST", "/lti/deep-link-return", self.http_deep_link_return)
        r.add("GET", "/lti/nrps/{ctx}", self.http_nrps)
        r.add("GET", "/lti/ags/{ctx}/lineitems", self.http_lineitems)
        r.add("POST", "/lti/ags/{ctx}/lineitems", self.http_lineitem_create)
        r.add("GET", "/lti/ags/{ctx}/lineitems/{li}", self.http_lineitem_get)
        r.add("PUT", "/lti/ags/{ctx}/lineitems/{li}", self.http_lineitem_put)
        r.add("DELETE", "/lti/ags/{ctx}/lineitems/{li}", self.http_lineitem_delete)
        r.add("POST", "/lti/ags/{ctx}/lineitems/{li}/scores", self.http_score)
        r.add("GET", "/lti/ags/{ctx}/lineitems/{li}/results", self.http_results)

    async def _call(self, fn: Optional[Callable[..., Any]], *args: Any) -> Any:
        if fn is None:
            return None
        out = fn(*args)
        return await out if inspect.isawaitable(out) else out

    async def key(self) -> Any:
        if self._key is None:
            if self._key_src:
                self._key = _load_pem(self._key_src)
            else:
                rec = await self.storage.get_record("ltip", "key")
                self._key = _load_pem(rec["pem"]) if rec else generate_key()
                if not rec:
                    await self.storage.put_record("ltip", "key", {"pem": _pem(self._key)})
            self.kid = public_jwk(self._key, "x")["n"][:16]
        return self._key

    async def _once(self, kind: str, value: str) -> None:
        if await self.storage.get_record(kind, value):
            raise HTTPError(401, "replay", "this message was already used")
        await self.storage.put_record(kind, value, {"at": time.time()})

    # -- tools ----------------------------------------------------------------------------------
    async def add_tool(self, name: str, login_url: str, launch_url: str, jwks_url: str = "", *,
                       redirect_uris: Optional[List[str]] = None, scopes: Optional[List[str]] = None,
                       deep_link_url: str = "", public_jwk: Optional[Dict[str, Any]] = None) -> Tool:
        """Register a tool by hand (the LMS admin's "add external tool" form)."""
        await self.startup()
        t = Tool("c-" + secrets.token_hex(8), "d-" + secrets.token_hex(6), name, login_url, launch_url, jwks_url,
                 redirect_uris or [launch_url], self.known(scopes or SCOPES), deep_link_url or launch_url, public_jwk)
        await self.storage.put_record("ltip_tool", t.client_id, t.to_dict())
        return t

    async def tool(self, client_id: str) -> Tool:
        await self.startup()
        rec = await self.storage.get_record("ltip_tool", client_id)
        if not rec or not rec.get("active", True):
            raise HTTPError(400, "unknown_tool", "no such tool")
        return Tool.from_dict(rec)

    async def tools(self) -> List[Tool]:
        await self.startup()
        return [Tool.from_dict(v) for _, v in await self.storage.list_records("ltip_tool")]

    async def remove_tool(self, client_id: str) -> None:
        await self.storage.delete_record("ltip_tool", client_id)

    async def _tool_keys(self, t: Tool, refresh: bool = False) -> List[Dict[str, Any]]:
        if t.public_jwk:
            return [t.public_jwk]
        hit = self._jwks.get(t.client_id)
        if hit and hit[0] > time.monotonic() and not refresh:
            return hit[1]
        res = await self.http.request("GET", t.jwks_url, expect_ok=True, what="tool JWKS")
        keys = (res.json() or {}).get("keys") or []
        self._jwks[t.client_id] = (time.monotonic() + 600, keys)
        return keys

    async def _verify_tool_jwt(self, t: Tool, token: str, audiences: Iterable[str]) -> Dict[str, Any]:
        head, claims, si, sig = jwt_parts(token)
        if head.get("alg") != "RS256":
            raise HTTPError(401, "bad_alg", "RS256 only")
        for refresh in (False, True):
            keys = [k for k in await self._tool_keys(t, refresh) if not head.get("kid") or k.get("kid") == head["kid"]]
            if keys:
                break
        if not keys:
            raise HTTPError(401, "unknown_key", "tool signed with an unknown key")
        jwt_verify_signature(si, sig, jwk_public_key(keys[0]))
        aud = claims.get("aud")
        if not set(aud if isinstance(aud, list) else [aud]) & set(audiences):
            raise HTTPError(401, "bad_audience", "wrong audience")
        if float(claims.get("exp", 0)) < time.time() - 60:
            raise HTTPError(401, "expired", "token expired")
        return claims

    # -- launching (your LMS calls these, then redirects the browser to the URL) -------------------
    async def launch(self, tool_id: str, user_id: str, roles: Iterable[str], context: Dict[str, Any],
                     resource_link: Dict[str, Any], *, custom: Optional[Dict[str, Any]] = None,
                     target: Optional[str] = None, return_url: Optional[str] = None, lineitem: Optional[str] = None) -> str:
        """URL that starts a resource-link launch for ``user_id`` (a student clicking an activity)."""
        t = await self.tool(tool_id)
        return await self._start(t, {"m": "LtiResourceLinkRequest", "u": str(user_id), "r": list(roles),
                                     "ctx": context, "rl": resource_link, "cu": custom or {}, "li": lineitem,
                                     "tg": target or t.launch_url, "ret": return_url})

    async def deep_link(self, tool_id: str, user_id: str, roles: Iterable[str], context: Dict[str, Any], *,
                        return_to: str = "") -> str:
        """URL that opens the tool's "pick something to add" page (a teacher adding an activity)."""
        t = await self.tool(tool_id)
        return await self._start(t, {"m": "LtiDeepLinkingRequest", "u": str(user_id), "r": list(roles),
                                     "ctx": context, "tg": t.deep_link_url or t.launch_url, "ret": return_to})

    async def launch_link(self, link_id: str, user_id: str, roles: Iterable[str], **kw: Any) -> str:
        """Launch an activity a teacher added through deep linking."""
        link = await self.storage.get_record("ltip_link", link_id)
        if not link:
            raise HTTPError(404, "not_found", "no such activity")
        return await self.launch(link["tool"], user_id, roles, link["context"],
                                 {"id": link_id, "title": link.get("title", "")}, custom=link.get("custom"),
                                 target=link.get("url"), lineitem=link.get("lineitem"), **kw)

    async def links(self, context_id: Optional[str] = None) -> List[Dict[str, Any]]:
        await self.startup()
        rows = await self.storage.list_records("ltip_link")
        return [{"id": k, **v} for k, v in rows if not context_id or v["context"].get("id") == context_id]

    async def _start(self, t: Tool, hint: Dict[str, Any]) -> str:
        ctx = hint["ctx"]
        rec = await self.storage.get_record("ltip_ctx", str(ctx["id"])) or {"tools": []}
        if t.client_id not in rec["tools"]:
            rec["tools"].append(t.client_id)
        await self.storage.put_record("ltip_ctx", str(ctx["id"]), {**rec, "context": ctx})
        hint = {**hint, "t": t.client_id, "j": secrets.token_urlsafe(8)}
        q = {"iss": self.issuer, "login_hint": hint["u"], "target_link_uri": hint["tg"], "client_id": t.client_id,
             "lti_deployment_id": t.deployment_id,
             "lti_message_hint": sign_blob(self.signer, hint, ttl=300, purpose="ltip-hint")}
        return t.login_url + ("&" if "?" in t.login_url else "?") + urlencode(q)

    # -- OIDC authorization endpoint (browser comes back from the tool) ----------------------------
    async def http_auth(self, req: Request) -> Response:
        q = dict(req.query)
        if req.body:
            q.update({k: v[0] for k, v in parse_qs(req.body.decode()).items()})
        t = await self.tool(q.get("client_id", ""))
        if q.get("redirect_uri") not in (t.redirect_uris or [t.launch_url]):
            raise HTTPError(400, "bad_redirect_uri", "redirect_uri is not registered for this tool")
        if q.get("response_type") != "id_token" or "openid" not in q.get("scope", "").split() or not q.get("nonce"):
            raise HTTPError(400, "invalid_request", "need response_type=id_token, scope=openid and a nonce")
        try:
            hint = verify_blob(self.signer, q.get("lti_message_hint", ""), purpose="ltip-hint")
        except Exception:  # noqa: BLE001
            raise HTTPError(400, "bad_hint", "launch expired, click the activity again") from None
        if hint["t"] != t.client_id or hint["u"] != q.get("login_hint"):
            raise HTTPError(400, "bad_hint", "launch does not match this tool or user")
        if self.authenticate:
            who = await self._call(self.authenticate, req)
            if str(who or "") != hint["u"]:
                raise HTTPError(403, "not_signed_in", "sign in to the LMS as this user first")
        else:
            await self._once("ltip_hint", hint["j"])  # without a session check, each launch link works once
        token = await self.id_token(t, hint, q["nonce"])
        return _autopost(q["redirect_uri"], {"id_token": token, "state": q.get("state", "")})

    async def id_token(self, t: Tool, hint: Dict[str, Any], nonce: str) -> str:
        key = await self.key()
        now = int(time.time())
        info = await self._call(self.user, hint["u"]) or {}
        ctx = str(hint["ctx"]["id"])
        claims: Dict[str, Any] = {
            "iss": self.issuer, "aud": t.client_id, "azp": t.client_id, "sub": hint["u"], "iat": now,
            "exp": now + 300, "nonce": nonce,
            **{k: info[k] for k in ("name", "given_name", "family_name", "email", "picture") if info.get(k)},
            C + "version": "1.3.0", C + "message_type": hint["m"], C + "deployment_id": t.deployment_id,
            C + "target_link_uri": hint["tg"], C + "roles": role_uris(hint["r"]),
            C + "context": {"type": ["http://purl.imsglobal.org/vocab/lis/v2/course#CourseOffering"], **hint["ctx"]},
            C + "tool_platform": {"guid": self.issuer, "name": self.name, "product_family_code": self.product},
            C + "launch_presentation": {"document_target": "iframe", **({"return_url": hint["ret"]} if hint.get("ret") else {})},
        }
        if S_NRPS in self.allowed(t.scopes):
            claims[NRPS] = {"context_memberships_url": f"{self.base}/lti/nrps/{ctx}", "service_versions": ["2.0"]}
        ags_scopes = [s for s in self.allowed(t.scopes) if "lti-ags" in s]
        if ags_scopes:
            claims[AGS] = {"scope": ags_scopes, "lineitems": f"{self.base}/lti/ags/{ctx}/lineitems",
                           **({"lineitem": hint["li"]} if hint.get("li") else {})}
        if hint["m"] == "LtiResourceLinkRequest":
            claims[C + "resource_link"] = hint["rl"]
            if hint.get("cu"):
                claims[C + "custom"] = hint["cu"]
        else:
            data = sign_blob(self.signer, {"t": t.client_id, "ctx": hint["ctx"], "u": hint["u"],
                                           "ret": hint.get("ret") or ""}, ttl=3600, purpose="ltip-dl")
            claims[DL + "deep_linking_settings"] = {
                "deep_link_return_url": f"{self.base}/lti/deep-link-return", "accept_types": ["ltiResourceLink"],
                "accept_presentation_document_targets": ["iframe", "window"], "accept_multiple": True, "data": data}
        return jwt_encode(claims, key, self.kid)

    # -- deep linking return --------------------------------------------------------------------
    async def http_deep_link_return(self, req: Request) -> Response:
        form = {k: v[0] for k, v in parse_qs(req.body.decode()).items()} if req.body else {}
        _, raw, _, _ = jwt_parts(form.get("JWT", ""))
        t = await self.tool(str(raw.get("iss", "")))
        claims = await self._verify_tool_jwt(t, form["JWT"], [self.issuer])
        if claims.get(C + "message_type") != "LtiDeepLinkingResponse" or claims.get(C + "deployment_id") != t.deployment_id:
            raise HTTPError(400, "bad_message", "not a deep linking response for this deployment")
        try:
            data = verify_blob(self.signer, claims.get(DL + "data", ""), purpose="ltip-dl")
        except Exception:  # noqa: BLE001
            raise HTTPError(400, "bad_data", "deep linking session expired") from None
        if data["t"] != t.client_id:
            raise HTTPError(400, "bad_data", "deep linking session belongs to another tool")
        await self._once("ltip_nonce", str(claims.get("nonce") or claims.get("jti") or form["JWT"][-40:]))
        added = []
        for item in claims.get(DL + "content_items") or []:
            if item.get("type") != "ltiResourceLink":
                continue
            lid = "l-" + secrets.token_hex(6)
            link = {"tool": t.client_id, "context": data["ctx"], "title": item.get("title", ""),
                    "url": item.get("url") or t.launch_url, "custom": item.get("custom") or {}, "by": data["u"]}
            if item.get("lineItem") and self.grades:
                li = await self._create_lineitem(str(data["ctx"]["id"]), t, {**item["lineItem"], "resourceLinkId": lid})
                link["lineitem"] = li["id"]
            await self.storage.put_record("ltip_link", lid, link)
            added.append({"id": lid, **link})
        out = await self._call(self.on_content_items, data["ctx"], data["u"], added)
        if isinstance(out, Response):
            return out
        if isinstance(out, str) or data.get("ret"):
            return _redirect(out if isinstance(out, str) else data["ret"])
        return text_response(f"<!doctype html><p>Added {len(added)} item(s). You can close this window.</p>", "text/html")

    # -- OAuth2 token endpoint for services ------------------------------------------------------
    async def http_token(self, req: Request) -> Response:
        f = {k: v[0] for k, v in parse_qs(req.body.decode()).items()} if req.body else {}
        if f.get("grant_type") != "client_credentials" or \
                f.get("client_assertion_type") != "urn:ietf:params:oauth:client-assertion-type:jwt-bearer":
            return json_response({"error": "unsupported_grant_type"}, 400)
        _, raw, _, _ = jwt_parts(f.get("client_assertion", ""))
        try:
            t = await self.tool(str(raw.get("iss", "")))
            claims = await self._verify_tool_jwt(t, f["client_assertion"], [f"{self.base}/lti/token", self.issuer])
            if claims.get("sub") != t.client_id:
                raise HTTPError(401, "bad_client", "sub must be the client id")
            await self._once("ltip_jti", f"{t.client_id}|{claims.get('jti') or f['client_assertion'][-40:]}")
        except HTTPError as exc:
            return json_response({"error": "invalid_client", "error_description": exc.message}, 401)
        granted = [s for s in self.allowed(f.get("scope", "").split()) if s in t.scopes]
        if not granted:
            return json_response({"error": "invalid_scope"}, 400)
        token = sign_blob(self.signer, {"t": t.client_id, "s": granted}, ttl=3600, purpose="ltip-at")
        return json_response({"access_token": token, "token_type": "Bearer", "expires_in": 3600,
                              "scope": " ".join(granted)})

    async def _authorize(self, req: Request, *scopes: str) -> Tool:
        self._service_on(scopes)  # switched off -> 404, as if the endpoint didn't exist
        auth = req.header("authorization")
        try:
            at = verify_blob(self.signer, auth.split(" ", 1)[1] if " " in auth else "", purpose="ltip-at")
        except Exception:  # noqa: BLE001
            raise HTTPError(401, "invalid_token", "missing or expired access token") from None
        if not set(scopes) & set(at["s"]):
            raise HTTPError(403, "insufficient_scope", "token lacks the needed scope")
        t = await self.tool(at["t"])
        ctx = await self.storage.get_record("ltip_ctx", req.match_info["ctx"])
        if not ctx or t.client_id not in ctx["tools"]:
            raise HTTPError(404, "not_found", "this tool is not used in that course")
        return t

    # -- Names and Role Provisioning -------------------------------------------------------------
    async def http_nrps(self, req: Request) -> Response:
        await self._authorize(req, S_NRPS)
        ctx_id = req.match_info["ctx"]
        everyone = list(await self._call(self.members, ctx_id) or [])
        limit = max(1, min(int(req.query.get("limit", 100)), 1000))
        start = int(req.query.get("from", 0))
        page = everyone[start:start + limit]
        members = [{"user_id": str(m["user_id"]), "roles": role_uris(m.get("roles") or ["Learner"]),
                    "status": m.get("status", "Active"),
                    **{k: m[k] for k in ("name", "given_name", "family_name", "email", "picture") if m.get(k)}}
                   for m in page]
        ctx = (await self.storage.get_record("ltip_ctx", ctx_id) or {}).get("context") or {"id": ctx_id}
        url = f"{self.base}/lti/nrps/{ctx_id}"
        headers = {}
        if start + limit < len(everyone):
            headers["Link"] = f'<{url}?{urlencode({"limit": limit, "from": start + limit})}>; rel="next"'
        body = json.dumps({"id": url, "context": ctx, "members": members}).encode()
        return Response(200, body, CT_NRPS, headers)

    # -- Assignment and Grade Services -----------------------------------------------------------
    def _li_url(self, ctx: str, li: str) -> str:
        return f"{self.base}/lti/ags/{ctx}/lineitems/{li}"

    async def _create_lineitem(self, ctx: str, t: Tool, body: Dict[str, Any]) -> Dict[str, Any]:
        if "scoreMaximum" not in body or "label" not in body:
            raise HTTPError(400, "bad_request", "scoreMaximum and label are required")
        li = "li-" + secrets.token_hex(6)
        rec = {k: body[k] for k in ("scoreMaximum", "label", "resourceId", "resourceLinkId", "tag",
                                    "startDateTime", "endDateTime") if body.get(k) is not None}
        rec.update(tool=t.client_id, ctx=ctx)
        await self.storage.put_record("ltip_lineitem", li, rec)
        return self._li_out(ctx, li, rec)

    def _li_out(self, ctx: str, li: str, rec: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": self._li_url(ctx, li), **{k: v for k, v in rec.items() if k not in ("tool", "ctx")}}

    async def _lineitem(self, req: Request, t: Tool) -> Tuple[str, Dict[str, Any]]:
        li = req.match_info["li"]
        rec = await self.storage.get_record("ltip_lineitem", li)
        if not rec or rec["ctx"] != req.match_info["ctx"] or rec["tool"] != t.client_id:
            raise HTTPError(404, "not_found", "no such line item")
        return li, rec

    async def http_lineitems(self, req: Request) -> Response:
        t = await self._authorize(req, S_LINEITEM, S_LINEITEM_RO)
        ctx = req.match_info["ctx"]
        out = []
        for li, rec in await self.storage.list_records("ltip_lineitem"):
            if rec["ctx"] != ctx or rec["tool"] != t.client_id:
                continue
            if any(req.query.get(q) and rec.get(k) != req.query[q] for q, k in
                   (("resource_link_id", "resourceLinkId"), ("resource_id", "resourceId"), ("tag", "tag"))):
                continue
            out.append(self._li_out(ctx, li, rec))
        return Response(200, json.dumps(out).encode(), CT_LINEITEMS)

    async def http_lineitem_create(self, req: Request) -> Response:
        t = await self._authorize(req, S_LINEITEM)
        out = await self._create_lineitem(req.match_info["ctx"], t, req.json())
        return Response(201, json.dumps(out).encode(), CT_LINEITEM)

    async def http_lineitem_get(self, req: Request) -> Response:
        t = await self._authorize(req, S_LINEITEM, S_LINEITEM_RO)
        li, rec = await self._lineitem(req, t)
        return Response(200, json.dumps(self._li_out(req.match_info["ctx"], li, rec)).encode(), CT_LINEITEM)

    async def http_lineitem_put(self, req: Request) -> Response:
        t = await self._authorize(req, S_LINEITEM)
        li, rec = await self._lineitem(req, t)
        b = req.json()
        rec.update({k: b[k] for k in ("scoreMaximum", "label", "resourceId", "tag", "startDateTime", "endDateTime") if k in b})
        await self.storage.put_record("ltip_lineitem", li, rec)
        return Response(200, json.dumps(self._li_out(req.match_info["ctx"], li, rec)).encode(), CT_LINEITEM)

    async def http_lineitem_delete(self, req: Request) -> Response:
        t = await self._authorize(req, S_LINEITEM)
        li, _ = await self._lineitem(req, t)
        await self.storage.delete_record("ltip_lineitem", li)
        return Response(204)

    async def http_score(self, req: Request) -> Response:
        t = await self._authorize(req, S_SCORE)
        li, rec = await self._lineitem(req, t)
        s = req.json()
        if not s.get("userId") or not s.get("timestamp") or "activityProgress" not in s or "gradingProgress" not in s:
            raise HTTPError(400, "bad_request", "userId, timestamp, activityProgress and gradingProgress are required")
        key = f"{li}|{s['userId']}"
        old = await self.storage.get_record("ltip_score", key)
        if old and old["timestamp"] >= s["timestamp"]:
            return json_response({"ignored": "older than the stored score"}, 200)
        score = {**s, "lineitem": self._li_url(req.match_info["ctx"], li), "lineitem_id": li,
                 "context_id": req.match_info["ctx"], "tool": t.client_id, "label": rec.get("label"),
                 "scoreMaximum": s.get("scoreMaximum", rec.get("scoreMaximum"))}
        await self.storage.put_record("ltip_score", key, score)
        await self._call(self.on_score, score)
        return json_response({}, 200)

    async def http_results(self, req: Request) -> Response:
        t = await self._authorize(req, S_RESULT)
        li, rec = await self._lineitem(req, t)
        url = self._li_url(req.match_info["ctx"], li)
        out = [{"id": f"{url}/results/{s['userId']}", "scoreOf": url, "userId": s["userId"],
                "resultScore": s.get("scoreGiven"), "resultMaximum": s.get("scoreMaximum"),
                **({"comment": s["comment"]} if s.get("comment") else {})}
               for k, s in await self.storage.list_records("ltip_score", f"{li}|")
               if not req.query.get("user_id") or s["userId"] == req.query["user_id"]]
        return Response(200, json.dumps(out).encode(), CT_RESULTS)

    async def gradebook(self, context_id: str) -> List[Dict[str, Any]]:
        """Every score tools sent for a course (handy for your LMS's gradebook page)."""
        await self.startup()
        return [s for _, s in await self.storage.list_records("ltip_score") if s.get("context_id") == context_id]

    # -- dynamic registration --------------------------------------------------------------------
    def registration_url(self, tool_registration_url: str, ttl: int = 3600) -> str:
        """Open this in the admin's browser (popup or iframe) to connect a tool in one click."""
        tok = sign_blob(self.signer, {"n": secrets.token_urlsafe(8)}, ttl=ttl, purpose="ltip-reg")
        q = urlencode({"openid_configuration": f"{self.base}/.well-known/openid-configuration",
                       "registration_token": tok})
        return tool_registration_url + ("&" if "?" in tool_registration_url else "?") + q

    async def http_config(self, req: Request) -> Response:
        return json_response({
            "issuer": self.issuer, "authorization_endpoint": f"{self.base}/lti/auth",
            "token_endpoint": f"{self.base}/lti/token", "jwks_uri": f"{self.base}/lti/jwks",
            "registration_endpoint": f"{self.base}/lti/register",
            "token_endpoint_auth_methods_supported": ["private_key_jwt"],
            "token_endpoint_auth_signing_alg_values_supported": ["RS256"],
            "id_token_signing_alg_values_supported": ["RS256"], "response_types_supported": ["id_token"],
            "subject_types_supported": ["public", "pairwise"], "scopes_supported": ["openid", *self.allowed([*SCOPES, S_LINEITEM_RO])],
            "claims_supported": ["iss", "aud", "sub", "name", "given_name", "family_name", "email", "picture"],
            PLATFORM_CONF: {"product_family_code": self.product, "version": "1.0",
                            "messages_supported": [{"type": "LtiResourceLinkRequest"}, {"type": "LtiDeepLinkingRequest"}],
                            "variables": []}})

    async def http_register(self, req: Request) -> Response:
        auth = req.header("authorization")
        try:
            reg = verify_blob(self.signer, auth.split(" ", 1)[1] if " " in auth else "", purpose="ltip-reg")
        except Exception:  # noqa: BLE001
            raise HTTPError(401, "invalid_token", "registration link expired") from None
        await self._once("ltip_reg", reg["n"])
        b = req.json()
        conf = b.get(TOOL_CONF) or {}
        if not b.get("initiate_login_uri") or not b.get("jwks_uri") or not b.get("redirect_uris"):
            raise HTTPError(400, "invalid_client_metadata", "initiate_login_uri, jwks_uri and redirect_uris are required")
        dl = next((m.get("target_link_uri") for m in conf.get("messages") or [] if m.get("type") == "LtiDeepLinkingRequest"), "")
        launch = conf.get("target_link_uri") or b["redirect_uris"][0]
        scopes = self.known((b.get("scope") or "").split())
        t = Tool("c-" + secrets.token_hex(8), "d-" + secrets.token_hex(6), b.get("client_name") or "Tool",
                 b["initiate_login_uri"], launch, b["jwks_uri"], list(b["redirect_uris"]), scopes, dl or launch)
        await self.storage.put_record("ltip_tool", t.client_id, t.to_dict())
        await self._call(self.on_tool_registered, t)
        return json_response({**b, "client_id": t.client_id, "scope": " ".join(self.allowed(scopes)),
                              TOOL_CONF: {**conf, "deployment_id": t.deployment_id}}, 201)

    async def http_jwks(self, req: Request) -> Response:
        k = await self.key()
        return json_response({"keys": [public_jwk(k, self.kid)]})
