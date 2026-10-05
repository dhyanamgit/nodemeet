"""Verify nodemeet webhooks in your backend."""
from aiohttp import web

from nodemeet import verify_signature

SECRET = "webhook-secret"


async def receive(request):
    body = await request.read()
    if not verify_signature(SECRET, body, request.headers.get("X-NodeMeet-Signature", "")):
        raise web.HTTPUnauthorized()
    event = await request.json()
    print(event["event"], event["data"])
    return web.Response(text="ok")

app = web.Application()
app.router.add_post("/hooks/nodemeet", receive)
web.run_app(app, port=9000)
