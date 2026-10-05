"""Run nodemeet on its own: python examples/standalone.py"""
from nodemeet import Availability, NodeMeet, SQLiteStorage

meet = NodeMeet("change-me-to-a-long-random-secret", api_key="dev-admin-key",
                storage=SQLiteStorage("nodemeet.sqlite3"), base_url="http://localhost:8080")


async def seed(app):
    await meet.bookings.set_availability(Availability.from_hours(
        "dr-ada", "Asia/Kolkata", {"mon-fri": "09:00-13:00, 14:00-18:00"},
        duration_minutes=30, buffer_after=10, min_notice_minutes=120, title="Consultation",
        host_name="Dr. Ada", host_email="ada@example.com"))
    print("Host link: ", meet.invite("demo", "ada", role="host", name="Ada"))
    print("Guest link:", meet.invite("demo", "guest", name="Guest"))
    print("Booking page:", meet.booking_page_url("dr-ada"))

meet.app().on_startup.append(seed)
meet.run(port=8080)
