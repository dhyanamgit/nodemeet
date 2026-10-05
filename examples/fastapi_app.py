"""nodemeet inside FastAPI (pip install fastapi uvicorn nodemeet)

    uvicorn fastapi_app:app --port 8000
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from nodemeet import Availability, NodeMeet

meet = NodeMeet("change-me-to-a-long-random-secret", api_key="master-key",
                base_url="http://localhost:8000/meet")


@asynccontextmanager
async def lifespan(app):
    await meet.startup()  # mounted ASGI apps don't get lifespan events, so start it here
    await meet.bookings.set_availability(Availability.from_hours(
        "dr-ada", "Asia/Kolkata", {"mon-fri": "09:00-17:00"}, title="Consultation"))
    yield
    await meet.shutdown()


app = FastAPI(lifespan=lifespan)
app.mount("/meet", meet.asgi())


@app.get("/", response_class=HTMLResponse)
async def home():
    token = meet.create_token("team", "u42", "host", name="Ada")  # after YOUR auth
    return f"<h1>FastAPI + nodemeet</h1>{meet.embed_room('team', token)}{meet.embed_booking('dr-ada')}"
