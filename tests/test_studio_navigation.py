"""Run Studio navigation in Chromium against the real local HTTP server."""
from pathlib import Path
import shutil

import pytest

import dashboard
from test_dashboard import server  # noqa: F401


def test_publications_opens_with_incorrect_system_mime_types(server, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
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
    with playwright.sync_playwright() as driver:
        bundled = Path(driver.chromium.executable_path)
        chromium = str(bundled) if bundled.is_file() else shutil.which("chromium")
        if not chromium:
            pytest.skip("Install Chromium with python -m playwright install chromium")
        browser = driver.chromium.launch(executable_path=chromium)
        try:
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda message: errors.append(message.text)
                    if message.type == "error" else None)
            page.goto(base)
            # Clear the startup race so the test exercises the authenticated state request.
            page.wait_for_function("typeof state === 'undefined' || Boolean(state.token)")
            page.get_by_role("button", name="Publicaciones").click()
            try:
                playwright.expect(page.locator("#publications-view")).to_be_visible(timeout=4000)
            except AssertionError as exc:
                raise AssertionError(f"{exc}\nChromium errors: {errors}") from exc
            playwright.expect(page.get_by_role("heading", name="Publicaciones")).to_be_visible()
            playwright.expect(page.locator("#tiktok-account")).to_contain_text("TikTok requiere")
            playwright.expect(page.locator("#new-view")).to_be_hidden()
            page.get_by_role("button", name="New project").click()
            playwright.expect(page.locator("#new-view")).to_be_visible()
            playwright.expect(page.locator("#publications-view")).to_be_hidden()
            assert not errors, errors
        finally:
            browser.close()
