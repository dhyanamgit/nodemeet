# nodemeet cheat sheet

The only page you need. Lines with `await` run inside async code (a route or a startup hook).

```text
nodemeet cheat sheet - everything is meet.<something>(...)

  meet = NodeMeet()                          # works as-is; add db=, email=, secret= when ready
  meet = NodeMeet(db="meet.db", email="console", secret="long-random")
  meet = NodeMeet.from_env()                 # or NodeMeet.from_config("nodemeet.toml")

ROOMS & LINKS
  room = meet.room("standup")                # presets: open, webinar, classroom, interview, 1on1...
  room.host_link("Ada")    room.guest_link("Bob")    meet.link("standup", "Cy", "viewer")
  meet.room("class", join_list=["a@x.com"])  # they skip the waiting room (on by default)
  meet.link("standup", "Dee", skip_waiting_room=True, ttl="2h", can="screen", cannot="chat")

BOOKING
  page = meet.hours("ada", "mon-fri 9-17", timezone="Asia/Kolkata", minutes=30, buffer=10)
  page.url                                   # share it, or page.embed() in your HTML
  await meet.slots("ada", days=7)   await meet.book("ada", "2026-10-05 10:30", name=.., email=..)

ROLES, PERMISSIONS & BRANDING
  meet.role("teacher", can="moderate, record", badge="T", rank=50)
  meet.role("student", cannot="screen, dm")  meet.role("viewer", can="chat")   # edit built-ins
  meet.brand(name="Acme", color="#ff5a00", logo="https://..", font="Inter", whiteboard=False)

EVENTS
  @meet.on("join")  @meet.on("leave")  @meet.on("chat")  @meet.on("booked")  @meet.on("blocked")

ADD-ONS (one line each)
  meet.add_email("smtp://user:pass@smtp.host:587")      meet.add_webhook(url, secret)
  meet.add_payments("stripe")   meet.add_calendars(google=True)   meet.add_sso(github=True)
  meet.add_sms("twilio")        meet.add_recordings("s3://bucket/rec")   meet.add_captions()
  meet.add_chat("slack", URL)   meet.add_crm("hubspot", TOKEN)   meet.add_push()   meet.add_payments("paypal")
  meet.add_conferencing("zoom")  # or teams, google_meet, webex, jitsi: bookings get that link

RUN
  meet.run()                                  # standalone
  app.mount("/meet", meet.asgi())             # FastAPI / Starlette / Django
  nodemeet init  |  nodemeet serve  |  nodemeet link standup Ada --host  |  nodemeet doctor
```

## Room presets
| preset | what you get |
|---|---|
| `meeting` (default) | waiting room on, everyone can talk and share |
| `open` | no waiting room: anyone with the link walks in |
| `private` | waiting room with a "host will let you in" message |
| `webinar` / `town-hall` | hosts on stage, everyone else joins as a viewer |
| `classroom` | waiting room; students can't share screen or send private messages |
| `interview` | waiting room, peer-to-peer, guests can't record |
| `1on1` | two people max, peer-to-peer |
| `support` / `drop-in` | people wait only until a host arrives |

## Permission words (for `can=` / `cannot=`)
`mic`, `camera`, `screen`, `hd`, `chat`, `dm`, `links`, `react`, `hand`, `rename`, `polls`, `create_polls`, `qa`, `answer`,
`whiteboard`, `draw`, `record`, `captions`, `transcript`, `breakouts`, `skip_waiting_room`, `mute`, `kick`, `ban`,
`admit`, `spotlight`, `lock`, `end`, `assign_roles`, `moderate` (all moderation), `everything`.
The exact names (`moderate.kick`, `chat.links`...) also work. `GET /api/permissions` lists all 61.

## Branding words (for `meet.brand(...)` or `branding=` on a room)
`name`, `color`, `logo`, `dark_logo`, `favicon`, `font` (Google Font name or CSS URL), `theme` ("dark"/"light"),
`background`, `text`, `css`, `title`, `white_label=True`, `layout`, `language`, `strings={...}`, `toolbar=[...]`, and any
feature turned off by name: `whiteboard=False`, `polls=False`, `reactions=False`, `chat=False`...

## Event names (for `@meet.on(...)`)
`join`, `leave`, `chat`, `hand`, `reaction`, `waiting`, `admitted`, `blocked`, `moderation`, `role_changed`,
`room_created`, `room_closed`, `booked`, `cancelled`, `rescheduled`, `reminder`, `before_join`.

## Database URLs
| URL | database |
|---|---|
| `memory` (default) | in-process, gone on restart |
| `meet.db` / `sqlite:///meet.db` | SQLite file, no install needed |
| `postgres://u:p@host/db` | PostgreSQL, CockroachDB, Supabase, Neon, Timescale (`[postgres]`) |
| `mysql://` / `mariadb://` | MySQL, MariaDB, TiDB, PlanetScale (`[mysql]`) |
| `mssql://` · `oracle://` · `db2://` · `hana://` · `firebird://` | other relational databases |
| `duckdb:///f.duckdb` · `snowflake://acct/db/schema` · `clickhouse://` | analytics databases |
| `redis://` (also valkey/keydb/dragonfly) · `etcd://` · `consul://` | key-value servers |
| `dynamodb://table?region=..` · `s3://bucket/prefix` | AWS / any S3-compatible store (`[aws]`) |
| `cassandra://h1,h2/keyspace` · `lmdb:///path` · `fdb://` · `dbm:///path` | other key-value stores |
| `mongodb://host/db` (also DocumentDB/Cosmos) · `couchdb://` · `elasticsearch://` / `opensearch://` | document databases |
| `neo4j://` / `bolt://` · `arangodb://` · `firestore://project` | graph / multi-model |
| `dir:///path` | plain JSON files, no database at all |
| `dialect+driver://...` | any SQLAlchemy async URL, passed straight through |

## Integrations (one line each, credentials from arguments or env vars)
| Call | Apps | Env vars if you pass nothing |
|---|---|---|
| `add_email(url)` | any SMTP server | NODEMEET_EMAIL |
| `add_chat(app, ...)` | slack, discord, teams, google_chat, telegram, mattermost, rocketchat, webhook | SLACK_WEBHOOK_URL, DISCORD_WEBHOOK_URL, TEAMS_WEBHOOK_URL, GOOGLE_CHAT_WEBHOOK_URL, TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID |
| `add_crm(app, ...)` | hubspot, salesforce, pipedrive, zoho | HUBSPOT_TOKEN, SALESFORCE_CLIENT_ID/_SECRET/_DOMAIN, PIPEDRIVE_API_TOKEN, ZOHO_CLIENT_ID/_SECRET/_REFRESH_TOKEN |
| `add_payments(app, ...)` | stripe, razorpay, paypal | STRIPE_SECRET_KEY/_WEBHOOK_SECRET, RAZORPAY_KEY_ID/_KEY_SECRET/_WEBHOOK_SECRET, PAYPAL_CLIENT_ID/_CLIENT_SECRET/_WEBHOOK_ID (+PAYPAL_SANDBOX) |
| `add_push(app, ...)` | webpush (default), fcm, onesignal, ntfy | VAPID_PRIVATE_KEY/_PUBLIC_KEY (optional), FCM_SERVICE_ACCOUNT, ONESIGNAL_APP_ID/_API_KEY, NTFY_SERVER |
| `add_conferencing(app, ...)` | zoom, teams, google_meet, webex, jitsi | ZOOM_ACCOUNT_ID/_CLIENT_ID/_CLIENT_SECRET, TEAMS_TENANT_ID/_CLIENT_ID/_CLIENT_SECRET, GOOGLE_MEET_CLIENT_ID/_SECRET/_REFRESH_TOKEN, WEBEX_TOKEN |
| `add_calendars(...)` | google, outlook, caldav | GOOGLE_CLIENT_ID/_SECRET, MICROSOFT_CLIENT_ID/_SECRET |
| `add_sso(...)` | google, microsoft, github, okta, auth0, keycloak, oidc, saml | GOOGLE_/MICROSOFT_/GITHUB_/OKTA_/AUTH0_/KEYCLOAK_ vars |
| `add_sms(app, ...)` | twilio, whatsapp, webhook | TWILIO_ACCOUNT_SID/_AUTH_TOKEN/_FROM, WHATSAPP_PHONE_NUMBER_ID/_ACCESS_TOKEN |
| `add_webhook(url, secret)` | your own endpoint | NODEMEET_WEBHOOK_SECRET |
| `add_recordings(where)` / `add_captions()` | folder or s3://, local Whisper | NODEMEET_RECORDINGS |

Or turn them on from the environment: `NODEMEET_CHAT=slack,discord`, `NODEMEET_CRM=hubspot`, `NODEMEET_PUSH=webpush`, `NODEMEET_CONFERENCING=zoom`, `NODEMEET_PAYMENTS=paypal`, `NODEMEET_CALENDARS=google`, `NODEMEET_SSO=github`, `NODEMEET_SMS=twilio`.
Browser push: `await NodeMeet.push.enable({ token })`. Room owner for "someone is waiting" pushes: `meet.room("desk", owner="ada")`.
