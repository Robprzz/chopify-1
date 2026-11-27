"""Local GPU runtime tests with simulated Windows, CUDA and missing libraries."""
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import download_and_transcribe as dt
import gpu_runtime
import projects
from test_dashboard import request, running_server
from test_workspace42 import chromium_page  # noqa: F401


def fake_windows(monkeypatch, tmp_path, packages=gpu_runtime.PACKAGES):
    prefix = tmp_path / ".venv"
    library = prefix / "Lib" / "site-packages"
    paths = [library / "nvidia" / name / "bin" for name in packages]
    for path in paths:
        path.mkdir(parents=True)
    opened = []

    def register(path):
        handle = object()
        opened.append((path, handle))
        return handle

    monkeypatch.setattr(gpu_runtime, "sys", SimpleNamespace(
        platform="win32", prefix=str(prefix), base_prefix=str(tmp_path / "base")))
    monkeypatch.setattr(gpu_runtime.sysconfig, "get_path", lambda _: str(library))
    monkeypatch.setattr(gpu_runtime, "_handles", {})
    monkeypatch.setattr(gpu_runtime.os, "add_dll_directory", register, raising=False)
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    return paths, opened


def test_windows_runtime_registers_wheel_paths_once_and_passes_path_to_child(monkeypatch, tmp_path):
    paths, opened = fake_windows(monkeypatch, tmp_path)
    previous = os.environ["PATH"]
    assert gpu_runtime.configure() is None
    assert gpu_runtime.configure() is None
    expected = [str(path) for path in paths]
    assert [path for path, _ in opened] == expected
    assert list(gpu_runtime._handles.values()) == [handle for _, handle in opened]
    child = subprocess.check_output(
        [sys.executable, "-X", "utf8", "-c", "import os; print(os.environ['PATH'])"], text=True, encoding="utf-8")
    assert child.strip().split(os.pathsep)[:3] == expected
    if previous:
        assert os.environ["PATH"].endswith(previous)


def test_windows_missing_wheel_never_registers_partial_paths(monkeypatch, tmp_path):
    _, opened = fake_windows(monkeypatch, tmp_path, ("cuda_runtime", "cublas"))
    previous = os.environ["PATH"]
    assert "cudnn" in gpu_runtime.candidate()
    assert not opened
    assert os.environ["PATH"] == previous


def test_windows_refuses_library_outside_active_environment(monkeypatch, tmp_path):
    _, opened = fake_windows(monkeypatch, tmp_path)
    monkeypatch.setattr(gpu_runtime.sysconfig, "get_path", lambda _: str(tmp_path))
    assert "cudnn" in gpu_runtime.candidate()
    assert not opened


def test_visible_device_is_only_a_candidate(monkeypatch):
    monkeypatch.setattr(gpu_runtime, "configure", lambda: None)
    ct = SimpleNamespace(get_cuda_device_count=lambda: 1)
    monkeypatch.setitem(sys.modules, "ctranslate2", ct)
    assert gpu_runtime.candidate() is None
    ct.get_cuda_device_count = lambda: 0
    assert "no disponible" in gpu_runtime.candidate()


def fake_model(device, compute, infer):
    class Model:
        model = SimpleNamespace(device=device, compute_type=compute)

        def transcribe(self, media, **_):
            return infer(Path(media), device), SimpleNamespace(
                language="es", language_probability=.99, duration=3.)
    return Model()


def segment(text, start=.1):
    return SimpleNamespace(start=start, end=start + .6, text=text,
                           words=[SimpleNamespace(word=text, start=start, end=start + .6)])


@pytest.mark.parametrize("requested, expected, compute", [("auto", "cuda", "float16"),
                                                         ("cpu", "cpu", "int8")])
def test_real_backend_metrics_require_completed_inference(monkeypatch, tmp_path, requested, expected, compute):
    checked = []
    monkeypatch.setattr(gpu_runtime, "candidate", lambda: checked.append("CUDA"))
    monkeypatch.setattr(dt, "_load_model", lambda _, device, mode:
                        fake_model(device, mode, lambda *_: iter([segment("こんにちは")])))
    out = dt.transcribe("fixture.mp4", tmp_path, "medium", requested, "default")
    result = projects.read_json(out)
    metrics = result["whisper_runtime"]
    assert (metrics["device"], metrics["model"], metrics["compute_type"]) == (expected, "medium", compute)
    assert metrics["elapsed_seconds"] >= metrics["inference_seconds"] >= 0
    assert result["words"][0]["word"] == "こんにちは"
    assert checked == (["CUDA"] if requested == "auto" else [])


@pytest.mark.parametrize("phase", ["load", "iterate"])
def test_cuda_fallback_retries_once_from_start_and_never_commits_partial(monkeypatch, tmp_path, phase):
    monkeypatch.setattr(gpu_runtime, "candidate", lambda: None)
    calls = []

    def infer(_media, device):
        if device == "cuda":
            yield segment("parcial GPU")
            raise RuntimeError("Library cublas64_12.dll is not found")
        yield segment("árabe العربية")

    def load(_, device, compute):
        calls.append((device, compute))
        if device == "cuda" and phase == "load":
            raise RuntimeError("CUDA failed with error out of memory")
        return fake_model(device, compute, infer)

    monkeypatch.setattr(dt, "_load_model", load)
    out = dt.transcribe("fixture.mp4", tmp_path, "small", "cuda", "float16")
    data = projects.read_json(out)
    assert [word["word"] for word in data["words"]] == ["árabe العربية"]
    assert calls == [("cuda", "float16"), ("cpu", "int8")]
    assert data["whisper_runtime"]["device"] == "cpu"
    assert "Falló CUDA" in data["whisper_runtime"]["fallback"]


@pytest.mark.parametrize("error", [PermissionError("acceso denegado"), FileNotFoundError("audio.mp4"),
                                   OSError("red desconectada"), ValueError("archivo dañado")])
def test_source_errors_propagate_without_cpu_retry(monkeypatch, tmp_path, error):
    monkeypatch.setattr(gpu_runtime, "candidate", lambda: None)
    loads = []

    def load(_, device, compute):
        loads.append(device)

        def infer(*_):
            raise error

        return fake_model(device, compute, infer)

    monkeypatch.setattr(dt, "_load_model", load)
    with pytest.raises(type(error), match=str(error)):
        dt.transcribe("fixture.mp4", tmp_path, "medium", "auto", "default")
    assert loads == ["cuda"]
    assert not (tmp_path / "transcript.json").exists()


def test_chunked_gpu_fallback_preserves_finished_and_legacy_checkpoints(monkeypatch, tmp_path):
    monkeypatch.setattr(gpu_runtime, "candidate", lambda: None)
    monkeypatch.setattr(dt, "CHUNK_SECONDS", 20)
    monkeypatch.setattr(dt, "OVERLAP_SECONDS", 2)
    monkeypatch.setattr(dt, "_extract_chunk", lambda _, start, end, path: path.touch())
    calls = []

    def infer(media, device):
        calls.append((media.stem, device))
        if media.stem == "part001" and device == "cuda":
            yield segment("parcial")
            raise RuntimeError("CUDA error: device lost")
        yield segment(media.stem, start=2.)

    monkeypatch.setattr(dt, "_load_model", lambda _, device, compute: fake_model(device, compute, infer))

    def run():
        return projects.read_json(dt.transcribe("fixture.mp4", tmp_path, "small", "auto", "default",
                                               chunked=True, duration=45., identity="video"))

    result = run()
    assert calls == [("part000", "cuda"), ("part001", "cuda"), ("part001", "cpu"), ("part002", "cpu")]
    assert [word["word"] for word in result["words"]] == ["part000", "part001", "part002"]
    assert result["whisper_runtime"]["device"] == "cpu / cuda"
    part = tmp_path / "transcript.part000.json"
    legacy = projects.read_json(part)
    del legacy["whisper_runtime"]
    projects.atomic_json(part, legacy)
    before = part.read_bytes()
    calls.clear()
    resumed = run()
    assert not calls and part.read_bytes() == before
    assert resumed["words"] == result["words"]
    assert resumed["whisper_runtime"]["elapsed_seconds"] is None
    assert "sin confirmar" in resumed["whisper_runtime"]["device"]


def test_chromium_project_acceleration_and_legacy_settings(tmp_path, monkeypatch, chromium_page):
    page, expect = chromium_page
    root = tmp_path / "projects"
    media = tmp_path / "source.mp4"
    media.touch()
    with running_server(root) as (server, base):
        monkeypatch.setattr(server.app, "enqueue", lambda *_args, **_kw: None)
        legacy = request(base, "/api/projects", {"source": "old.mp4"}, server.app.token)
        assert legacy["options"]["device"] == "cpu"
        page.goto(base)
        page.locator("#show-projects").click()
        page.locator("#new-project").click()
        expect(page.locator("#new-model")).to_have_value("medium")
        expect(page.locator("#new-device")).to_have_value("auto")
        page.locator("#new-model").select_option("small")
        page.locator("#new-device").select_option("cuda")
        page.locator("#sources").fill(str(media))
        with page.expect_response(lambda response: response.url.endswith("/api/batch")) as sent:
            page.locator("#create-form button[type=submit]").click()
        assert sent.value.ok
        item = sent.value.json()["projects"][0]
        persisted = projects.read_project(root / item["id"])
        assert persisted["options"]["device"] == "cuda"
        assert persisted["options"]["whisper_model"] == "small"
        projects.atomic_json(root / item["id"] / "transcript.json", {
            "video": str(media), "whisper_runtime": {"device": "cpu", "model": "small",
                "compute_type": "int8", "elapsed_seconds": 9.5, "fallback": "Falló CUDA."}})
        page.reload()
        page.locator("#show-projects").click()
        page.locator(f'#projects [data-id="{item["id"]}"]').click()
        expect(page.locator("#project-whisper")).to_contain_text("small · cpu · int8 · 9.5 s")
        expect(page.locator("#project-whisper")).to_contain_text("Falló CUDA.")
        page.locator("#show-settings").click()
        page.locator("#show-system").click()
        expect(page.locator("#system-whisper")).to_contain_text("small · cpu · int8 · 9.5 s")


def test_missing_cuda_dlls_run_cpu_int8_and_report_reason(monkeypatch, tmp_path):
    fake_windows(monkeypatch, tmp_path, ("cuda_runtime", "cublas"))
    monkeypatch.setattr(dt, "_load_model", lambda _, device, compute:
                        fake_model(device, compute, lambda *_: iter([segment("Hola")])))
    result = projects.read_json(dt.transcribe("fixture.mp4", tmp_path, "medium", "auto", "default"))
    metrics = result["whisper_runtime"]
    assert (metrics["device"], metrics["compute_type"]) == ("cpu", "int8")
    assert "Faltan las bibliotecas NVIDIA en .venv: cudnn." in metrics["fallback"]


def test_live_continuous_uses_cuda_or_yields_shared_resources(monkeypatch):
    import threading

    import live_signals

    resources = threading.Lock()
    shared = {"lock": threading.Lock(), "model": None, "resources": resources}
    analyzer = live_signals.TranscriptAnalyzer(None, None, device="auto", shared=shared)
    analyzer._bounds = (0., 5.)
    monkeypatch.setattr(live_signals, "decode_segment", lambda *_: None)
    monkeypatch.setattr(gpu_runtime, "candidate", lambda: None)
    monkeypatch.setattr(dt, "_load_model", lambda _, device, compute:
                        fake_model(device, compute, lambda *_: iter([segment("Hola")])))
    words, _, _, metrics = analyzer._decode("segment.ts")
    assert words[0]["word"] == "Hola"
    assert metrics["device"] == "cuda"
    assert shared["runtimes"]["auto"].model is None
    with resources:
        _, _, _, metrics = analyzer._decode("segment.ts")
    assert metrics["device"] == "cpu"
    assert "GPU ocupada" in metrics["fallback"]
