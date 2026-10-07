"""Push notifications: Web Push (standard VAPID, works in Chrome/Edge/Firefox/Safari incl.
iOS home-screen apps), Firebase Cloud Messaging (Android/iOS apps), OneSignal, ntfy.

    meet.add_push()                                    # Web Push; keys generated + stored for you
    meet.add_push("fcm", "service-account.json")
    meet.add_push("onesignal", APP_ID, API_KEY)
    meet.add_push("ntfy", "https://ntfy.sh", topic_prefix="acme-")

In the browser:  await NodeMeet.push.enable({ token })   (token = any join token of the user)
Server side:     await meet.push.notify("ada", "Title", "Body", url="https://...")

Sent automatically: new / moved / cancelled bookings and reminders (to the host and the
attendee), "someone is waiting" to a room's owner (``meet.room(..., metadata={"owner": "ada"})``).
Web Push and FCM need ``pip install "nodemeet[push]"`` (cryptography).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import time
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

from .._http import HTTPError as APIError, Request, Response, Router, json_response
from .http_client import HTTPClient
from .messages import describe

if TYPE_CHECKING:
    from ..server import NodeMeet

log = logging.getLogger("nodemeet.push")


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _crypto() -> Any:
    from ..easy import need
    need("cryptography", "push", "Web Push / FCM")
    from cryptography.hazmat.primitives.asymmetric import ec
    return ec


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


# -- Web Push (RFC 8030 / 8291 / 8292) ----------------------------------------------------------
def generate_vapid_keys() -> Tuple[str, str]:
    """-> (private key, public key), both base64url (raw 32-byte scalar / 65-byte point)."""
    ec = _crypto()
    from cryptography.hazmat.primitives import serialization
    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_numbers().private_value.to_bytes(32, "big")
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64u(priv), b64u(pub)


def _private_key(raw_b64: str) -> Any:
    ec = _crypto()
    return ec.derive_private_key(int.from_bytes(unb64u(raw_b64), "big"), ec.SECP256R1())


def _public_bytes(key: Any) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt_payload(payload: bytes, p256dh: str, auth: str, *, salt: Optional[bytes] = None,
                    sender_private: Optional[Any] = None, record_size: int = 4096) -> bytes:
    """RFC 8291 aes128gcm message body (header + one record)."""
    ec = _crypto()
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ua_public = unb64u(p256dh)
    auth_secret = unb64u(auth)
    as_key = sender_private or ec.generate_private_key(ec.SECP256R1())
    as_public = _public_bytes(as_key)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = as_key.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    if len(payload) > record_size - 17 - 86:
        raise ValueError("push payload too large (max ~3.9 KB)")
    cipher = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!IB", record_size, len(as_public)) + as_public + cipher


def decrypt_payload(body: bytes, ua_private_b64: str, auth: str) -> bytes:
    """Receiver side of RFC 8291 (used by the tests; handy for debugging)."""
    ec = _crypto()
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, (_rs, idlen) = body[:16], struct.unpack("!IB", body[16:21])
    as_public, cipher = body[21:21 + idlen], body[21 + idlen:]
    ua_key = _private_key(ua_private_b64)
    ua_public = _public_bytes(ua_key)
    shared = ua_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    ikm = _hkdf(unb64u(auth), shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    plain = AESGCM(cek).decrypt(nonce, cipher, None)
    return plain.rstrip(b"\x00")[:-1]


def vapid_header(endpoint: str, private_b64: str, subject: str, *, ttl: int = 12 * 3600) -> str:
    """``Authorization: vapid t=<ES256 JWT>, k=<public key>`` (RFC 8292)."""
    ec = _crypto()
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    u = urlsplit(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + ttl, "sub": subject},
                             separators=(",", ":")).encode())
    key = _private_key(private_b64)
    r, s = decode_dss_signature(key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    jwt = f"{head}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={jwt}, k={b64u(_public_bytes(key))}"


class PushProvider:
    name = "push"
    per_device = True  # False = the provider addresses users itself (OneSignal, ntfy)

    def __init__(self, http: Optional[HTTPClient] = None) -> None:
        self.http = http or HTTPClient()

    async def send(self, target: Any, message: Dict[str, Any]) -> bool:
        """Deliver; return False if the subscription is gone (it gets deleted)."""
        raise NotImplementedError


class WebPush(PushProvider):
    name = "webpush"

    def __init__(self, private_key: Optional[str] = None, public_key: Optional[str] = None, *,
                 subject: str = "mailto:admin@localhost", http: Optional[HTTPClient] = None) -> None:
        super().__init__(http)
        self.private_key, self.public_key, self.subject = private_key, public_key, subject

    async def send(self, target: Any, message: Dict[str, Any]) -> bool:
        sub = target
        body = encrypt_payload(json.dumps(message).encode(), sub["keys"]["p256dh"], sub["keys"]["auth"])
        res = await self.http.request("POST", sub["endpoint"], data=body, headers={
            "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
            "TTL": str(message.get("ttl", 86400)), "Urgency": message.get("urgency", "normal"),
            "Authorization": vapid_header(sub["endpoint"], self.private_key or "", self.subject)})
        if res.status in (404, 410):
            return False
        if not res.ok:
            raise RuntimeError(f"web push: HTTP {res.status} {res.text[:200]}")
        return True


class FCMPush(PushProvider):
    """Firebase Cloud Messaging HTTP v1 with a service-account JSON (path, JSON string or dict)."""

    name = "fcm"

    def __init__(self, service_account: Any, *, http: Optional[HTTPClient] = None) -> None:
        super().__init__(http)
        if isinstance(service_account, str):
            service_account = json.loads(open(service_account).read() if not service_account.lstrip().startswith("{")
                                         else service_account)
        self.sa = dict(service_account)
        self.project = self.sa["project_id"]
        self._token: Optional[str] = None
        self._expires = 0.0

    def _assertion(self) -> str:
        _crypto()
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        now = int(time.time())
        head = b64u(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = b64u(json.dumps({"iss": self.sa["client_email"], "scope": "https://www.googleapis.com/auth/firebase.messaging",
                                  "aud": self.sa.get("token_uri", "https://oauth2.googleapis.com/token"),
                                  "iat": now, "exp": now + 3600}).encode())
        key = serialization.load_pem_private_key(self.sa["private_key"].encode(), password=None)
        sig = key.sign(f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{head}.{claims}.{b64u(sig)}"

    async def _auth(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        res = await self.http.request("POST", self.sa.get("token_uri", "https://oauth2.googleapis.com/token"), form={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": self._assertion()},
            expect_ok=True, what="fcm token")
        data = res.json()
        self._token, self._expires = data["access_token"], time.time() + int(data.get("expires_in", 3600))
        return self._token

    async def send(self, target: Any, message: Dict[str, Any]) -> bool:
        token = target["token"] if isinstance(target, dict) else str(target)
        msg: Dict[str, Any] = {"token": token, "notification": {"title": message["title"], "body": message["body"]},
                               "data": {k: str(v) for k, v in (message.get("data") or {}).items()}}
        if message.get("url"):
            msg["data"]["url"] = message["url"]
            msg["webpush"] = {"fcm_options": {"link": message["url"]}}
        res = await self.http.request("POST", f"https://fcm.googleapis.com/v1/projects/{self.project}/messages:send",
                                      bearer=await self._auth(), json_body={"message": msg})
        if res.status == 404 or "UNREGISTERED" in res.text:
            return False
        if not res.ok:
            raise RuntimeError(f"fcm: HTTP {res.status} {res.text[:200]}")
        return True


class OneSignalPush(PushProvider):
    """Targets your users by external id (call OneSignal.login(userId) in your app)."""

    name = "onesignal"
    per_device = False

    def __init__(self, app_id: str, api_key: str, *, http: Optional[HTTPClient] = None) -> None:
        super().__init__(http)
        self.app_id, self.api_key = app_id, api_key

    async def send(self, target: Any, message: Dict[str, Any]) -> bool:
        body: Dict[str, Any] = {"app_id": self.app_id, "target_channel": "push",
                                "include_aliases": {"external_id": [str(target)]},
                                "headings": {"en": message["title"]}, "contents": {"en": message["body"]},
                                "data": message.get("data") or {}}
        if message.get("url"):
            body["url"] = message["url"]
        await self.http.request("POST", "https://api.onesignal.com/notifications", json_body=body,
                                headers={"Authorization": f"Key {self.api_key}"}, expect_ok=True, what="onesignal")
        return True


class NtfyPush(PushProvider):
    """ntfy.sh or your self-hosted ntfy: each user subscribes to topic ``<prefix><user id>``."""

    name = "ntfy"
    per_device = False

    def __init__(self, server: str = "https://ntfy.sh", *, topic_prefix: str = "nodemeet-",
                 token: Optional[str] = None, http: Optional[HTTPClient] = None) -> None:
        super().__init__(http)
        self.server, self.prefix, self.token = server.rstrip("/"), topic_prefix, token

    def topic(self, user_id: str) -> str:
        import re
        return self.prefix + re.sub(r"[^A-Za-z0-9_-]", "_", str(user_id))[:60]

    async def send(self, target: Any, message: Dict[str, Any]) -> bool:
        headers = {"Title": message["title"].encode("ascii", "replace").decode(), "Tags": "calendar"}
        if message.get("url"):
            headers["Click"] = message["url"]
        await self.http.request("POST", f"{self.server}/{self.topic(target)}", data=message["body"].encode(),
                                headers=headers, bearer=self.token, expect_ok=True, what="ntfy")
        return True


AUTO_EVENTS = ("booking.created", "booking.rescheduled", "booking.cancelled", "booking.reminder",
               "booking.paid", "participant.waiting")


class PushService:
    def __init__(self, meet: "NodeMeet", provider: PushProvider, *, events: Iterable[str] = AUTO_EVENTS) -> None:
        self.meet, self.provider, self.events = meet, provider, set(events)
        self.sent: List[Tuple[str, Dict[str, Any]]] = []
        meet.push = self
        meet.webhooks.taps.append(self.on_event)
        meet.use(self)

    # -- keys (Web Push) ---------------------------------------------------------------------
    async def ensure_keys(self) -> None:
        p = self.provider
        if not isinstance(p, WebPush) or (p.private_key and p.public_key):
            return
        stored = await self.meet.storage.get_record("push_config", "vapid")
        if not stored:
            priv, pub = generate_vapid_keys()
            stored = {"private_key": priv, "public_key": pub}
            await self.meet.storage.put_record("push_config", "vapid", stored)
        p.private_key, p.public_key = stored["private_key"], stored["public_key"]

    # -- subscriptions -------------------------------------------------------------------------
    @staticmethod
    def _key(user_id: str, sub: Any) -> str:
        raw = sub.get("endpoint") or sub.get("token") if isinstance(sub, dict) else str(sub)
        return f"{str(user_id).lower()}|{hashlib.sha256(str(raw).encode()).hexdigest()[:24]}"

    async def subscribe(self, user_id: str, subscription: Any) -> str:
        """Store a browser PushSubscription JSON (Web Push) or {"token": FCM token}."""
        if isinstance(self.provider, WebPush):
            if not (isinstance(subscription, dict) and subscription.get("endpoint")
                    and (subscription.get("keys") or {}).get("p256dh") and subscription["keys"].get("auth")):
                raise ValueError("expected a PushSubscription: {endpoint, keys: {p256dh, auth}}")
            if not str(subscription["endpoint"]).startswith("https://"):
                raise ValueError("push endpoints must be https")
        key = self._key(user_id, subscription)
        await self.meet.storage.put_record("push_sub", key, {"user_id": str(user_id).lower(), "sub": subscription,
                                                             "at": time.time()})
        await self.meet.emit_webhook("push.subscribed", {"user_id": str(user_id), "provider": self.provider.name,
                                                         "device": key.split("|", 1)[1]})
        return key

    async def unsubscribe(self, user_id: str, subscription: Any, reason: str = "user") -> None:
        key = self._key(user_id, subscription)
        await self.meet.storage.delete_record("push_sub", key)
        await self.meet.emit_webhook("push.unsubscribed", {"user_id": str(user_id), "device": key.split("|", 1)[1],
                                                           "reason": reason})

    async def subscriptions(self, user_id: str) -> List[Tuple[str, Any]]:
        prefix = f"{str(user_id).lower()}|"
        rows = await self.meet.storage.list_records("push_sub", prefix=prefix)
        return [(k, v["sub"]) for k, v in rows]

    async def notify(self, user_id: str, title: str, body: str, *, url: Optional[str] = None,
                     data: Optional[Dict[str, Any]] = None, urgency: str = "normal") -> int:
        """Send to every device of ``user_id``; returns how many deliveries succeeded."""
        msg = {"title": title, "body": body, "url": url, "data": data or {}, "urgency": urgency,
               "icon": (await self.meet.resolve_branding()).get("favicon_url") or None}
        await self.ensure_keys()
        sent = 0
        if not self.provider.per_device:
            if await self.provider.send(user_id, msg):
                sent = 1
        else:
            for key, sub in await self.subscriptions(user_id):
                try:
                    if await self.provider.send(sub, msg):
                        sent += 1
                    else:
                        await self.unsubscribe(user_id, sub, reason="expired")
                except Exception as exc:  # noqa: BLE001
                    log.exception("push to %s failed", user_id)
                    info = {"user_id": str(user_id), "provider": self.provider.name, "title": title, "error": str(exc)[:300]}
                    await self.meet.emit_webhook("push.failed", info)
                    await self.meet.emit_webhook("integration.error", {"source": "push", **info})
        self.sent.append((str(user_id), msg))
        if sent:
            await self.meet.emit_webhook("push.sent", {"user_id": str(user_id), "provider": self.provider.name,
                                                       "title": title, "devices": sent, "event": (data or {}).get("event")})
        return sent

    # -- automatic notifications ------------------------------------------------------------------
    async def on_event(self, event: str, data: Dict[str, Any], tenant_id: Optional[str]) -> None:
        if event not in self.events or event.startswith(("push.", "integration.")):
            return
        msg = describe(event, data, base_url=self.meet.base_url)
        if not msg:
            return
        targets: List[Tuple[str, Optional[str]]] = []
        b = data.get("booking") or {}
        if b:
            av = await self.meet.storage.get_availability(b["host_id"])
            targets.append((b["host_id"], msg.get("url")))
            if event in ("booking.reminder", "booking.rescheduled", "booking.cancelled"):
                targets.append((b["attendee_email"], None))
            if av and av.host_email:
                targets.append((av.host_email, msg.get("url")))
        elif event == "participant.waiting":
            targets += [(u, msg.get("url")) for u in [data.get("owner"), *(data.get("notify") or [])] if u]
        seen = set()
        for user, url in targets:
            if user.lower() in seen:
                continue
            seen.add(user.lower())
            await self.notify(user, msg["title"], msg["text"], url=url, data={"event": event},
                              urgency="high" if event in ("participant.waiting", "booking.reminder") else "normal")

    # -- REST ---------------------------------------------------------------------------------------
    def register(self, r: Router) -> None:
        r.add("GET", "/api/push/key", self.http_key)
        r.add("POST", "/api/push/subscribe", self.http_subscribe)
        r.add("POST", "/api/push/unsubscribe", self.http_unsubscribe)

    async def http_key(self, req: Request) -> Response:
        await self.ensure_keys()
        return json_response({"provider": self.provider.name,
                              "public_key": getattr(self.provider, "public_key", None)})

    def _user(self, req: Request, body: Dict[str, Any]) -> str:
        token = body.get("token") or (req.header("authorization") or "").removeprefix("Bearer ").strip()
        try:
            return self.meet.tokens.verify(token).user_id
        except Exception:  # noqa: BLE001
            raise APIError(401, "unauthorized", "send a valid join token as 'token'") from None

    async def http_subscribe(self, req: Request) -> Response:
        body = req.json() or {}
        user = self._user(req, body)
        try:
            key = await self.subscribe(user, body.get("subscription"))
        except ValueError as exc:
            raise APIError(400, "bad_subscription", str(exc)) from None
        return json_response({"ok": True, "id": key.split("|", 1)[1], "user_id": user}, status=201)

    async def http_unsubscribe(self, req: Request) -> Response:
        body = req.json() or {}
        await self.unsubscribe(self._user(req, body), body.get("subscription"))
        return json_response({"ok": True})
