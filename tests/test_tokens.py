import pytest

from nodemeet import ExpiredToken, InvalidToken, Role, TokenSigner
from nodemeet.tokens import permissions_for

SECRET = "test-secret-that-is-long-enough"


def test_roundtrip_and_claims():
    s = TokenSigner(SECRET)
    tok = s.create("standup", "u1", "host", name="Ada", meta={"org": 7})
    c = s.verify(tok, room="standup")
    assert c.room == "standup" and c.user_id == "u1" and c.role == Role.HOST == "host"
    assert c.name == "Ada" and c.meta == {"org": 7}
    assert c.can("moderate.kick") and c.can("audio.publish")


def test_roles_permissions():
    assert "audio.publish" not in permissions_for("viewer")
    assert "moderate.kick" not in permissions_for("participant")
    assert "chat.send" in permissions_for(Role.VIEWER)
    assert Role.parse("Teacher") == "teacher"  # custom role names are allowed
    with pytest.raises(ValueError):
        Role.parse("no spaces!")


def test_tampering_and_wrong_secret():
    s = TokenSigner(SECRET)
    tok = s.create("r", "u")
    head, payload, sig = tok.split(".")
    with pytest.raises(InvalidToken):
        s.verify(f"{head}.{payload}x.{sig}")
    with pytest.raises(InvalidToken):
        TokenSigner("another-secret-long-enough").verify(tok)
    with pytest.raises(InvalidToken):
        s.verify("garbage")


def test_room_binding_and_wildcard():
    s = TokenSigner(SECRET)
    with pytest.raises(InvalidToken):
        s.verify(s.create("a", "u"), room="b")
    assert s.verify(s.create("*", "u"), room="anything").room == "*"


def test_expiry_with_fake_clock():
    now = [1_000_000.0]
    s = TokenSigner(SECRET, clock=lambda: now[0], leeway=0)
    tok = s.create("r", "u", ttl=60)
    s.verify(tok)
    now[0] += 61
    with pytest.raises(ExpiredToken):
        s.verify(tok)
    assert s.verify(s.create("r", "u", ttl=0)).exp is None  # ttl=0 -> never expires


def test_empty_secret_rejected():
    with pytest.raises(ValueError):
        TokenSigner("")
