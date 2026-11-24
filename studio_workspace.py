"""Local views over existing Studio state for the Anqora dashboard."""
from datetime import datetime, timedelta
from urllib.parse import quote

import projects
from clip_workspace import media_info, media_stamp
import realtime_operations
from realtime_priority import distribution
from publishing_calendar import zone_for


def media_url(project, filename):
    base = "/".join(quote(part, safe="") for part in project.split("/"))
    return f"/media/{base}/clips/{quote(filename, safe='')}"


def snapshot(app, days):
    if days not in (1, 7, 30):
        raise ValueError("Elige un período de 1, 7 o 30 días.")
    settings = app.automation_settings.read()["settings"]
    zone = zone_for(settings["timezone"])
    now = datetime.now(zone)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = (today - timedelta(days=days - 1)).timestamp()
    app.editorial._migrate()
    realtime = realtime_operations.snapshot(app.production, app.live_production, {})
    sessions = app.live_list()
    active = [row for row in sessions if row["status"] in {"starting", "running"}]
    with app.live_lock:
        for item in active:
            session = app.live_sessions.get(item["id"])
            options = getattr(session, "options", {})
            item["aspect"] = options.get("aspect")
            item["height"] = options.get("live_max_height")
    enriched = {(row["project"], row["filename"]): row for row in realtime["clips"]}
    with app.production.store.connect() as db:
        rows = db.execute("""SELECT p.id,p.run,p.project,p.filename,p.active_filename,p.state,
              p.finished,p.seconds,p.claimed_at,e.detection,e.priority
              FROM production_jobs p LEFT JOIN production_evidence e ON p.id=e.job_id AND p.run=e.run""").fetchall()
        events = [dict(row) for row in db.execute("""SELECT p.filename,e.state,e.created,e.code
                 FROM production_events e JOIN production_jobs p ON p.id=e.job_id
                 WHERE e.state IN ('generated','ready','review','rejected','failed')
                 ORDER BY e.id DESC LIMIT 8""")]
    production = {(row["project"], row["filename"]): dict(row) for row in rows}
    by_media = {}
    with app.calendar.connect() as db:
        covers = {(row["project"], row["file"]): row["cover"]
                  for row in db.execute("SELECT project,file,cover FROM clip_art WHERE cover IS NOT NULL")}
    for entry in app.tiktok.exports():
        project, filename = entry["project"], entry["filename"]
        metrics = production.get((project, filename), {})
        ranking = enriched.get((project, filename), {})
        actual = filename
        try:
            media = app.tiktok._clip(project, actual)
            recent = media.stat().st_mtime
            thumbnail = media_url(project, media.with_suffix(".png").name) if media.with_suffix(".png").is_file() else None
            meta_path = media.with_suffix(".meta.json")
            meta = projects.read_json(meta_path) if meta_path.is_file() and meta_path.stat().st_size <= 128000 else {}
            if not isinstance(meta, dict):
                meta = {}
            subtitles = meta.get("subtitles")
            duration = subtitles.get("duration") if isinstance(subtitles, dict) else None
            if duration is None and all(type(meta.get(key)) in (int, float) for key in ("start", "end")):
                duration = meta["end"] - meta["start"]
            title = ranking.get("title") or meta.get("title") or media.stem
        except (OSError, ValueError, TypeError):
            continue
        key = (project, actual)
        if key in by_media and not metrics:
            continue
        cover = covers.get(key)
        if cover:
            thumbnail = app.clips.cover_url(project, actual, cover)
        try:
            facts = {key: value for key, value in media_info(media, media_stamp(media)).items() if key != "tracks"}
        except ValueError:
            facts = {"format": None, "width": None, "height": None}
        by_media[key] = {"project": project, "filename": actual, "job": metrics.get("id"),
                      "run": metrics.get("run"), "source": entry["project_name"], "title": title,
                      "status": metrics.get("state") or "exported", "created": recent, "duration": duration,
                      "score": metrics.get("detection"), "priority": ranking.get("temporal_score"),
                      "level": ranking.get("level"), "age": ranking.get("age_minutes"),
                      "review": (ranking.get("remote_review") or {}).get("decision"),
                      "url": media_url(project, actual), "thumbnail": thumbnail, **facts}
    clips = sorted(by_media.values(), key=lambda row: (-row["created"], row["filename"]))
    eligible = {row["job"]: row for row in clips if row["job"] is not None}
    prioritized = [eligible[row["id"]] for row in realtime["clips"] if row["id"] in eligible]
    prioritized.sort(key=lambda row: row["level"] != "urgent")
    calendar = app.calendar.snapshot()
    plans = [row for row in calendar["plans"] if row["status"] != "cancelled"]
    with app.live_production.connect() as db:
        live = db.execute("SELECT state,created,updated FROM jobs WHERE created>=? OR updated>=?", (cutoff, cutoff)).fetchall()
        pending_live = db.execute("SELECT count(*) FROM jobs WHERE state IN ('collecting','queued','transcribing','editing','quality')").fetchone()[0]
    period = [row for row in rows if row["finished"] is not None and row["finished"] >= cutoff]
    observed = [row["seconds"] for row in period if row["state"] == "ready" and row["claimed_at"] is not None]
    publishing = app.calendar.statistics(cutoff)
    latency = [row["timing"]["seconds"]["detection_to_review"] for row in realtime["clips"]
               if row["finished"] >= cutoff and row.get("timing")]
    counts = {"detected": sum(row["created"] >= cutoff for row in live),
              "finished": sum(row["created"] >= cutoff for row in clips),
              "rejected": sum(row["state"] == "rejected" for row in period),
              "recommended": len(realtime["recommendations"]),
              "pending": (sum(row["finished"] is None for row in rows)
                          + sum(app.busy(project["id"]) for project in app.list_projects())
                          + pending_live),
              "scheduled": publishing["scheduled"], "confirmed": publishing["manual"] + publishing["api"],
              "manual": publishing["manual"], "api": publishing["api"],
              "errors": sum(row["state"] == "failed" for row in period)
                        + sum(row["state"] == "error" and row["updated"] >= cutoff for row in live)
                        + publishing["errors"]}
    return {"timezone": zone.key, "active": active, "sessions": sessions, "clips": clips, "priority": prioritized,
            "events": events, "plans": plans, "statistics": counts,
            "processing": {**distribution(observed), "average": round(sum(observed) / len(observed), 2) if observed else None},
            "detection_latency": distribution(latency),
            "today": {"clips": sum(row["created"] >= today.timestamp() for row in clips),
                      "review": sum(row["state"] == "review" for row in rows)
                                + sum(not row["review"] for row in prioritized),
                      "recommended": len(realtime["recommendations"]), "limit": realtime["daily_limit"],
                      "urgent": realtime["urgent_count"]},
            "notifications": [row for row in prioritized if row["level"] == "urgent" and not row["review"]]
                             + [row for row in plans if row["status"] == "requires_action"]}
