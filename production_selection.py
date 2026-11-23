"""Local evidence and deterministic selection for clips finished by Studio."""
from datetime import datetime, timedelta
from difflib import SequenceMatcher
import hashlib
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from editorial_evidence import fold
import projects
from score_clips import overlap_ratio
import subtitle_quality as quality


def score(value):
    return float(value) if type(value) in (int, float) and 0 <= value <= 10 else None


def identity(media, project):
    base = media.parent.parent
    try:
        if project.startswith("live/"):
            from live_clips import finished_record
            data = json.loads(quality.local_text(base / "live_session.json", 2 * 1024 * 1024))
            meta = finished_record(data, media) or {}
            source = project  # Live clocks are relative to a session.
        else:
            meta = json.loads(quality.local_text(media.with_suffix(".meta.json"), quality.SIDECAR_LIMIT))
            data = json.loads(quality.local_text(base / "project.json", 2 * 1024 * 1024))
            source = data.get("cache", {}).get("source")
            if not source and data.get("source"):
                original = Path(data["source"])
                if original.is_absolute() and original.is_file():
                    source = projects.digest(projects.file_identity(original))
                elif data["source"].startswith(("https://", "http://")):
                    source = projects.digest(data["source"])
        start, end = meta.get("start"), meta.get("end")
        interval = ({"start": start, "end": end} if all(
            type(n) in (float, int) and math.isfinite(n) for n in (start, end)) and 0 <= start < end else None)
        return score(meta.get("score")), source or project, interval
    except (OSError, ValueError, TypeError, AttributeError):
        return None, project, None


def stamp(media):
    files = []
    for path in (media, *(media.with_suffix(s) for s in (".srt", ".ass", ".meta.json"))):
        try:
            if path.is_symlink():
                return None
            st = path.stat()
        except FileNotFoundError:
            if path == media:
                return None
            files.append(None)
        else:
            files.append([st.st_size, st.st_mtime_ns, st.st_ctime_ns])
    return files


def evidence(media, original, source, interval, report, cancellation):
    def digest(path):
        result = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                cancellation.check()
                result.update(block)
        return result.hexdigest()

    cues, _, _ = quality.load_track(media)
    tokens = fold(" ".join(text for _, _, text in cues)).split()
    return {"sha": [digest(original), digest(media)] if original != media else [digest(media)],
            "source": source, "interval": interval, "tokens": tokens if len(tokens) <= 3200 else [],
            "duration": report.get("duration"), "file_stamp": stamp(media), "original_stamp": stamp(original)}


def shingles(tokens):
    return set(zip(tokens, tokens[1:], tokens[2:], tokens[3:]))


def text_similarity(first, second):
    a, b = shingles(first), shingles(second)
    return len(a & b) / len(a | b) if len(a) >= 12 and len(b) >= 12 else 0


def duplicate(candidate, other):
    common = set(candidate["sha"]) & set(other["sha"])
    if common:
        return {"method": "sha256", "sha256": min(common)}
    if candidate["source"] == other["source"] and candidate["interval"] and other["interval"]:
        overlap = overlap_ratio(candidate["interval"], other["interval"])
        if overlap > .55:
            return {"method": "overlap", "ratio": round(overlap, 4),
                    "intervals": [candidate["interval"], other["interval"]]}
    first, second = candidate["tokens"], other["tokens"]
    duration, other_duration = candidate.get("duration"), other.get("duration")
    if min(len(first), len(second)) < 24 or not duration or not other_duration:
        return None
    if min(duration, other_duration) / max(duration, other_duration) < .8:
        return None
    if text_similarity(first, second) < .86:
        return None
    # Only hesitation differences may be ignored. Names, negation and
    # quantities must agree even when the rest of the dialogue matches.
    fillers = {"eh", "mmm", "uh", "um"}
    if [w for w in first if w not in fillers] != [w for w in second if w not in fillers]:
        return None
    similarity = SequenceMatcher(None, first, second, autojunk=False).ratio()
    if similarity >= .96:
        return {"method": "transcript", "ratio": round(similarity, 4), "words": [len(first), len(second)]}
    return None


def priority(detection, editorial, context, diversity):
    return round(.45 * detection + .35 * editorial + .10 * context + .10 * diversity, 3)


def local_day(now, timezone):
    instant = datetime.fromtimestamp(now, ZoneInfo(timezone))
    start = instant.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.date().isoformat(), start.timestamp(), (start + timedelta(days=1)).timestamp()
