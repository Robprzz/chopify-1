"""Content Discovery uses the existing store and provider boundary."""
import os
from pathlib import Path
import sqlite3
import time
from urllib.error import HTTPError
from uuid import uuid4

import pytest

import content_discovery as cd
import projects
import youtube_monitor as yt
from test_dashboard import request, running_server
from test_studio_navigation import chromium_page  # noqa: F401

CHANNEL = "UC" + "a" * 22
SECOND = "UC" + "b" * 22
VIDEO = "abcdefghijk"
NOW = 1_795_608_000


def video(number):
    return f"video{number:06}"


class Feed:
    resolve = staticmethod(yt.locator)

    def __init__(self):
        self.data = {}
        self.error = None
        self.calls = []

    def latest(self, ident):
        self.calls.append(ident)
        if self.error:
            raise self.error
        return self.data[ident]


@pytest.fixture
def services(tmp_path):
    feed = Feed()
    calls = []
    now = [NOW]
    monitor = yt.Monitor(tmp_path, lambda *args: calls.append(args) or ("ready", None),
                         lambda: True, feed, lambda: now[0], start=False)
    return monitor, cd.Discovery(monitor, tmp_path), feed, calls, now


def test_additive_v2_migration_preserves_legacy_links_and_production(tmp_path):
    legacy = tmp_path / ".youtube-monitor.sqlite"
    with sqlite3.connect(legacy) as db:
        db.executescript("""
          PRAGMA user_version=2;
          CREATE TABLE channels (id TEXT PRIMARY KEY,name TEXT NOT NULL,active INTEGER NOT NULL,
            authorized INTEGER NOT NULL,reason TEXT NOT NULL,interval INTEGER NOT NULL,maximum INTEGER NOT NULL,
            preferred INTEGER NOT NULL,template TEXT NOT NULL,category TEXT NOT NULL,render INTEGER NOT NULL,
            baseline REAL,checked REAL,next_check REAL NOT NULL DEFAULT 0,errors INTEGER NOT NULL DEFAULT 0,error TEXT);
          CREATE TABLE videos (channel TEXT NOT NULL,id TEXT NOT NULL,title TEXT NOT NULL,published REAL NOT NULL,
            state TEXT NOT NULL,attempts INTEGER NOT NULL,project TEXT,next_try REAL NOT NULL,error TEXT,
            lease_owner TEXT,lease_until REAL NOT NULL DEFAULT 0,PRIMARY KEY(channel,id));
        """)
        db.execute("INSERT INTO channels VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (CHANNEL, "Canal anterior", 1, 1, "Titular", 15, 3, 60, "inherit", "gaming", 1, 0, 30, 60, 0, None))
        db.execute("INSERT INTO videos VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (CHANNEL, VIDEO, "Proyecto anterior", 50, "ready", 2, "abcdabcdabcd", 0, None, None, 0))
    project = tmp_path / "abcdabcdabcd"
    projects.init_project(project, "https://www.youtube.com/watch?v=" + VIDEO, {}, project.name)
    original = (project / "project.json").read_bytes()
    for _ in range(2):
        monitor = yt.Monitor(tmp_path, lambda *_: None, lambda: False, start=False)
        discovery = cd.Discovery(monitor, tmp_path)
        assert discovery.sources()[0]["mode"] == "produce"
        assert discovery.metrics()["produced"] == 1
        item = discovery.browse({})["items"][0]
        assert item["project"] == project.name
        assert item["detected_at"] is None
        assert (project / "project.json").read_bytes() == original
        with monitor.connect() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] >= 3
            assert db.execute("SELECT attempts FROM videos").fetchone()[0] == 2
            assert db.execute("SELECT count(*) FROM discovery_evaluations").fetchone()[0] == 1


def test_discover_mode_never_dispatches_even_if_authorized_or_previously_queued(services, tmp_path):
    monitor, discovery, feed, calls, now = services
    source = discovery.source({"source": CHANNEL, "category": "gaming"})[0]
    assert (source["mode"], source["interval"]) == ("discover", 180)
    monitor.action("settings", {"id": CHANNEL, "authorized": True, "reason": "Titular", "render": True})
    feed.data[CHANNEL] = ("Canal", [{"id": VIDEO, "title": "Título", "published": NOW - 1}])
    monitor.poll()
    monitor.work()
    assert discovery.browse({})["total"] == 1 and calls == []
    with pytest.raises(ValueError, match="Solo descubrir"):
        monitor.action("import", {"id": CHANNEL})
    with monitor.connect(write=True) as db:
        db.execute("UPDATE videos SET state='queued' WHERE id=?", (VIDEO,))
    monitor.work()
    assert not calls
    now[0] += 60
    monitor.refresh_active()
    monitor.poll()
    assert discovery.browse({})["total"] == 1
    assert not list(tmp_path.glob("*/project.json"))
    # Production mode remains available, with the prior resolved link unchanged until dispatch.
    discovery.source({"id": CHANNEL, "mode": "produce"})
    monitor.work()
    assert len(calls) == 1
    assert calls[0][1] == yt.project_id(CHANNEL, VIDEO)


def test_offline_and_youtube_backoff_preserve_results_and_respect_refresh(services):
    monitor, discovery, feed, calls, now = services
    discovery.source({"source": CHANNEL})
    feed.data[CHANNEL] = ("Canal", [{"id": VIDEO, "title": "Historial disponible", "published": NOW - 1}])
    monitor.poll()
    now[0] += 30
    assert monitor.refresh_active() == {"scheduled": 1}
    feed.error = yt.FeedError("YouTube limitó las consultas (429).", 3600)
    monitor.poll()
    source = discovery.sources()[0]
    assert source["next_check"] >= now[0] + 3600 and "429" in source["error"]
    assert monitor.refresh_active() == {"scheduled": 0}
    assert len(feed.calls) == 2
    now[0] = int(source["next_check"])
    feed.error = OSError("Sin Internet")
    monitor.poll()
    assert "Sin Internet" in discovery.sources()[0]["error"]
    assert discovery.browse({})["items"][0]["title"] == "Historial disponible"
    assert discovery.metrics()["detected"] == 1 and not calls


def test_twenty_sources_and_refresh_preserve_poll_in_progress(services):
    monitor, discovery, feed, _, now = services
    channels = ["UC" + f"{number:022}" for number in range(20)]
    for index, channel in enumerate(channels):
        discovery.source({"source": channel, "active": index != 0})
    for channel in channels[1:]:
        feed.data[channel] = (channel, [])
    monitor.poll()
    assert feed.calls == [channels[1]]
    with pytest.raises(ValueError, match="Activa la fuente"):
        monitor.action("check", {"id": channels[0]})
    discovery.source({"id": channels[0], "active": True})
    now[0] += 30
    with monitor.connect(write=True) as db:
        db.execute("UPDATE channels SET next_check=?", (now[0] + 10800,))
        db.execute("UPDATE channels SET next_check=? WHERE id=?", (now[0] + 30, channels[0]))
    assert monitor.refresh_active() == {"scheduled": 19}
    with monitor.connect() as db:
        assert monitor._channel(db, channels[0])["next_check"] == now[0] + 30
    assert len(discovery.sources()) == 20


def test_atom_parses_only_observed_description_and_merges_metadata(services):
    monitor, discovery, feed, *_ = services
    discovery.source({"source": CHANNEL})
    raw = f"""<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/">
      <title>Canal</title><yt:channelId>{CHANNEL}</yt:channelId><entry><yt:channelId>{CHANNEL}</yt:channelId>
      <yt:videoId>{VIDEO}</yt:videoId><title>Título original</title><published>2026-08-25T12:00:00Z</published>
      <media:group><media:description>Entrevista sobre energía.</media:description></media:group></entry></feed>"""
    feed.data[CHANNEL] = yt.Atom(lambda _: raw.encode()).latest(CHANNEL)
    monitor.poll()
    first = discovery.browse({})["items"][0]
    assert first["description"] == "Entrevista sobre energía."
    assert first["dimensions"]["conversation"]["score"] is not None
    monitor.clock = lambda: NOW + 12000
    feed.data[CHANNEL][1][0].update(title="Título actualizado", description="")
    monitor.poll()
    updated = discovery.browse({})["items"][0]
    assert updated["title"] == "Título actualizado"
    assert updated["description"] == first["description"]


def seed(services, amount=26):
    monitor, discovery, feed, _, _ = services
    discovery.source({"source": CHANNEL, "category": "gaming"})
    discovery.source({"source": SECOND, "category": "podcast"})
    feed.data[CHANNEL] = ("Canal Principal", [{"id": video(index), "title": f"Entrevista sobre energía · Historia {index}",
        "published": NOW - index * 3600, "description": "Conversación pública sobre proyectos de energía solar."}
        for index in range(amount)])
    feed.data[SECOND] = ("Canal Secundario", [{"id": VIDEO, "title": "Medición adicional", "published": NOW - 5}])
    monitor.poll()
    monitor.poll()


def test_rank_explains_unavailable_dimensions_and_filters_all_pages(services):
    seed(services)
    monitor, discovery, *_ = services
    initial = discovery.rules()
    assert initial["weights"] == cd.WEIGHTS
    with pytest.raises(ValueError, match="100"):
        discovery.rules({**initial, "weights": {**cd.WEIGHTS, "affinity": 31}})
    rules = discovery.rules({**initial, "topics": [{"name": "Energía", "keywords": ["energía solar", "energía"]}]})
    assert rules["revision"] == 2
    with pytest.raises(cd.Conflict):
        discovery.rules(initial)
    first = discovery.browse({"q": "energia", "category": "gaming", "limit": 8})
    second = discovery.browse({"q": "energia", "channel": CHANNEL, "page": 2, "limit": 8, "at": first["at"]})
    assert first["total"] == second["total"] == 26
    assert not {item["id"] for item in first["items"]}.intersection(item["id"] for item in second["items"])
    assert first["items"][0]["score"] >= first["items"][-1]["score"] >= second["items"][0]["score"]
    top = first["items"][0]
    assert top["coverage"] == 90 and top["dimensions"]["verifiable"]["score"] is None
    assert top["dimensions"]["affinity"]["score"] == 80
    assert discovery.browse({"q": "Canal Secundario", "category": "podcast"})["total"] == 1
    assert discovery.browse({"from": "2000-01-01", "to": "2001-01-01", "sort": "date"})["total"] == 0
    discovery.rules({**rules, "topics": [{"name": "Renovables", "keywords": ["energía"]}]})
    assert discovery.browse({"q": "renovables"})["total"] == 26
    assert discovery.browse({"rights": "cleared"})["total"] == 0
    with monitor.connect() as db:
        assert db.execute("SELECT count(*) FROM videos").fetchone()[0] == 27


def test_review_idempotent_rights_and_future_submission_contract(services, tmp_path):
    seed(services, 1)
    monitor, discovery, *_ = services
    item = {"channel": CHANNEL, "video": video(0), "action": "save", "request_id": uuid4().hex,
            "revision": 0, "notes": "Verificar versión completa", "seconds": 12}
    saved = discovery.review(item)
    assert discovery.review(item) == saved
    with pytest.raises(cd.Conflict):
        discovery.review({**item, "action": "discard"})
    with pytest.raises(ValueError, match="referencia"):
        discovery.review({**item, "request_id": uuid4().hex, "revision": 1, "rights": "cleared"})
    with pytest.raises(ValueError, match="aprobación"):
        discovery.prepare_submission(CHANNEL, item["video"])
    approved = discovery.review({**item, "request_id": uuid4().hex, "revision": 1, "action": "approve",
                                 "rights": "cleared", "rights_note": "Permiso por escrito", "seconds": 4})
    assert approved["state"] == "approved"
    plan = discovery.prepare_submission(CHANNEL, item["video"])
    assert not list(tmp_path.glob("*/project.json"))
    conventional = tmp_path / "123456abcdef"
    projects.init_project(conventional, "https://youtu.be/" + item["video"] + "?t=15", {}, conventional.name)
    with pytest.raises(cd.Conflict):
        discovery.prepare_submission(CHANNEL, item["video"])
    (conventional / "project.json").unlink()
    project = tmp_path / plan["project_id"]
    projects.init_project(project, plan["url"], {}, project.name)
    with pytest.raises(cd.Conflict):
        discovery.prepare_submission(CHANNEL, item["video"])
    rebuilt = cd.Discovery(yt.Monitor(tmp_path, lambda *_: None, lambda: False, start=False), tmp_path)
    assert rebuilt.metrics()["review_seconds"] == 16
    assert rebuilt.browse({"library": "1"})["items"][0]["notes"] == item["notes"]
    rebuilt.review({**item, "request_id": uuid4().hex, "revision": 2, "action": "discard", "seconds": 0})
    assert rebuilt.browse({"state": "discarded"})["total"] == 1
    assert rebuilt.browse({"library": "1"})["total"] == 0


def test_native_ui_discovery_review_and_metrics(tmp_path, chromium_page):
    page, expect = chromium_page
    page.set_viewport_size({"width": 1440, "height": 1024})
    screenshots = os.environ.get("ANQORA_DISCOVERY_EVIDENCE")
    with running_server(tmp_path) as (server, base):
        server.app.channels.close()
        token = server.app.token
        feed = Feed()
        now = int(time.time())
        server.app.channels.feed = feed
        path = "/api/content-discovery"
        with pytest.raises(HTTPError) as missing_token:
            request(base, path + "/source", {"source": CHANNEL})
        assert missing_token.value.code == 403
        page.goto(base)
        page.get_by_role("button", name="Content Discovery", exact=True).click()
        expect(page.locator("#discovery-count")).to_have_text("0 oportunidades")
        page.get_by_role("tab", name="Fuentes").click()
        form = page.locator("#discovery-source-form")
        expect(form.get_by_label("Modalidad")).to_have_value("discover")
        expect(form.get_by_label("Frecuencia en minutos")).to_have_value("180")
        form.get_by_label("Canal", exact=True).fill(CHANNEL)
        form.get_by_label("Categoría").select_option("podcast")
        form.get_by_role("button", name="Guardar fuente").click()
        expect(page.locator("#discovery-feedback")).to_have_text("Fuente guardada.")
        feed.data[CHANNEL] = ("Radar de conversaciones", [
            {"id": VIDEO, "title": "¿Cómo funciona la energía solar? Entrevista y recorrido",
             "description": "Una conversación sobre baterías y proyectos locales.", "published": now - 3500},
            {"id": video(2), "title": "Mesa redonda sobre baterías y almacenamiento",
             "description": "Preguntas de la comunidad sobre proyectos energéticos.", "published": now - 7200},
            {"id": video(3), "title": "Cronología de una biblioteca comunitaria",
             "description": "Testimonio de participantes.", "published": now - 8400},
        ])
        server.app.channels.poll()
        page.get_by_role("tab", name="Fuentes").click()
        expect(page.locator("#discovery-source-list")).to_contain_text("Radar de conversaciones")
        page.get_by_text("Reglas de ranking y temas", exact=True).click()
        page.get_by_label("Temas y palabras clave").fill("Energía: energía solar, baterías")
        page.get_by_role("button", name="Guardar reglas").click()
        expect(page.locator("#discovery-feedback")).to_have_text("Reglas guardadas.")
        if screenshots:
            page.screenshot(path=str(Path(screenshots) / "discovery-fuentes.png"), full_page=True)
        page.get_by_role("tab", name="Descubrir").click()
        expect(page.locator("#discovery-count")).to_have_text("3 oportunidades")
        assert not list(tmp_path.glob("*/project.json"))
        card = page.locator(".discovery-video").filter(has_text="Cómo funciona")
        card.get_by_text("Ver justificación", exact=True).click()
        expect(card.get_by_text("Sin evaluar", exact=True)).to_be_visible()
        if screenshots:
            page.screenshot(path=str(Path(screenshots) / "discovery-bandeja.png"), full_page=True)
        card.get_by_role("button", name="Guardar", exact=True).click()
        page.get_by_role("tab", name="Biblioteca").click()
        expect(page.locator("#discovery-count")).to_have_text("1 oportunidad")
        rights_filter = page.locator('#discovery-filters select[name="rights"]')
        rights_filter.select_option("cleared")
        page.get_by_role("button", name="Aplicar filtros").click()
        expect(page.locator("#discovery-count")).to_have_text("0 oportunidades")
        rights_filter.select_option("pending")
        page.get_by_role("button", name="Aplicar filtros").click()
        expect(page.locator("#discovery-count")).to_have_text("1 oportunidad")
        if screenshots:
            page.screenshot(path=str(Path(screenshots) / "discovery-biblioteca.png"), full_page=True)
        page.get_by_role("button", name="Vista previa y revisión").click()
        review = page.get_by_role("dialog")
        expect(review.get_by_role("link", name="Abrir el video original en YouTube")).to_have_attribute(
            "href", "https://www.youtube.com/watch?v=" + VIDEO)
        expect(review.get_by_role("button", name="Enviar a proyecto")).to_be_disabled()
        review.get_by_label("Notas editoriales").fill("Revisar la fuente y confirmar licencias.")
        review.get_by_label("Estado editorial").select_option("approve")
        if screenshots:
            page.screenshot(path=str(Path(screenshots) / "discovery-revision.png"), full_page=True)
        review.get_by_role("button", name="Guardar revisión").click()
        expect(page.locator("#discovery-feedback")).to_have_text("Revisión guardada.")
        page.get_by_role("tab", name="Rendimiento").click()
        expect(page.locator("#discovery-metric-list")).to_contain_text("Oportunidades evaluadas")
        assert request(base, path + "/metrics")["approved"] == 1
        if screenshots:
            page.screenshot(path=str(Path(screenshots) / "discovery-rendimiento.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.get_by_role("tab", name="Descubrir").click()
        expect(page.locator("#discovery-count")).to_have_text("2 oportunidades")
        assert page.evaluate("document.documentElement.scrollWidth === innerWidth")
        if screenshots:
            page.screenshot(path=str(Path(screenshots) / "discovery-movil.png"), full_page=True)
        with pytest.raises(HTTPError) as outside_phase:
            request(base, path + "/send", {"video": VIDEO}, token)
        assert outside_phase.value.code == 404
