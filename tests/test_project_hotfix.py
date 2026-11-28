"""Production-shaped regression cases for the project hotfix."""
import json
import pathlib
import sys
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError

import numpy as np
import pytest

import download_and_transcribe as dt
import export_settings
import gpu_runtime
import project_cancel
import projects
import render_progress
import render_clips
import score_clips
from test_dashboard import request, running_server
from test_discovery_cache import cached_project  # noqa: F401
from test_signature import prepared
from test_studio_navigation import chromium_page  # noqa: F401
from test_watermark import gray_source
from test_workspace42 import fixture_media


def test_yunet_float32_observations_can_be_persisted_during_real_render(tmp_path, monkeypatch):
    media = gray_source(tmp_path)

    # YuNet returns float32 bounding boxes. The renderer calculates centres
    # from these exact OpenCV values before writing the camera cache.
    monkeypatch.setattr(render_clips, "detect_track", lambda *_: [
        (i / 5, [np.float32(250 + i * 5)], False) for i in range(6)
    ])
    output = render_clips.render(
        {"start": 0, "end": 1.2, "hook": "Entrevista"}, [], media, 640, 360,
        tmp_path, "9:16", camera_mode="smart", out_dir=tmp_path / "clips",
        allow_model_download=False, timeout=45)
    assert output.is_file()
    assert json.loads((tmp_path / ".render-camera.json").read_text(encoding="utf-8"))


def test_rejects_bad_json_without_touching_existing_manifest(tmp_path):
    import studio_json

    path = tmp_path / "project.json"
    value = {"float": np.float32(.25), "wide": np.float64(.5), "int": np.int64(42),
             "bool": np.bool_(True), "array": np.array([[1, 2]], dtype=np.uint32),
             "zero_dim": np.array(False)}
    studio_json.write(path, value)
    previous = path.read_bytes()
    assert json.loads(previous) == {"float": .25, "wide": .5, "int": 42,
                                    "bool": True, "array": [[1, 2]], "zero_dim": False}
    for bad in (float("nan"), np.float32("inf"), np.array([[np.inf]])):
        with pytest.raises(ValueError):
            studio_json.write(path, {"bad": bad})
    with pytest.raises(TypeError, match="not JSON serializable"):
        studio_json.write(path, {"bad": object()})
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]




@pytest.mark.parametrize("mode", ["quality", "fast", "template", "clips"])
def test_actual_ffmpeg_exports_preserve_numpy_scores_and_camera_cache(tmp_path, monkeypatch, mode):
    source = gray_source(tmp_path)
    monkeypatch.setattr(render_clips, "detect_track", lambda *_: [
        (i / 5, np.array([225 + 5 * i], dtype=np.float32), np.bool_(False)) for i in range(6)
    ])
    if mode == "clips":
        ident, original = fixture_media(tmp_path)
        state = projects.read_project(tmp_path / ident)
        state["options"]["camera_mode"] = "smart"
        projects.save_project(tmp_path / ident, state)
        with running_server(tmp_path) as (server, base):
            submitted = request(base, "/api/clip/render",
                                {"project": ident, "file": original.name, "aspect": "9:16"}, server.app.token)
            result = server.app.clip_jobs[submitted["job"]].result(timeout=40)
            assert result["state"] == "ready", result
            output = original.parent / result["file"]
            assert original.is_file() and original != output
    else:
        folder = prepared(tmp_path, source)
        state = projects.read_project(folder)
        state["options"].update(aspect="9:16", camera_mode="smart", export_profile="fast" if mode == "fast" else "quality")
        projects.save_project(folder, state)
        if mode == "template":
            export_settings.Store(tmp_path).save({"enabled": True, "aspect": "9:16", "signature": True,
                                                    "camera_mode": "smart", "watermark": False})
        projects.atomic_json(folder / "segments.json", [
            {"start": 0, "end": 1.2, "overall": np.float32(8.5), "hook": "Entrevista"},
        ])
        output = pathlib.Path(projects.run_project(folder, "render")["last_outputs"][0])
        assert output.with_suffix(".srt").is_file()
        assert output.with_suffix(".meta.json").is_file()
        assert projects.read_json(output.with_suffix(".meta.json"))["score"] == 8.5
    assert render_clips.ffprobe_dims(output) == (1080, 1920)
    assert (output.parent.parent / ".render-camera.json").is_file()


def test_stop_kills_downloader_and_children_and_cleans_staging(tmp_path, monkeypatch):
    job = project_cancel.Job(tmp_path, "a" * 32)
    ready, orphan = (tmp_path / name for name in ("downloading", "orphan"))
    child = "import pathlib, sys, time; time.sleep(3); pathlib.Path(sys.argv[1]).touch()"
    script = ("import pathlib, subprocess, sys, time\n"
              "subprocess.Popen([sys.executable, '-c', sys.argv[3], sys.argv[2]])\n"
              "pathlib.Path(sys.argv[1]).touch()\n"
              "time.sleep(30)\n")

    def download_tool(command):
        assert command[2] == "yt_dlp"
        partial = pathlib.Path(command[command.index("-o") + 1].replace("%(ext)s", "mp4.part"))
        partial.write_bytes(b"partial output")
        project_cancel.run([sys.executable, "-c", script, str(ready), str(orphan), child], check=True)

    monkeypatch.setattr(dt, "run", download_tool)

    def request_stop():
        deadline = time.monotonic() + 8
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        job.request()

    thread = threading.Thread(target=request_stop)
    thread.start()
    try:
        with project_cancel.scope(job), pytest.raises(project_cancel.Cancelled):
            dt.download("https://www.youtube.com/watch?v=ABCDEFGHIJK", tmp_path)
    finally:
        thread.join(10)
    assert ready.exists()
    assert not list(tmp_path.glob("download-*"))
    assert not list(tmp_path.glob("*.mp4*"))
    time.sleep(3)
    assert not orphan.exists()


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_whisper_stops_between_segments_and_keeps_finished_parts(tmp_path, monkeypatch, device):
    first = project_cancel.Job(tmp_path, "a" * 32)
    resumed = project_cancel.Job(tmp_path, "b" * 32)
    video = tmp_path / "source.mp4"
    video.touch()
    attempts = []
    monkeypatch.setattr(gpu_runtime, "candidate", lambda: None)
    monkeypatch.setattr(dt, "CHUNK_SECONDS", 20)
    monkeypatch.setattr(dt, "_extract_chunk", lambda source, start, end, wav: wav.touch())

    def transcribe(path, **_):
        index = pathlib.Path(path).stem
        attempts.append(index)

        def segments():
            if index == "part001" and len(attempts) == 2:
                first.request()
            yield SimpleNamespace(start=11., end=12., text="Hola.", words=[])

        return segments(), SimpleNamespace(language="es", duration=20)

    monkeypatch.setattr(dt, "_load_model", lambda name, backend, compute:
                        SimpleNamespace(transcribe=transcribe, model=SimpleNamespace(device=backend, compute_type=compute)))
    with project_cancel.scope(first), pytest.raises(project_cancel.Cancelled):
        dt.transcribe_chunked(video, tmp_path, "tiny", device, "default", duration=40)
    checkpoint = tmp_path / "transcript.part000.json"
    written = checkpoint.read_bytes()
    assert not (tmp_path / "transcript.part001.json").exists()
    assert not list((tmp_path / "audio_parts").glob("*.wav"))
    with project_cancel.scope(resumed):
        result = dt.transcribe_chunked(video, tmp_path, "tiny", device, "default", duration=40)
    assert result.is_file() and checkpoint.read_bytes() == written
    assert attempts == ["part000", "part001", "part001"]


@pytest.mark.parametrize(("stage", "function"), [("detect","detect_from_transcript"),
    ("discover","source_evidence"), ("select","dedupe_candidates")])
def test_stop_between_analysis_stages_reuses_completed_checkpoints(cached_project, monkeypatch, stage, function):
    import video_types

    folder, calls = cached_project
    job = project_cancel.Job(folder, "c" * 32)
    module = video_types if stage == "detect" else score_clips
    original = getattr(module, function)

    def request_during_stage(*args, **kwargs):
        if projects.read_project(folder)["stage"] == stage and not job.requested():
            job.request()
        project_cancel.check()
        return original(*args, **kwargs)

    monkeypatch.setattr(module, function, request_during_stage)
    state = projects.run_project(folder, "prepare", job=job)
    assert (state["status"], state["stage"]) == ("cancelled", stage)
    before = (folder / "transcript.json").read_bytes()
    discovery = projects.file_identity(folder / "candidates.json") if stage == "select" else None
    monkeypatch.setattr(module, function, original)
    assert projects.run_project(folder, "prepare", job=project_cancel.Job(folder, "d" * 32))["status"] == "review"
    assert calls == ["download", "transcribe"] and (folder / "transcript.json").read_bytes() == before
    if discovery:
        assert projects.file_identity(folder / "candidates.json") == discovery


def test_cancel_real_ffmpeg_and_keep_the_previous_export(tmp_path):
    folder = prepared(tmp_path, gray_source(tmp_path))
    segments = projects.read_json(folder / "segments.json")
    segments.append({"start": 1.2, "end": 2.4, "hook": "Segundo", "overall": 8})
    projects.atomic_json(folder / "segments.json", segments)
    first = projects.run_project(folder, "render", pick="1")["last_outputs"][0]
    data = pathlib.Path(first).read_bytes()
    job = project_cancel.Job(folder, "e" * 32)

    def progress(event):
        if event == "render 2/2":
            job.request()

    result = projects.run_project(folder, "render", job=job, progress=progress)
    assert result["status"] == "cancelled" and pathlib.Path(first).read_bytes() == data
    command = ["ffmpeg", "-nostdin", "-re", "-f", "lavfi", "-i", "color=size=32x32:rate=10",
               "-t", "6", "-f", "null", "-"]
    live = project_cancel.Job(folder, "f" * 32)
    timer = threading.Timer(.35, live.request)
    timer.start()
    try:
        with project_cancel.scope(live), pytest.raises(project_cancel.Cancelled):
            render_progress.encode(command, folder, 12, 6, render_progress.Progress(lambda _: None))
    finally:
        timer.join()
    resumed = projects.run_project(folder, "render", job=project_cancel.Job(folder, "1" * 32))
    assert resumed["last_outputs"][0] == first
    assert pathlib.Path(first).read_bytes() == data
    assert len(list((folder / "clips").glob("*.mp4"))) == 2
    assert not list((folder / "clips").glob(".render-*"))


def test_studio_stop_and_resume_control_one_project(chromium_page, tmp_path):
    page, expect = chromium_page
    first = prepared(tmp_path, gray_source(tmp_path))
    second = prepared(tmp_path, gray_source(tmp_path))
    with running_server(tmp_path) as (server, base):
        with server.app.resources:
            token = server.app.token
            for folder in (first, second):
                request(base, f"/api/projects/{folder.name}/run", {"action":"render", "pick":"1"}, token)
            page.goto(base)
            page.locator("#show-projects").click()
            page.locator(f'#projects [data-id="{first.name}"]').click()
            old = projects.read_project(first)["job"]["id"]
            expect(page.get_by_role("button", name="Detener proyecto")).to_be_visible()
            page.get_by_role("button", name="Detener proyecto").click()
            expect(page.locator("#status")).to_contain_text("Cancelado", timeout=10000)
            assert projects.read_project(second)["status"] == "queued"
            expect(page.get_by_role("button", name="Reanudar proyecto")).to_be_visible()
        page.get_by_role("button", name="Reanudar proyecto").click()
        expect(page.locator("#status")).to_contain_text("Completado", timeout=45000)
        assert projects.read_project(first)["job"]["id"] != old
        assert projects.read_project(first)["last_outputs"]
        assert projects.read_project(second)["status"] == "complete"



def test_queued_job_and_stale_requests_use_their_own_execution(tmp_path):
    first = prepared(tmp_path, gray_source(tmp_path))
    second = prepared(tmp_path, gray_source(tmp_path))
    with running_server(tmp_path) as (server, base):
        with server.app.resources:
            token = server.app.token
            first_job = request(base, f"/api/projects/{first.name}/run", {"action":"render"}, token)["job"]
            route = f"/api/projects/{second.name}"
            old_job = request(base, route + "/run", {"action":"render", "pick":"1"}, token)["job"]
            assert request(base, route + "/stop", {"job":old_job["id"]}, token)["status"] == "cancelled"
            new_job = request(base, route + "/resume", {"job":old_job["id"]}, token)["job"]
            assert new_job["id"] != old_job["id"] and new_job["pick"] == "1"
            for action in ("/stop", "/resume"):
                with pytest.raises(HTTPError) as rejected:
                    request(base, route + action, {"job":old_job["id"]}, token)
                assert rejected.value.code == 409
            assert projects.read_project(first)["job"] == first_job
        server.app.futures[first.name].result(timeout=45)
        server.app.futures[second.name].result(timeout=45)
        assert projects.read_project(second)["status"] == "complete"
