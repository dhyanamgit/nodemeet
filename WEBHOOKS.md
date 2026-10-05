# nodemeet webhooks

nodemeet sends **112 kinds of events** out and accepts webhooks **in** from 13 apps (plus an actions API for Zapier, Make, n8n or your own backend).

## Getting events out

```python
meet.add_webhook("https://you.com/hooks", "whsec_...", events=["booking.*", "payment.*", "*.failed"])

@meet.on_event("meeting.ended", "booking.no_show")      # in-process: same events, no HTTP
async def handle(event, data): ...
```

- **REST:** `POST /api/webhooks {url, events}` (per-customer with tenant API keys).
- **Catalogue:** `GET /api/webhooks/events`.
- **Delivery log:** `GET /api/webhooks/deliveries?failed=1`.
- **Redeliver:** `POST /api/webhooks/deliveries/{id}/redeliver`.
- **Send a test:** `POST /api/webhooks/test`.
- **No public URL?** Long-poll `GET /api/events?after=<seq>&wait=25&events=booking.*`.

**Patterns:** exact names, `booking.*`, `*.failed`, `custom.*`, or `*`.

**Noisy events:** high-volume events are marked *noisy* in the table below. `*` and "everything" skip them; name them or use a group pattern like `poll.*` to receive them.

**Every request is signed:** `X-NodeMeet-Signature: t=..,v1=HMAC-SHA256(secret, "t.body")`. Check it with `nodemeet.verify_signature`. Failed deliveries are retried with backoff and logged.

## Event catalogue

### room
| event | what happened | noisy |
|---|---|---|
| `room.created` | A room was created |  |
| `room.updated` | Room settings changed (waiting room, join list, lock, branding...) |  |
| `room.closed` | A room closed (everyone left / ended) |  |
| `room.locked` | A moderator locked the room |  |
| `room.unlocked` | A moderator unlocked the room |  |

### meeting
| event | what happened | noisy |
|---|---|---|
| `meeting.started` | First person joined a room: the meeting is live |  |
| `meeting.ended` | Last person left: duration, peak size, everyone who attended |  |
| `meeting.ended_by_host` | A moderator ended the meeting for everyone |  |

### participant
| event | what happened | noisy |
|---|---|---|
| `participant.joined` | Someone joined a meeting |  |
| `participant.left` | Someone left (with how long they stayed) |  |
| `participant.waiting` | Someone is in the waiting room |  |
| `participant.admitted` | Someone was let in from the waiting room |  |
| `participant.denied` | Someone was turned away from the waiting room |  |
| `participant.blocked_attempt` | A blocked person tried to rejoin |  |
| `participant.kicked` | A moderator removed someone |  |
| `participant.banned` | A moderator blocked someone |  |
| `participant.unbanned` | Someone was unblocked |  |
| `participant.role_changed` | Someone's role changed |  |
| `participant.muted` | A moderator muted someone |  |
| `participant.renamed` | Someone changed their display name |  |
| `participant.hand_raised` | Someone raised their hand |  |
| `participant.hand_lowered` | Someone lowered their hand | yes |
| `participant.reaction` | Someone sent an emoji reaction | yes |
| `participant.media_changed` | Someone turned mic / camera on or off | yes |
| `participant.moderated` | Any moderator action (raw) |  |

### screen_share
| event | what happened | noisy |
|---|---|---|
| `screen_share.started` | Someone started sharing their screen |  |
| `screen_share.stopped` | Someone stopped sharing their screen |  |

### chat
| event | what happened | noisy |
|---|---|---|
| `chat.message` | A chat message was sent |  |
| `chat.deleted` | A chat message was deleted |  |
| `chat.pinned` | A chat message was pinned / unpinned |  |

### poll
| event | what happened | noisy |
|---|---|---|
| `poll.created` | A poll was created |  |
| `poll.voted` | Someone voted in a poll | yes |
| `poll.closed` | A poll closed (with results) |  |
| `poll.deleted` | A poll was deleted |  |

### qa
| event | what happened | noisy |
|---|---|---|
| `qa.asked` | A question was asked in Q&A |  |
| `qa.upvoted` | A question was upvoted | yes |
| `qa.answered` | A question was answered |  |

### whiteboard
| event | what happened | noisy |
|---|---|---|
| `whiteboard.cleared` | The whiteboard was cleared |  |
| `whiteboard.drawn` | Someone drew on the whiteboard | yes |

### breakout
| event | what happened | noisy |
|---|---|---|
| `breakout.configured` | Breakout rooms were set up |  |
| `breakout.opened` | Breakout rooms opened |  |
| `breakout.closed` | Breakout rooms closed |  |
| `breakout.moved` | Someone was moved to a breakout room |  |
| `breakout.announced` | A message was broadcast to all breakout rooms |  |

### recording
| event | what happened | noisy |
|---|---|---|
| `recording.started` | Recording started |  |
| `recording.stopped` | Recording stopped |  |
| `recording.ready` | A recording finished processing (with download URL) |  |

### captions
| event | what happened | noisy |
|---|---|---|
| `captions.started` | Live captions were turned on |  |
| `captions.stopped` | Live captions were turned off |  |

### caption
| event | what happened | noisy |
|---|---|---|
| `caption.line` | A final caption line (live transcript) | yes |

### booking
| event | what happened | noisy |
|---|---|---|
| `booking.created` | A meeting was booked |  |
| `booking.rescheduled` | A booking moved |  |
| `booking.cancelled` | A booking was cancelled |  |
| `booking.reminder` | A reminder went out |  |
| `booking.pending_payment` | A paid booking is waiting for payment (slot held) |  |
| `booking.paid` | A booking was paid and confirmed |  |
| `booking.payment_late` | Payment arrived after the hold expired |  |
| `booking.payment_expired` | An unpaid hold expired and the slot was released |  |
| `booking.attendee_joined` | The person who booked joined the meeting |  |
| `booking.host_joined` | The host joined a booked meeting |  |
| `booking.no_show` | Nobody (or only one side) turned up to a booked meeting |  |
| `booking.completed` | A booked meeting took place (both sides joined) |  |
| `booking.attendee_confirmed` | The attendee replied YES / CONFIRM by SMS or WhatsApp |  |

### availability
| event | what happened | noisy |
|---|---|---|
| `availability.updated` | A host's bookable hours changed |  |

### payment
| event | what happened | noisy |
|---|---|---|
| `payment.checkout_created` | A checkout / payment link was created |  |
| `payment.refunded` | A payment was refunded (full or partial) |  |
| `payment.failed` | A payment attempt failed or the checkout expired |  |
| `payment.disputed` | A customer opened a dispute / chargeback |  |

### email
| event | what happened | noisy |
|---|---|---|
| `email.sent` | An email was sent | yes |
| `email.failed` | An email could not be sent |  |

### sms
| event | what happened | noisy |
|---|---|---|
| `sms.sent` | An SMS / WhatsApp message was sent | yes |
| `sms.failed` | An SMS / WhatsApp message failed |  |
| `sms.delivered` | The carrier / WhatsApp confirmed delivery (or read) | yes |
| `sms.received` | Someone texted back (SMS) |  |

### whatsapp
| event | what happened | noisy |
|---|---|---|
| `whatsapp.received` | Someone replied on WhatsApp |  |

### push
| event | what happened | noisy |
|---|---|---|
| `push.sent` | A push notification was delivered | yes |
| `push.failed` | A push notification failed |  |
| `push.subscribed` | A device turned on push notifications |  |
| `push.unsubscribed` | A device turned off / lost push notifications |  |

### chat_app
| event | what happened | noisy |
|---|---|---|
| `chat_app.sent` | A Slack / Discord / Teams... notification was posted | yes |
| `chat_app.failed` | Posting to a chat app failed |  |
| `chat_app.command` | Someone used a /meet command in Slack, Discord or Telegram |  |

### crm
| event | what happened | noisy |
|---|---|---|
| `crm.synced` | A booking was written to the CRM |  |
| `crm.failed` | Writing to the CRM failed |  |
| `crm.contact_updated` | The CRM reported a contact change (inbound webhook) |  |
| `crm.contact_deleted` | The CRM reported a contact deletion (inbound webhook) |  |
| `crm.event` | Any other inbound CRM webhook (raw) |  |

### calendar
| event | what happened | noisy |
|---|---|---|
| `calendar.connected` | A host connected a calendar |  |
| `calendar.disconnected` | A host disconnected a calendar |  |
| `calendar.synced` | A booking was written to a connected calendar | yes |
| `calendar.failed` | Writing to a calendar failed |  |
| `calendar.changed` | A connected calendar changed (busy times refreshed) |  |

### conference
| event | what happened | noisy |
|---|---|---|
| `conference.created` | A Zoom / Teams / Meet / Webex / Jitsi meeting was created for a booking |  |
| `conference.updated` | The external meeting moved |  |
| `conference.deleted` | The external meeting was deleted |  |
| `conference.failed` | Creating / updating the external meeting failed |  |
| `conference.started` | The external meeting started (Zoom / Webex / Teams webhook) |  |
| `conference.ended` | The external meeting ended |  |
| `conference.participant_joined` | Someone joined the external meeting |  |
| `conference.participant_left` | Someone left the external meeting |  |
| `conference.recording_ready` | The external meeting's cloud recording is ready |  |

### sso
| event | what happened | noisy |
|---|---|---|
| `sso.login` | Someone signed in with SSO |  |
| `sso.failed` | An SSO sign-in failed |  |

### role
| event | what happened | noisy |
|---|---|---|
| `role.saved` | A role was created or edited |  |
| `role.deleted` | A role was deleted |  |

### branding
| event | what happened | noisy |
|---|---|---|
| `branding.updated` | Branding changed |  |

### api_key
| event | what happened | noisy |
|---|---|---|
| `api_key.created` | An API key was created |  |
| `api_key.revoked` | An API key was revoked |  |

### inbound
| event | what happened | noisy |
|---|---|---|
| `inbound.received` | An inbound action / webhook was accepted |  |
| `inbound.rejected` | An inbound webhook failed its signature check |  |

### integration
| event | what happened | noisy |
|---|---|---|
| `integration.error` | Any integration failed (catch-all) |  |

### webhook
| event | what happened | noisy |
|---|---|---|
| `webhook.test` | A test event you triggered |  |

## Getting events in (inbound webhooks)

Paste these URLs into each app. Every request is signature-checked; anything that fails the check returns 401 and fires `inbound.rejected`.

| App | URL | Set up with | Becomes |
|---|---|---|---|
| Zapier / Make / n8n / your code | `/api/inbound/actions` | an API key, or `NodeMeet(inbound_secret=...)` + `X-NodeMeet-Signature` | runs an action (below), then `inbound.received` |
| Zoom | `/api/inbound/zoom` | `add_conferencing("zoom", ..., webhook_secret=)` | `conference.started/ended/participant_joined/left/recording_ready` |
| Webex | `/api/inbound/webex` | `add_conferencing("webex", ..., webhook_secret=)` | same as Zoom |
| Microsoft Graph (Outlook calendar, Teams) | `/api/inbound/microsoft` | `add_calendars(outlook=..)` / `add_conferencing("teams", client_state=)`, then `await meet.calendars.watch(host)` | `calendar.changed`, `conference.ended` |
| Google Calendar | `/api/inbound/google-calendar` | `add_calendars(google=..)`, then `await meet.calendars.watch(host)` | `calendar.changed` (busy times refresh) |
| Twilio SMS / WhatsApp | `/api/inbound/twilio` (delivery receipts are set up automatically) | `add_sms("twilio", ...)` | `sms.delivered/failed/received`, `whatsapp.received`, plus YES / CANCEL / RESCHEDULE replies |
| WhatsApp Cloud | `/api/inbound/whatsapp` (GET verify + POST) | `add_sms("whatsapp", ..., app_secret=, verify_token=)` | same as Twilio |
| Slack | `/api/inbound/slack` | `add_bot("slack", SIGNING_SECRET)` | `/meet` command replies, `chat_app.command` |
| Telegram | `/api/inbound/telegram` | `add_bot("telegram", SECRET_TOKEN)`, then setWebhook | `/meet` commands |
| Discord | `/api/inbound/discord` | `add_bot("discord", PUBLIC_KEY)` | `/meet` slash command |
| HubSpot | `/api/inbound/hubspot` | `add_crm("hubspot", TOKEN, client_secret=)` | `crm.contact_updated/deleted`, `crm.event` |
| Pipedrive | `/api/inbound/pipedrive` | `add_crm("pipedrive", ..., webhook_user=, webhook_password=)` | same as HubSpot |
| Salesforce Outbound Messages | `/api/inbound/salesforce` | `add_crm("salesforce", ..., org_id=)` | same as HubSpot |
| Zoho workflow webhooks | `/api/inbound/zoho?token=...` | `add_crm("zoho", ..., webhook_token=)` | same as HubSpot |
| Stripe / Razorpay / PayPal | `/api/payments/<provider>/webhook` | `add_payments(...)` | `booking.paid`, `payment.refunded/failed/disputed` |

### Actions API
```json
POST /api/inbound/actions
{"action": "booking.create", "data": {"host_id": "ada", "start": "2026-10-05T10:00:00Z", "name": "Ravi", "email": "ravi@x.com"}}
```
Available actions:
- `room.create`, `room.update`
- `room.broadcast` (an announcement in a live meeting)
- `link.create`
- `booking.create`, `booking.cancel`, `booking.reschedule`
- `push.notify`
- `chat.post` (to every connected chat app)
- `sms.send`, `email.send`
- `event.emit` (fires `custom.<name>` to all your subscribers)

### Chat commands
- `/meet [room]`: host and guest links.
- `/meet book <host>`: the booking page.
- `/meet today [host]`: the next 24 hours of bookings.
- `/meet help`.

Limit who can use them with `add_bot(..., allow=[...])`.

### Text replies
Attendees can reply to booking texts with:
- **YES** to confirm (`booking.attendee_confirmed`).
- **CANCEL** to cancel.
- **RESCHEDULE** to get a link for picking a new time.
- **HELP**.

Turn these off with `add_sms(..., keywords=False)`.
