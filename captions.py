"""Validate editable transcripts and export clip-relative SubRip captions."""
import copy
import math
from pathlib import Path

from score_clips import sentenceize


def update_transcript(data, words):
    if not isinstance(words, list) or not words:
        raise ValueError("Provide at least one timed word.")
    duration = float(data["duration"])
    clean = []
    previous = 0.0
    for item in words:
        text = str(item.get("word", "")).strip()
        start, end = float(item["start"]), float(item["end"])
        if (not text or len(text) > 500 or "\n" in text or "\r" in text
                or not math.isfinite(start) or not math.isfinite(end)
                or start < previous - 0.001 or end <= start or end > duration + 0.001):
            raise ValueError("Words must have text and ordered, non-overlapping times within the video.")
        entry = {"word": text, "start": start, "end": end}
        for flag in ("hidden", "emphasis"):
            if flag in item:
                if not isinstance(item[flag], bool):
                    raise ValueError("Word flags must be true or false.")
                if item[flag]:
                    entry[flag] = True
        clean.append(entry)
        previous = end
    result = copy.deepcopy(data)
    result["words"] = clean
    result["segments"] = [{k: s[k] for k in ("start", "end", "text")}
                          for s in sentenceize(clean)]
    return result


def srt_time(seconds):
    ms = max(0, round(seconds * 1000))
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    seconds, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"


TIMELINE_VERSION = 2
TIMING_FIELDS = ("cue_min_ms", "cue_max_ms", "cue_gap_ms", "cue_lines")


def plan_cues(words, start, end, group_size=8, *, cue_min_ms=500,
              cue_max_ms=4000, cue_gap_ms=650, cue_lines=2):
    """Group clip-relative timed words without bridging pauses or dropping text."""
    if type(group_size) is not int or not 1 <= group_size <= 100:
        raise ValueError("Caption group size must be an integer from 1 to 100.")
    if cue_max_ms < cue_min_ms:
        raise ValueError("La duración máxima del subtítulo debe alcanzar la mínima.")
    groups = []
    group = []
    previous = 0
    line = length = 0
    for word in words:
        if word.get("hidden"):
            continue
        a, b = word["start"], word["end"]
        if not (math.isfinite(a) and math.isfinite(b) and a >= 0 and b >= a):
            raise ValueError("La transcripción contiene tiempos de palabra inválidos.")
        if (b <= start and a < start) or a >= end:
            continue
        left, right = round((max(a, start) - start) * 1000), round((min(b, end) - start) * 1000)
        if left < previous - 120 or (b - a) * 1000 > cue_max_ms:
            raise ValueError("La transcripción contiene palabras superpuestas o demasiado largas. Revisa sus tiempos.")
        left = max(left, previous)
        if left >= right:
            # Whisper can assign several tokens the same edge. Keep those
            # tokens attached to the adjacent timed word, without making up time.
            if group and left - previous <= 120:
                group[-1]["word"] += " " + word["word"].strip()
                continue
            right = min(round((end - start) * 1000), left + 10)
            if left >= right:
                raise ValueError("No se pudo alinear una palabra dentro del clip.")
        token = word["word"].strip()
        if not token:
            continue
        width = max(1, sum(2 if ord(c) >= 0x2e80 else 1 for c in token))
        wraps = length and length + 1 + width > 30
        if group and (len(group) >= group_size or left - previous >= cue_gap_ms
                      or right - group[0]["left"] > cue_max_ms
                      or (wraps and line + 1 >= cue_lines)
                      or (previous - group[0]["left"] >= cue_min_ms
                          and group[-1]["word"].endswith((".", "!", "?", "…")))):
            groups.append(group)
            group, line, length, wraps = [], 0, 0, False
        if wraps:
            line += 1
            length = 0
        group.append({**word, "word": token, "left": left, "right": right, "line": line})
        previous = right
        length += width + bool(length)
    if group:
        groups.append(group)
    return groups


def cue_text(group, separator="\n"):
    return "".join(("" if i == 0 else separator if w["line"] != group[i - 1]["line"] else " ")
                   + w["word"] for i, w in enumerate(group))


def write_srt(words, start, end, path, group_size=8, *, groups=None, **timing):
    if groups is None:
        groups = plan_cues(words, start, end, group_size, **timing)
    blocks = []
    for group in groups:
        a, b = group[0]["left"] / 1000, group[-1]["right"] / 1000
        blocks.append(f"{len(blocks) + 1}\n{srt_time(a)} --> {srt_time(b)}\n{cue_text(group)}\n")
    Path(path).write_text("\n".join(blocks), encoding="utf-8")
    return Path(path)
