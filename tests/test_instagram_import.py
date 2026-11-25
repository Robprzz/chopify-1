"""Controlled Instagram responses with real local MP4 decoding and project export."""
import io
from pathlib import Path
import socket
import subprocess
from urllib.error import HTTPError

import pytest

import instagram_import as importer
import projects
import subtitle_quality
from test_dashboard import request, running_server
from test_studio_navigation import chromium_page  # noqa: F401
from test_watermark import gray_source

REEL = "https://www.instagram.com/reel/Anqora_1/"
POST = "https://www.instagram.com/p/Anqora_1/"
CDN = "https://video.cdninstagram.com/object.mp4?key=fixture"
AUTH = {"authorized":True,"max_seconds":10,"max_mib":8}


class Response(io.BytesIO):
    status = 200
    chunked = False

    def __init__(self, data, **headers):
        super().__init__(data)
        self.headers = headers

    def getheader(self, key, default=None):
        return self.headers.get(key, default)


class CloseOnly:
    closed = False

    def close(self):
        self.closed = True


def transport(monkeypatch, content, *, url=REEL, length=None, available=True, resources=None):
    calls = []
    html = (f'<meta property="og:url" content="{url}">'
            '<meta property="og:title" content="Grabación 🎬">'
            '<meta property="og:video:actor" content="Autora">'
            + (f'<meta property="og:video" content="{CDN}">' if available else "")).encode()

    def send(address, *, media, cancel):
        calls.append(address)
        cancel()
        response = (Response(content, **{"Content-Type":"video/mp4",
                    "Content-Length":str(len(content) if length is None else length)}) if media else Response(html))
        connection = CloseOnly()
        if resources is not None:
            resources.extend((connection, response))
        return connection, response

    monkeypatch.setattr(importer,"transfer",send)
    return calls


@pytest.mark.parametrize("url", [REEL, POST])
def test_public_fixture_import_uses_same_project_pipeline(tmp_path, monkeypatch, url):
    original = gray_source(tmp_path)
    resources = []
    calls = transport(monkeypatch, original.read_bytes(), url=url, resources=resources)
    project = projects.create_project(tmp_path, url + "?igsh=discard", {"aspect":"9:16","camera_mode":"stable",
        "min_score":6, "min_len":1}, instagram=AUTH)
    folder = tmp_path / project["id"]

    def transcribe(media, workdir, *_args, **kw):
        assert kw["source"] == url
        projects.atomic_json(workdir/"transcript.json", {"video":str(media),"duration":3,
            "words":[{"word":"Hola mundo.","start":0.10,"end":0.9}], "model":"fixture"})
    monkeypatch.setattr(projects.dt,"transcribe",transcribe)
    # The scoring stage may find no useful candidates in this three-second fixture.
    projects.run_project(folder, action="prepare")
    assert projects.read_project(folder)["source_meta"]["author"] == "Autora"
    assert projects.read_project(folder)["source_meta"]["duration"] == pytest.approx(3,abs=.04)
    projects.run_project(folder, action="prepare")
    assert calls == [url, CDN]
    assert len(resources) == 4 and all(item.closed for item in resources)
    projects.atomic_json(folder/"segments.json", [{"start":0,"end":1.2,"hook":"fixture","overall":9}])
    result = projects.run_project(folder, action="render")
    output = Path(result["last_outputs"][0])
    duration, tracks = subtitle_quality.probe_media(output)
    assert duration == pytest.approx(1.2,abs=.06)
    assert set(tracks) == {"video","audio"}
    assert projects.render_clips.ffprobe_dims(output) == (1080,1920)
    assert "Hola" in output.with_suffix(".srt").read_text(encoding="utf-8")
    assert (folder/"instagram.mp4").read_bytes() == original.read_bytes()
    assert "key=fixture" not in (folder/"project.json").read_text(encoding="utf-8")
    with running_server(tmp_path) as (httpd, base):
        payload = {"clips":[f"{project['id']}/{output.name}"],"platforms":["youtube"]}
        draft = request(base,"/api/calendar/drafts",payload,httpd.app.token)["plans"][0]
        assert draft["file"] == output.name and draft["status"] == "draft"
        assert draft["consent"] is None


@pytest.mark.parametrize("url", [
    "http://instagram.com/reel/Example/", "https://instagram.com/accounts/login/",
    "https://www.instagram.com:443/p/Example/", "https://a@www.instagram.com/p/Example/",
    "https://instagram.com.evil.invalid/reel/Example/", "https://www.instagram.com/reel/Example/embed/",
])
def test_invalid_link(url):
    with pytest.raises(ValueError):
        importer.canonical(url)


def test_inaccessible_response_does_not_request_media(tmp_path, monkeypatch):
    calls = transport(monkeypatch,b"",available=False)
    with pytest.raises(ValueError, match="no expone"):
        importer.import_video(REEL,tmp_path,importer.DEFAULTS,lambda *_:None)
    assert calls == [REEL]


def test_http_private_or_removed_and_redirect_rejected(monkeypatch):
    requested, connections = [], []
    class Conn(CloseOnly):
        def __init__(self, host, **_):
            connections.append(self)
            requested.append(host)
        def request(self,*_,**__):
            pass
        def getresponse(self):
            return response
    monkeypatch.setattr(importer,"PinnedHTTPS",Conn)
    for code, error in ((403,"restringe"), (404,"eliminado"), (429,"temporalmente"), (500,"HTTP 500")):
        response = Response(b"")
        response.status = code
        with pytest.raises(ValueError,match=error):
            importer.transfer(REEL,media=False,cancel=lambda:None)
        assert response.closed and connections[-1].closed
    response = Response(b"", Location="/accounts/login/?next=discard")
    response.status = 302
    with pytest.raises(ValueError, match="iniciar sesión"):
        importer.transfer(REEL, media=False, cancel=lambda: None)
    assert response.closed and connections[-1].closed
    response = Response(b"",Location="https://outside.invalid/file")
    response.status = 302
    with pytest.raises(ValueError,match="destino"):
        importer.transfer(CDN,media=True,cancel=lambda:None)
    assert requested == ["www.instagram.com"]*5 + ["video.cdninstagram.com"]
    assert response.closed and all(conn.closed for conn in connections)


@pytest.mark.parametrize("ip",["127.0.0.1","192.168.1.10","169.254.169.254","::1","::ffff:127.0.0.1","64:ff9b::7f00:1","224.1.1.1"])
def test_nonpublic_dns_answers_are_refused(monkeypatch, ip):
    monkeypatch.setattr(socket,"getaddrinfo",lambda *_a,**_kw:[(0,0,0,"",(ip,443))])
    with pytest.raises(ValueError,match="destino"):
        importer.addresses("video.cdninstagram.com")


@pytest.mark.parametrize("cancel", [False, True])
def test_interrupted_or_cancelled_transfer_cleans_partial_files(tmp_path,monkeypatch,cancel):
    resources = []
    transport(monkeypatch,b"partial",length=100,resources=resources)
    def progress(*_):
        if cancel:
            (tmp_path/importer.CANCEL_FILE).touch()
    if cancel:
        with pytest.raises(importer.Cancelled):
            importer.import_video(REEL,tmp_path,importer.DEFAULTS,progress)
    else:
        with pytest.raises(ValueError,match="incompleta"):
            importer.import_video(REEL,tmp_path,importer.DEFAULTS,progress)
    assert all(item.closed for item in resources)
    assert not list(tmp_path.glob(".instagram-*/video.mp4"))
    assert not (tmp_path/"instagram.mp4").exists()


def test_browser_checks_permission_and_api_reuses_project(tmp_path, monkeypatch, chromium_page):
    page, expect = chromium_page
    with running_server(tmp_path) as (httpd, base):
        app = httpd.app
        token = app.token
        queued = []
        monkeypatch.setattr(app, "enqueue", lambda ident, action: queued.append((ident, action)))
        with pytest.raises(HTTPError) as blocked:
            request(base,"/api/batch",{"sources":[REEL]},token)
        assert blocked.value.code == 400
        for after_host in (".evil.invalid", "@127.0.0.1"):
            with pytest.raises(HTTPError):
                request(base,"/api/batch",{"sources":[f"https://instagram.com{after_host}/p/test"],"instagram":AUTH},token)
        assert not request(base, "/api/projects")["projects"]
        page.goto(base)
        page.locator("#show-projects").click()
        page.locator("#new-project").click()
        page.locator("#sources").fill(REEL)
        expect(page.locator("#import-origin")).to_contain_text("Instagram")
        page.locator("#instagram-authorized").check()
        with page.expect_response(lambda response: response.url.endswith("/api/batch")) as submitted:
            page.locator("#create-form button[type=submit]").click()
        result = submitted.value.json()["projects"][0]
        assert result["instagram_import"]["authorized"]
        expect(page.locator("#instagram-metadata")).to_contain_text("Instagram")
        again = request(base,"/api/batch", {"sources":[POST+"?igsh=duplicate"],"instagram":AUTH}, token)
        assert again["projects"][0]["id"] == result["id"] and again["projects"][0]["reused"]
        assert queued == [(result["id"],"prepare")]


def test_cancel_endpoint_signals_only_active_instagram_import(tmp_path, monkeypatch):
    with running_server(tmp_path) as (httpd, base):
        app = httpd.app
        state = request(base,"/api/projects",{"source":REEL,"instagram":AUTH},app.token)
        endpoint = f"/api/projects/{state['id']}/cancel-import"
        with pytest.raises(HTTPError):
            request(base,endpoint,{},app.token)
        monkeypatch.setattr(app,"busy",lambda ident: ident == state["id"])
        folder = tmp_path/state["id"]
        state["stage"] = "instagram download"
        projects.save_project(folder,state)
        assert request(base,endpoint,{},app.token)["ok"]
        assert (folder/importer.CANCEL_FILE).is_file()


def test_limits_abort_before_commit(tmp_path,monkeypatch):
    source = gray_source(tmp_path)
    transport(monkeypatch,source.read_bytes(),length=9*1024**2)
    with pytest.raises(ValueError,match="límite de tamaño"):
        importer.import_video(REEL,tmp_path,{"max_mib":8,"max_seconds":10},lambda *_:None)
    transport(monkeypatch,source.read_bytes())
    with pytest.raises(ValueError,match="límite de duración"):
        importer.import_video(REEL,tmp_path,{"max_mib":8,"max_seconds":1},lambda *_:None)
    assert not (tmp_path/"instagram.mp4").exists()


def test_removes_only_own_abandoned_partial(tmp_path):
    stage = tmp_path/".instagram-abcdefgh"
    stage.mkdir()
    (stage/"video.mp4").write_bytes(b"partial")
    protected = tmp_path/".instagram-cancel"
    protected.touch()
    importer.clean_interrupted(tmp_path)
    assert not stage.exists() and protected.exists()


def test_real_connection_contract():
    connection = importer.PinnedHTTPS("www.instagram.com")
    with pytest.raises(TypeError, match="context manager"):
        with connection:
            pass
    connection.close()


def test_allowed_redirect_closes_before_retry_and_connection_timeout(tmp_path, monkeypatch):
    connections = []
    redirect = Response(b"", Location=REEL.replace("www.instagram.com", "m.instagram.com"))
    redirect.status = 302

    class Conn(CloseOnly):
        def __init__(self, host, **_):
            self.host = host
            connections.append(self)

        def request(self, *_args, **_kwargs):
            if self.host == "m.instagram.com":
                assert redirect.closed and connections[0].closed
                raise TimeoutError("socket")

        def getresponse(self):
            return redirect

    monkeypatch.setattr(importer, "PinnedHTTPS", Conn)
    with pytest.raises(importer.ImportTimeout, match="tiempo de conexión"):
        importer.import_video(REEL, tmp_path, importer.DEFAULTS, lambda *_: None)
    assert [conn.host for conn in connections] == ["www.instagram.com", "m.instagram.com"]
    assert all(conn.closed for conn in connections)


def test_media_timeout_closes_both_responses_and_connections(tmp_path, monkeypatch):
    resources = []
    transport(monkeypatch, b"partial", resources=resources)
    read = Response.read1

    def slow(response, size):
        if response.getheader("Content-Type") == "video/mp4":
            raise TimeoutError("timed out")
        return read(response, size)

    monkeypatch.setattr(Response, "read1", slow)
    with pytest.raises(importer.ImportTimeout, match="tiempo de espera"):
        importer.import_video(REEL, tmp_path, importer.DEFAULTS, lambda *_: None)
    assert len(resources) == 4 and all(item.closed for item in resources)
    assert not list(tmp_path.glob(".instagram-*"))


def test_retry_in_chromium_reuses_failed_project_and_continues(tmp_path, monkeypatch, chromium_page):
    page, expect = chromium_page
    source = gray_source(tmp_path)
    calls = transport(monkeypatch, source.read_bytes())
    legacy_error = "'PinnedHTTPS' object does not support the context manager protocol"
    root = tmp_path / "projects"
    project = projects.create_project(root, REEL, {"min_score":6,"min_len":1}, instagram=AUTH)
    project.update(status="failed", stage="instagram download", error=legacy_error)
    folder = root / project["id"]
    projects.save_project(folder, project)
    old_stage = folder / ".instagram-abcdefgh"
    old_stage.mkdir()
    (old_stage / "video.mp4").write_bytes(b"old partial")
    (folder / importer.CANCEL_FILE).touch()

    def transcribe(media, workdir, *_args, **kw):
        assert kw["source"] == REEL
        projects.atomic_json(workdir/"transcript.json", {"video":str(media),"duration":3,
            "words":[{"word":"Hola mundo.","start":.1,"end":.9}],"model":"fixture"})

    monkeypatch.setattr(projects.dt, "transcribe", transcribe)
    with running_server(root) as (server, base):
        monkeypatch.setattr(server.app, "_run", lambda path, action, *_: projects.run_project(path, action=action))
        page.goto(base)
        page.locator("#show-projects").click()
        page.locator(f'#projects [data-id="{project["id"]}"]').click()
        expect(page.locator("#job-error")).to_contain_text("Error interno de Python")
        page.get_by_role("button", name="Reintentar descarga").click()
        expect(page.locator("#status")).to_contain_text("En revisión",timeout=30000)
        page.locator("#instagram-history summary").click()
        expect(page.locator("#instagram-errors")).to_contain_text("Error interno de Python")
        data = request(base, "/api/projects")["projects"]
        assert len(data) == 1 and data[0]["id"] == project["id"] and data[0]["source"] == REEL
        assert data[0]["instagram_errors"][0]["error"] == legacy_error
        assert not old_stage.exists()
        assert (folder/"instagram.mp4").read_bytes() == source.read_bytes()
        assert (folder/"segments.json").is_file()
        assert calls == [REEL, CDN]


def test_retry_accepts_legacy_authorized_url_for_same_post(tmp_path, monkeypatch):
    """Legacy query parameters and host aliases must not invalidate prior consent."""
    post = "https://www.instagram.com/p/DglkhqQvpW4/"
    state = projects.create_project(tmp_path, post, {}, instagram=AUTH)
    folder = tmp_path / state["id"]
    state["instagram_import"]["url"] = "https://m.instagram.com/p/DglkhqQvpW4/?igsh=legacy"
    projects.save_project(folder, state)
    (folder / "instagram.mp4").write_bytes(b"fixture")
    monkeypatch.setattr(importer, "import_video", lambda *_args: (folder / "instagram.mp4", {"platform": "instagram"}))
    file, meta = projects.instagram_source(folder, state, lambda *_args: None)
    assert file.name == "instagram.mp4"
    assert meta["platform"] == "instagram"


def test_retry_rejects_authorization_for_different_post(tmp_path, monkeypatch):
    post = "https://www.instagram.com/p/DglkhqQvpW4/"
    state = projects.create_project(tmp_path, post, {}, instagram=AUTH)
    folder = tmp_path / state["id"]
    state["instagram_import"]["url"] = "https://m.instagram.com/p/AnotherPost/?igsh=legacy"
    projects.save_project(folder, state)
    monkeypatch.setattr(importer, "import_video", lambda *_args: pytest.fail("Unauthorized download attempted"))
    with pytest.raises(ValueError, match="autorización"):
        projects.instagram_source(folder, state, lambda *_args: None)
