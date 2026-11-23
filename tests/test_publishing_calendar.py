"""Exercise the persistent scheduler and Anqora Studio over the real HTTP server."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import threading
import urllib.error
from uuid import uuid4

import pytest

import projects
import publishing_calendar as calendar
from test_dashboard import request, running_server, server  # noqa: F401
from test_studio_navigation import chromium_page  # noqa: F401


@pytest.fixture
def agenda(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 11, 23, 12, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr(calendar, "time", SimpleNamespace(time=lambda: clock.now))
    settings = SimpleNamespace(read=lambda: {"settings": {
        "timezone": "America/Santo_Domingo", "minimum_clip_score": 6}})
    receipts = []
    tiktok = SimpleNamespace(lock=threading.RLock(), sending=False, _account_history=lambda: receipts)
    state = SimpleNamespace(run=1, editorial_revision=1, approved=True, identical=False)

    def clip(self, job, project=None, filename=None):
        return {"project": "c" * 12, "file": f"clip_{job}.mp4", "run": state.run,
                "editorial_revision": state.editorial_revision, "integrity": json.dumps({"size": 20}),
                "hashes": ["a" * 64 if state.identical else f"{job:064x}"],
                "approval": clock.now - 200 if state.approved else None,
                "duration": 60, "status": "editorial_approved" if state.approved else "pending_review",
                "title": "Entrevista 📹", "description": "Conversación sobre música", "hashtags": ["#música"]}

    monkeypatch.setattr(calendar.Calendar, "_clip", clip)

    def open_store():
        return calendar.Calendar(tmp_path, None, tiktok, settings)

    return SimpleNamespace(clock=clock, open=open_store, store=open_store(), source=state, receipts=receipts)


def plan(job=1, **fields):
    return {"id": str(uuid4()), "job": job, "platform": "tiktok", **fields}


def change(store, item, **fields):
    return store.save({"id": item["id"], "revision": item["revision"],
                       "platform": item["platform"], **fields})


def action(store, verb, item, **fields):
    return store.action(verb, {"id": item["id"], "revision": item["revision"], **fields})


def test_plan_lifecycle_transaction_and_restart(agenda):
    store = agenda.store
    agenda.source.approved = False
    payload = plan(local="2026-11-23T09:00")
    with pytest.raises(calendar.Conflict, match="Aprueba editorialmente"):
        store.save(payload)
    original = plan()
    row = store.save(original)
    assert row["status"] == "pending_review"
    assert store.save({**original, "title": "Retried text"}) == row
    assert len(store.snapshot(row["id"])["history"]) == 1
    agenda.source.approved = True
    row = change(store, row, ready=True)
    assert row["status"] == "ready"
    scheduled = change(store, row, title="La música de hoy", local="2026-11-23T09:00")
    assert scheduled["status"] == "scheduled"
    assert datetime.fromtimestamp(scheduled["scheduled"], timezone.utc).hour == 13
    with pytest.raises(calendar.Conflict, match="cambió"):
        change(store, row, title="Edición atrasada")
    shifted = change(store, scheduled, local="2026-11-23T15:00", zone="UTC")
    restarted = agenda.open()
    assert restarted.snapshot(shifted["id"])["scheduled"] == shifted["scheduled"]
    agenda.clock.now = shifted["scheduled"] + 5
    expired = restarted.snapshot(shifted["id"])
    assert expired["status"] == "requires_action"
    assert expired["external_id"] is expired["consent"] is None
    assert expired["attempts"] == 0
    assert "vencido" in expired["error"]
    canceled = action(restarted, "cancel", expired)
    assert canceled["status"] == agenda.open().snapshot(expired["id"])["status"] == "cancelled"


def test_timezones_dst_conflicts_and_exact_duplicates(agenda):
    store = agenda.store
    zone = calendar.zone_for("America/New_York")
    for local in ("2026-03-08T02:30", "2026-11-01T01:30"):
        with pytest.raises(ValueError, match="ambigua o no existe"):
            calendar.instant(local, zone)
    normalized = store.action("preferences", {**calendar.DEFAULTS, "timezone": "UTC", "daily": 2})
    assert agenda.open().snapshot()["preferences"] == normalized
    first = store.save(plan(local="2026-11-23T15:00"))
    with pytest.raises(calendar.Conflict, match="intervalo"):
        store.save(plan(2, local="2026-11-23T15:30"))
    second = store.save(plan(2, local="2026-11-23T16:00"))
    with pytest.raises(calendar.Conflict, match="cantidad diaria"):
        store.save(plan(3, local="2026-11-23T19:00"))
    assert store.save(plan(3, platform="youtube", local="2026-11-23T19:00"))["status"] == "scheduled"
    with pytest.raises(calendar.Conflict, match="ya tiene"):
        store.save(plan())
    agenda.source.identical = True
    store.save(plan(4))
    with pytest.raises(calendar.Conflict, match="ya tiene"):
        store.save(plan(5))
    assert action(store, "cancel", first)["status"] == "cancelled"
    assert action(store, "cancel", second)["status"] == "cancelled"
    assert store.action("suggest", {"platform": "tiktok", "priority": "urgent"})["basis"] == "heuristic"


def test_obsolete_approval_and_manual_confirmation(agenda):
    store = agenda.store
    row = store.save(plan(platform="youtube", local="2026-11-23T09:30"))
    agenda.source.editorial_revision += 1
    assert store.snapshot(row["id"])["stale"]
    with pytest.raises(calendar.Conflict, match="versión"):
        action(store, "prepare", row)
    with pytest.raises(calendar.Conflict, match="Actualiza"):
        change(store, row, title="Texto editado")
    agenda.source.approved = False
    row = change(store, row, refresh_version=True)
    assert row["status"] == "pending_review"
    with pytest.raises(calendar.Conflict, match="aprobación"):
        action(store, "confirm", row, confirmed=True)
    agenda.source.approved = True
    row = action(store, "prepare", row)
    assert row["status"] == "requires_action"
    with pytest.raises(ValueError, match="confirmación manual"):
        action(store, "confirm", row)
    published = action(store, "confirm", row, confirmed=True)
    assert published["status"] == "published" and published["confirmation"] == "manual"
    assert published["publish_id"] is None


def test_tiktok_recovers_ambiguity_inbox_and_verified_confirmation(agenda):
    store = agenda.store
    row = store.save(plan())
    assert action(store, "prepare", row)["consent"] is None
    with pytest.raises(ValueError, match="confirmación manual"):
        action(store, "confirm", row, confirmed=True)
    record = {"id": "a" * 32, "project": row["project"], "filename": row["file"], "digest": "wrong"}
    with pytest.raises(calendar.Conflict, match="archivo exacto"):
        store.reserve_tiktok(row["id"], row["revision"], record)
    store.reserve_tiktok(row["id"], row["revision"], {**record, "digest": f"{1:064x}"})
    uncertain = agenda.open().snapshot(row["id"])
    assert uncertain["attempts"] == 1 and uncertain["consent"]
    assert uncertain["status"] == "requires_action" and "sin confirmar" in uncertain["error"]
    with pytest.raises(calendar.Conflict, match="comprobación"):
        action(store, "retry", uncertain)
    agenda.receipts.append({"id": record["id"], "status": "PROCESSING_UPLOAD", "publish_id": "upload-id"})
    assert store.snapshot(row["id"])["status"] == "processing"
    agenda.receipts[0]["status"] = "SEND_TO_USER_INBOX"
    inbox = store.snapshot(row["id"])
    assert inbox["status"] == "requires_action" and "buzón" in inbox["error"]
    assert inbox["confirmation"] is None
    agenda.receipts[0].update(status="PUBLISH_COMPLETE", checked_at=agenda.clock.now + 100)
    published = agenda.open().snapshot(row["id"])
    assert published["status"] == "published" and published["confirmation"] == "api"
    assert published["publish_id"] == "upload-id"


def test_calendar_http_token_required(server):
    httpd, base = server
    assert not request(base, "/api/calendar")["plans"]
    with pytest.raises(urllib.error.HTTPError) as unauthorized:
        request(base, "/api/calendar/preferences", calendar.DEFAULTS)
    assert unauthorized.value.code == 403
    assert request(base, "/api/calendar/preferences", calendar.DEFAULTS, httpd.app.token) == calendar.DEFAULTS


def test_calendar_ui_and_navigation_over_http(tmp_path, chromium_page):
    page, expect = chromium_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    project = projects.create_project(tmp_path, "Multilingüe.mp4", {})
    output = tmp_path / project["id"] / "clips"
    output.mkdir(exist_ok=True)
    (output / "Entrevista.mp4").write_bytes(b"local export fixture")
    with running_server(tmp_path) as (_, base):
        page.goto(base)
        expect(page.locator("#home-greeting")).to_have_text("Inicio")
        expect(page.locator(".main-navigation button")).to_have_count(7)
        expect(page.locator("#home-kpis")).to_contain_text("0/5")
        assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(247, 249, 253)"
        assert "rgb(16, 27, 53)" in page.locator(".home-intro .primary").evaluate("e => getComputedStyle(e).backgroundImage")
        page.keyboard.press("Control+k")
        expect(page.locator("#workspace-query")).to_be_focused()
        page.locator("#workspace-query").fill("Entrevista")
        expect(page.locator("#workspace-results")).to_contain_text("Entrevista")
        page.keyboard.press("Escape")
        page.locator("#show-live").click()
        expect(page.locator("#live-aspect")).to_have_value("9:16")
        expect(page.locator("#live-max-height")).to_be_hidden()
        page.locator("#show-publications").click()
        expect(page.locator("#calendar-grid .calendar-cell")).to_have_count(7)
        page.locator("#calendar-view-mode").select_option("month")
        expect(page.locator("#calendar-grid .calendar-cell")).to_have_count(42)
        page.locator("#calendar-view-mode").select_option("day")
        expect(page.locator("#calendar-grid .calendar-cell")).to_have_count(1)
        page.locator("#calendar-create").click()
        page.locator("#calendar-clip").select_option(project["id"] + "/Entrevista.mp4")
        page.locator("#calendar-title").fill("Entrevista en español")
        page.locator("#show-clips-library").click()
        page.locator("#show-publications").click()
        expect(page.locator("#calendar-title")).to_have_value("Entrevista en español")
        page.locator("#calendar-form button[type=submit]").click()
        expect(page.locator("#calendar-detail-status")).to_have_text("Borrador")
        page.reload()
        expect(page.get_by_role("heading", name="Calendario editorial")).to_be_visible()
        expect(page.locator("#calendar-drafts")).to_contain_text("Entrevista en español")
    with running_server(tmp_path) as (_, base):
        page.goto(base)
        page.locator("#show-publications").click()
        page.locator("#calendar-drafts button").click()
        expect(page.locator("#calendar-title")).to_have_value("Entrevista en español")
    assert not errors, errors


@pytest.mark.parametrize("width,height", [(1920, 1080), (1366, 768), (390, 844)])
def test_anqora_dashboard_responsive(server, chromium_page, width, height):
    _, base = server
    page, expect = chromium_page
    page.set_viewport_size({"width": width, "height": height})
    page.goto(base)
    expect(page.locator("#home-kpis")).to_contain_text("Capturas activas")
    expect(page.locator("#home-live")).to_contain_text("Sin captura activa")
    assert page.evaluate("document.documentElement.scrollWidth === innerWidth")
    assert page.title() == "Anqora Media Studio"
    assert page.locator('link[rel="icon"]').get_attribute("href") == "/anqora-symbol"
    for route in ("/anqora-logo", "/anqora-symbol"):
        response = page.request.get(base + route)
        assert response.ok and response.headers["content-type"] == "image/png"
    logo, mark = page.locator(".brand-full"), page.locator(".brand-mark")
    if width > 640:
        expect(logo).to_be_visible()
        assert logo.evaluate("image => image.naturalWidth") == 2172
        page.locator("#rail-toggle").click()
        expect(logo).to_be_hidden()
    expect(mark).to_be_visible()
    assert mark.evaluate("image => image.naturalWidth") == 512


def test_current_automation_evidence_and_explicit_tiktok_consent(server, monkeypatch):
    """Use an actual H.264/AAC fixture with a simulated provider, never TikTok."""
    import hashlib
    import shutil
    import time

    from production_selection import stamp
    from test_realtime_operations import seed
    from test_subtitle_quality import media_file

    if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg and FFprobe are required for the local MP4 fixture")
    httpd, base = server
    app = httpd.app
    app.editorial._migrate()
    project = projects.create_project(app.root, "Volumen.mp4", {})["id"]
    output = app.root / project / "clips"
    output.mkdir()
    media = media_file(output / "avance.mp4", size="360x640")
    digest = hashlib.sha256(media.read_bytes()).hexdigest()
    evidence = {"sha": [digest], "file_stamp": stamp(media), "original_stamp": stamp(media),
                "duration": 3, "tokens": [], "source": project, "interval": {"start": 0, "end": 3}}
    now = time.time()
    app.production.store.config(paused=True)
    job = seed(app.production.store, now, "avance", project=project, evidence=evidence, with_editor=True)
    app.production.store.set_event(job, 1, "conventional", now - 60)
    token = app.token
    assert request(base, "/api/calendar-clip?job=" + str(job))["status"] == "pending_review"
    assert app.production.store.remote_review(job, 1, 5, "approved", "studio")
    assert request(base, "/api/workspace")["priority"][0]["job"] == job
    row = request(base, "/api/calendar/save", plan(job), token)
    assert row["status"] == "editorial_approved"

    monkeypatch.setattr(app.tiktok, "account", {"key": "simulation"})
    monkeypatch.setattr(app.tiktok, "_access", lambda: "simulated-access")
    transfers = []
    def transfer(path, review, record, access):
        assert path == media
        assert review["digest"] == digest
        transfers.append(record["id"])
        record.update(status="SEND_TO_USER_INBOX", publish_id="simulated-inbox")
        app.tiktok._save(record)
    monkeypatch.setattr(app.tiktok, "_transfer", transfer)
    review = request(base, "/api/tiktok/inspect", {"project": project, "filename": media.name}, token)
    payload = {"review_id": review["review_id"], "description": "Un clip de prueba", "calendar_id": row["id"],
               "calendar_revision": row["revision"]}
    with pytest.raises(urllib.error.HTTPError) as rejected:
        request(base, "/api/tiktok/upload", payload, token)
    assert rejected.value.code == 400 and not transfers
    assert request(base, "/api/calendar?id=" + row["id"])["consent"] is None
    uploaded = request(base, "/api/tiktok/upload", {**payload, "approved": True}, token)
    assert uploaded["status"] == "SEND_TO_USER_INBOX"
    assert len(transfers) == 1
    entry = request(base, "/api/calendar?id=" + row["id"])
    assert entry["status"] == "requires_action" and entry["confirmation"] is None
    assert entry["consent"] and entry["external_id"] == uploaded["id"]
    request(base, "/api/tiktok/upload", {**payload, "approved": True}, token)
    assert len(transfers) == 1


def test_archive_date_can_reopen_old_plans(agenda):
    old = agenda.store.save(plan(local="2026-11-23T09:00"))
    agenda.clock.now = datetime(2027, 7, 14, 16, tzinfo=timezone.utc).timestamp()
    pending = agenda.store.snapshot()["plans"][0]
    assert pending["id"] == old["id"] and pending["status"] == "requires_action"
    action(agenda.store, "cancel", pending)
    assert not agenda.store.snapshot()["plans"]
    archived = agenda.store.snapshot(around="2026-11-23")["plans"][0]
    assert archived["id"] == old["id"] and archived["status"] == "cancelled"
