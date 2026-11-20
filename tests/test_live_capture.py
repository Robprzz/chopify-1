import io
import json
import os
import subprocess
import sys
import time

import pytest

import live_capture


def test_resolve_direct_hls():
    source = live_capture.resolve_live_source(" https://x/live.m3u8 ", {})
    assert source.kind == "hls"
    assert source.url == "https://x/live.m3u8"
    assert source.resolved_by == "passthrough"


def test_resolve_direct_rtmp():
    source = live_capture.resolve_live_source("rtmp://x/live", {})
    assert source.kind == "rtmp"
    assert source.platform == "generic"


@pytest.mark.parametrize("page, platform", [
    ("https://www.twitch.tv/foo", "twitch"),
    ("https://www.youtube.com/watch?v=2dt0yxm4RUE", "youtube"),
    ("https://kick.com/foo", "kick"),
])
def test_resolve_page_uses_ytdlp(monkeypatch, page, platform):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            stdout = json.dumps({"is_live": True, "protocol": "hls",
                                 "url": "https://video/live.m3u8",
                                 "http_headers": {"User-Agent": "provider", "Referer": page}})
        return Result()

    monkeypatch.setattr(live_capture.ingest, "_run", fake_run)
    source = live_capture.resolve_live_source(page, {"live_headers": ["user-agent: configured"]})
    assert source.kind == "hls"
    assert source.platform == platform
    assert source.resolved_by == "yt-dlp"
    assert source.url == "https://video/live.m3u8"
    assert calls[0][:3] == [sys.executable, "-m", "yt_dlp"]
    assert source.headers == ["user-agent: configured", f"Referer: {page}"]


def test_resolve_rejects_finished_broadcast():
    result = subprocess.CompletedProcess([], 0, json.dumps({
        "live_status": "was_live", "url": "https://example.com/archive.mp4"}))
    with pytest.raises(ValueError, match="not currently live"):
        live_capture.resolve_live_source("https://youtu.be/123", {}, runner=lambda *a, **k: result)


@pytest.mark.parametrize("height, expected", [(720, "311"), (1080, "312")])
def test_ytdlp_selects_independent_hls_at_requested_height(tmp_path, height, expected):
    pytest.importorskip("yt_dlp")
    formats = [
        {"format_id": ident, "url": f"https://example.invalid/{ident}.m3u8", "protocol": "m3u8_native",
         "height": pixels, "vcodec": video, "acodec": audio, "ext": ext}
        for ident, pixels, video, audio, ext in [
            ("234", None, "none", "mp4a.40.2", "m4a"),
            ("311", 720, "avc1", "none", "mp4"),
            ("312", 1080, "avc1", "none", "mp4"),
        ]
    ]
    info = tmp_path / "formats.json"
    info.write_text(json.dumps({"id": "fixture", "title": "Live fixture", "is_live": True,
                                "extractor": "youtube", "formats": formats}))
    command = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--simulate", "--dump-single-json",
               "--load-info-json", str(info), "-f"]
    rejected = subprocess.run([*command, "best[protocol^=m3u8]/best"], capture_output=True, text=True, timeout=15)
    assert rejected.returncode and "Requested format is not available" in rejected.stderr
    selected = subprocess.check_output([*command, live_capture._format_selector(height)], timeout=15)
    assert [track["format_id"] for track in json.loads(selected)["requested_formats"]] == [expected, "234"]


def test_unsynchronized_segments_are_not_exposed(tmp_path):
    def inspect(*args, **kwargs):
        return subprocess.CompletedProcess([], 0, json.dumps({"streams": [
            {"codec_type": "video", "height": 720, "start_time": 0, "duration": 4},
            {"codec_type": "audio", "start_time": 2, "duration": 2},
        ]}))

    buffer = live_capture.SegmentBuffer(tmp_path, "https://x/v", {"live_max_height": 720},
                                         runner=inspect, audio_url="https://x/a")
    for index in range(4):
        (tmp_path / f"segment{index:08d}.ts").touch()
    assert buffer.ready_segments() == []
    assert buffer.segment_problem == live_capture.SYNC_ERROR


def test_resolve_rejects_non_url():
    with pytest.raises(ValueError, match="http"):
        live_capture.resolve_live_source("not a url", {})


def test_segment_window_and_retention(tmp_path):
    buffer = live_capture.SegmentBuffer(
        tmp_path, "https://x/live.m3u8",
        {"live_buffer": 300, "live_buffer_max_mb": 512, "live_headers": []},
        runner=lambda cmd, **kw: None)
    for i in range(5):
        (tmp_path / f"segment{i:08d}.ts").write_bytes(b"x" * 100)
    assert [p.name for p in buffer.segments()] == [
        f"segment{i:08d}.ts" for i in range(5)]
    assert buffer.time_to_index(9.9) == 2
    assert buffer.index_to_time(2) == 8.0
    selected = buffer.window(4.0, 12.0)
    assert [p.name for p in selected] == ["segment00000001.ts", "segment00000002.ts"]
    old = tmp_path / "segment00000000.ts"
    old.touch()
    os.utime(old, (time.time() - 400, time.time() - 400))
    buffer.cleanup()
    assert not old.exists()


def test_segment_buffer_cap(tmp_path):
    buffer = live_capture.SegmentBuffer(
        tmp_path, "https://x/live.m3u8",
        {"live_buffer": 300, "live_buffer_max_mb": 1, "live_headers": []},
        runner=lambda cmd, **kw: None)
    for i in range(12):
        (tmp_path / f"segment{i:08d}.ts").write_bytes(b"x" * 100_000)
    buffer.cleanup()
    assert len(buffer.segments()) < 12


def test_capture_logging_redacts_ffmpeg_urls_and_headers(tmp_path, monkeypatch):
    captured = {}

    class FakeProcess:
        stderr = io.BytesIO(b"HTTP error 403 for https://cdn.example/live?signature=secret\nAuthorization: bearer secret")

        def poll(self):
            return 0

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(live_capture.subprocess, "Popen", fake_popen)
    buffer = live_capture.SegmentBuffer(
        tmp_path, "https://x/live.m3u8", {"live_headers": []})
    buffer.start()
    buffer.stop()
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.PIPE
    assert (tmp_path / "ffmpeg.log").read_text() == live_capture.NETWORK_ERROR + "\n"


def test_resolve_split_formats_refreshes_both_inputs_and_keeps_distinct_headers(tmp_path):
    calls = []

    def extract(cmd, **kwargs):
        calls.append(cmd)
        n = len(calls)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({
            "is_live": True,
            "http_headers": {"Authorization": "should-not-cross-origins"},
            "requested_formats": [
                {"vcodec": "none", "acodec": "aac", "url": f"https://audio.example/{n}.m3u8",
                 "http_headers": {"Referer": "https://audio.example", "X-Bad": "one\r\ntwo"}},
                {"vcodec": "avc1", "acodec": "none", "url": f"https://video.example/{n}.m3u8",
                 "http_headers": {"Referer": "https://video.example"}},
            ],
        }))

    page = "https://www.youtube.com/live/2dt0yxm4RUE"
    selected = live_capture.resolve_live_source(page, {}, runner=extract)
    assert selected.url.endswith("/1.m3u8") and selected.audio_url.endswith("/1.m3u8")
    assert "[height<=720]" in calls[0][calls[0].index("-f") + 1]
    options = {"live_max_height": 1080, "live_headers": ["X-Common: configured"]}
    selected = selected.refresh(options, runner=extract)
    assert selected.url.endswith("/2.m3u8") and selected.audio_url.endswith("/2.m3u8")
    assert "[height<=1080]" in calls[1][calls[1].index("-f") + 1]
    capture = live_capture.SegmentBuffer(tmp_path, selected.url, options, headers=selected.headers,
                                         audio_url=selected.audio_url, audio_headers=selected.audio_headers)
    cmd = capture.ffmpeg_command()
    input_idx = cmd.index("-i")
    assert cmd[cmd.index("-headers"):input_idx] == [
        "-headers", "X-Common: configured\r\nReferer: https://video.example\r\n"]
    audio_idx = cmd.index("-i", input_idx + 1)
    assert cmd[cmd.index("-headers", input_idx):audio_idx] == [
        "-headers", "X-Common: configured\r\nReferer: https://audio.example\r\n"]
    assert cmd[audio_idx + 2:audio_idx + 6] == ["-map", "0:v:0", "-map", "1:a:0"]
    assert cmd.count("-reconnect") == 2


@pytest.mark.parametrize("stderr,message", [
    ("ERROR: Requested format is not available", "No playable YouTube audio and video formats"),
    ("WARNING: No supported JavaScript runtime could be found.", "Install Deno"),
    ("ERROR: Connection reset", "Could not connect"),
])
def test_resolution_errors_are_specific_and_safe(stderr, message):
    def fail(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr=stderr + " https://x/?signed-secret")

    with pytest.raises(ValueError, match=message) as exc:
        live_capture.resolve_live_source("https://youtu.be/123", {}, runner=fail)
    assert "signed-secret" not in str(exc.value)


def test_diagnostic_preserves_success_with_js_warning():
    result = subprocess.CompletedProcess([], 0, json.dumps({"url": "https://cdn/live.m3u8"}),
                                        "WARNING: No supported JavaScript runtime could be found.")
    source = live_capture.resolve_live_source("https://youtu.be/123", {}, runner=lambda *a, **k: result)
    assert source.warning == live_capture.JS_WARNING


@pytest.mark.parametrize("audio_url", ["file:///etc/passwd", "https://user:pass@example.com/a"])
def test_split_source_rejects_invalid_audio_url(audio_url):
    result = subprocess.CompletedProcess([], 0, json.dumps({"requested_formats": [
        {"vcodec": "avc1", "acodec": "none", "url": "https://example.com/v"},
        {"vcodec": "none", "acodec": "aac", "url": audio_url},
    ]}))
    with pytest.raises(ValueError, match="valid HTTP media URL"):
        live_capture.resolve_live_source("https://youtu.be/123", {}, runner=lambda *a, **k: result)


def test_ready_segments_excludes_newest(tmp_path):
    buffer = live_capture.SegmentBuffer(
        tmp_path, "https://x/live.m3u8", {"live_headers": []})
    for i in range(3):
        (tmp_path / f"segment{i:08d}.ts").write_bytes(b"x")
    assert [p.name for p in buffer.ready_segments()] == [
        "segment00000000.ts", "segment00000001.ts"]


def test_live_source_refresh(monkeypatch):
    calls = []

    class Result:
        stdout = json.dumps({"url": "https://cdn/new.m3u8", "protocol": "hls"})

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Result()

    monkeypatch.setattr(live_capture.ingest, "_run", fake_run)
    source = live_capture.resolve_live_source(
        "https://twitch.tv/channel", {}, runner=fake_run)
    assert source.page_url == "https://twitch.tv/channel"
    refreshed = source.refresh({}, runner=fake_run)
    assert refreshed.url == "https://cdn/new.m3u8"
    assert refreshed.page_url == "https://twitch.tv/channel"
    assert calls


def test_direct_source_refresh_is_noop():
    source = live_capture.resolve_live_source(
        "https://cdn/live.m3u8", {})
    assert source.refresh({}) is source
