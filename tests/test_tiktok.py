"""Exercise the real Studio routes with a mocked official API transport."""
import hashlib
import io
import json
import shutil
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPSHandler, build_opener, urlopen
from urllib.response import addinfourl

import pytest

import projects
import tiktok
from test_dashboard import request, server  # noqa: F401


def api_reply(data):
    return 200, json.dumps({"data": data, "error": {"code": "ok"}}).encode()


class FakeTikTok:
    def __init__(self):
        self.calls = []
        self.token = "access-for-test"
        self.open_id = "account-for-test"
        self.scopes = "user.info.basic,video.upload,video.publish"
        self.result = "SEND_TO_USER_INBOX"
        self.fail_upload = False
        self.init_result = {
            "publish_id": "v_inbox_file~v2.123",
            "upload_url": "https://open-upload.tiktokapis.com/video/?upload_token=mock",
        }
        self.init_error = None

    def __call__(self, req):
        self.calls.append(req)
        path = urlsplit(req.full_url).path
        if path == "/v2/oauth/token/":
            fields = parse_qs(req.data.decode())
            if fields["grant_type"] == ["refresh_token"]:
                assert fields["refresh_token"] == ["refresh-for-test"]
                self.token = "rotated-access-for-test"
            return 200, json.dumps({
                "access_token": self.token, "refresh_token": "refresh-for-test",
                "open_id": self.open_id, "scope": self.scopes,
                "expires_in": 3600, "refresh_expires_in": 86400,
            }).encode()
        if path == "/v2/oauth/revoke/":
            assert parse_qs(req.data.decode())["token"] == [self.token]
            return 200, b""
        if path in {"/video/", "/upload"}:
            assert req.get_method() == "PUT"
            assert req.get_header("Authorization") is None
            if self.fail_upload:
                raise tiktok.TikTokError("Interrupción simulada.")
            return 201, b""
        assert req.get_header("Authorization") == f"Bearer {self.token}"
        if path == "/v2/user/info/":
            assert req.get_method() == "GET"
            return api_reply({"user": {"open_id": self.open_id, "display_name": "Cuenta de prueba"}})
        if path == "/v2/post/publish/creator_info/query/":
            return api_reply({"privacy_level_options": ["SELF_ONLY", "MUTUAL_FOLLOW_FRIENDS"]})
        if path == tiktok.INBOX_INIT:
            if self.init_error:
                raise self.init_error
            return api_reply(self.init_result)
        if path == tiktok.POST_STATUS:
            assert json.loads(req.data) == {"publish_id": self.init_result["publish_id"]}
            return api_reply({"status": self.result, "fail_reason": "duration_check_failed"})
        pytest.fail(f"Unexpected request to {path}")


@pytest.fixture
def fake_studio(server, monkeypatch):
    httpd, base = server
    service = httpd.app.tiktok
    fake = FakeTikTok()
    monkeypatch.setattr(service, "api", tiktok.TikTokAPI("key-for-test", "secret-for-test"))
    monkeypatch.setattr(service.api, "send", fake)
    monkeypatch.setattr(service, "scopes", tiktok.SCOPES + ("video.publish",))

    def post(action, **kwargs):
        return request(base, "/api/tiktok/" + action, kwargs, httpd.app.token, base)

    return service, fake, base, post


def connect(base, post):
    url = urlsplit(post("connect")["url"])
    params = parse_qs(url.query)
    assert url.netloc == "www.tiktok.com"
    assert params["redirect_uri"] == [base + tiktok.CALLBACK]
    assert params["code_challenge_method"] == ["S256"]
    assert params["scope"] == ["user.info.basic,video.upload,video.publish"]
    callback = base + tiktok.CALLBACK + "?" + urlencode({
        "state": params["state"][0], "code": "approved-code",
    })
    with urlopen(callback) as response:
        assert response.geturl() == base + "/#publicaciones"
    return params, callback


def export(service, monkeypatch):
    project = projects.create_project(service.root, "recording.mp4", {})
    path = projects.project_path(service.root, project["id"])
    (path / "clips").mkdir()
    video = path / "clips" / "victoria.mp4"
    video.write_bytes(b"local export bytes")
    projects.atomic_json(video.with_suffix(".meta.json"), {
        "title": "Ganamos #shorts", "hashtags": ["#shorts", "#reels", "#gaming"],
    })
    monkeypatch.setattr(tiktok, "_inspect_mp4", lambda _path: {
        "width": 1080, "height": 1920, "duration": 22.5, "vertical_9_16": True,
    })
    return project["id"], video


def test_oauth_pkce_state_rotation_and_revoke(fake_studio):
    service, fake, base, post = fake_studio
    with pytest.raises(HTTPError) as exc:
        urlopen(base + tiktok.CALLBACK + "?state=forged&code=x")
    assert exc.value.code == 400
    assert fake.calls == []
    params, callback = connect(base, post)
    form = parse_qs(fake.calls[0].data.decode())
    assert form["client_secret"] == ["secret-for-test"]
    assert form["code"] == ["approved-code"]
    assert hashlib.sha256(form["code_verifier"][0].encode()).hexdigest() == params["code_challenge"][0]
    with pytest.raises(HTTPError):
        urlopen(callback)
    assert post("state")["account"] == "Cuenta de prueba"
    assert "secret-for-test" not in json.dumps(post("state"))
    service.token["expires"] = time.monotonic() - 1
    assert post("privacy")["options"] == ["SELF_ONLY", "MUTUAL_FOLLOW_FRIENDS"]
    assert service.token["access_token"] == "rotated-access-for-test"
    assert post("disconnect") == {"ok": True}
    assert post("state")["account"] is None
    assert service.token is None


def test_missing_scope_and_cross_origin_prevent_requests(fake_studio):
    _, fake, base, post = fake_studio
    with pytest.raises(HTTPError) as exc:
        request(base, "/api/tiktok/connect", {}, origin="https://untrusted.invalid")
    assert exc.value.code == 403
    assert fake.calls == []
    fake.scopes = "user.info.basic"
    connect(base, post)
    state = post("state")
    assert state["account"] is None
    assert "no concedió" in state["oauth_error"]
    assert len(fake.calls) == 1


def test_review_approval_upload_inbox_and_persisted_status(fake_studio, monkeypatch):
    service, fake, base, post = fake_studio
    connect(base, post)
    project, video = export(service, monkeypatch)
    assert post("state")["clips"] == [
        {"project": project, "project_name": "recording.mp4", "filename": video.name}]
    review = post("inspect", project=project, filename=video.name)
    assert review["description"] == "Ganamos #gaming"
    assert review["url"] == f"/media/{project}/clips/victoria.mp4"
    with urlopen(base + review["url"]) as response:
        assert response.read() == video.read_bytes()
    before_upload = len(fake.calls)
    with pytest.raises(HTTPError):
        post("upload", review_id=review["review_id"], description="Editada", approved=1)
    assert len(fake.calls) == before_upload
    job = post("upload", review_id=review["review_id"], description="Editada", approved=True)
    assert job["status"] == "PROCESSING_UPLOAD"
    assert json.loads(fake.calls[-2].data) == {"source_info": {
        "source": "FILE_UPLOAD", "video_size": video.stat().st_size,
        "chunk_size": video.stat().st_size, "total_chunk_count": 1}}
    assert fake.calls[-1].data == video.read_bytes()
    assert fake.calls[-1].get_header("Content-range") == f"bytes 0-{video.stat().st_size - 1}/{video.stat().st_size}"
    assert post("upload", review_id=review["review_id"], description="Editada", approved=True) == job
    assert len(fake.calls) == before_upload + 2
    assert post("status", id=job["id"])["status"] == "SEND_TO_USER_INBOX"
    pending_review = post("inspect", project=project, filename=video.name)
    assert post("upload", review_id=pending_review["review_id"], description="", approved=True)["id"] == job["id"]
    assert len(fake.calls) == before_upload + 3
    assert post("state")["history"][0]["description"] == "Editada"
    persisted = next(service.history.glob("*.json")).read_text(encoding="utf-8")
    assert all(value not in persisted for value in
               ("access-for-test", "secret-for-test", "refresh-for-test", "upload_token", "account-for-test"))


def test_changed_file_and_other_account_cannot_reuse_approval(fake_studio, monkeypatch):
    service, fake, base, post = fake_studio
    connect(base, post)
    project, video = export(service, monkeypatch)
    with pytest.raises(HTTPError):
        post("inspect", project=project, filename="../victoria.mp4")
    review = post("inspect", project=project, filename=video.name)
    video.write_bytes(b"altered MP4")
    before_upload = len(fake.calls)
    job = post("upload", review_id=review["review_id"], description="", approved=True)
    assert job["status"] == "LOCAL_ERROR"
    assert "cambió" in job["last_error"]
    assert len(fake.calls) == before_upload
    post("disconnect")
    fake.open_id = "some-other-account"
    connect(base, post)
    assert post("state")["history"] == []
    with pytest.raises(HTTPError):
        post("status", id=job["id"])


def test_interrupted_transfer_reuses_history_after_restart(fake_studio, server, monkeypatch):
    service, fake, base, post = fake_studio
    connect(base, post)
    project, video = export(service, monkeypatch)
    review = post("inspect", project=project, filename=video.name)
    fake.fail_upload = True
    job = post("upload", review_id=review["review_id"], description="Descripción", approved=True)
    assert job["status"] == "CHECK_STATUS"
    assert job["publish_id"]

    # Both a new review and a restart can occur after the PUT reached TikTok.
    renamed = video.with_name("victoria-copia.mp4")
    renamed.write_bytes(video.read_bytes())
    restarted = tiktok.TikTokStudio(service.root, service.busy)
    monkeypatch.setattr(restarted, "api", service.api)
    monkeypatch.setattr(restarted, "scopes", service.scopes)
    monkeypatch.setattr(server[0].app, "tiktok", restarted)
    connect(base, post)
    before = len(fake.calls)
    review = post("inspect", project=project, filename=renamed.name)
    assert post("upload", review_id=review["review_id"], description="", approved=True) == job
    assert len(fake.calls) == before

    fake.result = "FAILED"
    result = post("status", id=job["id"])
    assert result["status"] == "FAILED"
    assert [entry["message"] for entry in result["errors"]] == [
        "Interrupción simulada.", "duration_check_failed"]
    assert len(fake.calls) == before + 1
    fake.fail_upload = False
    review = post("inspect", project=project, filename=renamed.name)
    retry = post("upload", review_id=review["review_id"], description="", approved=True)
    assert retry["status"] == "PROCESSING_UPLOAD"
    assert retry["id"] != job["id"]
    assert len(fake.calls) == before + 3


@pytest.mark.parametrize("url", [
    "https://open-upload.tiktokapis.com/video/?upload_token=mock%2Btoken&upload_id=123",
    "https://UPLOAD.US.TIKTOKAPIS.COM:443/video/?upload_id=123&upload_token=mock%2Btoken",
    "https://open-upload-i18n.tiktokapis.com/upload?upload_token=mock%2Btoken&upload_id=123",
])
def test_transfer_preserves_signed_url_and_sequential_ranges(monkeypatch, url):
    api = tiktok.TikTokAPI("", "")
    calls = []

    def send(req):
        calls.append(req)
        return (206 if len(calls) == 1 else 201), b""

    monkeypatch.setattr(api, "send", send)
    payload = b"abcdefghijklmn"
    api.upload(url, io.BytesIO(payload), 14, 6, 2)
    assert [(req.get_header("Content-range"), req.data) for req in calls] == [
        ("bytes 0-5/14", b"abcdef"), ("bytes 6-13/14", b"ghijklmn")]
    assert all(req.full_url == url and req.get_header("Authorization") is None for req in calls)


@pytest.mark.parametrize("url,code", [
    (None, "upload_url_missing"),
    ("", "upload_url_missing"),
    ({"url": "signed-secret"}, "upload_url_invalid"),
    ("https://open-upload.tiktokapis.com.evil.example/video/?token=signed-secret", "upload_host_unexpected"),
    ("https://unknown.tiktokapis.com/video/?token=signed-secret", "upload_host_unexpected"),
    ("https://127.0.0.1/video/?token=signed-secret", "upload_host_unexpected"),
    ("http://open-upload.tiktokapis.com/video/", "upload_url_invalid"),
    ("https://u:signed-secret@open-upload.tiktokapis.com/video/", "upload_url_invalid"),
    ("https://open-upload.tiktokapis.com:8080/video/", "upload_url_invalid"),
    ("https://open-upload.tiktokapis.com:bad/video/", "upload_url_invalid"),
    ("https://[invalid/video/", "upload_url_invalid"),
    ("https://open-upload.tiktokapis.com/video/#", "upload_url_invalid"),
    ("https://open-upload.tiktokapis.com/video/?token=%zz", "upload_url_invalid"),
    ("https://open-upload.tiktokapis.com/vid\neo/", "upload_url_invalid"),
    ("https://open-upload.tiktokapis.com\\evil.example/video/", "upload_url_invalid"),
])
def test_invalid_upload_urls_are_rejected_without_revealing_them(monkeypatch, url, code):
    api = tiktok.TikTokAPI("", "")
    monkeypatch.setattr(api, "send", lambda _: pytest.fail("Invalid upload URL reached transport"))
    with pytest.raises(tiktok.TikTokError) as error:
        api.upload(url, io.BytesIO(b"abc"), 3, 3, 1)
    assert error.value.code == code
    assert not any(text in str(error.value) for text in ("signed-secret", "evil.example", "tiktokapis.com"))


@pytest.mark.parametrize("upload_url,code", [
    (None, "upload_url_missing"),
    ("https://unknown.tiktokapis.com/video/?token=private-signed-value", "upload_host_unexpected"),
    ("https://open-upload.tiktokapis.com/video/#private-signed-value", "upload_url_invalid"),
])
def test_init_url_diagnostics_keep_publish_id_and_allow_retry_before_any_put(
    fake_studio, monkeypatch, upload_url, code,
):
    service, fake, base, post = fake_studio
    connect(base, post)
    project, video = export(service, monkeypatch)
    review = post("inspect", project=project, filename=video.name)
    # Treat the identifier as an opaque string and keep it for status lookup.
    fake.init_result = {"publish_id": "v_inbox_file~v2:opaque"}
    if upload_url is not None:
        fake.init_result["upload_url"] = upload_url
    before = len(fake.calls)
    job = post("upload", review_id=review["review_id"], description="", approved=True)
    assert job["publish_id"] == fake.init_result["publish_id"]
    assert job["status"] == "CHECK_STATUS"
    assert job["errors"][-1]["code"] == code
    assert len(fake.calls) == before + 1
    assert all("private-signed-value" not in data for data in (
        json.dumps(job), service._history_path(job["id"]).read_text(encoding="utf-8"),
    ))
    fake.result = "PROCESSING_UPLOAD"
    assert post("status", id=job["id"])["status"] == "PROCESSING_UPLOAD"
    fake.init_result["upload_url"] = "https://open-upload-i18n.tiktokapis.com/upload?upload_token=mock"
    review = post("inspect", project=project, filename=video.name)
    new_job = post("upload", review_id=review["review_id"], description="", approved=True)
    assert new_job["id"] != job["id"]
    assert new_job["status"] == "PROCESSING_UPLOAD"
    assert len(fake.calls) == before + 4


@pytest.mark.parametrize("init_error,publish_id,status", [
    (None, None, "CHECK_INBOX"),
    (None, "a" * 65, "CHECK_INBOX"),
    (None, "invalid\nidentifier", "CHECK_INBOX"),
    (tiktok.TikTokError("Interrupción simulada."), None, "CHECK_INBOX"),
    (tiktok.TikTokError("TikTok rechazó la solicitud (invalid_param).", "invalid_param"),
     None, "INIT_REJECTED"),
])
def test_init_failure_without_publish_id_never_sends_a_put(
    fake_studio, monkeypatch, init_error, publish_id, status,
):
    service, fake, base, post = fake_studio
    connect(base, post)
    project, video = export(service, monkeypatch)
    fake.init_error = init_error
    fake.init_result["publish_id"] = publish_id
    review = post("inspect", project=project, filename=video.name)
    before = len(fake.calls)
    job = post("upload", review_id=review["review_id"], description="", approved=True)
    assert job["status"] == status
    assert "publish_id" not in job
    assert len(fake.calls) == before + 1
    if init_error is None:
        assert job["errors"][-1]["code"] == "publish_id_invalid"
    if status == "CHECK_INBOX":
        review = post("inspect", project=project, filename=video.name)
        assert post("upload", review_id=review["review_id"], description="", approved=True) == job
        assert len(fake.calls) == before + 1
        path = service._history_path(job["id"])
        entry = projects.read_json(path)
        entry["updated"] -= tiktok.UPLOAD_URL_LIFETIME
        projects.atomic_json(path, entry)
    fake.init_error = None
    fake.init_result["publish_id"] = "v_inbox_file~v2.123"
    review = post("inspect", project=project, filename=video.name)
    assert post("upload", review_id=review["review_id"], description="", approved=True)["id"] != job["id"]
    assert len(fake.calls) == before + 3


def test_legacy_upload_rejected_before_put_can_be_retried(fake_studio, monkeypatch):
    service, fake, base, post = fake_studio
    connect(base, post)
    project, video = export(service, monkeypatch)
    service._save({
        "id": "a" * 32, "owner": service.account["key"], "project": project,
        "filename": video.name, "status": "CHECK_STATUS", "publish_id": "v_inbox_file~v2.1",
        "errors": [{"at": time.time(), "message": "TikTok no devolvió una dirección oficial de subida."}],
    })
    fake.init_result["upload_url"] = "https://open-upload-i18n.tiktokapis.com/upload"
    review = post("inspect", project=project, filename=video.name)
    assert post("upload", review_id=review["review_id"], description="", approved=True)["status"] == (
        "PROCESSING_UPLOAD"
    )


@pytest.mark.parametrize("status", [302, 403])
def test_put_errors_do_not_expose_signed_urls_or_provider_echoes(monkeypatch, status):
    api = tiktok.TikTokAPI("", "")
    secret = "mock-secret-echo"
    url = "https://open-upload-i18n.tiktokapis.com/upload?upload_token=" + secret

    def open_request(req, timeout):
        raise HTTPError(req.full_url, status, secret, {},
                        io.BytesIO(json.dumps({"error": {"code": secret}}).encode()))

    monkeypatch.setattr(api.opener, "open", open_request)
    with pytest.raises(tiktok.TikTokError) as error:
        api.upload(url, io.BytesIO(b"abc"), 3, 3, 1)
    assert error.value.code == f"HTTP_{status}"
    assert secret not in str(error.value)


def test_put_does_not_follow_redirects(monkeypatch):
    api = tiktok.TikTokAPI("", "")
    url = "https://open-upload-i18n.tiktokapis.com/upload?upload_token=mock"
    addresses = []

    class RedirectingHTTPS(HTTPSHandler):
        def https_open(self, req):
            addresses.append(req.full_url)
            response = addinfourl(io.BytesIO(), {"Location": "https://evil.example/"}, url, 302)
            response.msg = "Found"
            return response

    monkeypatch.setattr(api, "opener", build_opener(tiktok._NoRedirect(), RedirectingHTTPS()))
    with pytest.raises(tiktok.TikTokError) as error:
        api.upload(url, io.BytesIO(b"abc"), 3, 3, 1)
    assert error.value.code == "HTTP_302"
    assert addresses == [url]


def test_content_api_errors_do_not_expose_unknown_provider_codes(monkeypatch):
    api = tiktok.TikTokAPI("", "")
    monkeypatch.setattr(api, "send", lambda _: (
        200, b'{"data": {}, "error": {"code": "mock-secret-echo"}}',
    ))
    with pytest.raises(tiktok.TikTokError) as error:
        api.authorized(tiktok.INBOX_INIT, "mock-access-token")
    assert error.value.code == "invalid_response"
    assert "mock-secret-echo" not in str(error.value)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required")
def test_read_real_vertical_mp4(tmp_path):
    path = tmp_path / "vertical.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=360x640:r=30",
        "-t", "0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
    ], check=True)
    assert tiktok._inspect_mp4(path) == {
        "width": 360, "height": 640, "duration": 0.5, "vertical_9_16": True,
    }
