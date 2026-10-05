# Connecting an LMS (LTI 1.3)

nodemeet speaks **LTI 1.3 / LTI Advantage**, the standard that Moodle, Canvas, Blackboard, Brightspace, Schoology, Sakai and Open edX use to add tools. You connect it once and it stays connected. Your course platform keeps feeding students into the LMS, and the LMS feeds them into nodemeet.

```
course platform --(enrolments)--> LMS --(LTI 1.3)--> nodemeet
                                   ^                    |
                                   +---- grades --------+
```

## 1. Add it to your app (3 lines)

```python
from nodemeet import NodeMeet
meet = NodeMeet(db="postgresql://...", base_url="https://meet.school.edu")
lti = meet.add_lti(grade_attendance=True, attendance_minutes=30)
print(lti.registration_url())        # the link you give the LMS admin (valid 7 days)
meet.run()
```

Use a real database. The LMS registration and the tool's key pair are saved there, so they survive restarts.

## 2. Connect the LMS

**Easiest way (dynamic registration):** works on Moodle 4.1+, Canvas, Brightspace and Sakai.

| LMS | Where to paste `registration_url()` |
|---|---|
| Moodle | Site admin -> Plugins -> Activity modules -> External tool -> Manage tools -> "Tool URL" -> **Add LTI Advantage** |
| Canvas | Admin -> Developer Keys -> + LTI Registration (newer Canvas versions), or use the JSON config below |
| Brightspace | Admin Tools -> Manage Extensibility -> LTI Advantage -> Register Tool -> Dynamic |

**Manual way:** for any LMS, or when dynamic registration isn't available.

1. Give the LMS these URLs (replace `https://meet.school.edu` with your `base_url`):

   | Field | URL |
   |---|---|
   | Login / initiate login URL | `https://meet.school.edu/lti/login` |
   | Redirect / launch URL | `https://meet.school.edu/lti/launch` |
   | Public keyset (JWKS) URL | `https://meet.school.edu/lti/jwks` |
   | Canvas JSON config | `https://meet.school.edu/lti/canvas.json` |

2. Turn on the services: Names and Role Provisioning, Assignment and Grade Services, and Deep Linking.
3. The LMS then shows you a client ID and a deployment ID. Put them in your code:

```python
meet.add_lti(moodle=("https://moodle.school.edu", CLIENT_ID, DEPLOYMENT_ID))
meet.add_lti(canvas=(CLIENT_ID, DEPLOYMENT_ID))                      # Instructure-hosted Canvas
meet.add_lti(brightspace=("school.brightspace.com", CLIENT_ID, DEPLOYMENT_ID))
meet.add_lti(blackboard=(CLIENT_ID, DEPLOYMENT_ID))
meet.add_lti(LTIPlatform(issuer, client_id, auth_url, token_url, jwks_url, [deployment]))  # anything else
```

You can also add a platform at runtime with `POST /api/lti/platforms` (master key), using a body like `{"preset": "moodle", "args": [url, client_id, deployment]}`.

## 3. What happens

- **A teacher adds an activity:** they pick "Live meeting room" or "Booking page" (that's deep linking). They can tick "send attendance to the gradebook".
- **Anyone clicks the course link:**
  - They're signed in already, with no extra login.
  - Instructors, TAs and admins become `host`; students become `participant`.
  - Each course link is its own permanent room.
  - Enrolled people skip the waiting room.
- **Roster sync:** `await lti.sync_roster(room)` or `POST /api/lti/rooms/{room}/sync-roster` copies the course roster to the room's join list.
- **Grades:**
  - `await lti.send_grade(room, "asha@school.edu", 90)` or `POST /api/lti/rooms/{room}/grades` sends a grade.
  - With `grade_attendance=True`, everyone gets graded automatically when the meeting ends: full marks after `attendance_minutes`, partial marks below that.
- **Events:** `lti.launch`, `lti.deep_link`, `lti.registered`, `lti.roster_synced`, `lti.grade_sent` and `lti.failed` go to your webhooks and to `@meet.on_event`.

## 4. Customise

```python
meet.add_lti(role_map={"TeachingAssistant": "moderator", "Learner": "student"},  # your custom roles
             role_for=lambda launch: "viewer" if "Observer" in launch.roles else None,  # None = use role_map
             room_for=lambda launch: f"course-{launch.context['id']}",   # one room per course instead of per link
             on_launch=my_hook,            # return a URL / Response to take over, or {"room":..,"role":..}
             join_list=True,               # enrolled people skip the waiting room
             registration="invite")        # "open" = anyone may register (not recommended), None = off
```

`launch` objects have these fields: `user_id`, `email`, `name`, `sub`, `roles`, `is_staff`, `is_learner`, `context` (the course), `resource_link`, `custom` and `claims` (the raw token).

## No grades? No roster?

Grades are optional. Nothing gets graded unless you turn on `grade_attendance` or call `send_grade`. To remove gradebook access completely:

```python
meet.add_lti(grades=False)                 # never asks the LMS for gradebook access
meet.add_lti(grades=False, roster=False)   # meetings only: launch, roles, rooms, deep linking
```

## Security

Every launch is checked against the LMS's published keys:

- RS256 signature
- issuer
- audience and authorised party (`aud` / `azp`)
- expiry
- nonce: single use, tied to a signed state
- LTI version
- deployment ID

Only rooms that the LMS itself created can be targeted by a link's custom room id. Registration links are signed and expire after 7 days.

## Course platforms

- **Platforms with an LMS behind them** (Open edX, Moodle-based platforms, Canvas Catalog, Blackboard-based platforms): these work through the LMS, as described above.
- **Thinkific, Teachable, Kajabi and LearnDash** don't use LTI. Connect them with their "student enrolled" webhooks and `meet.add_to_join_list(room, email)`, or the `/api/inbound/actions` API.

## How it was tested

`tests/test_lti.py` runs against a fake LMS that signs real RS256 tokens. It covers:

- the login → launch flow
- forged, expired, replayed and wrong-audience tokens
- deep linking
- dynamic registration
- roster paging
- grade passback
- the key being kept after a restart

It has **not** been run against a live Moodle or Canvas yet. Do that next, and send me any error message you get.

---

# Making your own LMS an LTI platform

If you're building your own LMS, `nodemeet.lti_platform.LTIPlatformKit` gives it the platform side of LTI 1.3, the same part Moodle and Canvas implement. nodemeet, and any other LTI 1.3 tool such as H5P, Turnitin or Zoom's LTI app, then connect to your LMS the normal way.

```python
from nodemeet.lti_platform import LTIPlatformKit

kit = LTIPlatformKit(
    "https://lms.example.com/lti-platform",          # issuer = where you mount it
    secret=os.environ["LMS_LTI_SECRET"], db="postgresql://...",
    authenticate=lambda req: session_user_id(req),   # who is logged in to YOUR LMS (from req.headers cookie)
    user=lambda uid: {"name": ..., "email": ...},    # profile shared with tools
    members=lambda course_id: [{"user_id": "s1", "roles": ["Learner"], "email": ..., "name": ...}],
    on_score=lambda s: save_grade(s["context_id"], s["userId"], s["scoreGiven"], s["scoreMaximum"]))
app.mount("/lti-platform", kit.asgi())               # FastAPI/Starlette; Django: nodemeet.asgi.route
```

Meetings only? Use `LTIPlatformKit(..., grades=False)` (and `roster=False` too if you like). The gradebook endpoints are then not served at all, and you don't need `on_score`.

Your LMS calls these, then redirects the browser to the URL they return:

| When | Call |
|---|---|
| Admin connects a tool | `kit.registration_url("https://meet.example.com/meet/lti/register?invite=...")` (open in a popup) |
| Teacher clicks "add activity" | `await kit.deep_link(tool_id, user_id, ["Instructor"], {"id": course_id, "title": ...}, return_to=course_url)` |
| Anyone clicks an added activity | `await kit.launch_link(link_id, user_id, roles)` (list them with `await kit.links(course_id)`) |
| A fixed link (e.g. course menu) | `await kit.launch(tool_id, user_id, roles, course, {"id": "menu", "title": "Live classes"})` |
| Gradebook page | `await kit.gradebook(course_id)` |

The endpoints it serves, all under your mount:

- `/.well-known/openid-configuration`
- `/lti/jwks`
- `/lti/auth` (OIDC)
- `/lti/token` (client credentials with signed JWT)
- `/lti/register` (dynamic registration)
- `/lti/deep-link-return`
- `/lti/nrps/{course}` (paged)
- `/lti/ags/{course}/lineitems` (plus `/{id}`, `/{id}/scores` and `/{id}/results`)

Security:

- **Sign-in check:** `authenticate` makes sure the logged-in user is the one being launched. If you leave it out, each launch link works only once.
- **Single use:** registration links, client-assertion `jti`s and deep-link nonces are each accepted only once.
- **Course access:** tools can only read rosters and grades of courses they were launched from.

`tests/test_lti_platform.py` runs your LMS (the kit) and nodemeet against each other over HTTP, with a browser that follows the redirects and auto-submitted forms. It covers:

- dynamic registration
- a teacher adding a graded live class
- a student joining, and a teacher joining as host
- an impersonation attempt being blocked
- roster sync
- attendance grades arriving in the LMS
- paging, scope and wrong-course checks
