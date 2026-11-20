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


def test_start_does_not_pipe_ffmpeg_stderr(tmp_path, monkeypatch):
    captured = {}

    class FakeProcess:
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
    assert captured["stderr"] is not subprocess.PIPE
    assert hasattr(captured["stderr"], "write")
    assert (tmp_path / "ffmpeg.log").exists()


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
