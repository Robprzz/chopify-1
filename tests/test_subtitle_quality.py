"""Automation 2.1 regressions, including real local media and Studio routes."""
import json
import subprocess
import time
from urllib.error import HTTPError
from types import SimpleNamespace
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest

import captions
import editorial
import editorial_content as content
from editorial_providers import Cancellation
from fixtures.mock_ollama import MODEL, mock_ollama
import projects
import render_clips
import subtitle_quality as quality
import subtitle_repair
from test_dashboard import request, server  # noqa: F401
from test_editorial_studio import configured, wait_job, write_export
from test_studio_navigation import chromium_page  # noqa: F401


INCIDENT_SRT = """1
00:00:00,000 --> 00:00:00,840
Yo le había dado un poco.

2
00:00:00,840 --> 00:01:09,280
de agua.

3
00:01:09,540 --> 00:01:11,040
Aproximadamente como 10 minutos.
"""


def words(*items):
    return [{"word": text, "start": start, "end": end} for text, start, end in items]


def media_file(path, duration=3, audio="aevalsrc=0.2*sin(2*PI*(180*t+60*t*t))"):
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:size=320x180:rate=25",
                    "-f", "lavfi", "-i", audio, "-t", str(duration), "-pix_fmt", "yuv420p",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path)],
                   check=True, capture_output=True)
    return path


def test_reported_srt_is_blocked_before_local_ollama_or_rules(server):
    httpd, base = server
    source = media_file(httpd.app.root / "long.mp4", 71.04, "anullsrc")
    project, clip = write_export(httpd.app.root, source)
    clip.with_suffix(".srt").write_text(INCIDENT_SRT, encoding="utf-8")
    body = {"project": project, "filename": clip.name}
    token = httpd.app.token
    with mock_ollama() as (mock, provider):
        engine = httpd.app.editorial
        engine.provider = provider
        engine.ensure()
        configured(engine.store)
        diagnostic = repair_request(httpd, base, "diagnose", body)["diagnosis"]
        assert diagnostic["duration"] == pytest.approx(71.04, abs=.05)
        assert diagnostic["segments"] == 3
        assert diagnostic["max_cue_seconds"] == 68.44
        assert diagnostic["state"] == "blocked"
        with pytest.raises(HTTPError) as error:
            request(base, "/api/editorial/generate", body, token)
        assert error.value.code == 400 and "68.44 s" in json.load(error.value)["error"]
        engine.auto(project, clip.name)
        until = time.monotonic() + 5
        while not engine.last_auto_error and time.monotonic() < until:
            time.sleep(.025)
        assert "68.44 s" in engine.last_auto_error
        assert not mock.entered.is_set()
        assert not engine.store.history(None, None)["jobs"]


def test_independent_word_timestamps_never_hold_agua_across_the_silence(tmp_path):
    data = words(("Yo le había dado un poco.", 90, 90.84), ("de", 90.84, 91.05),
                 ("agua.", 91.05, 91.55), ("Aproximadamente", 159.54, 160.10),
                 ("como 10 minutos.", 160.10, 161.04))
    groups = captions.plan_cues(data, 90, 161.04)
    srt = captions.write_srt(data, 90, 161.04, tmp_path / "fixed.srt", groups=groups)
    ass = tmp_path / "fixed.ass"
    render_clips.build_ass(data, 90, 161.04, ass, 320, 180, 20, 15, groups=groups)
    assert quality.read_srt(srt) == [(0, .84, "Yo le había dado un poco."), (.84, 1.55, "de agua."),
                                    (69.54, 71.04, "Aproximadamente como 10 minutos.")]
    assert [(a, b) for a, b, _ in quality.read_ass(ass) if 1 <= a < 69] == [(1.05, 1.55)]
    assert render_clips.ass_time(59.999) == "0:01:00.00"
    assert quality.coverage(quality.read_srt(srt), [(0, 1.55), (69.54, 71.04)]) == pytest.approx((1.0, 3.05, 0.0))


def test_continuous_speech_keeps_spanish_dialogue_and_bounds(tmp_path):
    text = "¿Ya llegó Lucía? Sí, aquí estoy. Déjame explicar por qué esta es una conversación con cambios de voz."
    sequence = [{"word": word, "start": 50 + index * .25, "end": 50.25 + index * .25}
                for index, word in enumerate(text.split())]
    groups = captions.plan_cues(sequence, 50.1, 54.5, cue_max_ms=1500, cue_lines=1)
    srt = captions.write_srt([], 0, 0, tmp_path / "sequence.srt", groups=groups)
    actual = quality.read_srt(srt)
    assert " ".join(text for _, _, text in actual) == " ".join(text.split()[:18])
    assert actual[0][0] == 0
    assert actual[-1][1] == 4.4
    assert not quality.timing_errors(actual, 4.4, 1.5)
    assert all(len({word["line"] for word in group}) == 1 for group in groups)
    assert quality.coverage(actual, [(0, 4.4)])[0] == 1.0
    isolated = words(("¡Sí!", 5.18, 5.25))
    assert captions.plan_cues(isolated, 5, 5.3)[0][0]["right"] == 250
    late_token = captions.plan_cues(words(("Bien", 0, .3), ("Luego", 3, 3)), 0, 4)
    assert [g[0]["word"] for g in late_token] == ["Bien", "Luego"]
    assert late_token[1][0]["left"] == 3000
    with pytest.raises(ValueError, match="demasiado largas"):
        captions.plan_cues(words(("agua", .84, 69.28)), 0, 71.04)


def test_temporal_validation_and_missing_speech():
    cues = [(0.1, 1.3, "hola"), (-1, 0, "no"), (.05, 1, "sí"), (2.2, 2.5, "fin")]
    messages = " ".join(quality.timing_errors(cues, 2.3))
    assert all(word in messages for word in ("negativos", "cronológico", "solapamiento", "duración del MP4"))
    cues = [(0, .84, "bien"), (7, 8, "fin")]
    assert quality.coverage(cues, [(0, .84), (7, 8)])[0] == 1
    assert quality.speech_deficit(*quality.coverage(cues, [(0, 8)]))
    assert not quality.speech_deficit(*quality.coverage(cues, []))


def test_legacy_caption_slightly_over_target_only_warns(tmp_path):
    output = media_file(tmp_path / "legacy.mp4", 6, "anullsrc")
    output.with_suffix(".srt").write_text(
        "1\n00:00:00,000 --> 00:00:05,000\nUn subtítulo anterior razonable.\n", encoding="utf-8")
    diagnostic = quality.diagnose(output)
    assert diagnostic["errors"] == []
    assert any("duración recomendada" in warning for warning in diagnostic["warnings"])


def test_editorial_worker_rejects_structurally_valid_srt_with_uncovered_voice(server, monkeypatch):
    httpd, base = server
    project, clip = write_export(httpd.app.root)
    clip.with_suffix(".srt").write_text("1\n00:00:00,000 --> 00:00:00,250\nHola\n", encoding="utf-8")
    monkeypatch.setattr(quality, "speech_ranges", lambda *_: [(0, 2.75)])
    with mock_ollama() as (mock, provider):
        engine = httpd.app.editorial
        engine.provider = provider
        engine.ensure()
        configured(engine.store)
        posted = request(base, "/api/editorial/generate",
                         {"project": project, "filename": clip.name}, httpd.app.token)
        row = wait_job(engine, posted["job_id"], "failed")
        assert "Actividad de voz sin subtítulos" in row["error"]
        assert not mock.entered.is_set()


def test_zero_editorial_scores_do_not_pick_a_recommendation():
    source = editorial.TEST_SOURCE
    settings = {"platform": "tiktok", "profile": content.DEFAULT_CONFIG["profiles"]["tiktok"],
                "weights": content.WEIGHTS}
    plan = content.Rules().generate(None, content.messages(source, "tiktok", settings["profile"]),
                                    content.plan_schema(len(source["excerpts"])), 1, Cancellation())
    plan["confidence"] = 0
    for group in ("variants", "criteria"):
        for item in plan[group].values():
            item["score"] = 0
    result = content.assemble(plan, source, settings, "ollama", MODEL)
    assert result["recommended_variant"] is None
    assert result["editorial_score"] == 0
    assert "No hay una propuesta" in result["recommendation_reason"]
    assert len(result["variants"]) == 3


def test_real_vad_does_not_count_long_synthetic_silence_as_uncovered(tmp_path):
    output = tmp_path / "voice.mp4"
    try:
        media_file(output, 9, "flite=text='this is an example sentence for checking the spoken audio':voice=slt,adelay=1000,apad")
    except subprocess.CalledProcessError as exc:
        if b"No such filter" in exc.stderr:
            pytest.skip("This FFmpeg build lacks the optional flite fixture filter")
        raise
    detected = quality.speech_ranges(output, 9, 0)
    assert detected and 0.7 < detected[0][0] < 1.4
    assert detected[-1][1] < 7
    ratio, voiced, longest = quality.coverage([(0.6, 5, "Speech in a test fixture")], detected)
    assert .9 <= ratio <= 1
    assert 1 < voiced < 5
    assert longest < .4


def repair_request(httpd, base, action, body):
    initial = request(base, "/api/subtitle-repair/" + action, body, httpd.app.token)
    end = time.monotonic() + 20
    while time.monotonic() < end:
        state = request(base, "/api/subtitle-repair/" + initial["id"])
        if state["state"] != "running":
            return state
        time.sleep(.04)
    pytest.fail("Local subtitle job did not finish")


def frame_energy(media, offset, destination):
    result = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(offset), "-i", str(media),
                             "-frames:v", "1", "-vf", "format=gray", "-threads", "1", "-f", "rawvideo", "pipe:1"],
                            check=True, capture_output=True)
    assert result.stdout
    subprocess.run(["ffmpeg", "-v", "error", "-ss", str(offset), "-i", str(media),
                    "-frames:v", "1", "-threads", "1", str(destination)], check=True, capture_output=True)
    return sum(result.stdout)


def test_chromium_repair_local_source_export_and_visual_timing(server, chromium_page, monkeypatch, tmp_path):
    httpd, base = server
    source = media_file(httpd.app.root / "clean.mp4")
    project = projects.create_project(httpd.app.root, str(source), {"aspect": "16:9"})["id"]
    folder = httpd.app.root / project
    clips = folder / "clips"
    clips.mkdir()
    original = clips / "frozen.mp4"
    # The original fixture has burned text on every frame. The repair must use clean video.
    frozen_ass = httpd.app.root / "frozen.ass"
    render_clips.build_ass(words(("agua", 0, 3)), 0, 3, frozen_ass, 320, 180, 22, 24)
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-vf",
                    "subtitles=frozen.ass", "-c:a", "copy", str(original)],
                   cwd=frozen_ass.parent, check=True, capture_output=True)
    srt = original.with_suffix(".srt")
    srt.write_text("1\n00:00:00,000 --> 00:00:08,000\nagua\n", encoding="utf-8")
    projects.atomic_json(original.with_suffix(".meta.json"), {"title": "Fixture", "score": 7.14, "start": 0, "end": 3})
    projects.atomic_json(folder / "transcript.json", {"video": str(source), "model": "tiny", "words": []})
    original_hashes = quality.artifact_hash(original), quality.artifact_hash(srt)

    aligned = words(("Yo", .12, .34), ("tengo", .34, .60), ("agua.", .60, .86),
                    ("Ahora", 2.02, 2.22), ("habla", 2.22, 2.40), ("Lucía.", 2.40, 2.66))
    monkeypatch.setattr(subtitle_repair, "transcribe_final", lambda *_: {
        "model": "tiny", "language": "es", "words": aligned, "whisper_segments": 2})
    monkeypatch.setitem(render_clips.ASPECTS, "16:9", (320, 180, 18, 24))
    monkeypatch.setitem(render_clips.PREVIEW, "16:9", (320, 180))

    page, expect = chromium_page
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    page.goto(base)
    page.get_by_role("button", name="Automation", exact=True).click()
    page.locator("#editorial-clip").select_option(f"{project}/{original.name}")
    page.get_by_role("button", name="Generar propuestas", exact=True).click()
    expect(page.locator("#editorial-result")).to_contain_text("Subtítulos bloqueados")
    assert page.locator("#subtitle-quality").get_attribute("open") is not None
    page.locator("#subtitle-diagnose").click()
    expect(page.locator("#subtitle-status")).to_have_text("Diagnóstico · Bloqueado")
    expect(page.locator("#subtitle-diagnostics")).to_contain_text("excede la duración del MP4")
    page.locator("#subtitle-quality").screenshot(path=str(tmp_path / "diagnostico-chromium.png"))
    page.locator("#subtitle-transcribe").click()
    expect(page.locator("#subtitle-status")).to_contain_text("Borrador listo")
    page.locator("#subtitle-draft summary").click()
    expect(page.locator("#subtitle-draft-text")).to_contain_text("Ahora habla Lucía.")
    query = urlencode({"project": project, "filename": original.name})
    latest = request(base, "/api/subtitle-repair/latest?" + query)
    assert "Ahora habla Lucía." in latest["draft"]["text"]
    page.reload()
    page.get_by_role("button", name="Automation", exact=True).click()
    page.locator("#editorial-clip").select_option(f"{project}/{original.name}")
    expect(page.locator("#editorial-result")).to_contain_text("Subtítulos bloqueados")
    expect(page.locator("#subtitle-preview")).to_be_visible()
    expect(page.locator("#subtitle-preview")).to_be_enabled()
    srt_link = page.locator("#subtitle-srt").get_attribute("href")
    with urlopen(base + srt_link) as response:
        assert "Yo tengo agua." in response.read().decode()
    page.locator("#subtitle-preview").click()
    expect(page.locator("#subtitle-video")).to_be_visible(timeout=15000)
    preview = page.locator("#subtitle-video").get_attribute("src")
    with urlopen(base + preview) as response:
        assert response.headers["Content-Type"] == "video/mp4"
    page.locator("#subtitle-export").click()
    expect(page.locator("#subtitle-status")).to_contain_text("disponible en Publicaciones", timeout=15000)
    expect(page.locator("#subtitle-output")).to_be_visible()
    page.locator("#subtitle-quality").screenshot(path=str(tmp_path / "reparacion-chromium.png"))
    corrected = clips / page.locator("#subtitle-output").get_attribute("download")
    assert corrected.is_file() and corrected != original
    assert (quality.artifact_hash(original), quality.artifact_hash(srt)) == original_hashes
    assert any(c["filename"] == corrected.name for c in httpd.app.tiktok.exports())
    assert quality.diagnose(corrected)["errors"] == []
    assert quality.diagnose(corrected)["timeline_version"] == captions.TIMELINE_VERSION

    duration, streams = quality.probe_media(corrected)
    assert duration == pytest.approx(3, abs=.05)
    assert set(streams) == {"video", "audio"}
    assert abs(float(streams["audio"]["duration"]) - float(streams["video"]["duration"])) < .05
    for clip in original, corrected:
        data = subprocess.run(["ffmpeg", "-v", "error", "-i", str(clip), "-map", "0:a:0", "-c", "copy",
                               "-f", "hash", "-hash", "sha256", "pipe:1"], check=True, capture_output=True)
        if clip == original:
            digest = data.stdout
        else:
            assert data.stdout == digest
    assert frame_energy(original, 1.6, tmp_path / "antes-silencio.png") > 10000
    assert frame_energy(corrected, 1.6, tmp_path / "despues-silencio.png") == 0
    assert frame_energy(corrected, 2.5, tmp_path / "despues-dialogo.png") > 10000
    page.locator("#editorial-clip").select_option(f"{project}/{corrected.name}")
    page.locator("#editorial-generate").click()
    expect(page.locator("#editorial-result")).to_contain_text("Editorial", timeout=10000)
    assert not errors

    # A mismatched subtitle sidecar must not be associated with this MP4.
    corrected.with_suffix(".srt").write_text(INCIDENT_SRT, encoding="utf-8")
    assert any("no coincide" in error for error in quality.diagnose(corrected)["errors"])


def test_missing_clean_source_retains_the_retranscribed_draft(server, monkeypatch):
    httpd, base = server
    project, clip = write_export(httpd.app.root)
    folder = clip.parent.parent
    projects.atomic_json(folder / "transcript.json", {"video": str(folder / "not-downloaded.mp4")})
    monkeypatch.setattr(subtitle_repair, "transcribe_final", lambda *_: {
        "model": "tiny", "words": words(("Hola", .1, .6)), "whisper_segments": 1})
    args = {"project": project, "filename": clip.name, "model": "tiny"}
    draft = repair_request(httpd, base, "transcribe", args)
    assert draft["state"] == "done"
    render = repair_request(httpd, base, "export", {**args, "draft": draft["id"]})
    assert render["state"] == "failed" and "video limpio" in render["error"]
    with urlopen(f"{base}/subtitle-repair/{draft['id']}/draft.srt") as response:
        assert "Hola" in response.read().decode()
    assert clip.is_file()



def test_whisper_subprocess_contract_loads_only_local_weights(tmp_path, monkeypatch):
    import faster_whisper
    from faster_whisper import utils

    model_path = tmp_path / "cached-model"
    calls = []

    def local_model(name, **kwargs):
        assert kwargs == {"local_files_only": True}
        if name == "large-v3":
            raise ValueError("no cache")
        assert name == "tiny"
        return str(model_path)

    def model(path, **kwargs):
        calls.append((path, kwargs))
        segment = SimpleNamespace(words=[SimpleNamespace(word="Hola", start=.2, end=.6)])
        return SimpleNamespace(transcribe=lambda *_, **__: ([segment], SimpleNamespace(language="es")))

    monkeypatch.setattr(utils, "download_model", local_model)
    monkeypatch.setattr(faster_whisper, "WhisperModel", model)
    audio = tmp_path / "clip.mp4"
    output = tmp_path / "draft.json"
    with pytest.raises(quality.SubtitleError, match="caché local"):
        subtitle_repair._whisper(audio, output, "large-v3")
    subtitle_repair._whisper(audio, output, "tiny")
    assert calls == [(str(model_path), {"device": "cpu", "compute_type": "int8",
                                       "cpu_threads": 2, "local_files_only": True})]
    assert projects.read_json(output)["words"] == words(("Hola", .2, .6))
