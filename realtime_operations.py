"""Read-only Automation 3.1 view and version-aware editorial actions."""
import json
from statistics import median
import time

from editorial_semantic import eligible
import projects
from production_selection import local_day
from realtime_priority import assess, distinct, distribution, valid_number

RECENT = 120


def since(start, end):
    if valid_number(start, 0, 2**53) and valid_number(end, start, 2**53):
        return round(end - start, 3)
    return None


def _editorial(db, row):
    if row["editorial_id"] is None:
        return {}, {}
    # A completed file has an editorial ID only after the editor initializes.
    data = db.execute("""SELECT revision,created,claimed_at,finished,elapsed,
        json_extract(result,'$.criteria') AS criteria
        FROM editorial_jobs WHERE id=? AND state='done'""", (row["editorial_id"],)).fetchone()
    if not data:
        return {}, {}
    return dict(data), json.loads(data["criteria"] or "{}")


def _ranking(ranked, limit, start, end):
    picked = []
    for clip, evidence in ranked:
        if not start <= clip["finished"] < end or clip["temporal_score"] is None:
            continue
        if all(distinct(evidence, previous) for _, previous in picked):
            picked.append((clip, evidence))
            if len(picked) == limit:
                break
    return [row["id"] for row, _ in picked]


def _timing(row, editorial, live, conventional, publication):
    queued = row["queued_at"]
    editorial_this_run = queued is not None and editorial and editorial["created"] >= queued
    ready = row["finished"]
    review = row.get("remote_review") or {}
    approved = review.get("created") if review.get("decision") == "approved" else None
    consent, published = publication if publication else (None, None)
    return {
        "timestamps": {"detected": live.get("detected_at") or conventional.get("detected_at"),
                       "context_preserved": live.get("sealed_at"),
                       "media_available": live.get("available_at") or conventional.get("available_at"),
                       "automation_claimed": row["claimed_at"],
                       "ready": ready, "approved": approved, "consent": consent, "publication_observed": published},
        "seconds": {
            "context": since(live.get("detected_at"), live.get("sealed_at")),
            "live_queue": since(live.get("sealed_at"), live.get("claimed_at")),
            "transcription": live.get("transcription_seconds"),
            "source_transcription": (conventional.get("source_transcription") or {}).get("seconds"),
            "render": live.get("render_seconds", conventional.get("render_seconds")),
            "live_technical": live.get("technical_seconds"),
            "automation_queue": since(queued, row["claimed_at"]), "technical": row["technical_seconds"],
            "editorial_queue": since(editorial["created"], editorial["claimed_at"]) if editorial_this_run else None,
            "editorial": editorial["elapsed"] if editorial_this_run else None,
            "detection_to_review": since(live.get("detected_at") or conventional.get("detected_at"), ready),
            "queue_to_review": since(queued, ready) if not live else None,
            "event_to_review": since(row["event_at"], ready) if row["event_kind"] != "evergreen" else None,
            "review_to_approval": since(ready, approved),
            "consent_to_confirmation": since(consent, published),
        },
    }


def publication_receipts(service):
    """Read existing TikTok receipts without contacting the platform."""
    receipts = {}
    if not service.lock.acquire(blocking=False):
        return receipts
    try:
        for row in service._account_history():
            key = (row.get("project"), row.get("filename"))
            start = row.get("created")
            confirmed = row.get("checked_at") if row["status"] == "PUBLISH_COMPLETE" else None
            if key not in receipts or (confirmed and not receipts[key][1]):
                receipts[key] = start, confirmed
    finally:
        service.lock.release()
    return receipts


def snapshot(worker, pipeline, receipts, *, now=None):
    now = time.time() if now is None else now
    settings = worker.settings.read()["settings"]
    limit = settings["daily_publication_limit"]
    day, start, end = local_day(now, settings["timezone"])
    store = worker.store
    ranked = []
    samples = {}
    seen_transcripts = set()
    project_times = {}
    with store.connect() as db:
        # Prior days contribute to the comparison panel. Every eligible clip from
        # the current day remains available to fill the recommendation cap.
        rows = db.execute("""SELECT p.*,e.file,e.evidence,e.detection,e.editorial,e.diversity,
            e.priority,o.event_kind,o.event_at
            FROM production_jobs p JOIN production_evidence e ON p.id=e.job_id AND p.run=e.run
            LEFT JOIN production_opportunity o ON o.job_id=p.id
            WHERE p.state='ready' AND p.technical='passed' AND p.finished<=?
              AND e.priority IS NOT NULL AND e.duplicate IS NULL AND e.editorial IS NOT NULL
              AND e.detection>=? AND (p.finished>=? OR p.id IN (
                  SELECT id FROM production_jobs WHERE state='ready' ORDER BY finished DESC LIMIT ?))
            ORDER BY p.finished DESC,p.id DESC""", (now, max(6, settings["minimum_clip_score"]), start, RECENT)).fetchall()
        for record in rows:
            row = dict(record)
            evidence = json.loads(row.pop("evidence"))
            if not worker.current_evidence(row, evidence):
                continue
            editorial, row["criteria"] = _editorial(db, row)
            row["remote_review"] = store._review(db, row["id"], row["run"], row["editorial_id"])
            assessment = assess(row, settings["priority_engine"], now)
            clip = {key: row[key] for key in ("id", "run", "project", "filename", "file", "state", "finished",
                                             "detection", "editorial", "priority", "remote_review")}
            clip.update(assessment, revision=editorial.get("revision"))
            if len(samples.get("jobs", [])) < RECENT:
                live = pipeline.operations_timing(row["project"], row["filename"])
                conventional = {}
                if not row["project"].startswith("live/"):
                    project = row["project"]
                    if project not in project_times:
                        try:
                            path = projects.project_path(worker.root, project)
                            project_times[project] = projects.read_project(path).get("automation_render_metrics", {})
                        except (OSError, ValueError):
                            project_times[project] = {}
                    conventional = project_times[project].get(row["filename"], {})
                receipt = receipts.get((row["project"], row["file"])) or receipts.get((row["project"], row["filename"]))
                clip["timing"] = _timing(row, editorial, live, conventional, receipt)
                source_key = (row["project"], (conventional.get("source_transcription") or {}).get("key"))
                for name, value in clip["timing"]["seconds"].items():
                    if name == "source_transcription" and source_key in seen_transcripts:
                        continue
                    samples.setdefault(name, []).append(value)
                seen_transcripts.add(source_key)
                samples.setdefault("jobs", []).append(since(row["claimed_at"], row["finished"]))
            if not row["remote_review"] or row["remote_review"]["decision"] == "approved":
                ranked.append((clip, evidence))
        waiting = store.waiting(db, now, 30)
        active = db.execute("""SELECT id,project,filename,state,claimed_at FROM production_jobs
                            WHERE owner IS NOT NULL AND finished IS NULL""").fetchone()
        paused = bool(db.execute("SELECT paused FROM production_config WHERE id=1").fetchone()[0])
        queued_count = db.execute("SELECT count(*) FROM production_jobs WHERE finished IS NULL AND owner IS NULL").fetchone()[0]
    ranked.sort(key=lambda entry: (-(entry[0]["temporal_score"] if entry[0]["temporal_score"] is not None else -1),
                                   -entry[0]["priority"], entry[0]["id"]))
    recommendation_ids = _ranking(ranked, limit, start, end)
    suggestions = [{"id": ident, "rank": index} for index, ident in enumerate(recommendation_ids, 1)]
    suggested = set(recommendation_ids)
    # Keep all recommendations visible even if quieter historical clips score higher.
    shown = [row for row, _ in ranked[:RECENT]]
    shown_ids = {row["id"] for row in shown}
    shown.extend(row for row, _ in ranked if row["id"] in suggested - shown_ids)
    service_times = [value for value in samples.pop("jobs", []) if value is not None]
    eta_service = median(service_times) if not paused and len(service_times) >= 5 else None
    remaining = 0 if not active else eta_service - (now - active["claimed_at"]) if eta_service is not None and active["claimed_at"] else None
    pending = [{
        "id": item["id"], "project": item["project"], "filename": item["filename"], "state": item["state"],
        "eta_seconds": round(remaining + index * eta_service, 1)
        if eta_service is not None and remaining is not None and remaining >= 0 else None,
    } for index, item in enumerate(waiting)]
    if active:
        pending.insert(0, {**dict(active), "eta_seconds": None})
    distributions = {name: distribution(values) for name, values in samples.items()}
    stages = {"context", "live_queue", "transcription", "source_transcription", "render", "live_technical",
              "automation_queue", "technical", "editorial_queue", "editorial"}
    bottlenecks = sorted((name for name in stages if distributions.get(name, {}).get("median") is not None),
                         key=lambda name: (-distributions[name]["median"], name))[:3]
    return {"day": day, "daily_limit": limit, "recommendations": suggestions, "clips": shown,
            "urgent_count": sum(clip["level"] == "urgent" for clip, _ in ranked), "eligible_count": len(ranked),
            "production": pending, "queue_count": queued_count, "paused": paused, "metrics": distributions,
            "bottlenecks": bottlenecks, "live_production": pipeline.operations_pending(now)}


def decision(worker, job, run, revision, choice):
    row = worker.store.review_version(job, run, revision)
    result = json.loads(row["result"])
    minimum = worker.settings.read()["settings"]["minimum_clip_score"]
    if (row["detection"] is None or row["detection"] < max(6, minimum)
            or not eligible(result) or result.get("standalone") is not True
            or not worker.current_evidence(row, json.loads(row["evidence"]))):
        raise ValueError("La evidencia o la versión del clip cambió. Solicita una reevaluación.")
    return worker.store.remote_review(job, run, revision, choice, "studio")
