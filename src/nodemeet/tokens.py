"""Signed HMAC join tokens.

Format: ``nm1.<base64url(json claims)>.<base64url(hmac-sha256)>``

Tokens are stateless: your backend mints them (``TokenSigner.create``) after
it has authenticated the user in *your* system, and the nodemeet server only
has to verify the signature. No user database is needed on the meeting side.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, FrozenSet, Iterable, Optional, Tuple, Union

from .exceptions import ExpiredToken, InvalidToken

PREFIX = "nm1"


class Role(str, Enum):
    """Names of the built-in roles. Any other role name (custom roles) is a plain str."""

    HOST = "host"
    PARTICIPANT = "participant"
    VIEWER = "viewer"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def parse(cls, value: Union[str, "Role"]) -> str:
        """Normalise a role name (built-in or custom) to a plain lower-case string."""
        name = (value.value if isinstance(value, Role) else str(value)).strip().lower()
        if not _ROLE_NAME.match(name):
            raise ValueError(f"invalid role name {value!r}")
        return name


_ROLE_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def permissions_for(role: Union[str, Role]) -> FrozenSet[str]:
    """Permissions of a *built-in* role as shipped (custom roles: use RoleRegistry)."""
    from .permissions import builtin_roles
    r = builtin_roles().get(Role.parse(role))
    return frozenset(r.permissions) if r else frozenset()


class _RolePerms(dict):
    def __missing__(self, key: Any) -> FrozenSet[str]:
        return permissions_for(key)


# Backwards compatible view: ROLE_PERMISSIONS["host"] -> frozenset(...)
ROLE_PERMISSIONS: Dict[Any, FrozenSet[str]] = _RolePerms()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


@dataclass(frozen=True)
class TokenClaims:
    room: str
    user_id: str
    role: str
    name: Optional[str] = None
    exp: Optional[int] = None
    iat: int = 0
    nonce: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)
    tenant: Optional[str] = None
    grant: Tuple[str, ...] = ()  # extra permissions for this one user
    revoke: Tuple[str, ...] = ()

    @property
    def permissions(self) -> FrozenSet[str]:
        """Built-in permissions + token grants/revokes (custom roles: resolved at join)."""
        return frozenset((set(permissions_for(self.role)) | set(self.grant)) - set(self.revoke))

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"room": self.room, "sub": self.user_id, "role": str(self.role),
                             "iat": self.iat, "nonce": self.nonce}
        if self.name is not None:
            d["name"] = self.name
        if self.exp is not None:
            d["exp"] = self.exp
        if self.meta:
            d["meta"] = self.meta
        if self.tenant:
            d["tid"] = self.tenant
        if self.grant:
            d["grant"] = list(self.grant)
        if self.revoke:
            d["revoke"] = list(self.revoke)
        return d


class TokenSigner:
    """Create and verify join tokens.

    >>> signer = TokenSigner("change-me")
    >>> tok = signer.create("standup", "u_42", role="host", name="Ada")
    >>> signer.verify(tok, room="standup").role
    'host'

    ``room="*"`` creates a wildcard token valid for any room (use sparingly).
    """

    def __init__(self, secret: Union[str, bytes], *, default_ttl: Optional[int] = 3600,
                 leeway: int = 30, clock: Callable[[], float] = time.time) -> None:
        if not secret:
            raise ValueError("a non-empty secret is required")
        self._key = secret.encode() if isinstance(secret, str) else bytes(secret)
        if len(self._key) < 16:
            import warnings
            warnings.warn("nodemeet secret is shorter than 16 bytes; use a long random value",
                          stacklevel=2)
        self.default_ttl = default_ttl
        self.leeway = leeway
        self.clock = clock

    def _sign(self, payload: str) -> str:
        mac = hmac.new(self._key, f"{PREFIX}.{payload}".encode(), hashlib.sha256).digest()
        return _b64e(mac)

    def create(self, room: str, user_id: str, role: Union[str, Role] = Role.PARTICIPANT, *,
               name: Optional[str] = None, ttl: Optional[int] = None,
               meta: Optional[Dict[str, Any]] = None, tenant: Optional[str] = None,
               grant: Iterable[str] = (), revoke: Iterable[str] = ()) -> str:
        if not room or not user_id:
            raise ValueError("room and user_id are required")
        now = int(self.clock())
        ttl = self.default_ttl if ttl is None else ttl
        claims = TokenClaims(room=str(room), user_id=str(user_id), role=Role.parse(role),
                             name=name, exp=(now + ttl) if ttl else None, iat=now,
                             nonce=secrets.token_hex(6), meta=dict(meta or {}), tenant=tenant,
                             grant=tuple(grant), revoke=tuple(revoke))
        if claims.grant or claims.revoke:
            from .permissions import validate_permissions
            validate_permissions(claims.grant)
            validate_permissions(claims.revoke)
        body = json.dumps(claims.to_dict(), separators=(",", ":"), sort_keys=True)
        payload = _b64e(body.encode())
        return f"{PREFIX}.{payload}.{self._sign(payload)}"

    def verify(self, token: str, *, room: Optional[str] = None) -> TokenClaims:
        if not isinstance(token, str):
            raise InvalidToken("token must be a string")
        parts = token.strip().split(".")
        if len(parts) != 3 or parts[0] != PREFIX:
            raise InvalidToken("malformed token")
        _, payload, sig = parts
        if not hmac.compare_digest(self._sign(payload), sig):
            raise InvalidToken("bad signature")
        try:
            data = json.loads(_b64d(payload))
            claims = TokenClaims(room=str(data["room"]), user_id=str(data["sub"]),
                                 role=Role.parse(data["role"]), name=data.get("name"),
                                 exp=data.get("exp"), iat=int(data.get("iat", 0)),
                                 nonce=str(data.get("nonce", "")), meta=data.get("meta") or {},
                                 tenant=data.get("tid"), grant=tuple(data.get("grant") or ()),
                                 revoke=tuple(data.get("revoke") or ()))
        except (ValueError, KeyError, TypeError) as exc:
            raise InvalidToken(f"malformed claims: {exc}") from None
        if claims.exp is not None and self.clock() > claims.exp + self.leeway:
            raise ExpiredToken("token expired")
        if room is not None and claims.room not in (room, "*"):
            raise InvalidToken("token is for a different room")
        return claims


def sign_blob(signer: "TokenSigner", payload: Dict[str, Any], ttl: int = 600, purpose: str = "state") -> str:
    """Short-lived signed blob (OAuth state, SSO sessions, admin sessions)."""
    import hashlib
    import hmac
    body = dict(payload, _p=purpose, _exp=int(time.time()) + int(ttl))
    raw = _b64e(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64e(hmac.new(signer._key, f"{purpose}.{raw}".encode(), hashlib.sha256).digest())
    return f"nms1.{raw}.{sig}"


def verify_blob(signer: "TokenSigner", blob: str, purpose: str = "state") -> Dict[str, Any]:
    import hashlib
    import hmac
    try:
        prefix, raw, sig = str(blob).split(".")
    except ValueError:
        raise InvalidToken("malformed") from None
    good = _b64e(hmac.new(signer._key, f"{purpose}.{raw}".encode(), hashlib.sha256).digest())
    if prefix != "nms1" or not hmac.compare_digest(good, sig):
        raise InvalidToken("bad signature")
    data = json.loads(_b64d(raw))
    if data.get("_p") != purpose or int(data.get("_exp", 0)) < time.time():
        raise InvalidToken("expired")
    return {k: v for k, v in data.items() if not k.startswith("_")}
