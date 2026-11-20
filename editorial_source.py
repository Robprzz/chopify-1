"""Select bounded, timestamped source material from a finished clip."""
import hashlib
import json
import math
import re


MAX_TEXT = 4800
MAX_EXCERPTS = MAX_TEXT // 165
TIME_LINE = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{3})")
# Exclude instruction-like passages rather than let them become copy or hashtags.
INSTRUCTIONS = re.compile(
    r"ignore\s+(?:all |the |any )?(?:previous|prior|above|system)|"
    r"ignor[ae]\s+(?:(?:todas?|las?|mis|tus)\s+)*(?:instrucciones|reglas)|"
    r"system\s+prompt|developer\s+message|reveal\s+(?:the |your )?prompt|"
    r"act\s+as\s+(?:an?\s+)?(?:assistant|admin|system)|"
    r"run\s+(?:this\s+)?(?:command|powershell)|ejecuta\s+(?:el\s+)?comando|"
    r"<\|[^|]+\|>|<\/?(?:system|assistant|developer)>", re.IGNORECASE,
)
SENSITIVE = re.compile(r"https?://\S+|\b(?:sk-[\w-]{12,}|Bearer\s+[\w.~-]+|"
                       r"(?:api[_ -]?key|password|secret|token)\s*[:=]\s*\S+)", re.I)


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _local_text(path, limit):
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("El material editorial debe estar en la carpeta del clip.")
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("El archivo de transcripción local supera el límite editorial.")
    return raw.decode("utf-8-sig")


def _seconds(parts):
    hours, minutes, seconds, millis = map(int, parts)
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def _subtitles(path):
    for block in re.split(r"\n\s*\n", _local_text(path, 512 * 1024).replace("\r", "")):
        lines = block.splitlines()
        for i, line in enumerate(lines[:2]):
            times = TIME_LINE.fullmatch(line.strip())
            if times:
                yield _seconds(times.groups()[:4]), _seconds(times.groups()[4:]), " ".join(lines[i + 1:])
                break


def _transcript(path, start, end):
    words = json.loads(_local_text(path, 16 * 1024 * 1024)).get("words", [])
    for word in words:
        if not isinstance(word, dict):
            continue
        left, right, text = word.get("start"), word.get("end"), word.get("word")
        if (type(left) in (int, float) and type(right) in (int, float)
                and isinstance(text, str) and start <= left < right and left < end):
            yield left - start, min(end, right) - start, text


def excerpts(cues, fingerprint=None):
    """Group adjacent phrases, then sample the whole clip under a strict budget."""
    segments = []
    text = ""
    start = end = 0.0
    rejected = 0
    for left, right, phrase in cues:
        phrase = " ".join(SENSITIVE.sub("", re.sub(r"<(?:/?[ibu]|/?font[^>]*)>", "", phrase)).split())
        if not phrase or not (0 <= left < right < 86400):
            continue
        if fingerprint is not None:
            fingerprint.update(json.dumps([left, right, phrase], ensure_ascii=False).encode())
        if INSTRUCTIONS.search(phrase):
            rejected += 1
            continue
        if text and (left - end > 1.5 or len(text) + len(phrase) > 165 or right - start > 12
                     or text.endswith((".", "!", "?"))):
            segments.append((start, end, text))
            text = ""
        if not text:
            start = left
        end = right
        text = (text + " " + phrase[:165]).strip()
    if text:
        segments.append((start, end, text))
    safe = [entry for entry in segments if not INSTRUCTIONS.search(entry[2])]
    rejected += len(segments) - len(safe)
    if not safe:
        raise ValueError("El clip no tiene diálogo verificable para analizar.")
    count = min(MAX_EXCERPTS, len(safe))
    picks = [safe[i * (len(safe) - 1) // max(1, count - 1)] for i in range(count)]
    return [{"start": round(left, 3), "end": round(right, 3), "text": phrase}
            for left, right, phrase in picks], rejected, len(safe) > count


def read_clip(media, project, filename):
    live = project.startswith("live/")
    base = media.parent.parent
    meta_path = media.with_suffix(".meta.json")
    meta = json.loads(_local_text(meta_path, 64 * 1024)) if meta_path.is_file() else {}
    if live:
        state = json.loads(_local_text(base / "live_session.json", 2 * 1024 * 1024))
        matches = [record for record in state["triggers"] if not record.get("error")
                   and record.get("files", {}).get("mp4", "").replace("\\", "/").rsplit("/", 1)[-1] == filename]
        if len(matches) != 1:
            raise ValueError("No se encontró un registro Live único para el clip.")
        score = matches[0].get("score")
    else:
        score = meta.get("score")
    if type(score) not in (int, float) or not 0 <= score <= 10:
        score = None
    srt = media.with_suffix(".srt")
    if srt.is_file() and srt.stat().st_size:
        cues = _subtitles(srt)
        origin = "srt"
    else:
        start, end = meta.get("start"), meta.get("end")
        if live or not all(type(v) in (int, float) and math.isfinite(v) for v in (start, end)) or not 0 <= start < end:
            raise ValueError("El clip no tiene subtítulos ni un intervalo de transcripción disponible.")
        cues = _transcript(base / "transcript.json", start, end)
        origin = "transcript"
    fingerprint = hashlib.sha256()
    entries, rejected, sampled = excerpts(cues, fingerprint)
    transcript_hash = fingerprint.hexdigest()
    return {"clip_id": f"{project}/{filename}", "project": project, "filename": filename,
            "original_score": score, "excerpts": entries, "transcript_id": f"{origin}:{transcript_hash}",
            "transcript_hash": transcript_hash, "excerpt_hash": key(entries),
            "filtered": rejected, "sampled": sampled}
