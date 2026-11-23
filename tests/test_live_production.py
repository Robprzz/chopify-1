"""Live 1.2 durable queue and real HLS/FFmpeg acceptance tests."""
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import threading
import time

import pytest

import live_capture
import live_clips
import live_production as lp
import live_signals
import projects
import subtitle_quality
from test_dashboard import running_server
from test_live_recovery import local_server
from test_production import until


def sentence(end):
    words = [{"word": "historia", "start": n / 100, "end": (n + 42) / 100}
             for n in range(40, round(end * 100) - 70, 45)]
    words.append({"word": "terminó.", "start": end - .28, "end": end})
    return words


def test_context_can_extend_past_the_initial_range_but_not_fill_silence():
    words = sentence(119.9)
    assert lp.select_window(words, [(.4, 120)], 158, 48, "60-90") == (.16, 120.15)
    assert lp.select_window([], [], 185, 48, "60-90") == (18., 78.)
    with pytest.raises(lp.Rejected, match="límites"):
        lp.select_window(words, [(.4, 120)], 158, 48, "30-60")
    with pytest.raises(lp.Rejected, match="Whisper"):
        lp.select_window([], [(4., 50.)], 140, 48, "60-90")
    unfinished = [{"word": "y", "start": .4, "end": 74.9}]
    with pytest.raises(lp.Rejected, match="límites"):
        lp.select_window(unfinished, [(.4, 75)], 135, 48, "60-90")


def test_duration_can_follow_unpunctuated_span_after_a_pause():
    words = sentence(75.)
    words[-1]["word"] = "terminó"
    assert lp.select_window(words, [(.4, 75)], 100, 42, "60-90") == (.16, 75.25)
    with pytest.raises(lp.Rejected, match="límites"):
        lp.select_window(words, [(.4, 75)], 75, 42, "60-90")


def test_queue_bounds_explicit_limits_and_more_than_ten(tmp_path):
    pipeline = lp.Pipeline(tmp_path)
    opts = projects.validate_options({"live_max_per_hour": 0})
    session = "abcdef123456"
    trigger = lambda time: live_clips.Trigger(time, 8., {"audio": 8.})
    for index in range(12):
        ident, err = pipeline.reserve(session, trigger(index * 120 + 1), opts)
        assert ident and not err
        pipeline.seal(ident)
    assert pipeline.status(session)["counts"] == {"rejected": 12}
    _, duplicate = pipeline.reserve(session, trigger(11 * 120 + 12), opts)
    assert "solapado" in duplicate
    for index in range(lp.MAX_PENDING):
        assert pipeline.reserve(session, trigger((12 + index) * 120 + 1), opts)[0]
    assert "llena" in pipeline.reserve(session, trigger(9999), opts)[1]
    assert "sesión" in pipeline.reserve(session, trigger(9999), {**opts, "live_max_clips": 1})[1]
    assert "hora" in pipeline.reserve(session, trigger(9999), {**opts, "live_max_per_hour": 1})[1]


def test_more_than_ten_completed_jobs_in_one_session(tmp_path, monkeypatch):
    pipeline = lp.Pipeline(tmp_path)
    session = "abcdef123456"
    opts = projects.validate_options({"live_duration": "30-60", "live_max_per_hour": 0})
    monkeypatch.setattr(pipeline, "_produce", lambda job: job["id"] + "-clip.mp4")
    for index in range(12):
        start = index * 75
        ident, error = pipeline.reserve(session, live_clips.Trigger(start + 24, 8, {"audio": 8}), opts)
        assert ident and not error
        with pipeline.connect(write=True) as conn:
            conn.execute("UPDATE jobs SET media=? WHERE id=?",
                         (lp.json.dumps([["segment00000000.ts", start, start + 60, 8]]), ident))
        pipeline.seal(ident)
        pipeline._execute(pipeline._claim())
    assert pipeline.status(session)["counts"] == {"done": 12}


def test_generation_gap_and_stale_lease(tmp_path):
    class Buffer:
        @contextmanager
        def pin(self, ranges):
            yield ranges
    session = "abcdef123456"
    pipeline = lp.Pipeline(tmp_path)
    opts = projects.validate_options({"live_duration": "30-60"})
    ident, _ = pipeline.reserve(session, live_clips.Trigger(24., 8., {"audio": 8.}), opts)
    first = tmp_path / "segment00000000.ts"
    later = tmp_path / "segment00000001.ts"
    first.write_bytes(b"source")
    later.write_bytes(b"other generation")
    pipeline.preserve(session, ident, Buffer(), {first: (0., 40.)}, 0.)
    with pytest.raises(lp.Rejected, match="discontinuidad"):
        pipeline.preserve(session, ident, Buffer(), {later: (0., 4.)}, 44.)
    first.unlink()
    assert (pipeline.folder(session, ident) / first.name).read_bytes() == b"source"
    with pipeline.connect(write=True) as conn:
        conn.execute("UPDATE jobs SET updated=?", (time.time() - lp.LEASE - 1,))
    rebuilt = lp.Pipeline(tmp_path)
    assert rebuilt._claim()["id"] == ident
    assert pipeline._claim() is None
    with rebuilt.connect(write=True) as conn:
        conn.execute("UPDATE jobs SET lease=0")
    assert pipeline._claim()["id"] == ident


def test_whisper_cuda_failure_uses_real_cpu_backend_and_reuses_model(monkeypatch):
    import ctranslate2
    from types import SimpleNamespace

    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 1)
    monkeypatch.setattr(lp, "gpu_sample", lambda: {"free_mib": 8192})
    attempts = []

    def load(size, device, compute):
        attempts.append(device)
        if device == "cuda":
            raise RuntimeError("driver unavailable")
        return SimpleNamespace(model=SimpleNamespace(device="cpu", compute_type=compute))

    monkeypatch.setattr(lp.dt, "_load_model", load)
    monkeypatch.setattr(lp.dt, "_transcribe_media", lambda *args: ([], sentence(30), "es", 30))
    whisper = lp._WhisperRuntime()
    _, _, fallback = whisper.transcribe(Path("fixture.mp4"), 30, "auto")
    _, _, reused = whisper.transcribe(Path("fixture.mp4"), 30, "cpu")
    assert attempts == ["cuda", "cpu"]
    assert "CUDA" in fallback["fallback"]
    assert reused["device"] == fallback["device"] == "cpu"
    assert reused["rtf"] >= 0 and reused["fallback"] is None


def fake_whisper_worker(connection):
    with connection:
        while True:
            try:
                connection.recv()
            except EOFError:
                return
            connection.send((True, (sentence(30), "es", {"device": "cpu"})))


def test_production_whisper_uses_spawn_worker_reuse_and_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(lp, "_whisper_worker", fake_whisper_worker)
    stopping = threading.Event()
    whisper = lp.Whisper(threading.Lock(), stopping)
    try:
        assert whisper.transcribe(tmp_path / "a.mp4", 30, "cpu")[1] == "es"
        process = whisper.process
        assert whisper.transcribe(tmp_path / "b.mp4", 30, "cpu")[2]["device"] == "cpu"
        assert whisper.process is process
        stopping.set()
        with pytest.raises(lp.Paused):
            whisper.transcribe(tmp_path / "c.mp4", 30, "cpu")
    finally:
        whisper.close()
    assert not process.is_alive()


@pytest.fixture(scope="module")
def controlled_hls(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and FFprobe required")
    path = tmp_path_factory.mktemp("live12-hls")
    run = subprocess.run
    run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=navy:s=160x90:r=5",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "188",
         "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-g", "20",
         "-c:a", "aac", "-f", "hls", "-hls_time", "4", "-hls_list_size", "0", str(path / "index.m3u8")], check=True)
    http, url = local_server(path)
    try:
        yield url + "/index.m3u8"
    finally:
        http.shutdown()
        http.server_close()


@pytest.mark.parametrize(("profile", "target"), [("30-60", 60), ("60-90", 90), ("90-180", 180)])
def test_real_hls_production_from_preserved_source_with_139_second_lag(tmp_path, monkeypatch, controlled_hls, profile, target):
    session = "abcdef123456"
    home = tmp_path / "live" / session
    home.mkdir(parents=True)
    projects.atomic_json(home / "live_session.json", {"id": session, "status": "stopped", "triggers": [],
                                                    "export": {"completed": 0, "errors": 0}})
    options = projects.validate_options({"live_duration": profile, "aspect": "9:16", "live_whisper_device": "cpu"})
    buffer = live_capture.SegmentBuffer(home / "capture-000001", controlled_hls, options).start()
    try:
        assert buffer.process.wait(timeout=50) == 0
        closed = buffer.closed_ranges()
        assert closed and list(closed.values())[-1][1] >= 180
        pipeline = lp.Pipeline(tmp_path)
        trigger = live_clips.Trigger(35., 8.3, {"audio": 8.1, "visual": 8.5})
        ident, error = pipeline.reserve(session, trigger, options)
        assert ident and error is None
        # Production sees a closed copy, independent of a live ASR cursor 139 s behind.
        live_through = list(closed.values())[-1][1] - 139
        assert target > live_through
        pipeline.preserve(session, ident, buffer, closed, 0)
        pipeline.seal(ident)
    finally:
        buffer.stop()
        buffer.retire()
    assert not buffer.ready_segments()
    words = sentence(target - .13)
    monkeypatch.setattr(subtitle_quality, "speech_ranges", lambda path, duration, offset=0:
                        [(.4, target - .14)] if path.name == "source.mp4" else [(.25, min(duration - .20, target - .20))])
    calls = []
    def transcribe(source, duration, requested):
        assert source.is_file() and duration > 180 and requested == "cpu"
        calls.append(source)
        return words, "es", {"model": "fixture", "device": "cpu", "inference_seconds": .1, "rtf": .001}
    # Start the real HTTP server on the same project root. It recovers the persisted
    # job, renders it and triggers normal Automation admission.
    monkeypatch.setattr(lp.Whisper, "transcribe", lambda self, *args: transcribe(*args))
    with running_server(tmp_path) as (http, base):
        from test_dashboard import request
        path = "/api/live/" + session
        detail = until(lambda: request(base, path),
                       lambda value: value["production"]["recent"][0]["state"] in {"done", "error", "rejected"}, 110)
        job = detail["production"]["recent"][0]
        assert job["state"] == "done", job
        assert len(calls) == 1
        clip, = detail["triggers"]
        assert clip["score"] == 8.3 and clip["voice"] == "present"
        output = Path(clip["files"]["mp4"])
        real, tracks = subtitle_quality.probe_media(output)
        assert real == pytest.approx(target, abs=.35)
        assert tracks["video"]["height"] > tracks["video"]["width"]
        assert subtitle_quality.read_srt(output.with_suffix(".srt"))
        assert "Dialogue:" in output.with_suffix(".ass").read_text(encoding="utf-8-sig")
        assert http.app.tiktok._clip("live/" + session, output.name) == output
        visible = list(live_clips.live_exports(tmp_path))
        assert visible == [{"project": "live/" + session, "project_name": "Live · " + session, "filename": output.name}]
        assert Path(clip["files"]["window"]).is_file()
        assert request(base, "/api/live")["sessions"][0]["clips"] == 1


def test_live_session_hands_closed_source_to_durable_queue_without_continuous_whisper(tmp_path, monkeypatch, controlled_hls):
    from test_dashboard import request

    # HLS and FFmpeg are real. Fix the highlight sample and stop at the
    # production boundary so this test isolates the capture handoff.
    observed = threading.Event()

    def capture_job(self, job):
        media = lp.json.loads(job["media"])
        assert job["score"] == 7.5
        assert len(media) > 8 and media[-1][2] - media[0][1] >= 30
        assert all((self.folder(job["session"], job["id"]) / entry[0]).is_file() for entry in media)
        observed.set()
        raise lp.Rejected("Control de aceptación de la cola")

    monkeypatch.setattr(lp.Pipeline, "_produce", capture_job)
    monkeypatch.setattr(live_signals.HypeDetector, "evaluate",
                        lambda *args, **kwargs: {"score": 7.5, "signals": {"audio": 7.5}})
    with running_server(tmp_path) as (http, base):
        result = request(base, "/api/live", {"source": controlled_hls,
                         "options": {"live_duration": "30-60", "live_no_transcript": True}}, http.app.token)
        ident = result["id"]
        assert observed.wait(40)
        status = until(lambda: request(base, "/api/live/" + ident),
                       lambda detail: detail["production"]["counts"].get("rejected"))
        assert status["threshold"] == 6
        assert not status["health"]["analyzers"]["transcript"]["processed"]
