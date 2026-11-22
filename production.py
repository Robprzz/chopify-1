"""Coordinate local technical QA, bounded repair and editorial preparation."""
from pathlib import Path
import math
import secrets
import sqlite3
import subprocess
import threading
import time

import automation
import branding
import captions
from editorial_providers import Cancellation, Cancelled
from editorial_store import QueueFull, dump
from local_process import run, use_resources
import projects
from production_store import Store
import subtitle_quality as quality
import subtitle_repair as repair


MAX_SKEW = .22
SCAN_INTERVAL = 15


def media_check(media, cancellation):
    """Decode the MP4 locally, check the track clocks, then validate caption evidence."""
    try:
        duration, streams = quality.probe_media(media)
    except FileNotFoundError:
        return "ffprobe_missing", {"errors": ["FFprobe es necesario para comprobar el MP4."]}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return "media_unreadable", {"errors": ["No se pudo leer el MP4 con FFprobe."]}
    if "audio" not in streams:
        return "no_audio", {"errors": ["El MP4 no contiene audio para verificar el diálogo."], "duration": duration}
    try:
        starts = [float(streams[k].get("start_time") or 0) for k in ("video", "audio")]
        ends = [a + float(streams[k]["duration"]) for a, k in zip(starts, ("video", "audio"))]
        if not all(math.isfinite(t) for t in (*starts, *ends)) or any(end <= start for start, end in zip(starts, ends)):
            return "av_unverified", {"errors": ["El MP4 no informa relojes de pista válidos."], "duration": duration}
        if abs(starts[0] - starts[1]) > MAX_SKEW or abs(ends[0] - ends[1]) > MAX_SKEW:
            return "av_sync", {"errors": ["Los relojes de audio y video están desalineados."],
                               "duration": duration, "av_start_skew": abs(starts[0] - starts[1]),
                               "av_end_skew": abs(ends[0] - ends[1])}
    except (KeyError, TypeError, ValueError):
        return "av_unverified", {"errors": ["El MP4 no informa ambos relojes de pista."], "duration": duration}
    if duration > quality.MAX_AUDIO_SECONDS:
        return "duration_limit", {"errors": ["El control completo admite clips de hasta diez minutos."],
                                  "duration": duration}
    try:
        child = run(["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-threads", "2",
                        "-protocol_whitelist", "file,pipe", "-f", "mov", "-i", str(media),
                        "-map", "0:v:0", "-map", "0:a:0", "-threads", "2", "-f", "null", "-"],
                                  min(180, max(30, duration * 2)), cancellation)
        child.check_returncode()
    except subprocess.TimeoutExpired:
        return "decode_timeout", {"errors": ["La verificación de integridad superó el tiempo disponible."],
                                  "duration": duration}
    except FileNotFoundError:
        return "ffmpeg_missing", {"errors": ["FFmpeg es necesario para verificar la integridad del MP4."]}
    except (OSError, subprocess.SubprocessError):
        return "media_decode", {"errors": ["FFmpeg detectó errores al decodificar el MP4."], "duration": duration}
    report = quality.diagnose(media)
    report.update(integrity="decoded", av_start_skew=round(abs(starts[0] - starts[1]), 3),
                  av_end_skew=round(abs(ends[0] - ends[1]), 3))
    if report["errors"]:
        code = "voice_coverage" if any("Actividad de voz" in e for e in report["errors"]) else "subtitles"
        return code, report
    if any("No se pudo medir" in warning for warning in report["warnings"]):
        return "vad_unavailable", report
    if report["speech_coverage"] is None:
        return "voice_unconfirmed", report
    return None, report


def project_transcript(media, project):
    from subtitle_quality import local_text
    import json

    if project.startswith("live/"):
        return None
    base = media.parent.parent
    try:
        meta = json.loads(local_text(media.with_suffix(".meta.json"), 16384))
        data = json.loads(local_text(base / "transcript.json", 16 * 1024 * 1024))
        if (meta.get("subtitles") or {}).get("tightened"):
            return None
        start, end = meta["start"], meta["end"]
        selected = [{**w, "start": max(0, w["start"] - start), "end": min(end, w["end"]) - start}
                    for w in data["words"] if w["start"] < end and w["end"] > start and not w.get("hidden")]
        return {"words": selected, "model": data.get("model"),
                "origin": "Tiempos de la transcripción existente"} if selected else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def cached_models(hint):
    from faster_whisper.utils import download_model

    for name in dict.fromkeys((hint, "small", "base", "tiny", "medium")):
        if name not in repair.MODELS:
            continue
        try:
            download_model(name, local_files_only=True)
        except (OSError, ValueError):
            continue
        yield name


class Production:
    def __init__(self, root, editor, settings, clip_path, catalog, resources):
        self.root = Path(root)
        self.editor = editor
        self.settings = settings
        self.clip_path = clip_path
        self.catalog = catalog
        self.resources = resources
        self.store = Store(editor.store)
        self.store.migrate()
        self.editor.production_stage = self.store.editorial_stage
        self.owner = secrets.token_hex(12)
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.dispatch_lock = threading.Lock()
        self.quiescing = False
        self.thread = threading.Thread(target=self._run, daemon=True, name="chopify-production")
        self.thread.start()

    def enqueue(self, project, filename):
        if self.stopping.is_set():
            return
        settings = self.settings.read()["settings"]
        if not project.startswith("live/") or settings["live_clipping"]:
            self.store.enqueue(project, filename)
            self.wake.set()

    def pause_dispatch(self, paused):
        with self.dispatch_lock:
            self.quiescing = paused
        self.wake.set()

    def active(self):
        with self.store.connect() as db:
            return bool(db.execute("SELECT 1 FROM production_jobs WHERE owner=? AND finished IS NULL",
                                   (self.owner,)).fetchone())

    def close(self):
        self.stopping.set()
        self.wake.set()
        self.thread.join()

    def _scan(self):
        allow_live = self.settings.read()["settings"]["live_clipping"]
        clips = [(project, filename) for project, filename in self.catalog()
                 if allow_live or not project.startswith("live/")]
        if clips and not self.stopping.is_set():
            self.store.enqueue_many(clips)

    def _run(self):
        next_scan = 0
        while not self.stopping.is_set():
            try:
                if time.monotonic() >= next_scan:
                    next_scan = time.monotonic() + SCAN_INTERVAL
                    self._scan()
                with self.dispatch_lock:
                    job = None if self.quiescing else self.store.claim(self.owner)
                if job:
                    self._process(job)
                    continue
            except (sqlite3.Error, automation.SettingsUnavailable, OSError):
                next_scan = min(next_scan, time.monotonic() + 2)
            self.wake.wait(.5)
            self.wake.clear()

    def _monitor(self, job, cancellation, done):
        while not done.wait(.5):
            if self.stopping.is_set():
                cancellation.cancel()
                return
            try:
                if not self.store.renew(job, self.owner):
                    cancellation.cancel()
                    return
            except sqlite3.Error:
                continue

    def _process(self, job):
        ident, run = job["id"], job["run"]
        elapsed = time.monotonic()
        seconds = lambda: round(job["seconds"] + time.monotonic() - elapsed, 3)
        cancellation = Cancellation()
        done = threading.Event()
        monitor = threading.Thread(target=self._monitor, args=(ident, cancellation, done), daemon=True)
        monitor.start()

        def change(state, **fields):
            self.store.change(ident, self.owner, state, seconds=seconds(), **fields)

        try:
            settings = self.settings.read()["settings"]
            if job["project"].startswith("live/") and not settings["live_clipping"]:
                change("review", code="live_disabled", error="Automation para Live está desactivado.")
                return
            media = self.clip_path(job["project"], job["active_filename"] or job["filename"])
            editorial_id = job["editorial_id"]
            if not editorial_id:
                with use_resources(self.resources, cancellation):
                    media, ok = self._technical(job, media, settings["subtitle_correction"], cancellation, change)
                if not ok:
                    return
                cancellation.check()
                if not settings["ai_captions"]:
                    change("review", code="ai_disabled", error="La generación editorial automática está desactivada.")
                    return
                change("analyzing")
                while True:
                    cancellation.check()
                    try:
                        posted = self.editor.submit(job["project"], media.name, None, False,
                                                    production=(ident, run))
                        editorial_id = posted["job_id"]
                        self.store.link_editorial(ident, self.owner, editorial_id)
                        break
                    except QueueFull:
                        cancellation.event.wait(.5)
            while True:
                cancellation.check()
                result = self.editor.store.detail(editorial_id)
                if result["state"] in ("done", "failed", "cancelled"):
                    self._editorial_result(result, change)
                    break
                cancellation.event.wait(.25)
        except Cancelled:
            if self.stopping.is_set():
                self.store.release(ident, self.owner)
            else:
                if self.store.detail(ident)["editorial_id"]:
                    self.editor.cancel(self.store.detail(ident)["editorial_id"])
                change("cancelled")
        except sqlite3.Error:
            self.store.release(ident, self.owner)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, subprocess.SubprocessError) as exc:
            if isinstance(exc, quality.SubtitleError):
                change("review", code="subtitle_evidence", error=str(exc))
            else:
                change("failed", code="work_unavailable", error="No se pudo continuar con el archivo o los datos locales.")
        finally:
            done.set()
            monitor.join()

    def _technical(self, job, media, auto_repair, cancellation, change):
        change("validating", technical=None)
        code, report = media_check(media, cancellation)
        cancellation.check()
        if not code:
            change("analyzing", technical="passed", diagnosis=dump(report))
            return media, True
        reason = (report["errors"] or report.get("warnings") or ["El clip no superó la comprobación."])[0]
        if code not in {"subtitles", "voice_coverage"}:
            terminal = "rejected" if code in {"media_unreadable", "media_decode", "av_sync"} else "review"
            change(terminal, code=code, error=reason, diagnosis=dump(report), technical="failed")
            return media, False
        if not auto_repair:
            change("review", code=code, error=reason, diagnosis=dump(report), technical="failed")
            return media, False

        origin = self.clip_path(job["project"], job["filename"])
        repair_started = time.monotonic()
        maximum = self.store.config()["max_repairs"]
        cached = project_transcript(origin, job["project"]) if code != "voice_coverage" else None
        failure = "No se pudo reparar el clip con los recursos locales disponibles."
        attempts = job["attempts"]
        base = self.root / ".production-repairs"
        if base.resolve() != base:
            raise quality.SubtitleError("La carpeta local de reparaciones no es válida.")
        models = None
        for attempt in range(attempts + 1, maximum + 1):
            use_cached = attempt == 1 and cached is not None
            strategy = "transcript_reuse" if use_cached else "whisper"
            change("repairing", code=code, error=reason, diagnosis=dump(report), technical="failed")
            try:
                if not use_cached:
                    if models is None:
                        hint = report.get("model") or (cached or {}).get("model")
                        models = list(cached_models(hint))
                    offset = attempt - (2 if cached is not None else 1)
                    if not models:
                        raise quality.SubtitleError("No se encontró un modelo de Faster-Whisper en la caché local.")
                    model = models[min(offset, len(models) - 1)]
                else:
                    model = None
                amended = self._repair(job, origin, model, cached if use_cached else None,
                                        attempt, base, cancellation)
                change("revalidating", attempts=attempt, active_filename=amended.name,
                       repair_seconds=time.monotonic() - repair_started)
                code, report = media_check(amended, cancellation)
                cancellation.check()
                if not code:
                    change("analyzing", technical="passed", diagnosis=dump(report))
                    return amended, True
                reason = (report["errors"] or report.get("warnings") or [failure])[0]
                failure = reason
                change("repairing", code=code, error=failure, diagnosis=dump(report))
            except (OSError, ValueError, KeyError, TypeError, ImportError, subprocess.SubprocessError) as exc:
                failure = str(exc) if isinstance(exc, quality.SubtitleError) else "No se pudo regenerar la variante con el montaje local."
                change("repairing", code=strategy, error=failure, attempts=attempt,
                       repair_seconds=time.monotonic() - repair_started)
        change("review", code="repair_exhausted", error=failure if maximum else reason,
               diagnosis=dump(report), technical="failed")
        return media, False

    def _repair(self, job, original, model, evidence, attempt, base, cancellation):
        export_id = f"auto-{job['id']}-{job['run']}-{attempt}"
        expected = original.parent / (export_id + "-corregido.mp4")
        if expected.is_file() and expected.resolve() == expected:
            return expected  # Complete promotion happened before a crash. Revalidate it in the caller.
        folder = base / export_id
        if folder.resolve() != folder:
            raise quality.SubtitleError("La carpeta del intento de reparación no es válida.")
        folder.mkdir(parents=True, exist_ok=True)
        duration, streams = quality.probe_media(original)
        meta = projects.read_json(original.with_suffix(".meta.json"))
        repair.clean_source(original, job["project"], meta, duration)
        timing = {k: (meta.get("subtitles") or {}).get("timing", {}).get(k, branding.CAPTION_DEFAULTS[k])
                  for k in captions.TIMING_FIELDS}
        fingerprint = quality.artifact_hash(original)
        if evidence is None:
            cache = folder / "whisper.json"
            stamp = folder / "input.json"
            if stamp.is_file() and projects.read_json(stamp) == [fingerprint, model] and cache.is_file():
                evidence = projects.read_json(cache)
            else:
                cancellation.check()
                evidence = repair.transcribe_final(original, cache, model, cancellation)
                projects.atomic_json(stamp, [fingerprint, model])
        cancellation.check()
        if quality.artifact_hash(original) != fingerprint:
            raise quality.SubtitleError("El MP4 cambió durante la reparación automática.")
        draft, cues = repair.make_draft(original, job["project"], folder, evidence, timing, duration, fingerprint)
        speech = quality.speech_ranges(original, duration, max(0, float(streams["audio"].get("start_time") or 0)))
        if quality.speech_deficit(*quality.coverage(cues, speech)):
            raise quality.SubtitleError("La transcripción disponible deja actividad de voz sin subtítulos.")
        result = repair._rebuild(self.root, original, job["project"], folder, draft,
                                 preview=False, export_id=export_id, check=cancellation.check)
        return original.parent / result["export"]

    @staticmethod
    def _editorial_result(job, change):
        elapsed = job["elapsed"]
        if job["state"] == "cancelled":
            change("review", code="editorial_cancelled", error="Se canceló el trabajo editorial.",
                   editorial_seconds=elapsed)
            return
        result = job.get("result") or {}
        if result.get("mode") == "semantic" and result.get("recommended_variant"):
            change("ready", editorial_seconds=elapsed)
        else:
            change("review", code="editorial_review", editorial_seconds=elapsed,
                   error=job.get("error") or "Se requiere validación semántica con Ollama local para preparar la aprobación.")
