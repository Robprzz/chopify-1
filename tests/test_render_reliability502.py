import json
import shutil
import subprocess

import pytest

import render_clips as render
from test_watermark import configure, gray_source, logo  # noqa: F401
from watermark import Store
from test_studio_navigation import chromium_page  # noqa: F401

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="FFmpeg required")


@pytest.mark.parametrize("with_mark", [False, True])
def test_real_stacked_and_invalid_crop(tmp_path, logo, with_mark):
    original = subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","color=s=2x2:d=0.1",
        "-vf","crop=0:2:0:0","-f","null","-"],capture_output=True,text=True)
    assert original.returncode and "Invalid too big or non positive size" in original.stderr
    assert render.crop_for(2,2,1080,1920)[:2] == (2,2)
    path = tmp_path / "Grabación 測試 [1], O'Brien"
    path.mkdir()
    source = path / "grabación a 60 fps.mp4"
    subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","testsrc2=s=640x360:r=60:d=1.5",
        "-f","lavfi","-i","sine=frequency=440:duration=1.5","-c:v","libx264","-c:a","aac",
        "-shortest",str(source)],check=True)
    if with_mark:
        configure(path, logo)
    output = render.render({"id":"001","start":0,"end":1}, [{"word":"Árabe 日本語","start":0.1,"end":0.6}],
        source,640,360,path,"9:16",out_dir=path/"salida",layout="stacked",
        facecam_box=(0.7,0.6,0.28,0.35),allow_model_download=False,mark=Store(path).for_render())
    data = json.loads(subprocess.check_output(["ffprobe","-v","error","-show_streams","-of","json",str(output)]))
    video, audio = data["streams"]
    assert (video["width"],video["height"],video["sample_aspect_ratio"]) == (1080,1920,"1:1")
    assert video["avg_frame_rate"] == "60/1"
    assert abs(float(video["duration"])-float(audio["duration"])) < .06
    assert "日本語" in output.with_suffix(".ass").read_text(encoding="utf-8-sig")


def test_ffmpeg_diagnostic_identifies_ass_without_blame(tmp_path, monkeypatch, capsys):
    source = gray_source(tmp_path)
    run = subprocess.run

    def missing_ass(command, *args, **kwargs):
        if "-filter_complex" in command:
            command = [part.replace("subtitles=_caption.ass", "subtitles=absent.ass") for part in command]
        return run(command, *args, **kwargs)

    monkeypatch.setattr(render.subprocess, "run", missing_ass)
    args = ({"id":"001","start":0,"end":1}, [], source,640,360,tmp_path,"9:16")
    with pytest.raises(render.RenderFailure) as error:
        render.render(*args,out_dir=tmp_path/"clips",camera_mode="stable")
    assert error.value.alternative == "subtitles"
    assert "Parsed_subtitles" in capsys.readouterr().err
    assert not list((tmp_path/"clips").glob("*.mp4"))
    output = render.render(*args,out_dir=tmp_path/"clips",camera_mode="stable",burn_subtitles=False)
    assert output.is_file() and output.with_suffix(".srt").is_file()
    assert not output.with_suffix(".ass").exists()
    again = render.render(*args,out_dir=tmp_path/"clips",camera_mode="stable",burn_subtitles=False)
    assert output != again and output.is_file()


def test_windows_signed_exit_code_and_conservative_classification():
    assert render.ffmpeg_failure("Invalid argument",4294967274,layout="stacked",mark=None).alternative is None
    error = render.ffmpeg_failure("[Parsed_crop_0] Invalid size",4294967274,layout="stacked",mark=None)
    assert error.alternative == "stable" and "(-22)" in str(error)



def test_http_recovery_requires_confirmation_and_keeps_project_options(tmp_path, monkeypatch, chromium_page):
    import projects
    from urllib.error import HTTPError
    from test_dashboard import request, running_server

    project = projects.create_project(tmp_path, "source.mp4", {"aspect":"9:16","layout":"stacked"})
    path = tmp_path / project["id"]
    project.update(status="review",stage="render recovery",
                   render_recovery={"alternative":"stable","pick":"3","preview":False})
    projects.save_project(path, project)
    with running_server(tmp_path) as (server, base):
        calls = []
        monkeypatch.setattr(server.app, "_run", lambda *args: calls.append(args))
        endpoint = f"/api/projects/{project['id']}/render-recovery"
        for payload in ({"alternative":"stable"}, {"alternative":"watermark","confirmed":True}):
            with pytest.raises(HTTPError):
                request(base, endpoint, payload, server.app.token)
        page, expect = chromium_page
        page.goto(base)
        page.locator("#show-projects").click()
        page.locator(f"#projects [data-id='{project['id']}']").click()
        button = page.locator("#render-recovery")
        expect(button).to_have_text("Exportar con encuadre vertical estable")
        page.once("dialog", lambda dialog: dialog.dismiss())
        button.click()
        assert not calls
        page.once("dialog", lambda dialog: dialog.accept())
        with page.expect_response(lambda response: response.url.endswith(endpoint)) as response:
            button.click()
        assert response.value.ok
        server.app.futures[project["id"]].result(timeout=5)
        assert calls == [(path,"render",False,"3","stable")]
        assert projects.read_project(path)["options"]["layout"] == "stacked"



def test_display_matrix_rotation_prevents_stacked_crop_failure(tmp_path):
    source = gray_source(tmp_path)
    rotated = tmp_path / "screen rotated.mp4"
    help_text = subprocess.check_output(["ffmpeg","-hide_banner","-h","full"], text=True)
    modern = "-display_rotation" in help_text
    rotation = ["-display_rotation:v:0","90"] if modern else []
    tags = [] if modern else ["-metadata:s:v:0","rotate=90"]
    subprocess.run(["ffmpeg","-v","error",*rotation,"-i",str(source),"-c","copy",*tags,str(rotated)],check=True)
    width, height = render.ffprobe_dims(rotated)
    assert (width,height) == (360,640)
    old, _ = render.stacked_graph(640,360,1080,1920,.34,(0,0,1,.4))
    (tmp_path/"_caption.ass").write_text("",encoding="utf-8")
    bad = subprocess.run(["ffmpeg","-v","error","-i",str(rotated),"-filter_complex_threads","1",
        "-filter_complex",old.replace(",subtitles=_caption.ass",""),"-map","[v]","-t","0.5","-f","null","-"],
        text=True,capture_output=True)
    assert bad.returncode and "Parsed_crop" in bad.stderr
    output = render.render({"id":"001","start":0,"end":1}, [],rotated,width,height,tmp_path,"9:16",
        out_dir=tmp_path/"clips",layout="stacked",facecam_box=(0,0,1,.4),allow_model_download=False)
    assert render.ffprobe_dims(output) == (1080,1920)
