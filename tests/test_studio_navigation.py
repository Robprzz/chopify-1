"""Run Studio navigation in Chromium against the real local HTTP server."""
from pathlib import Path
import shutil

import pytest

import dashboard
from test_dashboard import server  # noqa: F401


@pytest.fixture
def chromium_page():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        bundled = Path(driver.chromium.executable_path)
        chromium = str(bundled) if bundled.is_file() else shutil.which("chromium")
        if not chromium:
            pytest.skip("Install Chromium with python -m playwright install chromium")
        browser = driver.chromium.launch(executable_path=chromium)
        try:
            yield browser.new_page(), playwright.expect
        finally:
            browser.close()


def test_publications_opens_with_incorrect_system_mime_types(server, monkeypatch, chromium_page):
    guess_type = dashboard.mimetypes.guess_type

    def system_mime(name):
        # Windows can obtain script and stylesheet MIME types from the registry.
        if name.endswith((".js", ".css")):
            return "text/plain", None
        return guess_type(name)

    monkeypatch.setattr(dashboard.mimetypes, "guess_type", system_mime)
    httpd, base = server
    monkeypatch.setattr(httpd.app.tiktok.api, "key", "")
    monkeypatch.setattr(httpd.app.tiktok.api, "secret", "")
    page, expect = chromium_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text)
            if message.type == "error" else None)
    with page.expect_response(base + "/publications.js") as script:
        page.goto(base)
    assert script.value.status == 200
    assert script.value.headers["content-type"] == "text/javascript; charset=utf-8"
    page.locator("#show-publications").click()
    with page.expect_response(base + "/api/tiktok/state") as tiktok:
        page.locator("#calendar-tiktok").click()
    assert tiktok.value.status == 200
    expect(page.get_by_role("heading", name="Buzón e historial de TikTok")).to_be_visible()
    expect(page.locator("#tiktok-account")).to_contain_text("TikTok requiere")
    expect(page.locator("#new-view")).to_be_hidden()

    with page.expect_response(base + "/api/live") as live:
        page.locator("#show-live").click()
    assert live.value.status == 200
    assert live.value.json() == {"sessions": []}
    expect(page.locator("#live-view")).to_be_visible()
    expect(page.locator("#publications-view")).to_be_hidden()
    page.locator("#show-projects").click()
    page.locator("#new-project").click()
    expect(page.locator("#new-view")).to_be_visible()
    assert not errors, errors


def test_publications_explains_missing_script(server, monkeypatch, chromium_page):
    httpd, base = server
    # Reproduce a running older backend that does not serve the new script.
    monkeypatch.delitem(httpd.static_files, "publications.js")
    page, expect = chromium_page
    with page.expect_response(base + "/publications.js") as script:
        page.goto(base)
    assert script.value.status == 404
    page.locator("#show-publications").click()
    page.locator("#calendar-tiktok").click()
    expect(page.locator("#notice")).to_contain_text("inicia dashboard.py desde la copia actualizada")
