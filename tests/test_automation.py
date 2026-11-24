"""Automation settings, HTTP protection and real Chromium persistence."""
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import automation
from test_dashboard import request, running_server, server  # noqa: F401
from test_studio_navigation import chromium_page  # noqa: F401

SETTINGS_ENDPOINT = "/api/automation/settings"
APPROVED = {
    "live_clipping": True,
    "ai_captions": True,
    "subtitle_correction": True,
    "force_vertical_format": False,
    "duplicate_prevention": True,
    "minimum_clip_score": 6.0,
    "daily_publication_limit": 5,
    "priority_engine": automation.DEFAULT_SETTINGS["priority_engine"],
    "ai_engine": "local",
    "ai_provider": "ollama",
    "require_publication_consent": True,
    "timezone": "America/Santo_Domingo",
}


def test_settings_defaults_persist_and_retry_idempotently(tmp_path):
    store = automation.SettingsStore(tmp_path)
    initial = store.read()
    assert initial == {"revision": 0, "settings": APPROVED}
    assert not store.path.exists()
    changes = {**APPROVED, "daily_publication_limit": 2, "timezone": "America/La_Paz"}
    saved = store.save(changes, initial["revision"])
    reopened = automation.SettingsStore(tmp_path)
    assert reopened.read() == reopened.save(changes, 0) == {"revision": 1, "settings": changes}


@pytest.mark.parametrize(("field", "value"), [
    ("minimum_clip_score", 5.99), ("minimum_clip_score", True), ("minimum_clip_score", float("nan")),
    ("daily_publication_limit", 6), ("daily_publication_limit", 2.0),
    ("timezone", "America/Not_A_Zone"), ("timezone", "../UTC"),
    ("duplicate_prevention", False), ("require_publication_consent", False),
    ("ai_engine", "cloud"), ("ai_provider", "other"), ("live_clipping", 1),
    ("token", "could-be-a-secret"),
])
def test_settings_validate_before_writing(tmp_path, field, value):
    store = automation.SettingsStore(tmp_path)
    with pytest.raises(ValueError):
        store.save({**APPROVED, field: value}, 0)
    assert not store.path.exists()


def test_concurrent_writers_do_not_overwrite_newer_revision(tmp_path):
    store = automation.SettingsStore(tmp_path)
    initial = store.save(APPROVED, 0)

    def save(limit):
        candidate = {**APPROVED, "daily_publication_limit": limit}
        try:
            return store.save(candidate, initial["revision"])
        except automation.SettingsConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, (1, 2)))
    assert results.count(None) == 1
    assert store.read() == next(result for result in results if result)


def test_invalid_database_is_preserved(tmp_path):
    store = automation.SettingsStore(tmp_path)
    original = b"unreadable local data"
    store.path.write_bytes(original)
    with pytest.raises(automation.SettingsUnavailable):
        store.read()
    with pytest.raises(automation.SettingsUnavailable):
        store.save(APPROVED, 0)
    assert store.path.read_bytes() == original


def test_automation_http_authorization_and_validation(server):
    httpd, base = server
    token = httpd.app.token
    config = request(base, SETTINGS_ENDPOINT)
    assert config == {"revision": 0, "settings": APPROVED}
    for candidate, origin in [(None, base), (token, "https://untrusted.invalid")]:
        with pytest.raises(HTTPError) as response:
            request(base, SETTINGS_ENDPOINT, config, candidate, origin)
        assert response.value.code == 403
    with pytest.raises(HTTPError) as response:
        urlopen(Request(base + SETTINGS_ENDPOINT, headers={"Host": "untrusted.invalid"}))
    assert response.value.code == 403
    with pytest.raises(HTTPError) as response:
        request(base, SETTINGS_ENDPOINT, {
            **config, "settings": {**APPROVED, "require_publication_consent": False},
        }, token, base)
    assert response.value.code == 400
    assert request(base, SETTINGS_ENDPOINT) == config
    with urlopen(base + "/automation.js") as asset:
        assert asset.headers.get_content_type() == "text/javascript"
        assert asset.headers.get("X-Content-Type-Options") == "nosniff"


def test_settings_in_chromium_survive_server_restart(tmp_path, chromium_page):
    page, expect = chromium_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    open_automation = page.locator("#show-automation")
    save = page.get_by_role("button", name="Guardar ajustes")
    limit = page.get_by_label("Recomendaciones diarias", exact=False)
    vertical = page.get_by_label("Forzar exportación vertical")

    with running_server(tmp_path) as (httpd, base):
        project = request(base, "/api/projects", {"source": "recording.mp4"}, httpd.app.token)
        page.goto(base)
        page.locator("#show-settings").click()
        open_automation.click()
        expect(limit).to_have_value("5")
        expect(vertical).not_to_be_checked()
        limit.fill("2")
        with page.expect_response(lambda r: r.url.endswith(SETTINGS_ENDPOINT)
                                  and r.request.method == "POST") as result:
            save.click()
        assert result.value.status == 200
        expect(page.locator("#automation-status")).to_have_text("Ajustes guardados.")

        saved = request(base, SETTINGS_ENDPOINT)
        request(base, SETTINGS_ENDPOINT, {
            **saved, "settings": {**saved["settings"], "minimum_clip_score": 8.5},
        }, httpd.app.token)
        vertical.check()
        save.click()
        expect(page.locator("#automation-error")).to_contain_text("otra pestaña")
        expect(vertical).to_be_checked()
        page.get_by_role("button", name="Recargar ajustes").click()
        expect(page.get_by_label("Puntuación mínima", exact=False)).to_have_value("8.5")
        expect(vertical).not_to_be_checked()

    with running_server(tmp_path) as (_, base):
        page.goto(base)
        page.locator("#show-settings").click()
        open_automation.click()
        expect(limit).to_have_value("2")
        expect(page.get_by_label("Puntuación mínima", exact=False)).to_have_value("8.5")
        page.get_by_role("button", name="Live Studio").click()
        expect(page.locator("#automation-view")).to_be_hidden()
        expect(page.locator("#live-view")).to_be_visible()
        page.locator("#show-settings").click()
        open_automation.click()
        page.get_by_role("button", name="Publicaciones", exact=True).click()
        expect(page.locator("#automation-view")).to_be_hidden()
        expect(page.locator("#calendar-view")).to_be_visible()
        page.locator("#show-settings").click()
        open_automation.click()
        page.locator("#show-projects").click()
        page.locator("#new-project").click()
        expect(page.locator("#automation-view")).to_be_hidden()
        expect(page.locator("#new-view")).to_be_visible()
        page.locator("#show-settings").click()
        open_automation.click()
        page.locator("#show-projects").click()
        page.locator(f"#projects [data-id='{project['id']}']").click()
        expect(page.locator("#automation-view")).to_be_hidden()
        expect(page.locator("#project-view")).to_be_visible()
    assert not errors
