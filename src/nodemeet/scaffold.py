"""Starter projects for ``nodemeet init``."""
from __future__ import annotations

import secrets
from pathlib import Path
from typing import Dict, List

CONFIG = '''# nodemeet settings. ${VARS} come from the environment or the .env file next to this one.
secret = "${NODEMEET_SECRET}"
db = "${NODEMEET_DB:-meet.db}"            # or postgres://user:pass@host/app, mongodb://..., redis://...
email = "${NODEMEET_EMAIL:-console}"      # or smtp://user:pass@smtp.host:587?from=you@site.com
base_url = "${NODEMEET_BASE_URL:-http://localhost:8080}"
waiting_room = true                       # people not on a room's join list wait to be let in

[brand]
name = "My App"
color = "#6366f1"
# logo = "https://example.com/logo.png"
# font = "Inter"
# theme = "light"

[rooms.standup]
preset = "open"                           # meeting, open, private, webinar, classroom, interview, 1on1

[rooms.team-sync]
join_list = ["ada@example.com"]           # these people skip the waiting room

[hours.ada]
spec = "mon-fri 9-17"                     # "mon-fri 9am-12pm, 2pm-6pm; sat 10-13" also works
timezone = "UTC"
minutes = 30
buffer = 10
name = "Ada"
email = "ada@example.com"

[roles.moderator]
can = ["moderate"]                        # plain words: mic, camera, screen, chat, record, kick...
badge = "MOD"
rank = 50

# [payments]
# provider = "stripe"                     # reads STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET
# [calendars]
# google = true                           # reads GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
# [sso]
# github = true
# admins = ["you@example.com"]
# [sms]
# provider = "twilio"                     # reads TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM
'''

ENV = '''# Keep this file out of git.
NODEMEET_SECRET={secret}
# NODEMEET_DB=postgres://user:pass@localhost/app
# NODEMEET_EMAIL=smtp://you@gmail.com:APP_PASSWORD@smtp.gmail.com:587
# STRIPE_SECRET_KEY=sk_test_...
# STRIPE_WEBHOOK_SECRET=whsec_...
'''

BASIC = '''"""Run:  python app.py   then open the host link it prints."""
from nodemeet import NodeMeet

meet = NodeMeet.from_config("nodemeet.toml")


@meet.on("join")
def joined(room, person):
    print(f"{person.name} joined {room.id}")


@meet.on("booked")
def booked(booking):
    print(f"New booking: {booking.attendee_name} at {booking.start}")


if __name__ == "__main__":
    print("host link :", meet.host_link("standup", "Me"))
    print("guest link:", meet.guest_link("standup"))
    print("booking   :", meet.booking_page_url("ada"))
    meet.run(port=8080)
'''

FASTAPI = '''"""Run:  pip install fastapi uvicorn   then   uvicorn app:app --reload"""
from fastapi import FastAPI
from nodemeet import NodeMeet

meet = NodeMeet.from_config("nodemeet.toml", base_url="http://localhost:8000/meet")
app = FastAPI()
app.mount("/meet", meet.asgi())


@app.get("/join/{room}")
def join(room: str, name: str = "Guest", host: bool = False):
    # Put your own login check here, then hand out a link.
    return {"url": meet.host_link(room, name) if host else meet.guest_link(room, name)}
'''

BOOKING = '''"""A booking site in one file. Run:  python app.py  and open the booking page it prints."""
from nodemeet import NodeMeet

meet = NodeMeet.from_config("nodemeet.toml")
page = meet.hours("ada", "mon-fri 9am-12pm, 2pm-5pm", timezone="Asia/Kolkata", minutes=30,
                  buffer=10, notice="2h", name="Ada", email="ada@example.com")


@meet.on("booked")
def booked(booking):
    print("booked:", booking.attendee_email, booking.start)


if __name__ == "__main__":
    print("booking page:", page.url)
    print("embed it    :", page.embed())
    meet.run(port=8080)
'''

TEMPLATES: Dict[str, str] = {"basic": BASIC, "fastapi": FASTAPI, "booking": BOOKING}


def init_project(directory: str = ".", template: str = "basic", force: bool = False) -> List[str]:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    files = {"app.py": TEMPLATES[template], "nodemeet.toml": CONFIG,
             ".env": ENV.format(secret=secrets.token_urlsafe(32)), ".gitignore": ".env\n*.db\nrecordings/\n"}
    written: List[str] = []
    for name, body in files.items():
        path = root / name
        if path.exists() and not force:
            if name == ".gitignore" and ".env" not in path.read_text("utf-8"):
                path.write_text(path.read_text("utf-8").rstrip("\n") + "\n.env\n", "utf-8")
                written.append(str(path) + " (updated)")
            continue
        path.write_text(body, "utf-8")
        written.append(str(path))
    return written
