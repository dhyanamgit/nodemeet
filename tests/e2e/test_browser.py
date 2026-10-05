"""Real browsers, real WebRTC: waiting room, admit, video, chat, booking widget, mobile layout.

The same steps were run in headless Chromium while building 0.7.1 (all passed).
Firefox gets fake camera/mic via prefs. WebKit has no fake devices, so there people join
with camera and mic off and the video check is skipped.
"""
import pytest

REMOTE_VIDEO = """() => [...document.querySelectorAll('.nm-tile video')]
  .some(v => v.videoWidth > 0 && v.srcObject && !v.closest('.nm-self'))"""


def join(page, browser, name=None):
    page.wait_for_selector('[data-x="join"]')
    if browser.nm_name == "webkit":  # no fake media devices in WebKit
        for box in ("mic", "cam"):
            el = page.query_selector(f'input[name="{box}"]')
            if el and el.is_checked():
                el.uncheck()
    page.click('[data-x="join"]')


def test_waiting_room_admit_video_and_chat(server, browser, new_page):
    base, meet = server
    host = new_page(meet.host_link("e2e", "Host"))
    join(host, browser)
    host.wait_for_selector('[data-tab="people"]')

    guest = new_page(meet.guest_link("e2e", "Guest"))
    join(guest, browser)
    guest.wait_for_selector(".nm-lobby-msg")                    # waiting room

    host.click('[data-tab="people"]')
    host.click('[data-lobby="admit"]')
    guest.wait_for_selector('[data-tab="chat"]')                # admitted

    host.wait_for_function("document.querySelectorAll('.nm-tile').length >= 2")
    guest.wait_for_function("document.querySelectorAll('.nm-tile').length >= 2")
    if browser.nm_name != "webkit":
        host.wait_for_function(REMOTE_VIDEO, timeout=30000)     # WebRTC media really flows

    guest.click('[data-tab="chat"]')
    guest.fill(".nm-chatform input", f"hello from {browser.nm_name}")
    guest.press(".nm-chatform input", "Enter")
    host.click('[data-tab="chat"]')
    host.wait_for_selector(f".nm-msg:has-text('hello from {browser.nm_name}')")


def test_open_room_skips_the_waiting_room(server, browser, new_page):
    base, meet = server
    a = new_page(meet.guest_link("open-e2e", "Alice"))
    join(a, browser)
    a.wait_for_selector('[data-tab="people"]')
    b = new_page(meet.guest_link("open-e2e", "Bob"))
    join(b, browser)
    b.wait_for_selector('[data-tab="people"]')
    assert b.query_selector(".nm-lobby-msg") is None
    a.wait_for_function("document.querySelectorAll('.nm-tile').length >= 2")


def test_booking_widget_books_a_slot(server, browser, new_page):
    base, meet = server
    page = new_page(f"{base}/book/ada")
    page.click(".nmb-day >> nth=0")
    page.click(".nmb-time >> nth=0")
    page.fill('.nmb-form input[name="name"]', f"E2E {browser.nm_name}")
    page.fill('.nmb-form input[name="email"]', f"e2e-{browser.nm_name}@example.com")
    page.click('.nmb-form button[type="submit"]')
    page.wait_for_selector(".nmb-ok")
    assert page.query_selector(".nmb-ok a.nmb-btn").get_attribute("href").startswith(base + "/r/")


def test_phone_sized_screen(server, browser, new_page, pw):
    if browser.nm_name == "firefox":
        pytest.skip("Playwright can't emulate phones in Firefox")
    base, meet = server
    phone = {k: v for k, v in pw.devices["iPhone 13"].items() if k != "default_browser_type"}
    page = new_page(meet.guest_link("open-e2e", "Phone"), **phone)
    join(page, browser)
    page.wait_for_selector('[data-tab="people"]')
    page.wait_for_selector(".nm-tile")
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 1                                         # fits the phone: no sideways scrolling
