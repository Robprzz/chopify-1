"""System Settings and lifecycle operations for the authenticated loopback API."""
from contextlib import contextmanager
import os
import sqlite3
from pathlib import Path
import threading
import time

import studio_settings as settings
import studio_ollama
from studio_updates import Repository
import studio_windows


class System:
    def __init__(self, store, app):
        self.store = store
        self.app = app
        self.ollama = studio_ollama.Manager(store.logs / "ollama.log")
        self.repository = Repository(store.checkout, store.directory / "update.lock")
        self.restore_lock = threading.Lock()
        self.restore_thread = None
        self.media_lock = threading.Lock()
        self.original_path = os.environ.get("PATH")

    def _find_media(self, saved=""):
        return settings.find_ffmpeg(self.store.checkout, saved, search_path=self.original_path or "")

    def _apply_media(self, report):
        environment = {} if self.original_path is None else {"PATH": self.original_path}
        settings.tool_path(report, environment)
        if "PATH" in environment:
            os.environ["PATH"] = environment["PATH"]
        else:
            os.environ.pop("PATH", None)

    def configure_media(self):
        report = self._find_media(self.store.read()["ffmpeg_dir"])
        self._apply_media(report)
        return report

    @contextmanager
    def _media_operation(self):
        if not self.media_lock.acquire(blocking=False):
            raise ValueError("Ya hay una operación de FFmpeg en curso. Espera a que termine y vuelve a intentarlo.")
        try:
            yield
        finally:
            self.media_lock.release()

    def diagnose_media(self, folder):
        with self._media_operation():
            if folder := settings.local_folder(folder):
                return settings.ffmpeg_checks(folder)
            if report := self._find_media():
                return settings.ffmpeg_checks(report["directory"])
            return None

    def _model(self):
        try:
            self.app.editorial._migrate()
            return self.app.editorial.store.config()["settings"]["model"] or "qwen3:4b"
        except (ValueError, OSError, sqlite3.Error):
            return "qwen3:4b"

    def status(self):
        prefs = self.store.read()
        try:
            version = self.repository.local()
        except ValueError:
            version = {"installed": None, "branch": None, "dirty": None}
        return {"settings": prefs, "projects_dir": str(self.app.root),
                "projects_next": str(self.store.projects(prefs)), "python": settings.dependency_report(),
                "installation": version, "windows": os.name == "nt", "logs": str(self.store.logs),
                "integrations": self.app.tiktok.integrations(), "active": self.app.activity()}

    def diagnose(self, start=False):
        prefs = self.store.read()
        report = self._find_media(prefs["ffmpeg_dir"])
        model = self._model()
        ollama = (self.ollama.ensure(prefs["ollama_auto_start"], model) if start
                  else self.ollama.status(model))
        return {"ffmpeg": report, "ollama": ollama, "python": settings.dependency_report(),
                "secure_storage": self.app.tiktok.integrations()["protected_available"]}

    def save(self, changes):
        if "ffmpeg_dir" not in changes:
            return self._save(changes)
        with self._media_operation():
            folder = settings.local_folder(changes["ffmpeg_dir"])
            report = settings.ffmpeg_directory(folder, report_timeout=True) if folder else self._find_media()
            if folder and not report:
                raise ValueError("La carpeta debe contener FFmpeg y FFprobe ejecutables y compatibles.")
            result = self._save({**changes, "ffmpeg_dir": folder})
            self._apply_media(report)
            return result

    def _save(self, changes):
        prefs = self.store.normalize(changes, self.store.read())
        if "projects_dir" in changes and prefs["projects_dir"]:
            path = Path(prefs["projects_dir"])
            if path.exists() and not path.is_dir():
                raise ValueError("La ubicación de proyectos debe ser una carpeta.")
        if "tiktok_profile" in changes:
            # Serialize the change with OAuth refresh and transfer start.
            with self.app.tiktok.lock:
                if self.app.tiktok.sending:
                    raise ValueError("Espera a que termine la subida para cambiar de perfil de TikTok.")
                result = self.store.save(changes)
                if prefs["tiktok_profile"] != self.app.tiktok.profile:
                    self.app.tiktok.select_profile(result["tiktok_profile"])
            self.restore_in_background()
        else:
            result = self.store.save(changes)
        return result

    def restore_in_background(self):
        with self.restore_lock:
            if self.restoring() or not self.app.tiktok.token or self.app.tiktok.account:
                return
            self.restore_thread = threading.Thread(target=lambda: self.app.tiktok.verify_authorization(quiet=True),
                                                   daemon=True, name="chopify-oauth-restore")
            self.restore_thread.start()

    def restoring(self):
        return bool(self.restore_thread and self.restore_thread.is_alive())

    def close(self):
        if self.restore_thread:
            self.restore_thread.join()
        self.ollama.close()

    def start_optional(self):
        self.restore_in_background()
        if self.store.read()["ollama_auto_start"]:
            def start():
                try:
                    self.ollama.ensure(True, self._model())
                except (OSError, ValueError):
                    pass
            threading.Thread(target=start, daemon=True, name="chopify-ollama-start").start()

    def lifecycle(self, server, mode, body):
        self.app.quiesce(mode)
        try:
            if mode == "update":
                if any(self.app.activity().values()) or self.app.active_requests > 1:
                    raise ValueError("Espera a que terminen los trabajos activos y las solicitudes antes de actualizar.")
                result = self.repository.apply(body.get("installed"), body.get("remote"))
            elif mode in {"wait", "cancel"}:
                result = {"mode": mode}
            else:
                raise ValueError("Elige esperar a los trabajos activos o cancelarlos.")
        except BaseException:
            self.app.unquiesce()
            raise

        return result

    def stop_server(self, server, mode):
        def stop():
            if mode in {"wait", "cancel"}:
                for session in list(self.app.live_sessions.values()):
                    session.stop()
                # A transfer writes a recoverable history record, and is never terminated by a normal shutdown.
                editorial_paused = mode == "cancel"
                while True:
                    if mode == "wait" and not editorial_paused and not self.app.production.active():
                        self.app.editorial.pause_dispatch(True)
                        editorial_paused = True
                    if not (self.app.tiktok.sending or self.restoring() or self.app.active_requests
                            or (mode == "wait" and any(self.app.activity().values()))):
                        break
                    time.sleep(.2)
            server.shutdown()

        threading.Thread(target=stop, daemon=True, name="chopify-safe-shutdown").start()

    def open_logs(self):
        if os.name != "nt":
            raise ValueError("Abre la ruta indicada en el explorador de archivos.")
        self.store.logs.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.store.logs))
