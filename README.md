# nodemeet

[![tests](https://github.com/dhyanamgit/nodemeet/actions/workflows/tests.yml/badge.svg)](https://github.com/dhyanamgit/nodemeet/actions/workflows/tests.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
[![Licence: PolyForm Noncommercial](https://img.shields.io/badge/licence-PolyForm%20Noncommercial-orange)](LICENSE)

**Embed live video meetings and scheduling in your own Python app.** Self-hosted and free for personal, educational and nonprofit use. No paid services, no vendor accounts.

- **Meetings**: an aiohttp server you mount into your app or run on its own. Signed HMAC join tokens with roles (host / participant / viewer). No participant cap. Topology `auto` runs peer-to-peer up to 4 people, then switches to an aiortc SFU. There's also a webinar mode.
- **Hooks**: `before_join`, `on_join`, `on_leave`, `on_chat` (moderate or rewrite messages), plus room, booking and reminder events.
- **Scheduling**: weekly availability per host with time zones (DST-safe), buffers, minimum notice, booking horizon, blackout dates and per-date overrides. Also slot finding, book / cancel / reschedule, `.ics` invites and reminders.
- **Integrations**: SMTP email (stdlib), HMAC-signed webhooks, SQLite and in-memory storage, a pluggable `Storage` interface for *your* database, a REST API, and Google/Outlook "add to calendar" links.
- **Frontend**: `nodemeet.js` (vanilla client), a ready-made meeting UI (grid, mute, camera, screen share, chat, raise hand, host moderation), a booking widget, and `<nodemeet-room>` / `<nodemeet-booking>` embed tags.
- **CLI**: `nodemeet init`, `nodemeet serve`, `nodemeet link`, `nodemeet doctor`, `nodemeet cheatsheet`, `nodemeet token`.

## Install

```bash
pip install "nodemeet"            # core + aiohttp server
pip install "nodemeet[sfu]"       # + aiortc SFU for rooms larger than 4
pip install "nodemeet[redis]"     # + several servers behind a load balancer
pip install "nodemeet[postgres]"  # + SQLAlchemy storage (also [mysql], [sql])
pip install "nodemeet[asgi]"      # + uvicorn, to serve the ASGI app
pip install "nodemeet[all]"       # everything
```
Or download the wheel from the [latest release](https://github.com/dhyanamgit/nodemeet/releases/latest) and `pip install` it.

## Quickstart: 3 lines

```python
from nodemeet import NodeMeet
meet = NodeMeet()                              # every setting has a default
print(meet.room("standup").host_link("Ada"))   # open this link, share room.guest_link() with others
meet.run()
```

Or, without writing any code: `nodemeet init myapp && cd myapp && python app.py`.

Camera and mic need `https://` (or `localhost`). In production, put a TLS proxy such as Caddy or nginx in front.

## The whole API on one page

Everything is `meet.<something>(...)` and takes plain values. The full list is in `CHEATSHEET.md`, or run `nodemeet.help()` / `nodemeet cheatsheet`.

```python
meet = NodeMeet(db="postgres://u:p@host/app", email="smtp://u:p@smtp.host:587", secret="long-random")

room = meet.room("class-7", preset="classroom", join_list=["ravi@x.com"])  # presets: open, webinar, 1on1...
room.host_link("Ms Rao")      room.guest_link("Ravi")      meet.link("class-7", "Sam", "viewer")

page = meet.hours("rao", "mon-fri 9am-12pm, 2pm-5pm", timezone="Asia/Kolkata", minutes=30, buffer=10)
page.url                       # booking page   ·   page.embed() for your HTML

meet.role("teacher", can="moderate, record", badge="T", rank=50)  # permissions in plain words
meet.brand(name="Acme", color="#ff5a00", logo="https://acme.com/logo.png", font="Inter")

@meet.on("join")
def joined(room, person): print(person.name, "joined", room.id)

meet.add_payments("stripe")   meet.add_calendars(google=True)   meet.add_sso(github=True)   meet.add_sms("twilio")
```

**Integrations, one line each:**
- Chat: Slack, Discord, Teams, Google Chat, Telegram, Mattermost.
- CRM: HubSpot, Salesforce, Pipedrive, Zoho.
- Payments: Stripe, Razorpay, PayPal.
- Push: Web Push, FCM, OneSignal, ntfy.
- Meetings handed off to: Zoom, Teams, Google Meet, Webex, Jitsi.
- Also: calendars, SSO, SMS/WhatsApp, webhooks, S3 recordings, local captions.

See the table in `CHEATSHEET.md`.

**Webhooks in and out:**
- 112 outgoing events with patterns, a delivery log, redelivery and a live feed.
- Inbound webhooks from Zoom, Webex, Microsoft, Google, Twilio, WhatsApp, Slack, Telegram, Discord, HubSpot, Pipedrive, Salesforce, Zoho and payment providers.
- An actions API for Zapier / n8n.

See `WEBHOOKS.md`.

**Settings from a file or env vars.** Use `NodeMeet.from_config("nodemeet.toml")` (written for you by `nodemeet init`) or `NodeMeet.from_env()` (reads `NODEMEET_*` vars and `.env`). In both, `${VAR}` keeps secrets out of the file.

**Databases are just URLs:** `meet.db`, `postgres://`, `mysql://`, `mssql://`, `oracle://`, `duckdb:///`, `snowflake://`, `clickhouse://`, `redis://`, `mongodb://`, `couchdb://`, `elasticsearch://`, `neo4j://`, `arangodb://`, `firestore://`, `dynamodb://`, `s3://`, `cassandra://`, `etcd://`, `consul://`, `lmdb:///`, `dir:///` and more. If a driver is missing, the error tells you the exact `pip install` line.

**Typos get suggestions.** For example: `unknown room preset 'webnar'. Did you mean 'webinar'?`. Run `nodemeet doctor` to check your setup.

**Declarations are safe to keep in code.** Rooms, roles and hours declared in code are applied on startup. If an admin edits one later through the API, that edit survives restarts, until you change the declaration in code.

## Plug into your stack

| Your app | How |
|---|---|
| **FastAPI / Starlette** | `app.mount("/meet", meet.asgi())` (see `examples/fastapi_app.py`) |
| **Django** | `application = route({"/meet": meet.asgi()}, default=get_asgi_application())` (`examples/django_asgi.py`) |
| **aiohttp** | `meet.mount(app, "/meet")` |
| **Quart, Litestar, any ASGI** | mount `meet.asgi()`; run with uvicorn/hypercorn/daphne |
| **Flask / anything else** | run `nodemeet serve` next to it and mint tokens with `nodemeet.TokenSigner` |

```python
from nodemeet import NodeMeet, JoinRejected

meet = NodeMeet(secret="long-random-secret", base_url="https://myapp.com/meet")

@meet.before_join
async def gate(ctx):                       # your auth/billing rules
    if not ctx.claims.meta.get("paid"):
        raise JoinRejected("Upgrade to join")

token = meet.create_token("project-7", "u42", "host", name="Ada", meta={"paid": True})
html = meet.embed_room("project-7", token)   # drop into any template
```

Your users never log in to nodemeet: your backend authenticates them and mints a signed token.

## LMS (Moodle, Canvas, Blackboard, Brightspace...) over LTI 1.3

```python
lti = meet.add_lti(grade_attendance=True)   # or moodle=(URL, CLIENT_ID, DEPLOYMENT_ID)
print(lti.registration_url())                # paste into the LMS once: done, permanently
```

Students arrive already signed in and teachers become hosts. Each course link is its own room. The roster syncs to the join list, and attendance goes to the gradebook. See [LTI.md](LTI.md).

## Scheduling

```python
from nodemeet import Availability
await meet.bookings.set_availability(Availability.from_hours(
    "dr-ada", "Asia/Kolkata", {"mon-fri": "09:00-13:00, 14:00-18:00", "sat": "10:00-12:00"},
    duration_minutes=30, buffer_before=5, buffer_after=10, min_notice_minutes=120,
    max_days_ahead=30, blackout_dates=["2026-12-25"], host_email="ada@example.com"))
slots = await meet.bookings.find_slots("dr-ada", date(2026, 10, 5), date(2026, 10, 9))
b = await meet.bookings.book("dr-ada", slots[0].start, attendee_name="Sam", attendee_email="sam@x.com")
await meet.bookings.reschedule(b.id, slots[3].start)
await meet.bookings.cancel(b.id, reason="sick")
```

`BookingService` also works without the HTTP server. Feed busy times from your own systems with `busy_provider=async def f(host_id, start, end) -> [(start, end), ...]`. Reminders run in the background (default 24h and 15 min before). In multi-process deploys, call `meet.reminders.run_once()` from your own scheduler instead.

## Databases

| Storage | Use it for |
|---|---|
| `MemoryStorage` | tests, demos |
| `SQLiteStorage("file.db")` | single server; stdlib only; versioned schema; safe with several worker processes |
| `SQLAlchemyStorage(url or engine=...)` | Postgres, MySQL/MariaDB, your existing DB; share your app's engine; Alembic-friendly (`nodemeet.storage.sql.metadata`) |
| `SyncStorageAdapter(obj)` | wrap a *synchronous* implementation (Django ORM, sync SQLAlchemy) |
| subclass `Storage` | anything else (see `examples/custom_storage.py`) |

`nodemeet db upgrade --db-url postgresql+asyncpg://...` creates/upgrades tables ahead of deploys.

**No double bookings, no duplicate reminders, with any number of workers/servers:** bookings go through `Storage.insert_booking_if_free()` (SQLite `BEGIN IMMEDIATE`; SQL backends lock a per-host row), and reminders are claimed with `mark_reminder_sent()` (unique key) before sending. Custom backends should implement both atomically.

## Scaling out (several servers)

```python
meet = NodeMeet(secret, broker="redis://redis:6379/0", storage=SQLAlchemyStorage(pg_url))
```

or `nodemeet serve --redis redis://... --db-url ...`. Redis (or Valkey/KeyDB/Dragonfly) syncs presence, chat, WebRTC signaling, moderation, topology and rate limits across servers, and cleans up after a crashed server. Peer-to-peer rooms work with plain round-robin. The SFU relays media inside one server, so for SFU/webinar rooms route by room: the JS client connects to `/ws?room=<id>`, so nginx `hash $arg_room consistent;` pins a room to one server. Full setup: `examples/cluster/` (docker-compose + nginx).

## Multi-tenant API keys

For SaaS platforms: give each of *your* customers (tenants) their own keys.

```bash
nodemeet keys create --tenant acme --scopes rooms:write,tokens:create --rate-limit 600 --db app.db
# or: POST /api/keys {"tenant_id":"acme","scopes":[...]} with the master key
```

* The key is shown once; only its SHA-256 hash is stored. Keys can be revoked, expire, and carry per-minute rate limits.
* Everything a tenant key creates (rooms, hosts, bookings, webhooks) is stamped with its tenant. Other tenants get 404s, and join tokens minted for a tenant only work in that tenant's rooms.
* Scopes: `rooms:read`, `rooms:write`, `tokens:create`, `availability:write`, `bookings:read`, `bookings:write`, `webhooks:manage` or `*`.
* Tenants register their own webhooks (`POST /api/webhooks`) and receive only their own events, signed with their own secret.
* The `api_key` you give `NodeMeet` is the master key: it sees everything and manages keys.

## Email and webhooks

```python
from nodemeet import SMTPMailer, WebhookDispatcher
meet = NodeMeet(secret, mailer=SMTPMailer("smtp.example.com", 587, username="u", password="p", sender="no-reply@example.com"),
                webhooks=WebhookDispatcher())
meet.webhooks.add("https://myapp.com/hooks/nodemeet", secret="whsec", events=["booking.created", "participant.joined"])
```

Webhooks carry `X-NodeMeet-Signature: t=<ts>,v1=<hmac>`. Check them with `nodemeet.verify_signature(secret, body, header)`. To rebrand emails, subclass `EmailTemplates`.

## REST API

`Authorization: Bearer <master key or tenant key>`. Public endpoints power the booking widget; booking management also accepts the per-booking `manage_token` (`?t=`).

| Method | Path | Auth |
|---|---|---|
| GET | `/api/health`, `/api/me` | public / any key |
| POST/GET | `/api/rooms`; GET/PATCH/DELETE `/api/rooms/{id}` | `rooms:write` / `rooms:read` |
| POST | `/api/tokens` `{room, user_id, role, name, ttl, meta}` | `tokens:create` |
| PUT / GET | `/api/hosts/{host}/availability` | `availability:write` / public (redacted) |
| GET | `/api/hosts/{host}/slots?start=YYYY-MM-DD&end=...&tz=...` | public |
| POST | `/api/bookings` `{host_id, start, name, email, timezone, notes}` | public |
| GET | `/api/bookings` | `bookings:read` |
| GET | `/api/bookings/{id}`, `/invite.ics` | `bookings:read` or manage token |
| POST | `/api/bookings/{id}/cancel`, `/reschedule` | `bookings:write` or manage token |
| POST/GET/DELETE | `/api/keys`, `/api/keys/{id}` | master (tenants can list/revoke their own) |
| POST/GET/DELETE | `/api/webhooks`, `/api/webhooks/{id}` | `webhooks:manage` |

Pages: `/r/{room}#token=...`, `/book/{host}`, `/book/manage/{id}#t=...`. WebSocket: `/ws`.

## Frontend

```html
<script src="https://myapp.com/meet/static/embed.js" async></script>
<nodemeet-room token="nm1...." height="600px"></nodemeet-room>      <!-- iframe; add `inline` for no iframe -->
<nodemeet-booking host="dr-ada"></nodemeet-booking>
```

Or build your own UI with `new NodeMeet.Client({base, token})`: events are `stream`, `stream-removed`, `chat`, `participants`, `topology`, and more. Methods include `join()`, `toggleAudio()`, `toggleVideo()`, `startScreenShare()`, `sendChat()`, `raiseHand()`, `mute(peer)` and `kick(peer)`. See `examples/html/custom_client.html`. To theme the UI, override the CSS variables in `nodemeet.css`.

## Media topology notes

- `auto`: mesh up to `p2p_max` (4). Above that it switches to the SFU and drops back when the room shrinks to half.
- `webinar`: only hosts, or people a host "invites to stage", publish.
- The aiortc SFU relays decoded frames, so it's CPU-bound. It's good for roughly 5 to 25 people per server. For huge rooms, implement `nodemeet.sfu.MediaBackend` on top of an OSS SFU (mediasoup, Janus, LiveKit OSS) and pass `NodeMeet(sfu=MyBackend())`.
- The default STUN server is Google's free public one. Behind strict NATs, run [coturn](https://github.com/coturn/coturn) and pass `ice_servers=[{"urls": "turn:turn.example.com", "username": ..., "credential": ...}]`.

## Development

```bash
pip install -e ".[dev]"
pytest                    # ASGI, cluster, tenancy and concurrency tests need nothing extra
python -m build     # sdist + wheel in dist/
```

## Contributors

Everyone who helps is credited in [CONTRIBUTORS.md](CONTRIBUTORS.md): code, docs, bug reports, ideas and reviews all count.

<a href="https://github.com/dhyanamgit/nodemeet/graphs/contributors">[https://contrib.rocks/image?repo=dhyanamgit/nodemeet](https://contrib.rocks/image?repo=dhyanamgit/nodemeet)</a>
## Contributors

Everyone who helps is credited in [CONTRIBUTORS.md](CONTRIBUTORS.md): code, docs, bug reports, ideas and reviews all count.

<a href="https://github.com/dhyanamgit/nodemeet/graphs/contributors">[https://contrib.rocks/image?repo=dhyanamgit/nodemeet](https://contrib.rocks/image?repo=dhyanamgit/nodemeet)</a>
## Licence

nodemeet is **free for noncommercial use** under the [PolyForm Noncommercial License 1.0.0](LICENSE): personal
projects, study, research, hobby use, and any school, college, charity, public research body or government
office. Keep the `Required Notice:` lines from [LICENSE](LICENSE) in every copy.

Selling nodemeet, or using it in a product or service that makes money, needs a **commercial licence** from the
author, Dhyanam Shah. Open an issue titled "Commercial licence" or write through the GitHub profile.

Copyright (c) 2026 Dhyanam Shah. "nodemeet" is the author's project name; forks must use a different name.

## Contributing

Bug fixes, docs, tests and new integrations are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) and look for issues
labelled `good first issue`.

## New in 0.3: roles, branding, settings, every database

```python
from nodemeet import NodeMeet
from nodemeet.storage import DBAPIStorage, KeyValueStorage, DocumentStorage, MemoryKV

nm = NodeMeet(secret="change-me",
              branding={"name": "Acme Meet", "colors": {"primary": "#7c3aed"}, "font_family": "Inter",
                        "strings": {"join_now": "Hop in"}, "toolbar": ["mic", "camera", "chat", "leave"]},
              roles=[{"name": "moderator",
                      "permissions": ["chat.*", "moderate.mute", "moderate.lobby"],
                      "attributes": {"label": "Moderator", "badge": "🛡", "rank": 50}}])
```

Roles/permissions: `GET /api/permissions`, `GET/POST /api/roles`, `PUT/PATCH/DELETE /api/roles/{name}`, `POST /api/roles/{name}/reset`.
Branding: `GET/PUT/PATCH /api/branding`, public `GET /api/branding/resolve?room=&host=`.
Storage: pick SQL (`DBAPIStorage(connect_fn, dialect="postgres")`), key-value (`KeyValueStorage(RedisKV(...))`) or document (`DocumentStorage(MongoDocs(...))`) and plug in your own driver.
Note: in peer-to-peer mode, media permissions are enforced by the client. In SFU mode the server enforces them.

## Waiting room (on by default) and blocked people

Every new room has a waiting room. Hosts, people on the room's **join list**, people who
booked the meeting and links made with `skip_waiting_room=True` go straight in; everyone
else waits until a moderator admits them.

```python
await meet.create_room(room_id="class-1", join_list=["ravi@x.com", "u_42"])  # these skip it
await meet.add_to_join_list("class-1", "neha@x.com")
link = meet.invite("class-1", "guest-9", skip_waiting_room=True)            # one-off pass
await meet.create_room(room_id="open-hall", waiting_room=False)             # off for one room
meet = NodeMeet(secret, waiting_room=False)                                 # off everywhere
```

Blocking (ban) someone, either in the meeting or straight from the waiting room:

* removes them and records who blocked them and when; they also drop off the join list;
* any later join attempt with that user id (even with a fresh token) is refused with a clear
  "you can't rejoin" message, never reaching the waiting room;
* moderators in the meeting get a notice ("X tried to rejoin") with an **Unblock** button
  (at most once a minute per person); `on_blocked_attempt` hook and
  `participant.blocked_attempt` webhook fire too;
* unblocking (`meet.unban(room, user)`, UI, or `DELETE /api/rooms/{id}/bans/{user}`) lets them
  come back through the waiting room.

REST: `GET|POST /api/rooms/{id}/join-list`, `POST /api/rooms/{id}/join-list/remove`,
`GET /api/rooms/{id}/bans`, `DELETE /api/rooms/{id}/bans/{user_id}`; `POST /api/tokens`
accepts `"skip_waiting_room": true`. CLI: `nodemeet serve --no-waiting-room`,
`nodemeet token ROOM USER --role teacher --grant chat.pin --skip-waiting-room`.

See RECIPES.md for copy-paste snippets for every use case.
