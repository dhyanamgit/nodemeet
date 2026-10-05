"""Starts a nodemeet server for the browser tests:  python e2e_server.py 8765"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
from nodemeet import NodeMeet  # noqa: E402

SECRET = "e2e-secret-change-me-not-for-production"


def build(port: int) -> NodeMeet:
    meet = NodeMeet(SECRET, base_url=f"http://127.0.0.1:{port}", email="memory", reminders=False, sfu=False)
    meet.room("e2e")                         # waiting room on (default)
    meet.room("open-e2e", preset="open")     # no waiting room
    meet.hours("ada", "mon-sun 0-23:59", timezone="UTC", minutes=30, name="Ada")
    return meet


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    build(port).run(host="127.0.0.1", port=port, server="dev", banner=True)
