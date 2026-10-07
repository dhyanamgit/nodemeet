"""Single sign-on: OpenID Connect (Google, Microsoft Entra ID, Okta, Auth0, Keycloak,
any OIDC IdP), GitHub OAuth, and SAML 2.0 (via the free ``python3-saml`` package).

    from nodemeet.sso import SSOService, OIDCProvider
    sso = SSOService(meet, [OIDCProvider.google(CLIENT_ID, SECRET),
                            OIDCProvider.microsoft(CLIENT_ID, SECRET, tenant="contoso.onmicrosoft.com")],
                     role_for=lambda user, room: "host" if user["email"].endswith("@acme.com") else "participant",
                     admin_emails=["it@acme.com"])

* ``/sso/<provider>/login?room=standup`` -> sign in -> straight into the meeting.
* Rooms with ``sso_required="google"`` send anyone without a link through SSO first.
* ``/sso/<provider>/login?admin=1`` signs into the admin dashboard.

Security note: identities come from the provider's *userinfo* endpoint over TLS
(OIDC Core 3.1.3.7 allows this instead of verifying the ID token signature), with
PKCE and a signed, expiring ``state``.
"""
from __future__ import annotations

import base64
import hashlib
import inspect
import logging
import secrets
from html import escape
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, Optional, Sequence
from urllib.parse import urlencode

from ._http import HTTPError as APIError, Request, Response, Router, text_response
from .integrations.http_client import HTTPClient
from .tokens import sign_blob, verify_blob

if TYPE_CHECKING:
    from .server import NodeMeet

log = logging.getLogger("nodemeet.sso")
User = Dict[str, Any]  # {"id", "email", "name", "groups", "provider", "raw"}


class OIDCProvider:
    def __init__(self, name: str, client_id: str, client_secret: str, *, issuer: Optional[str] = None,
                 authorization_endpoint: Optional[str] = None, token_endpoint: Optional[str] = None,
                 userinfo_endpoint: Optional[str] = None, scopes: Sequence[str] = ("openid", "email", "profile"),
                 extra_auth_params: Optional[Dict[str, str]] = None, groups_claim: str = "groups",
                 http: Optional[HTTPClient] = None) -> None:
        self.name, self.client_id, self.client_secret = name, client_id, client_secret
        self.issuer = issuer.rstrip("/") if issuer else None
        self.endpoints = {"authorization_endpoint": authorization_endpoint, "token_endpoint": token_endpoint,
                          "userinfo_endpoint": userinfo_endpoint}
        self.scopes, self.extra, self.groups_claim = list(scopes), dict(extra_auth_params or {}), groups_claim
        self.http = http or HTTPClient()

    # presets -----------------------------------------------------------------------------
    @classmethod
    def google(cls, client_id: str, client_secret: str, **kw: Any) -> "OIDCProvider":
        return cls("google", client_id, client_secret, issuer="https://accounts.google.com", **kw)

    @classmethod
    def microsoft(cls, client_id: str, client_secret: str, tenant: str = "common", **kw: Any) -> "OIDCProvider":
        base = f"https://login.microsoftonline.com/{tenant}"
        return cls("microsoft", client_id, client_secret, authorization_endpoint=f"{base}/oauth2/v2.0/authorize",
                   token_endpoint=f"{base}/oauth2/v2.0/token",
                   userinfo_endpoint="https://graph.microsoft.com/oidc/userinfo", **kw)

    @classmethod
    def okta(cls, domain: str, client_id: str, client_secret: str, **kw: Any) -> "OIDCProvider":
        return cls("okta", client_id, client_secret, issuer=f"https://{domain}", **kw)

    @classmethod
    def auth0(cls, domain: str, client_id: str, client_secret: str, **kw: Any) -> "OIDCProvider":
        return cls("auth0", client_id, client_secret, issuer=f"https://{domain}", **kw)

    @classmethod
    def keycloak(cls, base_url: str, realm: str, client_id: str, client_secret: str, **kw: Any) -> "OIDCProvider":
        return cls("keycloak", client_id, client_secret, issuer=f"{base_url.rstrip('/')}/realms/{realm}", **kw)

    @classmethod
    def github(cls, client_id: str, client_secret: str, **kw: Any) -> "OIDCProvider":
        return GitHubProvider(client_id, client_secret, **kw)

    async def discover(self) -> Dict[str, str]:
        if all(self.endpoints.values()):
            return self.endpoints  # type: ignore[return-value]
        if not self.issuer:
            raise RuntimeError(f"{self.name}: give an issuer or all three endpoints")
        res = await self.http.request("GET", f"{self.issuer}/.well-known/openid-configuration",
                                      expect_ok=True, what="OIDC discovery")
        conf = res.json() or {}
        for k in self.endpoints:
            self.endpoints[k] = self.endpoints[k] or conf.get(k)
        return self.endpoints  # type: ignore[return-value]

    async def login_url(self, state: str, redirect_uri: str, verifier: str) -> str:
        ep = await self.discover()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        q = {"client_id": self.client_id, "response_type": "code", "redirect_uri": redirect_uri,
             "scope": " ".join(self.scopes), "state": state, "code_challenge": challenge,
             "code_challenge_method": "S256", **self.extra}
        return f"{ep['authorization_endpoint']}?{urlencode(q)}"

    async def user(self, code: str, redirect_uri: str, verifier: str) -> User:
        ep = await self.discover()
        res = await self.http.request("POST", ep["token_endpoint"], form={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": self.client_id, "client_secret": self.client_secret, "code_verifier": verifier},
            expect_ok=True, what=f"{self.name} token")
        token = (res.json() or {})["access_token"]
        info = (await self.http.request("GET", ep["userinfo_endpoint"], bearer=token, expect_ok=True,
                                        what=f"{self.name} userinfo")).json() or {}
        return self.map_user(info, token)

    def map_user(self, info: Dict[str, Any], token: str = "") -> User:
        groups = info.get(self.groups_claim) or []
        return {"id": str(info.get("sub") or info.get("id")), "email": (info.get("email") or "").lower(),
                "email_verified": info.get("email_verified", True), "name": info.get("name") or info.get("email") or "",
                "groups": list(groups) if isinstance(groups, (list, tuple)) else [groups],
                "provider": self.name, "raw": info}


class GitHubProvider(OIDCProvider):
    """GitHub is OAuth 2 (not OIDC); emails come from /user/emails."""

    def __init__(self, client_id: str, client_secret: str, **kw: Any) -> None:
        super().__init__("github", client_id, client_secret, scopes=("read:user", "user:email"),
                         authorization_endpoint="https://github.com/login/oauth/authorize",
                         token_endpoint="https://github.com/login/oauth/access_token",
                         userinfo_endpoint="https://api.github.com/user", **kw)

    async def user(self, code: str, redirect_uri: str, verifier: str) -> User:
        res = await self.http.request("POST", self.endpoints["token_endpoint"], form={  # type: ignore[arg-type]
            "code": code, "redirect_uri": redirect_uri, "client_id": self.client_id,
            "client_secret": self.client_secret, "code_verifier": verifier}, expect_ok=True, what="github token")
        token = (res.json() or {})["access_token"]
        info = (await self.http.request("GET", "https://api.github.com/user", bearer=token, expect_ok=True)).json() or {}
        if not info.get("email"):
            emails = (await self.http.request("GET", "https://api.github.com/user/emails", bearer=token)).json() or []
            primary = next((e for e in emails if e.get("primary") and e.get("verified")), None)
            info["email"] = primary["email"] if primary else ""
        info["sub"] = info.get("id")
        info["name"] = info.get("name") or info.get("login")
        return self.map_user(info, token)


class SAMLProvider:
    """SAML 2.0 (Okta, Entra ID, ADFS, Google Workspace, OneLogin, Shibboleth...) using
    ``pip install python3-saml``. ``settings`` is python3-saml's settings dict; the ACS URL
    is ``{base_url}/sso/<name>/acs``."""

    saml = True

    def __init__(self, name: str, settings: Dict[str, Any], *, email_attribute: str = "email",
                 name_attribute: str = "displayName", groups_attribute: str = "groups") -> None:
        self.name, self.settings = name, settings
        self.attrs = (email_attribute, name_attribute, groups_attribute)

    def _auth(self, req: Request) -> Any:
        try:
            from onelogin.saml2.auth import OneLogin_Saml2_Auth
        except ImportError:
            raise RuntimeError("SAML needs `pip install python3-saml`") from None
        from urllib.parse import parse_qs
        post = {k: v[0] for k, v in parse_qs(req.body.decode()).items()} if req.body else {}
        data = {"https": "on" if req.scheme == "https" else "off", "http_host": req.host or req.header("host"),
                "script_name": req.prefix + req.path, "get_data": dict(req.query), "post_data": post}
        return OneLogin_Saml2_Auth(data, self.settings)

    def login_url(self, req: Request, state: str) -> str:
        return self._auth(req).login(return_to=state)

    def user(self, req: Request) -> tuple:
        auth = self._auth(req)
        auth.process_response()
        if auth.get_errors() or not auth.is_authenticated():
            raise APIError(401, "saml_failed", auth.get_last_error_reason() or "SAML login failed")
        a = auth.get_attributes()
        email_a, name_a, groups_a = self.attrs
        email = (a.get(email_a) or [auth.get_nameid()])[0]
        user = {"id": auth.get_nameid(), "email": str(email).lower(), "email_verified": True,
                "name": (a.get(name_a) or [email])[0], "groups": list(a.get(groups_a) or []),
                "provider": self.name, "raw": a}
        return user, _relay(req)


def _relay(req: Request) -> str:
    from urllib.parse import parse_qs
    return (parse_qs(req.body.decode()).get("RelayState") or [""])[0] if req.body else ""


RoleFor = Callable[[User, Optional[str]], Any]


class SSOService:
    def __init__(self, meet: "NodeMeet", providers: Iterable[Any], *, role_for: Optional[RoleFor] = None,
                 admin_emails: Iterable[str] = (), is_admin: Optional[Callable[[User], Any]] = None,
                 allowed_domains: Iterable[str] = (), session_hours: int = 8,
                 on_login: Optional[Callable[[User], Any]] = None) -> None:
        self.meet = meet
        self.providers = {p.name: p for p in providers}
        self.role_for = role_for
        self.admin_emails = {e.lower() for e in admin_emails}
        self.is_admin = is_admin
        self.allowed_domains = {d.lower().lstrip("@") for d in allowed_domains}
        self.session_hours = session_hours
        self.on_login = on_login
        meet.sso = self
        meet.use(self)

    def register(self, r: Router) -> None:
        r.add("GET", "/sso/{provider}/login", self.login)
        r.add("GET", "/sso/{provider}/callback", self.callback)
        r.add("POST", "/sso/{provider}/acs", self.acs)

    def redirect_uri(self, name: str) -> str:
        return f"{self.meet.base_url}/sso/{name}/callback"

    def _provider(self, req: Request) -> Any:
        p = self.providers.get(req.match_info["provider"])
        if p is None:
            raise APIError(404, "unknown_provider", "no such SSO provider")
        return p

    async def login(self, req: Request) -> Response:
        p = self._provider(req)
        verifier = secrets.token_urlsafe(48)
        state = sign_blob(self.meet.tokens, {"room": req.query.get("room"), "admin": req.query.get("admin") == "1",
                                             "next": req.query.get("next", ""), "v": verifier, "p": p.name},
                          ttl=600, purpose="sso")
        url = p.login_url(req, state) if getattr(p, "saml", False) else await p.login_url(
            state, self.redirect_uri(p.name), verifier)
        return Response(302, b"", "text/plain", {"Location": url})

    async def callback(self, req: Request) -> Response:
        p = self._provider(req)
        if req.query.get("error"):
            return text_response(f"Sign-in cancelled: {escape(req.query['error'])}", "text/html", 401)
        state = self._state(req.query.get("state", ""))
        user = await p.user(req.query.get("code", ""), self.redirect_uri(p.name), state["v"])
        return await self._finish(user, state)

    async def acs(self, req: Request) -> Response:
        p = self._provider(req)
        user, relay = p.user(req)
        return await self._finish(user, self._state(relay))

    def _state(self, raw: str) -> Dict[str, Any]:
        try:
            return verify_blob(self.meet.tokens, raw, purpose="sso")
        except Exception:  # noqa: BLE001
            raise APIError(400, "bad_state", "this sign-in link expired, please try again") from None

    async def _call(self, fn: Optional[Callable[..., Any]], *args: Any) -> Any:
        if fn is None:
            return None
        out = fn(*args)
        return await out if inspect.isawaitable(out) else out

    async def _finish(self, user: User, state: Dict[str, Any]) -> Response:
        email = user.get("email", "")
        if self.allowed_domains and email.split("@")[-1] not in self.allowed_domains:
            raise APIError(403, "domain_not_allowed", "your account's domain is not allowed here")
        if not user.get("email_verified", True):
            raise APIError(403, "email_unverified", "verify your email address with the provider first")
        await self._call(self.on_login, user)
        await self.meet.emit_webhook("sso.login", {"provider": user.get("provider"), "email": email,
                                                   "name": user.get("name"), "groups": user.get("groups") or [],
                                                   "admin": bool(state.get("admin")), "room": state.get("room")})
        if state.get("admin"):
            ok = email in self.admin_emails or bool(await self._call(self.is_admin, user))
            if not ok:
                raise APIError(403, "not_admin", "this account is not an administrator")
            session = sign_blob(self.meet.tokens, {"email": email, "tenant": None, "sso": user["provider"]},
                                ttl=self.session_hours * 3600, purpose="admin")
            return Response(302, b"", "text/plain", {"Location": f"{self.meet.base_url}/admin#session={session}"})
        room = state.get("room")
        if not room:
            nxt = state.get("next") or "/"
            return Response(302, b"", "text/plain", {"Location": nxt if nxt.startswith("/") else "/"})
        role = await self._call(self.role_for, user, room) if self.role_for else "participant"
        if not role:
            raise APIError(403, "not_allowed", "your account may not join this meeting")
        cfg = await self.meet.storage.get_room(room)
        token = self.meet.create_token(room, email or f"{user['provider']}:{user['id']}", str(role),
                                       name=user.get("name") or email, tenant=cfg.tenant_id if cfg else None,
                                       meta={"sso": user["provider"], "email": email, "groups": user.get("groups", [])})
        return Response(302, b"", "text/plain", {"Location": self.meet.join_url(room, token)})
