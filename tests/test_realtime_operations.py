"""Automation 3.1 behavior with persisted stores and the real Studio HTTP server."""
from copy import deepcopy
import json
import time
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

import automation
from editorial_content import WEIGHTS
from editorial_store import EditorialStore
from live_production import ACTIVE, MAX_PENDING, Pipeline
from production_store import Store
import realtime_operations as operations
import realtime_priority as priority
from test_dashboard import request, server  # noqa: F401
from test_studio_navigation import chromium_page  # noqa: F401

NOW = 1795456800.


def reference(key):
    return {"sha": ["bytes-" + key], "source": "source-" + key, "interval": {"start": 0, "end": 60},
            "tokens": [f"{key}-{i}" for i in range(35)], "duration": 60, "current": True}


def seed(store, now, key, *, project="project", finished=None, evidence=None, state="ready",
         detection=8.6, editorial=8.2, legacy=8.1, with_editor=False):
    finished = now - 20 if finished is None else finished
    evidence = reference(key) if evidence is None else evidence
    with store.connect(write=True) as db:
        editor_id = None
        if with_editor:
            result = {"mode": "semantic", "editorial_score": editorial, "standalone": True,
                      "recommended_variant": "contextual", "variants": [{"style": "contextual", "score": 8}],
                      "criteria": {name: {"score": 8.5} for name in WEIGHTS}}
            editor_id = db.execute("""INSERT INTO editorial_jobs(clip_id,fingerprint,source,settings,created,state,
                revision,result,claimed_at,elapsed,finished) VALUES (?,?, '{}','{}',?,'done',5,?,?,?,?)""",
                                   (key, key, finished - 9, json.dumps(result), finished - 7, 7, finished)).lastrowid
        ident = db.execute("""INSERT INTO production_jobs(project,filename,state,created,updated,finished,
            editorial_id,technical,queued_at,claimed_at,technical_seconds)
            VALUES (?,?,?,?,?,?,?,'passed',?,?,?)""", (project, key + ".mp4", state, finished - 100,
                                                       finished, finished, editor_id, finished - 100,
                                                       finished - 40, 5)).lastrowid
        db.execute("""INSERT INTO production_evidence(job_id,run,file,detection,editorial,priority,diversity,evidence,report)
                      VALUES (?,1,?,?,?,?,?,?,'{}')""", (ident, key + ".mp4", detection, editorial, legacy,
                                                        9, json.dumps(evidence)))
    return ident


def environment(root):
    root.mkdir(exist_ok=True, parents=True)
    settings = automation.SettingsStore(root)
    settings.save({**automation.DEFAULT_SETTINGS, "timezone": "Etc/UTC"}, 0)
    editor = EditorialStore(settings.path)
    editor.migrate()
    store = Store(editor)
    store.migrate()
    return SimpleNamespace(store=store, settings=settings, root=root, current_evidence=lambda _, ev: ev["current"])


def test_curve_interpolation_and_no_fake_event_age_or_double_engagement():
    config = priority.validate(priority.DEFAULT_PRIORITY)
    for minute, score in [(0, 100), (5, 100), (10, 92.5), (15, 85), (22.5, 75), (30, 65),
                          (60, 40), (120, 20), (180, 10), (1000, 10)]:
        assert priority.freshness(minute, config["curve"]) == pytest.approx(score)
    clip = {"project": "live/session", "detection": 9, "editorial": 9, "diversity": 9, "priority": 8,
            "criteria": {name: {"score": 9 if name == "engagement" else 8} for name in WEIGHTS}}
    recent = priority.assess({**clip, "event_at": NOW - 300}, config, NOW)
    old = priority.assess({**clip, "event_at": NOW - 7200}, config, NOW)
    unknown = priority.assess({**clip, "event_at": NOW + 10}, config, NOW)
    evergreen = priority.assess({**clip, "event_kind": "evergreen"}, config, NOW)
    assert recent["level"] == "urgent" and recent["temporal_score"] > old["temporal_score"]
    assert recent["factors"]["quality"] == pytest.approx(8.6)
    assert unknown["age_minutes"] is evergreen["age_minutes"] is None
    assert unknown["event_kind"] == "live" and unknown["level"] == "high"
    assert evergreen["event_kind"] == "evergreen" and evergreen["factors"]["opportunity"] is None
    historical = priority.assess({**clip, "criteria": {"engagement": {"score": 10}}}, config, NOW)
    assert historical["factors"]["interaction"] is None
    assert clip["priority"] == 8 and clip["detection"] == clip["editorial"] == 9
    with pytest.raises(ValueError):
        priority.validate({**config, "curve": [90, 100, 60, 40, 30, 10]})


def test_dynamic_rank_keeps_original_and_five_distinct_including_after_restart(tmp_path):
    app = environment(tmp_path)
    pipeline = Pipeline(tmp_path)
    store = app.store
    items = [seed(store, NOW, str(i), legacy=9 - i / 10) for i in range(5)]
    for index, ident in enumerate(items):
        store.set_event(ident, 1, "conventional", NOW - (100 - index * 10) * 60)
    initial = operations.snapshot(app, pipeline, {}, now=NOW)
    assert len(initial["recommendations"]) == 5
    assert initial["recommendations"][0]["id"] == items[-1]
    duplicate = seed(store, NOW, "duplicate", evidence=reference("0"))
    store.set_event(duplicate, 1, "conventional", NOW - 20 * 60)
    newest = seed(store, NOW, "fresh", project="live/synthetic", legacy=7.1)
    store.set_event(newest, 1, "live", NOW - 60)
    rejected = seed(store, NOW, "rejected", state="rejected", detection=10, editorial=10)
    weak = seed(store, NOW, "weak", detection=5.9)
    stale = seed(store, NOW, "stale", evidence={**reference("stale"), "current": False})
    app.store = Store(EditorialStore(app.settings.path))
    result = operations.snapshot(app, Pipeline(tmp_path), {}, now=NOW)
    recommended = [item["id"] for item in result["recommendations"]]
    assert len(recommended) == 5 and recommended[0] == newest
    assert len({items[0], duplicate} & set(recommended)) <= 1
    assert {rejected, weak, stale}.isdisjoint(recommended)
    assert store.detail(newest)["priority"] == 7.1
    original = store.status(settings=app.settings.read()["settings"], current=app.current_evidence, now=NOW)
    assert next(row for row in original["jobs"] if row["id"] == newest)["detection"] == 8.6


def test_cup_does_not_fill_with_same_context_or_rejected_review(tmp_path):
    app = environment(tmp_path)
    first = seed(app.store, NOW, "first", with_editor=True)
    # Different SHA, source and final words but almost the same context.
    similar = reference("different")
    similar["tokens"] = reference("first")["tokens"][:-1] + ["variation"]
    second = seed(app.store, NOW, "similar", evidence=similar)
    third = seed(app.store, NOW, "reject", with_editor=True)
    app.store.remote_review(third, 1, 5, "rejected", "telegram:100")
    data = operations.snapshot(app, Pipeline(tmp_path), {}, now=NOW)
    assert data["recommendations"] == [{"id": first, "rank": 1}]
    assert any(row["id"] == second for row in data["clips"])
    assert app.store.detail(third)["remote_review"]["decision"] == "rejected"
    assert app.store.detail(third)["remote_review"]["origin"] == "Telegram"


def test_queue_priority_fairness_saturation_and_non_preemption(tmp_path):
    app = environment(tmp_path)
    store = app.store
    now = time.time()
    store.enqueue("recording", "old.mp4")
    store.enqueue("live/session", "new.mp4")
    job = store.claim("production")
    assert job["filename"] == "new.mp4"
    assert store.claim("other") is None
    store.change(job["id"], "production", "cancelled")
    with store.connect(write=True) as db:
        db.execute("UPDATE production_jobs SET queued_at=? WHERE filename='old.mp4'", (now - 901,))
    store.enqueue("live/session", "newer.mp4")
    assert store.claim("next")["filename"] == "old.mp4"

    pipeline = Pipeline(tmp_path)
    with pipeline.connect(write=True) as db:
        for index in range(MAX_PENDING):
            stamp = now - 1000 if index == 0 else now
            db.execute("""INSERT INTO jobs(id,session,state,created,updated,sealed_at,moment,profile,options,score,signals)
                VALUES (?,'123456abcdef','queued',?,?,?,0,'30-60','{}',?,'{}')""",
                       (f"{index:012x}", stamp, stamp, stamp, index + 6))
    pending = pipeline.operations_pending(now)
    assert pending["saturated"] and pending["jobs"][0]["id"] == "000000000000"
    assert pipeline._claim()["id"] == "000000000000"
    assert pipeline._claim() is None and all(row["state"] in ACTIVE for row in pending["jobs"])

    editor = EditorialStore(app.settings.path)
    with editor.connect(write=True) as db:
        for tag, stamp, metadata in (("low", now, "{}"), ("aged", now - 910, "{}"),
                                      ("fresh", now, '{"production":[1,1]}')):
            db.execute("""INSERT INTO editorial_jobs(clip_id,fingerprint,source,settings,created,state)
                VALUES (?,?,'{"original_score":9}',?,?,'queued')""", (tag, tag, metadata, stamp))
    active_id, *_ = editor.claim("editor")
    with editor.connect() as db:
        assert db.execute("SELECT clip_id FROM editorial_jobs WHERE id=?", (active_id,)).fetchone()[0] == "aged"
    assert editor.claim("second") is None
    editor.finish(active_id, "editor", "done", {}, None, 0)
    next_id, *_ = editor.claim("editor")
    with editor.connect() as db:
        assert db.execute("SELECT clip_id FROM editorial_jobs WHERE id=?", (next_id,)).fetchone()[0] == "fresh"


def test_persisted_operational_metrics_and_estimate_use_actual_samples(tmp_path):
    app = environment(tmp_path)
    pipeline = Pipeline(tmp_path)
    for index in range(5):
        seed(app.store, NOW, "job" + str(index), project="live/abcdef123456", with_editor=True)
    app.store.enqueue("waiting", "queue1.mp4")
    app.store.enqueue("waiting", "queue2.mp4")
    with app.store.connect(write=True) as db:
        db.execute("UPDATE production_jobs SET queued_at=? WHERE finished IS NULL", (NOW - 5,))
    with pipeline.connect(write=True) as db:
        db.execute("""INSERT INTO jobs(id,session,state,created,updated,sealed_at,claimed_at,moment,profile,options,
            score,signals,result,transcription_seconds,render_seconds,technical_seconds)
            VALUES('111111111111','abcdef123456','done',?,?,?,?,0,'30-60','{}',9,'{}','job0.mp4',10,13,3)""",
                   (NOW - 200, NOW - 130, NOW - 190, NOW - 180))
    data = operations.snapshot(app, Pipeline(tmp_path), {("live/abcdef123456", "job0.mp4"): (NOW - 10, NOW - 1)}, now=NOW)
    assert data["metrics"]["context"] == {"samples": 1, "median": 10, "p95": 10}
    assert data["metrics"]["detection_to_review"]["median"] == 180
    assert data["metrics"]["editorial"]["samples"] == 5
    assert data["metrics"]["consent_to_confirmation"]["median"] == 9
    assert data["production"][1]["eta_seconds"] == 40
    assert data["bottlenecks"][0] == "automation_queue"


def test_http_and_browser_updates_and_studio_decisions_without_telegram(server, chromium_page, monkeypatch):
    web, base = server
    app = web.app
    monkeypatch.setattr(app.production, "current_evidence", lambda _, evidence: evidence.get("current", True))
    app.editorial._migrate()
    now = time.time()
    ident = seed(app.production.store, now, "test-event", with_editor=True)
    app.telegram.connection = "reconnecting"
    modal = lambda path, data=None: request(base, "/api/production/" + path, data, app.token)
    assert modal("realtime")["clips"][0]["age_minutes"] is None
    with pytest.raises(HTTPError) as invalid:
        modal("event", {"id": ident, "run": 1, "event_at": now + 60, "evergreen": False})
    assert invalid.value.code == 400
    page, expect = chromium_page
    page.goto(base)
    page.get_by_role("button", name="Automation", exact=True).click()
    expect(page.get_by_role("heading", name="Real-Time Operations")).to_be_visible()
    expect(page.locator("#realtime-summary")).to_contain_text("1/5")
    first = page.locator(f'#realtime-ranking [data-realtime-id="{ident}"]')
    expect(first).to_contain_text("Sin verificar")
    first.get_by_role("button", name="Hora del acontecimiento").click()
    page.locator("#realtime-evergreen").check()
    page.get_by_role("button", name="Guardar referencia").click()
    expect(first).to_contain_text("Atemporal")
    other = seed(app.production.store, now, "new-live", project="live/another", with_editor=True)
    modal("event", {"id": other, "run": 1, "event_at": now - 30, "evergreen": False})
    expect(page.locator("#realtime-summary")).to_contain_text("2/5", timeout=9000)
    first.get_by_role("button", name="Aprobar editorialmente").click()
    expect(first).to_contain_text("Aprobado editorialmente")
    assert app.production.store.detail(ident)["remote_review"]["decision"] == "approved"
    assert app.production.store.detail(ident)["remote_review"]["origin"] == "Studio"
    with pytest.raises(HTTPError) as stale:
        modal("review", {"id": ident, "run": 1, "revision": 4, "decision": "rejected"})
    assert stale.value.code == 400
    assert app.tiktok.account is None


def test_legacy_saved_config_retains_three_recommendations(tmp_path):
    store = automation.SettingsStore(tmp_path)
    legacy = {key: value for key, value in automation.DEFAULT_SETTINGS.items() if key != "priority_engine"}
    legacy["daily_publication_limit"] = 3
    saved = store.save(legacy, 0)
    assert automation.SettingsStore(tmp_path).read() == saved
    assert saved["settings"]["daily_publication_limit"] == 3
    assert saved["settings"]["priority_engine"] == priority.DEFAULT_PRIORITY
    copied = deepcopy(saved["settings"])
    copied["priority_engine"]["weights"]["quality"] = 55
    with pytest.raises(ValueError):
        store.save(copied, 1)
