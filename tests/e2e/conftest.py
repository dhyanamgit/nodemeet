"""Browser tests (Playwright). Run:  python -m pytest tests/e2e -v
Pick browsers with NM_BROWSERS=chromium,firefox,webkit (default: all three; missing ones are skipped)."""
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "src"))
try:
    from playwright import sync_api as playwright_api
except ImportError:  # the tests in this folder skip themselves
    playwright_api = None

from e2e_server import build  # noqa: E402

BROWSERS = [b.strip() for b in os.environ.get("NM_BROWSERS", "chromium,firefox,webkit").split(",") if b.strip()]


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server():
    port = _free_port()
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "e2e_server.py"), str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1)
            break
        except Exception:  # noqa: BLE001
            if proc.poll() is not None:
                raise RuntimeError("e2e server crashed:\n" + proc.stdout.read().decode(errors="replace"))
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("e2e server did not start")
    meet = build(port)  # same secret: links made here work on the server
    yield base, meet
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def pw():
    if playwright_api is None:
        pytest.skip("browser tests need: pip install playwright && python -m playwright install")
    with playwright_api.sync_playwright() as p:
        yield p


@pytest.fixture(scope="session", params=BROWSERS)
def browser(request, pw):
    name = request.param
    opts = {}
    if name == "chromium":
        opts["args"] = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                        "--autoplay-policy=no-user-gesture-required"]
    elif name == "firefox":
        opts["firefox_user_prefs"] = {"media.navigator.streams.fake": True, "media.navigator.permission.disabled": True,
                                      "media.autoplay.default": 0}
    try:
        b = getattr(pw, name).launch(**opts)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"{name} is not installed ({exc.__class__.__name__}); run: python -m playwright install {name}")
    b.nm_name = name
    yield b
    b.close()


@pytest.fixture
def new_page(browser):
    contexts = []

    def make(url, **ctx):
        if browser.nm_name == "chromium":
            ctx.setdefault("permissions", ["camera", "microphone"])
        c = browser.new_context(**ctx)
        contexts.append(c)
        page = c.new_page()
        page.set_default_timeout(20000)
        page.goto(url)
        return page

    yield make
    for c in contexts:
        c.close()
