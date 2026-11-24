"""Pixel-level checks for stored PNG watermarks and actual Studio exports."""
import base64
from pathlib import Path
import shutil
import subprocess
from urllib.error import HTTPError

import cv2
import numpy as np
import pytest

import projects
import render_clips
import watermark
from test_dashboard import request, running_server, server  # noqa: F401
from test_live_integration import probe_streams
from test_studio_navigation import chromium_page  # noqa: F401


@pytest.fixture
def logo():
    pixels = np.zeros((40, 80, 4), dtype=np.uint8)
    pixels[:, :, 2] = 255
    pixels[:, :40, 3] = 255
    pixels[:, 40:60, 3] = 128
    success, png = cv2.imencode(".png", pixels)
    assert success
    return png.tobytes()


def encoded(png):
    return base64.b64encode(png).decode("ascii")


def configure(root, logo, **options):
    return watermark.Store(root).save({**watermark.DEFAULTS, "enabled": True, **options}, encoded(logo))


def test_store_persists_valid_png_and_detects_missing_file(tmp_path, logo):
    saved = configure(tmp_path, logo, position="center", size=23)
    store = watermark.Store(tmp_path)
    assert store.read() == saved
    assert store.preview(saved["image_id"]) == logo
    assert store.for_render().dimensions == (80, 40)
    image = store.for_render().image
    image.write_bytes(b"damaged PNG")
    with pytest.raises(watermark.WatermarkError, match="cambió o está dañada"):
        store.for_render()
    image.unlink()
    with pytest.raises(watermark.WatermarkError, match="Falta la imagen"):
        store.for_render()
    store.save({**saved, "enabled": False})
    assert store.for_render() is None


@pytest.mark.parametrize(("change", "upload"), [
    ({"image_id": "../outside"}, None),
    ({"position": "other"}, None),
    ({"size": 99}, None),
    ({"opacity": 0}, None),
    ({"margin": -1}, None),
    ({"enabled": True}, None),
    ({}, "not base64!"),
    ({}, encoded(b"not a PNG")),
    ({}, encoded(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (10_000).to_bytes(4, "big") * 2 + b"\x00" * 20)),
])
def test_store_rejects_invalid_settings_or_image(tmp_path, change, upload):
    store = watermark.Store(tmp_path)
    with pytest.raises(watermark.WatermarkError):
        store.save({**watermark.DEFAULTS, **change}, upload)
    assert not store.directory.exists()


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg and FFprobe required")
@pytest.mark.parametrize(("aspect", "layout", "position", "limit"), [
    ("16:9", "single", "top-left", (1280, 720)),
    ("16:9", "single", "bottom-right", (1920, 1080)),
    ("9:16", "split", "top-right", (1080, 1920)),
    ("9:16", "stacked", "center", (1080, 1920)),
    ("1:1", "single", "bottom-left", (1080, 1080)),
])
def test_real_overlay_pixels(tmp_path, logo, monkeypatch, aspect, layout, position, limit):
    source = gray_source(tmp_path)
    configure(tmp_path, logo, position=position, opacity=60, margin=7, size=20)
    mark = watermark.Store(tmp_path).for_render()
    monkeypatch.setitem(render_clips.ASPECTS, aspect, (*limit, *render_clips.ASPECTS[aspect][2:]))
    clip = render_clips.render({"start": 0, "end": 1}, [{"word": "Watermark", "start": 0, "end": 1}],
                               source, 640, 360, tmp_path, aspect, out_dir=tmp_path / "clips",
                               layout=layout, facecam_box=[0, 0, 0.3, 0.3],
                               caption={"highlight": "#FFFFFF"}, mark=mark)
    frame = extract_frame(clip)
    assert frame.shape[:2] == limit[::-1]
    assert set(probe_streams(clip)) == {"audio", "video"}
    assert "Watermark" in clip.with_suffix(".srt").read_text()
    assert np.count_nonzero(frame.min(axis=2) > 190) > 20  # burnt captions
    w, h, x, y = mark.geometry(*limit)
    yy, xx = np.where(red(frame) > 20)
    assert (xx.min(), yy.min(), xx.max() + 1, yy.max() + 1) == pytest.approx(
        (x, y, x + w * 0.75, y + h), abs=3)
    assert w / h == pytest.approx(2, abs=0.03)
    full, half, clear = (red(frame)[y + h // 2, x + int(w * part)] for part in (.25, .625, .875))
    assert full == pytest.approx(255 * .60, abs=16)
    assert half == pytest.approx(full / 2, abs=14)
    assert abs(clear) < 8


def gray_source(tmp_path):
    path = tmp_path / "gray source.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=0x404040:s=640x360:r=30:d=3", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True, timeout=15)
    return path


def extract_frame(clip):
    png = subprocess.check_output(["ffmpeg", "-v", "error", "-ss", "0.3", "-i", str(clip),
                                   "-frames:v", "1", "-threads", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                                  timeout=15)
    return cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)


def red(frame):
    pixels = frame.astype(np.int16)
    return pixels[:, :, 2] - np.maximum(pixels[:, :, 0], pixels[:, :, 1])


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg and FFprobe required")
def test_batch_regeneration_keeps_original_and_exports_to_publications(tmp_path, logo, server):
    app, base = server
    source = gray_source(tmp_path)
    project = projects.create_project(tmp_path, str(source), {"fit": "contain"})
    path = projects.project_path(tmp_path, project["id"])
    projects.atomic_json(path / "transcript.json", {"video": str(source), "duration": 3, "words": []})
    projects.atomic_json(path / "segments.json", [
        {"start": 0, "end": 1, "overall": 8}, {"start": 1, "end": 2, "overall": 8}])
    original = [Path(p) for p in projects.run_project(path, action="render")["last_outputs"]]
    assert red(extract_frame(original[0])).max() < 8
    contents = [p.read_bytes() for p in original]
    saved = configure(tmp_path, logo)
    marked = [Path(p) for p in projects.run_project(path, action="render")["last_outputs"]]
    assert len(marked) == 2 and set(marked).isdisjoint(original)
    assert all(red(extract_frame(p)).max() > 130 for p in marked)
    assert [p.read_bytes() for p in original] == contents
    state = request(base, "/api/tiktok/state", {}, app.app.token)
    assert {p.name for p in marked} <= {clip["filename"] for clip in state["clips"]}
    preview = request(base, "/api/tiktok/inspect", {
        "project": project["id"], "filename": marked[0].name}, app.app.token)
    assert preview["width"] == 1920
    store = watermark.Store(tmp_path)
    store.for_render().image.unlink()
    with pytest.raises(watermark.WatermarkError, match="Falta la imagen"):
        projects.run_project(path, action="render")
    assert set((path / "clips").glob("*.mp4")) == set(original + marked)
    store.save({**saved, "enabled": False})
    assert projects.run_project(path, action="render")["last_outputs"] == [str(p) for p in original]


@pytest.mark.parametrize(("width", "height"), [(1920, 1080), (1366, 768), (390, 844)])
def test_watermark_panel_upload_preview_replace_and_remove(tmp_path, logo, chromium_page, width, height):
    page, expect = chromium_page
    page.set_viewport_size({"width": width, "height": height})
    with running_server(tmp_path) as (app, base):
        with pytest.raises(HTTPError) as error:
            request(base, "/api/watermark", {"settings": watermark.DEFAULTS})
        assert error.value.code == 403
        page.goto(base)
        page.locator("#show-settings").click()
        expect(page.locator(".settings-subnav button")).to_have_count(9)
        assert page.evaluate("document.documentElement.scrollWidth === innerWidth")
        page.locator("#settings-query").fill("marca de agua")
        expect(page.locator(".settings-subnav button:visible")).to_have_count(1)
        page.locator("[data-setting=appearance]").click()
        with page.expect_file_chooser() as selection:
            page.get_by_role("button", name="Cambiar imagen").click()
        selection.value.set_files({"name": "logo.png", "mimeType": "image/png", "buffer": logo})
        expect(page.locator("#watermark-thumbnail")).to_be_visible()
        page.get_by_label("Activar marca de agua").check()
        page.locator("#watermark-form select[name=position]").select_option("bottom-left")
        page.locator("#watermark-form").get_by_label("Opacidad (%)").fill("50")
        page.get_by_label("Tamaño (% del ancho)").fill("26")
        page.locator(".watermark-advanced summary").click()
        page.get_by_label("Margen (% del lado corto)").fill("8")
        expect(page.locator("#watermark-overlay")).to_have_css("opacity", "0.5")
        assert page.locator("#watermark-overlay").evaluate("(node) => node.offsetTop > 100")
        page.get_by_role("button", name="Guardar cambios").click()
        expect(page.locator("#watermark-status")).to_contain_text("guardada")
        saved = request(base, "/api/watermark")
        assert saved["position"] == "bottom-left"
        assert page.request.get(base + "/watermark-image/" + saved["image_id"]).body() == logo
    with running_server(tmp_path) as (_, base):
        page.goto(base)
        page.locator("#show-settings").click()
        page.locator("[data-setting=appearance]").click()
        expect(page.locator("#watermark-overlay")).to_be_visible()
        expect(page.locator("#watermark-form").get_by_label("Opacidad (%)")).to_have_value("50")
        image = cv2.imdecode(np.frombuffer(logo, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        replacement = cv2.imencode(".png", cv2.flip(image, 1))[1].tobytes()
        saved_url = "/watermark-image/" + saved["image_id"]
        for payload, problem in (
            (b"x" * (watermark.MAX_BYTES + 1), "2 MiB"),
            (cv2.imencode(".png", np.zeros((4, 2049, 3), np.uint8))[1].tobytes(), "2048 × 2048"),
            (logo[:32], "dañada"),
        ):
            with page.expect_file_chooser() as selection:
                page.get_by_role("button", name="Cambiar imagen").click()
            selection.value.set_files({"name": "replacement.png", "mimeType": "image/png", "buffer": payload})
            expect(page.locator("#watermark-error")).to_contain_text(problem)
            expect(page.locator("#watermark-thumbnail")).to_have_attribute("src", saved_url)
            assert request(base, "/api/watermark") == saved

        with page.expect_file_chooser() as selection:
            page.get_by_role("button", name="Cambiar imagen").click()
        selection.value.set_files({"name": "replacement.png", "mimeType": "image/png", "buffer": replacement})
        expect(page.locator("#watermark-status")).to_contain_text("Imagen lista")
        assert request(base, "/api/watermark") == saved
        expect(page.locator("#watermark-thumbnail")).to_have_attribute("src", "data:image/png;base64," + encoded(replacement))
        page.locator("#watermark-aspect").select_option("9:16")
        expect(page.locator("#watermark-stage")).to_have_attribute("data-aspect", "9:16")
        expect(page.locator("#watermark-overlay")).to_be_visible()
        with page.expect_response(lambda r: r.url.endswith("/api/watermark") and r.request.method == "POST") as submitted:
            page.get_by_role("button", name="Guardar cambios").click()
        assert submitted.value.ok
        current = request(base, "/api/watermark")
        current_id = current["image_id"]
        assert saved["image_id"] != current_id
        assert current == {**saved, "image_id": current_id}
        assert page.request.get(base + "/watermark-image/" + current_id).body() == replacement
        page.reload()
        expect(page.locator("#watermark-thumbnail")).to_have_attribute("src", "/watermark-image/" + current_id)
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.get_by_role("button", name="Quitar imagen").click()
        expect(page.locator("#watermark-overlay")).to_be_visible()
        page.once("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name="Quitar imagen").click()
        expect(page.locator("#watermark-overlay")).to_be_hidden()
        page.get_by_role("button", name="Guardar cambios").click()
        expect(page.locator("#watermark-status")).to_contain_text("guardada")
        assert request(base, "/api/watermark")["image_id"] is None
        assert page.request.get(base + "/watermark-image/" + saved["image_id"]).status == 404


def test_failed_settings_write_preserves_previous_image(tmp_path, logo, monkeypatch):
    before = configure(tmp_path, logo, position="bottom-left")
    store = watermark.Store(tmp_path)
    write = watermark._write
    decoded = cv2.imdecode(np.frombuffer(logo, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    new = cv2.imencode(".png", cv2.flip(decoded, 1))[1].tobytes()

    def fail_config(path, data):
        if path.name == "settings.json":
            raise PermissionError("simulated locked settings")
        write(path, data)

    monkeypatch.setattr(watermark, "_write", fail_config)
    with pytest.raises(watermark.WatermarkError, match="permisos"):
        store.save(before, encoded(new))
    assert store.read() == before
    assert store.preview(before["image_id"]) == logo


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg and FFprobe required")
def test_replacement_appears_in_all_camera_modes_and_landscape(tmp_path, logo, server, monkeypatch):
    import smart_camera

    httpd, base = server
    source = gray_source(tmp_path)
    previous = configure(tmp_path, logo, position="top-left", opacity=76, margin=7, size=22)
    original = render_clips.render({"start": 0, "end": 1}, [], source, 640, 360, tmp_path, "16:9",
                                   out_dir=tmp_path / "original", mark=watermark.Store(tmp_path).for_render())
    before = original.read_bytes()
    assert red(extract_frame(original)).max() > 130
    pixels = np.zeros((24, 60, 4), dtype=np.uint8)
    pixels[:, :, 1] = pixels[:, :, 3] = 255
    replacement = cv2.imencode(".png", pixels)[1].tobytes()
    settings = request(base, "/api/watermark", {"settings": previous, "image_data": encoded(replacement)}, httpd.app.token)
    assert settings == {**previous, "image_id": settings["image_id"]}
    mark = watermark.Store(tmp_path).for_render()
    for aspect, requested, applied, faces in (
        ("9:16", "auto", "smart", [270]),
        ("9:16", "smart", "smart", [270]),
        ("9:16", "split", "split", [130, 500]),
        ("9:16", "stable", "stable", []),
        ("16:9", "auto", None, []),
    ):
        monkeypatch.setattr(render_clips, "detect_track", lambda *_: [(i / 5, faces, False) for i in range(5)])
        limit = render_clips.ASPECTS[aspect][:2]
        clip = render_clips.render(
            {"start": 0, "end": 1}, [], source, 640, 360, tmp_path, aspect, camera_mode=requested,
            out_dir=tmp_path / "new" / aspect.replace(":", "-") / requested, mark=mark, timeout=45,
        )
        frame = extract_frame(clip)
        w, h, x, y = mark.geometry(*limit)
        blue, green, crimson = map(int, frame[y + h // 2, x + w // 2])
        assert green - max(blue, crimson) > 140
        assert red(frame).max() < 15
        if applied:
            report = smart_camera.saved_motion(clip)
            assert report["applied"] == applied
            sar = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                          "-show_entries", "stream=sample_aspect_ratio", "-of", "csv=p=0", str(clip)])
            assert sar.strip() == b"1:1"
            assert report["av_start_skew"] < .04 and report["av_end_skew"] < .04
        assert float(probe_streams(clip)["video"]["duration"]) == pytest.approx(1, abs=.04)
    assert original.read_bytes() == before
