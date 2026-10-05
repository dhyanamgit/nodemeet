"""Not using aiohttp? Run `nodemeet serve` as a service and mint tokens from any framework."""
from flask import Flask, redirect

from nodemeet import TokenSigner  # pure Python, no aiohttp needed

app = Flask(__name__)
signer = TokenSigner("same-secret-as-the-nodemeet-server")


@app.get("/call/<room>")
def call(room):
    token = signer.create(room, "user-123", "participant", name="Ada")
    return redirect(f"https://meet.example.com/r/{room}#token={token}")
