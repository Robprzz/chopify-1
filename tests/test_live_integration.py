"""Offline integration test: a local HLS fixture drives the live pipeline."""
import json
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


def probe_streams(path):
    result = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries",
        "stream=codec_type,start_time,duration,avg_frame_rate", "-of", "json", str(path),
    ])
    return {stream["codec_type"]: stream for stream in json.loads(result)["streams"]}


def marker_skew(path):
    from fractions import Fraction

    import numpy as np

    tracks = probe_streams(path)
    video = subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(path), "-an", "-vf", "scale=8:8,format=gray",
        "-threads", "1", "-f", "rawvideo", "-",
    ])
    audio = subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "48000", "-f", "s16le", "-",
    ])
    flash = np.flatnonzero(np.frombuffer(video, dtype=np.uint8).reshape(-1, 64).mean(axis=1) > 110)[0]
    tone = np.flatnonzero(np.abs(np.frombuffer(audio, dtype="<i2")) > 10000)[0]
    visual = float(tracks["video"]["start_time"]) + flash / float(Fraction(tracks["video"]["avg_frame_rate"]))
    audible = float(tracks["audio"]["start_time"]) + tone / 48000
    return abs(visual - audible)


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
    page.get_by_role("button", name="Live Studio").click()
    page.locator("#live-source").fill("https://www.youtube.com/watch?v=2dt0yxm4RUE")
    with page.expect_response(lambda response: response.request.method == "POST"
                              and response.url == base + "/api/live") as reply:
        page.locator("#live-start").click()
    assert reply.value.status == 400
    expect(page.locator("#live-start-error")).to_contain_text("yt-dlp is missing from Studio's Python environment")
    expect(page.locator("#live-status")).to_have_text("Sin captura activa.")


def test_live_capture_downscales_when_source_exceeds_selected_limit(tmp_path):
    import live
    from fixtures.make_hls_fixture import make_fixture
    from live_capture import resolve_live_source

    manifest = make_fixture(tmp_path / "stream", seconds=6, size="480x736")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(manifest.parent)))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    options = {"live_max_height": 720, "live_no_chat": True, "live_no_transcript": True, "live_threshold": 10}
    source = resolve_live_source(f"http://127.0.0.1:{httpd.server_port}/index.m3u8", options)
    session = live.LiveSession(source, options, tmp_path)
    worker = threading.Thread(target=session.start, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            buffer = session.buffer
            if buffer and buffer.scale_capture and buffer.ready_segments():
                assert buffer.probe()["height"] == 720
                assert buffer.probe()["has_audio"]
                break
            time.sleep(0.2)
        else:
            pytest.fail(f"No downscaled capture: {session.health['warnings']}, {session.errors}")
    finally:
        session.stop()
        worker.join(timeout=8)
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("split", [False, True], ids=["combined", "independent"])
def test_live_clip_reaches_publications_in_chromium(server, chromium_page, monkeypatch, tmp_path, split):
    import download_and_transcribe
    import live_capture
    import live_signals
    import projects
    from fixtures.make_hls_fixture import make_fixture, make_split_fixture

    # Real capture, render, media and preview. Replace only the trigger and speech inference.
    monkeypatch.setattr(live_signals.HypeDetector, "evaluate",
                        lambda *args, **kwargs: {"score": 9, "signals": {"audio": 9}})
    monkeypatch.setattr(download_and_transcribe, "_load_model", lambda *args: object())
    monkeypatch.setattr(download_and_transcribe, "_transcribe_media", lambda model, media, offset: (
        [], [{"word": "victoria", "start": offset + 0.2, "end": offset + 0.8}], None, None,
    ))
    stream = tmp_path / "stream"
    tracks_seen = set()

    class FixtureHandler(QuietHandler):
        def do_GET(self):
            if split:
                track = self.path.split("/")[1]
                if self.headers.get("X-Live-Track") != track:
                    return self.send_error(403)
                tracks_seen.add(track)
            return super().do_GET()

    if split:
        make_split_fixture(stream)
        first_video = probe_streams(stream / "video" / "index0.ts")["video"]
        first_audio = probe_streams(stream / "audio" / "index0.ts")["audio"]
        assert float(first_video["start_time"]) - float(first_audio["start_time"]) == pytest.approx(0.16, abs=0.005)
    else:
        make_fixture(stream, seconds=14)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(FixtureHandler, directory=str(stream)))
    media_base = f"http://127.0.0.1:{httpd.server_port}"
    url = media_base + "/index.m3u8"
    if split:
        real_runner = live_capture.ingest._run
        url = "https://www.youtube.com/live/2dt0yxm4RUE"

        def extract_or_run(cmd, **kwargs):
            if cmd[1:3] != ["-m", "yt_dlp"]:
                return real_runner(cmd, **kwargs)
            assert "[height<=1080]" in cmd[cmd.index("-f") + 1]
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"is_live": True, "requested_formats": [
                {"url": media_base + "/video/index.m3u8", "protocol": "m3u8_native", "vcodec": "avc1.4D4020",
                 "format_id": "311",
                 "acodec": "none", "http_headers": {"X-Live-Track": "video"}},
                {"url": media_base + "/audio/index.m3u8", "protocol": "m3u8_native", "vcodec": "none",
                 "format_id": "234", "http_headers": {"X-Live-Track": "audio"}},
            ]}))

        monkeypatch.setattr(live_capture.ingest, "_run", extract_or_run)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    app, base = server
    start_live = app.app.start_live
    monkeypatch.setattr(app.app, "start_live", lambda source, options, **kwargs:
                        start_live(source, {**options, "live_duration": "legacy", "live_max_clips": 1}, **kwargs))
    page, expect = chromium_page
    if split:
        import base64
        import cv2
        import numpy as np
        import watermark

        pixels = np.full((20, 40, 4), (0, 255, 0, 255), dtype=np.uint8)
        ok, png = cv2.imencode(".png", pixels)
        assert ok
        watermark.Store(app.app.root).save(
            {**watermark.DEFAULTS, "enabled": True, "position": "top-left"},
            base64.b64encode(png).decode("ascii"))
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(base)
        page.get_by_role("button", name="Live Studio").click()
        page.locator("#live-source").fill(url)
        page.locator("#live-aspect").select_option("1:1" if split else "9:16")
        expect(page.locator("#live-max-height")).to_have_value("720")
        if split:
            page.locator("#live-advanced summary").click()
            page.locator("#live-max-height").select_option("1080")
        with page.expect_response(lambda response: response.request.method == "POST"
                                  and response.url == base + "/api/live") as reply:
            page.locator("#live-start").click()
        assert reply.value.status == 201
        session = reply.value.json()["id"]
        if split:
            deadline = time.monotonic() + 15
            captured_skew = None
            while captured_skew is None and time.monotonic() < deadline:
                buffer = app.app.live_sessions[session].buffer
                if buffer:
                    with buffer.pin() as captured:
                        if captured:
                            assert all(set(probe_streams(segment)) == {"video", "audio"}
                                       for segment in captured)
                            captured_skew = marker_skew(next(iter(captured)))
                if captured_skew is None:
                    time.sleep(0.1)
            assert captured_skew is not None and captured_skew < 0.075
        assert page.request.get(base + "/api/projects").json() == {"projects": []}
        assert not list(app.app.root.glob("**/transcript.json"))
        link = page.locator("#live-clips a").first
        expect(link).to_be_visible(timeout=25000)
        expect(page.locator("#live-components")).to_contain_text("Transcripción")
        expect(page.locator("#live-score")).to_contain_text("Umbral 6")
        mp4 = link.get_attribute("href")
        assert mp4.startswith(f"/media/live/{session}/clips/")
        response = page.request.get(base + mp4, headers={"Range": "bytes=0-31"})
        assert response.status == 206
        assert response.headers["content-type"] == "video/mp4"
        srt = page.request.get(base + mp4.removesuffix(".mp4") + ".srt")
        assert srt.ok and "victoria" in srt.text()
        if split:
            active = app.app.live_sessions[session]
            time_before_navigation = active.stream_time
            live_writes = []
            page.on("request", lambda req: live_writes.append(req.url)
                    if req.method == "POST" and req.url.startswith(base + "/api/live") else None)
            with page.expect_popup() as opened:
                link.click()
            player = opened.value
            expect(player).to_have_url(base + mp4)
            player.wait_for_function("() => document.querySelector('video')?.readyState >= 2", timeout=10000)
            player.close()
            expect(page).to_have_url(base + "/#live")
            page.reload()
            expect(page.locator("#live-sessions button")).to_have_attribute("aria-pressed", "true")
            expect(link).to_have_attribute("href", mp4)
            expect(page.locator("#live-stop")).to_be_visible()
            assert app.app.live_sessions == {session: active}
            assert active.stream_time >= time_before_navigation
            assert live_writes == []
            page.locator("#show-publications").click()
            page.locator("#calendar-tiktok").click()
            page.get_by_role("button", name="Live Studio").click()
            expect(link).to_have_attribute("href", mp4)
            page.evaluate("() => localStorage.setItem('chopify.live.selected', 'expired')")
            page.reload()
            expect(link).to_have_attribute("href", mp4)
            assert page.evaluate("() => localStorage.getItem('chopify.live.selected')") == session
            historical = "0123456789ab"
            manifest = app.app.root / "live" / historical / "live_session.json"
            manifest.parent.mkdir()
            projects.atomic_json(manifest, {"id": historical, "status": "stopped", "stream_time": 60,
                                            "started_at": 123, "source": {"label": "Directo previo"},
                                            "errors": ["Error guardado"], "recent_scores": [8.2], "triggers": []})
            previous = page.get_by_role("button", name=f"Directo previo · {historical}")
            previous.click()
            expect(page.locator("#live-error")).to_have_text("Error guardado")
            expect(page.locator("#live-score")).to_contain_text("8.20")
            expect(page.locator("#live-stop")).to_be_hidden()
            page.reload()
            expect(previous).to_have_attribute("aria-pressed", "true")
            page.locator(f'[data-live-session="{session}"]').click()
            expect(link).to_have_attribute("href", mp4)
        exports = app.app.root / "live" / session / "clips"
        (exports / "partial.mp4").write_bytes(b"unfinished")
        page.locator("#show-publications").click()
        page.locator("#calendar-tiktok").click()
        select = page.locator("#tiktok-clip")
        expect(select.locator("option")).to_have_count(2)
        select.select_option("0")
        expect(page.locator("#tiktok-media-info")).to_contain_text(
            "1080 × 1080" if split else "1080 × 1920", timeout=10000)
        expect(page.locator("#tiktok-preview")).to_have_attribute("src", mp4)
        saved = projects.read_json(app.app.root / "live" / session / "live_session.json")
        assert saved["triggers"][0]["files"]["mp4"].endswith(mp4.rsplit("/", 1)[-1])
        if split:
            assert tracks_seen == {"video", "audio"}
            clip = exports / mp4.rsplit("/", 1)[-1]
            color = subprocess.check_output([
                "ffmpeg", "-v", "error", "-i", str(clip), "-vf", "crop=4:4:60:50,format=rgb24",
                "-frames:v", "1", "-threads", "1", "-f", "rawvideo", "-",
            ], timeout=15)
            assert color[1] > 200 and color[0] < 20 and color[2] < 20
            assert set(probe_streams(clip)) == {"video", "audio"}
            assert marker_skew(clip) < 0.075
            page.wait_for_function(
                """async clip => {
                    const response = await fetch('/api/editorial/status?clip=' + encodeURIComponent(clip));
                    const data = await response.json();
                    return data.jobs.some(job => job.state === 'done' && job.provider === 'rules');
                }""", arg=f"live/{session}/{clip.name}", timeout=10000,
            )
            page.request.post(base + f"/api/live/{session}/stop",
                              headers={"X-Chopify-Token": app.app.token}, data={})
            assert not buffer.alive
            page.get_by_role("button", name="Live Studio").click()
            expect(page.locator("#live-stop")).to_be_hidden()
            expect(page.locator("#live-sessions")).to_contain_text("Finalizada")
            page.reload()
            with page.expect_popup() as opened:
                page.locator("#live-clips a").first.click()
            expect(opened.value).to_have_url(base + mp4)
            opened.value.close()
        assert not errors
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
