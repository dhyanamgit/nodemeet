"""Multi-tenant API keys.

A *tenant* is one of your customers (an org/workspace). Each tenant gets its
own API keys; every room, availability and booking created with a tenant key
is stamped with that tenant id, and a key can only see and change its own
tenant's resources. The ``api_key`` given to :class:`NodeMeet` is the
*master* key (super-admin) that can manage tenants and keys.

Keys are shown once at creation and only a SHA-256 hash is stored.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from .models import fmt_dt, new_id, parse_dt, utcnow

KEY_PREFIX = "nmk_"
SCOPES: Tuple[str, ...] = (
    "rooms:read", "rooms:write", "tokens:create", "availability:write",
    "bookings:read", "bookings:write", "webhooks:manage", "roles:manage", "branding:manage",
)


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def generate_key() -> Tuple[str, str, str]:
    """Return (plaintext, display_prefix, sha256_hash)."""
    plaintext = KEY_PREFIX + secrets.token_urlsafe(32)
    return plaintext, plaintext[:12], hash_key(plaintext)


def _check_scopes(scopes: Iterable[str]) -> List[str]:
    out = sorted(set(scopes))
    for s in out:
        if s != "*" and s not in SCOPES:
            raise ValueError(f"unknown scope {s!r}; choose from {SCOPES} or '*'")
    return out or ["*"]


@dataclass
class ApiKey:
    tenant_id: str
    key_hash: str
    prefix: str
    name: str = ""
    scopes: List[str] = field(default_factory=lambda: ["*"])
    rate_limit_per_minute: Optional[int] = None
    id: str = field(default_factory=lambda: new_id("key_"))
    revoked: bool = False
    expires_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=utcnow)
    last_used_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("tenant_id is required")
        self.scopes = _check_scopes(self.scopes)

    @property
    def active(self) -> bool:
        return not self.revoked and (self.expires_at is None or self.expires_at > utcnow())

    def to_dict(self, *, include_hash: bool = True) -> Dict[str, Any]:
        d = {"id": self.id, "tenant_id": self.tenant_id, "name": self.name, "prefix": self.prefix,
             "scopes": list(self.scopes), "rate_limit_per_minute": self.rate_limit_per_minute,
             "revoked": self.revoked, "expires_at": fmt_dt(self.expires_at),
             "created_at": fmt_dt(self.created_at), "last_used_at": fmt_dt(self.last_used_at)}
        if include_hash:
            d["key_hash"] = self.key_hash
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ApiKey":
        return cls(tenant_id=d["tenant_id"], key_hash=d["key_hash"], prefix=d.get("prefix", ""),
                   name=d.get("name", ""), scopes=list(d.get("scopes") or ["*"]),
                   rate_limit_per_minute=d.get("rate_limit_per_minute"), id=d["id"],
                   revoked=bool(d.get("revoked")), expires_at=parse_dt(d.get("expires_at")),
                   created_at=parse_dt(d.get("created_at")) or utcnow(),
                   last_used_at=parse_dt(d.get("last_used_at")))


@dataclass(frozen=True)
class Principal:
    """Who is calling the API. ``tenant_id=None`` means the master key."""

    tenant_id: Optional[str]
    scopes: FrozenSet[str] = frozenset({"*"})
    key_id: Optional[str] = None

    @property
    def is_master(self) -> bool:
        return self.tenant_id is None

    def can(self, scope: str) -> bool:
        return self.is_master or "*" in self.scopes or scope in self.scopes

    def owns(self, resource_tenant: Optional[str]) -> bool:
        return self.is_master or resource_tenant == self.tenant_id


MASTER = Principal(tenant_id=None)


class RateLimiter:
    """Fixed one-minute windows per key. Uses the broker when clustered."""

    def __init__(self, broker: Any = None) -> None:
        self.broker = broker
        self._local: Dict[Tuple[str, int], int] = {}

    async def hit(self, key_id: str, limit: Optional[int]) -> bool:
        """Count one request; returns False if the key is over its limit."""
        if not limit:
            return True
        window = int(time.time() // 60)
        if self.broker is not None:
            count = await self.broker.incr(f"ratelimit:{key_id}:{window}", ttl=70)
        else:
            k = (key_id, window)
            count = self._local[k] = self._local.get(k, 0) + 1
            if len(self._local) > 10_000:  # drop old windows
                self._local = {kk: v for kk, v in self._local.items() if kk[1] >= window}
        return count <= limit


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
