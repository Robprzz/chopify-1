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
        from live_clips import finished_record
        record = finished_record(state, media)
        if record is None:
            raise ValueError("No se encontró un registro Live para el clip.")
        score = record.get("score")
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


def safe_context(value, limit):
    if not isinstance(value, str) or INSTRUCTIONS.search(value) or re.search(r"[\ud800-\udfff]", value):
        return ""
    return " ".join(SENSITIVE.sub("", value).split())[:limit]


def semantic_context(media, project, source):
    cues, _, _ = subtitle_quality.load_track(media)
    clips = []
    total = sum(len(phrase) for _, _, phrase in cues)
    safe_chars = covered = 0
    for left, right, phrase in cues:
        if INSTRUCTIONS.search(phrase):
            continue
        speaker = re.match(r"^\s*\[([^\[\]\n]{1,60})\]\s+", phrase)
        label = safe_context(speaker[1], 60) if speaker else ""
        dialogue = phrase[speaker.end():] if speaker else phrase
        dialogue = re.sub(r"<(?:/?[ibu]|/?font[^>]*)>", "", dialogue)
        words = " ".join(SENSITIVE.sub("", dialogue).split())
        if not words:
            continue
        safe_chars += len(words)
        covered += 1
        # Split oversized cues on whitespace, or on characters for scripts without spaces.
        pieces = []
        while len(words) > 480:
            boundary = words.rfind(" ", 0, 481)
            cut = boundary if boundary > 0 else 480
            pieces.append(words[:cut])
            words = words[cut:].lstrip()
        pieces.append(words)
        for text in pieces:
            row = {"start": left, "end": right, "text": text}
            if label:
                row["speaker"] = label
            if (clips and -.01 <= left - clips[-1]["end"] <= .65 and clips[-1].get("speaker") == row.get("speaker")
                    and len(clips[-1]["text"]) + len(text) < 465):
                clips[-1]["end"] = right
                clips[-1]["text"] += " " + text
            else:
                clips.append(row)
    evidence = [row for row in clips if not INSTRUCTIONS.search(row["text"])]
    outside = {}
    language = "und"
    base = media.parent.parent
    if not project.startswith("live/"):
        try:
            project_data = json.loads(subtitle_quality.local_text(base / "project.json", 2 * 1024 * 1024))
            meta = project_data.get("source_meta") or {}
            for key, limit in (("title", 160), ("description", 450), ("channel", 120)):
                value = safe_context(meta.get(key), limit)
                if value:
                    outside[key] = value
            transcript = json.loads(subtitle_quality.local_text(base / "transcript.json", 16 * 1024 * 1024))
            lang = transcript.get("language")
            if isinstance(lang, str) and re.fullmatch(r"[a-z]{2,3}", lang):
                language = lang
            clip_meta = json.loads(subtitle_quality.local_text(media.with_suffix(".meta.json"), 16384))
            start, end = clip_meta["start"], clip_meta["end"]
            for label, low, high in (("before", max(0, start - 35), start), ("after", end, end + 35)):
                snippet = " ".join(w["word"] for w in transcript["words"]
                                   if low <= w["start"] < high and not w.get("hidden"))
                value = safe_context(snippet, 390)
                if value:
                    outside[label] = value
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
    return {**source, "semantic_evidence": evidence, "outside_clip": outside, "language_hint": language,
            "transcript_coverage": {"cues": len(cues), "safe_cues": covered, "source_chars": total,
                                    "safe_chars": safe_chars, "passages": len(evidence),
                                    "filtered_cues": len(cues) - covered, "filtered_passages": len(clips) - len(evidence)}}
