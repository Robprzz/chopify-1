"""Bounded background editorial engine shared by Studio's clip entry points."""
from queue import Empty, Full, Queue
import secrets
import shutil
import sqlite3
import threading
import time

import automation
import captions
import editorial_semantic
import subtitle_quality
from editorial_content import PLATFORMS, PROMPT_VERSION, Rules, assemble, messages, plan_schema, validate_config
from editorial_providers import Cancelled, Cancellation, Ollama, ProviderError
from editorial_source import key, read_clip, semantic_context
from editorial_store import EditorialStore, MAX_PENDING
from local_process import use_resources


TEST_SOURCE = {
    "clip_id": "test", "project": None, "filename": None, "original_score": None,
    "excerpts": [
        {"start": 0, "end": 2, "text": "¿Cómo guardar las semillas después de la cosecha?"},
        {"start": 2, "end": 5, "text": "Deja secar las semillas y guárdalas en un frasco etiquetado."},
        {"start": 5, "end": 8, "text": "Así puedes identificar la variedad cuando llegue el momento de sembrar."},
    ],
    "filtered": 0, "sampled": False,
}
TEST_SOURCE["transcript_hash"] = key(TEST_SOURCE["excerpts"])
TEST_SOURCE["transcript_id"] = "test:" + TEST_SOURCE["transcript_hash"]


class Engine:
    def __init__(self, settings_store, clip_path, provider=None, resources=None):
        self.phase_one = settings_store
        self.store = EditorialStore(settings_store.path)
        self.clip_path = clip_path
        self.resources = resources or threading.Lock()
        self.production_stage = None
        self.provider = provider or Ollama()
        self.fallback = Rules()
        self.owner = secrets.token_hex(16)
        self.events = Queue(maxsize=MAX_PENDING)
        self.lock = threading.Lock()
        self.model_lock = threading.Lock()
        self.schema_lock = threading.Lock()
        self.migrated = False
        self.model_status = {"available": None, "models": [], "error": None, "executable_found": None}
        self.model_checked = 0
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.thread = None
        self.active = None
        self.last_auto_error = None
        if settings_store.path.is_file():
            self._start()

    def _start(self):
        with self.lock:
            if self.stopping.is_set():
                raise ValueError("El motor editorial se está cerrando.")
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name="chopify-editorial", daemon=True)
                self.thread.start()

    def ensure(self):
        self._migrate()
        self._start()

    def _migrate(self):
        with self.schema_lock:
            if not self.migrated:
                self.store.migrate()
                self.migrated = True

    def save_config(self, settings, revision):
        self.ensure()
        settings = validate_config(settings)
        model = settings["model"]
        if model and model != self.store.config()["settings"]["model"]:
            if not any(m["name"] == model and m["digest"] for m in self.refresh_models(force=True)["models"]):
                raise ValueError("Selecciona un modelo local instalado con una versión verificable.")
        return self.store.save_config(settings, revision)

    def close(self):
        self.stopping.set()
        with self.lock:
            if self.active:
                self.active[1].cancel()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=5)

    def refresh_models(self, force=False):
        with self.model_lock:
            if not force and time.monotonic() - self.model_checked < 10 and self.model_checked:
                return self.model_status
            try:
                self.model_status = {"available": True, "models": self.provider.models(), "error": None,
                                     "executable_found": bool(shutil.which("ollama"))}
            except ProviderError as exc:
                self.model_status = {"available": False, "models": [], "error": str(exc),
                                     "executable_found": bool(shutil.which("ollama"))}
            self.model_checked = time.monotonic()
            return self.model_status

    def auto(self, project, filename):
        if self.stopping.is_set():
            return
        try:
            self.events.put_nowait((project, filename))
        except Full:
            self.last_auto_error = "Cola editorial automática llena. El clip se puede analizar manualmente."
        else:
            try:
                self._start()
            except ValueError:  # Studio closed while a clip was finishing.
                return
            self.wake.set()

    def submit(self, project, filename, platform, force, *, test=False, auto=False, production=None, semantic=False):
        if type(semantic) is not bool or type(force) is not bool:
            raise ValueError("La opción de regenerar debe ser booleana.")
        if not test and (not isinstance(project, str) or not isinstance(filename, str)):
            raise ValueError("Selecciona un clip terminado de Studio.")
        if not auto:
            self.ensure()
        config = self.store.config()["settings"]
        if platform is None:
            platform = config["platform"]
        if not isinstance(platform, str) or platform not in PLATFORMS:
            raise ValueError("Selecciona un perfil editorial válido.")
        try:
            if test:
                source = TEST_SOURCE
            else:
                path = self.clip_path(project, filename)
                source = read_clip(path, project, filename)
                if production or semantic:
                    source = semantic_context(path, project, source)
        except (OSError, TypeError, KeyError, AttributeError, UnicodeError):
            raise ValueError("La transcripción del clip no se puede leer.") from None
        model = config["model"]
        status = self.refresh_models(force=not auto and not production) if model or production or semantic else self.model_status
        if (production or semantic) and not model:
            model = next((item["name"] for item in status["models"]
                          if item["name"] == "qwen3:4b" and item["digest"]), "")
        if semantic and not model:
            raise ValueError("Selecciona un modelo local para generar propuestas semánticas.")
        digest = next((item["digest"] for item in status["models"] if item["name"] == model), None)
        settings = {"platform": platform, "profile": config["profiles"][platform], "weights": config["weights"],
                    "timeout": config["timeout"], "model": model, "model_digest": digest,
                    "prompt_version": editorial_semantic.VERSION if model and (production or semantic) else PROMPT_VERSION}
        if production:
            settings["production"] = list(production)
        if model and (production or semantic):
            settings["semantic"] = True
        fingerprint = key([source["clip_id"], source["transcript_id"], source["original_score"],
                           source["sampled"], source["filtered"], source.get("media_version"),
                           captions.TIMELINE_VERSION, source.get("outside_clip"),
                           source.get("semantic_evidence"), source.get("language_hint"), settings])
        job, cached = self.store.submit(source, settings, fingerprint, force)
        self.wake.set()
        return {"job_id": job, "reused": cached}

    def cancel(self, job):
        result = self.store.cancel(job)
        with self.lock:
            if self.active and self.active[0] == job:
                self.active[1].cancel()
        return result

    def _accept_auto(self):
        try:
            project, filename = self.events.get_nowait()
        except Empty:
            return False
        try:
            settings = self.phase_one.read()["settings"]
            if settings["ai_captions"] and (not project.startswith("live/") or settings["live_clipping"]):
                self.submit(project, filename, None, False, auto=True)
        except (ValueError, automation.SettingsUnavailable, sqlite3.Error) as exc:
            self.last_auto_error = str(exc) if isinstance(exc, ValueError) else "No se pudo registrar el análisis automático."
        return True

    def _semantic(self, source, settings, cancellation):
        def progress(stage):
            if self.production_stage and "production" in settings:
                self.production_stage(*settings["production"], stage)

        status = self.refresh_models(force=True)
        local = next((item for item in status["models"] if item["name"] == settings["model"]), None)
        if not status["available"] or not local or not local["digest"]:
            raise ProviderError("offline", "Conecta Ollama local con el modelo seleccionado para reintentar.")
        if local["digest"] != settings["model_digest"]:
            raise ProviderError("model_changed", "Cambió el modelo local. Reintenta con la versión actual.")
        result = editorial_semantic.generate(self.provider, source, settings, cancellation, progress)
        result["model_version"] = local["digest"]
        return result, None

    def _generate(self, source, settings, cancellation):
        if settings.get("semantic"):
            return self._semantic(source, settings, cancellation)
        if self.production_stage and "production" in settings:
            self.production_stage(*settings["production"], "generating")
        started = time.monotonic()
        model = settings["model"]
        schema = plan_schema(len(source["excerpts"]))
        prompt = messages(source, settings["platform"], settings["profile"])
        diagnostic = None
        attempts = 0
        if model:
            try:
                status = self.refresh_models(force=True)
                cancellation.check()
                if not status["available"]:
                    raise ProviderError("offline", status["error"])
                local = next((item for item in status["models"] if item["name"] == model), None)
                if not local or not local["digest"]:
                    raise ProviderError("model_missing", "El modelo local seleccionado no está instalado o no tiene una versión verificable.")
                if settings["model_digest"] != local["digest"]:
                    raise ProviderError("model_changed", "Cambió el modelo disponible. Regenera la propuesta para usar su versión actual.")
                for attempt in range(2):
                    remaining = settings["timeout"] - (time.monotonic() - started)
                    if remaining <= 0:
                        raise ProviderError("timeout", "Ollama superó el tiempo máximo.")
                    try:
                        attempts += 1
                        plan = self.provider.generate(model, prompt, schema, remaining, cancellation)
                        result = assemble(plan, source, settings, self.provider.name, model)
                        result["model_version"] = local["digest"]
                        result["attempts"] = attempts
                        return result, None
                    except ProviderError as exc:
                        if not exc.retryable or attempt:
                            raise
            except ProviderError as exc:
                diagnostic = str(exc)
        plan = self.fallback.generate(None, prompt, schema, 1, cancellation)
        result = assemble(plan, source, settings, self.fallback.name, None)
        result["model_version"] = None
        result["attempts"] = attempts
        if diagnostic:
            result["warnings"].append(diagnostic)
        return result, diagnostic

    def _run(self):
        while not self.stopping.is_set():
            try:
                self._migrate()
                auto = self._accept_auto()
                claimed = self.store.claim(self.owner)
                if claimed:
                    self._process(*claimed)
                    continue
                if auto:
                    continue
            except sqlite3.Error:
                self.last_auto_error = "La base editorial está ocupada o no se puede abrir."
            self.wake.wait(1)
            self.wake.clear()

    def _renew(self, job, cancellation, done):
        while not done.wait(1):
            try:
                if not self.store.renew(job, self.owner):
                    cancellation.cancel()
                    return
            except sqlite3.Error:
                continue

    def _process(self, job, source, settings):
        started = time.monotonic()
        cancellation = Cancellation()
        with self.lock:
            self.active = job, cancellation
            if self.stopping.is_set():
                cancellation.cancel()
        done = threading.Event()
        renewer = threading.Thread(target=self._renew, args=(job, cancellation, done), daemon=True)
        renewer.start()
        state, result, error = "failed", None, None
        try:
            if self.store.cancel_requested(job):
                cancellation.cancel()
            cancellation.check()
            with use_resources(self.resources, cancellation):
                if source["project"] is not None:
                    path = self.clip_path(source["project"], source["filename"])
                    before = read_clip(path, source["project"], source["filename"])
                    if before["transcript_hash"] != source["transcript_hash"] or (
                            before["media_version"] != source.get("media_version")):
                        raise subtitle_quality.SubtitleError("El clip o el subtítulo cambió. Vuelve a solicitar el análisis.")
                    diagnostic = subtitle_quality.diagnose(path)
                    if diagnostic["errors"]:
                        raise subtitle_quality.SubtitleError("Subtítulos bloqueados. " + diagnostic["errors"][0] + " Abre Calidad de subtítulos para repararlos.")
                    cancellation.check()
                    after = read_clip(path, source["project"], source["filename"])
                    if before != after:
                        raise subtitle_quality.SubtitleError("El clip cambió durante el diagnóstico. Repite el análisis.")
                    if settings.get("semantic"):
                        current = semantic_context(path, source["project"], before)
                        if current != source:
                            raise subtitle_quality.SubtitleError("El contexto cambió. Vuelve a solicitar el análisis.")
                result, error = self._generate(source, settings, cancellation)
                cancellation.check()
            state = "done"
        except Cancelled:
            # Closing Studio preserves outstanding work. User cancellation is terminal.
            state = "queued" if self.stopping.is_set() else "cancelled"
            result = None
        except (subtitle_quality.SubtitleError, ProviderError) as exc:
            error = str(exc)
        except (ValueError, OSError, TypeError, KeyError, AttributeError, sqlite3.Error):
            error = "No se pudo completar el análisis editorial."
        finally:
            done.set()
            renewer.join(timeout=2)
            with self.lock:
                self.active = None
        self.store.finish(job, self.owner, state, result, error, time.monotonic() - started)
