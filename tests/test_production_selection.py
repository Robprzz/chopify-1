"""Automation 3.0 gates, duplicate evidence, local-day ranking and reuse."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace

from editorial_store import EditorialStore
import production
import production_selection as selection
import production_store
import projects
import subtitle_quality
from test_dashboard import request, running_server
from test_production import SemanticDouble, configured, media_file, setup_asset, until


DIALOGUE = ("la investigacion describe las grabaciones de la entrada y revisa el horario exacto de la visita "
            "mientras el testigo comenta el sonido del porton y la posicion de la camara para entender el relato").split()


def reference(sha, *, source=None, interval=None, tokens=()):
    return {"sha": [sha], "source": source or sha, "interval": interval, "tokens": list(tokens),
            "file_stamp": [sha], "original_stamp": [sha], "duration": 30}


def test_duplicates_require_identical_bytes_overlapping_source_or_substantive_dialogue():
    one = {"start": 3, "end": 23}
    two = {"start": 10, "end": 30}
    original = reference("first", source="same", interval=one, tokens=DIALOGUE)
    assert selection.duplicate(original, reference("first"))["method"] == "sha256"
    assert selection.duplicate(original, reference("second", source="same", interval=two))["method"] == "overlap"
    assert selection.duplicate(original, reference("second", interval=two)) is None
    assert selection.duplicate(original, reference("second", tokens=DIALOGUE + ["eh"]))["method"] == "transcript"
    assert selection.duplicate(original, reference("second", tokens=DIALOGUE[:15] + ["no"] + DIALOGUE[15:])) is None
    assert selection.duplicate(original, reference("second", tokens=DIALOGUE[:15] + ["matias"] + DIALOGUE[16:])) is None
    assert selection.duplicate(original, reference("second", tokens="la camara apunta a la entrada y es una grabacion".split())) is None
    assert selection.duplicate(original, reference("second", tokens=["camara"] * 40)) is None


def test_packet_continuity_uses_presentation_order_and_detects_holes(monkeypatch, tmp_path):
    stream = lambda index, values: [{"stream_index": index, "pts_time": pts, "duration_time": duration}
                                     for pts, duration in values]
    payload = {"streams": [{"codec_type": "video", "index": 0}, {"codec_type": "audio", "index": 1}],
               "packets": stream(0, [(1, .2), (0, .2), (.2, .2)]) + stream(1, [(0, .6), (.6, .6)])}
    monkeypatch.setattr(production, "run", lambda *_: SimpleNamespace(
        stdout=json.dumps(payload), check_returncode=lambda: None))
    assert production.continuity(tmp_path / "synthetic.mp4", None) == {"video": .6, "audio": 0}


def test_ranking_daily_cap_timezone_and_duplicate_history_after_restart(tmp_path, monkeypatch):
    now = datetime(2026, 11, 23, 3, 55, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(production_store, "time", SimpleNamespace(time=lambda: now))
    path = tmp_path / "automation.sqlite3"
    store = production_store.Store(EditorialStore(path))
    store.migrate()
    detection = [8.8, 8.1, 9.7, 8.8, 7.2]
    editorial = [9, 9.8, 8.9, 9, 7.5]
    fixed = lambda *_: True
    for i in range(5):
        store.enqueue("project", f"{i}.mp4")
        job = store.claim("worker")
        store.record_evidence(job["id"], "worker", job["filename"], detection[i], reference(str(i)),
                              {"integrity": "decoded"}, fixed)
        store.change(job["id"], "worker", "ready", editorial=editorial[i], context=8, technical="passed")
    store.enqueue("other", "duplicate.mp4")
    job = store.claim("worker")
    match = store.record_evidence(job["id"], "worker", "duplicate.mp4", 8, reference("0"), {}, fixed)
    store.change(job["id"], "worker", "rejected", code="duplicate", technical="passed")
    assert match["job_id"] == 1

    recovered = production_store.Store(EditorialStore(path))
    recovered.migrate()
    settings = {"timezone": "America/Santo_Domingo", "daily_publication_limit": 3, "minimum_clip_score": 7}
    status = recovered.status(view="recommended", settings=settings)
    assert status["selection_day"] == "2026-11-22"
    assert [item["id"] for item in status["jobs"]] == [3, 1, 4]
    assert [item["recommendation_rank"] for item in status["jobs"]] == [1, 2, 3]
    assert recovered.status(view="recommended", settings={**settings, "daily_publication_limit": 2})["recommended_count"] == 2
    assert recovered.status(view="recommended", settings=settings, now=now + 600)["recommended_count"] == 0
    raised = {**settings, "minimum_clip_score": 9}
    assert recovered.status(view="recommended", settings=raised)["recommended_count"] == 1
    assert {row["id"] for row in recovered.status(view="discarded", settings=raised)["jobs"]} == {1, 2, 4, 5}
    assert recovered.status(view="duplicate", settings=settings)["jobs"][0]["id"] == job["id"]
    assert recovered.detail(job["id"])["duplicate_history"] == [match]
    recovered.retry(job["id"], distinct=True)
    job = recovered.claim("worker")
    assert recovered.record_evidence(job["id"], "worker", "duplicate.mp4", 8, reference("0"), {}, fixed) is None
    assert recovered.detail(job["id"])["duplicate_history"] == [match]


def test_http_gates_and_reuse_avoid_new_model_or_media_work(tmp_path, monkeypatch):
    monkeypatch.setattr(subtitle_quality, "speech_ranges", lambda *_: [(.2, .9), (1.42, 2.6)])
    root = tmp_path / "projects"
    root.mkdir()
    with running_server(root) as (server, base):
        app = server.app
        request(base, "/api/production/pause", {}, app.token)
        request(base, "/api/production/settings", {"max_repairs": 0}, app.token)
        app.editorial.provider = double = SemanticDouble()
        app.editorial.ensure()
        configured(app.editorial.store)
        source = media_file(root / "clean.mp4")
        for kind, rating in (("valid", 8.2), ("same", 8.2), ("low", 5.5), ("corrupt", 9),
                             ("bad_subtitles", 9), ("unknown", None)):
            project, clip = setup_asset(root, source, kind)
            path = clip.with_suffix(".meta.json")
            projects.atomic_json(path, {**projects.read_json(path), "score": rating})
            if kind == "bad_subtitles":
                clip.with_suffix(".srt").write_text("sin tiempos", encoding="utf-8")
            request(base, "/api/production/enqueue", {"project": project, "filename": clip.name}, app.token)
        request(base, "/api/production/resume", {}, app.token)
        result = until(lambda: request(base, "/api/production/status"), lambda data: data["metrics"]["pending"] == 0)
        jobs = {job["filename"]: job for job in result["jobs"]}
        assert jobs["valid.mp4"]["decision"] == "recommended"
        assert jobs["same.mp4"]["duplicate"]["method"] == "sha256"
        assert jobs["low.mp4"]["code"] == "low_score"
        assert jobs["corrupt.mp4"]["code"] == "media_unreadable"
        assert jobs["bad_subtitles.mp4"]["code"] == "repair_exhausted"
        assert jobs["unknown.mp4"]["code"] == "score_unknown"
        assert all(job["editorial_id"] is None for file, job in jobs.items() if file != "valid.mp4")
        assert double.calls["valid", "proposal"] == 1
        saved = jobs["valid.mp4"]["editorial_id"]
        ident = jobs["valid.mp4"]["id"]

    def forbidden(*args, **kwargs):
        raise AssertionError("La evidencia vigente debe reutilizarse.")

    with running_server(root) as (server, base):
        app = server.app
        monkeypatch.setattr(app.editorial.provider, "models", forbidden)
        monkeypatch.setattr(production, "media_check", forbidden)
        monkeypatch.setattr(selection, "evidence", forbidden)
        request(base, "/api/production/retry", {"id": ident}, app.token)
        result = until(lambda: request(base, f"/api/production/job?id={ident}"), lambda data: data["finished"] is not None)
        assert result["state"] == "ready" and result["editorial_id"] == saved
        clip = app.production.clip_path(result["project"], result["filename"])
        with clip.with_suffix(".srt").open("a", encoding="utf-8") as stream:
            stream.write("\n")
        assert request(base, "/api/production/status?view=recommended")["recommended_count"] == 0
        assert request(base, "/api/production/status")["jobs"][-1]["state"] == "review"
