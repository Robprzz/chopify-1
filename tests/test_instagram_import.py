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


def transport(monkeypatch, content, *, url=REEL, length=None, available=True):
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
        return io.BytesIO(), response

    monkeypatch.setattr(importer,"transfer",send)
    return calls


@pytest.mark.parametrize("url", [REEL, POST])
def test_public_fixture_import_uses_same_project_pipeline(tmp_path, monkeypatch, url):
    original = gray_source(tmp_path)
    calls = transport(monkeypatch, original.read_bytes(), url=url)
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
    requested = []
    class Conn(io.BytesIO):
        def __init__(self, host, **_):
            super().__init__()
            requested.append(host)
        def request(self,*_,**__):
            pass
        def getresponse(self):
            return response
    monkeypatch.setattr(importer,"PinnedHTTPS",Conn)
    for code, error in ((403,"restringe"), (404,"eliminado"), (429,"temporalmente")):
        response = Response(b"")
        response.status = code
        with pytest.raises(ValueError,match=error):
            importer.transfer(REEL,media=False,cancel=lambda:None)
    response = Response(b"",Location="https://outside.invalid/file")
    response.status = 302
    with pytest.raises(ValueError,match="destino"):
        importer.transfer(CDN,media=True,cancel=lambda:None)
    assert requested == ["www.instagram.com"]*3 + ["video.cdninstagram.com"]


@pytest.mark.parametrize("ip",["127.0.0.1","192.168.1.10","169.254.169.254","::1","::ffff:127.0.0.1","64:ff9b::7f00:1","224.1.1.1"])
def test_nonpublic_dns_answers_are_refused(monkeypatch, ip):
    monkeypatch.setattr(socket,"getaddrinfo",lambda *_a,**_kw:[(0,0,0,"",(ip,443))])
    with pytest.raises(ValueError,match="destino"):
        importer.addresses("video.cdninstagram.com")


@pytest.mark.parametrize("cancel", [False, True])
def test_interrupted_or_cancelled_transfer_cleans_partial_files(tmp_path,monkeypatch,cancel):
    transport(monkeypatch,b"partial",length=100)
    def progress(*_):
        if cancel:
            (tmp_path/importer.CANCEL_FILE).touch()
    if cancel:
        with pytest.raises(importer.Cancelled):
            importer.import_video(REEL,tmp_path,importer.DEFAULTS,progress)
    else:
        with pytest.raises(ValueError,match="incompleta"):
            importer.import_video(REEL,tmp_path,importer.DEFAULTS,progress)
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
