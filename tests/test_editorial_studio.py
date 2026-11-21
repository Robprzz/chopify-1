"""SQLite jobs, Studio HTTP, and Chromium with a fake local Ollama server."""
from copy import deepcopy
from functools import lru_cache
import hashlib
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time
from urllib.error import HTTPError

import pytest

import automation
import editorial
import editorial_content as content
from editorial_providers import ProviderError
from editorial_source import key
from editorial_store import EditorialStore, MAX_PENDING, QueueFull
from fixtures.mock_ollama import MODEL, mock_ollama
import projects
from test_dashboard import request, running_server, server  # noqa: F401
from test_studio_navigation import chromium_page  # noqa: F401


SRT = """1
00:00:00,000 --> 00:00:01,100
¿Cómo elegir semillas para sembrar tomates?

2
00:00:01,100 --> 00:00:02,600
Deja secar las semillas y guárdalas en un frasco etiquetado.
"""


@lru_cache(maxsize=1)
def editorial_fixture_mp4():
    with TemporaryDirectory() as work:
        media = Path(work) / "fixture.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=size=160x90:rate=25",
                        "-f", "lavfi", "-i", "anullsrc", "-t", "3", "-c:v", "libx264", "-c:a", "aac",
                        str(media)], check=True)
        return media.read_bytes()


def write_export(root, source=None, live=False):
    if live:
        project = "live/" + "a" * 12
        folder = root / project
        (folder / "clips").mkdir(parents=True)
    else:
        project = projects.create_project(root, "grabacion.mp4", {})["id"]
        folder = root / project
        (folder / "clips").mkdir()
    clip = folder / "clips" / "tomates.mp4"
    if source:
        shutil.copyfile(source, clip)
    else:
        clip.write_bytes(editorial_fixture_mp4())
    clip.with_suffix(".srt").write_text(SRT, encoding="utf-8")
    projects.atomic_json(clip.with_suffix(".meta.json"), {"score": 8.3, "start": 0, "end": 3})
    if live:
        projects.atomic_json(folder / "live_session.json", {
            "triggers": [{"score": 9.1, "files": {"mp4": str(clip)}, "error": None}],
        })
    return project, clip


def wait_job(engine, ident, expected="done", deadline=7):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        row = engine.store.detail(ident)
        if row["state"] in {"done", "failed", "cancelled"}:
            assert row["state"] == expected, row
            return row
        time.sleep(.025)
    pytest.fail(f"Editorial job {ident} did not finish: {row}")


def configured(store, model=MODEL):
    return store.save_config({**content.DEFAULT_CONFIG, "model": model}, store.config()["revision"])


def test_sqlite_phase_one_migration_is_additive_and_old_leases_recover(tmp_path):
    settings = automation.SettingsStore(tmp_path)
    approved = settings.save({**automation.DEFAULT_SETTINGS, "minimum_clip_score": 8}, 0)
    store = EditorialStore(settings.path)
    store.migrate()
    assert settings.read() == approved
    assert store.config()["settings"]["platform"] == "tiktok"

    source = editorial.TEST_SOURCE
    config = {"platform": "tiktok", "profile": content.DEFAULT_CONFIG["profiles"]["tiktok"],
              "timeout": 5, "weights": content.WEIGHTS, "model": "", "model_digest": None,
              "prompt_version": content.PROMPT_VERSION}
    ident, _ = store.submit(source, config, key(config), False)
    assert store.claim("former")[0] == ident
    assert EditorialStore(settings.path).claim("other") is None
    with store.connect(write=True) as db:
        db.execute("UPDATE editorial_jobs SET lease=0 WHERE id=?", (ident,))
    engine = editorial.Engine(settings, lambda *_: None)
    try:
        row = wait_job(engine, ident)
        assert row["result"]["provider"] == "rules"
        assert settings.read() == approved
    finally:
        engine.close()


def test_bounded_queue_serial_and_idempotent_across_instances(tmp_path):
    store = EditorialStore(tmp_path / "db.sqlite")
    store.migrate()
    src = editorial.TEST_SOURCE
    config = {"platform": "tiktok", "timeout": 5, "model_digest": None}
    for i in range(MAX_PENDING):
        store.submit(src, config, str(i), False)
    assert store.submit(src, config, "0", True) == (1, True)
    with pytest.raises(QueueFull):
        store.submit(src, config, "new", False)
    assert store.claim("worker")[0] == 1
    assert EditorialStore(store.path).claim("another") is None
    store.cancel(1)
    store.finish(1, "worker", "done", {"provider": "rules"}, None, 0)
    assert store.detail(1)["state"] == "cancelled" and not store.detail(1)["result"]
    assert store.claim("another")[0] == 2


def test_editorial_http_cache_revisions_edits_provider_versions_and_fallback(server):
    httpd, base = server
    engine = httpd.app.editorial
    project, clip = write_export(httpd.app.root)
    args = {"project": project, "filename": clip.name, "platform": "tiktok"}
    original = hashlib.sha256(clip.read_bytes()).hexdigest()
    token = httpd.app.token
    with mock_ollama() as (fake, provider):
        engine.provider = provider
        first = request(base, "/api/editorial/status")
        with pytest.raises(HTTPError) as error:
            request(base, "/api/editorial/generate", args)
        assert error.value.code == 403
        config = {**content.DEFAULT_CONFIG, "model": MODEL}
        request(base, "/api/editorial/config", {"settings": config, "revision": first["config"]["revision"]}, token)
        posted = request(base, "/api/editorial/generate", args, token)
        job = wait_job(engine, posted["job_id"])
        assert job["result"]["model_version"] == "a" * 64
        assert job["result"]["original_score"] == 8.3
        assert request(base, "/api/editorial/generate", args, token) == {"job_id": job["id"], "reused": True}
        assert len(fake.calls) == 1
        variant = job["result"]["variants"][0]
        fields = {key: variant[key] for key in (*content.COPY_FIELDS, "hashtags")}
        custom = {**fields, "title": "Mi título editado"}
        edit = {"job_id": job["id"], "style": variant["style"], "fields": custom, "revision": job["revision"]}
        saved = request(base, "/api/editorial/edit", edit, token)
        assert saved["edits"][variant["style"]] == custom
        with pytest.raises(HTTPError) as conflict:
            request(base, "/api/editorial/edit", edit, token)
        assert conflict.value.code == 409

        fake.models[0]["digest"] = "d" * 64
        changed = request(base, "/api/editorial/generate", args, token)["job_id"]
        regenerated = wait_job(engine, changed)
        assert changed != job["id"] and regenerated["result"]["model_version"] == "d" * 64
        assert regenerated["edits"][variant["style"]] == custom
        assert request(base, "/api/editorial/edit", {
            **edit, "job_id": changed, "revision": regenerated["revision"], "fields": None,
        }, token)["edits"] == {}
        assert engine.store.detail(job["id"])["edits"][variant["style"]] == custom
        assert engine.store.detail(changed)["result"]["variants"][0]["title"] == variant["title"]

        clip.with_suffix(".srt").write_text(SRT.replace("tomates", "pimientos"), encoding="utf-8")
        changed_text = request(base, "/api/editorial/generate", args, token)["job_id"]
        after = wait_job(engine, changed_text)
        assert after["transcript_id"] != job["transcript_id"]
        alternate = request(base, "/api/editorial/generate", {**args, "platform": "facebook"}, token)["job_id"]
        assert wait_job(engine, alternate)["result"]["platform"] == "facebook"
        config = deepcopy(config)
        config["profiles"]["tiktok"]["instructions"] = "Selecciona un ejemplo sobre conservación."
        snapshot = engine.store.config()
        request(base, "/api/editorial/config", {"settings": config, "revision": snapshot["revision"]}, token)
        instructions = request(base, "/api/editorial/generate", args, token)["job_id"]
        assert instructions != changed_text
        wait_job(engine, instructions)
        assert "conservación" in fake.calls[-1]["messages"][0]["content"]

        total = len(fake.calls)
        fake.mode = "invalid"
        fallback = request(base, "/api/editorial/generate", {**args, "force": True}, token)["job_id"]
        failed_response = wait_job(engine, fallback)
        assert len(fake.calls) == total + 2
        assert failed_response["result"]["provider"] == "rules"
        assert "inválido" in failed_response["error"]

        fake.models = []
        missing = request(base, "/api/editorial/generate", args, token)["job_id"]
        assert "no está instalado" in wait_job(engine, missing)["error"]
        assert hashlib.sha256(clip.read_bytes()).hexdigest() == original


def test_stopped_and_uninstalled_ollama_keep_rules_available(tmp_path):
    class Offline:
        name = "ollama"

        def models(self):
            raise ProviderError("offline", "Servicio local detenido.")

    settings = automation.SettingsStore(tmp_path)
    engine = editorial.Engine(settings, lambda *_: None, Offline())
    try:
        engine.ensure()
        assert not engine.refresh_models()["available"]
        configured(engine.store)
        ident = engine.submit(None, None, None, True, test=True)["job_id"]
        assert wait_job(engine, ident)["result"]["provider"] == "rules"
    finally:
        engine.close()


def test_live_automatic_processing_and_phase_one_switch(server):
    httpd, base = server
    engine = httpd.app.editorial
    project, clip = write_export(httpd.app.root, live=True)
    engine.auto(project, clip.name)
    end = time.monotonic() + 5
    status = {}
    while time.monotonic() < end:
        status = request(base, "/api/editorial/status")
        if status["jobs"]:
            break
        time.sleep(.04)
    assert status["jobs"]
    row = wait_job(engine, status["jobs"][0]["id"])
    assert row["result"]["original_score"] == 9.1

    phase_one = httpd.app.automation_settings.read()
    httpd.app.automation_settings.save({**phase_one["settings"], "ai_captions": False}, phase_one["revision"])
    clip.with_suffix(".srt").write_text(SRT.replace("tomates", "pimientos"), encoding="utf-8")
    engine.auto(project, clip.name)
    end = time.monotonic() + 2
    while time.monotonic() < end and not engine.events.empty():
        time.sleep(.02)
    assert len(engine.store.history(None, None)["jobs"]) == 1
    # Manual requests remain available when automatic generation is disabled.
    posted = request(base, "/api/editorial/generate", {"project": project, "filename": clip.name}, httpd.app.token)
    assert wait_job(engine, posted["job_id"])["id"] != row["id"]


def test_conventional_export_completion_notifies_background_engine(server, monkeypatch):
    httpd, base = server
    project, clip = write_export(httpd.app.root)

    def fake_render(path, *_args):
        state = projects.read_project(path)
        assert not httpd.app.editorial.events.qsize()
        state.update(status="complete", last_outputs=[str(clip)])
        projects.save_project(path, state)

    monkeypatch.setattr(httpd.app, "_run", fake_render)
    request(base, f"/api/projects/{project}/run", {"action": "render"}, httpd.app.token)
    engine = httpd.app.editorial
    end = time.monotonic() + 5
    while time.monotonic() < end:
        rows = request(base, "/api/editorial/status")["jobs"]
        if rows:
            break
        time.sleep(.02)
    assert rows
    assert wait_job(engine, rows[0]["id"])["result"]["original_score"] == 8.3


def test_stop_request_interrupts_active_editorial_job(server):
    httpd, base = server
    engine = httpd.app.editorial
    token = httpd.app.token
    with mock_ollama() as (fake, provider):
        engine.provider = provider
        engine.ensure()
        configured(engine.store)
        fake.mode = "stall"
        result = request(base, "/api/editorial/test", {}, token)
        assert fake.entered.wait(3)
        response = request(base, "/api/editorial/cancel", result, token)
        assert response["id"] == result["job_id"]
        assert wait_job(engine, response["id"], "cancelled", deadline=2)["result"] is None


def test_editorial_browser_generation_and_history_after_restart(tmp_path, chromium_page):
    page, expect = chromium_page
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    project, clip = write_export(tmp_path)
    with mock_ollama() as (_, provider), running_server(tmp_path) as (httpd, base):
        httpd.app.editorial.provider = provider
        page.goto(base)
        page.get_by_role("button", name="Automation", exact=True).click()
        expect(page.locator("#editorial-ollama")).to_contain_text("Ollama disponible")
        page.locator("#editorial-model").select_option(MODEL)
        page.get_by_role("button", name="Guardar configuración de AI Engine").click()
        expect(page.locator("#notice")).to_contain_text("AI Engine guardados")
        page.locator("#editorial-clip").select_option(f"{project}/{clip.name}")
        page.get_by_role("button", name="Generar propuestas", exact=True).click()
        output = page.locator("#editorial-result")
        expect(output).to_contain_text("Editorial", timeout=10000)
        expect(output).to_contain_text("8.3")
        expect(output.locator(".editorial-variant")).to_have_count(3)
        first_variant = output.locator("[data-variant=curiosity]")
        if first_variant.get_attribute("open") is None:
            first_variant.locator("summary").click()
        first_variant.get_by_role("button", name="Elegir variante").click()
        expect(output.locator("[data-variant=curiosity] summary")).to_contain_text("Seleccionada")
        first_variant.get_by_label("Título").fill("Mi edición de tomates")
        first_variant.get_by_role("button", name="Guardar edición").click()
        expect(output.locator("h3")).to_have_text("Mi edición de tomates")
        page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base)
        first_variant.get_by_role("button", name="Copiar", exact=True).click()
        expect(page.locator("#notice")).to_contain_text("Texto editorial copiado.")
        assert "Mi edición de tomates" in page.evaluate("navigator.clipboard.readText()")
        page.locator("#automation-view").screenshot(path=str(tmp_path / "ai-engine.png"))
    with running_server(tmp_path) as (_, base):
        page.goto(base)
        page.get_by_role("button", name="Automation", exact=True).click()
        expect(page.locator("#editorial-result h3")).to_have_text("Mi edición de tomates", timeout=10000)
        expect(page.locator("#editorial-result")).to_contain_text("Ollama")
        expect(page.locator("#editorial-ollama")).to_contain_text("Respaldo local activo")
    assert not errors
