"""Controlled Linux tests for Live 1.1. No network services or speech models."""
import json
import shutil
import subprocess
import threading
import time
from functools import partial
from http.server import ThreadingHTTPServer

import pytest

import download_and_transcribe
import live
import live_capture
import live_signals
import projects
from fixtures.make_hls_fixture import make_fixture
from test_dashboard import request, server  # noqa: F401
from test_live_integration import QuietHandler, probe_streams
from test_production import until


def local_server(path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_real_ffmpeg_skips_corrupt_segment_and_transcribes_next(tmp_path, monkeypatch):
    media = make_fixture(tmp_path / "fixture", seconds=6).parent
    buffer = live_capture.SegmentBuffer(tmp_path / "buffer", "http://unused.invalid", {})
    buffer.directory.mkdir()
    bad = buffer.directory / "segment00000000.ts"
    bad.write_bytes(b"not an MPEG transport stream\x00")
    good = buffer.directory / "segment00000001.ts"
    shutil.copyfile(media / "index0.ts", good)
    (buffer.directory / "segment00000002.ts").touch()
    analyzer = live_signals.TranscriptAnalyzer(buffer, live_signals.SignalBus(), model=object())
    monkeypatch.setattr(download_and_transcribe, "_transcribe_media", lambda model, wave, offset: (
        [], [{"word": "victoria", "start": offset + .1, "end": offset + .6}], "es", 2.))
    thread = threading.Thread(target=analyzer.run, args=(buffer._stop,), daemon=True)
    thread.start()
    try:
        status = until(lambda: analyzer.status(8), lambda value: value["processed"] == 1)
        assert status["state"] == "ok" and status["errors"] == status["retries"] == 1
        assert status["last_error"]["segment"] == bad.name
        assert status["last_error"]["exit_code"] != 0 and status["last_error"]["stderr"]
        assert analyzer.snapshot()[1][0]["start"] == pytest.approx(4.1)
    finally:
        buffer._stop.set()
        thread.join(3)


def test_closed_csv_snapshot_is_pinned_until_export_releases(tmp_path):
    buffer = live_capture.SegmentBuffer(tmp_path, "http://unused.invalid", {"live_buffer": 1})
    buffer.process = type("Process", (), {"poll": lambda self: None})()
    buffer._check_segment = lambda path: None
    first = tmp_path / "segment00000000.ts"
    second = tmp_path / "segment00000001.ts"
    first.touch()
    second.touch()
    (tmp_path / "segments.csv").write_text(f"{first.name},1.25,5.25\n{second.name},5.25", encoding="utf-8")
    assert buffer.closed_ranges() == {first: (1.25, 5.25)}
    with buffer.pin([first]) as paths:
        assert paths == {first: (1.25, 5.25)}
        buffer.retire()
        assert first.exists() and not second.exists()
        assert buffer.closed_ranges() == {}
    assert not first.exists()


def test_windows_dword_and_timeout_have_safe_diagnostics(monkeypatch):
    for error, expected in [
        (subprocess.CalledProcessError(4294967294, ["ffmpeg"], stderr=b"Input error https://server/private"),
         {"exit_code": -2, "stderr": "Input error [URL]"}),
        (subprocess.TimeoutExpired(["ffmpeg"], 12, stderr=b"Authorization: bearer SECRET"),
         {"timeout_seconds": 12, "stderr": ""}),
    ]:
        def fail(*args, **kwargs):
            raise error
        monkeypatch.setattr(live_capture.subprocess, "run", fail)
        with pytest.raises(live_capture.SegmentError) as result:
            live_capture.decode_segment(["ffmpeg"], "transcript")
        assert all(result.value.detail[key] == value for key, value in expected.items())


def test_reconnect_and_manual_export_via_http_reach_automation(server, tmp_path, monkeypatch):
    first = make_fixture(tmp_path / "first", seconds=10).parent
    second = make_fixture(tmp_path / "second", seconds=14).parent
    httpd, local = local_server(tmp_path)
    web, base = server
    app = web.app
    watched = "https://www.youtube.com/watch?v=edition"
    def source(url, options, runner=None):
        leaf = second.name if app.live_sessions else first.name
        return live_capture.LiveSource(f"{local}/{leaf}/index.m3u8", "hls", "youtube", "fixture", [], watched)

    monkeypatch.setattr(live_capture, "resolve_live_source", source)
    monkeypatch.setattr(live_signals.HypeDetector, "evaluate", lambda *args, **kwargs: {"score": 0, "signals": {}})
    monkeypatch.setattr(download_and_transcribe, "_load_model", lambda *args: object())
    monkeypatch.setattr(download_and_transcribe, "_transcribe_media", lambda model, media, offset: (
        [], [{"word": "victoria", "start": offset + .3, "end": offset + .9}], "es", 2.))
    try:
        ident = request(base, "/api/live", {"source": watched, "options": {"aspect": "9:16"}}, app.token)["id"]
        session = app.live_sessions[ident]
        session._restart_delays = (.1,)
        first_buffer = until(lambda: session.buffer, bool)
        state = lambda: request(base, f"/api/live/{ident}")
        before = until(state, lambda data: data["health"].get("analyzers", {}).get("transcript", {}).get("processed", 0) > 0)
        assert before["threshold"] == 6
        old_bus = session.bus
        until(state, lambda data: data["health"]["restarts"] > 0 and
              data["health"].get("capture", {}).get("available", 0) > 0
              and (session.buffer.directory.name == "capture-000001"), 30)
        assert session.bus is not old_bus
        assert not first_buffer.alive
        assert session.buffer.directory != first_buffer.directory
        request(base, f"/api/live/{ident}/export", {"seconds": 90}, app.token)
        after = until(state, lambda data: data["export"]["completed"] == 1, 25)
        clip = after["triggers"][-1]
        assert clip["manual"] and clip["score"] is None and clip["start"] >= before["stream_time"]
        media = live.Path(clip["files"]["mp4"])
        tracks = probe_streams(media)
        assert set(tracks) == {"video", "audio"}
        assert "victoria" in media.with_suffix(".srt").read_text(encoding="utf-8")
        assert media.is_file()
        job = until(lambda: request(base, "/api/production/status"),
                    lambda data: any(row["filename"] == media.name and row["state"] == "review"
                                     for row in data["jobs"]), 30)
        row = next(row for row in job["jobs"] if row["filename"] == media.name)
        assert row["detection"] is None and row["editorial_id"] is None
        assert "victoria" in media.with_suffix(".srt").read_text(encoding="utf-8")
    finally:
        for session in list(app.live_sessions.values()):
            session.stop()
        httpd.shutdown()
        httpd.server_close()
