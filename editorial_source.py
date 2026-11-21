"""Select bounded, timestamped source material from a finished clip."""
import hashlib
import json
import re

import subtitle_quality


MAX_TEXT = 4800
MAX_EXCERPTS = MAX_TEXT // 165
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


key = subtitle_quality.content_key


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
    meta = json.loads(subtitle_quality.local_text(meta_path, subtitle_quality.SIDECAR_LIMIT)) if meta_path.is_file() else {}
    if live:
        state = json.loads(subtitle_quality.local_text(base / "live_session.json", 2 * 1024 * 1024))
        matches = [record for record in state["triggers"] if not record.get("error")
                   and record.get("files", {}).get("mp4", "").replace("\\", "/").rsplit("/", 1)[-1] == filename]
        if len(matches) != 1:
            raise ValueError("No se encontró un registro Live único para el clip.")
        score = matches[0].get("score")
    else:
        score = meta.get("score")
    if type(score) not in (int, float) or not 0 <= score <= 10:
        score = None
    cues, manifest, summary = subtitle_quality.load_track(media)
    errors = subtitle_quality.timing_errors(cues, limit=subtitle_quality.max_cue(manifest))
    if errors:
        raise subtitle_quality.SubtitleError("Subtítulos bloqueados. " + errors[0] + " Abre Calidad de subtítulos para repararlos.")
    if manifest and subtitle_quality.artifact_hash(media.with_suffix(".srt")) != manifest.get("srt_hash"):
        raise subtitle_quality.SubtitleError("El SRT cambió desde la exportación. Abre Calidad de subtítulos.")
    stats = media.stat()
    media_version = [stats.st_size, stats.st_mtime_ns]
    if manifest:
        ass = media.with_suffix(".ass")
        if ass.is_symlink() or not ass.is_file():
            raise subtitle_quality.SubtitleError("Falta el ASS de esta exportación. Abre Calidad de subtítulos.")
        media_version.append(subtitle_quality.artifact_hash(ass))
    fingerprint = hashlib.sha256()
    entries, rejected, sampled = excerpts(cues, fingerprint)
    transcript_hash = fingerprint.hexdigest()
    return {"clip_id": f"{project}/{filename}", "project": project, "filename": filename,
            "original_score": score, "excerpts": entries, "transcript_id": f"{summary['origin']}:{transcript_hash}",
            "transcript_hash": transcript_hash, "excerpt_hash": key(entries),
            "filtered": rejected, "sampled": sampled, "media_version": media_version}
