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
import smart_camera
import watermark
import signature
from export_settings import Store as TemplateStore


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
    record = next((record for record in state.get("triggers", []) if not record.get("error")
                   and Path(record.get("files", {}).get("mp4") or "").name == original), None)
    if record:
        return record
    ident = original.split("-", 1)[0]
    if re.fullmatch(r"[a-f0-9]{12}", ident):
        try:
            from subtitle_quality import local_text
            import json
            item = json.loads(local_text(media.parent.parent / "records" / (ident + ".json"), 16384))
            if original in finished_names({"triggers": [item]}):
                return item
        except (OSError, ValueError, TypeError, KeyError):
            pass
    return None


def live_exports(root):
    for manifest in sorted((root / "live").glob("*/live_session.json")):
        session_id = manifest.parent.name
        try:
            live_clips_dir(root, session_id)
            state = projects.read_json(manifest)
            clips = live_clips_dir(root, session_id)
            repaired = {p.name for p in clips.glob("auto-*-corregido.mp4")
                        if PRODUCTION_REPAIR.fullmatch(p.name) and finished_record(state, p)}
            archived = {path.name for path in clips.glob("*.mp4") if finished_record(state, path)}
            for name in sorted(finished_names(state) | archived | repaired):
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
    score: float | None
    signals: dict
    files: dict = field(default_factory=dict)
    error: str | None = None
    manual: bool = False


@dataclass
class Trigger:
    time: float
    score: float | None
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
    listing = destination.with_suffix(".concat.txt")
    escaped = [p.resolve().as_posix().replace("'", "'\\''") for p in paths]
    listing.write_text("".join(f"file '{path}'\n" for path in escaped), encoding="utf-8")
    cmd = ["ffmpeg", "-y", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe", "-f", "concat", "-safe", "0",
           "-i", str(listing), "-c", "copy", "-movflags", "+faststart",
           str(destination)]
    try:
        runner(cmd, timeout=25)
    finally:
        listing.unlink(missing_ok=True)
    return destination


class LiveClipWriter:
    def __init__(self, session_dir, root=None):
        self.session_dir = Path(session_dir)
        self.watermark = watermark.Store(root) if root is not None else None
        self.template = TemplateStore(root) if root is not None else None
        self.resources = nullcontext()
        self.clips_dir = self.session_dir / "clips"
        self.windows_dir = self.session_dir / "windows"
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self.windows_dir.mkdir(parents=True, exist_ok=True)

    def write(self, trigger, options, probe, words, segments, renderer=None, ranges=None, manual_window=None):
        """Render one clip; return a ClipRecord (with .error on failure)."""
        render_fn = renderer or render_clips.render
        start, end = manual_window or plan_window(
            trigger.time, options["live_buffer"], options["live_pre_roll"],
            options["live_tail"], options["min_len"], options["max_len"],
            max(right for _, right in ranges.values()) if ranges else trigger.time + options["live_tail"])
        if ranges:
            segments = [path for path in segments if ranges[path][0] < end and ranges[path][1] > start]
        offset = ranges[segments[0]][0] if ranges and segments else start
        start = max(start, offset)
        record = ClipRecord(uuid.uuid4().hex[:12], start, end, trigger.score, trigger.signals,
                            manual=manual_window is not None)
        window = self.windows_dir / f"{record.id}.mp4"
        try:
            if self.template:
                options, _, _ = self.template.resolve(options)
            frame_options = options.get("signature")
            frame = signature.load(frame_options["template"]) if frame_options else None
            use_mark = options.get("use_watermark", True) and (not frame or frame_options["watermark"])
            mark = self.watermark.for_render() if self.watermark and use_mark else None
            brand = branding.load_brand(options["brand"]) if options["brand"] else None
            window = stitch(segments, window)
            seg = {"id": record.id, "start": round(start - offset, 3), "end": round(end - offset, 3)}
            with self.resources:
                render_fn(seg, remap_words(words, offset, end), window,
                          probe["width"], probe["height"], str(self.session_dir),
                          options["aspect"], out_dir=str(self.clips_dir),
                          style=options["style"], do_tighten=options["tighten"] and manual_window is None,
                          loudnorm=options["loudnorm"], brand=brand,
                          layout=options["layout"], centers=options["centers"], camera_mode=options.get("camera_mode", "auto"),
                          caption=options["caption"], fit=options["fit"],
                          facecam_box=options["facecam_box"],
                          facecam_height=options["facecam_height"], mark=mark, retain_for_repair=True,
                          signature_frame=frame, export_profile=options.get("export_profile", "quality"),
                          timeout=max(60, min(600, (end - start) * 8 + 60)),
                          transcript_info={"model": LIVE_TRANSCRIPT_MODEL if words else None,
                                           "origin": "Transcripción Live"})
            record.files = {"window": str(window)}
            for path in sorted(self.clips_dir.glob(f"{record.id}-*")):
                record.files[path.suffix.lstrip(".") or "file"] = str(path)
        except smart_camera.MotionError as exc:
            record.error = str(exc)
        except Exception as exc:
            from live_capture import safe_stderr
            detail = safe_stderr(getattr(exc, "stderr", ""))
            status = getattr(exc, "returncode", None)
            message = str(exc) if isinstance(exc, (ValueError, OSError)) else ""
            record.error = f"{type(exc).__name__}" + (f" (código {status})" if status is not None else "")
            detail = detail or safe_stderr(message)
            if detail:
                record.error += f": {detail}"
            window.unlink(missing_ok=True)
        return record
