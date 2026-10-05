"""Mount nodemeet at /meet inside your own aiohttp app, using your own auth."""
from aiohttp import web

from nodemeet import JoinRejected, NodeMeet

meet = NodeMeet("change-me-to-a-long-random-secret", base_url="http://localhost:8080/meet")


@meet.before_join
async def check_subscription(ctx):
    if ctx.claims.meta.get("plan") == "expired":
        raise JoinRejected("Your subscription has expired")


@meet.on_join
async def audit(room, participant):
    print(f"{participant.name} joined {room.id} ({room.topology})")


async def start_call(request):
    user = {"id": "u_42", "name": "Ada"}  # replace with your session/auth lookup
    token = meet.create_token("project-7", user["id"], "host", name=user["name"],
                              meta={"plan": "pro"})
    return web.Response(text=f"<h1>My SaaS</h1>{meet.embed_room('project-7', token)}",
                        content_type="text/html")

app = web.Application()
app.router.add_get("/", start_call)
meet.mount(app, "/meet")
web.run_app(app, port=8080)
