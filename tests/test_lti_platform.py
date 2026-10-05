"""End to end, the way a real school connects: a custom LMS built on LTIPlatformKit and
nodemeet as the tool, talking over (in-process) HTTP with a browser that follows redirects
and auto-submitted forms. Dynamic registration -> teacher adds a meeting -> student joins ->
roster sync -> attendance lands in the LMS gradebook."""
import html
import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from helpers import run
from nodemeet import MemoryMailer, NodeMeet
from nodemeet.integrations.http_client import HTTPClient, HTTPResult
from nodemeet.lti import S_NRPS, LTIPlatform
from nodemeet.lti_platform import LTIPlatformKit

SECRET = "lti-platform-test-secret-long-enough!"
PEOPLE = {"t1": {"name": "Prof Kumar", "email": "kumar@school.edu"},
          "s1": {"name": "Asha Rao", "email": "asha@school.edu"},
          "s2": {"name": "Ravi Shah", "email": "ravi@school.edu"}}
ROSTER = [{"user_id": "t1", "roles": ["Instructor"], **PEOPLE["t1"]},
          {"user_id": "s1", "roles": ["Learner"], **PEOPLE["s1"]},
          {"user_id": "s2", "roles": ["Learner"], **PEOPLE["s2"]},
          {"user_id": "s3", "roles": ["Learner"], "status": "Inactive", "email": "left@school.edu"}]
COURSE = {"id": "phy101", "title": "Physics 101", "label": "PHY101"}


class Net:
    """Routes http://lms.test and http://meet.test to the two ASGI apps."""

    def __init__(self):
        self.apps = {}

    async def __call__(self, method, url, headers, body):
        u = urlsplit(url)
        app = self.apps.get(u.netloc)
        if app is None:
            return HTTPResult(404, b"no such host")
        sent = []
        scope = {"type": "http", "method": method, "path": u.path, "root_path": "", "query_string": u.query.encode(),
                 "scheme": "http", "client": ("127.0.0.1", 1),
                 "headers": [(k.lower().encode(), v.encode()) for k, v in {"host": u.netloc, **headers}.items()]}
        done = [False]

        async def receive():
            if done[0]:
                return {"type": "http.disconnect"}
            done[0] = True
            return {"type": "http.request", "body": body or b"", "more_body": False}

        async def send(m):
            sent.append(m)
        await app(scope, receive, send)
        hdrs = {k.decode(): v.decode() for k, v in sent[0]["headers"]}
        return HTTPResult(sent[0]["status"], b"".join(m.get("body", b"") for m in sent[1:]), hdrs)


class Browser:
    """Follows 302s and auto-posting forms like a real browser; stops at the meeting room."""

    def __init__(self, net, user=None):
        self.net, self.user, self.history = net, user, []

    async def go(self, url, method="GET", form=None):
        for _ in range(12):
            headers = {"x-user": self.user} if self.user and "lms.test" in url else {}
            body = None
            if form is not None:
                body = urlencode(form).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            res = await self.net(method, url.split("#")[0], headers, body)
            self.history.append((method, url, res.status))
            if res.status in (301, 302, 303):
                url, method, form = res.headers["location"], "GET", None
                if "/r/" in url:
                    return url, res
                continue
            m = re.search(r'<form id=f method=post action="([^"]+)">(.*?)</form>', res.text, re.S)
            if res.status == 200 and m and "document.getElementById" in res.text:
                fields = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', m.group(2)))
                url, method, form = html.unescape(m.group(1)), "POST", {k: html.unescape(v) for k, v in fields.items()}
                continue
            return url, res
        raise AssertionError(f"redirect loop: {self.history}")


def build(tool_kw=None, **lms_kw):
    net = Net()
    meet = NodeMeet(SECRET, api_key="k", mailer=MemoryMailer(), reminders=False, sfu=False, base_url="http://meet.test")
    tool_kw = {"grade_attendance": True, "attendance_minutes": 30} if tool_kw is None else tool_kw
    lti = meet.add_lti(http=HTTPClient(transport=net), allow_http=True, **tool_kw)
    scores, tools = [], []
    kit = LTIPlatformKit("http://lms.test", secret=SECRET + "-lms", name="School LMS",
                         authenticate=lambda req: req.header("x-user") or None,
                         user=lambda uid: PEOPLE.get(uid), members=lambda ctx: ROSTER if ctx == "phy101" else [],
                         on_score=scores.append, on_tool_registered=tools.append, http=HTTPClient(transport=net), **lms_kw)
    net.apps = {"meet.test": meet.asgi(), "lms.test": kit.asgi()}
    return net, meet, lti, kit, scores, tools


def test_school_connects_teacher_adds_class_student_joins_grades_flow_back():
    net, meet, lti, kit, scores, tools = build()

    async def scenario():
        # 1. LMS admin pastes nodemeet's registration link into the LMS -> one click, connected
        admin = Browser(net, "admin")
        url, res = await admin.go(kit.registration_url(lti.registration_url()))
        assert res.status == 200 and "org.imsglobal.lti.close" in res.text, res.text
        tool = tools[0]
        assert tool.login_url == "http://meet.test/lti/login" and tool.redirect_uris == ["http://meet.test/lti/launch"]
        plat = (await lti.platforms())[0]
        assert plat.issuer == "http://lms.test" and plat.deployment_ids == [tool.deployment_id]

        # 2. teacher clicks "add activity" -> nodemeet's picker -> picks a live room with grading
        teacher = Browser(net, "t1")
        url, res = await teacher.go(await kit.deep_link(tool.client_id, "t1", ["Instructor"], COURSE,
                                                         return_to="http://lms.test/course/phy101"))
        assert res.status == 200 and "Add to Physics 101" in res.text
        state = re.search(r'name=state value="([^"]+)"', res.text).group(1)
        url, res = await teacher.go("http://meet.test/lti/deeplink", "POST",
                                    {"state": html.unescape(state), "kind": "meeting", "title": "Live lecture",
                                     "waiting_room": "1", "grade": "1"})
        assert url == "http://lms.test/course/phy101"            # back in the course page
        links = await kit.links("phy101")
        assert len(links) == 1 and links[0]["title"] == "Live lecture" and links[0]["lineitem"]
        room = links[0]["custom"]["room"]

        # 3. student clicks the activity -> lands in the room, signed in, skips the waiting room
        student = Browser(net, "s1")
        url, _ = await student.go(await kit.launch_link(links[0]["id"], "s1", ["Learner"]))
        assert url.startswith(f"http://meet.test/r/{room}#token=")
        claims = meet.tokens.verify(url.split("#token=")[1])
        assert claims.user_id == "asha@school.edu" and str(claims.role) == "participant" and claims.name == "Asha Rao"
        # teacher opening the same activity is the host
        url, _ = await teacher.go(await kit.launch_link(links[0]["id"], "t1", ["Instructor"]))
        assert str(meet.tokens.verify(url.split("#token=")[1]).role) == "host"

        # someone else replaying Asha's launch link while signed in as Ravi is refused by the LMS
        url, res = await Browser(net, "s2").go(await kit.launch_link(links[0]["id"], "s1", ["Learner"]))
        assert res.status == 403 and json.loads(res.body)["error"] == "not_signed_in"

        # 4. roster sync over Names & Roles (inactive student left out)
        out = await lti.sync_roster(room)
        assert out["members"] == 3
        cfg = await meet.storage.get_room(room)
        assert cfg.on_join_list("ravi@school.edu") and not cfg.on_join_list("left@school.edu")

        # 5. meeting ends -> attendance grades land in the LMS gradebook via AGS
        await meet.emit_webhook("meeting.ended", {"room": room, "attendees": [
            {"user_id": "asha@school.edu", "seconds": 2400}, {"user_id": "ravi@school.edu", "seconds": 900},
            {"user_id": "kumar@school.edu", "seconds": 3000}]})
        await meet.webhooks.drain()
        await meet.webhooks.drain()
        got = {s["userId"]: s["scoreGiven"] for s in scores}
        assert got == {"s1": 100.0, "s2": 50.0}                    # teacher isn't graded
        assert scores[0]["lineitem"] == links[0]["lineitem"] and scores[0]["label"].endswith("(attendance)")
        assert len(await kit.gradebook("phy101")) == 2
    run(scenario())


def test_manual_registration_services_and_security():
    net, meet, lti, kit, scores, tools = build()

    async def scenario():
        # the "add external tool" form instead of dynamic registration
        tool = await kit.add_tool("nodemeet", "http://meet.test/lti/login", "http://meet.test/lti/launch",
                                  "http://meet.test/lti/jwks")
        await lti.add_platform(LTIPlatform("http://lms.test", tool.client_id, "http://lms.test/lti/auth",
                                           "http://lms.test/lti/token", "http://lms.test/lti/jwks",
                                           [tool.deployment_id], "school"))
        url, _ = await Browser(net, "s1").go(await kit.launch(tool.client_id, "s1", ["Learner"], COURSE,
                                                              {"id": "week-1", "title": "Week 1 class"}))
        assert "/r/lti-" in url
        room = urlsplit(url).path.split("/")[-1]
        assert (await meet.storage.get_room(room)).name == "Week 1 class"

        # services: paging, scopes, wrong course, bad tokens
        p = (await lti.platforms())[0]
        at = await lti.access_token(p, [S_NRPS])
        page = await net("GET", "http://lms.test/lti/nrps/phy101?limit=2", {"Authorization": f"Bearer {at}"}, None)
        assert len(page.json()["members"]) == 2 and 'rel="next"' in page.headers["link"]
        other = await net("GET", "http://lms.test/lti/nrps/chem200", {"Authorization": f"Bearer {at}"}, None)
        assert other.status == 404                                   # tool never used in that course
        nope = await net("GET", "http://lms.test/lti/ags/phy101/lineitems", {"Authorization": f"Bearer {at}"}, None)
        assert nope.status == 403                                    # NRPS token can't read grades
        bad = await net("GET", "http://lms.test/lti/nrps/phy101", {"Authorization": "Bearer junk"}, None)
        assert bad.status == 401
        # grade without a line item from deep linking -> nodemeet creates one through AGS
        out = await lti.send_grade(room, "asha@school.edu", 75, comment="good")
        assert scores[-1]["userId"] == "s1" and scores[-1]["scoreGiven"] == 75 and scores[-1]["comment"] == "good"
        # results readback + "older score ignored"
        tok = await lti.access_token(p, ["https://purl.imsglobal.org/spec/lti-ags/scope/result.readonly"])
        li = scores[-1]["lineitem"]
        res = await net("GET", f"{li}/results", {"Authorization": f"Bearer {tok}"}, None)
        assert res.json()[0]["resultScore"] == 75
        # registration link works once
        reg = kit.registration_url(lti.registration_url())
        assert (await Browser(net).go(reg))[1].status == 200
        assert (await Browser(net).go(reg))[1].status >= 400
        # unknown tool / bad redirect at the auth endpoint
        r = await net("GET", "http://lms.test/lti/auth?" + urlencode({"client_id": tool.client_id, "redirect_uri": "http://evil.test/x",
                      "response_type": "id_token", "scope": "openid", "nonce": "n"}), {}, None)
        assert r.status == 400 and r.json()["error"] == "bad_redirect_uri"
        cfg = (await net("GET", "http://lms.test/.well-known/openid-configuration", {}, None)).json()
        assert cfg["registration_endpoint"] == "http://lms.test/lti/register"
    run(scenario())


def test_no_grades_anywhere():
    """Meetings-only school: grades switched off on both sides; nothing about gradebooks is asked for or sent."""
    net, meet, lti, kit, scores, tools = build({"grades": False}, grades=False)

    async def scenario():
        await Browser(net, "admin").go(kit.registration_url(lti.registration_url()))
        tool = tools[0]
        assert not any("lti-ags" in s for s in tool.scopes) and any("nrps" in s for s in tool.scopes)
        teacher = Browser(net, "t1")
        _, res = await teacher.go(await kit.deep_link(tool.client_id, "t1", ["Instructor"], COURSE))
        assert "gradebook" not in res.text
        state = re.search(r'name=state value="([^"]+)"', res.text).group(1)
        await teacher.go("http://meet.test/lti/deeplink", "POST", {"state": html.unescape(state), "kind": "meeting",
                                                                    "title": "Talk", "grade": "1"})
        link = (await kit.links("phy101"))[0]
        assert "lineitem" not in link
        url, _ = await Browser(net, "s1").go(await kit.launch_link(link["id"], "s1", ["Learner"]))
        room = link["custom"]["room"]
        assert f"/r/{room}#token=" in url
        assert "ags" not in (await lti._get("lti_context", room))      # LMS sent no gradebook claim
        assert (await lti.sync_roster(room))["members"] == 3            # roster still works
        await meet.emit_webhook("meeting.ended", {"room": room, "attendees": [{"user_id": "asha@school.edu", "seconds": 9999}]})
        await meet.webhooks.drain()
        assert scores == []
        try:
            await lti.send_grade(room, "asha@school.edu", 10)
            raise AssertionError("should refuse")
        except Exception as exc:
            assert "grades are turned off" in str(exc)
        assert (await net("GET", "http://lms.test/lti/ags/phy101/lineitems", {}, None)).status == 404
    run(scenario())
