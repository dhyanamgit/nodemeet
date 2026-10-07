"""LTI 1.3: OIDC login -> launch -> room, deep linking, dynamic registration, Names & Roles,
Assignment & Grades, and the security checks - against a fake LMS (no network)."""
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from asgi_client import ASGIClient
from helpers import run
from nodemeet import MemoryMailer, NodeMeet
from nodemeet.integrations.http_client import HTTPClient, HTTPResult
from nodemeet.lti import (AGS, C, DL, NRPS, LTIPlatform, generate_key, jwk_public_key, jwt_encode, jwt_parts,
                          jwt_verify_signature, public_jwk)

SECRET = "lti-tests-secret-long-enough-1234!!"
ISS, CID, DEP = "https://lms.test", "tool-123", "dep-1"
ADMIN = {"Authorization": "Bearer k"}
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


class FakeLMS:
    """Signs id_tokens like Moodle/Canvas and answers JWKS, token, NRPS, AGS and registration calls."""

    def __init__(self):
        self.key, self.kid = generate_key(), "lms-key-1"
        self.calls, self.scores, self.registered = [], [], None
        self.http = HTTPClient(transport=self.transport)

    def platform(self, **kw):
        return LTIPlatform(ISS, CID, f"{ISS}/auth", f"{ISS}/token", f"{ISS}/jwks", [DEP], "fakelms", **kw)

    def id_token(self, nonce, roles=("Learner",), msg="LtiResourceLinkRequest", **extra):
        now = int(time.time())
        claims = {"iss": ISS, "aud": CID, "sub": "u-42", "iat": now, "exp": now + 300, "nonce": nonce,
                  "email": "Asha@School.edu", "name": "Asha Rao",
                  C + "version": "1.3.0", C + "message_type": msg, C + "deployment_id": DEP,
                  C + "roles": [f"http://purl.imsglobal.org/vocab/lis/v2/membership#{r}" for r in roles],
                  C + "context": {"id": "course-7", "title": "Physics 101"},
                  C + "resource_link": {"id": "link-1", "title": "Weekly live class"},
                  NRPS: {"context_memberships_url": f"{ISS}/nrps/course-7"},
                  AGS: {"scope": ["x"], "lineitems": f"{ISS}/ags/course-7/lineitems"}}
        claims.update(extra)
        return jwt_encode(claims, self.key, self.kid)

    async def transport(self, method, url, headers, body):
        self.calls.append((method, url))
        u = urlsplit(url)
        if u.path == "/jwks":
            return HTTPResult(200, json.dumps({"keys": [public_jwk(self.key, self.kid)]}).encode())
        if u.path == "/token":
            form = parse_qs(body.decode())
            self.assertion = form["client_assertion"][0]
            return HTTPResult(200, json.dumps({"access_token": "at-1", "expires_in": 3600}).encode())
        if u.path == "/nrps/course-7":
            assert headers["Authorization"] == "Bearer at-1"
            if "page=2" in url:
                members = [{"user_id": "u-43", "email": "ravi@school.edu", "name": "Ravi", "status": "Active",
                            "roles": ["http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"]},
                           {"user_id": "u-44", "email": "gone@school.edu", "status": "Inactive", "roles": []}]
                return HTTPResult(200, json.dumps({"members": members}).encode())
            members = [{"user_id": "u-42", "email": "asha@school.edu", "name": "Asha", "status": "Active",
                        "roles": ["http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"]}]
            return HTTPResult(200, json.dumps({"members": members}).encode(),
                              {"link": f'<{ISS}/nrps/course-7?page=2>; rel="next"'})
        if u.path.endswith("/lineitems"):
            return HTTPResult(201, json.dumps({"id": f"{ISS}/ags/course-7/lineitems/9"}).encode())
        if u.path.endswith("/scores"):
            assert headers["Content-Type"] == "application/vnd.ims.lis.v1.score+json"
            self.scores.append((url, json.loads(body)))
            return HTTPResult(200, b"{}")
        if u.path == "/.well-known/openid-configuration":
            return HTTPResult(200, json.dumps({
                "issuer": ISS, "authorization_endpoint": f"{ISS}/auth", "token_endpoint": f"{ISS}/token",
                "jwks_uri": f"{ISS}/jwks", "registration_endpoint": f"{ISS}/register",
                "https://purl.imsglobal.org/spec/lti-platform-configuration": {"product_family_code": "moodle"}}).encode())
        if u.path == "/register":
            assert headers["Authorization"] == "Bearer reg-tok"
            self.registered = json.loads(body)
            return HTTPResult(200, json.dumps({"client_id": "new-client", "https://purl.imsglobal.org/spec/lti-tool-configuration": {"deployment_id": "7"}}).encode())
        return HTTPResult(404, b"")


def make(lms, **kw):
    meet = NodeMeet(SECRET, api_key="k", mailer=MemoryMailer(), reminders=False, sfu=False, base_url="http://test")
    seen = []
    meet.on_event("lti.*")(lambda e, d: seen.append((e, d)))
    lti = meet.add_lti(lms.platform(), http=lms.http, allow_http=True, **kw)
    return meet, lti, seen


async def login(c, roles=("Learner",), **extra):
    """Run the OIDC dance; returns (lms_state, nonce) after checking the auth redirect."""
    r = await c.get(f"/lti/login?iss={ISS}&login_hint=u-42&client_id={CID}&lti_message_hint=abc")
    assert r.status == 302
    loc = urlsplit(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    assert loc.path == "/auth" and q["response_mode"] == "form_post" and q["redirect_uri"] == "http://test/lti/launch"
    assert q["lti_message_hint"] == "abc" and q["prompt"] == "none"
    return q["state"], q["nonce"]


async def launch(c, lms, roles=("Learner",), tamper=None, **extra):
    state, nonce = await login(c)
    tok = lms.id_token(nonce, roles, **extra)
    if tamper:
        tok = tamper(tok, state, nonce)
    body = f"id_token={tok}&state={state}".encode()
    return await c.request("POST", "/lti/launch", body=body, headers=FORM), state, tok


def test_jwt_roundtrip_and_jwks():
    k = generate_key()
    t = jwt_encode({"a": 1}, k, "kid1")
    h, claims, si, sig = jwt_parts(t)
    assert h["kid"] == "kid1" and claims == {"a": 1}
    jwt_verify_signature(si, sig, jwk_public_key(public_jwk(k, "kid1")))
    with pytest.raises(Exception):
        jwt_verify_signature(si + b"x", sig, jwk_public_key(public_jwk(k, "kid1")))


def test_launch_learner_and_instructor():
    lms = FakeLMS()
    meet, lti, seen = make(lms)

    async def scenario():
        c = ASGIClient(meet.asgi())
        jw = (await c.get("/lti/jwks")).json()["keys"][0]
        assert jw["kty"] == "RSA" and jw["kid"]
        r, _, _ = await launch(c, lms)
        assert r.status == 302, r.text
        loc = r.headers["location"]
        assert loc.startswith("http://test/r/lti-") and "#token=" in loc
        room = urlsplit(loc).path.split("/")[-1]
        claims = meet.tokens.verify(loc.split("#token=")[1])
        assert claims.user_id == "asha@school.edu" and str(claims.role) == "participant"
        cfg = await meet.storage.get_room(room)
        assert cfg.name == "Weekly live class" and cfg.on_join_list("asha@school.edu")   # enrolled -> no waiting room
        assert cfg.metadata["lti"]["context"] == "course-7"
        # same course link again (instructor) -> same permanent room, host role
        r2, _, _ = await launch(c, lms, roles=("Instructor",))
        assert urlsplit(r2.headers["location"]).path.endswith(room)
        assert str(meet.tokens.verify(r2.headers["location"].split("#token=")[1]).role) == "host"
        await meet.webhooks.drain()
        ev = [d for e, d in seen if e == "lti.launch"]
        assert ev[0]["course"]["title"] == "Physics 101" and ev[1]["role"] == "host"
    run(scenario())


def test_security_checks():
    lms = FakeLMS()
    meet, lti, seen = make(lms)

    async def scenario():
        c = ASGIClient(meet.asgi())
        # replayed launch
        r, state, tok = await launch(c, lms)
        assert r.status == 302
        again = await c.request("POST", "/lti/launch", body=f"id_token={tok}&state={state}".encode(), headers=FORM)
        assert again.status == 401 and again.json()["error"] == "replay"
        # forged signature (signed by someone else's key)
        evil = generate_key()
        bad = lambda t, s, n: jwt_encode(jwt_parts(t)[1], evil, lms.kid)
        assert (await launch(c, lms, tamper=bad))[0].json()["error"] == "bad_signature"
        # wrong audience, expired, wrong deployment, wrong nonce, LTI 1.1-style version
        cases = {"bad_audience": {"aud": "someone-else"}, "expired": {"exp": int(time.time()) - 3600},
                 "bad_deployment": {C + "deployment_id": "dep-evil"}, "bad_version": {C + "version": "1.1"}}
        for code, extra in cases.items():
            assert (await launch(c, lms, **extra))[0].json()["error"] == code, code
        wrong_nonce = lambda t, s, n: lms.id_token("other-nonce")
        assert (await launch(c, lms, tamper=wrong_nonce))[0].json()["error"] == "bad_nonce"
        # unknown LMS
        r = await c.get("/lti/login?iss=https://evil.test&login_hint=x")
        assert r.status == 400 and r.json()["error"] == "unknown_platform"
        await meet.webhooks.drain()
        assert any(e == "lti.failed" for e, _ in seen)
        # a forged custom room id pointing at a room this LMS did not create is ignored
        await meet.create_room(room_id="ceo-room", name="private")
        r, _, _ = await launch(c, lms, roles=("Instructor",), **{C + "custom": {"room": "ceo-room"}})
        assert "/r/ceo-room" not in r.headers["location"]
    run(scenario())


def test_deep_linking_meeting_and_booking():
    lms = FakeLMS()
    meet, lti, seen = make(lms)
    settings = {DL + "deep_linking_settings": {"deep_link_return_url": f"{ISS}/dl-return", "data": "opaque-xyz",
                                               "accept_types": ["ltiResourceLink"]}}

    async def scenario():
        c = ASGIClient(meet.asgi())
        r, _, _ = await launch(c, lms, roles=("Learner",), msg="LtiDeepLinkingRequest", **settings)
        assert r.status == 403  # students can't add activities
        r, _, _ = await launch(c, lms, roles=("Instructor",), msg="LtiDeepLinkingRequest", **settings)
        assert r.status == 200 and "Add to Physics 101" in r.text
        state = r.text.split('name=state value="')[1].split('"')[0]
        body = f"state={state}&kind=meeting&title=Lab+session&grade=1&waiting_room=1".encode()
        r = await c.request("POST", "/lti/deeplink", body=body, headers=FORM)
        assert r.status == 200 and f'action="{ISS}/dl-return"' in r.text
        jwt = r.text.split('name="JWT" value="')[1].split('"')[0]
        _, claims, si, sig = jwt_parts(jwt)
        jwt_verify_signature(si, sig, jwk_public_key((await c.get("/lti/jwks")).json()["keys"][0]))
        assert claims["iss"] == CID and claims["aud"] == ISS and claims[C + "deployment_id"] == DEP
        assert claims[C + "message_type"] == "LtiDeepLinkingResponse" and claims[DL + "data"] == "opaque-xyz"
        item = claims[DL + "content_items"][0]
        rid = item["custom"]["room"]
        assert item["url"] == "http://test/lti/launch" and item["lineItem"]["scoreMaximum"] == 100
        assert (await meet.storage.get_room(rid)).metadata["lti"]["grade"] is True
        # a learner opening that link lands in the picked room
        r, _, _ = await launch(c, lms, **{C + "custom": {"room": rid}})
        assert f"/r/{rid}#" in r.headers["location"]
        # booking page item -> launch redirects to the booking page, prefilled
        bstate = None
        r, _, _ = await launch(c, lms, roles=("Instructor",), msg="LtiDeepLinkingRequest", **settings)
        bstate = r.text.split('name=state value="')[1].split('"')[0]
        r = await c.request("POST", "/lti/deeplink", body=f"state={bstate}&kind=booking&title=Office+hours&booking=prof-k".encode(), headers=FORM)
        item = jwt_parts(r.text.split('name="JWT" value="')[1].split('"')[0])[1][DL + "content_items"][0]
        assert item["custom"] == {"booking": "prof-k"}
        r, _, _ = await launch(c, lms, **{C + "custom": {"booking": "prof-k"}})
        assert r.headers["location"].startswith("http://test/book/prof-k?name=Asha")
        await meet.webhooks.drain()
        assert [e for e, _ in seen].count("lti.deep_link") == 2
    run(scenario())


def test_roster_sync_and_grades():
    lms = FakeLMS()
    meet, lti, seen = make(lms, attendance_minutes=30)

    async def scenario():
        c = ASGIClient(meet.asgi())
        r, _, _ = await launch(c, lms)
        room = urlsplit(r.headers["location"]).path.split("/")[-1]
        out = (await c.post(f"/api/lti/rooms/{room}/sync-roster", headers=ADMIN)).json()
        assert out["members"] == 2 and out["added"] == 1           # Ravi added, inactive member skipped
        cfg = await meet.storage.get_room(room)
        assert cfg.on_join_list("ravi@school.edu") and not cfg.on_join_list("gone@school.edu")
        _, claims, _, _ = jwt_parts(lms.assertion)                   # client-credentials JWT is ours
        assert claims["iss"] == CID and claims["aud"] == f"{ISS}/token"
        # manual grade -> line item auto-created, score posted with the LMS user id
        g = (await c.post(f"/api/lti/rooms/{room}/grades", headers=ADMIN, json={"user": "asha@school.edu", "score": 80})).json()
        assert g["lms_user"] == "u-42"
        url, score = lms.scores[-1]
        assert url == f"{ISS}/ags/course-7/lineitems/9/scores" and score["scoreGiven"] == 80 and score["userId"] == "u-42"
        # attendance grading on meeting.ended (30 min needed; Ravi stayed 15 -> 50)
        lti.grade_attendance = True
        await meet.emit_webhook("meeting.ended", {"room": room, "attendees": [
            {"user_id": "asha@school.edu", "seconds": 2400}, {"user_id": "ravi@school.edu", "seconds": 900},
            {"user_id": "stranger@x.com", "seconds": 999}]})
        await meet.webhooks.drain()
        await meet.webhooks.drain()
        got = {s["userId"]: s["scoreGiven"] for _, s in lms.scores[1:]}
        assert got == {"u-42": 100.0, "u-43": 50.0}
        await meet.webhooks.drain()
        assert [e for e, _ in seen].count("lti.grade_sent") == 3
        # tenant keys can't touch rooms of other tenants; no LTI context -> 404
        assert (await c.post("/api/lti/rooms/nope/grades", headers=ADMIN, json={"user": "a", "score": 1})).status == 404
    run(scenario())


def test_dynamic_registration_and_admin_api():
    lms = FakeLMS()
    meet, lti, seen = make(lms)

    async def scenario():
        c = ASGIClient(meet.asgi())
        conf = f"{ISS}/.well-known/openid-configuration"
        r = await c.get(f"/lti/register?openid_configuration={conf}&registration_token=reg-tok")
        assert r.status == 403  # needs an invite link by default
        url = (await c.get("/api/lti/registration-url", headers=ADMIN)).json()["url"]
        assert url.startswith("http://test/lti/register?invite=")
        r = await c.get(url.replace("http://test", "") + f"&openid_configuration={conf}&registration_token=reg-tok")
        assert r.status == 200 and "org.imsglobal.lti.close" in r.text
        reg = lms.registered
        assert reg["initiate_login_uri"] == "http://test/lti/login" and reg["jwks_uri"] == "http://test/lti/jwks"
        assert "contextmembership.readonly" in reg["scope"]
        plats = (await c.get("/api/lti/platforms", headers=ADMIN)).json()["platforms"]
        new = next(p for p in plats if p["client_id"] == "new-client")
        assert new["deployment_ids"] == ["7"] and new["name"] == "moodle"
        await meet.webhooks.drain()
        assert any(e == "lti.registered" for e, _ in seen)
        # two registrations for one issuer -> login must say which client
        r = await c.get(f"/lti/login?iss={ISS}&login_hint=x")
        assert r.json()["error"] == "unknown_platform"
        assert (await c.get(f"/lti/login?iss={ISS}&login_hint=x&client_id=new-client")).status == 302
        # admin add / delete with a preset; requires the master key
        assert (await c.post("/api/lti/platforms", json={"preset": "canvas", "args": ["cv-1", "d1"]})).status == 401
        p = (await c.post("/api/lti/platforms", headers=ADMIN, json={"preset": "canvas", "args": ["cv-1", "d1"]})).json()
        assert p["issuer"] == "https://canvas.instructure.com" and p["jwks_url"].endswith("/api/lti/security/jwks")
        assert (await c.request("DELETE", f"/api/lti/platforms/{p['id']}", headers=ADMIN)).status == 200
        assert all(x["id"] != p["id"] for x in (await c.get("/api/lti/platforms", headers=ADMIN)).json()["platforms"])
        cj = (await c.get("/lti/canvas.json")).json()
        assert cj["oidc_initiation_url"] == "http://test/lti/login" and cj["public_jwk_url"] == "http://test/lti/jwks"
    run(scenario())


def test_presets_and_key_persistence():
    m = LTIPlatform.moodle("https://moodle.school.edu/", "c", "d")
    assert m.auth_login_url == "https://moodle.school.edu/mod/lti/auth.php" and m.deployment_ids == ["d"]
    b = LTIPlatform.brightspace("school.brightspace.com", "c")
    assert b.issuer == "https://school.brightspace.com" and b.auth_server.startswith("https://api.brightspace")
    assert LTIPlatform.blackboard("abc").jwks_url.endswith("/applications/abc/jwks.json")
    meet = NodeMeet(SECRET, mailer=MemoryMailer(), reminders=False, sfu=False, base_url="http://test")
    lti = meet.add_lti(moodle=("https://moodle.school.edu", "c", "d"), canvas=("cv",))
    assert "lti:moodle,canvas" in repr(meet)

    async def scenario():
        k1 = (await lti.jwks())["keys"][0]
        meet2 = NodeMeet(SECRET, storage=meet.storage, mailer=MemoryMailer(), reminders=False, sfu=False)
        lti2 = meet2.add_lti()
        assert (await lti2.jwks())["keys"][0]["n"] == k1["n"]  # same key after "restart" on the same database
    run(scenario())
