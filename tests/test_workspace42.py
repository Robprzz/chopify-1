"""Exercise the Clips workspace with a local HTTP server and real FFmpeg artifacts."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import shutil
import subprocess
import time
from urllib.error import HTTPError
from uuid import uuid4

import pytest

import asset_lifecycle
import dashboard
import projects
from test_dashboard import request, running_server, server  # noqa: F401
from test_studio_navigation import chromium_page  # noqa: F401
from test_watermark import extract_frame, gray_source, red


def fixture_media(root):
    source = gray_source(root)
    ident = projects.create_project(root, str(source), {"camera_mode": "stable"})["id"]
    folder = root / ident
    (folder / "clips").mkdir()
    path = folder / "clips" / "clip.mp4"
    shutil.copyfile(source, path)
    projects.atomic_json(path.with_suffix(".meta.json"), {"title": "Entrevista", "start": 0, "end": 3})
    projects.atomic_json(folder / "transcript.json", {"video": str(source), "words": []})
    return ident, path


def new_plan(project, file, **changes):
    return {"id": str(uuid4()), "project": project, "file": file, "platform": "youtube",
            "zone": "UTC", "local": (datetime.now(timezone.utc)+timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
            "title": "Entrevista", "caption": "¿Qué dijeron? 🎙️", **changes}


def test_verified_clip_can_schedule_without_editorial_and_rejects_duplicates_and_corruption(tmp_path):
    project, media = fixture_media(tmp_path)
    duplicate = media.with_name("duplicate.mp4")
    shutil.copyfile(media, duplicate)
    corrupt = media.with_name("corrupt.mp4")
    corrupt.write_bytes(b"not a video")
    with running_server(tmp_path) as (httpd, base):
        token = httpd.app.token
        item = request(base, "/api/calendar/save", new_plan(project, media.name), token)
        assert item["status"] == "scheduled" and item["job"] is None and item["consent"] is None
        assert item["caption"] == "¿Qué dijeron? 🎙️" and item["duration"] == pytest.approx(3, abs=.03)
        for path, code in ((duplicate, 409), (corrupt, 400)):
            with pytest.raises(HTTPError) as error:
                request(base, "/api/calendar/save", new_plan(project, path.name), token)
            assert error.value.code == code
    with running_server(tmp_path) as (_, base):
        assert request(base, "/api/calendar?id=" + item["id"])["status"] == "scheduled"
        media.write_bytes(b"replaced")
        assert request(base, "/api/calendar?id=" + item["id"])["stale"] is True


def test_replacement_artwork_and_version_do_not_modify_original(tmp_path):
    import cv2
    import numpy as np

    project, media = fixture_media(tmp_path)
    original = hashlib.sha256(media.read_bytes()).hexdigest()
    logo = np.zeros((64, 64, 4), dtype=np.uint8)
    logo[:] = (0, 0, 255, 255)
    png = base64.b64encode(cv2.imencode(".png", logo)[1]).decode()
    shared = {"project": project, "file": media.name, "platform": "tiktok"}
    with running_server(tmp_path) as (httpd, base):
        token = httpd.app.token
        art = request(base, "/api/clip/artwork", {**shared, "cover": png, "mark": "custom", "image": png}, token)
        assert art["cover"] and art["mark"]["url"]
        assert request(base, "/api/watermark")["enabled"] is False
        original_cover = art["cover"]
        with pytest.raises(HTTPError):
            request(base, "/api/clip/artwork", {**shared, "cover": "invalid"}, token)
        assert httpd.app.clips.read(project, media.name, "tiktok")["cover"] == original_cover
        job = request(base, "/api/clip/render", {**shared, "aspect": "9:16"}, token)["job"]
        result = httpd.app.clip_jobs[job].result(timeout=60)
        assert result["state"] == "ready", result
        final = media.with_name(result["file"])
        assert hashlib.sha256(media.read_bytes()).hexdigest() == original
        assert red(extract_frame(final)).sum() > 1000
        details = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(final)]))
        video, audio = details["streams"]
        assert (video["width"], video["height"], video["sample_aspect_ratio"]) == (1080, 1920, "1:1")
        assert abs(float(video["duration"])-float(audio["duration"])) < .06
        assert float(video["duration"]) == pytest.approx(3, abs=.06)


def test_recovery_and_permanent_delete_protect_shared_files(tmp_path, monkeypatch):
    send_json = dashboard.Handler.send_json

    def slow_cleanup(handler, payload, status=200):
        response = send_json(handler, payload, status)
        if status == 400 and handler.path == "/api/trash/purge":
            time.sleep(.08)
        return response

    monkeypatch.setattr(dashboard.Handler, "send_json", slow_cleanup)
    project, media = fixture_media(tmp_path)
    other = projects.create_project(tmp_path, str(media), {})["id"]
    with running_server(tmp_path) as (httpd, base):
        token = httpd.app.token
        row = request(base, "/api/trash/clip", {"project":project,"filename":media.name}, token)
        data = request(base, "/api/trash")["items"][0]
        assert "fuente" in data["blocked"] and media.is_file()
        with pytest.raises(HTTPError):
            request(base, "/api/trash/purge", {"ids":[row["id"]],"confirmation":"ELIMINAR"}, token)
    with running_server(tmp_path) as (httpd, base):
        token = httpd.app.token
        request(base, "/api/trash/restore", {"id":row["id"]}, token)
        assert media.is_file()
        # Remove the test dependency by purging the project that owns its reference.
        dependent = request(base, f"/api/projects/{other}/manage", {"action":"trash"}, token)
        with pytest.raises(HTTPError):
            request(base, "/api/trash/purge", {"ids":[dependent["id"]],"confirmation":"ELIMINAR"}, token)
        assert request(base, "/api/trash/purge", {"ids":[dependent["id"]],"confirmation":dependent["title"]}, token)["released"]
        assert not (tmp_path / other).exists()
        row = request(base, "/api/trash/clip", {"project":project,"filename":media.name}, token)
        assert request(base, "/api/trash/purge", {"ids":[row["id"]],"confirmation":"ELIMINAR"}, token)["released"]
        assert not media.exists() and not media.with_suffix(".meta.json").exists()
        assert request(base, f"/api/projects/{project}")["project"]["id"] == project


def test_hardlinks_and_symlinks_are_never_purged(tmp_path):
    project, media = fixture_media(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"preserve")
    sidecar = media.with_suffix(".png")
    os.link(outside, sidecar)
    row = asset_lifecycle.send_to_trash(tmp_path, project, media.name, "Clip")
    with pytest.raises(ValueError, match="compartido"):
        asset_lifecycle.purge(tmp_path, [row["id"]],"ELIMINAR")
    sidecar.unlink()
    try:
        sidecar.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink rights unavailable")
    with pytest.raises(ValueError, match="enlace"):
        asset_lifecycle.purge(tmp_path, [row["id"]],"ELIMINAR")
    assert media.exists() and outside.read_bytes() == b"preserve"


@pytest.mark.parametrize("width,height", [(1920,1080),(1366,768),(390,844)])
def test_clips_first_project_caption_persistence_and_calendar(tmp_path, chromium_page, width, height):
    project, media = fixture_media(tmp_path)
    page, expect = chromium_page
    page.set_viewport_size({"width": width, "height": height})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    with running_server(tmp_path) as (_, base):
        page.goto(base)
        expect(page.get_by_role("heading", name="Clips", exact=True)).to_be_visible()
        assert page.locator(".main-navigation button").all_text_contents() == [
            "Clips","Proyectos","Live Studio","Publicaciones","Centro de operaciones","Estadísticas","Configuración"]
        clip = page.locator("#clip-library .library-item")
        expect(clip).to_have_count(1)
        page.screenshot(path=str(tmp_path / f"clips-{width}.png"), full_page=True)
        page.locator("#show-projects").click()
        page.locator(f"#projects [data-id='{project}']").click()
        page.locator("#project-exported-clips").get_by_role("button",name="Editar clip").click()
        copy = page.locator("#project-clip-detail #clip-copy-form")
        expect(copy.locator("[name=title]")).to_have_value("Entrevista")
        copy.locator("[name=caption]").fill("Una conversación 🎙️ sobre edición.")
        copy.get_by_role("button",name="Guardar",exact=True).click()
        expect(copy.locator("[name=caption]")).to_have_value("Una conversación 🎙️ sobre edición.")
        page.screenshot(path=str(tmp_path / f"proyecto-{width}.png"), full_page=True)
        page.reload()
        expect(page.locator("#project-view")).to_be_visible()
        expect(copy.locator("[name=caption]")).to_have_value("Una conversación 🎙️ sobre edición.")
        copy.get_by_role("button",name="Programar",exact=True).click()
        expect(page.locator("#calendar-caption")).to_have_value("Una conversación 🎙️ sobre edición.")
        page.locator("#calendar-suggest").click()
        expect(page.locator("#calendar-feedback")).to_contain_text("Horario sugerido")
        page.locator("#calendar-edit-tools button[type=submit]").click()
        expect(page.locator("#calendar-detail-status")).to_contain_text("Programado")
        assert page.evaluate("document.documentElement.scrollWidth === innerWidth")
        page.screenshot(path=str(tmp_path / f"publicaciones-{width}.png"), full_page=True)
    assert not errors


def test_manual_package_without_editorial_or_account(tmp_path):
    project, media = fixture_media(tmp_path)
    with running_server(tmp_path) as (httpd, base):
        token = httpd.app.token
        draft = request(base, "/api/calendar/save", new_plan(project, media.name, local="", platform="instagram"), token)
        prepared = request(base, "/api/calendar/prepare", {"id":draft["id"],"revision":draft["revision"]}, token)
        assert prepared["status"] == "requires_action"
        assert prepared["confirmation"] is prepared["external_id"] is None
        published = request(base, "/api/calendar/confirm", {"id":draft["id"],"revision":prepared["revision"],"confirmed":True}, token)
        assert published["status"] == "published" and published["confirmation"] == "manual"


def test_scheduled_inbox_uses_explicit_account_and_never_repeats_ambiguous_attempt(tmp_path, monkeypatch):
    project, media = fixture_media(tmp_path)
    with running_server(tmp_path) as (httpd, base):
        service = httpd.app.tiktok
        agenda = httpd.app.calendar
        token = httpd.app.token
        service.account = {"key": "sandbox", "name": "Cuenta de prueba"}
        service.token = {"scopes": ["video.upload"]}
        monkeypatch.setattr(service, "_access", lambda: "fake")
        sends = []

        def mocked_transfer(path, review, record, access):
            sends.append(path)
            record.update(status="SEND_TO_USER_INBOX", publish_id="sandbox-inbox")
            service._save(record)

        monkeypatch.setattr(service, "_transfer", mocked_transfer)
        plan = request(base, "/api/calendar/save",
                       new_plan(project, media.name, platform="tiktok", inbox_consent=True), token)
        assert plan["consent"]
        agenda.deliver_due()
        assert sends == []
        with agenda.connect(write=True) as db:
            db.execute("UPDATE plans SET scheduled=? WHERE id=?", (time.time()-10, plan["id"]))
        agenda.deliver_due()
        assert sends == [media]
        state = agenda.snapshot(plan["id"])
        assert state["status"] == "requires_action" and state["confirmation"] is None
        assert state["publish_id"] == "sandbox-inbox"
        assert state["inbox_state"] == "done"
        agenda.recover_dispatches()
        agenda.deliver_due()
        assert sends == [media]


def test_interrupted_purge_restores_uncommitted_file(tmp_path):
    project, media = fixture_media(tmp_path)
    row = asset_lifecycle.send_to_trash(tmp_path, project, media.name, "Clip")
    stage = tmp_path / ".anqora-purge-test"
    stage.mkdir()
    projects.atomic_json(stage / "journal.json", {"rows":[row],"moves":[media.relative_to(tmp_path).as_posix()]})
    media.replace(stage / "0")
    asset_lifecycle.recover(tmp_path)
    assert media.is_file() and not stage.exists()
    assert asset_lifecycle.hidden(tmp_path, project, media.name)


def test_caption_generation_keeps_original_file_and_uses_selected_platform(tmp_path, chromium_page, monkeypatch):
    project, media = fixture_media(tmp_path)
    original = media.read_bytes()
    page, expect = chromium_page
    with running_server(tmp_path) as (httpd, base):
        engine = httpd.app.editorial
        engine.ensure()
        calls = []

        def submit(p, filename, platform, force, **options):
            calls.append((p, filename, platform, force, options["semantic"]))
            return {"job_id": 42}

        monkeypatch.setattr(engine, "submit", submit)
        monkeypatch.setattr(engine.store, "detail", lambda _: {"state":"done", "result":{
            "recommended_variant":"informative", "variants":[{
                "style":"informative", "title":"Entrevista original", "caption":"El texto de prueba",
                "description":"Grabación local", "hashtags":["#Entrevista"]}]}})
        page.goto(base)
        page.locator("#clip-library").get_by_role("button",name="Editar clip").click()
        page.locator("#clip-platform").select_option("youtube")
        page.get_by_role("button",name="Regenerar caption con IA").click()
        expect(page.locator("#clip-copy-form [name=caption]")).to_have_value("El texto de prueba")
        assert calls == [(project,media.name,"youtube",True,True)]
        assert list(media.parent.glob("*.mp4")) == [media] and media.read_bytes() == original


def test_purge_waits_for_related_plan(tmp_path):
    project, media = fixture_media(tmp_path)
    with running_server(tmp_path) as (httpd, base):
        token = httpd.app.token
        row = request(base, "/api/calendar/save", new_plan(project, media.name), token)
        trash = request(base, "/api/trash/clip", {"project":project,"filename":media.name}, token)
        payload = {"ids":[trash["id"]], "confirmation":"ELIMINAR"}
        with pytest.raises(HTTPError) as blocked:
            request(base, "/api/trash/purge", payload, token)
        assert "programaciones relacionadas" in blocked.value.read().decode() and media.exists()
        request(base, "/api/calendar/cancel", {"id":row["id"],"revision":row["revision"]}, token)
        assert request(base, "/api/trash/purge", payload, token)["released"]
        assert not media.exists()


def test_chromium_immediate_manual_package_and_confirmed_purge(tmp_path, chromium_page):
    project, media = fixture_media(tmp_path)
    page, expect = chromium_page
    with running_server(tmp_path) as (_, base):
        page.goto(base)
        page.locator("#clip-library").get_by_role("button", name="Editar clip").click()
        page.locator("#clip-platform").select_option("instagram")
        page.locator("#clip-publish").click()
        expect(page.locator("#calendar-platform-status")).to_contain_text("Flujo manual")
        with page.expect_download() as package:
            page.get_by_role("button", name="Preparar paquete manual").click()
        assert package.value.suggested_filename.endswith(".json")
        expect(page.locator("#calendar-detail-status")).to_contain_text("Requiere acción")
        expect(page.get_by_role("link",name="Descargar MP4 original")).to_be_visible()
        page.once("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button",name="Confirmar publicación manual").click()
        expect(page.locator("#calendar-detail-status")).to_contain_text("Publicado · confirmación manual")
        page.locator("#brand-home").click()
        page.locator("#clip-library").get_by_role("button",name="Enviar a la papelera").click()
        page.get_by_role("button", name="Sí, eliminar").click()
        expect(page.locator("#clip-library")).to_contain_text("No hay exportaciones")
        page.locator("#show-settings").click()
        page.locator("#show-trash").click()
        expect(page.locator("#asset-trash-list")).to_contain_text(media.name)
        page.get_by_role("button",name="Eliminar definitivamente").click()
        page.get_by_role("button", name="Sí, eliminar").click()
        expect(page.locator("#asset-trash-list")).to_contain_text("La papelera está vacía")
        assert not media.exists()


def test_bulk_clip_trash_atomic_and_confirmed_purge_preserves_source(tmp_path):
    project, media = fixture_media(tmp_path)
    sibling = media.with_name("second.mp4")
    shutil.copyfile(media, sibling)
    with running_server(tmp_path) as (server, base):
        token = server.app.token

        def trash(*files):
            return request(base, "/api/trash/clips", {
                "clips": [{"project": project, "filename": name} for name in files]}, token)

        with pytest.raises(HTTPError):
            trash(media.name, "missing.mp4")
        assert request(base, "/api/trash")["items"] == []
        rows = trash(media.name, sibling.name)["items"]
        assert len(rows) == 2 and media.exists() and sibling.exists()
        restored = request(base, "/api/trash/restore", {"id": rows[0]["id"]}, token)
        assert restored["file"] == media.name
        assert request(base, "/api/trash/purge", {"ids": [rows[1]["id"]], "confirmation": True}, token)["released"]
        assert media.exists() and not sibling.exists() and (tmp_path / "gray source.mp4").exists()


def test_live_trashed_session_disappears_and_restores_before_purge(tmp_path):
    ident = "abcdef123456"
    session = tmp_path / "live" / ident
    session.mkdir(parents=True)
    projects.atomic_json(session / "live_session.json", {"id": ident, "status": "stopped"})
    with running_server(tmp_path) as (server, base):
        from types import SimpleNamespace
        token = server.app.token
        endpoint = f"/api/live/{ident}/trash"
        server.app.live_sessions[ident] = SimpleNamespace(status="running")
        try:
            with pytest.raises(HTTPError):
                request(base, endpoint, {"confirmed": True}, token)
        finally:
            del server.app.live_sessions[ident]
        row = request(base, endpoint, {"confirmed": True}, token)
        assert request(base, "/api/live")["sessions"] == []
        assert session.exists()
        request(base, "/api/trash/restore", {"id": row["id"]}, token)
        assert request(base, "/api/live")["sessions"][0]["id"] == ident
        row = request(base, endpoint, {"confirmed": True}, token)
        request(base, "/api/trash/purge", {"ids": [row["id"]], "confirmation": True}, token)
        assert not session.exists()


def test_calendar_purge_removes_pending_queue_and_rolls_back_batch(tmp_path):
    project, media = fixture_media(tmp_path)
    with running_server(tmp_path) as (server, base):
        token = server.app.token
        a = request(base, "/api/calendar/save", new_plan(project, media.name), token)
        b = request(base, "/api/calendar/save", new_plan(project, media.name, platform="facebook", local=""), token)
        with server.app.calendar.connect(write=True) as db:
            db.execute("INSERT INTO inbox_schedule VALUES(?,?,?,?)", (a["id"], "test", a["revision"], "pending"))

        def remove(revision):
            return request(base, "/api/calendar/delete", {"confirmed": True,
                "items": [{"id": a["id"], "revision": a["revision"]},
                          {"id": b["id"], "revision": revision}]}, token)

        with pytest.raises(HTTPError) as stale:
            remove(b["revision"] + 1)
        assert stale.value.code == 409
        with server.app.calendar.connect(write=True) as db:
            assert db.execute("SELECT count(*) FROM plans").fetchone()[0] == 2
            db.execute("UPDATE inbox_schedule SET state='claimed' WHERE plan=?", (a["id"],))
        with pytest.raises(HTTPError) as claimed:
            remove(b["revision"])
        assert claimed.value.code == 409
        with server.app.calendar.connect(write=True) as db:
            db.execute("UPDATE inbox_schedule SET state='pending' WHERE plan=?", (a["id"],))
            db.execute("UPDATE plans SET status='published' WHERE id=?", (b["id"],))
        with pytest.raises(HTTPError) as published:
            remove(b["revision"])
        assert published.value.code == 409
        with server.app.calendar.connect(write=True) as db:
            db.execute("UPDATE plans SET status='draft' WHERE id=?", (b["id"],))
        assert remove(b["revision"]) == {"deleted": 2}
        with server.app.calendar.connect() as db:
            for name in ("plans", "inbox_schedule", "calendar_events"):
                assert not db.execute(f"SELECT 1 FROM {name}").fetchone()
        assert media.exists()


def test_library_filter_and_trash_blocked_entry_in_chromium(tmp_path, chromium_page):
    project, media = fixture_media(tmp_path)
    second = media.with_name("distinct.mp4")
    shutil.copyfile(media, second)
    projects.atomic_json(second.with_suffix(".meta.json"), {"title": "Distinto", "start": 0, "end": 3})
    page, expect = chromium_page
    with running_server(tmp_path) as (server, base):
        missing = asset_lifecycle.send_to_trash(tmp_path, "123456789abc", None, "Proyecto ausente")
        page.goto(base)
        page.locator("#show-clips-library").click()
        page.locator("#clip-library-query").fill("Distinto")
        page.get_by_role("button", name="Seleccionar todos", exact=True).click()
        expect(page.locator("#clip-selected-count")).to_have_text("1 seleccionados")
        page.get_by_role("button", name="Deseleccionar todos", exact=True).click()
        expect(page.locator("#clip-selected-count")).to_have_text("0 seleccionados")
        page.locator("#clip-library [data-clip-choice]").check()
        page.locator("#clip-trash-selected").click()
        page.locator("#delete-confirmation").get_by_role("button", name="Cancelar").click()
        assert all(item["file"] is None for item in asset_lifecycle.entries(tmp_path))
        page.locator("#clip-trash-selected").click()
        page.get_by_role("button", name="Sí, eliminar").click()
        expect(page.locator("#clip-library")).to_contain_text("No hay exportaciones")
        page.locator("#show-settings").click()
        page.locator("#show-trash").click()
        page.get_by_role("button", name="Vaciar papelera").click()
        expect(page.locator("#delete-confirmation-detail")).to_contain_text("1 elementos bloqueados")
        page.get_by_role("button", name="Sí, eliminar").click()
        expect(page.locator("#trash-summary")).to_contain_text("1 elementos")
        assert asset_lifecycle.entries(tmp_path) == [missing] and not second.exists() and media.exists()


def test_calendar_batch_and_live_trash_controls_in_chromium(tmp_path, chromium_page):
    project, media = fixture_media(tmp_path)
    ident = "abcdeffedcba"
    session = tmp_path / "live" / ident
    session.mkdir(parents=True)
    projects.atomic_json(session / "live_session.json", {"id": ident, "status": "stopped"})
    page, expect = chromium_page
    with running_server(tmp_path) as (server, base):
        token = server.app.token
        rows = [request(base, "/api/calendar/save", new_plan(project, media.name, platform=platform), token)
                for platform in ("youtube", "facebook")]
        page.goto(base)
        page.locator("#show-publications").click()
        page.get_by_text("Eliminar planes en grupo", exact=True).click()
        page.locator("#calendar-delete-selection").select_option([row["id"] for row in rows])
        page.locator("#calendar-delete-batch").click()
        page.get_by_role("button", name="Sí, eliminar").click()
        expect(page.locator("#calendar-delete-selection option")).to_have_count(0)
        assert request(base, "/api/calendar")["plans"] == []
        page.locator("#show-live").click()
        page.locator("#live-sessions").get_by_role("button", name="Eliminar sesión").click()
        page.get_by_role("button", name="Sí, eliminar").click()
        expect(page.locator("#live-sessions-empty")).to_be_visible()
        assert request(base, "/api/live")["sessions"] == [] and session.exists()
