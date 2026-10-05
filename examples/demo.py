"""nodemeet demo, using the short API. Run:  python demo.py   then open the links it prints.

Things to try in the browser:
  1. Open the HOST link and click Join.
  2. Open the GUEST link in another browser or a private window. The guest waits in the
     waiting room: let them in from People -> Admit.
  3. Open the WEBINAR link. Viewers can watch and chat, but can't talk.
  4. Open the BOOKING page and book a slot. The email shows up in this terminal.
"""
import logging

from nodemeet import NodeMeet

logging.basicConfig(level=logging.INFO, format="%(message)s")

meet = NodeMeet(db="demo.db", email="console", secret="demo-secret-change-me-in-production-please")
meet.brand(name="Demo Co", color="#7c3aed")

standup = meet.room("standup")                               # waiting room on (default)
town_hall = meet.room("town-hall", preset="webinar")
meet.role("teacher", can="moderate, record", badge="T", rank=50)
page = meet.hours("ada", "mon-sun 9-21", timezone="Asia/Kolkata", minutes=30, buffer=10, name="Ada")


@meet.on("join")
def joined(room, person):
    print(f">>> {person.name} joined {room.id} as {person.role}")


@meet.on("booked")
def booked(booking):
    print(f">>> booking: {booking.attendee_name} at {booking.start}")


if __name__ == "__main__":
    print("\nHOST    :", standup.host_link("Ada"))
    print("GUEST   :", standup.guest_link("Bob"))
    print("TEACHER :", standup.link("Ms Rao", "teacher"))
    print("WEBINAR :", town_hall.viewer_link("Viewer"))
    print("BOOKING :", page.url, "\n")
    meet.run(port=8080)
