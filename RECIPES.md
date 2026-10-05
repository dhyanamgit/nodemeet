# nodemeet recipes

Short snippets you can copy and paste. Lines with `await` run inside async code (a route or a startup hook).
Everything is `meet.<something>(...)`. If you forget a name, run `nodemeet.help()` or see `CHEATSHEET.md`.

## Basics
**1. Run a meeting server**
```python
from nodemeet import NodeMeet
meet = NodeMeet()
print(meet.room("standup").host_link("Ada"))   # guests: meet.guest_link("standup", "Bob")
meet.run()
```
**2. Keep data and send real email**
```python
meet = NodeMeet(db="meet.db", email="smtp://you@gmail.com:APP_PASSWORD@smtp.gmail.com:587",
                secret="long-random-string")    # or put all of this in nodemeet.toml / .env
```
**3. Start from a ready-made project** with `nodemeet init myapp --template basic|fastapi|booking`. Settings live in `nodemeet.toml`; load them with `NodeMeet.from_config()`.
**4. FastAPI / Starlette / Django / Quart**
```python
meet = NodeMeet.from_config("nodemeet.toml", base_url="https://you.com/meet")
app.mount("/meet", meet.asgi())                 # aiohttp: meet.mount(aiohttp_app, "/meet")
```
**5. Your backend hands out links**
```python
@app.get("/join/{room}")
def join(room: str, user=Depends(current_user)):
    return {"url": meet.link(room, user.name, user_id=user.id)}
```

## Frontend
**6. Embed anywhere**
```html
<script src="https://you.com/meet/static/embed.js" async></script>
<nodemeet-room token="TOKEN_FROM_YOUR_BACKEND" height="640px"></nodemeet-room>
```
Or from Python, put `meet.room("standup").embed("Ada")` in your page template. It returns an `<iframe>`.
**7. Ready-made UI in your page** (`base` and `token` are found automatically)
```html
<link rel="stylesheet" href="/meet/static/nodemeet.css">
<script src="/meet/static/nodemeet.js"></script><script src="/meet/static/nodemeet-ui.js"></script>
<div id="call" style="height:600px"></div>
<script>NodeMeet.UI.mount('#call', { token: TOKEN });</script>
```
**8. Fully custom UI**
```js
const c = new NodeMeet.Client(TOKEN);
c.on('stream', ({ peerId, stream }) => attach(peerId, stream));
c.on('chat', (m) => show(m.name, m.text));
await c.join();
```

## Rooms & the waiting room (on by default)
**9. Pick a preset**
```python
meet.room("all-hands", preset="webinar")     # open, private, classroom, interview, 1on1, support...
meet.room("lobby-free", preset="open")       # nobody waits
```
**10. Who skips the waiting room**
```python
room = meet.room("class-1", join_list=["ravi@x.com", "u_42"])
await room.let_in("neha@x.com")                          # later, from async code
meet.link("class-1", "Guest", skip_waiting_room=True)    # a one-off pass
```
**11. Turn the waiting room off everywhere** with `NodeMeet(waiting_room=False)`.
**12. Admit people from your own UI**
```js
c.on('lobby-update', (list) => render(list));       // moderators only
c.admit(peerId); c.admitAlways(peerId); c.deny(peerId); c.block(peerId); c.admitAll();
```
**13. Blocked people**
```python
@meet.on("blocked")
async def alert(room, event):
    await notify_admin(f"{event['name']} tried to rejoin {room.id}")
await meet.room("class-1").unblock("u_42")
```

## Roles & permissions (plain words)
**14. A custom role** (`meet.role(...)` returns the role's name)
```python
meet.role("teacher", can="moderate, record", badge="🎓", rank=50, color="#22c55e")
meet.link("class-1", "Ms Rao", "teacher")
```
**15. Edit or delete built-in roles**
```python
meet.role("viewer", can="chat")                # viewers may now chat
meet.role("participant", cannot="screen")      # nobody but hosts shares screens
await meet.delete_role("viewer")
```
**16. One person, different rules** with `meet.link("class-1", "Sam", can="screen", cannot="chat", ttl="2h")`.
**17. Gate joins**
```python
from nodemeet.exceptions import JoinRejected
@meet.on("before_join")
async def check(ctx):
    if not await is_paid(ctx.claims.user_id): raise JoinRejected("Subscription required")
```
**18. Filter chat**
```python
@meet.on("chat")
def clean(room, person, text):
    return False if "badword" in text else text
```

## Branding
**19. Brand every page and email**
```python
meet.brand(name="Acme Meet", color="#7c3aed", logo="https://acme.com/logo.svg", font="Inter",
           theme="light", white_label=True, whiteboard=False)
```
**20. Per room** with `meet.room("vip", branding={"color": "#d4af37", "name": "VIP Lounge"})`. **Per customer**: `PUT /api/branding` with that customer's API key.

## Scheduling
**21. Working hours plus a booking page**
```python
page = meet.hours("dr-lee", "mon-fri 9am-12pm, 2pm-5pm", timezone="Asia/Kolkata",
                  minutes=30, buffer=10, notice="2h", email="lee@clinic.com")
print(page.url)                                   # or page.embed() in your HTML
```
**22. Find a slot and book from code**
```python
slots = await meet.slots("dr-lee", days=7)
b = await meet.book("dr-lee", slots[0], name="Ravi", email="ravi@x.com")   # or "2026-10-05 10:30"
await meet.reschedule(b, slots[3]);  await meet.cancel(b)
```
**23. Paid bookings**
```python
meet.add_payments("stripe")                       # reads STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET
meet.hours("coach", "mon-fri 10-18", price="₹999")
```
**24. Calendars, SMS and webhooks**
```python
meet.add_calendars(google=True, outlook=True)     # reads GOOGLE_CLIENT_ID / MICROSOFT_CLIENT_ID ...
meet.add_sms("twilio")                            # reads TWILIO_ACCOUNT_SID / _AUTH_TOKEN / _FROM
meet.add_webhook("https://you.com/hooks", "whsec_123")
```

## Databases (just a URL)
**25. Common ones**: `NodeMeet(db="postgres://u:p@host/app")`, `"mysql://..."`, `"mongodb://host/app"`, `"redis://host"`.
**26. Unusual ones**: `"mssql://"`, `"oracle://"`, `"snowflake://acct/db/schema"`, `"clickhouse://"`, `"cassandra://h1,h2/ks"`, `"dynamodb://table?region=ap-south-1"`, `"neo4j://"`, `"arangodb://"`, `"firestore://project"`, `"etcd://"`, `"lmdb:///data.lmdb"`, `"dir:///data"`. The full table is in `CHEATSHEET.md`.
**27. Your own database**: subclass `KVBackend` (get/put/delete/scan/put_if_absent) and pass `KeyValueStorage(YourKV())` as `db=`.

## Production
**28. Settings from the environment**: `NodeMeet.from_env()` reads `NODEMEET_SECRET`, `NODEMEET_DB`, `NODEMEET_EMAIL`, `NODEMEET_REDIS_URL`, `NODEMEET_PAYMENTS=stripe`, `NODEMEET_SSO=google,github`... and `.env`.
**29. Many servers**: `NodeMeet(redis="redis://redis:6379/0", db="postgres://...", trust_proxy=True)`. Every server needs the same secret. See `examples/cluster`.
**30. Single sign-on**: `meet.add_sso(google=True, admins=["me@acme.com"], domains=["acme.com"])`.
**31. CLI**
- `nodemeet init`: start a project.
- `nodemeet serve`: reads `nodemeet.toml`.
- `nodemeet link standup Ada --host`: print a join link.
- `nodemeet doctor`: check your setup.
- `nodemeet keys create --tenant acme`: create a customer API key.

## More integrations (0.6)
**32. Team chat**
```python
meet.add_chat("slack", "https://hooks.slack.com/services/...")            # new/moved/cancelled bookings...
meet.add_chat("discord", DISCORD_URL, events=["booking.*", "participant.waiting"])
```
**33. CRM**: `meet.add_crm("hubspot", "pat-...")`. Every attendee becomes a contact with the meeting logged on it.
**34. PayPal**: `meet.add_payments("paypal", CLIENT_ID, SECRET, WEBHOOK_ID)`, then `meet.hours("coach", "mon-fri 10-18", price="$49")`.
**35. Push notifications**
```python
meet.add_push()                                   # Web Push, nothing else to set up
await meet.push.notify("ada", "Your 3pm is here", "Ravi is waiting", url=room_link)
```
```js
await NodeMeet.push.enable({ token: TOKEN });     // in the browser, after a click
```
**36. Use Zoom / Teams / Meet for bookings**: `meet.add_conferencing("zoom")` (reads ZOOM_* env vars). Emails, invites and the CRM get the Zoom link.
