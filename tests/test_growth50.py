from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError

import pytest

from test_dashboard import request, running_server
from test_workspace42 import fixture_media
from test_studio_navigation import chromium_page  # noqa: F401

HEADER = "post_id,published_at,observed_at,views,likes,comments,shares\n"


def test_csv_preview_idempotence_and_historical_slots(tmp_path, monkeypatch):
    import publishing_calendar

    class Morning(datetime):
        @classmethod
        def now(cls, zone=None):
            return datetime(2026, 11, 24, 12, tzinfo=timezone.utc).astimezone(zone)

    monkeypatch.setattr(publishing_calendar, "datetime", Morning)
    content = HEADER
    for i in range(10):
        start = datetime(2025, 4, i+1, 13 if i < 5 else 17, tzinfo=timezone.utc)
        content += f"p{i},{start.isoformat()},{(start+timedelta(days=1)).isoformat()},100,{i*10},0,0\n"
    payload = {"platform":"youtube", "account":"canal", "csv":content}
    with running_server(tmp_path) as (server, base):
        token = server.app.token
        endpoint = "/api/growth/import"
        assert request(base, endpoint, payload, token)["rows"] == 10
        assert request(base, "/api/growth")["groups"] == []
        assert request(base, endpoint, {**payload,"confirm":True}, token)["imported"] == 10
        assert request(base, endpoint, {**payload,"confirm":True}, token)["imported"] == 0
        with pytest.raises(HTTPError):
            request(base, endpoint, {**payload,"csv":HEADER+"x,2023-01-01,2023-01-02,-1,0,0,0"}, token)
        slot = request(base, "/api/calendar/suggest", {"platform":"youtube","account":"canal"}, token)
        assert slot["basis"] == "imported_csv" and slot["history_samples"] == 5
    with running_server(tmp_path) as (_, base):
        result = request(base, "/api/growth")
        assert result["groups"][0]["views"] == 1000 and result["source"] == "user_csv"


def test_drafts_atomic_and_platform_specific(tmp_path):
    project, media = fixture_media(tmp_path)
    with running_server(tmp_path) as (server, base):
        token = server.app.token
        for platform in ("tiktok", "youtube"):
            request(base, "/api/clip/copy", {"project":project,"file":media.name,"platform":platform,
                "title":platform,"caption":"Texto para "+platform}, token)
        endpoint = "/api/calendar/drafts"
        item = {"clips":[f"{project}/{media.name}"],"platforms":["tiktok","youtube"]}
        drafts = request(base, endpoint, item, token)["plans"]
        assert [row["caption"] for row in drafts] == ["Texto para tiktok", "Texto para youtube"]
        assert all(row["status"] == "draft" and row["consent"] is None for row in drafts)
        with pytest.raises(HTTPError):
            request(base, endpoint, {**item,"platforms":["facebook","youtube"]}, token)
        assert len(request(base, "/api/calendar")["plans"]) == 2
        assert not request(base, "/api/publishing-capabilities")["tiktok"]["direct_post"]


def test_browser_csv_and_drafts(tmp_path, chromium_page):
    fixture_media(tmp_path)
    page, expect = chromium_page
    with running_server(tmp_path) as (_, base):
        page.goto(base)
        page.locator("#show-publications").click()
        page.get_by_text("Preparar varios clips", exact=True).click()
        options = page.locator("#batch-clips option")
        expect(options).to_have_count(1)
        page.locator("#batch-clips").select_option(index=0)
        page.locator("#batch-platforms").select_option(["youtube","tiktok"])
        page.get_by_role("button",name="Crear borradores").click()
        expect(page.locator("#calendar-feedback")).to_contain_text("2 borradores guardados")
        page.locator("#publishing-view-mode").select_option("draft")
        page.locator("#publishing-list button").first.click()
        page.get_by_role("button", name="Asignar fecha").click()
        page.locator("#calendar-suggest").click()
        expect(page.locator("#calendar-feedback")).to_contain_text("Horario sugerido")
        page.locator("#calendar-edit-tools button[type=submit]").click()
        expect(page.locator("#calendar-detail-status")).to_contain_text("Programado")
        page.locator("#show-statistics").click()
        expect(page.locator("#growth-results")).to_contain_text("Todavía no hay métricas")
        page.get_by_text("Importar métricas CSV",exact=True).click()
        form = page.locator("#growth-import")
        form.locator("[name=account]").fill("canal")
        form.locator("[name=file]").set_input_files({"name":"resultados.csv","mimeType":"text/csv",
            "buffer":(HEADER+"p1,2025-01-01T12:00:00Z,2025-01-02T12:00:00Z,12,2,0,0\n").encode()})
        form.get_by_role("button",name="Ver vista previa").click()
        expect(page.locator("#growth-preview")).to_contain_text("12 vistas")
        form.locator("#growth-confirm").click()
        expect(page.locator("#growth-results")).to_contain_text("12 vistas")


def test_assisted_preparation_is_persistent_and_never_dispatches(tmp_path):
    project, media = fixture_media(tmp_path)
    with running_server(tmp_path) as (server, base):
        app = server.app
        request(base,"/api/publishing-assist",{"mode":"assisted","platforms":["youtube"]},app.token)
        app.preparation.prepare_one(app.tiktok.exports,app.clips)
        plan = request(base,"/api/calendar")["plans"][0]
        assert plan["file"] == media.name and plan["status"] == "draft" and plan["consent"] is None
    with running_server(tmp_path) as (server, base):
        app = server.app
        app.preparation.prepare_one(app.tiktok.exports,app.clips)
        assert len(request(base,"/api/calendar")["plans"]) == 1
        assert request(base,"/api/publishing-assist")["history"][0]["result"] == "prepared"
