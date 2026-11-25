"""Anqora Media Studio: loopback browser UI with a serial, resumable project queue."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import secrets
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit
import webbrowser

import automation
import branding
import editorial
import editorial_providers
import editorial_store
import live_production
import projects
import production
import publishing_calendar
import instagram_import
import publishing_platforms
from assisted_publishing import Preparation
import clip_workspace
import realtime_operations
from realtime_priority import valid_number
import render_clips
import subtitle_repair
import telegram_gateway
from telegram_api import TelegramError
import studio_settings
import studio_system
import studio_windows
import tiktok
import video_types
import asset_lifecycle
import watermark
from unicode_io import configure_utf8, utf8_environment

HERE = Path(__file__).resolve().parent
STATIC = HERE / "assets" / "studio"
LIVE_ACTIVE = {"starting", "running"}


class LiveAlreadyActive(ValueError):
    def __init__(self, session_id=None):
        super().__init__("Ya hay una sesión activa o iniciándose para esta fuente.")
        self.session_id = session_id


def live_source_identity(source):
    """Return an in-memory identity and a label safe to persist."""
    url = urlsplit(source)
    host = url.hostname or ""
    parts = url.path.strip("/").split("/")
    if host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"):
        if host == "youtu.be":
            video = parts[0]
        elif parts == ["watch"]:
            video = next(iter(parse_qs(url.query).get("v", [])), "")
        else:
            video = parts[1] if len(parts) == 2 and parts[0] in {"live", "shorts", "embed"} else ""
        if re.fullmatch(r"[\w-]{11}", video, re.ASCII):
            return ("youtube", video), "YouTube · " + video
    for domain, name in (("twitch.tv", "Twitch"), ("kick.com", "Kick")):
        if host in {domain, "www." + domain} and len(parts) == 1 and re.fullmatch(r"[a-zA-Z0-9_]+", parts[0]):
            channel = parts[0].lower()
            return (domain, channel), name + " · " + channel
    # Query strings and credentials can distinguish direct feeds. Keep them only
    # in memory so signed URLs and RTMP stream keys never enter the manifest.
    port = url.port
    if port == {"https": 443, "http": 80}.get(url.scheme):
        port = None
    return (url.scheme, host, port, url.username, url.password, url.path, url.query), host


# Studio assets must not depend on the host's MIME mappings (including the Windows registry).
STATIC_FILES = {
    "": ("index.html", "text/html; charset=utf-8"),
    "app.js": ("app.js", "text/javascript; charset=utf-8"),
    "publications.js": ("publications.js", "text/javascript; charset=utf-8"),
    "automation.js": ("automation.js", "text/javascript; charset=utf-8"),
    "watermark.js": ("watermark.js", "text/javascript; charset=utf-8"),
    "editorial.js": ("editorial.js", "text/javascript; charset=utf-8"),
    "subtitles.js": ("subtitles.js", "text/javascript; charset=utf-8"),
    "production.js": ("production.js", "text/javascript; charset=utf-8"),
    "realtime.js": ("realtime.js", "text/javascript; charset=utf-8"),
    "workspace.js": ("workspace.js", "text/javascript; charset=utf-8"),
    "clip-workspace.js": ("clip-workspace.js", "text/javascript; charset=utf-8"),
    "calendar.js": ("calendar.js", "text/javascript; charset=utf-8"),
    "anqora.css": ("anqora.css", "text/css; charset=utf-8"),
    "anqora.svg": ("anqora.svg", "image/svg+xml"),
    "anqora-mark.svg": ("anqora-mark.svg", "image/svg+xml"),
    "system.js": ("system.js", "text/javascript; charset=utf-8"),
    "telegram.js": ("telegram.js", "text/javascript; charset=utf-8"),
    "chopify.ico": ("chopify.ico", "image/x-icon"),
    "style.css": ("style.css", "text/css; charset=utf-8"),
}


def config_payload(root=None):
    """Config fields for video types and available clipping formats."""
    custom, warnings = video_types.load_custom_formats(root)
    return {
        "video_types": list(video_types.VIDEO_TYPES),
        "formats": {**video_types.FORMATS, **custom},
        "format_warnings": warnings,
    }


def save_format(body, root=None):
    """Validate and persist a custom clipping format."""
    return video_types.save_custom_format(body, root=root)


def project_detail(root, project_id):
    """Build the JSON payload for one project, including selection.json."""
    path = projects.project_path(root, project_id)
    if asset_lifecycle.hidden(root, project_id):
        raise FileNotFoundError("Proyecto en la papelera.")
    state = projects.read_project(path)
    trash = asset_lifecycle.entries(root)
    data = {"project": state, "busy": False, "candidates": [],
            "transcript": None, "selection": {}, "files": [], "log": ""}
    for filename, key in [("segments.json", "candidates"),
                          ("transcript.json", "transcript"),
                          ("selection.json", "selection")]:
        if (path / filename).exists():
            data[key] = projects.read_json(path / filename)
    for directory in ("previews", "clips"):
        for file in sorted((path / directory).glob("*")):
            if (file.suffix in {".mp4", ".srt", ".png", ".json"}
                    and (directory != "clips" or not asset_lifecycle.hidden(root, project_id, file.name, records=trash))):
                data["files"].append({"name": file.name, "kind": directory,
                                      "url": f"/media/{project_id}/{directory}/{file.name}"})
    if (path / "job.log").exists():
        with (path / "job.log").open("rb") as log:
            offset = max(0, os.fstat(log.fileno()).st_size - 12000)
            log.seek(max(0, offset - 3))
            tail = log.read()
            start = min(offset, 3)
            # The displayed tail may start inside a multibyte character.
            while start and start < len(tail) and (tail[start] & 0xC0) == 0x80:
                start -= 1
            data["log"] = tail[start:].decode("utf-8", errors="backslashreplace")
    transcript = data.get("transcript") or {}
    source_meta = state.get("source_meta") or {}
    render_from_sections = bool(
        source_meta.get("render_from_sections")
        or transcript.get("render_from_sections"))
    video = transcript.get("video") or state.get("video")
    data.update({
        "source_meta": source_meta,
        "progress": state.get("progress") or {},
        "render_errors": state.get("render_errors") or [],
        "playable_source": bool(
            not render_from_sections and video
            and Path(video).is_file()),
        "detection": state.get("detection"),
        "format": state.get("format"),
        "format_recipe": state.get("format_recipe"),
    })
    return data


class Studio:
    def __init__(self, root, installation=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        asset_lifecycle.recover(self.root)
        (self.root / "brands").mkdir(exist_ok=True)
        self.token = secrets.token_urlsafe(32)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chopify")
        self.lock = threading.RLock()
        self.futures = {}
        self.resources = threading.Lock()
        self.installation = installation
        self.identity = installation.identity() if installation else None
        self.active_requests = 0
        self.requests_idle = threading.Condition(self.lock)
        profile = installation.read()["tiktok_profile"] if installation else "production"
        self.tiktok = tiktok.TikTokStudio(self.root, self.busy, installation.vault if installation else None, profile)
        self.automation_settings = automation.SettingsStore(self.root)
        self.watermark = watermark.Store(self.root)
        self.editorial = editorial.Engine(self.automation_settings, self.tiktok._clip, resources=self.resources)
        self.subtitle_repair = subtitle_repair.Repair(self.root, self.tiktok._clip, resources=self.resources)
        self.processes = set()
        self.live_sessions = {}
        self.live_lock = threading.Lock()
        self.live_starting = set()
        self.closing = False
        self.closed = False
        for path in self.root.glob("*/project.json"):
            try:
                state = projects.read_project(path.parent)
                if state["status"] in {"queued", "running"}:
                    state.update(status="interrupted", error="Studio stopped. Resume this project to continue.")
                    projects.save_project(path.parent, state)
            except (ValueError, OSError, KeyError):
                continue

        self.production = production.Production(self.root, self.editorial, self.automation_settings,
                                               self.tiktok._clip, self._production_catalog, self.resources)
        self.live_production = live_production.Pipeline(self.root, self.resources, self._production_clip)
        self.live_production.start()
        self.calendar = publishing_calendar.Calendar(self.root, self.production, self.tiktok, self.automation_settings)
        self.growth = self.calendar.growth
        self.preparation = Preparation(self.calendar)
        self.clips = clip_workspace.Clips(self.root, self.calendar, self.tiktok, self.editorial)
        self.clip_jobs = {}
        self.calendar.recover_dispatches()
        self.preparing_publications = False
        self.schedule_stop = threading.Event()
        self.schedule_thread = threading.Thread(target=self._deliver_scheduled, name="studio-calendar", daemon=True)
        self.system = studio_system.System(installation or studio_settings.Settings(HERE), self)
        self.telegram = telegram_gateway.Gateway(self, installation)
        self.telegram.start()
        self.schedule_thread.start()

    def _deliver_scheduled(self):
        while not self.schedule_stop.wait(5):
            if not self.closing:
                try:
                    self.calendar.deliver_due()
                    settings = self.preparation.settings()
                    if settings["mode"] == "manual" or not settings["platforms"]:
                        continue
                    with self.lock:
                        if self.closing or not self.resources.acquire(blocking=False):
                            continue
                        self.preparing_publications = True
                    try:
                        self.preparation.prepare_one(self.tiktok.exports, self.clips)
                    finally:
                        self.resources.release()
                        with self.lock:
                            self.preparing_publications = False
                except (OSError, sqlite3.Error):
                    continue

    def render_clip_version(self, project, filename, aspect):
        with self.lock:
            if any(not item.done() for item in self.clip_jobs.values()):
                raise ValueError("Espera a que termine la exportación anterior.")
            self.tiktok._clip(project, filename)
            while len(self.clip_jobs) >= 24:
                self.clip_jobs.pop(next(iter(self.clip_jobs)))
            ident = secrets.token_hex(12)

            def render_version():
                with self.resources:
                    try:
                        return {"state": "ready", "file": self.clips.reexport(project, filename, aspect)}
                    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                        return {"state": "error", "error": "No se pudo exportar. Comprueba el original, los subtítulos y FFmpeg."}
            self.clip_jobs[ident] = self.executor.submit(render_version)
            return ident

    def purge_assets(self, ids, confirmation):
        with self.requests_idle:
            # A previous response may reach the browser just before its request is released.
            if not self.requests_idle.wait_for(lambda: self.active_requests <= 1, timeout=0.5):
                raise ValueError("Espera a que terminen las operaciones en curso antes de eliminar archivos.")
            with self.tiktok.lock:
                if any(self.activity().values()):
                    raise ValueError("Espera a que terminen los trabajos y las capturas antes de eliminar archivos.")
                selected = [row for row in asset_lifecycle.entries(self.root) if row["id"] in ids]
                with self.calendar.connect(write=True) as db:
                    for row in selected:
                        if db.execute("""SELECT 1 FROM plans WHERE project=? AND (? IS NULL OR file=?)
                        AND status NOT IN ('cancelled','published') LIMIT 1""", (row["project"], row["file"], row["file"])).fetchone():
                            raise ValueError("Cancela o resuelve las programaciones relacionadas antes de eliminar los archivos.")
                    return asset_lifecycle.purge(self.root, ids, confirmation)

    def _production_catalog(self):
        from live_clips import PRODUCTION_REPAIR, live_exports

        trash = asset_lifecycle.entries(self.root)
        for path in self.root.glob("*/project.json"):
            try:
                projects.project_path(self.root, path.parent.name)
                state = projects.read_project(path.parent)
                if self.busy(path.parent.name):
                    continue
                outputs = set(state.get("last_outputs", [])) | set((state.get("renders") or {}).values())
                for output in sorted(outputs):
                    clip = Path(output)
                    if (clip.suffix == ".mp4" and clip.parent.name == "clips" and clip.is_file()
                            and not asset_lifecycle.hidden(self.root, path.parent.name, clip.name, records=trash)):
                        yield path.parent.name, clip.name
            except (OSError, ValueError, TypeError, KeyError):
                continue
        for clip in live_exports(self.root):
            if (not PRODUCTION_REPAIR.fullmatch(clip["filename"])
                    and not asset_lifecycle.hidden(self.root, clip["project"], clip["filename"], records=trash)):
                yield clip["project"], clip["filename"]

    def _production_clip(self, project, filename):
        try:
            self.production.enqueue(project, filename)
        except (sqlite3.Error, automation.SettingsUnavailable, OSError):
            # The persistent output catalog reconciles events after the database is writable.
            pass

    def busy(self, project_id):
        future = self.futures.get(project_id)
        return future is not None and not future.done()

    def list_projects(self):
        result = []
        trash = asset_lifecycle.entries(self.root)
        for path in self.root.glob("*/project.json"):
            try:
                if not asset_lifecycle.hidden(self.root, path.parent.name, records=trash):
                    result.append(projects.read_project(path.parent))
            except (ValueError, OSError):
                continue
        return sorted(result, key=lambda p: p["created"], reverse=True)

    def import_project(self, source, options, authorization):
        if isinstance(source, str):
            auth = projects.validate_instagram(source.strip(), authorization)
            if auth:
                shortcode = auth["url"].split("/")[-2]
                for project in self.list_projects():
                    old = project.get("instagram_import") or {}
                    old_code = str(old.get("url", "")).rstrip("/").rsplit("/", 1)[-1]
                    if old.get("authorized") and old_code == shortcode:
                        return project, True
        return projects.create_project(self.root, source, options, instagram=authorization), False

    def enqueue(self, project_id, action, preview=False, pick=None, *, output=None, recovery=None):
        if action not in {"prepare", "render", "all"}:
            raise ValueError("Unknown action.")
        if not isinstance(preview, bool):
            raise ValueError("Preview must be true or false.")
        if pick is not None and (not isinstance(pick, str) or not re.fullmatch(r"\d+(,\d+)*", pick)):
            raise ValueError("Select numbered clips, for example 1,3,5.")
        with self.lock:
            path = projects.project_path(self.root, project_id)
            if self.busy(project_id):
                raise ValueError("This project is already queued or running.")
            state = projects.read_project(path)
            if recovery is not None:
                pending = state.get("render_recovery") or {}
                if recovery not in {"stable", "subtitles", "watermark"} or pending.get("alternative") != recovery:
                    raise ValueError("La alternativa de render ya no está disponible.")
                preview, pick = pending["preview"], pending["pick"]
            if output is not None:
                state["options"] = projects.validate_options({**state["options"], **output})
            if action in {"prepare", "all"} and state.get("instagram_import"):
                (path / instagram_import.CANCEL_FILE).unlink(missing_ok=True)
                if (state["status"] in instagram_import.RETRY_STATUSES
                        and state["stage"] in instagram_import.DOWNLOAD_STAGES and state.get("error")):
                    state.setdefault("instagram_errors", []).append({"at": state["updated"], "error": state["error"]})
            state.update(status="queued", stage=action, error=None)
            projects.save_project(path, state)
            future = self.executor.submit(self._run, path, action, preview, pick, recovery)
            self.futures[project_id] = future
            if action != "prepare" and not preview and not recovery:
                future.add_done_callback(lambda _: self._editorial_exports(project_id, path))

    def _editorial_exports(self, project_id, path):
        try:
            state = projects.read_project(path)
        except (OSError, ValueError):
            return
        if state["status"] != "complete" and state.get("stage") != "render incomplete":
            return
        for output in state.get("last_outputs", []):
            self._production_clip(project_id, Path(output).name)

    def _run(self, path, action, preview, pick, recovery=None):
        cmd = [sys.executable, "-X", "utf8", str(HERE / "chopify.py"), "--project", str(path),
               "--action", action, "--out", str(path / ("previews" if preview else "clips"))]
        # Studio jobs run unattended: never wait for interactive CLI questions.
        cmd.extend(["--no-input", "--automation-export"])
        if recovery:
            cmd.extend(["--render-recovery", recovery])
        if preview:
            cmd.append("--preview")
        if pick:
            cmd += ["--clips", pick]
        try:
            with self.resources, (path / "job.log").open("a", encoding="utf-8") as log:
                print(f"\n--- {action} {time.strftime('%Y-%m-%d %H:%M:%S')} ---", file=log, flush=True)
                with self.lock:
                    if self.closing:
                        return
                    child = subprocess.Popen(cmd, cwd=HERE, stdin=subprocess.DEVNULL, stdout=log,
                                             stderr=subprocess.STDOUT, env=utf8_environment(),
                                             start_new_session=os.name != "nt")
                    self.processes.add(child)
                code = child.wait()
                with self.lock:
                    self.processes.discard(child)
            if code:
                state = projects.read_project(path)
                state.update(status="interrupted" if self.closing else ("review" if state.get("stage") == "render recovery" else
                                 "cancelled" if state["status"] == "cancelled" else "failed"),
                             error="Studio se cerró. Reanuda el proyecto para continuar." if self.closing else (
                                 state.get("error") or "Job failed. Open the job log for setup or input details."))
                projects.save_project(path, state)
        except Exception as exc:
            state = projects.read_project(path)
            state.update(status="failed", error=str(exc))
            projects.save_project(path, state)

    def start_live(self, source, options, brief_text=None):
        import live
        from live_capture import check_capture_tools, resolve_live_source
        if len(source) > 2000:
            raise ValueError("Provide a live stream URL of at most 2000 characters.")
        key, label = live_source_identity(source)
        with self.live_lock:
            for item in self.live_sessions.values():
                if getattr(item, "source_key", None) == key and (
                        item.status in LIVE_ACTIVE or item.buffer and item.buffer.alive):
                    raise LiveAlreadyActive(item.id)
            if key in self.live_starting:
                raise LiveAlreadyActive()
            self.live_starting.add(key)
        try:
            check_capture_tools()
            resolved = resolve_live_source(source, options)
            with self.lock:
                if self.closing:
                    raise ValueError("Studio is closing. Restart it before starting a live session.")
                session = live.LiveSession(resolved, options, self.root, brief_text=brief_text, pipeline=self.live_production)
                session.source_key, session.source_label = key, label
                session.writer.resources = self.resources
                session.on_clip = lambda path: self._production_clip(f"live/{session.id}", Path(path).name)
                session._save()
                with self.live_lock:
                    self.live_sessions[session.id] = session
                threading.Thread(target=session.start, name=f"live-{session.id}", daemon=True).start()
            return session
        finally:
            with self.live_lock:
                self.live_starting.remove(key)

    def stop_live(self, session_id):
        with self.live_lock:
            session = self.live_sessions.get(session_id)
        if session is None:
            raise ValueError("Unknown live session.")
        session.stop()
        return session

    def export_live(self, session_id, seconds):
        with self.live_lock:
            session = self.live_sessions.get(session_id)
        if session is None:
            raise ValueError("Sesión Live desconocida.")
        session.request_export(seconds)

    def live_state(self, session_id):
        with self.live_lock:
            session = self.live_sessions.get(session_id)
        directory = session.dir if session else self.live_directory(session_id)
        detail = self.read_live_manifest(directory, active=session is not None)
        if detail.get("id") != session_id:
            raise FileNotFoundError(directory)
        records = {item["id"]: item for item in detail.get("triggers", []) if isinstance(item, dict) and "id" in item}
        for path in (directory / "records").glob("*.json"):
            if path.stem in records or path.resolve() != path:
                continue
            try:
                record = projects.read_json(path)
                if record["id"] == path.stem and record.get("files", {}).get("mp4") and not record.get("error"):
                    records[path.stem] = record
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
        trash = asset_lifecycle.entries(self.root)
        detail["triggers"] = sorted((record for record in records.values()
                                    if not asset_lifecycle.hidden(self.root, "live/" + session_id,
                                        Path(record.get("files", {}).get("mp4") or "").name, records=trash)),
                                    key=lambda item: item.get("start", 0))
        detail["production"] = self.live_production.status(session_id)
        if "export" in detail:
            detail["export"]["completed"] = sum(bool(row.get("files", {}).get("mp4")) and not row.get("error")
                                                for row in records.values())
        return detail

    def live_directory(self, session_id):
        from live_clips import live_clips_dir
        return live_clips_dir(self.root, session_id).parent

    @staticmethod
    def read_live_manifest(directory, *, active):
        manifest = directory / "live_session.json"
        if manifest.is_symlink():
            raise FileNotFoundError(manifest)
        data = projects.read_json(manifest)
        if not isinstance(data, dict) or data.get("status") not in LIVE_ACTIVE | {"stopped", "failed", "interrupted"}:
            raise ValueError("Invalid live session state.")
        if not active and data["status"] in LIVE_ACTIVE:
            data["status"] = "interrupted"
        return data

    def live_list(self):
        directories = {path.parent.name: path.parent for path in (self.root / "live").glob("*/live_session.json")}
        with self.live_lock:
            sessions = dict(self.live_sessions)
        directories.update({ident: session.dir for ident, session in sessions.items()})
        result = []
        for ident, directory in directories.items():
            try:
                if ident not in sessions:
                    directory = self.live_directory(ident)
                detail = self.read_live_manifest(directory, active=ident in sessions)
                if detail.get("id") != ident:
                    continue
                # The list is polled; avoid sending every clip, warning and FFmpeg error.
                summary = {key: detail.get(key) for key in ("id", "status", "source", "started_at", "stream_time")}
                if type(summary["started_at"]) not in (float, int):
                    summary["started_at"] = None
                export = detail.get("export") or {}
                scores = detail.get("recent_scores") or []
                score = (detail.get("health", {}).get("detection") or {}).get("last_score")
                count = export.get("completed")
                if count is None:
                    from live_clips import finished_names
                    count = len(finished_names(detail))
                else:
                    count += self.live_production.completed(ident)
                summary.update(
                    clips=count, score=score if score is not None else scores[-1] if scores else None,
                    errors=max(len(detail.get("errors") or []), export.get("errors", 0)),
                )
                result.append(summary)
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return sorted(result, key=lambda item: (item["status"] not in LIVE_ACTIVE, -(item["started_at"] or 0)))

    def activity(self):
        with self.lock:
            return {
                "projects": sum(not future.done() for future in [*self.futures.values(), *self.clip_jobs.values()]),
                "live": sum(getattr(session, "status", None) in {"starting", "running"}
                            or getattr(session, "exporting", lambda: False)()
                            for session in self.live_sessions.values()) + int(self.live_production.busy()),
                "production": self.production.active(),
                "editorial": self.editorial.busy(),
                "subtitle_repair": bool(self.subtitle_repair.future and not self.subtitle_repair.future.done()),
                "tiktok_transfer": self.tiktok.sending,
                "publishing_preparation": self.preparing_publications,
                "tiktok_verification": self.system.restoring(),
            }

    def quiesce(self, mode):
        with self.lock:
            if self.closing:
                raise ValueError("Studio ya está preparando el cierre.")
            self.closing = True
            self.production.pause_dispatch(True)
            if mode != "wait":
                self.editorial.pause_dispatch(True)

    def unquiesce(self):
        with self.lock:
            self.closing = False
            self.production.pause_dispatch(False)
            self.editorial.pause_dispatch(False)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = self.closing = True
            sessions = list(self.live_sessions.values())
            children = list(self.processes)
        self.schedule_stop.set()
        self.schedule_thread.join()
        self.telegram.close()
        self.production.pause_dispatch(True)
        self.editorial.pause_dispatch(True)
        for session in sessions:
            session.stop()
        self.live_production.close()
        for session in sessions:
            wait_export = getattr(session, "wait_for_export", None)
            if wait_export:
                wait_export()
        for child in children:
            if child.poll() is not None:
                continue
            if os.name == "nt":
                from ingest import _kill_tree
                _kill_tree(child)
            else:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.production.close()
        self.editorial.close()
        self.subtitle_repair.close(wait=True)
        self.system.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "ChopifyStudio/1.4.0"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; media-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_content(body, "application/json; charset=utf-8", status)

    def send_content(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def do_GET(self):
        if not self.valid_host():
            return self.send_json({"error": "Use the 127.0.0.1 Studio address."}, 403)
        try:
            location = urlsplit(self.path)
            if location.path in {tiktok.AUTHORIZE_START, tiktok.CALLBACK}:
                return self.oauth_navigation(location)
            parts = unquote(location.path).strip("/").split("/")
            if parts in (["api", "clip"], ["api", "clip-cover"], ["api", "clip-mark"]):
                query = parse_qs(location.query, max_num_fields=3)
                project, filename = (query.get(key, [""])[0] for key in ("project", "file"))
                if parts[-1] == "clip":
                    return self.send_json(self.app.clips.read(project, filename, query.get("platform", ["tiktok"])[0]))
                return self.send_file(self.app.clips.image(project, filename, query.get("id", [""])[0], parts[-1][5:]))
            if parts == ["api", "clip-render"]:
                ident = parse_qs(location.query, max_num_fields=1).get("id", [""])[0]
                with self.app.lock:
                    future = self.app.clip_jobs.get(ident)
                if future is None:
                    raise FileNotFoundError
                return self.send_json(future.result() if future.done() else {"state": "running"})
            if parts == ["api", "publishing-assist"]:
                return self.send_json({**self.app.preparation.settings(), "history": self.app.preparation.history()})
            if parts == ["api", "publishing-capabilities"]:
                return self.send_json(publishing_platforms.capabilities(self.app.tiktok))
            if parts == ["api", "growth"]:
                return self.send_json(self.app.growth.snapshot())
            if parts == ["api", "calendar"]:
                query = parse_qs(location.query, max_num_fields=2)
                return self.send_json(self.app.calendar.snapshot(query.get("id", [None])[0],
                                                                around=query.get("date", [None])[0]))
            if parts == ["api", "calendar-clip"]:
                job = int(parse_qs(location.query, max_num_fields=1).get("job", ["0"])[0])
                clip = self.app.calendar._clip(job)
                return self.send_json({key: clip[key] for key in ("title", "description", "hashtags", "status")})
            if parts == ["api", "workspace"]:
                from studio_workspace import snapshot
                days = int(parse_qs(location.query, max_num_fields=1).get("days", ["1"])[0])
                return self.send_json(snapshot(self.app, days))
            if parts == ["api", "health"] and self.app.identity:
                nonce = parse_qs(location.query, max_num_fields=1).get("nonce", [""])[0]
                return self.send_json({"proof": studio_settings.instance_proof(self.app.identity, nonce)})
            if parts == ["api", "system", "status"]:
                return self.send_json(self.app.system.status())
            if parts == ["api", "config"]:
                brands = sorted(p.stem for p in (self.app.root / "brands").glob("*.json"))
                caption_brands = {}
                for brand_path in (self.app.root / "brands").glob("*.json"):
                    try:
                        brand = branding.validate_brand(
                            projects.read_json(brand_path), brand_path.parent)
                    except (ValueError, OSError, KeyError):
                        continue
                    caption_brands[brand_path.stem] = {
                        key: brand[key] for key in branding.CAPTION_FIELDS
                        if brand[key] is not None
                    }
                payload = {
                    "token": self.app.token,
                    "managed_settings": self.app.installation is not None,
                    "defaults": projects.DEFAULT_OPTIONS,
                    "brands": brands,
                    "caption_brands": caption_brands,
                    "caption_presets": render_clips.CAPTION_STYLES,
                    "caption_defaults": branding.CAPTION_DEFAULTS,
                    "caption_aspects": {
                        name: {"font_size": size, "margin": margin}
                        for name, (_, _, size, margin) in render_clips.ASPECTS.items()
                    },
                    "caption_power_words": sorted(render_clips.POWER_WORDS),
                    "caption_emoji_rules": [
                        {"words": list(stems), "emoji": emoji}
                        for stems, emoji in render_clips.EMOJI_RULES
                    ],
                }
                payload.update(config_payload(self.app.root))
                return self.send_json(payload)
            if parts == ["api", "trash"]:
                return self.send_json({"items": asset_lifecycle.describe(self.app.root)})
            if parts == ["api", "projects"]:
                return self.send_json({"projects": self.app.list_projects()})
            if parts == ["api", "automation", "settings"]:
                return self.automation_settings_action()
            if parts == ["api", "telegram", "status"]:
                return self.telegram_action("status")
            if len(parts) == 3 and parts[:2] == ["api", "production"]:
                return self.production_action(parts[2], query=parse_qs(location.query, max_num_fields=2))
            if len(parts) == 3 and parts[:2] == ["api", "editorial"]:
                return self.editorial_action(parts[2], query=parse_qs(location.query, max_num_fields=4))
            if len(parts) == 3 and parts[:2] == ["api", "subtitle-repair"]:
                if parts[2] == "latest":
                    query = parse_qs(location.query, max_num_fields=2)
                    return self.send_json(self.app.subtitle_repair.latest(
                        query.get("project", [""])[0], query.get("filename", [""])[0]))
                return self.send_json(self.app.subtitle_repair.status(parts[2]))
            if len(parts) == 3 and parts[0] == "subtitle-repair":
                return self.send_file(self.app.subtitle_repair.asset(parts[1], parts[2]))
            if parts == ["api", "watermark"]:
                return self.send_json(self.app.watermark.read())
            if len(parts) == 2 and parts[0] == "watermark-image":
                if not re.fullmatch(r"[a-f0-9]{64}", parts[1]):
                    raise ValueError("Identificador de marca de agua no válido.")
                return self.send_content(self.app.watermark.preview(parts[1]), "image/png")
            if len(parts) == 3 and parts[:2] == ["api", "projects"]:
                data = project_detail(self.app.root, parts[2])
                data["busy"] = self.app.busy(parts[2])
                return self.send_json(data)
            if len(parts) == 5 and parts[:2] == ["media", "live"] and parts[3] == "clips":
                from live_clips import live_clip_path
                if asset_lifecycle.hidden(self.app.root, "live/" + parts[2], parts[4]):
                    raise FileNotFoundError
                return self.send_file(live_clip_path(self.app.root, parts[2], parts[4]))
            if parts and parts[0] == "media" and len(parts) >= 3:
                if asset_lifecycle.hidden(self.app.root, parts[1], parts[3] if len(parts) == 4 and parts[2] == "clips" else None):
                    raise FileNotFoundError
                path = projects.project_path(self.app.root, parts[1]).resolve()
                if parts[2:] == ["source"]:
                    source = projects.read_json(path / "transcript.json")["video"]
                    return self.send_file(Path(source))
                if len(parts) != 4 or parts[2] not in {"clips", "previews"}:
                    raise ValueError("Unknown media path.")
                file = (path / parts[2] / parts[3]).resolve()
                if file.parent != path / parts[2] or file.suffix not in {".mp4", ".srt", ".png", ".json"}:
                    raise ValueError("Invalid media path.")
                return self.send_file(file)
            if parts == ["api", "live"]:
                return self.send_json({"sessions": self.app.live_list()})
            if len(parts) == 3 and parts[:2] == ["api", "live"]:
                return self.send_json(self.app.live_state(parts[2]))
            asset = self.server.static_files.get("/".join(parts))
            if asset:
                return self.send_content(*asset)
            self.send_json({"error": "Not found."}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (sqlite3.Error, automation.SettingsUnavailable):
            self.send_json({"error": "El espacio local no está disponible."}, 503)
        except (OSError, KeyError):
            self.send_json({"error": "Project or file not found."}, 404)
        except (ValueError, TypeError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def oauth_navigation(self, location):
        with self.app.lock:
            if self.app.closing:
                return self.send_json({"error": "Studio está cerrando. Vuelve a conectar TikTok al iniciar."}, 409)
            self.app.active_requests += 1
        try:
            if location.path == tiktok.AUTHORIZE_START:
                url = self.app.tiktok.take_authorization_url(parse_qs(location.query, max_num_fields=1))
            else:
                self.app.tiktok.callback(parse_qs(location.query, max_num_fields=16), self.server.server_port)
                url = "/#publicaciones"
            self.send_response(303)
            self.send_header("Location", url)
            self.send_header("Content-Length", "0")
            self.end_headers()
        finally:
            with self.app.requests_idle:
                self.app.active_requests -= 1
                self.app.requests_idle.notify_all()

    def do_POST(self):
        origin = self.headers.get("Origin")
        expected = f"http://127.0.0.1:{self.server.server_port}"
        token = self.headers.get("X-Chopify-Token", "")
        if (not token.isascii() or not self.valid_host() or (origin is not None and origin != expected)
                or not secrets.compare_digest(token, self.app.token)):
            return self.send_json({"error": "Request rejected. Reload Studio and retry."}, 403)
        with self.app.lock:
            if self.app.closing:
                return self.send_json({"error": "Studio está preparando el cierre."}, 409)
            self.app.active_requests += 1
        try:
            return self._dispatch_POST()
        finally:
            with self.app.requests_idle:
                self.app.active_requests -= 1
                self.app.requests_idle.notify_all()

    def _dispatch_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 10_000_000:
                raise ValueError("Request must be JSON smaller than 10 MB.")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            parts = unquote(urlsplit(self.path).path).strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "clip"]:
                project, filename, platform = (body.get(key) for key in ("project", "file", "platform"))
                if parts[2] == "copy":
                    return self.send_json(self.app.clips.save(project, filename, platform, body))
                if parts[2] == "artwork":
                    return self.send_json(self.app.clips.artwork(project, filename, platform, body))
                if parts[2] == "render":
                    return self.send_json({"job": self.app.render_clip_version(project, filename, body.get("aspect"))}, 202)
                if parts[2] == "generate":
                    if platform not in clip_workspace.LIMITS:
                        raise ValueError("Elige una plataforma disponible.")
                    return self.editorial_action("generate", {"project": project, "filename": filename,
                        "platform": "facebook" if platform == "instagram" else platform, "force": True, "semantic": True})
            if parts == ["api", "trash", "purge"]:
                if not isinstance(body.get("ids"), list) or not all(isinstance(item, str) for item in body["ids"]):
                    raise ValueError("Selecciona los elementos de la papelera.")
                if len(body["ids"]) > 1000:
                    raise ValueError("Selecciona hasta 1000 elementos por operación.")
                return self.send_json(self.app.purge_assets(body["ids"], body.get("confirmation")))
            if parts == ["api", "publishing-assist"]:
                return self.send_json(self.app.preparation.settings(body))
            if parts == ["api", "growth", "import"]:
                return self.send_json(self.app.growth.ingest(body))
            if parts == ["api", "calendar", "drafts"]:
                return self.send_json(self.app.calendar.drafts(body, self.app.clips))
            if len(parts) == 3 and parts[:2] == ["api", "calendar"]:
                return self.send_json(self.app.calendar.action(parts[2], body))
            if len(parts) == 3 and parts[:2] == ["api", "system"]:
                return self.system_action(parts[2], body)
            if len(parts) == 3 and parts[:2] == ["api", "telegram"]:
                return self.telegram_action(parts[2], body)
            if len(parts) == 3 and parts[:2] == ["api", "tiktok"]:
                return self.tiktok_action(parts[2], body)
            if parts == ["api", "automation", "settings"]:
                return self.automation_settings_action(body)
            if len(parts) == 3 and parts[:2] == ["api", "production"]:
                return self.production_action(parts[2], body=body)
            if len(parts) == 3 and parts[:2] == ["api", "editorial"]:
                return self.editorial_action(parts[2], body)
            if len(parts) == 3 and parts[:2] == ["api", "subtitle-repair"]:
                if parts[2] not in {"diagnose", "transcribe", "preview", "export"}:
                    return self.send_json({"error": "Acción de subtítulos desconocida."}, 404)
                return self.send_json(self.app.subtitle_repair.start(parts[2], body), 202)
            if parts == ["api", "live"]:
                source = body.get("source")
                if not isinstance(source, str) or not source.strip():
                    raise ValueError("Provide a live source URL.")
                raw_options = body.get("options", {})
                if not isinstance(raw_options, dict):
                    raise ValueError("Live options must be a JSON object.")
                if "live_threshold" not in raw_options:
                    try:
                        minimum = self.app.automation_settings.read()["settings"]["minimum_clip_score"]
                    except automation.SettingsUnavailable as exc:
                        return self.send_json({"error": str(exc)}, 503)
                    raw_options = {**raw_options, "live_threshold": minimum}
                options = projects.validate_options(raw_options)
                brief_text = body.get("brief")
                if brief_text is not None and not isinstance(brief_text, str):
                    raise ValueError("Brief must be text.")
                try:
                    session = self.app.start_live(source.strip(), options, brief_text=brief_text)
                except LiveAlreadyActive as exc:
                    return self.send_json({"error": str(exc), "session_id": exc.session_id}, 409)
                except OSError:
                    return self.send_json({"error": "Could not save the live session. Check access to the Studio projects folder."}, 500)
                return self.send_json({"id": session.id}, 201)
            if len(parts) == 4 and parts[:2] == ["api", "live"] and parts[3] == "export":
                self.app.export_live(parts[2], body.get("seconds"))
                return self.send_json({"queued": True}, 202)
            with self.app.lock:
                if parts == ["api", "watermark"]:
                    if "settings" not in body or body.keys() - {"settings", "image_data"}:
                        raise ValueError("Envía los ajustes y el PNG de marca de agua.")
                    return self.send_json(self.app.watermark.save(body["settings"], body.get("image_data")))
                if len(parts) == 4 and parts[:2] == ["api", "live"] and parts[3] == "stop":
                    self.app.stop_live(parts[2])
                    return self.send_json({"ok": True})
                if parts == ["api", "formats"]:
                    return self.send_json(save_format(body, root=self.app.root), 201)
                if parts == ["api", "projects"]:
                    project, reused = self.app.import_project(body["source"], body.get("options", {}),body.get("instagram"))
                    return self.send_json({**project, "reused": True} if reused else project, 200 if reused else 201)
                if parts == ["api", "batch"]:
                    sources = body["sources"]
                    if not isinstance(sources, list) or not 1 <= len(sources) <= 100 or any(not isinstance(s, str) or not s.strip() for s in sources):
                        raise ValueError("Provide 1 to 100 sources.")
                    options = projects.validate_options(body.get("options", {}))
                    brief_text = body.get("brief")
                    if brief_text is not None and not isinstance(brief_text, str):
                        raise ValueError("Brief must be text.")
                    for source in sources:
                        projects.validate_instagram(source.strip(), body.get("instagram"))
                    result = []
                    for source in sources:
                        state, reused = self.app.import_project(source, options, body.get("instagram"))
                        if reused:
                            result.append({**state, "reused": True})
                            continue
                        if brief_text and brief_text.strip():
                            import campaign
                            campaign.save_rules(
                                projects.project_path(self.app.root, state["id"]),
                                campaign.parse_brief(brief_text))
                        self.app.enqueue(state["id"], "prepare")
                        result.append(state)
                    return self.send_json({"projects": result}, 201)
                if parts == ["api", "brands"]:
                    name = body["name"]
                    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", name):
                        raise ValueError("Preset name must use letters, numbers, - or _.")
                    brand = branding.validate_brand(body["preset"])
                    path = self.app.root / "brands" / f"{name}.json"
                    projects.atomic_json(path, brand)
                    return self.send_json({"name": name, "path": str(path)})
                if parts == ["api", "trash", "restore"]:
                    return self.send_json(asset_lifecycle.restore(self.app.root, body.get("id")))
                if parts == ["api", "trash", "clip"]:
                    project, filename = body.get("project"), body.get("filename")
                    with self.app.tiktok.lock:
                        if self.app.tiktok.sending:
                            raise ValueError("Espera a que termine el envío en curso.")
                        media = self.app.tiktok._clip(project, filename)
                        trashed = asset_lifecycle.send_to_trash(self.app.root, project, media.name, media.stem)
                    return self.send_json(trashed)
                if len(parts) == 4 and parts[:2] == ["api", "projects"]:
                    project_id, action = parts[2:]
                    path = projects.project_path(self.app.root, project_id)
                    if asset_lifecycle.hidden(self.app.root, project_id):
                        raise FileNotFoundError
                    state = projects.read_project(path)
                    if action == "retry-import":
                        if (not state.get("instagram_import") or path.resolve() != path
                                or state["status"] not in instagram_import.RETRY_STATUSES
                                or state["stage"] not in instagram_import.DOWNLOAD_STAGES):
                            raise ValueError("Este proyecto no tiene una descarga de Instagram pendiente de reintento.")
                        self.app.enqueue(project_id, "prepare")
                        return self.send_json({"ok": True}, 202)
                    if action == "cancel-import":
                        if (not state.get("instagram_import") or not self.app.busy(project_id)
                                or state["stage"] not in instagram_import.DOWNLOAD_STAGES):
                            raise ValueError("La descarga de Instagram ya no está activa.")
                        if path.resolve() != path:
                            raise ValueError("El proyecto de Instagram debe estar en el espacio local.")
                        try:
                            with (path / instagram_import.CANCEL_FILE).open("x"):
                                pass
                        except FileExistsError:
                            pass
                        return self.send_json({"ok": True})
                    if self.app.busy(project_id):
                        raise ValueError("Wait for this project's current job to finish before editing.")
                    if action == "manage":
                        command = body.get("action")
                        if path.is_symlink() or path.resolve() != path:
                            raise ValueError("La carpeta de proyecto debe ser local.")
                        if command == "trash":
                            with self.app.tiktok.lock:
                                if self.app.tiktok.sending:
                                    raise ValueError("Espera a que termine el envío en curso.")
                                trashed = asset_lifecycle.send_to_trash(
                                    self.app.root, project_id, None, state.get("name") or project_id)
                            return self.send_json(trashed)
                        if command == "rename":
                            name = body.get("name")
                            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
                                raise ValueError("El nombre debe contener de 1 a 120 caracteres.")
                            state["name"] = name.strip()
                        elif command == "archive" or command == "unarchive":
                            state["archived"] = command == "archive"
                        else:
                            raise ValueError("Acción de proyecto desconocida.")
                        projects.save_project(path, state)
                        return self.send_json(state)
                    if action == "settings":
                        settings = dict(body)
                        if "brand_name" in settings:
                            name = settings.pop("brand_name")
                            if name and (not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", name)):
                                raise ValueError("Invalid brand preset.")
                            settings["brand"] = str(self.app.root / "brands" / f"{name}.json") if name else None
                        state["options"] = projects.validate_options({**state["options"], **settings})
                        projects.save_project(path, state)
                    elif action == "transcript":
                        projects.edit_transcript(path, body["words"])
                    elif action == "candidates":
                        duration = projects.read_json(path / "transcript.json")["duration"]
                        segments = projects.validate_segments(body["candidates"], duration)
                        projects.atomic_json(path / "segments.json", segments)
                    elif action == "render-recovery":
                        recovery = body.get("alternative")
                        if recovery not in ("stable", "subtitles", "watermark") or body.get("confirmed") is not True:
                            raise ValueError("Confirma la alternativa de render.")
                        self.app.enqueue(project_id, "render", recovery=recovery)
                    elif action == "run":
                        if "output" in body:
                            output = body["output"]
                            if (body.get("action") != "render" or not isinstance(output, dict)
                                    or output.keys() - {"aspect", "layout", "camera_mode"}):
                                raise ValueError("Configuración de exportación no válida.")
                            self.app.enqueue(project_id, "render", body.get("preview", False), body.get("pick"), output=output)
                        else:
                            self.app.enqueue(project_id, body.get("action", "prepare"), body.get("preview", False), body.get("pick"))
                    else:
                        return self.send_json({"error": "Not found."}, 404)
                    return self.send_json({"ok": True})
            self.send_json({"error": "Not found."}, 404)
        except FileNotFoundError:
            self.send_json({"error": "Project or transcript not found. Prepare the project first."}, 404)
        except publishing_calendar.Conflict as exc:
            return self.send_json({"error": str(exc)}, 409)
        except (sqlite3.Error, automation.SettingsUnavailable):
            return self.send_json({"error": "El espacio local no está disponible."}, 503)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def system_action(self, action, body):
        system = self.app.system
        if action == "save":
            return self.send_json(system.save(body))
        if action == "diagnose":
            return self.send_json(system.diagnose())
        if action == "ollama":
            return self.send_json(system.diagnose(start=True))
        if action == "credentials":
            self.app.tiktok.change_credentials(body.get("profile"), body.get("action"), body.get("key"), body.get("secret"))
            system.restore_in_background()
            return self.send_json(self.app.tiktok.integrations())
        if action == "verify":
            return self.send_json(self.app.tiktok.verify_authorization(renew=body.get("renew") is True))
        if action == "forget":
            return self.send_json(self.app.tiktok.disconnect(local_only=True))
        if action == "shortcut":
            command = body.get("action")
            if command not in {"status", "create", "delete"}:
                raise ValueError("Selecciona una acción de acceso directo.")
            return self.send_json({"shortcut": studio_windows.shortcut(system.store.checkout, command)})
        if action == "choose-folder":
            return self.send_json({"path": studio_windows.choose_folder()})
        if action == "logs":
            system.open_logs()
            return self.send_json({"ok": True})
        if action == "update-check":
            return self.send_json(system.repository.check())
        if action in {"update", "shutdown"}:
            if body.get("confirmed") is not True:
                raise ValueError("Confirma la operación en Studio.")
            mode = "update" if action == "update" else body.get("mode")
            if mode == "update" and action != "update":
                raise ValueError("Elige una modalidad de cierre válida.")
            result = system.lifecycle(self.server, mode, body)
            try:
                return self.send_json(result)
            finally:
                system.stop_server(self.server, mode)
        return self.send_json({"error": "Acción de sistema desconocida."}, 404)

    def automation_settings_action(self, body=None):
        store = self.app.automation_settings
        try:
            if body is None:
                return self.send_json(store.read())
            if body.keys() != {"settings", "revision"}:
                raise ValueError("Envía los ajustes y la revisión de Automation.")
            return self.send_json(store.save(body["settings"], body["revision"]))
        except automation.SettingsConflict as exc:
            return self.send_json({"error": str(exc)}, 409)
        except automation.SettingsUnavailable as exc:
            return self.send_json({"error": str(exc)}, 503)

    def telegram_action(self, action, body=None):
        try:
            if body is None and action == "status":
                return self.send_json(self.app.telegram.status())
            return self.send_json(self.app.telegram.control(action, body))
        except TelegramError as exc:
            return self.send_json({"error": str(exc)}, 400 if exc.unauthorized else 503)
        except sqlite3.Error:
            return self.send_json({"error": "El almacenamiento local de Telegram no está disponible."}, 503)

    def production_action(self, action, body=None, query=None):
        worker = self.app.production
        store = worker.store

        def identifier(value):
            if type(value) is not int or not 1 <= value <= 2**63 - 1:
                raise ValueError("Identificador de producción inválido.")
            return value

        try:
            if body is None:
                if action == "realtime":
                    self.app.editorial._migrate()
                    return self.send_json(realtime_operations.snapshot(
                        worker, self.app.live_production, realtime_operations.publication_receipts(self.app.tiktok)))
                if action == "status":
                    before = query.get("before")
                    cursor = identifier(int(before[0])) if before else None
                    return self.send_json(worker.status(cursor, query.get("view", ["all"])[0]))
                if action == "job":
                    return self.send_json(store.detail(identifier(int(query.get("id", ["0"])[0]))))
            elif action == "event":
                if body.keys() != {"id", "run", "evergreen", "event_at"} or type(body["evergreen"]) is not bool:
                    raise ValueError("Indica el origen temporal del clip.")
                stamp = body["event_at"]
                if stamp is not None and (body["evergreen"] or not valid_number(stamp, 0, time.time())):
                    raise ValueError("La hora verificable del acontecimiento debe ser anterior a ahora.")
                job = identifier(body["id"])
                item = store.detail(job)
                kind = "evergreen" if body["evergreen"] else "live" if item["project"].startswith("live/") else "conventional"
                store.set_event(job, identifier(body["run"]), kind, stamp)
                return self.send_json({"ok": True})
            elif action == "review":
                if body.keys() != {"id", "run", "revision", "decision"} or body["decision"] not in (
                        "approved", "rejected", "correction"):
                    raise ValueError("Decisión editorial inválida.")
                revision = body["revision"]
                if type(revision) is not int or not 0 <= revision < 2**63:
                    raise ValueError("Revisión editorial inválida.")
                changed = realtime_operations.decision(worker, identifier(body["id"]), identifier(body["run"]),
                                                       revision, body["decision"])
                return self.send_json({"ok": True, "changed": changed})
            elif action in ("pause", "resume"):
                settings = store.config(paused=action == "pause")
                worker.wake.set()
                return self.send_json(settings)
            elif action == "settings":
                if body.keys() != {"max_repairs"} or type(body["max_repairs"]) is not int:
                    raise ValueError("Indica el máximo de reparaciones.")
                return self.send_json(store.config(max_repairs=body["max_repairs"]))
            elif action in ("cancel", "retry", "distinct"):
                job = identifier(body.get("id"))
                if action == "cancel":
                    editorial_id = store.cancel(job)
                    if editorial_id:
                        self.app.editorial.cancel(editorial_id)
                else:
                    store.retry(job, distinct=action == "distinct")
                worker.wake.set()
                return self.send_json(store.detail(job))
            elif action == "enqueue":
                project, filename = body.get("project"), body.get("filename")
                self.app.tiktok._clip(project, filename)
                worker.enqueue(project, filename)
                return self.send_json({"ok": True}, 202)
        except automation.SettingsUnavailable as exc:
            return self.send_json({"error": str(exc)}, 503)
        except sqlite3.Error:
            return self.send_json({"error": "La base de producción no está disponible."}, 503)
        return self.send_json({"error": "Acción de producción desconocida."}, 404)

    def editorial_action(self, action, body=None, query=None):
        engine = self.app.editorial

        def identifier(value):
            if type(value) is not int or not 1 <= value <= 2**63 - 1:
                raise ValueError("Identificador editorial inválido.")
            return value

        try:
            if body is None:
                if action == "clips":
                    return self.send_json({"clips": self.app.tiktok.exports()})
                engine.ensure()
                if action == "status":
                    clip = query.get("clip", [None])[0]
                    before = query.get("before")
                    before = identifier(int(before[0])) if before else None
                    return self.send_json({
                        **engine.store.history(clip, before), "config": engine.store.config(),
                        "ollama": engine.model_status, "address": editorial_providers.OLLAMA_URL,
                        "auto_error": engine.last_auto_error, "auto_pending": engine.events.qsize(),
                    })
                if action == "result":
                    return self.send_json(engine.store.detail(identifier(int(query.get("id", ["0"])[0]))))
            else:
                if action == "models":
                    return self.send_json(engine.refresh_models(force=True))
                if action == "config":
                    return self.send_json(engine.save_config(body.get("settings"), body.get("revision")))
                if action in ("generate", "test"):
                    return self.send_json(engine.submit(
                        body.get("project"), body.get("filename"), body.get("platform"),
                        body.get("force", action == "test"), test=action == "test",
                        semantic=body.get("semantic")), 202)
                engine.ensure()
                if action == "reprocess":
                    return self.send_json(engine.reprocess(body.get("mode"), body.get("clip_id")), 202)
                if action == "variant":
                    return self.send_json(engine.revise_variant(identifier(body.get("job_id")), body.get("style"),
                                                                body.get("regenerate", False)), 202)
                if action == "cancel":
                    return self.send_json(engine.cancel(identifier(body.get("job_id"))))
                if action == "edit":
                    return self.send_json(engine.store.edit(
                        identifier(body.get("job_id")), body.get("style"), body.get("fields"),
                        body.get("revision"), body.get("select", False)))
        except editorial_store.Conflict as exc:
            return self.send_json({"error": str(exc)}, 409)
        except editorial_store.QueueFull as exc:
            return self.send_json({"error": str(exc)}, 429)
        except sqlite3.Error:
            return self.send_json({"error": "La base de datos editorial no está disponible."}, 503)
        return self.send_json({"error": "Acción editorial desconocida."}, 404)

    def tiktok_action(self, action, body):
        service = self.app.tiktok
        if action == "state":
            return self.send_json({**service.state(), "clips": service.exports()})
        if action == "connect":
            return self.send_json(service.connect(self.server.server_port))
        if action == "disconnect":
            return self.send_json(service.disconnect())
        if action == "privacy":
            return self.send_json(service.privacy())
        if action == "inspect":
            return self.send_json(service.inspect(body["project"], body["filename"]))
        if action == "upload":
            plan = body.get("calendar_id")
            if plan is not None:
                result = service.submit(body["review_id"], body["description"], body.get("approved"),
                                        on_consent=lambda review: self.app.calendar.reserve_tiktok(
                                            plan, body.get("calendar_revision"), review))
            else:
                result = service.submit(body["review_id"], body["description"], body.get("approved"))
            return self.send_json(result)
        if action == "status":
            return self.send_json(service.refresh(body["id"]))
        return self.send_json({"error": "Unknown TikTok action."}, 404)

    def send_file(self, path):
        if not path.is_file():
            raise FileNotFoundError(path)
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        requested = self.headers.get("Range")
        if requested:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested)
            if not match:
                return self.send_json({"error": "Unsupported byte range."}, 416)
            start = int(match[1])
            end = min(int(match[2]) if match[2] else end, end)
            if start > end or start >= size:
                return self.send_json({"error": "Range outside file."}, 416)
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with path.open("rb") as file:
                file.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = file.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


def load_static_files():
    """Keep the interface paired with the running backend across checkout updates."""
    assets = {}
    for route, (filename, content_type) in STATIC_FILES.items():
        path = STATIC / filename
        try:
            data = path.read_bytes()
        except OSError:
            data = b""
        if not data:
            raise RuntimeError(f"Cannot load Studio asset {path}. Update the complete checkout and restart Studio.")
        assets[route] = (data, content_type)
    references = HERE / "design" / "references" / "branding"
    for route, fallback, candidates in (
        ("anqora-logo", "anqora.svg", (STATIC / "anqora-horizontal.png", references / "anqora_logotipo_horizontal.png")),
        ("anqora-symbol", "anqora-mark.svg", (STATIC / "anqora-symbol.png",)),
    ):
        assets[route] = assets[fallback]
        for path in candidates:
            try:
                with path.open("rb") as image:
                    data = image.read(8_000_001)
                if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) <= 8_000_000:
                    assets[route] = (data, "image/png")
                    break
            except OSError:
                continue
    return assets


class StudioServer(ThreadingHTTPServer):
    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Winsock can otherwise let two servers listen on the same port.
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(root="projects", port=8765, installation=None):
    static_files = load_static_files()
    server = StudioServer(("127.0.0.1", port), Handler)
    server.static_files = static_files
    try:
        server.app = Studio(root, installation)
    except BaseException:
        server.server_close()
        raise
    return server


def main():
    configure_utf8()
    parser = argparse.ArgumentParser(description="Open Anqora Media Studio on your own computer.")
    parser.add_argument("--projects-dir")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--managed-launch", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    job_handle = studio_windows.managed_job() if args.managed_launch else None
    installation = studio_settings.Settings(HERE)
    try:
        prefs = installation.read()
        print("Studio: verificando FFmpeg y FFprobe.", flush=True)
        media = studio_settings.find_ffmpeg(HERE, prefs["ffmpeg_dir"])
        studio_settings.tool_path(media, os.environ)
        print("FFmpeg y FFprobe disponibles." if media else
              "FFmpeg y FFprobe pendientes de configurar.", flush=True)
        print(f"Studio: preparando el servidor local en 127.0.0.1:{args.port}.", flush=True)
        server = make_server(args.projects_dir or installation.projects(prefs), args.port, installation)
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    except RuntimeError as exc:
        parser.exit(1, f"{exc}\n")
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE and getattr(exc, "winerror", None) not in {10013, 10048}:
            raise
        parser.exit(1, f"Cannot bind Studio to port {args.port}. Stop any previous Studio process and check whether the port is already in use.\n")
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Anqora Media Studio: {url}\nCheckout: {HERE}\nProjects: {server.app.root}\nPress Ctrl+C to stop.", flush=True)
    for name in ("CHOPIFY_TIKTOK_CLIENT_KEY", "CHOPIFY_TIKTOK_CLIENT_SECRET"):
        os.environ.pop(name, None)
    if args.managed_launch and os.name == "nt" and not job_handle:
        print("Windows no permitió crear el grupo de procesos administrado.", flush=True)
    server.app.system.start_optional()
    if not args.no_browser and prefs["open_browser"]:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.app.close()
        server.server_close()


if __name__ == "__main__":
    main()
