"""Turn a hype trigger into a finished chopify clip artifact set."""
from __future__ import annotations

from contextlib import nullcontext
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import branding
import ingest
from live_signals import LIVE_TRANSCRIPT_MODEL
import projects
import render_clips
import watermark


def live_clips_dir(root, session_id):
    if not re.fullmatch(r"[a-f0-9]{12}", session_id):
        raise ValueError("Invalid live session ID.")
    clips = Path(root).resolve() / "live" / session_id / "clips"
    if clips.resolve() != clips:
        raise ValueError("Invalid live clips folder.")
    return clips


def live_clip_path(root, session_id, filename):
    clips = live_clips_dir(root, session_id)
    path = clips / filename
    if (path.parent != clips or "/" in filename or "\\" in filename
            or path.suffix not in {".mp4", ".srt", ".png", ".json"} or path.resolve() != path):
        raise ValueError("Invalid live clip path.")
    return path


def finished_names(state):
    return {Path(record["files"]["mp4"]).name for record in state.get("triggers", [])
            if not record.get("error") and record.get("files", {}).get("mp4")}


PRODUCTION_REPAIR = re.compile(r"auto-\d+-\d+-\d+-corregido\.mp4")


def finished_record(state, media):
    original = media.name
    if PRODUCTION_REPAIR.fullmatch(original):
        try:
            manifest = media.with_suffix(".meta.json")
            if manifest.resolve() != manifest or manifest.stat().st_size > 16384:
                return None
            original = projects.read_json(manifest)["subtitles"]["repaired_from"]
        except (OSError, ValueError, TypeError, KeyError):
            return None
    return next((record for record in state.get("triggers", []) if not record.get("error")
                 and Path(record.get("files", {}).get("mp4") or "").name == original), None)


def live_exports(root):
    for manifest in sorted((root / "live").glob("*/live_session.json")):
        session_id = manifest.parent.name
        try:
            live_clips_dir(root, session_id)
            state = projects.read_json(manifest)
            clips = live_clips_dir(root, session_id)
            repaired = {p.name for p in clips.glob("auto-*-corregido.mp4")
                        if PRODUCTION_REPAIR.fullmatch(p.name) and finished_record(state, p)}
            for name in sorted(finished_names(state) | repaired):
                path = live_clip_path(root, session_id, name)
                if path.suffix == ".mp4" and path.is_file():
                    yield {"project": f"live/{session_id}", "project_name": f"Live · {session_id}",
                           "filename": name}
        except (OSError, TypeError, ValueError, KeyError, AttributeError):
            continue


@dataclass
class ClipRecord:
    id: str
    start: float
    end: float
    score: float
    signals: dict
    files: dict = field(default_factory=dict)
    error: str | None = None


@dataclass
class Trigger:
    time: float
    score: float
    signals: dict


def plan_window(trigger_time, buffer_seconds, pre_roll, tail,
                min_len, max_len, now):
    """Return (start, end) in stream seconds for a trigger."""
    now = min(float(now), float(trigger_time) + float(tail))
    end = min(float(trigger_time) + float(tail), now)
    start = max(0.0, float(trigger_time) - float(pre_roll))
    start = max(start, end - float(buffer_seconds))
    length = end - start
    if length < min_len:
        start = max(0.0, end - min_len)
    if length > max_len:
        start = end - max_len
    return round(start, 3), round(end, 3)


def remap_words(words, start, end):
    """Shift word timestamps into a window-relative timeline."""
    result = []
    for word in words or []:
        if not (start <= float(word.get("start", -1)) <= end):
            continue
        item = dict(word)
        item["start"] = float(word["start"]) - start
        item["end"] = min(end, float(word["end"])) - start
        result.append(item)
    return result


def stitch(paths, destination, runner=None):
    """Concatenate selected segments into one seekable local MP4."""
    runner = runner or ingest._run
    paths = [Path(p) for p in paths]
    if not paths:
        raise ValueError("No segments available for this clip window.")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    listing = destination.parent / "window_list.txt"
    listing.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in paths),
                       encoding="utf-8")
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
           "-i", str(listing), "-c", "copy", "-movflags", "+faststart",
           str(destination)]
    runner(cmd)
    return destination


class LiveClipWriter:
    def __init__(self, session_dir, root=None):
        self.session_dir = Path(session_dir)
        self.watermark = watermark.Store(root) if root is not None else None
        self.resources = nullcontext()
        self.clips_dir = self.session_dir / "clips"
        self.windows_dir = self.session_dir / "windows"
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self.windows_dir.mkdir(parents=True, exist_ok=True)

    def write(self, trigger, options, probe, words, segments, renderer=None, ranges=None):
        """Render one clip; return a ClipRecord (with .error on failure)."""
        render_fn = renderer or render_clips.render
        start, end = plan_window(
            trigger.time, options["live_buffer"], options["live_pre_roll"],
            options["live_tail"], options["min_len"], options["max_len"],
            max(right for _, right in ranges.values()) if ranges else trigger.time + options["live_tail"])
        if ranges:
            segments = [path for path in segments if ranges[path][0] < end and ranges[path][1] > start]
        offset = ranges[segments[0]][0] if ranges and segments else start
        start = max(start, offset)
        record = ClipRecord(uuid.uuid4().hex[:12], start, end, trigger.score, trigger.signals)
        try:
            mark = self.watermark.for_render() if self.watermark else None
            brand = branding.load_brand(options["brand"]) if options["brand"] else None
            window = stitch(segments, self.windows_dir / f"{record.id}.mp4")
            seg = {"id": record.id, "start": round(start - offset, 3), "end": round(end - offset, 3)}
            with self.resources:
                render_fn(seg, remap_words(words, offset, end), window,
                          probe["width"], probe["height"], str(self.session_dir),
                          options["aspect"], out_dir=str(self.clips_dir),
                          style=options["style"], do_tighten=options["tighten"],
                          loudnorm=options["loudnorm"], brand=brand,
                          layout=options["layout"], centers=options["centers"],
                          caption=options["caption"], fit=options["fit"],
                          facecam_box=options["facecam_box"],
                          facecam_height=options["facecam_height"], mark=mark, retain_for_repair=True,
                          transcript_info={"model": LIVE_TRANSCRIPT_MODEL if words else None,
                                           "origin": "Transcripción Live"})
            record.files = {"window": str(window)}
            for path in sorted(self.clips_dir.glob(f"{record.id}-*")):
                record.files[path.suffix.lstrip(".") or "file"] = str(path)
        except Exception as exc:
            record.error = f"{type(exc).__name__}: {exc}"
        return record
