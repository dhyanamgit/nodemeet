import json

from helpers import run
from nodemeet import WebhookDispatcher, sign_payload, verify_signature
from nodemeet.integrations.webhooks import EVENT_HEADER, SIGNATURE_HEADER


def test_sign_and_verify():
    body = b'{"a":1}'
    header = sign_payload("s3cret", body, timestamp=1000)
    assert verify_signature("s3cret", body, header, now=1010)
    assert not verify_signature("wrong", body, header, now=1010)
    assert not verify_signature("s3cret", body + b" ", header, now=1010)
    assert not verify_signature("s3cret", body, header, now=5000)  # too old
    assert not verify_signature("s3cret", body, "garbage")


def test_dispatch_filters_retries_and_signs():
    calls = []
    status = {"n": 0}

    async def transport(url, body, headers, timeout):
        calls.append((url, body, headers))
        status["n"] += 1
        return 500 if (url.endswith("/flaky") and status["n"] == 1) else 200

    async def scenario():
        d = WebhookDispatcher(transport=transport, backoff=0)
        d.add("https://a.test/flaky", "k1")
        d.add("https://b.test/only-bookings", "k2", events=["booking.created"])
        res = await d.emit("room.created", {"id": "r1"}, wait=True)
        assert res == [True]
        res = await d.emit("booking.created", {"id": "b1"}, wait=True)
        assert res == [True, True]

    run(scenario())
    assert len(calls) == 4  # one retry on the flaky endpoint
    url, body, headers = calls[0]
    assert headers[EVENT_HEADER] == "room.created"
    assert verify_signature("k1", body, headers[SIGNATURE_HEADER])
    assert json.loads(body)["data"] == {"id": "r1"}


def test_background_emit_and_failures_do_not_raise():
    async def transport(url, body, headers, timeout):
        raise ConnectionError("down")

    async def scenario():
        d = WebhookDispatcher(transport=transport, retries=1, backoff=0)
        d.add("https://down.test", "k")
        assert await d.emit("chat.message", {}) == []
        await d.close()
        assert await d.emit("chat.message", {}, wait=True) == [False]

    run(scenario())
