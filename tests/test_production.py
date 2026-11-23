"""Automation 2.4 through SQLite, the real Studio server, media tools and Chromium."""
from collections import Counter
import sys
import threading
import json
import shutil
import subprocess
import time
from urllib.error import HTTPError

import pytest

import editorial_semantic as semantic
from editorial_providers import Cancellation, Cancelled
from fixtures.mock_ollama import mock_ollama
from local_process import run
import production
import projects
import render_clips
import subtitle_quality
import subtitle_repair
from test_dashboard import request, running_server, server  # noqa: F401
from test_editorial_studio import configured, wait_job
from test_studio_navigation import chromium_page  # noqa: F401
from test_subtitle_quality import INCIDENT_SRT, media_file, words


DIALOGUE = """1
00:00:00,100 --> 00:00:01,050
La albahaca necesita luz.

2
00:00:01,360 --> 00:00:02,800
Riega la albahaca cuando notes la tierra seca.
"""
MODEL = "qwen3:4b"
PASSAGE = "La albahaca necesita luz. Riega la albahaca cuando notes la tierra seca."
KINDS = (
    *(["valid"] * 7), "live_valid", *(["cached"] * 3), "whisper", "live_whisper",
    "no_source", "no_model", "av_skew", "corrupt", "editorial_retry", "editorial_fail", "cancel",
)


def proposal():
    variants = {
        "curiosity": {"title": "¿Cuándo conviene regar la albahaca?",
                      "caption": "La tierra da una pista: fíjate si está seca antes de regar.",
                      "description": "El clip explica cómo usar la tierra como señal para regar la albahaca.",
                      "call_to_action": "", "hashtags": ["#Albahaca"], "score": 8.6, "evidence": [0]},
        "interaction": {"title": "Tu rutina de cuidado de la albahaca",
                        "caption": "¿Qué miras primero al cuidar tu albahaca?",
                        "description": "Compara tu rutina de riego con la pauta del clip.",
                        "call_to_action": "Cuéntanos cómo la riegas.", "hashtags": ["#Albahaca"], "score": 8.2, "evidence": [0]},
        "contextual": {"title": "Luz y riego para la albahaca",
                       "caption": "Dos puntos del fragmento: luz y humedad de la tierra.",
                       "description": "La albahaca necesita luz; el riego depende de cuándo se seca la tierra.",
                       "call_to_action": "", "hashtags": [], "score": 8, "evidence": [0]},
    }
    return {"theme": "Cuidado de la albahaca", "secondary": ["Luz", "Riego"], "message": "Observa la tierra antes de regar.",
            "intent": "explain", "emotion": "neutral", "audience": "Personas que cultivan hierbas",
            "interest": "Una señal práctica para regar", "standalone": True, "people": [], "events": [],
            "variants": variants}


class SemanticDouble:
    name = "ollama"

    def __init__(self):
        self.calls = Counter()
        self.current = ""
        self.questions = []

    def models(self):
        return [{"name": MODEL, "digest": "a" * 64}]

    def generate(self, model, messages, schema, timeout, cancellation):
        cancellation.check()
        assert model == MODEL
        content = json.loads(messages[1]["content"])
        if "passages" in content:
            self.current = content["outside_clip"].get("title", "live")
            self.calls[self.current, "analysis"] += 1
            self.questions.append((self.current, messages[0]["content"], content))
            return comprehension()
        if "repair" in content:
            self.calls[self.current, "repair"] += 1
            self.questions.append((self.current, messages[0]["content"], content))
            return {style: {field: ("La pista de la tierra antes de regar" if field == "title"
                                    and self.current != "editorial_fail"
                                    else content["current_proposals"][style].get(field))
                            for field in fields} for style, fields in content["repair"].items()}
        if "requested_angles" in content:
            assert "outside_clip" not in content
            self.calls[self.current, "proposal"] += 1
            self.questions.append((self.current, messages[0]["content"], content))
            result = {style: proposal()["variants"][style] for style in content["requested_angles"]}
            for style, variant in result.items():
                variant.pop("score")
                variant.pop("evidence")
                variant.update(claims=[0], strategy="Enfoque de " + style)
                if self.current == "editorial_fail":
                    variant["title"] = "Descubre las 47 albahacas"
            return result
        assert "outside_clip" not in content
        self.calls[self.current, "review"] += 1
        review = {name: 8 for name in semantic.CRITERIA}
        review.update(grounded=True, sensational=False, uncertainty="low", fields=[])
        return {style: ({**review, "grounded": False, "fields": ["title"]}
                        if self.current == "editorial_retry" and self.calls[self.current, "review"] == 1
                        and style == "curiosity" else review) for style in content["variants_to_review"]}


def comprehension():
    return {"language": "es", "theme": "Cuidado de la albahaca", "secondary": ["Luz", "Riego"],
            "message": "Observa la tierra antes de regar.", "intent": "explain", "emotion": "neutral",
            "tone": "práctico", "audience": "Personas que cultivan hierbas", "value": "educational",
            "standalone": True, "context_needed": "", "uncertainty": "low", "ambiguities": [],
            "claims": [{"text": "Cuidados de la albahaca.", "quote": "La albahaca necesita luz.",
                        "kind": "attributed_fact", "refs": [0], "ambiguity": ""}],
            "entities": [], "highlights": [0], "sensitive": False}


def setup_asset(root, source, kind):
    live = kind.startswith("live_")
    if live:
        ident = "a" * 12 if kind == "live_valid" else "b" * 12
        project = "live/" + ident
        folder = root / project
        (folder / "windows").mkdir(parents=True)
    else:
        original = root / "missing.mp4" if kind == "no_source" else source
        project = projects.create_project(root, str(original), {"aspect": "16:9"})["id"]
        folder = root / project
        state = projects.read_project(folder)
        state["source_meta"] = {"title": kind, "description": "Contexto externo: Patio 68 y un patio soleado.",
                                "channel": "Cultivo local"}
        projects.save_project(folder, state)
    (folder / "clips").mkdir()
    media = folder / "clips" / (kind + ".mp4")
    if kind == "corrupt":
        media.write_bytes(b"not an MP4")
    elif kind == "av_skew":
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-itsoffset", "0.5", "-i", str(source),
                        "-map", "0:v", "-map", "1:a", "-c", "copy", str(media)], check=True)
    else:
        shutil.copyfile(source, media)
    caption = media.with_suffix(".srt")
    caption.write_text(INCIDENT_SRT if kind in {"cached", "whisper", "live_whisper", "no_source", "no_model"}
                       else DIALOGUE, encoding="utf-8")
    projects.atomic_json(media.with_suffix(".meta.json"), {"start": 0, "end": 3, "score": 8.2, "title": "Albahaca"})
    if live:
        window = folder / "windows" / "clean.mp4"
        shutil.copyfile(source, window)
        projects.atomic_json(folder / "live_session.json", {
            "triggers": [{"score": 8.2, "files": {"mp4": str(media), "window": str(window)}}]})
    else:
        cached_words = words(("La albahaca necesita luz.", .1, 1.05),
                             ("Riega la albahaca cuando notes la tierra seca.", 1.36, 2.8)) if kind == "cached" else []
        projects.atomic_json(folder / "transcript.json", {"video": str(original), "model": "small",
            "language": "es", "words": cached_words + words(("Cultivo doméstico.", 3.4, 4))})
    return project, media


def until(call, condition, seconds=35):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = call()
        if condition(result):
            return result
        time.sleep(.05)
    pytest.fail(f"Production did not reach the expected state: {result}")


def test_twenty_jobs_automatic_quality_repair_and_semantic_gate(server, monkeypatch, tmp_path):
    monkeypatch.setattr(subtitle_quality, "speech_ranges", lambda *_: [(.2, .9), (1.42, 2.6)])
    httpd, base = server
    app = httpd.app
    producer = app.production
    double = SemanticDouble()
    app.editorial.provider = double
    app.editorial.ensure()
    config = configured(app.editorial.store)
    settings = config["settings"]
    identity = {**settings["identity"], "tone": "Sobrio y explicativo"}
    app.editorial.store.save_config({**settings, "identity": identity}, config["revision"])
    token = app.token
    request(base, "/api/production/pause", {}, token)
    source = media_file(app.root / "clean.mp4")
    monkeypatch.setitem(render_clips.ASPECTS, "16:9", (320, 180, 18, 24))
    monkeypatch.setattr(production, "cached_models", lambda _: iter(["small"]))
    calls = Counter()

    def transcribe(media, *_):
        if media.stem == "no_model":
            raise subtitle_quality.SubtitleError("Modelo local no encontrado.")
        calls[media.stem] += 1
        assert len([item for item in double.questions if item[0] == media.stem]) == 0
        return {"model": "small", "words": words(("La albahaca necesita luz.", .1, 1.05),
                                                ("Riega la albahaca cuando notes la tierra seca.", 1.36, 2.8))}
    monkeypatch.setattr(subtitle_repair, "transcribe_final", transcribe)

    assets = {}
    for kind in KINDS:
        project, media = setup_asset(app.root, source, kind)
        # Keep the repair branches independent of the duplicate gate.
        if kind != "corrupt":
            marker = project.encode("ascii")
            with media.open("ab") as stream:
                stream.write((8 + len(marker)).to_bytes(4, "big") + b"free" + marker)
        if not project.startswith("live/"):
            project_folder = media.parent.parent
            state = projects.read_project(project_folder)
            state["cache"]["source"] = project
            projects.save_project(project_folder, state)
        for _ in range(2):
            request(base, "/api/production/enqueue", {"project": project, "filename": media.name}, token)
        assets[project] = (kind, media, media.read_bytes(), media.with_suffix(".srt").read_bytes())
    status = request(base, "/api/production/status")
    assert status["metrics"]["total"] == 20 and status["counts"]["generated"] == 20
    cancel = next(row for row in status["jobs"] if row["filename"] == "cancel.mp4")
    request(base, "/api/production/cancel", {"id": cancel["id"]}, token)
    request(base, "/api/production/resume", {}, token)
    result = until(lambda: request(base, "/api/production/status"), lambda state: state["metrics"]["pending"] == 0, 90)
    assert result["counts"] == {"ready": 10, "review": 3, "rejected": 6, "cancelled": 1}
    assert result["recommended_count"] == 5
    assert Counter(job["decision"] for job in result["jobs"]) == {
        "recommended": 5, "eligible": 5, "duplicate": 4, "discarded": 3, "review": 3,
    }
    assert all(job["editorial_id"] is None for job in result["jobs"] if job["decision"] == "duplicate")
    assert result["metrics"]["repaired"] == 5
    assert result["metrics"]["manual_repairs_avoided"] == 1
    assert calls == {"whisper": 1, "live_whisper": 1}
    summary = []
    for row in sorted(result["jobs"], key=lambda item: item["id"]):
        kind, file, original_mp4, original_srt = assets[row["project"]]
        assert file.read_bytes() == original_mp4
        assert file.with_suffix(".srt").read_bytes() == original_srt
        detail = request(base, "/api/production/job?id=" + str(row["id"]))
        phases = {event["state"] for event in detail["events"]}
        if row["state"] == "ready":
            assert {"validating", "analyzing", "understanding", "generating", "validating_proposals", "ready"} <= phases
            editorial = app.editorial.store.detail(row["editorial_id"])
            assert editorial["result"]["mode"] == "semantic"
            assert editorial["result"]["confidence"] is None
            assert editorial["result"]["editorial_identity"] == identity
            assert not any(variant["title"] in PASSAGE for variant in editorial["result"]["variants"])
        if row["active_filename"]:
            assert {"repairing", "revalidating"} <= phases
            assert (file.parent / row["active_filename"]).is_file()
            assert app.tiktok._clip(row["project"], row["active_filename"]).is_file()
        if kind in {"no_source", "no_model"}:
            assert row["attempts"] == 2 and row["editorial_id"] is None
        summary.append({key: row[key] for key in ("filename", "state", "code", "attempts", "seconds",
                                                   "repair_seconds", "editorial_seconds")})
    assert double.calls["editorial_retry", "proposal"] == 1
    assert double.calls["editorial_retry", "repair"] == 1
    assert double.calls["editorial_retry", "review"] == 2
    assert double.calls["editorial_fail", "proposal"] == 1
    assert double.calls["editorial_fail", "repair"] == 1
    assert double.calls["editorial_fail", "review"] == 0
    assert any(list(data["repair"]) == ["curiosity"] and list(data["repair"]["curiosity"]) == ["title"]
               for title, _, data in double.questions if title == "editorial_retry" and "repair" in data)
    assert any(data["outside_clip"].get("channel") == "Cultivo local" and "Patio 68" not in
               str(data["passages"]) for _, _, data in double.questions if "passages" in data)
    assert all(data["editorial_identity"] == identity for _, _, data in double.questions)
    assert any(title == "live" for title, _, _ in double.questions)
    assert not any("de agua" in str(data) for _, _, data in double.questions)
    with pytest.raises(HTTPError) as rejected:
        request(base, "/api/tiktok/upload", {"review_id": "example", "description": "albahaca", "approved": False}, token)
    assert rejected.value.code >= 400 and app.tiktok.state()["history"] == []
    (tmp_path / "production-20.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def test_restart_keeps_recovery_checkpoint_and_never_submits_twice(tmp_path, monkeypatch):
    monkeypatch.setattr(subtitle_quality, "speech_ranges", lambda *_: [(.2, .9), (1.42, 2.6)])
    root = tmp_path / "projects"
    root.mkdir()
    source = media_file(root / "clean.mp4")
    project, file = setup_asset(root, source, "valid")
    with running_server(root) as (server, base):
        app = server.app
        request(base, "/api/production/pause", {}, app.token)
        request(base, "/api/production/enqueue", {"project": project, "filename": file.name}, app.token)
        app.editorial.provider = SemanticDouble()
        app.editorial.ensure()
        configured(app.editorial.store)
        row = request(base, "/api/production/status")["jobs"][0]
        assert row["state"] == "generated"
    with running_server(root) as (server, base):
        app = server.app
        double = SemanticDouble()
        app.editorial.provider = double
        assert request(base, "/api/production/status")["config"]["paused"]
        store = app.production.store
        # Simulate a crashed process after registering the editorial job but before saving its link.
        posted = app.editorial.submit(project, file.name, None, False, production=(row["id"], row["run"]))
        with store.connect(write=True) as db:
            db.execute("UPDATE production_jobs SET state='analyzing', owner='old', lease=1 WHERE id=?", (row["id"],))
        request(base, "/api/production/resume", {}, app.token)
        final = until(lambda: request(base, f"/api/production/job?id={row['id']}"), lambda item: item["state"] == "ready")
        assert final["editorial_id"] == posted["job_id"]
        assert any(event["code"] == "interrupted" for event in final["events"])
        assert double.calls["valid", "proposal"] == 1
        assert len(app.editorial.store.history(None, None)["jobs"]) == 1


def test_operations_chromium_pause_and_semantic_result(server, chromium_page, tmp_path, monkeypatch):
    monkeypatch.setattr(subtitle_quality, "speech_ranges", lambda *_: [(.2, .9), (1.42, 2.6)])
    httpd, base = server
    app = httpd.app
    source = media_file(app.root / "clean.mp4", size="640x360")
    project, file = setup_asset(app.root, source, "editorial_retry")
    app.editorial.provider = SemanticDouble()
    app.editorial.ensure()
    configured(app.editorial.store)
    page, expect = chromium_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base)
    page.get_by_role("button", name="Automation", exact=True).click()
    paused = page.get_by_role("button", name="Pausar cola")
    paused.click()
    expect(page.locator("#production-status")).to_contain_text("Pausada")
    with pytest.raises(HTTPError) as unauthorized:
        request(base, "/api/production/resume", {})
    assert unauthorized.value.code == 403
    page.locator("#production-limit").fill("1")
    page.get_by_role("button", name="Guardar límite").click()
    until(lambda: request(base, "/api/production/status"),
          lambda data: data["config"]["max_repairs"] == 1)
    page.locator("#editorial-clip").select_option(f"{project}/{file.name}")
    page.get_by_role("button", name="Añadir a producción").click()
    expect(page.locator("#production-jobs")).to_contain_text("Generado")
    page.get_by_role("button", name="Reanudar cola").click()
    expect(page.locator("#production-jobs")).to_contain_text("Listo para aprobación", timeout=20000)
    page.locator("#production-filter").select_option("recommended")
    expect(page.locator("#production-jobs")).to_contain_text("Recomendado · #1")
    expect(page.locator("#production-jobs")).to_contain_text("8.20")
    page.locator("#production-filter").select_option("discarded")
    expect(page.locator("#production-empty")).to_be_visible()
    page.locator("#production-filter").select_option("all")
    page.get_by_role("button", name="Ver detalle", exact=True).click()
    expect(page.locator("#production-detail")).to_contain_text("Multimedia decodificado")
    page.get_by_role("region", name="Centro de operaciones").screenshot(path=str(tmp_path / "operations-chromium.png"))
    page.get_by_role("button", name="Ver propuestas", exact=True).click()
    output = page.locator("#editorial-result")
    expect(output).to_contain_text("Mayor puntuación entre las propuestas")
    expect(output.locator(".editorial-variant")).to_have_count(3)
    output.locator("summary").filter(has_text="Evaluación y evidencia").click()
    expect(output).to_contain_text("Mensaje central")
    output.locator("summary").filter(has_text="Diagnóstico de ejecución").click()
    expect(output).to_contain_text("Campos corregidos · Curiosidad / Título")
    output.screenshot(path=str(tmp_path / "semantic-chromium.png"))
    output.get_by_role("button", name="Revisar en Publicaciones").click()
    expect(page.locator("#publications-view")).to_be_visible()
    expect(page.locator("#tiktok-description")).to_have_value("El clip explica cómo usar la tierra como señal para regar la albahaca.\n\n#Albahaca")
    expect(page.locator("#tiktok-send")).to_be_disabled()
    expect(page.locator("#tiktok-consent")).not_to_be_checked()
    assert not errors
    page.locator("#publications-view").screenshot(path=str(tmp_path / "consent-chromium.png"))



def test_semantic_jobs_use_bounded_loopback_transport(server, monkeypatch):
    server, base = server
    monkeypatch.setattr(subtitle_quality, "speech_ranges", lambda *_: [(.2, .9), (1.42, 2.6)])
    app = server.app
    source = media_file(app.root / "clean.mp4")
    project, file = setup_asset(app.root, source, "editorial_retry")
    double = SemanticDouble()
    with mock_ollama() as (fake, provider):
        fake.generate_reply = lambda d: double.generate(
            d["model"], d["messages"], d["format"], 15, Cancellation())
        app.editorial.provider = provider
        app.editorial.ensure()
        configured(app.editorial.store)
        job = request(base, "/api/editorial/generate",
                      {"project": project, "filename": file.name, "semantic": True}, app.token)["job_id"]
        result = wait_job(app.editorial, job)["result"]
        assert result["mode"] == "semantic" and result["attempts"] == 2
        assert result["model_calls"] == len(fake.calls) == 5
        assert all(call["options"]["num_ctx"] == 6144 and call["stream"] is False
                   and call["think"] is False and "tools" not in call for call in fake.calls)


def test_active_media_and_production_cancellation(server, monkeypatch):
    httpd, base = server
    app = httpd.app
    source = media_file(app.root / "clean.mp4")
    project, file = setup_asset(app.root, source, "cancel")
    started = threading.Event()
    results = []

    def long_check(_, cancellation):
        started.set()
        begin = time.monotonic()
        try:
            run([sys.executable, "-c", "import time; time.sleep(30)"], 40, cancellation)
        except Cancelled:
            results.append(time.monotonic() - begin)
            raise
        pytest.fail("Subprocess should have been cancelled")

    monkeypatch.setattr(production, "media_check", long_check)
    request(base, "/api/production/enqueue", {"project": project, "filename": file.name}, app.token)
    assert started.wait(4)
    data = request(base, "/api/production/status")
    job = data["jobs"][0]["id"]
    request(base, "/api/production/cancel", {"id": job}, app.token)
    found = until(lambda: request(base, f"/api/production/job?id={job}"),
                  lambda row: row["state"] == "cancelled", 6)
    assert found["editorial_id"] is None
    assert results and results[0] < 5


def test_broken_export_is_persisted_for_automatic_intake(server, monkeypatch):
    httpd, base = server
    app = httpd.app
    request(base, "/api/production/pause", {}, app.token)
    source = media_file(app.root / "clean.mp4")
    project, clip = setup_asset(app.root, source, "valid")
    folders = clip.parent.parent
    monkeypatch.setitem(render_clips.ASPECTS, "16:9", (320, 180, 18, 24))
    bad = words(("Una palabra congelada", 0, 65))
    first = render_clips.render({"start": 0, "end": 3, "hook": "defective"}, bad, source, 320, 180,
                                folders, "16:9", out_dir=clip.parent, retain_for_repair=True)
    assert first.is_file() and first.with_suffix(".srt").read_text(encoding="utf-8") == ""
    assert projects.read_json(first.with_suffix(".meta.json"))["subtitles"]["caption_error"]
    app._production_clip(project, first.name)
    state = request(base, "/api/production/status")
    assert [(job["filename"], job["state"]) for job in state["jobs"]] == [(first.name, "generated")]
