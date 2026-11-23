"""Chopify Studio: loopback browser UI with a serial, resumable project queue."""
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
from urllib.parse import parse_qs, unquote, urlsplit
import webbrowser

import automation
import branding
import editorial
import editorial_providers
import editorial_store
import projects
import production
import render_clips
import subtitle_repair
import studio_settings
import studio_system
import studio_windows
import tiktok
import video_types
import watermark
from unicode_io import configure_utf8, utf8_environment

HERE = Path(__file__).resolve().parent
STATIC = HERE / "assets" / "studio"
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
    "system.js": ("system.js", "text/javascript; charset=utf-8"),
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
    state = projects.read_project(path)
    data = {"project": state, "busy": False, "candidates": [],
            "transcript": None, "selection": {}, "files": [], "log": ""}
    for filename, key in [("segments.json", "candidates"),
                          ("transcript.json", "transcript"),
                          ("selection.json", "selection")]:
        if (path / filename).exists():
            data[key] = projects.read_json(path / filename)
    for directory in ("previews", "clips"):
        for file in sorted((path / directory).glob("*")):
            if file.suffix in {".mp4", ".srt", ".png", ".json"}:
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
        (self.root / "brands").mkdir(exist_ok=True)
        self.token = secrets.token_urlsafe(32)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chopify")
        self.lock = threading.RLock()
        self.futures = {}
        self.resources = threading.Lock()
        self.installation = installation
        self.identity = installation.identity() if installation else None
        self.active_requests = 0
        profile = installation.read()["tiktok_profile"] if installation else "production"
        self.tiktok = tiktok.TikTokStudio(self.root, self.busy, installation.vault if installation else None, profile)
        self.automation_settings = automation.SettingsStore(self.root)
        self.watermark = watermark.Store(self.root)
        self.editorial = editorial.Engine(self.automation_settings, self.tiktok._clip, resources=self.resources)
        self.subtitle_repair = subtitle_repair.Repair(self.root, self.tiktok._clip, resources=self.resources)
        self.processes = set()
        self.live_sessions = {}
        self.live_lock = threading.Lock()
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
        self.system = studio_system.System(installation or studio_settings.Settings(HERE), self)

    def _production_catalog(self):
        from live_clips import PRODUCTION_REPAIR, live_exports

        for path in self.root.glob("*/project.json"):
            try:
                projects.project_path(self.root, path.parent.name)
                state = projects.read_project(path.parent)
                if self.busy(path.parent.name):
                    continue
                outputs = set(state.get("last_outputs", [])) | set((state.get("renders") or {}).values())
                for output in sorted(outputs):
                    clip = Path(output)
                    if clip.suffix == ".mp4" and clip.parent.name == "clips" and clip.is_file():
                        yield path.parent.name, clip.name
            except (OSError, ValueError, TypeError, KeyError):
                continue
        for clip in live_exports(self.root):
            if not PRODUCTION_REPAIR.fullmatch(clip["filename"]):
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
        for path in self.root.glob("*/project.json"):
            try:
                result.append(projects.read_project(path.parent))
            except (ValueError, OSError):
                continue
        return sorted(result, key=lambda p: p["created"], reverse=True)

    def enqueue(self, project_id, action, preview=False, pick=None):
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
            state.update(status="queued", stage=action, error=None)
            projects.save_project(path, state)
            future = self.executor.submit(self._run, path, action, preview, pick)
            self.futures[project_id] = future
            if action != "prepare" and not preview:
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

    def _run(self, path, action, preview, pick):
        cmd = [sys.executable, "-X", "utf8", str(HERE / "chopify.py"), "--project", str(path),
               "--action", action, "--out", str(path / ("previews" if preview else "clips"))]
        # Studio jobs run unattended: never wait for interactive CLI questions.
        cmd.extend(["--no-input", "--automation-export"])
        if preview:
            cmd.append("--preview")
        if pick:
            cmd += ["--clips", pick]
        try:
            with self.resources, (path / "job.log").open("w", encoding="utf-8") as log:
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
                state.update(status="interrupted" if self.closing else "failed",
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
        check_capture_tools()
        resolved = resolve_live_source(source, options)
        session = live.LiveSession(resolved, options, self.root, brief_text=brief_text)
        session.writer.resources = self.resources
        session.on_clip = lambda path: self._production_clip(f"live/{session.id}", Path(path).name)
        with self.lock:
            if self.closing:
                raise ValueError("Studio is closing. Restart it before starting a live session.")
            session._save()
            with self.live_lock:
                self.live_sessions[session.id] = session
            threading.Thread(target=session.start, name=f"live-{session.id}", daemon=True).start()
        return session

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
        if session is None:
            raise ValueError("Unknown live session.")
        return projects.read_json(session.dir / "live_session.json")

    def live_list(self):
        with self.live_lock:
            sessions = list(self.live_sessions.values())
        result = []
        for session in sessions:
            try:
                result.append(projects.read_json(session.dir / "live_session.json"))
            except FileNotFoundError:
                continue
        return result

    def activity(self):
        with self.lock:
            return {
                "projects": sum(not future.done() for future in self.futures.values()),
                "live": sum(getattr(session, "status", None) in {"starting", "running"}
                            or getattr(session, "exporting", lambda: False)()
                            for session in self.live_sessions.values()),
                "production": self.production.active(),
                "editorial": self.editorial.busy(),
                "subtitle_repair": bool(self.subtitle_repair.future and not self.subtitle_repair.future.done()),
                "tiktok_transfer": self.tiktok.sending,
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
        self.production.pause_dispatch(True)
        self.editorial.pause_dispatch(True)
        for session in sessions:
            session.stop()
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
            if parts == ["api", "projects"]:
                return self.send_json({"projects": self.app.list_projects()})
            if parts == ["api", "automation", "settings"]:
                return self.automation_settings_action()
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
                return self.send_file(live_clip_path(self.app.root, parts[2], parts[4]))
            if parts and parts[0] == "media" and len(parts) >= 3:
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
            with self.app.lock:
                self.app.active_requests -= 1

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
            with self.app.lock:
                self.app.active_requests -= 1

    def _dispatch_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 10_000_000:
                raise ValueError("Request must be JSON smaller than 10 MB.")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            parts = unquote(urlsplit(self.path).path).strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "system"]:
                return self.system_action(parts[2], body)
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
                    return self.send_json(projects.create_project(self.app.root, body["source"], body.get("options", {})), 201)
                if parts == ["api", "batch"]:
                    sources = body["sources"]
                    if not isinstance(sources, list) or not 1 <= len(sources) <= 100 or any(not isinstance(s, str) or not s.strip() for s in sources):
                        raise ValueError("Provide 1 to 100 sources.")
                    options = projects.validate_options(body.get("options", {}))
                    brief_text = body.get("brief")
                    if brief_text is not None and not isinstance(brief_text, str):
                        raise ValueError("Brief must be text.")
                    result = []
                    for source in sources:
                        state = projects.create_project(self.app.root, source, options)
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
                if len(parts) == 4 and parts[:2] == ["api", "projects"]:
                    project_id, action = parts[2:]
                    path = projects.project_path(self.app.root, project_id)
                    state = projects.read_project(path)
                    if self.app.busy(project_id):
                        raise ValueError("Wait for this project's current job to finish before editing.")
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
                    elif action == "run":
                        self.app.enqueue(project_id, body.get("action", "prepare"), body.get("preview", False), body.get("pick"))
                    else:
                        return self.send_json({"error": "Not found."}, 404)
                    return self.send_json({"ok": True})
            self.send_json({"error": "Not found."}, 404)
        except FileNotFoundError:
            self.send_json({"error": "Project or transcript not found. Prepare the project first."}, 404)
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

    def production_action(self, action, body=None, query=None):
        worker = self.app.production
        store = worker.store

        def identifier(value):
            if type(value) is not int or not 1 <= value <= 2**63 - 1:
                raise ValueError("Identificador de producción inválido.")
            return value

        try:
            if body is None:
                if action == "status":
                    before = query.get("before")
                    cursor = identifier(int(before[0])) if before else None
                    return self.send_json(worker.status(cursor, query.get("view", ["all"])[0]))
                if action == "job":
                    return self.send_json(store.detail(identifier(int(query.get("id", ["0"])[0]))))
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
    parser = argparse.ArgumentParser(description="Open Chopify Studio on your own computer.")
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
        if not media:
            print("FFmpeg y FFprobe pendientes de configurar.", flush=True)
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
    print(f"Chopify Studio: {url}\nCheckout: {HERE}\nProjects: {server.app.root}\nPress Ctrl+C to stop.", flush=True)
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
