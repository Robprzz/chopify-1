"""Deterministic framing and real FFmpeg export checks."""
from fractions import Fraction
import json
from pathlib import Path
import shutil
import subprocess

import pytest

import render_clips
import smart_camera


def ffmpeg(*args):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and FFprobe are required")
    return subprocess.run(["ffmpeg", "-nostdin", "-v", "error", *map(str, args)],
                          capture_output=True, check=True, timeout=30).stdout


def source(path, rate=30, duration=3, filter_=None, picture=None):
    args = ["-f", "lavfi", "-i", picture or f"testsrc2=s=640x360:r={rate}",
            "-f", "lavfi", "-i", "sine=sample_rate=48000", "-t", duration]
    if filter_:
        args += ["-vf", filter_, "-fps_mode", "vfr"]
    ffmpeg(*args, "-threads", "2", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", path)
    return path


def test_subject_hysteresis_occlusion_and_scene_cut():
    # An unrelated face repeatedly appears while the subject advances slowly.
    timeline = [(i / 5, [330 + i * 12, 1000], False) for i in range(12)]
    timeline += [(i / 5, [1000], False) for i in range(12, 16)]
    timeline += [(3.2, [510, 1000], False), (3.4, [501, 1000], False),
                 (3.6, [1150], True), (3.8, [1150], False)]
    result = smart_camera.plan(timeline, 1280, 400, 4)
    assert result.reliable and result.subject_changes == 0 and result.scene_cuts == 1
    assert result.points[17][1] < 650 and result.points[18][1] == 1080
    assert result.peak_speed <= .22 and result.peak_acceleration <= .58
    assert all(200 <= x <= 1080 for _, x, _, _ in result.points)
    assert max(x for _, x, _, _ in result.points[:15]) < 650


def test_stable_split_and_static_discrimination():
    pair = [(i / 5, [250 + i % 3, 710 - i % 4], False) for i in range(15)]
    assert smart_camera.plan(pair, 960, 360, 3).pairs == pytest.approx((251/960, 708/960), abs=.002)
    assert not smart_camera.plan([(0, [], False)], 960, 360, 1).reliable
    assert smart_camera.excess_freezes([(0, 3)], [(0, 3)]) == []
    assert smart_camera.excess_freezes([(1, 2.3)], [(0, .7)]) == [[1, 2.3]]


@pytest.mark.parametrize("rate", [24, 25, 30, 60])
def test_cfr_delivery_rate_and_square_pixels(tmp_path, rate):
    path = source(tmp_path / "source.mp4", rate=rate, duration=1.2)
    assert smart_camera.source_rate(path, 0, 20) == (Fraction(rate), "cfr")
    output = render_clips.render({"start": 0, "end": 1.2}, [], path, 640, 360, tmp_path, "9:16",
                                 out_dir=tmp_path / "render", camera_mode="stable", timeout=45)
    meta = json.loads(output.with_suffix(".meta.json").read_text())["motion"]
    assert meta["applied"] == "stable"
    assert meta["frames"] == pytest.approx(1.2 * rate, abs=1)
    assert meta["max_frame_jitter"] < .0001


def test_variable_clock_native_picture_and_horizontal(tmp_path):
    path = source(tmp_path / "variable.mp4", filter_="select='not(mod(n,5)) + not(mod(n,5)-1) + not(mod(n,5)-4)'")
    rate, cadence = smart_camera.source_rate(path, 0, 20)
    assert cadence == "vfr"
    out = render_clips.render({"start": 0, "end": 2.9}, [], path, 640, 360, tmp_path, "9:16",
                              out_dir=tmp_path / "vertical", camera_mode="stable", timeout=45)
    report = json.loads(out.with_suffix(".meta.json").read_text())["motion"]
    assert report["source_cadence"] == "vfr" and report["fps"] == str(rate)
    assert report["max_frame_jitter"] < .0001
    horizontal = render_clips.render({"start": 0, "end": 2.9}, [], path, 640, 360, tmp_path, "16:9",
                                     out_dir=tmp_path / "horizontal", timeout=45)
    assert render_clips.ffprobe_dims(horizontal) == (1920, 1080)


def test_real_render_moves_crop_between_detections(tmp_path, monkeypatch):
    path = source(tmp_path / "gradient.mp4", duration=3,
                  picture="nullsrc=s=640x360:r=30,geq=lum=16+200*X/W:cb=128:cr=128")
    observations = [(i / 5, [170 + 15 * i], False) for i in range(15)]
    monkeypatch.setattr(render_clips, "detect_track", lambda *_: observations)
    output = render_clips.render({"start": 0, "end": 3}, [], path, 640, 360, tmp_path, "9:16",
                                 out_dir=tmp_path / "render", camera_mode="smart", timeout=45)
    data = json.loads(output.with_suffix(".meta.json").read_text())
    assert data["motion"]["applied"] == "smart"
    raw = ffmpeg("-i", output, "-an", "-vf", "scale=2:2,format=gray", "-f", "rawvideo", "-threads", "1", "-")
    luma = [sum(raw[i:i+4]) / 4 for i in range(0, len(raw), 4)]
    delta = [b - a for a, b in zip(luma, luma[1:])]
    assert luma[-1] - luma[0] > 20
    assert max(delta) <= 4
    assert sum(value > 0 for value in delta) > len(observations) * 2


def test_fallback_preserves_sources_and_revalidates_output(tmp_path, monkeypatch):
    path = source(tmp_path / "source.mp4")
    original = path.read_bytes()
    observations = [(i / 5, [170 + 15 * i], False) for i in range(15)]
    monkeypatch.setattr(render_clips, "detect_track", lambda *_: observations)
    calls = []

    def freeze(file, *args):
        calls.append(file)
        return [(1, 2.3)] if len(calls) == 1 else []
    monkeypatch.setattr(smart_camera, "freeze_spans", freeze)
    out = render_clips.render({"start": 0, "end": 3}, [], path, 640, 360, tmp_path, "9:16",
                              out_dir=tmp_path / "render", camera_mode="smart", timeout=45)
    report = smart_camera.saved_motion(out)
    assert report["applied"] == "stable" and report["fallback"]["intervals"] == [[1, 2.3]]
    assert not report["unexplained_freezes"] and path.read_bytes() == original
    assert len(calls) == 3


def test_explicit_split_uses_verified_pair_or_stable_fallback(tmp_path, monkeypatch):
    observations = [(i / 5, [140, 505], False) for i in range(6)]
    monkeypatch.setattr(render_clips, "detect_track", lambda *_: observations)
    for name, expected in (("pair", "split"), ("single", "stable")):
        path = source(tmp_path / f"{name}.mp4", duration=1.2)
        out = render_clips.render({"start": 0, "end": 1.2}, [], path, 640, 360, tmp_path, "9:16",
                                  out_dir=tmp_path / name, camera_mode="split", timeout=45)
        assert smart_camera.saved_motion(out)["applied"] == expected
        observations[:] = [(i / 5, [140], False) for i in range(6)]
