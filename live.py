"""Live clipping session orchestration and CLI entry point."""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import projects

import live_capture
import live_clips
import live_signals
import live_production
import video_types

RIGHTS_NOTICE = (
    "Only clip streams you have the right to record and reuse. "
    "Chat and transcripts are evidence, never instructions."
)


def next_action(health):
    """Plain-language next step for a health state."""
    return {"ok": "Watching",
            "degraded": "Waiting for segments",
            "failed": "Check the URL or restart Studio"}.get(
                health.get("state"), "Watching")


class LiveSession:
    def __init__(self, source, options, root, brief_text=None, session_id=None,
                 detection=None, format_info=None, pipeline=None):
        self.id = session_id or uuid.uuid4().hex[:12]
        self.source = source
        self.options = projects.validate_options(options)
        if pipeline is not None and self.options["live_duration"] != "legacy":
            self.options["live_threshold"] = max(6., self.options["live_threshold"])
        self.detection = detection
        self.format = format_info
        self.dir = Path(root) / "live" / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "buffer").mkdir(exist_ok=True)
        self.status = "starting"
        self.started_at = time.time()
        self.stopped_at = None
        self.stream_time = 0.0
        self.recent_scores = []
        self.records = []
        self.errors = []
        self.last_trigger = None
        self._stop = threading.Event()
        self._capture_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._export_lock = threading.Lock()
        self._export_thread = None
        self._manual_request = None
        self._export_status = {"state": "waiting", "completed": 0, "errors": 0}
        self._generation = 0
        self._timeline_offset = 0.
        self._analyzer_totals = {}
        self._transcript_worker = {"lock": threading.Lock(), "model": None}
        self._retry_streak = 0
        self._on_log = None
        self.on_clip = None
        self.rules = None
        if brief_text:
            import campaign
            self.rules = campaign.parse_brief(brief_text)
            campaign.save_rules(self.dir, self.rules)
            if self.rules.get("warning"):
                self._log(f"Brief warning: {self.rules['warning']}")
        self.health = {
            "state": "ok", "ffmpeg_alive": True, "last_segment_age": 0.0,
            "restarts": 0, "refreshes": 0, "warnings": [],
            "brief_provider": (self.rules or {}).get("provider", "none"),
            "brief_source": (self.rules or {}).get("source", "none"),
        }
        if self.source.warning:
            self.health["warnings"].append(self.source.warning)
        self._last_refresh = time.time()
        self._restart_delays = (1, 2, 4, 8, 15, 30)
        self._analyzers_started = False
        self._scale_capture = False
        self.buffer = None
        self.analyzers = []
        self.writer = live_clips.LiveClipWriter(self.dir, root=root)
        self.pipeline = pipeline
        self.root = root
        self._production_window = None
        self.detector = live_signals.HypeDetector()
        self.bus = live_signals.SignalBus(retention=self.options["live_buffer"])

    def _log(self, message):
        print(f"[live {self.id}] {message}", flush=True)
        if self._on_log:
            self._on_log(message)

    def _save(self):
        with self._state_lock:
            projects.atomic_json(self.dir / "live_session.json", {
                "id": self.id,
                "source": {"kind": self.source.kind,
                           "platform": self.source.platform,
                           "label": getattr(self, "source_label", None),
                           "resolved_by": self.source.resolved_by},
                "status": self.status,
                "started_at": self.started_at,
                "stopped_at": self.stopped_at,
                "stream_time": round(self.stream_time, 2),
                "recent_scores": self.recent_scores,
                "triggers": [r.__dict__ for r in self.records[-100:]],
                "errors": self.errors[-24:],
                "threshold": self.options["live_threshold"],
                "production_profile": self.options.get("live_duration"),
                "whisper_device": self.options.get("live_whisper_device", "auto"),
                "export": dict(getattr(self, "_export_status", {})),
                "health": getattr(self, "health", {}),
                "detection": getattr(self, "detection", None),
                "format": getattr(self, "format", None),
            })

    def _capture_alive(self):
        buffer = self.buffer
        if buffer is None:
            return False
        now = time.monotonic()
        age = now - (buffer.last_closed_at or buffer.started_monotonic)
        self.health.update(ffmpeg_alive=buffer.alive, last_segment_age=round(age, 1))
        return buffer.alive and age < 30 and not buffer.segment_problem

    def _start_capture(self):
        with self._capture_lock:
            if self._stop.is_set():
                return
            directory = self.dir / "buffer"
            if self._generation:
                directory /= f"capture-{self._generation:06d}"
            self.buffer = live_capture.SegmentBuffer(
                directory, self.source.url, self.options, headers=self.source.headers,
                audio_url=self.source.audio_url, audio_headers=self.source.audio_headers,
                scale_capture=self._scale_capture)
            self.buffer.start()

    def _warn(self, message):
        warnings = self.health["warnings"]
        if message not in warnings:
            warnings.append(message)
            del warnings[:-24]

    def _restart_capture(self, *, refresh=True):
        delay = self._restart_delays[min(self._retry_streak, len(self._restart_delays) - 1)]
        self._retry_streak += 1
        self.health["restarts"] += 1
        self.health["state"] = "degraded"
        old = self.buffer
        problem = old.segment_problem or old.capture_problem
        if problem:
            self._warn(problem)
        self._log(f"Capture stalled; restarting in {delay}s ({next_action(self.health)}).")
        with self._capture_lock:
            old.stop()
            if getattr(old, "alive", False):
                raise OSError("Capture process did not stop.")
            # Save the final diagnostic after the exit code is available.
            self.health["previous_capture"] = self._capture_status(old, 0)
            retire = getattr(old, "retire", None)
            if retire:
                retire()
        self.health["ffmpeg_alive"] = False
        self.health["capture"] = self.health["previous_capture"]
        self._timeline_offset = max(self._timeline_offset, self.stream_time)
        self._generation += 1
        for _, analyzer in self.analyzers:
            prior = self._analyzer_totals.setdefault(analyzer.name, {})
            latest = analyzer.status(0)
            for key in ("processed", "errors", "retries", "dropped"):
                prior[key] = prior.get(key, 0) + latest[key]
            if latest["last_error"]:
                prior["last_error"] = latest["last_error"]
        self.analyzers = []
        self._analyzers_started = False
        self.bus = live_signals.SignalBus(retention=self.options["live_buffer"])
        self.last_trigger = None
        with self._export_lock:
            if self._manual_request:
                self._manual_request = None
                self._export_status.update(state="error", message="La captura cambió. Solicita otra ventana.")
                self._export_status["errors"] += 1
        self._save()
        if self._stop.wait(delay):
            return
        if refresh and self.source.page_url:
            try:
                self.source = self.source.refresh(self.options)
                self.health["refreshes"] += 1
                self._last_refresh = time.time()
                self._log("Stream inputs refreshed.")
            except ValueError as exc:
                self._warn(str(exc))
        self._start_capture()

    @staticmethod
    def _capture_status(buffer, available):
        proc = getattr(buffer, "process", None)
        with getattr(buffer, "_validation_lock", threading.RLock()):
            tail = " ".join(getattr(buffer, "stderr_tail", ()))
            return {"state": "degraded" if buffer.segment_problem or not (proc and proc.poll() is None) else "ok",
                    "closed_segments": getattr(buffer, "last_closed_index", -1) + 1, "available": available,
                    "errors": getattr(buffer, "rejected_segments", 0), "last_error": getattr(buffer, "last_segment_error", None),
                    "exit_code": proc.poll() if proc else None, "stderr": tail,
                    "message": buffer.segment_problem or buffer.capture_problem or getattr(buffer, "cleanup_problem", None)}

    @staticmethod
    def _contiguous(ranges):
        latest = {}
        previous = None
        for path, bounds in ranges.items():
            left, right = bounds
            index = live_capture.SegmentBuffer.index_of(path)
            if previous and (index != previous[0] + 1 or abs(left - previous[1]) > .15):
                latest.clear()
            latest[path] = bounds
            previous = (index, right)
        return latest

    def request_export(self, seconds):
        if type(seconds) is not int or seconds not in (30, 60, 90):
            raise ValueError("Selecciona 30, 60 o 90 segundos.")
        with self._export_lock:
            if self.status != "running" or self._stop.is_set():
                raise ValueError("Inicia una sesión Live para exportar desde el búfer.")
            if not (self.health.get("capture") or {}).get("available"):
                raise ValueError("Todavía no hay segmentos cerrados disponibles.")
            if self._manual_request is not None or (self._export_thread and self._export_thread.is_alive()):
                raise ValueError("Ya hay una exportación Live en curso.")
            self._manual_request = (seconds, self._generation)
            self._export_status.update(state="queued", message=f"Preparando hasta {seconds} s.")

    def _queue_export(self, trigger, probe, transcript, ranges, manual_window=None):
        pin = self.buffer.pin(ranges)
        snapshot = pin.__enter__()
        if snapshot.keys() != ranges.keys():
            pin.__exit__(None, None, None)
            self._export_status.update(state="error", message="La ventana ya no está completa en el búfer.")
            self._export_status["errors"] += 1
            return
        offset = self._timeline_offset

        def export():
            try:
                words = transcript.snapshot()[1] if transcript else []
                record = self.writer.write(trigger, self.options, probe, words, list(snapshot),
                                           ranges=snapshot, manual_window=manual_window)
                record.start += offset
                record.end += offset
                if not record.error and not record.files.get("mp4"):
                    record.error = "La exportación no produjo un MP4."
                with self._state_lock:
                    if not record.error:
                        archive = self.dir / "records"
                        archive.mkdir(exist_ok=True)
                        projects.atomic_json(archive / (record.id + ".json"), record.__dict__)
                        self._export_status["completed"] += 1
                    else:
                        self.errors.append(record.error)
                        del self.errors[:-24]
                        self._export_status["errors"] += 1
                    self.records.append(record)
                    del self.records[:-100]
                    if record.error:
                        message = record.error
                    else:
                        message = f"Clip guardado ({record.end - record.start:.1f} s)."
                        srt = record.files.get("srt")
                        if not srt or not Path(srt).stat().st_size:
                            message += " Subtítulos pendientes de revisión."
                    self._export_status.update(state="error" if record.error else "ok", message=message)
                self._save()
                if not record.error and self.on_clip:
                    self.on_clip(record.files["mp4"])
            except (OSError, RuntimeError, ValueError) as exc:
                with self._state_lock:
                    self._export_status.update(state="error", message=f"No se completó la exportación ({type(exc).__name__}).")
                    self._export_status["errors"] += 1
                self._save()
            finally:
                pin.__exit__(None, None, None)

        self._export_status.update(state="exporting", message="Exportando clip.")
        self._export_thread = threading.Thread(target=export, name=f"live-export-{self.id}", daemon=True)
        try:
            self._export_thread.start()
        except RuntimeError:
            pin.__exit__(None, None, None)
            raise

    def _allowed(self, score, now):
        if score < self.options["live_threshold"]:
            return False
        if self.last_trigger is not None and now - self.last_trigger < self.options["live_cooldown"]:
            return False
        completed = getattr(self, "_export_status", {}).get("completed", len(self.records))
        if (not getattr(self, "pipeline", None) or self.options["live_duration"] == "legacy") and completed >= self.options["live_max_clips"] > 0:
            return False
        if self.last_trigger is not None and now - self.last_trigger < self.options["live_window"] / 2:
            return False
        return True

    def _produce_live(self, ranges, latest, trigger_time, candidate, transcript, ending):
        """Preserve the current generation incrementally before buffer retirement."""
        try:
            if self._production_window:
                ident, start, target = self._production_window
                available = {path: bounds for path, bounds in ranges.items() if bounds[1] > start}
                try:
                    through = self.pipeline.preserve(self.id, ident, self.buffer, available, self._timeline_offset)
                    if ending or through is not None and through >= target + self._timeline_offset:
                        self._seal_production()
                except live_production.Rejected as exc:
                    self._seal_production(str(exc))
                return
            if not self._allowed(candidate["score"], trigger_time) or candidate["score"] < 6:
                return
            first = next(iter(ranges.values()))[0]
            low, _, _, hard = live_production.PROFILES[self.options["live_duration"]]
            if ending and latest - first < low:
                return
            # Leave material before the requested beginning so ASR can find
            # the start of a sentence. Pin only segments belonging to this generation.
            if trigger_time < first or trigger_time >= latest or latest - first < min(12., low / 3):
                return
            if self.options.get("live_skip_ads") and any(getattr(a, "ad_like", False) for _, a in self.analyzers):
                return
            if not self._brief_allows(transcript, trigger_time):
                return
            effective_pre = min(self.options["live_pre_roll"], hard / 2)
            start = max(first, trigger_time - effective_pre - 8.)
            target = max(start + hard + 3., trigger_time + 8.)
            trigger = live_clips.Trigger(self._timeline_offset + trigger_time, candidate["score"], candidate["signals"])
            self.last_trigger = trigger_time
            ident, error = self.pipeline.reserve(self.id, trigger, self.options)
            if not ident:
                self.health["production_message"] = error
                return
            self.health["production_message"] = None
            self._production_window = ident, start, target
            self._produce_live(ranges, latest, trigger_time, candidate, transcript, ending)
        except (OSError, sqlite3.Error) as exc:
            self.health["production_message"] = f"No se pudo actualizar la producción local ({type(exc).__name__})."

    def _seal_production(self, error=None):
        if self._production_window:
            self.pipeline.seal(self._production_window[0], error)
            self._production_window = None

    def _brief_allows(self, transcript, now):
        if not self.rules:
            return True
        from campaign import _matches
        words = transcript.snapshot()[1] if transcript else []
        text = " ".join(str(w.get("word", "")).lower() for w in words
                        if now - 10 <= w.get("start", 0) <= now)
        if any(b in text for b in self.rules.get("banned", [])):
            return False
        return all(_matches(text, phrase)
                   for phrase in self.rules.get("must_mention", []))

    def start(self):
        self._log(RIGHTS_NOTICE)
        self._log(f"Watching {self.source.platform} via {self.source.resolved_by}")
        try:
            self._start_capture()
            if not self._stop.is_set():
                self.status = "running"
                self._save()
                self._loop()
        except OSError:
            self._fail("Could not start or continue live capture. Check FFmpeg and write access to the Studio projects folder.")
        except Exception as exc:
            self._fail(f"Live se detuvo por un fallo interno ({type(exc).__name__}). Revisa el diagnóstico y abre una nueva sesión.")
        finally:
            try:
                self._seal_production()
            except (OSError, sqlite3.Error):
                pass  # The durable collector is sealed on lease expiry.
            with self._capture_lock:
                if self.buffer is not None:
                    self.buffer.stop()
            # Stopped sessions remain visible in Studio, but should not retain
            # native speech models or analyzer word buffers.
            self.analyzers = []
            self._transcript_worker = {"lock": threading.Lock(), "model": None}
        return self

    def _fail(self, message):
        self.health.update(state="failed", ffmpeg_alive=False)
        self.status = "failed"
        self.stopped_at = time.time()
        self.errors.append(message)
        self._log(message)
        self._save()

    def _loop(self):
        probe = pending = None
        ready_at = eof_at = None
        last_score_at = None
        revision = -1
        save_at = 0.
        while not self._stop.is_set():
            if (self.dir / "stop").exists():
                self._log("Stop requested by agent; shutting down.")
                self.stop()
                return
            if self._stop.wait(.5):
                break
            if shutil.disk_usage(self.dir).free < max(128 * 1024 * 1024, self.buffer.max_bytes):
                self._fail("Espacio libre insuficiente para mantener el búfer Live. Se conservaron las exportaciones.")
                break
            ranges = self._contiguous(self.buffer.closed_ranges())
            latest = next(reversed(ranges.values()))[1] if ranges else 0.
            self.stream_time = max(self.stream_time, self._timeline_offset + latest)
            healthy = self._capture_alive()
            self.health["capture"] = self._capture_status(self.buffer, len(ranges))
            self.bus.prune(latest)
            if probe is None and ranges:
                try:
                    candidate = self.buffer.probe()
                    if candidate.get("has_video") and candidate.get("width"):
                        probe = candidate
                except (OSError, subprocess.SubprocessError, ValueError):
                    self._warn("No se pudo verificar el formato del último segmento cerrado.")
            smart = self.pipeline is not None and self.options["live_duration"] != "legacy"
            if not self._analyzers_started and probe is not None:
                self.analyzers = live_signals.start_analyzers(
                    self.buffer, self.bus, {**self.options, "device": self.options["live_whisper_device"]},
                    self.source.platform, _channel(self.source.page_url or self.source.url),
                    shared=self.pipeline.continuous if self.pipeline else self._transcript_worker)
                self._analyzers_started = True
            analyzer_status = {analyzer.name: analyzer.status(latest) for _, analyzer in self.analyzers}
            for name, status in analyzer_status.items():
                totals = self._analyzer_totals.get(name, {})
                for key in ("processed", "errors", "retries", "dropped"):
                    status[key] += totals.get(key, 0)
                status["last_error"] = status["last_error"] or totals.get("last_error")
            self.health["analyzers"] = analyzer_status
            self.health["tracks"] = {"audio": probe["has_audio"], "video": probe["has_video"]} if probe else {}
            self.health["state"] = "ok" if healthy and ranges and not any(
                item["state"] == "degraded" for item in analyzer_status.values()) else "degraded"
            if healthy and ranges and time.monotonic() - self.buffer.started_monotonic > 30:
                self._retry_streak = 0

            # Select a recent measured peak when a new batch of samples arrives.
            # Batches can arrive together after HLS catch-up or Whisper inference.
            revision, sample_times = self.bus.recent_times(latest, 18., revision)
            measured = [(t, self.detector.evaluate(self.bus, t, freshness=8.)) for t in sample_times]
            result = self.detector.evaluate(self.bus, max(0., latest - .001), freshness=12.)
            trigger_time, candidate = max(
                ((t, item) for t, item in measured if item["signals"] and self._allowed(item["score"], t)),
                key=lambda pair: (pair[1]["score"], pair[0]), default=(latest, result))
            detected = result["score"] if result["signals"] else None
            self.health["detection"] = {"state": "ok" if detected is not None else "waiting",
                                        "last_score": detected, "signals": result["signals"]}
            if detected is not None and (latest, detected) != last_score_at:
                last_score_at = (latest, detected)
                self.recent_scores = (self.recent_scores + [detected])[-60:]
            transcript = next((analyzer for _, analyzer in self.analyzers
                               if isinstance(analyzer, live_signals.TranscriptAnalyzer)), None)
            if smart and (self._production_window or (probe and ranges and candidate["signals"])):
                self._produce_live(ranges, latest, trigger_time, candidate, transcript, ending=not healthy)
            with self._export_lock:
                exporting = self._export_thread and self._export_thread.is_alive()
                manual = self._manual_request
                if not exporting and manual and probe and ranges:
                    seconds, _ = manual
                    start = max(next(iter(ranges.values()))[0], latest - seconds)
                    if ready_at is None:
                        ready_at = time.monotonic()
                    through = transcript.snapshot()[0] if transcript else None
                    if (not transcript or not transcript.available or (through is not None and through >= latest - .1)
                            or time.monotonic() - ready_at >= 8):
                        self._manual_request = None
                        window = {path: pair for path, pair in ranges.items() if pair[1] > start}
                        self._queue_export(live_clips.Trigger(latest, None, {}), probe, transcript, window, (start, latest))
                        ready_at = None
                elif not smart and not exporting and not manual and pending and (latest >= pending.time + self.options["live_tail"] or not healthy):
                    if ready_at is None:
                        ready_at = time.monotonic()
                    caption_end = min(latest, pending.time + self.options["live_tail"])
                    through = transcript.snapshot()[0] if transcript else None
                    if (not ranges or pending.time < next(iter(ranges.values()))[0]):
                        pending = ready_at = None
                    elif (not transcript or not transcript.available or (through is not None and through >= caption_end - .1)
                          or time.monotonic() - ready_at >= 5):
                        start, end = live_clips.plan_window(
                            pending.time, self.options["live_buffer"], self.options["live_pre_roll"],
                            self.options["live_tail"], self.options["min_len"], self.options["max_len"], latest)
                        window = {path: pair for path, pair in ranges.items() if pair[1] > start and pair[0] < end}
                        if window:
                            self._queue_export(pending, probe, transcript, window)
                        pending = ready_at = None
                elif (not smart and not exporting and not manual and not pending and probe and ranges
                      and candidate["signals"] and self._allowed(candidate["score"], trigger_time)
                      and trigger_time >= next(iter(ranges.values()))[0]):
                    if not (self.options.get("live_skip_ads") and any(getattr(a, "ad_like", False)
                                                                       for _, a in self.analyzers)):
                        if self._brief_allows(transcript, trigger_time):
                            self.last_trigger = trigger_time
                            pending = live_clips.Trigger(trigger_time, candidate["score"], candidate["signals"])
            if time.monotonic() >= save_at:
                self._save()
                save_at = time.monotonic() + 2

            if self.source.page_url and time.time() - self._last_refresh > 600:
                self._last_refresh = time.time()
                try:
                    self.source = self.source.refresh(self.options)
                    self.health["refreshes"] += 1
                except ValueError as exc:
                    self._warn(str(exc))
            if healthy:
                eof_at = None
                continue

            natural_end = (not self.buffer.segment_problem and self.buffer.process is not None
                           and self.buffer.process.poll() == 0 and bool(ranges))
            if natural_end:
                eof_at = eof_at or time.monotonic()
                # A finite fixture or VOD can end before workers finish its last window.
                if time.monotonic() - eof_at < 8 or pending or (manual and self._manual_request):
                    continue
                if not self.source.page_url:
                    if self._export_thread and self._export_thread.is_alive():
                        continue
                    self.stop()
                    break
            if self._stop.is_set():
                break
            if smart:
                try:
                    self._seal_production()
                except (OSError, sqlite3.Error):
                    self._production_window = None
            pending = ready_at = eof_at = last_score_at = None
            revision = -1
            probe = None
            scaling = self.buffer.segment_problem == live_capture.HEIGHT_ERROR and not self._scale_capture
            if scaling:
                self._scale_capture = True
                self._warn(live_capture.SCALING_WARNING)
            self._restart_capture(refresh=not scaling)
        self._save()

    def exporting(self):
        worker = getattr(self, "_export_thread", None)
        return bool(worker and worker.is_alive())

    def wait_for_export(self):
        worker = getattr(self, "_export_thread", None)
        if worker is not None:
            worker.join()

    def stop(self):
        self._stop.set()
        with self._capture_lock:
            if self.buffer is not None:
                self.buffer.stop()
        for thread, _ in self.analyzers:
            thread.join(timeout=.25)
        self.health["ffmpeg_alive"] = False
        with self._export_lock:
            if self._manual_request is not None:
                self._manual_request = None
                self._export_status.update(state="error", message="Sesión detenida antes de exportar.")
        if self.status != "failed":
            self.status = "stopped"
        self.stopped_at = time.time()
        self._save()


def _channel(url):
    path = urlparse(url).path.strip("/")
    return path.split("/")[0] if path else ""


def run_live(url, options, root, on_log=None, brief_text=None, explicit_keys=None):
    """Start a live session and block until it stops."""
    options = projects.validate_options(options)
    if explicit_keys is None:
        # Preserve settings already chosen by the caller (CLI flags or a format).
        explicit_keys = {key for key, default in projects.DEFAULT_OPTIONS.items()
                         if options.get(key) != default}
    sig = video_types.signals_from_measurements(source=url)
    detection = video_types.classify(sig)
    resolved = video_types.resolve_format(
        detection, options.get("video_type"), options.get("format"))
    options = video_types.apply_format(options, resolved["format"], explicit_keys)
    if "live_threshold" not in explicit_keys and options["live_threshold"] == projects.LIVE_DEFAULTS["live_threshold"]:
        import automation
        settings = automation.SettingsStore(root).read()["settings"]
        options["live_threshold"] = settings["minimum_clip_score"]
    source = live_capture.resolve_live_source(url, options)
    pipeline = live_production.Pipeline(root)
    pipeline.start()
    try:
        session = LiveSession(source, options, root, brief_text=brief_text, pipeline=pipeline,
                              detection=detection, format_info={
                                  "name": resolved["format"]["name"],
                                  "source": resolved["source"],
                                  "needs_confirmation": resolved["needs_confirmation"]})
        session._on_log = on_log
        session.start()
        pipeline.drain(session.id)
    finally:
        pipeline.close()
    return session
