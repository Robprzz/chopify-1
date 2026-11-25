"""UTF-8 regressions under a legacy code page, using the normal media entry points."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from urllib.parse import quote
from urllib.request import urlopen

import pytest

import dashboard
import ingest
import projects
import studio_launcher
import studio_settings
from subtitle_quality import read_ass, read_srt
from test_dashboard import request
from test_studio_launcher import until


CHECKOUT = Path(dashboard.__file__).resolve().parent
TEXT = "治 日本語 العربية canción café 😃 𐐷 e\u0301"
LEGACY = {"LC_ALL": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1252:strict"}


@pytest.mark.parametrize("timeout", [None, 10])
def test_ingest_round_trips_multilingual_subprocess_metadata(timeout, monkeypatch):
    for name, value in LEGACY.items():
        monkeypatch.setenv(name, value)
    script = "import json, os, sys; print(json.dumps([sys.flags.utf8_mode, os.environ['PYTHONIOENCODING'], " + repr(TEXT) + "], ensure_ascii=False))"
    result = ingest._run([sys.executable, "-c", script], timeout=timeout)
    assert json.loads(result.stdout) == [1, "utf-8", TEXT]


def test_direct_agent_cli_uses_utf8_under_cp1252(tmp_path):
    root = tmp_path / "Proyectos 治 العربية"
    source = str(tmp_path / f"{TEXT}.mp4")
    result = subprocess.run(
        [sys.executable, str(CHECKOUT / "agent.py"), "project", "create", "--workdir", str(root), "--source", source],
        cwd=CHECKOUT, env={**os.environ, **LEGACY, "LC_ALL": "C.UTF-8"}, capture_output=True, timeout=15, check=True,
    )
    assert str(root) in result.stdout.decode("utf-8")
    record = next(root.glob("*/project.json"))
    assert projects.read_json(record)["source"] == source


def test_job_log_tail_preserves_split_utf8_character(tmp_path):
    project = projects.create_project(tmp_path, "source.mp4", {})
    path = tmp_path / project["id"] / "job.log"
    path.write_text("older log\n治" + ("a" * 11999), encoding="utf-8")
    assert dashboard.project_detail(tmp_path, project["id"])["log"] == "治" + "a" * 11999


def test_one_click_delayed_http_transcription_and_export_under_cp1252(tmp_path, monkeypatch):
    if sys.prefix == sys.base_prefix or os.name == "nt":
        pytest.skip("The subprocess fixture uses the Linux test virtual environment")
    tools = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe")}
    if not all(tools.values()):
        pytest.skip("FFmpeg and FFprobe are required for the media pipeline test")
    tool_dir = tmp_path / "FFmpeg & herramientas"
    tool_dir.mkdir()
    for name, executable in tools.items():
        (tool_dir / name).symlink_to(executable)
    checkout = tmp_path / "Chopify 日本語 café"
    checkout.mkdir()
    for source in CHECKOUT.glob("*.py"):
        shutil.copy2(source, checkout / source.name)
    shutil.copytree(CHECKOUT / "assets" / "studio", checkout / "assets" / "studio")
    (checkout / ".venv").symlink_to(sys.prefix, target_is_directory=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "estado 🧪"))
    for name, value in LEGACY.items():
        monkeypatch.setenv(name, value)

    # Delay the first HTTP response after the socket binds. Keep the actual launcher,
    # dashboard, pipeline, log writer, caption generators and FFmpeg in the test.
    script = checkout / "dashboard.py"
    source = script.read_text(encoding="utf-8")
    script.write_text(source.replace('if __name__ == "__main__":\n    main()', '''
if __name__ == "__main__":
    import time
    original_init = Studio.__init__
    def delayed_init(self, *args):
        time.sleep(1.25)
        original_init(self, *args)
    Studio.__init__ = delayed_init
    main()
'''), encoding="utf-8")
    script = checkout / "download_and_transcribe.py"
    with script.open("a", encoding="utf-8") as fixture:
        fixture.write(f'''
def _load_model(*_args):
    from types import SimpleNamespace as NS
    import os
    import sys
    assert sys.flags.utf8_mode == 1
    assert sys.stdout.encoding == "utf-8"
    assert os.environ["PYTHONUTF8"] == "1"
    assert os.environ["PYTHONIOENCODING"] == "utf-8"
    phrase = {TEXT!r}
    def transcribe(*args, **kwargs):
        segment = NS(start=.25, end=2.25, text=phrase,
                     words=[NS(start=.25, end=2.25, word=phrase)])
        return [segment], NS(language="zh", language_probability=1, duration=3)
    return NS(transcribe=transcribe)
''')
    root = tmp_path / "Proyectos العربية 治 😃"
    store = studio_settings.Settings(checkout)
    store.save({"projects_dir": str(root), "open_browser": True})
    media = tmp_path / "fuente 日本語 العربية 😊.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=black:s=320x180:r=25:d=3",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-c:v", "libx264", "-c:a", "aac", "-shortest", str(media)], check=True)
    monkeypatch.setenv("PATH", "")
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    original_probe = studio_launcher.probe
    statuses = []
    browser = []

    def observe_probe(*args):
        result = original_probe(*args)
        statuses.append(result)
        return result

    def open_browser(url):
        assert statuses[-1] == "ours"
        browser.append(url)
        return True

    monkeypatch.setattr(studio_launcher, "probe", observe_probe)
    monkeypatch.setattr(studio_launcher.webbrowser, "open", open_browser)
    try:
        url = studio_launcher.launch(store, port=port)
        assert "pending" in statuses and browser == [url]
        assert studio_launcher.launch(store, port=port) == url
        log = (store.logs / "studio.log").read_text(encoding="utf-8")
        assert log.count("Studio: verificando FFmpeg y FFprobe.") == 1
        assert "FFmpeg y FFprobe pendientes de configurar." in log
        assert "Chopify 日本語 café" in log and root.name in log
        base = url.rstrip("/")
        token = request(base, "/api/config")["token"]
        preferences = request(base, "/api/system/save", {"ffmpeg_dir": str(tool_dir)}, token)
        assert preferences["ffmpeg_dir"] == str(tool_dir)
        assert os.environ["PATH"] == ""
        project = request(base, "/api/projects", {"source": str(media), "options": {}}, token)
        route = f"/api/projects/{project['id']}"
        request(base, route + "/run", {"action": "prepare"}, token)

        def finished():
            detail = request(base, route)
            return detail if not detail["busy"] else None

        detail = until(finished, timeout=30)
        assert detail["project"]["status"] == "review", detail["log"]
        assert "PREFLIGHT OK" in detail["log"]
        assert detail["transcript"]["segments"][0]["text"] == TEXT
        assert detail["transcript"]["words"][0]["word"] == TEXT
        assert TEXT in detail["log"]
        request(base, route + "/candidates",
                {"candidates": [{"start": 0, "end": 3, "overall": 9, "hook": "治 日本語 العربية canción"}]}, token)
        request(base, route + "/run", {"action": "render"}, token)
        detail = until(finished, timeout=30)
        assert detail["project"]["status"] == "complete", detail["log"]
        clip = Path(detail["project"]["last_outputs"][0])
        assert "治-日本語-العربية-canción" in clip.name
        assert read_srt(clip.with_suffix(".srt"))[0][2] == TEXT
        assert TEXT in read_ass(clip.with_suffix(".ass"))[0][2]
        track = next(item for item in detail["files"] if item["name"] == clip.with_suffix(".srt").name)
        with urlopen(base + quote(track["url"], safe="/"), timeout=5) as response:
            assert TEXT in response.read().decode("utf-8")
    finally:
        if original_probe(port, store.identity()) == "ours":
            base = f"http://127.0.0.1:{port}"
            token = request(base, "/api/config")["token"]
            request(base, "/api/system/shutdown", {"confirmed": True, "mode": "cancel"}, token)
            until(lambda: original_probe(port, store.identity()) == "free")
