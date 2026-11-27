"""Studio integration with controlled feeds and actual local AVC/AAC exports."""
import hashlib
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

import export_cache
import export_settings
import ingest
import projects
import render_clips
import render_progress
import score_clips
import youtube_monitor
from test_dashboard import request, running_server
from test_signature import prepared
from test_studio_navigation import chromium_page  # noqa: F401
from test_watermark import gray_source
from test_workspace42 import fixture_media, new_plan

CHANNEL = "UC" + "a" * 22
OLD = "a" * 11
NEW = "b" * 11


def selected(project):
    return [item for item in project["videos"] if item["id"] == NEW][0]


def test_atom_only_accepts_official_identity_and_does_not_read_external_xml():
    feed = f"""<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
      <title>Canal</title><yt:channelId>{CHANNEL}</yt:channelId>
      <entry><yt:channelId>{CHANNEL}</yt:channelId><yt:videoId>{NEW}</yt:videoId>
      <title>Publicación</title><published>2026-08-07T00:00:00+00:00</published></entry>
      <entry><yt:channelId>wrong</yt:channelId><yt:videoId>{OLD}</yt:videoId></entry></feed>"""
    seen = []

    def fetch(path):
        seen.append(path)
        if path == "/@canal":
            return f'<meta itemprop="channelId" content="{CHANNEL}">'.encode()
        return feed.encode()

    adapter = youtube_monitor.Atom(fetch)
    assert adapter.resolve("https://www.youtube.com/@canal/videos") == CHANNEL
    name, videos = adapter.latest(CHANNEL)
    assert name == "Canal" and [item["id"] for item in videos] == [NEW]
    assert seen == ["/@canal", "/feeds/videos.xml?channel_id=" + CHANNEL]
    for address in ["http://youtube.com/@canal", "https://youtube.com.evil/@canal",
                    "https://youtube.com@127.0.0.1/@canal", "https://youtube.com/@foo%2Fother"]:
        with pytest.raises(ValueError):
            adapter.resolve(address)
    with pytest.raises(youtube_monitor.FeedError):
        youtube_monitor.Atom(lambda _: b'<!DOCTYPE feed [<!ENTITY e SYSTEM "file:///owned">]>').latest(CHANNEL)


def test_monitor_handles_backoff_permissions_and_restart_without_duplicates(tmp_path):
    now = [10000]
    items = [{"id": OLD, "title": "Inicial", "published": 9900}]
    calls = []

    class Feed:
        limited = False
        resolve = staticmethod(youtube_monitor.locator)

        def latest(self, channel):
            if self.limited:
                raise youtube_monitor.FeedError("429", 3600)
            return "Canal", items

    def dispatch(item, project):
        calls.append((item["id"], project, item["state"]))
        return ("ready", None) if item["state"] == "queued" else ("queued", None)

    feed = Feed()
    monitor = youtube_monitor.Monitor(tmp_path, dispatch, lambda: True, feed, lambda: now[0], start=False)
    monitor.action("add", {"source": CHANNEL, "interval": 5})
    monitor.poll()
    monitor.work()
    assert not calls and monitor.snapshot()["channels"][0]["videos"][0]["state"] == "reference"

    now[0] += 300
    feed.limited = True
    monitor.poll()
    with pytest.raises(ValueError, match="esperar"):
        monitor.action("check", {"id": CHANNEL})
    assert monitor.snapshot()["channels"][0]["next_check"] >= now[0] + 3600
    feed.limited = False
    now[0] += 3600
    items.append({"id": NEW, "title": "Nuevo", "published": now[0] - 5})
    monitor.poll()
    monitor.work()
    assert selected(monitor.snapshot()["channels"][0])["state"] == "unapproved"
    assert not calls
    monitor.action("settings", {"id": CHANNEL, "authorized": True, "reason": "Canal propio"})
    monitor.work()
    assert not calls  # Explicit import avoids a silent retrospective download.
    monitor.action("import", {"id": CHANNEL})
    monitor.work()
    now[0] += 5
    rebuilt = youtube_monitor.Monitor(tmp_path, dispatch, lambda: True, feed, lambda: now[0], start=False)
    rebuilt.work()
    rebuilt.work()
    assert [state for _, _, state in calls] == ["pending", "queued"]
    assert len({project for _, project, _ in calls}) == 1
    assert selected(rebuilt.snapshot()["channels"][0])["state"] == "ready"


def test_monitor_dispatch_reuses_same_real_studio_project(tmp_path, monkeypatch):
    item = {"id": NEW, "channel": CHANNEL, "published": 20, "reason": "Titular", "state": "pending"}
    with running_server(tmp_path) as (server, base):
        app = server.app
        app.channels.close()
        app.channels.action("add", {"source": CHANNEL, "authorized": True, "reason": "Titular",
                                   "preferred": 90, "maximum": 2, "template": "signature", "render": True})
        calls = []
        monkeypatch.setattr(app, "enqueue", lambda *args: calls.append(args))
        assert app._monitor_project(item, "abcabcabcabc")[0] == "queued"
        saved = request(base, "/api/projects/abcabcabcabc")["project"]
        assert saved["source"] == f"https://www.youtube.com/watch?v={NEW}"
        assert saved["youtube_monitor"]["authorization"] == "Titular"
        assert saved["options"]["device"] == "auto"
        assert saved["options"]["max_clips"] == 2 and saved["options"]["preferred_len"] == 90
        assert saved["options"]["signature"]["template"] == "anqora-signature-1"
        assert calls == [("abcabcabcabc", "all")]
        saved["status"] = "review"
        projects.save_project(tmp_path / saved["id"], saved)
    with running_server(tmp_path) as (server, base):
        assert server.app._monitor_project(item, "abcabcabcabc")[0] == "ready"
        assert len(request(base, "/api/projects")["projects"]) == 1


def test_monitored_download_requires_public_and_uses_no_credentials(tmp_path, monkeypatch):
    folder = tmp_path / "abcabcabcabc"
    state = projects.init_project(folder, f"https://www.youtube.com/watch?v={NEW}", {"device": "auto"},
                                  folder.name, monitored={"channel": CHANNEL, "authorization": "Titular"})
    calls = []

    def run(cmd):
        calls.append(cmd)
        return SimpleNamespace(stdout='{"availability":"private","channel_id":"' + CHANNEL + '","live_status":"not_live"}')

    monkeypatch.setattr(ingest, "_run", run)
    with pytest.raises(ValueError, match="público verificable"):
        projects.run_project(folder, action="prepare")
    assert projects.read_project(folder)["monitor_restricted"]
    assert len(calls) == 1 and "--no-cookies" in calls[0] and "--ignore-config" in calls[0]
    assert projects.read_project(folder)["source"] == state["source"]


def test_global_settings_drive_real_export_and_legacy_reuse_with_no_copy(tmp_path):
    source = gray_source(tmp_path)
    path = prepared(tmp_path, source)
    plain = Path(projects.run_project(path, action="render")["last_outputs"][0])
    checksum = hashlib.sha256(plain.read_bytes()).hexdigest()
    template = export_settings.Store(tmp_path)
    template.save({"enabled": True, "aspect": "9:16", "signature": True, "camera_mode": "stable", "watermark": False})
    branded = Path(projects.run_project(path, action="render")["last_outputs"][0])
    assert render_clips.ffprobe_dims(branded) == (1080, 1920)
    assert branded.with_suffix(".ass").is_file() and branded.with_suffix(".srt").is_file()
    with running_server(tmp_path) as (server, base):
        token = server.app.token
        detail = request(base, "/api/projects/" + path.name)
        assert detail["effective_export"]["inherited"] and detail["effective_export"]["signature"]
        request(base, f"/api/projects/{path.name}/settings", {"template_override": True}, token)
        detail = request(base, "/api/projects/" + path.name)
        assert detail["effective_export"]["aspect"] == "16:9" and not detail["effective_export"]["inherited"]
        assert server.app.clips.reexport(path.name, plain.name, "16:9") == plain.name
        assert len(list(plain.parent.glob("*.mp4"))) == 2
        request(base, "/api/export-template", {"enabled": True, "aspect": "1:1", "signature": True}, token)
        request(base, f"/api/projects/{path.name}/settings", {"template_override": False}, token)
        assert "9:16" in request(base, "/api/projects/" + path.name)["effective_export"]["warning"]
        request(base, "/api/export-template", {"enabled": False}, token)
    assert template.read()["enabled"] is False
    assert hashlib.sha256(plain.read_bytes()).hexdigest() == checksum
    assert Path(projects.run_project(path, action="render")["last_outputs"][0]) == plain


def test_project_cancel_interrupts_ffmpeg_and_leaves_no_partial_output(tmp_path):
    path = prepared(tmp_path, gray_source(tmp_path))
    state = projects.read_project(path)
    state["options"]["export_profile"] = "fast"
    projects.save_project(path, state)
    with running_server(tmp_path) as (server, base):
        (path / ".render-cancel").touch()
        with pytest.raises(render_progress.Cancelled):
            projects.run_project(path, action="render")
        assert projects.read_project(path)["status"] == "cancelled"
        assert not list((path / "clips").glob("*.mp4"))
    started = time.monotonic()
    tracker = render_progress.Progress(lambda _: None, lambda: time.monotonic() - started > .4)
    with pytest.raises(render_progress.Cancelled):
        render_progress.encode(["ffmpeg", "-nostdin", "-re", "-f", "lavfi", "-i", "color=size=32x32:rate=15",
                                "-t", "5", "-f", "null", "-"], tmp_path, 8, 5, tracker)
    assert time.monotonic() - started < 5


def test_recommendations_only_surface_qualified_distinct_windows(tmp_path):
    candidate = {"start": 0, "end": 8, "overall": 6.3, "seed_kind": "sentence", "endpoint_kind": "quality_best",
                 "score_parts": {"self_containment": .8, "payoff": .65, "penalties": 0}, "reason": "Contexto"}
    records = [candidate, {**candidate, "start": 1, "end": 9, "overall": 6.8},
               {**candidate, "start": 10, "end": 17},
               {**candidate, "start": 18, "end": 26, "score_parts": {"payoff": .9}}]
    clips, stats = score_clips.select_candidates(records, min_score=8)
    assert not clips and stats["passing"] == 0
    assert [item["start"] for item in stats["suggestions"]] == [1, 10]
    assert all(item["reason"] == "Contexto" for item in stats["suggestions"])
    words = [{"word": word, "start": i/3, "end": i/3 + .2}
             for i, word in enumerate("La Dra. Medina ayuda durante la entrevista.".split())]
    assert len(score_clips.sentenceize(words)) == 1

    path = prepared(tmp_path, gray_source(tmp_path))
    projects.atomic_json(path / "segments.json", [])
    short = dict(stats["suggestions"][0], start=0, end=1)
    projects.atomic_json(path / "selection.json", {**stats, "suggestions": [short]})
    with running_server(tmp_path) as (server, base):
        token = server.app.token
        detail = request(base, "/api/projects/" + path.name)
        assert detail["suggested"] and len(detail["candidates"]) == 1
        with pytest.raises(HTTPError):
            request(base, f"/api/projects/{path.name}/candidates",
                    {"candidates":[{**short,"end":2}], "suggested":True}, token)
        request(base, f"/api/projects/{path.name}/candidates",
                {"candidates":[{**short,"hook":"Declaración"}], "suggested":True}, token)
        assert not projects.read_json(path / "segments.json")
        assert projects.run_project(path, action="render", pick="1")["last_outputs"]
        request(base, f"/api/projects/{path.name}/settings", {"strict_selection":True}, token)
        assert not request(base, "/api/projects/" + path.name)["candidates"]
        with pytest.raises(ValueError, match="No clips reached"):
            projects.run_project(path, action="render", pick="1")


def test_chromium_template_channels_and_filtered_drafts(tmp_path, chromium_page):
    page, expect = chromium_page
    ident, media = fixture_media(tmp_path)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    with running_server(tmp_path) as (server, base):
        server.app.channels.close()
        token = server.app.token
        draft = request(base, "/api/calendar/save", new_plan(ident, media.name, local=""), token)
        keep = request(base, "/api/calendar/save", new_plan(ident, media.name, local="", platform="facebook"), token)
        published = request(base, "/api/calendar/save", new_plan(ident, media.name, local="", platform="instagram"), token)
        with server.app.calendar.connect(write=True) as db:
            db.execute("UPDATE plans SET status='published', confirmation='manual' WHERE id=?", (published["id"],))
        endpoint = "/api/calendar/delete-drafts"
        rejected = request(base, endpoint, {"confirmed":True, "items":[published, {**keep, "revision":0}]}, token)
        assert rejected["deleted"] == 0 and len(rejected["excluded"]) == 2
        page.goto(base)
        page.locator("#show-settings").click()
        page.locator("#show-watermark").click()
        form = page.locator("#export-template-form")
        expect(form.locator('[name="style"] option[value=default]')).to_have_count(1)
        form.locator('[name=enabled]').check()
        form.locator('[name=aspect]').select_option("1:1")
        form.locator('[name=signature]').select_option("signature")
        expect(page.locator("#export-template-warning")).to_contain_text("requiere 9:16")
        form.get_by_role("button", name="Guardar plantilla").click()
        expect(page.locator("#export-template-status")).to_contain_text("guardada")
        page.locator("#show-projects").click()
        page.locator("#show-youtube-monitor").click()
        channels = page.locator("#youtube-monitor-form")
        channels.locator('[name=source]').fill(CHANNEL)
        channels.get_by_role("button", name="Guardar canal").click()
        expect(page.locator("#youtube-monitor-list")).to_contain_text("Detección sin descarga")
        page.get_by_role("button", name="Pausar").click()
        expect(page.locator("#youtube-monitor-list")).to_contain_text("En pausa")
        page.locator("#show-publications").click()
        page.locator("#calendar-platform-filter").select_option("youtube")
        expect(page.locator("#calendar-drafts [data-draft-check]")).to_have_count(1)
        page.locator("#calendar-select-drafts").click()
        expect(page.locator("#calendar-draft-count")).to_have_text("1 seleccionados")
        page.locator("#calendar-remove-drafts").click()
        page.get_by_role("button", name="Sí, eliminar").click()
        expect(page.locator("#calendar-draft-message")).to_contain_text("1 borradores eliminados")
        with pytest.raises(HTTPError):
            request(base, "/api/calendar?id=" + draft["id"])
        assert request(base, "/api/calendar?id=" + keep["id"])["status"] == "draft"
        assert request(base, "/api/calendar?id=" + published["id"])["status"] == "published"
        assert media.exists() and not errors



def test_nvenc_probe_reports_actual_api_error_without_changing_profiles(monkeypatch):
    def run(command, **_):
        assert "h264_nvenc" in command and "-frames:v" in command
        return SimpleNamespace(returncode=1, stderr=b"Required: 13.1 Found: 13.0")

    monkeypatch.setattr(subprocess, "run", run)
    diagnostic = export_cache.nvenc_probe()
    assert not diagnostic["available"]
    assert "13.1" in diagnostic["message"] and "13.0" in diagnostic["message"]
    assert export_settings.PROFILES["quality"] == ("veryfast", "20")


def test_chromium_export_to_clips_reports_reuse(tmp_path, chromium_page):
    page, expect = chromium_page
    ident, media = fixture_media(tmp_path)
    digest = hashlib.sha256(media.read_bytes()).hexdigest()
    with running_server(tmp_path) as (_, base):
        page.goto(base)
        page.locator("#show-clips-library").click()
        page.locator(f'#clip-library [data-workspace="edit-clip"][data-key="{ident}/{media.name}"]').click()
        page.get_by_text("Formato y nueva exportación", exact=True).click()
        expect(page.locator("#clip-reexport")).to_be_visible()
        page.locator("#clip-reexport").click()
        expect(page.locator("#notice")).to_contain_text("Archivo reutilizado sin recodificar.", timeout=20000)
        assert list(media.parent.glob("*.mp4")) == [media]
        assert hashlib.sha256(media.read_bytes()).hexdigest() == digest


def test_public_monitor_ingestion_renders_through_existing_project_pipeline(tmp_path, monkeypatch):
    import json
    import shutil
    import download_and_transcribe as dt

    source = gray_source(tmp_path)
    folder = tmp_path / "abcabcabcabc"
    projects.init_project(folder, f"https://www.youtube.com/watch?v={NEW}",
                          {"device":"auto", "min_len":1, "max_len":3, "camera_mode":"stable", "min_score":7},
                          folder.name, monitored={"channel":CHANNEL, "authorization":"Titular"})
    commands = []

    def probe(command):
        commands.append(command)
        return SimpleNamespace(stdout=json.dumps({
            "availability":"public", "live_status":"not_live", "channel_id":CHANNEL,
            "duration":3, "formats":[{"height":360,"width":640,"acodec":"aac","vcodec":"avc1"}],
        }))

    def download(command):
        commands.append(command)
        target = Path(command[command.index("-o") + 1].replace("%(ext)s", "mp4"))
        shutil.copyfile(source, target)

    def transcribe(video, workdir, model, device, *_args, **_kwargs):
        assert model == "medium" and device == "auto"
        path = workdir / "transcript.json"
        projects.atomic_json(path, {"video":str(video), "duration":3.,
                                    "words":[{"word":"Hola.", "start": .1, "end": .8}]})
        return path

    def discover(workdir, **_):
        score_clips.save_candidates(workdir, [{"start":0, "end":1.2, "overall":8, "hook":"Autorizado"}], {})

    monkeypatch.setattr(ingest, "_run", probe)
    monkeypatch.setattr(dt, "run", download)
    monkeypatch.setattr(dt, "transcribe", transcribe)
    monkeypatch.setattr(score_clips, "discover", discover)
    result = projects.run_project(folder, "all")
    assert result["status"] == "complete"
    assert all("--ignore-config" in cmd and "--no-cookies-from-browser" in cmd for cmd in commands)
    assert len(commands) == 2
    output = Path(result["last_outputs"][0])
    assert render_clips.ffprobe_dims(output) == (1920, 1080)
    assert "Hola" in output.with_suffix(".srt").read_text(encoding="utf-8")


def test_camera_analysis_is_reused_until_the_source_changes(tmp_path, monkeypatch):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"source")
    samples = [(0.0, [140], False)]
    calls = []

    def detect(*_):
        calls.append("analysis")
        return samples[:]

    monkeypatch.setattr(render_clips, "detect_track", detect)
    result = render_clips.cached_track(path, 0, 3, 640, tmp_path, False, 30)
    samples[:] = [(0.0, [505], False)]
    assert render_clips.cached_track(path, 0, 3, 640, tmp_path, False, 30)[0][1] == result[0][1]
    assert len(calls) == 1
    path.write_bytes(b"changed source")
    assert render_clips.cached_track(path, 0, 3, 640, tmp_path, False, 30)[0][1] == [505]
    assert len(calls) == 2
