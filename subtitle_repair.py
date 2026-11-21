"""Local background diagnosis and non-destructive repair of finished clips."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import math
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import branding
import captions
import projects
import render_clips
import sections
import subtitle_quality as quality
import watermark


MODELS = {"tiny", "base", "small", "medium", "large-v2", "large-v3", "turbo", "distil-large-v3"}
RENDER_TIMEOUT = 300


def _whisper(media, destination, name):
    from faster_whisper import WhisperModel
    from faster_whisper.utils import download_model

    try:
        path = download_model(name, local_files_only=True)
    except (OSError, ValueError):
        raise quality.SubtitleError("El modelo de Faster-Whisper elegido no está en la caché local.") from None
    model = WhisperModel(path, device="cpu", compute_type="int8", cpu_threads=2, local_files_only=True)
    segments, info = model.transcribe(str(media), word_timestamps=True, vad_filter=True,
                                      vad_parameters={"min_silence_duration_ms": 350}, beam_size=3)
    words = []
    count = 0
    for segment in segments:
        count += 1
        words.extend({"word": w.word, "start": w.start, "end": w.end} for w in (segment.words or []))
    if not words:
        raise quality.SubtitleError("Faster-Whisper no encontró palabras en el audio final.")
    projects.atomic_json(destination, {"model": name, "language": info.language, "words": words,
                                        "whisper_segments": count})


def transcribe_final(media, destination, name):
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(media),
                                 str(destination), name],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    except subprocess.TimeoutExpired:
        raise quality.SubtitleError("La retranscripción local superó 5 minutos. Prueba con un modelo instalado más pequeño.") from None
    if result.returncode == 2:
        raise quality.SubtitleError(result.stdout.strip())
    if result.returncode:
        raise quality.SubtitleError("Faster-Whisper no pudo retranscribir el audio final con el modelo local.")
    return projects.read_json(destination)


def _options_for_clip(base, manifest, project):
    if project.startswith("live/"):
        return projects.validate_options(manifest.get("render_options") or {})
    return projects.validate_options({**projects.read_project(base)["options"],
                                      **(manifest.get("render_options") or {})})


def clean_source(media, project, meta, duration):
    """Resolve only project-owned sources or the original explicitly chosen local file."""
    base = media.parent.parent
    start, end = meta.get("start"), meta.get("end")
    if (not all(type(v) in (int, float) and math.isfinite(v) for v in (start, end))
            or start < 0 or abs(end - start - duration) > .18):
        raise quality.SubtitleError("El montaje original no coincide con el intervalo fuente. Puedes recuperar el SRT; revisa los cortes para volver a renderizar.")
    if project.startswith("live/"):
        state = projects.read_json(base / "live_session.json")
        for row in state.get("triggers", []):
            files = row.get("files", {})
            if Path(files.get("mp4", "")).name == media.name and files.get("window"):
                path = Path(files["window"])
                if path.is_file() and path.resolve() == path and path.parent == base / "windows":
                    return path, start
        raise quality.SubtitleError("No se conserva la ventana limpia de esta sesión Live.")
    state = projects.read_project(base)
    transcript = projects.read_json(base / "transcript.json")
    source = Path(transcript["video"]).resolve()
    original = Path(state["source"]).expanduser()
    in_project = source.parent.is_relative_to(base) and not source.parent.is_relative_to(media.parent)
    user_source = original.is_absolute() and original.resolve() == source
    identity = state.get("source_identity")
    if ((in_project or user_source) and source.is_file()
            and (not identity or projects.file_identity(source) == identity) and "video" in _streams(source)):
        return source, start
    source = sections.find_full_source(base)
    if source and source.resolve() == source and "video" in _streams(source):
        return source, start
    directory = base / sections.SECTION_DIR
    if directory.resolve() == directory:
        key = sections._source_key(transcript.get("source") or state["source"])
        for info in sorted(directory.glob("section[0-9]*.json")):
            source = info.with_suffix(".mp4")
            try:
                data = json.loads(quality.local_text(info, 16384))
                a, b = data["start"], data["end"]
                if (not data.get("failed") and a <= start < end <= b
                        and sections._matches(data, key, int(info.stem[7:]), a, b)
                        and source.resolve() == source and "video" in _streams(source)):
                    return source, start - a
            except (OSError, ValueError, KeyError, TypeError):
                continue
    raise quality.SubtitleError("No hay una copia local del video limpio. El SRT se puede recuperar. Para quemar los subtítulos, recupera la fuente original desde el proyecto.")


def _streams(path):
    try:
        return quality.probe_media(path)[1]
    except (ValueError, OSError, KeyError, subprocess.SubprocessError):
        return {}


def _audio_excerpt(media, start, length):
    import numpy as np

    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-i", str(media),
                          "-map", "0:a:0", "-t", f"{length:.3f}", "-ac", "1", "-ar", "8000", "-f", "s16le",
                          "pipe:1"], capture_output=True, check=True, timeout=15)
    return np.frombuffer(out.stdout, dtype="<i2").astype("float32")


def verify_alignment(original, source, start, duration):
    """Compare distributed audio excerpts using small timestamp tolerance."""
    import numpy as np

    checked = 0
    for fraction in (.08, .25, .45, .65, .85):
        left = duration * fraction
        length = min(.9, duration - left)
        reference = _audio_excerpt(original, left, length)
        if reference.size < 800 or np.std(reference) < 20:
            continue
        pad = min(.12, start + left)
        candidate = _audio_excerpt(source, start + left - pad, length + pad + .12)
        if candidate.size < reference.size:
            raise quality.SubtitleError("La fuente local no cubre todo el audio del clip.")
        reference -= reference.mean()
        n = reference.size
        sums = np.concatenate(([0.0], np.cumsum(candidate, dtype="float64")))
        squares = np.concatenate(([0.0], np.cumsum(candidate ** 2, dtype="float64")))
        spread = np.sqrt(np.maximum(1, squares[n:] - squares[:-n] - (sums[n:] - sums[:-n]) ** 2 / n))
        corr = np.correlate(candidate, reference, "valid") / (spread * np.linalg.norm(reference))
        # Check the intended source clock, and tolerate small seek/codec differences.
        offsets = np.arange(len(corr)) / 8000 - pad
        if not np.any((np.abs(offsets) <= .06) & (corr >= .75)):
            raise quality.SubtitleError("El audio no coincide con la fuente local en el mismo instante. Conserva el SRT y revisa el montaje original.")
        checked += 1
    if not checked:
        raise quality.SubtitleError("No se pudo confirmar que la fuente limpia coincide con el audio del clip.")


def _rebuild(root, media, project, folder, draft, *, preview):
    duration, streams = quality.probe_media(media)
    meta = projects.read_json(media.with_suffix(".meta.json"))
    manifest = meta.get("subtitles") or {}
    source, start = clean_source(media, project, meta, duration)
    verify_alignment(media, source, start, duration)
    source_size = render_clips.ffprobe_dims(source)
    dimensions = streams["video"]["width"], streams["video"]["height"]
    options = _options_for_clip(media.parent.parent, manifest, project)
    aspect = next((name for name, (w, h, *_rest) in render_clips.ASPECTS.items()
                   if abs(dimensions[0] / dimensions[1] - w / h) < .015), None)
    if aspect is None:
        raise quality.SubtitleError("No se reconoce la relación de aspecto del MP4 original.")
    mark = watermark.Store(root).for_render()
    brand = branding.load_brand(options["brand"]) if options["brand"] else None
    words = [{**w, "start": w["start"] + start, "end": w["end"] + start} for w in draft["words"]]
    tag = uuid.uuid4().hex[:10]
    seg = {"id": "subtitulos-" + tag, "hook": meta.get("title", "clip").removesuffix(" #shorts"),
           "start": start, "end": start + duration, "overall": meta.get("score")}
    with tempfile.TemporaryDirectory(prefix="render-", dir=folder) as staging:
        staging = Path(staging)
        out = render_clips.render(
            seg, words, source, *source_size, staging, aspect, out_dir=staging,
            style=options["style"], brand=brand,
            caption={**(options["caption"] or {}), **draft["timing"], "words_per_cue": draft["group_size"]},
            mark=mark, fit=options["fit"], layout=options["layout"], centers=options["centers"],
            facecam_box=options["facecam_box"], facecam_height=options["facecam_height"],
            transcript_info={"model": draft["model"], "origin": "Faster-Whisper sobre el audio del MP4",
                             "transcript_version": 1, "transcript_hash": projects.digest(draft["words"]),
                             "repaired_from": media.name},
            preview=preview, timeout=RENDER_TIMEOUT, allow_model_download=False,
        )
        # The verified clean source supplies pixels. Reuse the final original
        # audio stream unchanged, including the original audio mix.
        remuxed = staging / "synced.mp4"
        subprocess.run(["ffmpeg", "-y", "-nostdin", "-v", "error", "-i", str(out), "-i", str(media),
                        "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-movflags", "+faststart", str(remuxed)],
                       check=True, capture_output=True, timeout=30)
        checked_duration, repaired = quality.probe_media(remuxed)
        if (abs(checked_duration - duration) > .12 or "audio" not in repaired
                or abs(float(repaired["video"]["duration"]) - float(repaired["audio"]["duration"])) > .18
                or abs(float(repaired["audio"]["start_time"]) - float(streams["audio"]["start_time"])) > .06):
            raise quality.SubtitleError("El MP4 corregido no superó la verificación de sincronización audiovisual.")
        if preview:
            name = f"preview-{tag}.mp4"
            remuxed.replace(folder / name)
            return {"preview": name}
        metadata = projects.read_json(out.with_suffix(".meta.json"))
        metadata["subtitles"]["media_hash"] = quality.artifact_hash(remuxed)
        metadata["subtitles"]["duration"] = checked_duration
        metadata["start"], metadata["end"] = meta["start"], meta["end"]
        for field in ("title", "description", "hashtags"):
            if field in meta:
                metadata[field] = meta[field]
        projects.atomic_json(out.with_suffix(".meta.json"), metadata)
        remuxed.replace(out)
        result = quality.diagnose(out)
        if result["errors"]:
            raise quality.SubtitleError("El render corregido no superó la validación. " + result["errors"][0])
        destination = folder if project.startswith("live/") else media.parent
        for suffix in (".srt", ".ass", ".meta.json", ".png"):
            out.with_suffix(suffix).replace((destination / out.name).with_suffix(suffix))
        out.replace(destination / out.name)
        return {"export": out.name, "in_repair_folder": destination == folder}


class Repair:
    def __init__(self, root, clip_path):
        self.root = root
        self.clip_path = clip_path
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="subtitle-repair")
        self.lock = threading.Lock()
        self.future = None
        self.active_id = None

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _folder(self, ident):
        if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{20}", ident):
            raise ValueError("Identificador de reparación no válido.")
        path = self.root / ".subtitle-repairs" / ident
        if path.resolve() != path:
            raise ValueError("Carpeta de reparación no válida.")
        return path

    def _latest_file(self, project, filename):
        base = self.root / ".subtitle-repairs"
        if base.resolve() != base:
            raise ValueError("Carpeta de reparación no válida.")
        return base / (projects.digest([project, filename]) + ".json")

    def latest(self, project, filename):
        self.clip_path(project, filename)
        try:
            pointer = json.loads(quality.local_text(self._latest_file(project, filename), 256))
            status = self.status(pointer["id"])
        except FileNotFoundError:
            return {"draft": None}
        return {"draft": status.get("draft")}

    def start(self, action, selection):
        project, filename = selection.get("project"), selection.get("filename")
        media = self.clip_path(project, filename)
        with self.lock:
            if self.future and not self.future.done():
                raise ValueError("Ya hay una reparación o un diagnóstico en curso.")
            model = selection.get("model")
            draft_id = selection.get("draft")
            timing = {**{k: branding.CAPTION_DEFAULTS[k] for k in captions.TIMING_FIELDS},
                      **branding.validate_caption(selection.get("timing", {}))}
            if set(timing) != set(captions.TIMING_FIELDS) or timing["cue_min_ms"] > timing["cue_max_ms"]:
                raise ValueError("Ajustes de tiempo de subtítulos inválidos.")
            if action == "transcribe" and (not isinstance(model, str) or model not in MODELS):
                raise ValueError("Selecciona un modelo local de Faster-Whisper.")
            draft = None
            if action in {"preview", "export"}:
                draft_path = self._folder(draft_id)
                draft = json.loads(quality.local_text(draft_path / "draft.json", quality.SIDECAR_LIMIT))
                if (draft["project"], draft["filename"]) != (project, filename):
                    raise quality.SubtitleError("El MP4 cambió desde la retranscripción. Crea un nuevo borrador.")
            ident = uuid.uuid4().hex[:20]
            folder = self._folder(ident)
            folder.mkdir(parents=True)
            status = {"id": ident, "state": "running", "action": action}
            projects.atomic_json(folder / "status.json", status)
            self.active_id = ident
            self.future = self.pool.submit(self._run, status, folder, media, project, model, timing, draft)
            return status

    def status(self, ident):
        result = json.loads(quality.local_text(self._folder(ident) / "status.json", quality.SIDECAR_LIMIT))
        with self.lock:
            if result["state"] == "running" and (self.active_id != ident or self.future.done()):
                result = json.loads(quality.local_text(self._folder(ident) / "status.json", quality.SIDECAR_LIMIT))
                if result["state"] == "running":
                    result.update(state="failed", error="Studio se cerró durante la reparación. Repite el último paso.")
        return result

    def asset(self, ident, filename):
        if not re.fullmatch(r"(draft\.srt|preview-[0-9a-f]{10}\.mp4|subtitulos-[^/\\]+\.mp4)", filename):
            raise ValueError("Archivo de reparación no válido.")
        path = self._folder(ident) / filename
        if not path.is_file() or path.resolve() != path:
            raise FileNotFoundError(path)
        return path

    def _run(self, status, folder, media, project, model, timing, draft):
        started = time.monotonic()
        action = status["action"]
        try:
            if action == "diagnose":
                status["diagnosis"] = quality.diagnose(media)
            elif action == "transcribe":
                duration, streams = quality.probe_media(media)
                if "audio" not in streams or duration > quality.MAX_AUDIO_SECONDS:
                    raise quality.SubtitleError("Para retranscribir se necesita una pista de audio y un clip de hasta diez minutos.")
                fingerprint = quality.artifact_hash(media)
                evidence = transcribe_final(media, folder / "whisper.json", model)
                if fingerprint != quality.artifact_hash(media):
                    raise quality.SubtitleError("El MP4 cambió durante la retranscripción.")
                metadata = media.with_suffix(".meta.json")
                manifest = (projects.read_json(metadata).get("subtitles") or {}) if metadata.is_file() else {}
                options = _options_for_clip(media.parent.parent, manifest, project)
                brand = branding.load_brand(options["brand"]) if options["brand"] else None
                style = render_clips.resolve_caption(options["style"], brand, options["caption"])
                group_size = style["words_per_cue"]
                groups = captions.plan_cues(evidence["words"], 0, duration, group_size, **timing)
                srt = captions.write_srt([], 0, duration, folder / "draft.srt", groups=groups)
                cues = quality.read_srt(srt)
                offset = max(0, float(streams["audio"].get("start_time") or 0))
                speech = quality.speech_ranges(media, duration, offset)
                ratio, voiced, longest = quality.coverage(cues, speech)
                if not cues or quality.speech_deficit(ratio, voiced, longest):
                    raise quality.SubtitleError("El borrador sigue teniendo voz sin transcribir. Prueba otro modelo local y revisa el audio.")
                draft = {**evidence, "duration": duration, "timing": timing, "group_size": group_size,
                         "media_hash": fingerprint, "project": project, "filename": media.name}
                projects.atomic_json(folder / "draft.json", draft)
                status["draft"] = {"id": status["id"], "model": evidence["model"], "segments": len(cues),
                                   "words": sum(len(g) for g in groups), "coverage": ratio,
                                   "text": "\n".join(f"{a:.2f}–{b:.2f} s  {text}" for a, b, text in cues[:150])}
            else:
                if draft["media_hash"] != quality.artifact_hash(media):
                    raise quality.SubtitleError("El MP4 cambió desde la retranscripción. Crea un nuevo borrador.")
                status.update(_rebuild(self.root, media, project, folder, draft, preview=action == "preview"))
            status["state"] = "done"
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, subprocess.SubprocessError) as exc:
            status.update(state="failed", error=str(exc) if isinstance(exc, quality.SubtitleError)
                          else "No se pudo completar la reparación local. Comprueba la fuente, modelo y FFmpeg.")
        status["elapsed"] = round(time.monotonic() - started, 3)
        projects.atomic_json(folder / "status.json", status)
        if status.get("draft"):
            projects.atomic_json(self._latest_file(project, media.name), {"id": status["id"]})


if __name__ == "__main__":
    try:
        _whisper(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
    except quality.SubtitleError as exc:
        print(exc)
        sys.exit(2)
