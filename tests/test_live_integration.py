"""Offline integration test: a local HLS fixture drives the live pipeline."""
import shutil
import subprocess
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from test_dashboard import server  # noqa: F401
from test_studio_navigation import chromium_page  # noqa: F401

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


def test_live_pipeline_against_local_hls(tmp_path):
    import live
    import projects
    from fixtures.make_hls_fixture import make_fixture
    from live_capture import LiveSource

    served = make_fixture(tmp_path / "stream", seconds=20)
    handler = partial(QuietHandler, directory=str(served.parent))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_port}/index.m3u8"

    options = projects.validate_options({
        "live_buffer": 60, "live_threshold": 0.5, "live_pre_roll": 5,
        "live_tail": 2, "live_max_clips": 1, "live_no_chat": True,
        "live_no_transcript": True})
    source = LiveSource(url, "hls", "generic", "passthrough", [])
    session = live.LiveSession(source, options, tmp_path)
    thread = threading.Thread(target=session.start, daemon=True)
    thread.start()
    deadline = time.time() + 25
    while time.time() < deadline and not session.records:
        time.sleep(0.5)
    session.stop()
    thread.join(timeout=5)
    httpd.shutdown()
    httpd.server_close()

    data = projects.read_json(session.dir / "live_session.json")
    assert data["health"]["state"] in {"ok", "degraded"}
    assert set(data["health"]) >= {"state", "ffmpeg_alive", "last_segment_age",
                                   "restarts", "refreshes", "warnings"}
    assert (session.dir / "clips").exists()
    assert list((session.dir / "clips").glob("*.mp4")), "no clip was rendered"


def test_live_start_explains_missing_resolver(server, chromium_page, monkeypatch):
    import live_capture

    def fail_command(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="No module named yt_dlp")

    monkeypatch.setattr(live_capture.ingest, "_run", fail_command)
    _, base = server
    page, expect = chromium_page
    page.goto(base)
    page.get_by_role("button", name="Live clipping").click()
    page.locator("#live-source").fill("https://www.youtube.com/watch?v=2dt0yxm4RUE")
    with page.expect_response(lambda response: response.request.method == "POST"
                              and response.url == base + "/api/live") as reply:
        page.get_by_role("button", name="Start live session").click()
    assert reply.value.status == 400
    expect(page.locator("#live-start-error")).to_contain_text("yt-dlp is missing from Studio's Python environment")
    expect(page.locator("#live-status")).to_have_text("Not running.")


def test_live_clip_reaches_publications_in_chromium(server, chromium_page, monkeypatch, tmp_path):
    import download_and_transcribe
    import live_signals
    import projects
    from fixtures.make_hls_fixture import make_fixture

    # Real capture, render, media and preview. Replace only the trigger and speech inference.
    monkeypatch.setattr(live_signals.HypeDetector, "evaluate",
                        lambda *args: {"score": 9, "signals": {}})
    monkeypatch.setattr(download_and_transcribe, "_load_model", lambda *args: object())
    monkeypatch.setattr(download_and_transcribe, "_transcribe_media", lambda model, media, offset: (
        [], [{"word": "victoria", "start": offset + 0.2, "end": offset + 0.8}], None, None,
    ))
    manifest = make_fixture(tmp_path / "stream", seconds=14)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(manifest.parent)))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    app, base = server
    page, expect = chromium_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(base)
        page.get_by_role("button", name="Live clipping").click()
        page.locator("#live-source").fill(f"http://127.0.0.1:{httpd.server_port}/index.m3u8")
        with page.expect_response(lambda response: response.request.method == "POST"
                                  and response.url == base + "/api/live") as reply:
            page.get_by_role("button", name="Start live session").click()
        assert reply.value.status == 201
        session = reply.value.json()["id"]
        assert page.request.get(base + "/api/projects").json() == {"projects": []}
        assert not list(app.app.root.glob("**/transcript.json"))
        link = page.locator("#live-clips a").first
        expect(link).to_be_visible(timeout=25000)
        mp4 = link.get_attribute("href")
        assert mp4.startswith(f"/media/live/{session}/clips/")
        response = page.request.get(base + mp4, headers={"Range": "bytes=0-31"})
        assert response.status == 206
        assert response.headers["content-type"] == "video/mp4"
        srt = page.request.get(base + mp4.removesuffix(".mp4") + ".srt")
        assert srt.ok and "victoria" in srt.text()
        exports = app.app.root / "live" / session / "clips"
        (exports / "partial.mp4").write_bytes(b"unfinished")
        page.get_by_role("button", name="Publicaciones").click()
        select = page.locator("#tiktok-clip")
        expect(select.locator("option")).to_have_count(2)
        select.select_option("0")
        expect(page.locator("#tiktok-media-info")).to_contain_text("1080 × 1920", timeout=10000)
        expect(page.locator("#tiktok-preview")).to_have_attribute("src", mp4)
        saved = projects.read_json(app.app.root / "live" / session / "live_session.json")
        assert saved["triggers"][0]["files"]["mp4"].endswith(mp4.rsplit("/", 1)[-1])
        assert not errors
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
