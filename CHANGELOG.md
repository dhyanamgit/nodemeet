# Changelog

## Unreleased
- CI: Added Ruff linting to CI (`.github/workflows/tests.yml`) and configured `[tool.ruff]` in `pyproject.toml`.

## 0.9.6
- Fixed: the built-in dbm storage failed on Python 3.13 ("SQLite objects created in a thread can only be used in
  that same thread"). 3.13 made dbm.sqlite3 the default; nodemeet now opens and uses the file on one dedicated thread.
- CI: one failing Python version no longer cancels the others; newer GitHub action versions.
- PyPI page now shows `pip install nodemeet`.

## 0.9.5 - first public release
- Licence: PolyForm Noncommercial 1.0.0 (free for personal, educational, nonprofit and government use; commercial
  licences from the author). Copyright Dhyanam Shah. Contributions need the agreement in CONTRIBUTING.md.
- Layout: the grid fits every tile on screen at any count and window size; spotlight no longer covers the grid.

## 0.9.4 - smoother video
- Auto quality is 720p; the camera keeps its frame rate under load (`maintain-framerate`), bitrate caps are applied
  after negotiation, and low-latency playout cuts the delay that made two people look out of sync.

## 0.9.3 - meeting UI fixes
- The More, Reactions and Layout menus stayed open only for a moment; they now open and close properly.
- The settings popup no longer overflows small windows.

## 0.9.0 - LTI platform kit (for building your own LMS)
- `nodemeet.lti_platform.LTIPlatformKit` gives your own LMS the platform side of LTI 1.3. It covers:
  - OIDC login (`/lti/auth`) and the LMS's public key set
  - the client-credentials token endpoint
  - dynamic registration (OpenID configuration plus the registration endpoint)
  - receiving deep links back from tools
  - Names and Roles, with paging
  - Assignment and Grade Services: line items, scores and results
- It mounts as an ASGI app and stores everything in any nodemeet database URL.
- nodemeet now returns a clear 502 error, and sends `lti.failed`, when an LMS refuses a dynamic registration.
- Grades and the roster are optional. Turn them off with `grades=False` / `roster=False` on `add_lti(...)` and on `LTIPlatformKit(...)`. When off, the scope is never requested or granted, the picker doesn't show the gradebook checkbox, and the endpoints aren't served.
- 3 end-to-end tests: a custom LMS and nodemeet talking to each other, with a real browser-style redirect flow.

## 0.8.0 - LMS connection (LTI 1.3)
- `meet.add_lti(...)` connects nodemeet to Moodle, Canvas, Blackboard, Brightspace, Schoology, Sakai and Open edX over LTI 1.3 and LTI Advantage. See LTI.md.
- **Launch:** the OIDC login and launch with full token checks (signature, issuer, audience, expiry, single-use nonce, deployment). LMS roles map to nodemeet roles, and each course link gets its own permanent room.
- **Deep linking:** instructors add a live meeting room or a booking page to a course.
- **Dynamic registration:** set up with a signed invite link. There is also a Canvas JSON config.
- **Names and Roles:** copies the course roster to the room's join list.
- **Assignment and Grade Services:** sends grades, and can grade attendance automatically when a meeting ends.
- Admin REST API at `/api/lti/*`.
- 6 new events: `lti.launch`, `lti.deep_link`, `lti.registered`, `lti.roster_synced`, `lti.grade_sent`, `lti.failed`.
- The tool's RSA key is generated once and saved in your database.
- 7 new tests against a fake LMS.

## 0.7.1 - test kit
- **Real-database tests** (`tests/integration`): a docker-compose file for 19 databases. Each one runs the storage contract, a 40-person double-booking race across 4 connections, and a REST booking that survives a restart.
- **Browser tests** (`tests/e2e`, Playwright): Chromium, Firefox and WebKit cover the waiting room, admitting people, live WebRTC video, chat, the booking widget and the phone layout. The same steps passed in headless Chromium during development.
- `scripts/test-all.ps1` / `test-all.sh` and a GitHub Actions workflow (3 operating systems, 19 databases, 3 browsers).
- **Fixed: DuckDB storage.** DuckDB cursors are separate autocommit connections, so writes weren't atomic, and its row counts read as -1, so rows were never inserted. DuckDB now uses explicit transactions and reads the real row counts, and its contract test is back on.
- `?namespace=` on every database URL (or `storage_from_url(url, namespace=...)`): several apps or test runs can share one database. Neo4j and ArangoDB gained a `prefix` option.
- `NodeMeet.run(banner=False)` turns off the startup line.

## 0.7.0 - webhooks everywhere
- **112 outgoing events** (was 9), in a catalogue at `nodemeet.events` and `GET /api/webhooks/events`. They cover:
  - Meeting lifecycle (`meeting.started` / `meeting.ended` with duration, peak size and attendees) and every moderator action.
  - Waiting-room outcomes, screen sharing, polls, Q&A, whiteboard, breakouts, captions and recording.
  - Booking attendance (`host_joined`, `attendee_joined`, `completed`, `no_show`), payment holds and expiry, refunds, failures and disputes.
  - Delivered / failed for email, SMS, push, chat apps, CRM, calendars and conferencing, plus the `integration.error` catch-all.
  - SSO logins and failures, and admin changes (roles, branding, API keys).
- Subscriptions accept patterns (`booking.*`, `*.failed`). High-volume events are opt-in.
- Delivery log, redelivery, test events, and a long-poll event feed (`GET /api/events`). `@meet.on_event(...)` gives in-process subscribers.
- **Inbound webhooks** at `/api/inbound/*`, all signature-checked:
  - Zoom, Webex and Microsoft Graph (calendar plus Teams call records).
  - Google Calendar push channels (`calendar.changed`; `meet.calendars.watch(host)` sets them up).
  - Twilio (delivery receipts plus replies) and WhatsApp Cloud.
  - Slack, Telegram and Discord `/meet` commands.
  - HubSpot, Pipedrive, Salesforce Outbound Messages and Zoho.
  - A generic **actions API** (create rooms, links and bookings; broadcast into a live meeting; push; chat; SMS; email; custom events).
- Stripe, Razorpay and PayPal refund / failure / dispute webhooks update the booking's payment (`cancel_on_refund=True` is optional).
- Attendees can reply YES / CANCEL / RESCHEDULE / HELP to texts. Twilio delivery receipts are wired up automatically.
- `meet.add_bot(...)`, plus webhook-secret options on `add_conferencing`, `add_sms`, `add_crm` and `add_calendars`. New env vars: NODEMEET_INBOUND_SECRET and NODEMEET_BOTS.
- See WEBHOOKS.md.

## 0.6.0 - more integrations
- **Team chat** (`meet.add_chat`): post bookings, payments, blocked rejoin attempts and recordings to Slack (webhook or bot token), Discord, Microsoft Teams (Adaptive Cards), Google Chat, Telegram, Mattermost / Rocket.Chat, or any webhook. Events can be filtered with patterns like `booking.*`.
- **CRM sync** (`meet.add_crm`): HubSpot, Salesforce (client credentials, re-authenticates on 401, Contact or Lead), Pipedrive and Zoho CRM (all data centres). The attendee is upserted as a contact; the meeting is logged, moved when rescheduled and marked when cancelled.
- **PayPal** (`meet.add_payments("paypal")`): Orders v2 checkout. Webhook signatures are verified with PayPal's API, approved orders are captured automatically, and duplicate webhooks are handled safely.
- **Push notifications** (`meet.add_push`):
  - Standard Web Push (VAPID, RFC 8291 encryption, keys generated and stored for you), Firebase Cloud Messaging v1, OneSignal and ntfy.
  - Hosts and attendees get booking, reschedule, cancel and reminder pushes; a room's owner gets "someone is waiting". `meet.push.notify(user, ...)` sends your own.
  - Browser side: `NodeMeet.push.enable({token})` plus a bundled service worker.
- **Conferencing handoff** (`meet.add_conferencing`): booked meetings can use Zoom, Microsoft Teams, Google Meet, Webex or Jitsi instead of a nodemeet room (or as well as one, with `mode="also"`). The link flows into emails, .ics files and the CRM; reschedules and cancellations update the external meeting.
- New `participant.waiting` webhook event. Webhook dispatcher taps let integrations listen to every event in-process.
- `from_env` / `from_config` support NODEMEET_CHAT, NODEMEET_CRM, NODEMEET_PUSH, NODEMEET_CONFERENCING and the `chat` / `crm` / `push` / `conferencing` config sections.

## 0.5.0 - easy mode
- `NodeMeet()` works with no arguments: it generates a secret (with a warning) and uses memory storage.
- Plain-string settings: `db=` takes a URL for any supported database (`storage_from_url`), `email="smtp://..."` or `"console"`, `redis=`, `webhooks=[urls]`, `recordings="s3://..."`.
- One-line helpers on `meet`:
  - `room(id, preset=...)` returns a handle with `host_link`, `guest_link`, `viewer_link`, `embed`, `let_in`, `unblock`.
  - `link`, `host_link`, `guest_link` (with `can=`, `cannot=`, `ttl="2h"`).
  - `hours("ada", "mon-fri 9-17")` returns a BookingPage.
  - `slots`, `book`, `reschedule`, `cancel`.
  - `role(name, can=, cannot=)` in plain words; `delete_role`; `brand(color=, logo=, font=)`.
  - `add_email`, `add_webhook`, `add_payments`, `add_calendars`, `add_sso`, `add_sms`, `add_recordings`, `add_captions`.
- Room presets: meeting, open, private, webinar, town-hall, classroom, interview, 1on1, support, drop-in.
- Plain-word permission aliases (mic, camera, screen, chat, record, kick, moderate...) work everywhere.
- Event aliases: `@meet.on("join")`, `"booked"`, `"blocked"`...
- New ways to load settings:
  - `NodeMeet.from_env()`, which also reads `.env`.
  - `NodeMeet.from_config("nodemeet.toml" | .json | .yaml)`, with `${VAR:-default}` expansion.
  - `NodeMeet.from_dict()`.
  - `nodemeet.quickstart()`.
- Declarations in code are applied on startup. A runtime edit survives restarts until the declaration in code changes.
- Errors suggest the closest match ("Did you mean 'webinar'?"). Missing drivers tell you the exact `pip install` line.
- CLI: `nodemeet init [--template basic|fastapi|booking]`, `nodemeet link`, `nodemeet doctor`, `nodemeet cheatsheet`. `serve` reads `nodemeet.toml` automatically.
- JS: `new NodeMeet.Client(token)`. `base` is detected from the script URL and the token from `#token=`. `NodeMeet.UI.mount('#call', {token})`.
- `nodemeet.help()`, CHEATSHEET.md, and RECIPES.md rewritten for the short API. New extras for each database driver.


## 0.2.0
- **ASGI adapter**: `meet.asgi()` for FastAPI, Starlette, Django (`nodemeet.asgi.route`), Quart, Litestar, uvicorn. Core no longer imports aiohttp; HTTP/WebSocket logic is framework-neutral.
- **Clustering**: `broker="redis://..."` syncs presence, chat, signaling, moderation, topology and rate limits across servers; crashed-server cleanup; `?room=` routing hint for SFU affinity; docker-compose + nginx example.
- **Database-safe scheduling**: atomic `insert_booking_if_free` and `mark_reminder_sent` (no double bookings or duplicate reminders across workers).
- **SQLAlchemy storage** (Postgres/MySQL/SQLite...), versioned SQLite schema with migrations, `SyncStorageAdapter` for Django ORM & friends, `nodemeet db upgrade`.
- **Multi-tenant API keys**: hashed keys, scopes, expiry, revocation, rate limits, tenant-scoped rooms/hosts/bookings, tenant-bound join tokens, per-tenant signed webhooks, `nodemeet keys` CLI.
- base_url auto-detection (honours `X-Forwarded-*` with `trust_proxy=True`), WebSocket keepalive pings, stdlib webhook transport fallback.

## 0.1.0
First release.

## 0.3.0
- Roles & permissions: 45 granular permissions (access, media, chat, self, moderation) with `*` / `group.*` wildcards; custom roles with custom attributes (label, color, badge, rank, stage, auto_mute, bitrate/fps/resolution caps, chat rate limit, immune…); edit, reset or delete built-in roles; per-tenant and per-room role overrides; token-level grant/revoke; rank-based moderation.
- Meeting features: waiting room (admit/deny/admit all), private chat, chat delete/pin, reactions, rename, spotlight, bans, end-for-all, mute/camera locks, hidden participants.
- Branding: colors (dark + light), fonts, radius, logos, background, toolbar order, feature toggles, every UI string, reactions, custom CSS/head HTML, meta tags, emails and booking pages; layered defaults < app < tenant < room; CSS-injection safe. REST: `/api/branding`, `/api/branding/resolve`.
- Settings: camera/mic/speaker pickers, live mic meter, speaker test, quality presets, echo/noise/AGC, background blur, mirror, hide self; persisted per browser.
- Databases: DB-API storage (Postgres, MySQL/MariaDB, SQL Server, Oracle, DB2, DuckDB, Firebird, SAP HANA, Snowflake, ClickHouse, generic), key-value family (memory, dbm, JSON dir, Redis, DynamoDB, Cassandra/Scylla, etcd, Consul, LMDB, FoundationDB, S3), document family (memory, MongoDB, CouchDB, Firestore, Elasticsearch/OpenSearch, Neo4j, ArangoDB), plus existing SQLite/SQLAlchemy/memory.

## 0.3.1
- Waiting room is now **on by default** (`NodeMeet(waiting_room=False)` or per room to turn it off).
- Join list per room: listed user ids/emails, booking attendees, hosts and `skip_waiting_room` links go straight in.
- Waiting-room actions: admit, always admit (adds to join list), deny, block.
- Blocked users: refused with a clear message on every rejoin attempt, moderators notified with an Unblock button, `on_blocked_attempt` hook + `participant.blocked_attempt` webhook, ban details (who/when), bans sync across servers.
- `create_room()` accepts waiting_room, join_list, chat_enabled, locked, default_role, role_overrides, branding.
- REST endpoints for the join list and bans; tokens accept `skip_waiting_room`.
- CLI: `token --role` accepts custom roles, plus `--grant`, `--revoke`, `--skip-waiting-room`; `serve --no-waiting-room`.
- Fixed: demo.py used removed v0.2 calls; README custom-role example; cluster nodes now reload room config on change.
- New RECIPES.md.

## 0.4.0
- Polls (live results permission, anonymous/multiple), Q&A (upvotes, answers, highlight), shared whiteboard.
- Breakout rooms: auto/manual assignment, let people choose, timers, broadcast message, hop between rooms, auto-return.
- Recording: in-browser compositor (all tiles + mixed audio) uploaded in chunks; file or S3/MinIO/R2 storage; REC indicator; webhooks.
- Live captions + transcripts: browser speech engine (no install) or local Whisper (faster-whisper/openai-whisper); TXT/VTT/JSON download.
- Two-way calendar sync: Google, Outlook/365 (Graph), CalDAV (iCloud, Fastmail, Nextcloud...); busy times block slots; events created/moved/deleted.
- Payments before booking: Stripe Checkout and Razorpay Payment Links with atomic slot holds, auto-release, late-payment flag.
- SMS/WhatsApp confirmations + reminders: Twilio, WhatsApp Cloud API, generic webhook.
- SSO: OIDC (Google, Microsoft, Okta, Auth0, Keycloak, any issuer) with PKCE, GitHub, SAML via python3-saml; SSO-required rooms; admin sessions.
- Waiting room extras: until-host mode, live message, position in line, knock sound + browser notifications.
- Bans by device (default) and optionally IP.
- Built-in zero-dependency HTTP/WebSocket server (`meet.run()` / `nodemeet serve --server dev`).
- Generic record store on every storage backend; storage contract suite runs on 11 backends; DB-API retries on conflicts.
- 61 permissions (new: polls.*, qa.*, whiteboard.*, breakout.*, recording.*, captions.*, transcript.download).
