"""Exercise Signature with the bundled artwork and actual AVC/AAC exports."""
import json
from pathlib import Path
import shutil
import subprocess

import cv2
import numpy as np
import pytest

import projects
import render_clips
import signature
from test_dashboard import request, running_server
from test_studio_navigation import chromium_page  # noqa: F401
from test_watermark import configure, extract_frame, gray_source, logo, red  # noqa: F401


def prepared(root, source):
    project = projects.create_project(root, str(source), {"camera_mode": "stable"})
    path = root / project["id"]
    words = [{"word": "Hola", "start": .1, "end": .7}]
    projects.atomic_json(path / "transcript.json", {"video": str(source), "duration": 3, "words": words})
    projects.atomic_json(path / "segments.json", [{"start": 0, "end": 1.2, "hook": "Signature", "overall": 8}])
    return path


def settings(include_mark=False):
    return {"template": signature.TEMPLATE_ID, "watermark": include_mark}


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="Requires FFmpeg and FFprobe")
def test_four_branding_combinations_keep_original_and_captions_in_center(tmp_path, logo, monkeypatch):
    media = gray_source(tmp_path)
    original = media.read_bytes()
    path = prepared(tmp_path, media)
    state = projects.read_project(path)
    # An older manifest has no Signature field.
    state["options"].pop("signature")
    projects.save_project(path, state)
    monkeypatch.chdir(tmp_path)
    plain = Path(projects.run_project(path, action="render")["last_outputs"][0])
    assert render_clips.ffprobe_dims(plain) == (1920, 1080)
    assert red(extract_frame(plain))[:300].max() < 8
    configure(tmp_path, logo, size=20, position="top-left")
    marked = Path(projects.run_project(path, action="render")["last_outputs"][0])
    assert red(extract_frame(marked)).max() > 130

    def render(include_mark):
        state = projects.read_project(path)
        state["options"]["signature"] = settings(include_mark)
        projects.save_project(path, state)
        return Path(projects.run_project(path, action="render")["last_outputs"][0])

    branded, both = render(False), render(True)
    assert len({plain, marked, branded, both}) == 4
    frame = signature.load(signature.TEMPLATE_ID)
    top, bottom = frame.bands(1080, 1920)
    assert (top, bottom) == (336, 228)
    clean, stamped = extract_frame(branded), extract_frame(both)
    assert clean.shape[:2] == (1920, 1080)
    for region, asset, height in ((clean[:top], frame.header, top), (clean[-bottom:], frame.footer, bottom)):
        resized = cv2.resize(cv2.imread(str(asset)), (1080, height))
        assert np.mean(np.abs(region.astype(float) - resized)) < 8
    assert np.mean(np.abs(clean[:top - 8].astype(float) - stamped[:top - 8])) < 2
    assert np.mean(np.abs(clean[-bottom + 8:].astype(float) - stamped[-bottom + 8:])) < 2
    assert red(stamped)[top + 70, 82] > 130 and red(clean)[top + 70, 82] < 8
    assert "PlayResY: 1356" in branded.with_suffix(".ass").read_text(encoding="utf-8")
    assert "Hola" in branded.with_suffix(".srt").read_text(encoding="utf-8")
    result = subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(branded)])
    tracks = {track["codec_type"]: track for track in json.loads(result)["streams"]}
    assert tracks["video"]["codec_name"] == "h264" and tracks["audio"]["codec_name"] == "aac"
    assert tracks["video"]["avg_frame_rate"] == "30/1"
    assert tracks["video"]["sample_aspect_ratio"] == "1:1"
    assert abs(float(tracks["video"]["duration"]) - float(tracks["audio"]["duration"])) <= .1
    state = projects.read_project(path)
    state["options"]["signature"] = None
    projects.save_project(path, state)
    assert projects.run_project(path, action="render")["last_outputs"] == [str(marked)]
    assert media.read_bytes() == original


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="Requires FFmpeg and FFprobe")
def test_vertical_source_signature_preview_and_settings_survive_server_restart(tmp_path, chromium_page, monkeypatch):
    media = tmp_path / "vertical.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(gray_source(tmp_path)),
                    "-vf", "transpose=clock", "-c:a", "copy", str(media)], check=True)
    path = prepared(tmp_path, media)
    page, expect = chromium_page
    with running_server(tmp_path) as (server, base):
        # Use the real worker and renderer in this process so the HTTP fixture can control it.
        monkeypatch.setattr(server.app, "_run", lambda work, action, preview, pick, recovery:
                            projects.run_project(work, action=action, preview=preview, pick=pick, recovery=recovery))
        page.goto(base)
        page.locator("#show-settings").click()
        page.locator("#show-watermark").click()
        toggle = page.get_by_role("switch", name="Activar template")
        toggle.check()
        expect(page.locator("#signature-header")).to_be_visible()
        expect(page.locator("#signature-source")).to_be_visible()
        toggle.uncheck()
        expect(page.locator("#signature-header")).to_be_hidden()
        toggle.check()
        page.get_by_role("button", name="Vista previa del template").click()
        expect(page.locator("#signature-status")).to_have_text("Vista previa lista.", timeout=30000)
        expect(page.locator("#signature-player")).to_be_visible()
        rendered = Path(request(base, f"/api/projects/{path.name}")["project"]["last_outputs"][0])
        assert render_clips.ffprobe_dims(rendered) == (480, 854)
        assert "Hola" in rendered.with_suffix(".srt").read_text(encoding="utf-8")
    with running_server(tmp_path) as (_, base):
        page.goto(base)
        page.locator("#show-settings").click()
        page.locator("#show-watermark").click()
        expect(page.get_by_role("switch", name="Activar template")).to_be_checked()
        page.get_by_role("button", name="Restablecer valores predeterminados").click()
        page.get_by_role("button", name="Guardar template").click()
        expect(page.locator("#signature-status")).to_have_text("Template guardado para este proyecto.")
        assert projects.read_project(path)["options"]["signature"] is None
