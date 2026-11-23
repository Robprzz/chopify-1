"""Exercise actual HTTP routes, mutation protection and saved project editing."""
from contextlib import contextmanager
import json
import shutil
import socket
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest


@contextmanager
def running_server(root):
    import dashboard
    httpd = dashboard.make_server(root, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        httpd.app.close()
        thread.join()


@pytest.fixture
def server(tmp_path):
    with running_server(tmp_path) as active:
        yield active


def request(base, path, data=None, token=None, origin=None):
    headers = {}
    if token:
        headers["X-Chopify-Token"] = token
    if origin:
        headers["Origin"] = origin
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path,
        data=json.dumps(data).encode() if data is not None else None, headers=headers)
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def test_http_rejects_cross_origin_and_unauthenticated_mutations(server):
    httpd, base = server
    for token, origin in [(None, base), (httpd.app.token, "https://evil.example")]:
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(base, "/api/projects", {"source": "test.mp4", "options": {}}, token, origin)
        assert exc.value.code == 403


def test_create_edit_and_read_project_over_http(server):
    httpd, base = server
    project = request(base, "/api/projects", {"source": "recording.mp4", "options": {}}, httpd.app.token)
    assert len(request(base, "/api/projects")["projects"]) == 1
    project_id = project["id"]
    request(base, f"/api/projects/{project_id}/settings", {"aspect": "9:16", "layout": "split"}, httpd.app.token)
    detail = request(base, f"/api/projects/{project_id}")
    assert detail["project"]["options"]["layout"] == "split"
    assert detail["candidates"] == []
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(base, f"/api/projects/{project_id}/settings", {"min_len": -1}, httpd.app.token)
    assert exc.value.code == 400


def test_config_exposes_caption_presets_and_defaults(server):
    httpd, base = server
    config = request(base, "/api/config")
    assert set(config["caption_presets"]) == {
        "default", "clean", "minimal", "bold", "boxed", "karaoke", "pop",
        "neon", "typewriter", "retro", "hormozi", "mrbeast", "podcast"}
    assert config["caption_presets"]["bold"]["animation"] == "pop"
    assert config["caption_defaults"]["words_per_cue"] == 3
    assert config["caption_aspects"]["9:16"]["font_size"] == 96
    assert "money" in config["caption_power_words"] or "secret" in config["caption_power_words"]
    assert any(rule["emoji"] == "💰" for rule in config["caption_emoji_rules"])


def test_publications_and_live_routes_serve_expected_bodies(server):
    import dashboard

    httpd, base = server
    with urllib.request.urlopen(base + "/publications.js") as response:
        assert response.status == 200
        assert response.headers.get_content_type() == "text/javascript"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.read() == (dashboard.STATIC / "publications.js").read_bytes()
    with urllib.request.urlopen(base + "/api/live") as response:
        assert response.status == 200
        assert response.headers.get_content_type() == "application/json"
        assert json.load(response) == {"sessions": []}
    tiktok = request(base, "/api/tiktok/state", {}, httpd.app.token)
    assert tiktok["history"] == tiktok["clips"] == []


def test_live_list_skips_session_file_removed_during_read(server, tmp_path, monkeypatch):
    import projects

    httpd, base = server
    state_file = tmp_path / "live_session.json"
    state_file.write_text("{}", encoding="utf-8")
    httpd.app.live_sessions["removed"] = SimpleNamespace(dir=tmp_path, stop=lambda: None)
    read_json = projects.read_json

    def remove_before_read(path):
        if path == state_file:
            path.unlink()
        return read_json(path)

    monkeypatch.setattr(projects, "read_json", remove_before_read)
    assert request(base, "/api/live") == {"sessions": []}


def test_studio_serves_startup_assets_until_restarted(tmp_path, monkeypatch, request):
    import dashboard

    assets = tmp_path / "studio"
    shutil.copytree(dashboard.STATIC, assets)
    monkeypatch.setattr(dashboard, "STATIC", assets)
    _, base = request.getfixturevalue("server")
    original_index = (assets / "index.html").read_bytes()
    original_script = (assets / "publications.js").read_bytes()

    # Files may be replaced or removed by a checkout update during a session.
    (assets / "index.html").write_text("New checkout")
    (assets / "publications.js").unlink()
    with urllib.request.urlopen(base) as response:
        assert response.read() == original_index
    with urllib.request.urlopen(base + "/publications.js") as response:
        assert response.status == 200
        assert response.read() == original_script


def test_studio_reports_missing_publications_script_before_listening(tmp_path, monkeypatch):
    import dashboard

    assets = tmp_path / "studio"
    shutil.copytree(dashboard.STATIC, assets)
    monkeypatch.setattr(dashboard, "STATIC", assets)
    (assets / "publications.js").unlink()

    with pytest.raises(RuntimeError, match=r"publications\.js.*complete checkout"):
        dashboard.make_server(tmp_path / "projects", port=0)


def test_studio_rejects_a_second_server_on_the_same_port(server, tmp_path):
    import dashboard

    httpd, _ = server
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        assert httpd.socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) == 1
    with pytest.raises(OSError):
        dashboard.make_server(tmp_path / "other", port=httpd.server_port)


def test_caption_style_saves_to_project_and_rejects_invalid(server):
    httpd, base = server
    project = request(base, "/api/projects", {"source": "recording.mp4", "options": {}}, httpd.app.token)
    prefix = f"/api/projects/{project['id']}"
    caption = {"words_per_cue": 2, "uppercase": True, "position": "top",
               "animation": "bounce", "background": "box",
               "background_color": "#101010", "background_opacity": 60,
               "highlight_mode": "karaoke", "emoji": True, "emphasis": True}
    request(base, prefix + "/settings", {"caption": caption}, httpd.app.token)
    detail = request(base, prefix)
    assert detail["project"]["options"]["caption"] == caption
    for bad in ({"words_per_cue": 99}, {"color": "red"}, {"animation": "spin"}):
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(base, prefix + "/settings", {"caption": bad}, httpd.app.token)
        assert exc.value.code == 400


def test_media_cannot_escape_project_and_supports_ranges(server):
    import projects
    httpd, base = server
    state = projects.create_project(httpd.app.root, "test.mp4", {})
    path = projects.project_path(httpd.app.root, state["id"])
    (path / "clips").mkdir()
    (path / "clips" / "test.mp4").write_bytes(b"0123456789")
    req = urllib.request.Request(base + f"/media/{state['id']}/clips/test.mp4", headers={"Range": "bytes=2-5"})
    with urllib.request.urlopen(req) as r:
        assert r.status == 206
        assert r.read() == b"2345"
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(base + f"/media/{state['id']}/clips/%2e%2e/project.json")
    assert exc.value.code in (400, 403, 404)


def test_queued_projects_reject_duplicate_jobs_and_edits(server, monkeypatch):
    httpd, base = server
    gate = threading.Event()
    monkeypatch.setattr(httpd.app, "_run", lambda *args: gate.wait(10))
    project = request(base, "/api/projects", {"source": "test.mp4", "options": {}}, httpd.app.token)
    prefix = f"/api/projects/{project['id']}"
    try:
        request(base, prefix + "/run", {"action": "prepare"}, httpd.app.token)
        assert request(base, prefix)["busy"]
        for action, body in [("run", {"action": "prepare"}),
                             ("settings", {"style": "podcast"}),
                             ("settings", {"caption": {"words_per_cue": 2}})]:
            with pytest.raises(urllib.error.HTTPError) as exc:
                request(base, prefix + "/" + action, body, httpd.app.token)
            assert exc.value.code == 400
    finally:
        gate.set()


def _fake_live_session(tmp_path, source, options):
    import live
    import projects
    from live_capture import LiveSource
    session = live.LiveSession.__new__(live.LiveSession)
    session._state_lock = threading.Lock()
    session.id = "abcdef123456"
    session.dir = tmp_path
    session.source = LiveSource(source, "hls", "generic", "passthrough", [])
    session.options = projects.validate_options(options)
    session.status = "running"
    session.started_at = 1.0
    session.stopped_at = None
    session.stream_time = 0.0
    session.recent_scores = []
    session.records = []
    session.errors = []
    session.stop = lambda: None
    session._save()
    return session


def test_live_routes(server, monkeypatch, tmp_path):
    httpd, base = server
    session = _fake_live_session(tmp_path, "https://x/l.m3u8", {
        "live_no_chat": True, "live_no_transcript": True})

    def fake_start_live(source, options, brief_text=None):
        httpd.app.live_sessions[session.id] = session
        return session

    monkeypatch.setattr(httpd.app, "start_live", fake_start_live)
    created = request(base, "/api/live", {
        "source": "https://x/l.m3u8",
        "options": {"live_no_chat": True, "live_no_transcript": True}}, httpd.app.token)
    assert created["id"] == "abcdef123456"
    listed = request(base, "/api/live")
    assert "abcdef123456" in [s["id"] for s in listed["sessions"]]
    detail = request(base, "/api/live/abcdef123456")
    assert detail["status"] == "running"
    stopped = request(base, "/api/live/abcdef123456/stop", {}, httpd.app.token)
    assert stopped["ok"] is True


def test_live_route_rejects_missing_token(server):
    _, base = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(base, "/api/live", {"source": "https://x/l.m3u8"})
    assert exc.value.code == 403


def test_live_history_with_archived_clips_after_server_restart(tmp_path):
    import projects

    ident = "0123456789ab"
    directory = tmp_path / "live" / ident
    records = directory / "records"
    records.mkdir(parents=True)
    clip = {"id": "aabbccddeeff", "start": 4, "end": 20, "score": 8, "files": {"mp4": "aabbccddeeff-clip.mp4"}}
    projects.atomic_json(records / "aabbccddeeff.json", clip)
    manifest = directory / "live_session.json"
    saved = {"id": ident, "status": "running", "started_at": 123, "stream_time": 23,
             "source": {"kind": "hls", "platform": "youtube", "label": "YouTube · 2dt0yxm4RUE"},
             "recent_scores": [6.5, 8.0], "errors": ["Capture interrupted"],
             "triggers": [], "export": {"completed": 1, "errors": 0}}
    projects.atomic_json(manifest, saved)
    with running_server(tmp_path) as (httpd, base):
        assert request(base, "/api/live")["sessions"] == [{
            "id": ident, "status": "interrupted", "source": saved["source"], "stream_time": 23,
            "started_at": 123, "clips": 1, "score": 8.0, "errors": 1,
        }]
        historical = request(base, f"/api/live/{ident}")
        assert historical.pop("production")["detected"] == 0
        assert historical == {**saved, "status": "interrupted", "triggers": [clip]}
        assert httpd.app.live_sessions == {}
        assert projects.read_json(manifest) == saved
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(base, "/api/live/ffffffffffff")
        assert exc.value.code == 404


def test_live_duplicate_start_reservation_and_url_aliases(server, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import live
    import live_capture

    httpd, base = server
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def resolve(source, options):
        calls.append(source)
        entered.set()
        assert release.wait(10)
        return live_capture.LiveSource(source, "hls", "youtube", "stub", [])

    monkeypatch.setattr(live_capture, "check_capture_tools", lambda: None)
    monkeypatch.setattr(live_capture, "resolve_live_source", resolve)
    monkeypatch.setattr(live.LiveSession, "start", lambda self: None)
    primary = "https://www.youtube.com/live/2dt0yxm4RUE?token=do-not-save"
    alternate = "https://youtu.be/2dt0yxm4RUE"

    def start(source):
        return request(base, "/api/live", {"source": source}, httpd.app.token)

    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(start, primary)
        try:
            assert entered.wait(5)
            with pytest.raises(urllib.error.HTTPError) as exc:
                start(alternate)
            assert exc.value.code == 409 and json.load(exc.value)["session_id"] is None
        finally:
            release.set()
        ident = future.result(timeout=5)["id"]

    with pytest.raises(urllib.error.HTTPError) as exc:
        start("https://m.youtube.com/watch?v=2dt0yxm4RUE")
    assert exc.value.code == 409 and json.load(exc.value)["session_id"] == ident
    assert calls == [primary]
    saved = request(base, f"/api/live/{ident}")
    assert saved["source"]["label"] == "YouTube · 2dt0yxm4RUE"
    assert "do-not-save" not in json.dumps(saved)
    assert saved["threshold"] == 6
    request(base, f"/api/live/{ident}/stop", {}, httpd.app.token)
    assert start(alternate)["id"] != ident
