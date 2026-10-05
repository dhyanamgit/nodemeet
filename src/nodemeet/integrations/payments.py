"""Take payment before a booking is confirmed (Stripe, Razorpay or PayPal).

    from nodemeet.integrations.payments import PaymentService, StripePayments, RazorpayPayments
    PaymentService(meet, StripePayments(api_key="sk_live_...", webhook_secret="whsec_..."))
    # set a price on the host:  Availability(..., price=150000, currency="INR")

Flow: booking is created *on hold* (the slot is reserved atomically), the API
returns ``payment_url``; the provider's webhook ``POST /api/payments/<name>/webhook``
confirms it (emails, calendar, reminders start). Unpaid holds are released after
``Availability.payment_hold_minutes``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from .._http import HTTPError as APIError, Request, Response, Router, json_response
from ..models import Booking
from .http_client import HTTPClient

if TYPE_CHECKING:
    from ..scheduling.availability import Availability
    from ..server import NodeMeet

log = logging.getLogger("nodemeet.payments")
Event = Tuple[str, str, Optional[int]]  # (booking_id, payment_id, amount)


class PaymentProvider:
    name = ""

    async def create_checkout(self, booking: Booking, amount: int, currency: str, *,
                              success_url: str, cancel_url: str, description: str) -> Dict[str, str]:
        """Return {"id": ..., "url": ...}."""
        raise NotImplementedError

    def parse_webhook(self, headers: Dict[str, str], body: bytes) -> Optional[Event]:
        """Verify the signature; return the paid booking, or None for other events."""
        raise NotImplementedError


class StripePayments(PaymentProvider):
    name = "stripe"

    def __init__(self, api_key: str, webhook_secret: str, *, http: Optional[HTTPClient] = None,
                 tolerance: int = 300) -> None:
        self.api_key, self.webhook_secret, self.tolerance = api_key, webhook_secret, tolerance
        self.http = http or HTTPClient()

    async def create_checkout(self, booking: Booking, amount: int, currency: str, *,
                              success_url: str, cancel_url: str, description: str) -> Dict[str, str]:
        res = await self.http.request("POST", "https://api.stripe.com/v1/checkout/sessions", basic=(self.api_key, ""),
                                      form={"mode": "payment", "success_url": success_url, "cancel_url": cancel_url,
                                            "client_reference_id": booking.id, "customer_email": booking.attendee_email,
                                            "line_items[0][quantity]": 1,
                                            "line_items[0][price_data][currency]": currency.lower(),
                                            "line_items[0][price_data][unit_amount]": amount,
                                            "line_items[0][price_data][product_data][name]": description,
                                            "metadata[booking_id]": booking.id,
                                            "payment_intent_data[metadata][booking_id]": booking.id,
                                            "expires_at": int(time.time()) + 30 * 60 + 60},
                                      headers={"Idempotency-Key": f"nm-{booking.id}"}, expect_ok=True,
                                      what="stripe checkout")
        data = res.json() or {}
        return {"id": data["id"], "url": data["url"]}

    def parse_webhook(self, headers: Dict[str, str], body: bytes) -> Optional[Event]:
        sig = headers.get("stripe-signature", "")
        parts = dict(p.split("=", 1) for p in sig.split(",") if "=" in p)
        ts, given = parts.get("t", ""), [v for k, v in (p.split("=", 1) for p in sig.split(",") if "=" in p) if k == "v1"]
        expected = hmac.new(self.webhook_secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        if not ts or not any(hmac.compare_digest(expected, g) for g in given):
            raise ValueError("bad Stripe signature")
        if abs(time.time() - int(ts)) > self.tolerance:
            raise ValueError("stale Stripe webhook")
        event = json.loads(body)
        if event.get("type") not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            return self._other(event)
        obj = event["data"]["object"]
        if obj.get("payment_status") not in ("paid", "no_payment_required"):
            return None
        bid = (obj.get("metadata") or {}).get("booking_id") or obj.get("client_reference_id")
        return (bid, obj.get("payment_intent") or obj["id"], obj.get("amount_total"))


    @staticmethod
    def _other(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        kind, obj = event.get("type", ""), (event.get("data") or {}).get("object") or {}
        bid = (obj.get("metadata") or {}).get("booking_id") or obj.get("client_reference_id")
        if kind == "charge.refunded":
            return {"type": "payment.refunded", "booking_id": bid, "payment_id": obj.get("payment_intent") or obj.get("id"),
                    "amount": obj.get("amount_refunded"), "full": bool(obj.get("refunded")), "raw": kind}
        if kind in ("payment_intent.payment_failed", "checkout.session.async_payment_failed", "checkout.session.expired"):
            err = (obj.get("last_payment_error") or {}).get("message") or ("checkout expired" if kind.endswith("expired") else "")
            return {"type": "payment.failed", "booking_id": bid, "payment_id": obj.get("payment_intent") or obj.get("id"),
                    "reason": err, "raw": kind}
        if kind == "charge.dispute.created":
            return {"type": "payment.disputed", "booking_id": bid, "payment_id": obj.get("payment_intent") or obj.get("charge"),
                    "amount": obj.get("amount"), "reason": obj.get("reason"), "raw": kind}
        return None


class RazorpayPayments(PaymentProvider):
    """Razorpay Payment Links (UPI, cards, netbanking, wallets)."""

    name = "razorpay"

    def __init__(self, key_id: str, key_secret: str, webhook_secret: str, *,
                 http: Optional[HTTPClient] = None) -> None:
        self.key_id, self.key_secret, self.webhook_secret = key_id, key_secret, webhook_secret
        self.http = http or HTTPClient()

    async def create_checkout(self, booking: Booking, amount: int, currency: str, *,
                              success_url: str, cancel_url: str, description: str) -> Dict[str, str]:
        body = {"amount": amount, "currency": currency.upper(), "description": description[:255],
                "reference_id": booking.id[:40], "callback_url": success_url, "callback_method": "get",
                "customer": {"name": booking.attendee_name, "email": booking.attendee_email,
                             **({"contact": booking.attendee_phone} if booking.attendee_phone else {})},
                "notify": {"sms": False, "email": False}, "notes": {"booking_id": booking.id},
                "expire_by": int(time.time()) + 20 * 60}
        res = await self.http.request("POST", "https://api.razorpay.com/v1/payment_links",
                                      basic=(self.key_id, self.key_secret), json_body=body, expect_ok=True,
                                      what="razorpay payment link")
        data = res.json() or {}
        return {"id": data["id"], "url": data["short_url"]}

    def parse_webhook(self, headers: Dict[str, str], body: bytes) -> Optional[Event]:
        expected = hmac.new(self.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, headers.get("x-razorpay-signature", "")):
            raise ValueError("bad Razorpay signature")
        event = json.loads(body)
        if event.get("event") != "payment_link.paid":
            kind, pl = event.get("event", ""), event.get("payload") or {}
            if kind in ("refund.processed", "refund.created"):
                r = (pl.get("refund") or {}).get("entity") or {}
                return {"type": "payment.refunded", "booking_id": (r.get("notes") or {}).get("booking_id"),
                        "payment_id": r.get("payment_id"), "amount": r.get("amount"), "raw": kind}
            if kind == "payment.failed":
                pay = (pl.get("payment") or {}).get("entity") or {}
                return {"type": "payment.failed", "booking_id": (pay.get("notes") or {}).get("booking_id"),
                        "payment_id": pay.get("id"), "reason": pay.get("error_description"), "raw": kind}
            if kind.startswith("payment.dispute.created"):
                d = (pl.get("dispute") or {}).get("entity") or {}
                return {"type": "payment.disputed", "booking_id": None, "payment_id": d.get("payment_id"),
                        "amount": d.get("amount"), "reason": d.get("reason_code"), "raw": kind}
            return None
        link = event["payload"]["payment_link"]["entity"]
        payment = (event["payload"].get("payment") or {}).get("entity") or {}
        bid = (link.get("notes") or {}).get("booking_id") or link.get("reference_id")
        return (bid, payment.get("id") or link["id"], payment.get("amount") or link.get("amount_paid"))


_PAYPAL_ZERO_DECIMAL = {"JPY", "HUF", "TWD"}


class PayPalPayments(PaymentProvider):
    """PayPal Orders v2 (PayPal balance, cards, Venmo, Pay Later).

    Webhook: subscribe your app's webhook (``.../api/payments/paypal/webhook``) to
    ``CHECKOUT.ORDER.APPROVED`` and ``PAYMENT.CAPTURE.COMPLETED`` and pass its ``webhook_id``.
    Approved orders are captured automatically; signatures are verified with PayPal's
    verify-webhook-signature API.
    """

    name = "paypal"

    def __init__(self, client_id: str, client_secret: str, webhook_id: str, *, sandbox: bool = False,
                 http: Optional[HTTPClient] = None) -> None:
        self.client_id, self.client_secret, self.webhook_id = client_id, client_secret, webhook_id
        self.base = "https://api-m.sandbox.paypal.com" if sandbox else "https://api-m.paypal.com"
        self.http = http or HTTPClient()
        self._token: Optional[str] = None
        self._expires = 0.0

    async def _auth(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        res = await self.http.request("POST", f"{self.base}/v1/oauth2/token", basic=(self.client_id, self.client_secret),
                                      form={"grant_type": "client_credentials"}, expect_ok=True, what="paypal token")
        data = res.json()
        self._token, self._expires = data["access_token"], time.time() + int(data.get("expires_in", 3000))
        return self._token

    @staticmethod
    def amount(minor: int, currency: str) -> str:
        cur = currency.upper()
        return str(minor) if cur in _PAYPAL_ZERO_DECIMAL else f"{minor / 100:.2f}"

    async def create_checkout(self, booking: Booking, amount: int, currency: str, *,
                              success_url: str, cancel_url: str, description: str) -> Dict[str, str]:
        body = {"intent": "CAPTURE", "purchase_units": [{
            "reference_id": booking.id[:256], "custom_id": booking.id[:127], "description": description[:127],
            "amount": {"currency_code": currency.upper(), "value": self.amount(amount, currency)}}],
            "payment_source": {"paypal": {"experience_context": {
                "return_url": success_url, "cancel_url": cancel_url, "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING", "brand_name": description[:127]}}}}
        res = await self.http.request("POST", f"{self.base}/v2/checkout/orders", bearer=await self._auth(),
                                      json_body=body, headers={"PayPal-Request-Id": f"nm-{booking.id}"},
                                      expect_ok=True, what="paypal order")
        data = res.json() or {}
        url = next((l["href"] for l in data.get("links", []) if l.get("rel") in ("payer-action", "approve")), "")
        if not url:
            raise RuntimeError("paypal order has no approval link")
        return {"id": data["id"], "url": url}

    async def verify(self, headers: Dict[str, str], event: Dict[str, Any]) -> bool:
        need = ("paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id", "paypal-transmission-sig",
                "paypal-transmission-time")
        if not all(headers.get(h) for h in need):
            return False
        res = await self.http.request("POST", f"{self.base}/v1/notifications/verify-webhook-signature",
                                      bearer=await self._auth(), json_body={
                                          "auth_algo": headers["paypal-auth-algo"], "cert_url": headers["paypal-cert-url"],
                                          "transmission_id": headers["paypal-transmission-id"],
                                          "transmission_sig": headers["paypal-transmission-sig"],
                                          "transmission_time": headers["paypal-transmission-time"],
                                          "webhook_id": self.webhook_id, "webhook_event": event},
                                      expect_ok=True, what="paypal verify")
        return (res.json() or {}).get("verification_status") == "SUCCESS"

    async def parse_webhook(self, headers: Dict[str, str], body: bytes) -> Optional[Event]:  # type: ignore[override]
        event = json.loads(body)
        if not await self.verify({k.lower(): v for k, v in headers.items()}, event):
            raise ValueError("bad PayPal signature")
        kind, res = event.get("event_type"), event.get("resource") or {}
        if kind == "CHECKOUT.ORDER.APPROVED":
            cap = await self.http.request("POST", f"{self.base}/v2/checkout/orders/{res['id']}/capture",
                                          bearer=await self._auth(), json_body={},
                                          headers={"PayPal-Request-Id": f"nm-cap-{res['id']}"}, what="paypal capture")
            if cap.status == 422 and "ORDER_ALREADY_CAPTURED" in cap.text:
                return None  # PAYMENT.CAPTURE.COMPLETED will arrive / already did
            if not cap.ok:
                raise ValueError(f"paypal capture failed: HTTP {cap.status}")
            order = cap.json() or {}
            if order.get("status") != "COMPLETED":
                return None
            unit = order["purchase_units"][0]
            capture = unit["payments"]["captures"][0]
            return (capture.get("custom_id") or unit.get("custom_id") or unit.get("reference_id")
                    or (res.get("purchase_units") or [{}])[0].get("custom_id"),
                    capture["id"], self._minor(capture["amount"]))
        if kind == "PAYMENT.CAPTURE.COMPLETED" and res.get("status") == "COMPLETED":
            return (res.get("custom_id"), res["id"], self._minor(res.get("amount") or {}))
        up = next((l["href"].rstrip("/").split("/")[-1] for l in res.get("links", []) if l.get("rel") == "up"), None)
        if kind == "PAYMENT.CAPTURE.REFUNDED" or kind == "PAYMENT.REFUND.COMPLETED":
            return {"type": "payment.refunded", "booking_id": res.get("custom_id"), "payment_id": up or res.get("id"),
                    "amount": self._minor(res.get("amount") or {}), "raw": kind}
        if kind in ("PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.DECLINED", "CHECKOUT.PAYMENT-APPROVAL.REVERSED"):
            return {"type": "payment.failed", "booking_id": res.get("custom_id"), "payment_id": res.get("id"),
                    "reason": (res.get("status_details") or {}).get("reason"), "raw": kind}
        if kind == "CUSTOMER.DISPUTE.CREATED":
            tx = (res.get("disputed_transactions") or [{}])[0]
            return {"type": "payment.disputed", "booking_id": tx.get("custom"), "payment_id": tx.get("seller_transaction_id"),
                    "amount": self._minor(res.get("dispute_amount") or {}), "reason": res.get("reason"), "raw": kind}
        return None

    @staticmethod
    def _minor(amount: Dict[str, Any]) -> Optional[int]:
        if not amount.get("value"):
            return None
        v = float(amount["value"])
        return int(round(v if amount.get("currency_code", "").upper() in _PAYPAL_ZERO_DECIMAL else v * 100))


class PaymentService:
    def __init__(self, meet: "NodeMeet", provider: PaymentProvider, *,
                 success_url: Optional[str] = None, cancel_url: Optional[str] = None,
                 cancel_on_refund: bool = False) -> None:
        self.meet, self.provider = meet, provider
        self.success_url, self.cancel_url = success_url, cancel_url
        self.cancel_on_refund = cancel_on_refund
        meet.bookings.payments = self
        meet.use(self)

    async def start_checkout(self, booking: Booking, av: "Availability") -> Dict[str, str]:
        manage = self.meet.manage_url(booking)
        out = await self.provider.create_checkout(
            booking, av.price, av.currency, success_url=self.success_url or manage + "&paid=1",
            cancel_url=self.cancel_url or manage, description=f"{booking.title} - {av.host_name or av.host_id}")
        pay = dict(booking.metadata.get("payment") or {}, provider=self.provider.name,
                   checkout_id=out["id"], url=out["url"])
        booking.metadata["payment"] = pay
        await self.meet.storage.save_booking(booking)
        return out

    def register(self, r: Router) -> None:
        r.add("POST", f"/api/payments/{self.provider.name}/webhook", self.webhook)

    async def webhook(self, req: Request) -> Response:
        try:
            event = self.provider.parse_webhook(req.headers, req.body)
            if hasattr(event, "__await__"):
                event = await event
        except (ValueError, KeyError) as exc:
            raise APIError(400, "bad_webhook", str(exc)) from None
        if event is None:
            return json_response({"ignored": True})
        if isinstance(event, dict):
            return await self._other(event)
        booking_id, payment_id, amount = event
        try:
            b = await self.meet.bookings.mark_paid(booking_id, provider=self.provider.name,
                                                   payment_id=payment_id, amount=amount)
        except Exception:  # noqa: BLE001
            log.exception("payment for unknown booking %s", booking_id)
            raise APIError(404, "not_found", "unknown booking") from None
        await self.meet.storage.put_record("payment_ref", f"{self.provider.name}|{payment_id}", {"booking_id": b.id})
        return json_response({"booking": b.id, "status": b.payment.get("status")})

    async def _other(self, ev: Dict[str, Any]) -> Response:
        """Refunds, failures, disputes: update the booking's payment and tell everyone."""
        bid = ev.get("booking_id")
        if not bid and ev.get("payment_id"):
            ref = await self.meet.storage.get_record("payment_ref", f"{self.provider.name}|{ev['payment_id']}")
            bid = ref and ref.get("booking_id")
        booking = await self.meet.storage.get_booking(bid) if bid else None
        status = {"payment.refunded": "refunded" if ev.get("full", True) else "partially_refunded",
                  "payment.failed": "failed", "payment.disputed": "disputed"}[ev["type"]]
        if booking is not None:
            pay = dict(booking.payment)
            if not (status == "failed" and pay.get("status") == "paid"):
                pay["status"] = status
            pay.setdefault("history", []).append({"status": status, "at": time.time(), "amount": ev.get("amount"),
                                                  "reason": ev.get("reason"), "provider_event": ev.get("raw")})
            if status == "refunded" and ev.get("amount") is not None:
                pay["amount_refunded"] = ev["amount"]
            booking.metadata["payment"] = pay
            await self.meet.storage.save_booking(booking)
            if status == "refunded" and self.cancel_on_refund and booking.status.value != "cancelled":
                booking = await self.meet.bookings.cancel(booking.id, reason="refunded")
        data = {"provider": self.provider.name, **{k: v for k, v in ev.items() if k != "type"},
                "booking": booking.to_dict(include_secrets=False) if booking else None}
        await self.meet.emit_webhook(ev["type"], data, tenant_id=booking.tenant_id if booking else None)
        return json_response({"event": ev["type"], "booking": booking.id if booking else None})
