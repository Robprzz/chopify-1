import hashlib
import json
from fractions import Fraction
from pathlib import Path
import subprocess

import pytest

import projects
import smart_camera as camera
from test_dashboard import request, running_server
from test_studio_navigation import chromium_page  # noqa: F401


def make_media(path, color, filters=None, size="1080x1920", audio_offset=0):
    command = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
               f"testsrc2=size={size}:rate=30", "-itsoffset", str(audio_offset),
               "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    if filters:
        command += ["-vf", filters, "-fps_mode", "vfr"]
    subprocess.run([*command, "-t", "1.2", "-c:v", "libx264", "-threads", "2",
                    "-pix_fmt", "yuv420p", "-color_range", color, "-color_primaries","bt709", "-colorspace","bt709","-color_trc","bt709", "-c:a", "aac", str(path)],
                   check=True, capture_output=True)


@pytest.mark.parametrize("color,pixels", [("tv", "yuv420p"), ("pc", "yuvj420p")])
def test_real_vertical_range(tmp_path, color, pixels):
    path = tmp_path / "vertical.mp4"
    make_media(path, color)
    media = camera.delivery_check(path, Fraction(30), 1.2, None)
    assert media["pix_fmt"] == pixels
    assert media["color_range"] == color and media["frames"] == 36
    assert media["av_end_skew"] < .06


@pytest.mark.parametrize("field,value,message", [
    ("width", 720, "ancho"), ("height", 1280, "alto"), ("sample_aspect_ratio", "2:1", "SAR"),
    ("display_aspect_ratio", "16:9", "DAR"), ("codec_name", "hevc", "códec"),
    ("pix_fmt", "yuv420p10le", "píxeles"), ("color_range", "tv", "rango"),
])
def test_invalid_stream_contract(field, value, message):
    video = {"width":1080,"height":1920,"sample_aspect_ratio":"1:1","display_aspect_ratio":"9:16",
             "codec_name":"h264","pix_fmt":"yuvj420p","color_range":"pc"}
    video[field] = value
    with pytest.raises(camera.DeliveryError, match=message):
        camera.delivery_stream(video)


def test_duration_rate_and_corruption(tmp_path):
    path = tmp_path / "vertical.mp4"
    make_media(path,"tv")
    with pytest.raises(camera.DeliveryError, match="duración"):
        camera.delivery_check(path,Fraction(30),3,None)
    with pytest.raises(camera.DeliveryError, match="frecuencia"):
        camera.delivery_check(path,Fraction(60),1.2,None)
    path.write_bytes(b"not an mp4")
    with pytest.raises(camera.DeliveryError, match="FFprobe"):
        camera.delivery_check(path,Fraction(30),1.2,None)


def test_audio_skew_and_packet_gaps(tmp_path):
    path = tmp_path / "vertical.mp4"
    make_media(path,"tv",audio_offset=.4)
    with pytest.raises(camera.DeliveryError,match="fuera de sincronía"):
        camera.delivery_check(path,Fraction(30),1.2,None)
    make_media(path,"tv",filters="select='not(between(n,9,13))'")
    probe = subprocess.check_output(["ffprobe","-v","error","-show_entries","stream=avg_frame_rate",
        "-select_streams","v:0","-of","json",str(path)])
    rate = Fraction(json.loads(probe)["streams"][0]["avg_frame_rate"])
    with pytest.raises(camera.DeliveryError,match="timestamps"):
        camera.delivery_check(path,rate,1.2,None)


@pytest.mark.parametrize("color,size", [("tv","640x360"),("pc","360x640")])
def test_chromium_local_source_clip_publish_draft(tmp_path,chromium_page,monkeypatch,color,size):
    page,expect = chromium_page
    source = tmp_path / "grabación 測試 O'Brien [1].mp4"
    make_media(source,color,size=size)
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    with running_server(tmp_path / "projects") as (httpd,base):
        real_run = httpd.app._run

        def fake_recognition(media, folder, *_args, **_kwargs):
            projects.atomic_json(folder/"transcript.json", {"video":str(media),"duration":1.2,
                "words":[{"word":"Hola.","start":.12,"end":.55}],"model":"fixture"})

        def worker(path, action, preview, pick, recovery):
            if action != "prepare":
                return real_run(path,action,preview,pick,recovery)
            projects.run_project(path,action="prepare")
            projects.atomic_json(path/"segments.json",[{"start":0,"end":1.1,"hook":"Prueba local","overall":9}])

        monkeypatch.setattr(projects.dt,"transcribe",fake_recognition)
        monkeypatch.setattr(httpd.app,"_run",worker)
        page.goto(base)
        page.locator("#show-projects").click()
        page.locator("#new-project").click()
        page.locator("#sources").fill(str(source))
        page.locator("#new-aspect").select_option("9:16")
        with page.expect_response(lambda response: response.url.endswith("/api/batch")) as submitted:
            page.locator("#create-form button[type=submit]").click()
        project = submitted.value.json()["projects"][0]["id"]
        expect(page.locator("#status")).to_contain_text("En revisión", timeout=30000)
        page.locator("#settings-form [name=camera_mode]").select_option("stable")
        page.locator("#render").click()
        expect(page.locator("#status")).to_contain_text("Completado",timeout=60000)
        detail = request(base,f"/api/projects/{project}")
        out = Path(detail["project"]["last_outputs"][0])
        assert hashlib.sha256(source.read_bytes()).hexdigest() == original
        motion = camera.saved_motion(out)
        assert motion["source_color"]["color_range"] == color
        assert motion["pix_fmt"] in ("yuvj420p","yuv420p")
        assert motion["av_end_skew"] < .06
        page.locator("#show-clips-library").click()
        page.locator("#clip-library").get_by_role("button",name="Editar clip").click()
        expect(page.locator("#clip-editor-media")).to_contain_text("1080 × 1920")
        assert page.locator("#clip-editor-video").evaluate("video => video.play().then(() => true)")
        page.locator("#clip-copy-form [name=caption]").fill("Prueba local")
        page.locator("#clip-schedule").click()
        expect(page.locator("#calendar-caption")).to_have_value("Prueba local")


def test_rejected_export_is_retained_outside_clip_catalog(tmp_path, monkeypatch):
    import render_clips
    source = tmp_path/"source.mp4"
    make_media(source, "pc", size="640x360")
    out = tmp_path/"clips"
    out.mkdir()
    previous = out/"previous.mp4"
    previous.write_bytes(b"previous export")

    def reject(*_):
        raise camera.DeliveryError("Control vertical: códec incompatible.")

    monkeypatch.setattr(camera, "delivery_check", reject)
    with pytest.raises(render_clips.RenderFailure) as error:
        render_clips.render({"id":"fixture","start":0,"end":1}, [], source,640,360,tmp_path,"9:16",out_dir=out,
                            layout="stacked",facecam_box=(0,0,.4,.4),allow_model_download=False)
    assert error.value.alternative == "stable"
    assert list(out.glob("*.mp4")) == [previous]
    assert previous.read_bytes() == b"previous export"
    assert render_clips.ffprobe_dims(out/".vertical-diagnostic"/"last-rejected.mp4") == (1080,1920)
