"""Exercise project ownership and reversible assets through Studio's real entry points."""
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

import projects
import render_clips
from test_dashboard import request, running_server
from test_studio_navigation import chromium_page  # noqa: F401
from test_watermark import gray_source


def test_project_and_clip_trash_restores_originals_across_restart(tmp_path):
    with running_server(tmp_path) as (httpd, base):
        token = httpd.app.token
        project = request(base, "/api/projects", {"source": "video.mp4"}, token)["id"]
        folder = tmp_path / project / "clips"
        folder.mkdir()
        (folder / "clip.mp4").write_bytes(b"original")
        (folder / "clip.srt").write_bytes(b"subtitles")
        endpoint = f"/api/projects/{project}/manage"
        assert request(base, endpoint, {"action": "rename", "name": "Entrevista"}, token)["name"] == "Entrevista"
        assert request(base, endpoint, {"action": "archive"}, token)["archived"] is True
        assert request(base, endpoint, {"action": "unarchive"}, token)["archived"] is False
        with pytest.raises(HTTPError) as unauthorized:
            request(base, "/api/trash/clip", {"project": project, "filename": "clip.mp4"})
        assert unauthorized.value.code == 403
        clip = request(base, "/api/trash/clip", {"project": project, "filename": "clip.mp4"}, token)
        assert not request(base, f"/api/projects/{project}")["files"]
        with pytest.raises(HTTPError) as unavailable:
            urlopen(base + f"/media/{project}/clips/clip.srt").close()
        assert unavailable.value.code == 404
        assert not request(base, "/api/tiktok/state", {}, token)["clips"]
        assert (folder / "clip.mp4").read_bytes() == b"original"
        trashed = request(base, endpoint, {"action": "trash"}, token)
        assert not request(base, "/api/projects")["projects"]

    with running_server(tmp_path) as (httpd, base):
        assert len(request(base, "/api/trash")["items"]) == 2
        token = httpd.app.token
        request(base, "/api/trash/restore", {"id": trashed["id"]}, token)
        assert request(base, f"/api/projects/{project}")["project"]["name"] == "Entrevista"
        assert not request(base, f"/api/projects/{project}")["files"]
        request(base, "/api/trash/restore", {"id": clip["id"]}, token)
        assert len(request(base, "/api/tiktok/state", {}, token)["clips"]) == 1
        with urlopen(base + f"/media/{project}/clips/clip.mp4") as restored:
            assert restored.read() == b"original"


def test_context_and_real_portrait_export_follow_visible_settings(tmp_path, chromium_page):
    page, expect = chromium_page
    source = gray_source(tmp_path)
    with running_server(tmp_path) as (httpd, base):
        project = request(base, "/api/projects", {"source": str(source)}, httpd.app.token)["id"]
        path = projects.project_path(tmp_path, project)
        projects.atomic_json(path / "transcript.json", {"video": str(source), "duration": 3, "words": []})
        projects.atomic_json(path / "segments.json", [{"start": 0, "end": 1, "overall": 8.5}])
        page.goto(base)
        page.locator("#show-projects").click()
        page.locator(f"#projects [data-id='{project}']").click()
        expect(page.locator("#project-view")).to_be_visible()
        page.locator("#settings-form [name=aspect]").select_option("9:16")
        page.locator("#settings-form [name=camera_mode]").select_option("stable")
        page.locator("#show-settings").click()
        page.locator("#show-ai-settings").click()
        expect(page.locator("#editorial-config")).to_be_visible()
        expect(page.locator("#editorial-result")).to_be_hidden()
        page.locator("#show-projects").click()
        page.get_by_role("button", name="Continuar edición").click()
        expect(page.locator("#settings-form [name=aspect]")).to_have_value("9:16")
        with page.expect_response(lambda r: r.url.endswith(f"/{project}/run")) as submitted:
            page.locator("#render").click()
        assert submitted.value.ok
        expect(page.locator("#status")).to_contain_text("Completado", timeout=30000)
        detail = request(base, f"/api/projects/{project}")
        assert detail["project"]["options"]["aspect"] == "9:16"
        output = next(Path(file) for file in detail["project"]["last_outputs"])
        assert render_clips.ffprobe_dims(output) == (1080, 1920)
        assert render_clips.smart_camera.saved_motion(output)["applied"] == "stable"
        page.locator("#show-clips-library").click()
        page.locator("#clip-library button.thumb").first.click()
        expect(page.locator("#clip-preview-dialog")).to_be_visible()
        expect(page.locator("#clip-preview-video")).to_have_attribute("src", f"/media/{project}/clips/{output.name}")
        page.locator("#clip-preview-dialog").get_by_role("button", name="Cerrar").click()
        page.reload()
        page.locator("#show-projects").click()
        expect(page.get_by_role("button", name="Continuar edición")).to_be_visible()


def test_saved_settings_destination_restores_while_project_catalog_is_delayed(tmp_path, chromium_page):
    page, expect = chromium_page
    with running_server(tmp_path) as (_, base):
        page.goto(base)
        page.locator("#show-settings").click()
        page.locator("#show-watermark").click()
        expect(page.locator("#watermark-fields")).to_be_enabled()
        page.wait_for_load_state("networkidle")
        pending = []
        page.route("**/api/projects", lambda route: pending.append(route))
        with page.expect_request("**/api/projects"):
            page.reload(wait_until="domcontentloaded")
        expect(page.locator("#watermark-view")).to_be_visible()
        expect(page.locator("#watermark-fields")).to_be_enabled()
