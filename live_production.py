"""Durable production of clips from closed Live segments."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager, redirect_stdout
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid

import branding
import download_and_transcribe as dt
import live_clips
import projects
import render_clips
import score_clips
import subtitle_quality as quality
from live_capture import safe_stderr

PROFILES = {
    "30-60": (30, 45, 60, 60),
    "60-90": (60, 75, 90, 180),
    "90-180": (90, 130, 180, 180),
    "auto": (30, 60, 90, 180),
}
ACTIVE = ("collecting", "queued", "transcribing", "editing", "quality")
MAX_PENDING = 4
DISK_RESERVE = 512 * 1024**2
SPOOL_LIMIT = 8 * 1024**3
JOB_LIMIT = 768 * 1024**2
LEASE = 45
MODEL = "small"


class Rejected(ValueError):
    pass


class Paused(Exception):
    pass


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


@contextmanager
def resource_slot(lock, stopping):
    while not lock.acquire(timeout=.25):
        if stopping.is_set():
            raise Paused()
    try:
        if stopping.is_set():
            raise Paused()
        yield
    finally:
        lock.release()


def topic_terms(sentences):
    text = " ".join(sentence["text"].casefold() for sentence in sentences)
    return set(re.findall(r"[^\W\d_]{4,}", text)) - score_clips.STOPWORDS


def select_window(words, speech, duration, trigger, profile, pre_roll=30):
    """Find a closed utterance around a highlight. Times are source-local."""
    low, preferred, usual, hard = PROFILES[profile]
    if duration + .08 < low:
        raise Rejected(f"Fuente continua insuficiente para el perfil {profile}.")
    trigger = min(duration, max(0., trigger))
    if not speech and not words:
        start = min(max(0., trigger - low / 2), duration - low)
        return round(max(0., start), 3), round(min(duration, start + low), 3)
    if not words:
        raise Rejected("Hay voz, pero Whisper no devolvió palabras verificables.")

    sentences = score_clips.sentenceize(words)
    starts, ends = [], []
    for i, sentence in enumerate(sentences):
        start, end = sentence["start"], sentence["end"]
        prev_end = sentences[i - 1]["end"] if i else 0.
        if start <= trigger and (i or start >= .35 or not speech or speech[0][0] >= .18):
            # Do not assume the first decoded words are the start of a sentence
            # when the source itself begins in speech.
            starts.append((max(prev_end, start - .24), i))
        following = sentences[i + 1]["start"] if i + 1 < len(sentences) else duration
        last = re.findall(r"[^\W\d_]+", sentence["text"].casefold())
        if last and last[-1] in {"y", "o", "ni", "si", "para", "con"}:
            continue
        if end < trigger or not score_clips.is_usable_endpoint(sentences, i, following_start=following):
            continue
        gap = following - end
        if sentence["boundary"] == "source_end" and gap < .6:
            continue
        if gap < .14 and sentence["boundary"] != "punct":
            continue
        if sentence["boundary"] == "source_end" and speech and speech[-1][1] > end + .25:
            continue
        prior = topic_terms(sentences[max(0, i - 1):i + 1])
        following_terms = topic_terms(sentences[i + 1:i + 3])
        common = len(prior & following_terms)
        continues = gap < 1.8 and common >= 2 and common / min(len(prior), len(following_terms)) >= .35
        ends.append((min(duration, end + min(.25, max(0., gap / 2))), gap, continues))

    choices = []
    desired_start = max(0., trigger - pre_roll)
    for start, _ in starts:
        for end, pause, continues in ends:
            length = end - start
            if low <= length <= hard and end >= trigger + min(2., max(0., duration - trigger)):
                # Prefer a resolved change of topic or pause to an apparently
                # complete sentence that immediately continues the same event.
                choices.append((continues, length > usual, abs(length - preferred) + .45 * abs(start - desired_start)
                                - min(5., pause * 2), start, end))
    if not choices:
        raise Rejected("No hay límites de conversación verificables que completen el momento en el perfil elegido.")
    _, _, _, start, end = min(choices)
    return round(start, 3), round(end, 3)


def gpu_sample():
    """Optional system-wide utilization; do not report it as Whisper-only."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        result = subprocess.run(["nvidia-smi", "-i", "0", "--query-gpu=utilization.gpu,memory.used,memory.free",
                                 "--format=csv,noheader,nounits"], capture_output=True, check=True, timeout=2)
        utilization, used, free = map(int, result.stdout.decode("ascii").splitlines()[0].split(","))
        return {"utilization_percent": utilization, "used_mib": used, "free_mib": free}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


class _WhisperRuntime:
    """Model cache inside the isolated production worker."""
    def __init__(self):
        self.cpu_model = None

    def transcribe(self, media, duration, requested):
        import ctranslate2
        import gc

        started = time.monotonic()
        fallback = None
        before = gpu_sample()
        try:
            cuda = requested != "cpu" and ctranslate2.get_cuda_device_count() > 0
        except (RuntimeError, OSError):
            cuda = False
        if requested != "cpu" and not cuda:
            fallback = "CUDA no disponible en CTranslate2."
        if cuda and before and before["free_mib"] < 2048:
            fallback = "Menos de 2048 MiB libres. Whisper usó CPU."
            cuda = False
        device = "cuda" if cuda else "cpu"
        model = None
        for attempt in range(2):
            compute = "int8_float16" if device == "cuda" else "int8"
            try:
                if device == "cpu":
                    if self.cpu_model is None:
                        self.cpu_model = dt._load_model(MODEL, device, compute)
                    model = self.cpu_model
                else:
                    model = dt._load_model(MODEL, device, compute)
                inference = time.monotonic()
                _, words, language, _ = dt._transcribe_media(model, media)
                seconds = time.monotonic() - inference
                backend = getattr(model, "model", None)
                actual = getattr(backend, "device", None) or "sin confirmar"
                metrics = {"requested": requested, "device": str(actual), "model": MODEL,
                           "compute_type": str(getattr(backend, "compute_type", compute)),
                           "inference_seconds": round(seconds, 3), "rtf": round(seconds / duration, 4),
                           "elapsed_seconds": round(time.monotonic() - started, 3), "fallback": fallback,
                           "gpu_before": before, "gpu_after": gpu_sample() if device == "cuda" else None}
                return words, language, metrics
            except (RuntimeError, OSError, ValueError) as exc:
                if device != "cuda" or attempt:
                    raise Rejected("Whisper no pudo transcribir la fuente preservada.") from exc
                fallback = f"Falló la inferencia o carga CUDA ({type(exc).__name__}); se reintentó en CPU."
                device = "cpu"
            finally:
                # Release GPU weights before handing the shared lock back to
                # Ollama/rendering. Retain only the CPU model between jobs.
                model = None
                gc.collect()


def _whisper_worker(connection):
    from unicode_io import configure_utf8

    configure_utf8()
    runtime = _WhisperRuntime()
    with connection, open(os.devnull, "w", encoding="utf-8") as discard, redirect_stdout(discard):
        while True:
            try:
                arguments = connection.recv()
            except EOFError:
                return
            try:
                connection.send((True, runtime.transcribe(*arguments)))
            except (OSError, ValueError, TypeError, RuntimeError, ImportError) as exc:
                message = str(exc) if isinstance(exc, Rejected) else f"Whisper no está operativo ({type(exc).__name__})."
                connection.send((False, message))


class Whisper:
    def __init__(self, resources, stopping):
        self.resources = resources
        self.stopping = stopping
        self.process = self.connection = None

    def transcribe(self, media, duration, requested):
        with resource_slot(self.resources, self.stopping):
            if self.process is None or not self.process.is_alive():
                self.close()
                context = multiprocessing.get_context("spawn")
                self.connection, child = context.Pipe()
                self.process = context.Process(target=_whisper_worker, args=(child,), daemon=True)
                try:
                    self.process.start()
                finally:
                    child.close()
            self.connection.send((media, duration, requested))
            deadline = time.monotonic() + min(600., max(240., duration * 5))
            while not self.stopping.is_set():
                try:
                    if self.connection.poll(.2):
                        ok, response = self.connection.recv()
                        if ok:
                            metrics = response[2]
                            if metrics.get("device") == "cuda" or (metrics.get("fallback") or "").startswith("Falló"):
                                self.close()  # Release the GPU context before Ollama can run.
                            return response
                        raise Rejected(response)
                except EOFError:
                    self.close()
                    raise Rejected("El proceso de Whisper se cerró inesperadamente.") from None
                if time.monotonic() >= deadline:
                    self.close()
                    raise Rejected("Whisper superó su tiempo límite. La fuente se conservó.")
            self.close()
            raise Paused()

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.process is not None:
            if self.process.pid is not None:
                self.process.join(.2)
                if self.process.is_alive():
                    self.process.terminate()
                    self.process.join(1)
                if self.process.is_alive():
                    self.process.kill()
                    self.process.join(1)
            self.process = None


class Pipeline:
    def __init__(self, root, resources=None, on_clip=None):
        self.root = Path(root).resolve()
        self.directory = self.root / "live"
        self.db = self.directory / "production.sqlite"
        self.resources = resources or threading.Lock()
        self.on_clip = on_clip
        self.owner = uuid.uuid4().hex
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.whisper = Whisper(self.resources, self.stop)
        self.continuous = {"lock": threading.Lock(), "model": None}
        self.worker = None
        self.directory.mkdir(exist_ok=True, parents=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, session TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                updated REAL NOT NULL, owner TEXT, lease REAL, moment REAL NOT NULL, profile TEXT NOT NULL,
                options TEXT NOT NULL, score REAL NOT NULL, signals TEXT NOT NULL, media TEXT NOT NULL DEFAULT '[]',
                error TEXT, metrics TEXT, result TEXT)""")
            conn.execute("CREATE INDEX IF NOT EXISTS jobs_session ON jobs(session, created)")
            conn.execute("CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, updated)")

    @contextmanager
    def connect(self, write=False):
        conn = sqlite3.connect(self.db, timeout=3)
        conn.row_factory = sqlite3.Row
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            with conn:
                yield conn
        finally:
            conn.close()

    def status(self, session):
        with self.connect() as conn:
            counts = Counter({row[0]: row[1] for row in conn.execute(
                "SELECT state, COUNT(*) FROM jobs WHERE session=? GROUP BY state", (session,))})
            recent = [dict(row) for row in conn.execute(
                "SELECT id,state,profile,score,error,metrics,result,created FROM jobs WHERE session=? ORDER BY created DESC LIMIT 12",
                (session,))]
            metrics = conn.execute("SELECT metrics FROM jobs WHERE session=? AND metrics IS NOT NULL ORDER BY updated DESC LIMIT 1",
                                   (session,)).fetchone()
        for row in recent:
            row["metrics"] = json.loads(row["metrics"]) if row["metrics"] else None
        return {"detected": sum(counts.values()), "counts": dict(counts), "recent": recent,
                "whisper": json.loads(metrics[0]) if metrics else None, "capacity": MAX_PENDING}

    def completed(self, session):
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM jobs WHERE session=? AND state='done'", (session,)).fetchone()[0]

    def busy(self, session=None):
        with self.connect() as conn:
            params = ACTIVE if session is None else (*ACTIVE, session)
            where = "" if session is None else " AND session=?"
            return bool(conn.execute("SELECT 1 FROM jobs WHERE state IN (?,?,?,?,?)" + where + " LIMIT 1", params).fetchone())

    def drain(self, session):
        while self.busy(session) and not self.stop.wait(.25):
            continue

    def reserve(self, session, trigger, options):
        now = time.time()
        ident = uuid.uuid4().hex[:12]
        persisted = {key: value for key, value in options.items() if key != "live_headers"}
        with self.connect(write=True) as conn:
            cap = options["live_max_clips"]
            if cap and conn.execute("SELECT COUNT(*) FROM jobs WHERE session=?", (session,)).fetchone()[0] >= cap:
                return None, "Límite de producción por sesión alcanzado."
            hour = options["live_max_per_hour"]
            if hour and conn.execute("SELECT COUNT(*) FROM jobs WHERE session=? AND created>=?",
                                     (session, now - 3600)).fetchone()[0] >= hour:
                return None, "Límite de producción por hora alcanzado."
            if conn.execute("SELECT COUNT(*) FROM jobs WHERE state IN (?,?,?,?,?)", ACTIVE).fetchone()[0] >= MAX_PENDING:
                return None, "Cola de producción llena; la captura continúa."
            last = conn.execute("SELECT MAX(moment) FROM jobs WHERE session=?", (session,)).fetchone()[0]
            if last is not None and trigger.time - last < max(20., PROFILES[options["live_duration"]][0] * .6):
                return None, "Momento solapado con una detección anterior."
            media_bytes = sum(path.stat().st_size for pattern in ("*/production/*/*.ts", "*/production/*/*.mp4",
                              "*/production/*/render/*.mp4", "*/windows/*.mp4")
                              for path in self.directory.glob(pattern) if path.is_file())
            if (shutil.disk_usage(self.directory).free < DISK_RESERVE
                    or media_bytes >= SPOOL_LIMIT - MAX_PENDING * JOB_LIMIT):
                return None, "Almacenamiento insuficiente para preservar nuevas fuentes."
            conn.execute("""INSERT INTO jobs(id,session,state,created,updated,moment,profile,options,score,signals)
                         VALUES(?,?,'collecting',?,?,?,?,?,?,?)""",
                         (ident, session, now, now, trigger.time, options["live_duration"],
                          json.dumps(persisted), trigger.score, json.dumps(trigger.signals)))
        return ident, None

    def folder(self, session, ident):
        if not all(re.fullmatch(r"[a-f0-9]{12}", key) for key in (session, ident)):
            raise Rejected("Identificador de producción inválido.")
        path = self.directory / session / "production" / ident
        if path.resolve() != path:
            raise Rejected("Ruta de producción inválida.")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def preserve(self, session, ident, buffer, ranges, offset):
        """Link closed files while pinned, then commit their time bounds."""
        directory = self.folder(session, ident)
        with self.connect() as conn:
            row = conn.execute("SELECT media FROM jobs WHERE id=? AND state='collecting'", (ident,)).fetchone()
        if row is None:
            raise Rejected("La reserva de producción ya expiró.")
        media = json.loads(row[0])
        known = {item[0] for item in media}
        size = sum(item[3] for item in media)
        additions = []
        remaining = {path: bounds for path, bounds in ranges.items() if path.name not in known}
        with buffer.pin(remaining) as snapshot:
            if snapshot.keys() != remaining.keys():
                raise Rejected("Parte de la ventana salió del búfer antes de poder preservarla.")
            for path, (start, end) in snapshot.items():
                start, end = start + offset, end + offset
                if media and abs(media[-1][2] - start) > .15:
                    raise Rejected("La fuente tiene una discontinuidad en el búfer Live.")
                segment_size = path.stat().st_size
                if size + segment_size > JOB_LIMIT:
                    raise Rejected("La fuente excede el máximo de almacenamiento por clip.")
                if shutil.disk_usage(directory).free - segment_size < DISK_RESERVE:
                    raise Rejected("Reserva de disco insuficiente para preservar el clip.")
                target = directory / path.name
                if not target.exists():
                    try:
                        os.link(path, target)
                    except OSError:
                        temporary = target.with_suffix(".tmp")
                        try:
                            shutil.copyfile(path, temporary)
                            temporary.replace(target)
                        finally:
                            temporary.unlink(missing_ok=True)
                size += segment_size
                entry = [path.name, start, end, segment_size]
                media.append(entry)
                additions.append(entry)
        if additions:
            with self.connect(write=True) as conn:
                conn.execute("UPDATE jobs SET media=?,updated=? WHERE id=? AND state='collecting'",
                             (json.dumps(media), time.time(), ident))
        return media[-1][2] if media else None

    def seal(self, ident, error=None):
        with self.connect(write=True) as conn:
            self._seal(conn, ident, error)
        self.wake.set()

    @staticmethod
    def _seal(conn, ident, error=None):
        row = conn.execute("SELECT media,profile,moment FROM jobs WHERE id=? AND state='collecting'", (ident,)).fetchone()
        if not row:
            return
        media = json.loads(row["media"])
        if (not media or media[-1][2] - media[0][1] + .08 < PROFILES[row["profile"]][0]
                or not media[0][1] <= row["moment"] < media[-1][2]):
            error = error or "La captura terminó antes de preservar el contexto mínimo."
        conn.execute("UPDATE jobs SET state=?,updated=?,error=? WHERE id=?",
                     ("rejected" if error else "queued", time.time(), error, ident))

    def start(self):
        self.worker = threading.Thread(target=self._run, name="live-production", daemon=True)
        self.worker.start()

    def _claim(self):
        now = time.time()
        with self.connect(write=True) as conn:
            for stale in conn.execute("SELECT id FROM jobs WHERE state='collecting' AND updated<?", (now - LEASE,)).fetchall():
                self._seal(conn, stale[0])
            if conn.execute("SELECT 1 FROM jobs WHERE lease>? LIMIT 1", (now,)).fetchone():
                return None
            job = conn.execute("""SELECT * FROM jobs WHERE state IN ('queued','transcribing','editing','quality')
                               ORDER BY created LIMIT 1""").fetchone()
            if job:
                conn.execute("UPDATE jobs SET state='transcribing',owner=?,lease=?,updated=? WHERE id=?",
                             (self.owner, now + LEASE, now, job["id"]))
            return job

    def _beat(self, close, ident):
        while not close.wait(LEASE / 3):
            try:
                with self.connect(write=True) as conn:
                    conn.execute("UPDATE jobs SET lease=? WHERE id=? AND owner=?", (time.time() + LEASE, ident, self.owner))
            except sqlite3.Error:
                continue

    def _stage(self, ident, state, metrics=None):
        if self.stop.is_set():
            raise Paused()
        with self.connect(write=True) as conn:
            updated = conn.execute("UPDATE jobs SET state=?,metrics=COALESCE(?,metrics),updated=? WHERE id=? AND owner=?",
                                   (state, json.dumps(metrics) if metrics else None, time.time(), ident, self.owner))
            if not updated.rowcount:
                raise Paused()

    def _run(self):
        try:
            while not self.stop.is_set():
                try:
                    job = self._claim()
                    if job:
                        self._execute(job)
                        continue
                except (OSError, sqlite3.Error):
                    pass
                self.wake.wait(1)
                self.wake.clear()
        finally:
            self.whisper.close()

    def _execute(self, job):
        done = threading.Event()
        threading.Thread(target=self._beat, args=(done, job["id"]), daemon=True).start()
        error = result = None
        try:
            result = self._produce(job)
            state = "done"
        except Paused:
            state = "queued"
        except Rejected as exc:
            state, error = "rejected", str(exc)
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError, TypeError, AttributeError, KeyError, ImportError) as exc:
            state = "error"
            detail = safe_stderr(getattr(exc, "stderr", None))
            error = f"Producción Live: {type(exc).__name__}" + (f" · {detail}" if detail else "")
        finally:
            done.set()
        with self.connect(write=True) as conn:
            conn.execute("UPDATE jobs SET state=?,error=?,result=?,owner=NULL,lease=NULL,updated=? WHERE id=? AND owner=?",
                         (state, error, result, time.time(), job["id"], self.owner))
        if result and self.on_clip:
            self.on_clip(f"live/{job['session']}", result)

    def _produce(self, job):
        ident, session = job["id"], job["session"]
        directory = self.folder(session, ident)
        home = directory.parent.parent
        archive = home / "records" / f"{ident}.json"
        if archive.is_file():
            saved = projects.read_json(archive)
            output = Path(saved["files"]["mp4"])
            if output.parent == home / "clips" and output.resolve() == output and output.is_file():
                self._clear_segments(directory)
                return output.name
        if shutil.disk_usage(directory).free < DISK_RESERVE:
            raise Rejected("No hay suficiente espacio libre para transcribir y renderizar.")
        options = json.loads(job["options"])
        media = json.loads(job["media"])
        source = directory / "source.mp4"
        if not source.exists():
            temp = directory / "stitching.mp4"
            live_clips.stitch([directory / item[0] for item in media], temp, runner=self._stitch)
            temp.replace(source)
        duration, tracks = quality.probe_media(source)
        self._verify_tracks(tracks, duration)
        expected_source = media[-1][2] - media[0][1]
        if abs(expected_source - duration) > max(1., expected_source * .02):
            raise Rejected("La duración preservada indica una discontinuidad audiovisual.")
        transcript = directory / "transcript.json"
        if transcript.is_file():
            info = projects.read_json(transcript)
        else:
            speech = quality.speech_ranges(source, duration, max(0., float(tracks["audio"].get("start_time") or 0)))
            words, language, metrics = self.whisper.transcribe(source, duration, options["live_whisper_device"]) if speech else ([], None, None)
            words = sorted((w for w in words if finite(w.get("start")) and finite(w.get("end"))
                            and 0 <= w["start"] < w["end"] and isinstance(w.get("word"), str)),
                           key=lambda w: (w["start"], w["end"]))
            info = {"words": words, "speech": speech, "language": language, "metrics": metrics}
            projects.atomic_json(transcript, info)
        metrics = {**info["metrics"], "time_since_detection_seconds": round(time.time() - job["created"], 3)} if info["metrics"] else None
        self._stage(ident, "editing", metrics)
        start, end = select_window(info["words"], info["speech"], duration, job["moment"] - media[0][1],
                                   job["profile"], options["live_pre_roll"])
        absolute = {"start": media[0][1] + start, "end": media[0][1] + end}
        self._check_overlap(home, absolute)
        files = self._render(home, directory, ident, start, end, tracks["video"], source, options, info)
        self._stage(ident, "quality")
        output = files["mp4"]
        voiced = any(left < end and right > start for left, right in info["speech"])
        report = self._check_output(output, end - start, voiced, info["speech"], start, end)
        report["profile"] = job["profile"]
        report["requested_seconds"] = end - start
        report["voice"] = "present" if voiced else "not_detected"
        projects.atomic_json(directory / "quality.json", report)

        # Promote sidecars first, MP4 last. Only the archive record makes the
        # export visible to Automation and Publicaciones.
        clips = home / "clips"
        clips.mkdir(exist_ok=True)
        promoted = {}
        for kind, path in sorted(files.items(), key=lambda item: item[0] == "mp4"):
            final = clips / path.name
            path.replace(final)
            promoted[kind] = str(final)
        # Keep this source in the queue's own directory so the quota accounts
        # for retained inputs and repaired versions can find it after a restart.
        promoted["window"] = str(source)
        promoted["transcript"] = str(transcript)
        record = live_clips.ClipRecord(ident, round(media[0][1] + start, 3), round(media[0][1] + end, 3),
                                       job["score"], json.loads(job["signals"]), files=promoted)
        archive.parent.mkdir(exist_ok=True)
        projects.atomic_json(archive, {**record.__dict__, "voice": report["voice"]})
        self._clear_segments(directory)
        return Path(promoted["mp4"]).name

    @staticmethod
    def _check_overlap(home, interval):
        from score_clips import overlap_ratio

        for path in (home / "records").glob("*.json"):
            try:
                record = projects.read_json(path)
                if (record.get("manual") or record.get("error") or not record.get("files", {}).get("mp4")
                        or not all(finite(record.get(key)) for key in ("start", "end"))):
                    continue
                if overlap_ratio(interval, record) > .55:
                    raise Rejected(f"El intervalo repite más del 55 % del clip {record['id']}.")
            except Rejected:
                raise
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                continue

    @staticmethod
    def _stitch(command, timeout):
        return subprocess.run(command, capture_output=True, check=True, timeout=max(60, timeout))

    @staticmethod
    def _verify_tracks(tracks, duration):
        from production import MAX_SKEW

        try:
            starts = [float(tracks[name]["start_time"]) for name in ("audio", "video")]
            lengths = [float(tracks[name]["duration"]) for name in ("audio", "video")]
        except (KeyError, ValueError, TypeError):
            raise Rejected("FFprobe no pudo verificar las dos pistas y su sincronía.") from None
        ends = [start + length for start, length in zip(starts, lengths)]
        if (not all(map(math.isfinite, starts + lengths)) or min(lengths) <= 0 or
                max(abs(starts[0] - starts[1]), abs(ends[0] - ends[1]), abs(duration - max(ends))) > MAX_SKEW):
            raise Rejected("Las pistas de audio y video están desincronizadas o incompletas.")

    def _render(self, home, directory, ident, start, end, video, source, options, info):
        writer = live_clips.LiveClipWriter(home, self.root)
        staging = directory / "render"
        brand = branding.load_brand(options["brand"]) if options["brand"] else None
        mark = writer.watermark.for_render()
        with resource_slot(self.resources, self.stop):
            render_clips.render({"id": ident, "start": start, "end": end}, info["words"], source,
                                int(video["width"]), int(video["height"]), str(home), options["aspect"],
                                out_dir=staging, style=options["style"], loudnorm=options["loudnorm"],
                                brand=brand, layout=options["layout"], centers=options["centers"],
                                caption=options["caption"], fit=options["fit"], facecam_box=options["facecam_box"],
                                facecam_height=options["facecam_height"], mark=mark, retain_for_repair=True,
                                timeout=600, allow_model_download=False,
                                transcript_info={"model": MODEL if info["words"] else None,
                                                 "origin": "Whisper de la fuente Live preservada"})
        return {path.suffix[1:]: path for path in staging.glob(f"{ident}-*")}

    def _check_output(self, output, planned, voiced, speech, start, end):
        import production
        from editorial_providers import Cancellation

        duration, tracks = quality.probe_media(output)
        self._verify_tracks(tracks, duration)
        if not all(output.with_suffix(suffix).is_file() for suffix in (".srt", ".ass")):
            raise Rejected("FFmpeg no generó los dos archivos de subtítulos esperados.")
        if abs(duration - planned) > .35:
            raise Rejected(f"Duración exportada {duration:.2f} s frente a {planned:.2f} s previstos.")
        with resource_slot(self.resources, self.stop):
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-threads", "1", "-i", str(output),
                            "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"],
                           capture_output=True, check=True, timeout=180)
            gaps = production.continuity(output, Cancellation())
        if max(gaps.values()) > .5:
            raise Rejected("El MP4 tiene una discontinuidad audiovisual mayor de medio segundo.")
        report = {"duration": duration, "decode": "complete", "errors": [], "continuity_gaps": gaps}
        if voiced:
            report.update(quality.diagnose(output))
            cues = quality.read_srt(output.with_suffix(".srt"))
            expected = [(max(left, start) - start, min(right, end) - start) for left, right in speech
                        if left < end and right > start]
            ratio, spoken, longest = quality.coverage(cues, expected)
            if report["errors"] or quality.speech_deficit(ratio, spoken, longest):
                reason = report["errors"][0] if report["errors"] else f"cobertura de voz {ratio:.0%}"
                raise Rejected(f"Subtítulos bloqueados: {reason}")
            report["source_speech_coverage"] = ratio
        return report

    @staticmethod
    def _clear_segments(directory):
        for path in directory.glob("segment*.ts"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.worker:
            self.worker.join(timeout=2)
