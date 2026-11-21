"""Clip-relative subtitle validation and local speech-coverage diagnostics."""
import hashlib
import json
import math
import re
import subprocess
import time

from captions import cue_text, plan_cues


SRT_LINE = re.compile(r"(\d{2,}:\d\d:\d\d[,.]\d{3})\s*-->\s*(\d{2,}:\d\d:\d\d[,.]\d{3})")
SIDECAR_LIMIT = 2 * 1024 * 1024
MAX_AUDIO_SECONDS = 600
BOUNDARY_TOLERANCE = .05


class SubtitleError(ValueError):
    pass


def local_text(path, limit):
    if path.is_symlink() or path.resolve() != path:
        raise SubtitleError("El archivo de subtítulos debe estar dentro de la carpeta del clip.")
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise SubtitleError("El archivo de subtítulos supera el límite de diagnóstico.")
    return raw.decode("utf-8-sig")


def content_key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def artifact_hash(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def seconds(value):
    hours, minutes, sec = value.replace(",", ".").split(":")
    whole, fraction = sec.split(".")
    minutes, whole = int(minutes), int(whole)
    if not 0 <= minutes < 60 or not 0 <= whole < 60:
        raise SubtitleError("El subtítulo contiene un timestamp inválido.")
    return int(hours) * 3600 + minutes * 60 + whole + float("0." + fraction)


def read_srt(path):
    text = local_text(path, SIDECAR_LIMIT).replace("\r", "").strip()
    cues = []
    for n, block in enumerate(re.split(r"\n[ \t]*\n", text) if text else [], 1):
        lines = block.splitlines()
        i = int(lines[0].strip().isdigit())
        match = SRT_LINE.fullmatch(lines[i].strip()) if i < len(lines) else None
        if not match or not any(line.strip() for line in lines[i + 1:]):
            raise SubtitleError(f"Subtítulo {n} inválido: comprueba su texto y sus timestamps.")
        a, b = map(seconds, match.groups())
        cues.append((a, b, " ".join(lines[i + 1:])))
    return cues


def read_ass(path):
    events = []
    for line in local_text(path, SIDECAR_LIMIT).splitlines():
        if line.startswith("Dialogue:"):
            fields = line.split(",", 9)
            if len(fields) != 10:
                raise SubtitleError("El archivo ASS contiene un evento inválido.")
            events.append((seconds(fields[1]), seconds(fields[2]), fields[9]))
    return events


def timing_errors(cues, duration=None, limit=4, label="SRT"):
    errors = []
    previous_start = previous_end = 0.0
    for i, (left, right, _) in enumerate(cues, 1):
        prefix = f"{label} {i}"
        if not math.isfinite(left) or not math.isfinite(right) or left < 0 or right <= left:
            errors.append(f"{prefix}: timestamps negativos o duración inválida.")
            continue
        if left < previous_start:
            errors.append(f"{prefix}: orden cronológico incorrecto.")
        if left < previous_end - .011:
            errors.append(f"{prefix}: solapamiento de subtítulos.")
        if duration is not None and right > duration + BOUNDARY_TOLERANCE:
            errors.append(f"{prefix}: excede la duración del MP4.")
        if right - left > limit + BOUNDARY_TOLERANCE:
            errors.append(f"{prefix}: {right - left:.2f} s en pantalla, supera el límite de {limit:g} s.")
        previous_start, previous_end = left, max(right, previous_end)
        if len(errors) >= 20:
            break
    return errors


def load_track(media):
    base = media.parent.parent
    meta_path = media.with_suffix(".meta.json")
    meta = json.loads(local_text(meta_path, SIDECAR_LIMIT)) if meta_path.is_file() else {}
    manifest = meta.get("subtitles") or {}
    srt = media.with_suffix(".srt")
    transcript = None
    version = manifest.get("transcript_version")
    model = manifest.get("model")
    origin = manifest.get("origin") or "SRT de la exportación"
    warnings = []
    current = base / "transcript.json"
    # A snapshot hash identifies what the renderer used. A later project edit
    # does not retroactively change the transcript belonging to a finished MP4.
    if current.is_file() and (not manifest or manifest.get("origin") == "project"):
        try:
            transcript = json.loads(local_text(current, 16 * 1024 * 1024))
            if manifest.get("transcript_hash"):
                if content_key(transcript["words"]) != manifest["transcript_hash"]:
                    warnings.append("transcript.json cambió después de esta exportación.")
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            transcript = None
            warnings.append("No se pudo cotejar transcript.json con la exportación.")
    if srt.is_file() and srt.stat().st_size:
        cues = read_srt(srt)
        if not manifest and transcript:
            model = transcript.get("model")
            version = transcript.get("transcript_version")
            if model:
                warnings.append("Modelo del proyecto actual; la exportación antigua no identifica su modelo.")
    elif manifest:
        raise SubtitleError("Falta el SRT asociado al MP4. Regenera los subtítulos.")
    elif transcript:
        start, end = meta.get("start"), meta.get("end")
        if not all(type(x) in (int, float) and math.isfinite(x) for x in (start, end)) or not 0 <= start < end:
            raise SubtitleError("No se conoce el intervalo de transcripción de este clip.")
        groups = plan_cues(transcript["words"], start, end)
        cues = [(g[0]["left"] / 1000, g[-1]["right"] / 1000, cue_text(g, " ")) for g in groups]
        model = transcript.get("model")
        version = transcript.get("transcript_version")
        origin = "transcript.json del proyecto"
        warnings.append("Sin SRT exportado. Los tiempos se reconstruyeron desde el proyecto actual.")
    else:
        raise SubtitleError("El clip no tiene subtítulos ni una transcripción asociada.")
    if not cues:
        raise SubtitleError("El clip no tiene subtítulos con diálogo.")
    return cues, manifest, {
        "model": model, "origin": origin, "version": version, "timeline_version": manifest.get("version"),
        "segments": len(cues), "words": sum(len(s.split()) for _, _, s in cues),
        "max_cue_seconds": round(max(b - a for a, b, _ in cues), 3),
        "warnings": warnings,
    }


def max_cue(manifest):
    # Older SRTs were grouped by word count. Treat long but otherwise
    # reasonable legacy cues as readability warnings, not malformed evidence.
    return (manifest.get("timing") or {}).get("cue_max_ms", 4000 if manifest else 8000) / 1000


def probe_media(media):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,duration,start_time,width,height:format=duration", "-of", "json", str(media)],
        capture_output=True, timeout=12, check=True,
    )
    data = json.loads(result.stdout)
    duration = float(data["format"]["duration"])
    streams = {stream["codec_type"]: stream for stream in reversed(data.get("streams", []))}
    if not math.isfinite(duration) or duration <= 0 or "video" not in streams:
        raise SubtitleError("No se pudo verificar la duración ni la pista de video del MP4.")
    return duration, streams


def speech_ranges(media, duration, offset=0):
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    import numpy as np

    if duration > MAX_AUDIO_SECONDS:
        raise SubtitleError("La comprobación de voz admite clips de hasta diez minutos.")
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(media), "-map", "0:a:0",
         "-t", str(MAX_AUDIO_SECONDS), "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
        capture_output=True, check=True, timeout=30,
    )
    audio = np.frombuffer(result.stdout, dtype="<i2").astype("float32") / 32768.0
    if not audio.size:
        return []
    speech = get_speech_timestamps(audio, VadOptions(min_speech_duration_ms=200,
                                                      min_silence_duration_ms=350, speech_pad_ms=80))
    return [(r["start"] / 16000 + offset, min(duration, r["end"] / 16000 + offset))
            for r in speech if r["start"] / 16000 + offset < duration]


def coverage(cues, speech):
    """Compare detected voice intervals with cue intervals, ignoring real silences."""
    voiced = sum(b - a for a, b in speech)
    if not voiced:
        return None, 0.0, 0.0
    spans = []
    for a, b, _ in sorted(cues):
        if spans and a <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], b))
        else:
            spans.append((a, b))
    index = 0
    covered = longest = 0.0
    for start, end in speech:
        left = start
        while index < len(spans) and spans[index][1] <= left:
            index += 1
        while index < len(spans) and spans[index][0] < end:
            a, b = spans[index]
            longest = max(longest, a - left)
            covered += max(0, min(end, b) - max(left, a))
            left = max(left, b)
            if b >= end:
                break
            index += 1
        longest = max(longest, end - left)
    return round(covered / voiced, 4), voiced, longest


def speech_deficit(ratio, voiced, longest):
    return ratio is not None and (longest >= 1.5 or (voiced >= 2 and ratio < .65 and voiced * (1 - ratio) >= 1))


def diagnose(media):
    started = time.monotonic()
    out = dict(model=None, origin=None, version=None, timeline_version=None, duration=None, segments=0, words=0,
               max_cue_seconds=None, speech_coverage=None, voiced_seconds=None, errors=[], warnings=[])
    errors = out["errors"]
    try:
        duration, streams = probe_media(media)
        out["duration"] = round(duration, 3)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        duration, streams = None, {}
        errors.append("No se pudo leer el MP4 con FFprobe.")
    try:
        cues, manifest, summary = load_track(media)
        out.update(summary)
        errors.extend(timing_errors(cues, duration, max_cue(manifest)))
        if manifest:
            for field, path in (("srt_hash", media.with_suffix(".srt")),
                                ("ass_hash", media.with_suffix(".ass")), ("media_hash", media)):
                if path.is_symlink() or not path.is_file() or artifact_hash(path) != manifest.get(field):
                    errors.append(f"{path.suffix.upper()[1:]} no coincide con la versión de la exportación.")
            ass = media.with_suffix(".ass")
            if ass.is_file() and not ass.is_symlink():
                events = read_ass(ass)
                errors.extend(timing_errors(events, duration, max_cue(manifest), "ASS"))
                index = matched = 0
                for a, b, _ in events:
                    # Each ASS karaoke event must fit in one SRT cue.
                    while index < len(cues) and a >= cues[index][1] - .005:
                        index += 1
                    if index == len(cues) or a < cues[index][0] - .011 or b > cues[index][1] + .011:
                        errors.append("ASS y SRT no coinciden en sus intervalos.")
                        break
                    if index == matched:
                        matched += 1
                if matched != len(cues):
                    errors.append("El ASS no representa todos los subtítulos del SRT.")
            if duration is not None and abs(duration - manifest["duration"]) > .2:
                errors.append("El MP4 y la línea de tiempo exportada tienen duraciones diferentes.")
        else:
            out["warnings"].append("Exportación anterior a la verificación de versiones SRT, ASS y MP4.")
            if out["max_cue_seconds"] > 4:
                out["warnings"].append("Hay subtítulos por encima de la duración recomendada de 4 s.")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        cues = []
        errors.append(str(exc) if isinstance(exc, SubtitleError) else "No se pudo leer la pista de subtítulos local.")
    if duration is not None and "audio" not in streams:
        errors.append("El MP4 no contiene pista de audio para verificar el diálogo.")
    elif duration is not None and cues and not errors:
        try:
            offset = max(0.0, float(streams["audio"].get("start_time") or 0))
            speech = speech_ranges(media, duration, offset)
            ratio, voiced, longest = coverage(cues, speech)
            out.update(speech_coverage=ratio, voiced_seconds=round(voiced, 3))
            if ratio is None:
                out["warnings"].append("El detector local no confirmó voz. Compara el audio y los subtítulos en la vista previa.")
            elif speech_deficit(ratio, voiced, longest):
                errors.append(f"Actividad de voz sin subtítulos: {ratio:.0%} de cobertura, hasta {longest:.2f} s seguidos sin texto.")
        except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError):
            out["warnings"].append("No se pudo medir la actividad de voz local.")
    elif cues and errors:
        out["warnings"].append("Corrige los tiempos antes de medir la cobertura de voz.")
    out["state"] = "blocked" if errors else "warning" if out["warnings"] else "valid"
    out["elapsed"] = round(time.monotonic() - started, 3)
    return out
