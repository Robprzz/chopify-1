"""
Stage 1 of the viral-clip pipeline.

Downloads a YouTube video at highest quality with yt-dlp, then transcribes it
locally with faster-whisper (word-level timestamps). Writes work/transcript.json.

No external/paid APIs. CPU-friendly (int8). Usage:
    python download_and_transcribe.py <youtube_url> --model small
"""
import sys
import json
import studio_json
import project_cancel
import argparse
import tempfile
import shutil
from urllib.parse import urlparse
from pathlib import Path
from unicode_io import configure_utf8, utf8_environment
from whisper_runtime import Session

TRANSCRIPT_VERSION = 2
CHUNK_SECONDS = 1800
OVERLAP_SECONDS = 30


def run(cmd):
    print(">", " ".join(str(c) for c in cmd), flush=True)
    project_cancel.run(cmd, env=utf8_environment(), check=True)


def resolve_source(source, workdir, *, public_only=False):
    """Keep local files in place; authorized Instagram imports use projects."""
    from instagram_import import candidate
    if candidate(str(source)):
        raise ValueError("Usa la importación autorizada de Instagram desde Studio.")
    path = Path(source).expanduser()
    if path.is_file():
        if path.suffix.lower() not in {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}:
            raise ValueError("Use a video file: MP4, MKV, WebM, MOV, M4V or AVI.")
        return path.resolve()
    if urlparse(source).scheme not in {"http", "https"}:
        raise ValueError(f"Video file does not exist or URL is invalid: {source}")
    return download(source, Path(workdir), public_only=True) if public_only else download(source, Path(workdir))


def download(url, workdir, *, public_only=False):
    workdir.mkdir(parents=True, exist_ok=True)
    # A fresh directory prevents a changed URL from reusing an old source.mp4.
    with tempfile.TemporaryDirectory(prefix="download-", dir=workdir) as temp:
        video = _download(url, Path(temp), public_only=True) if public_only else _download(url, Path(temp))
        destination = workdir / video.name
        shutil.move(str(video), str(destination))
        return destination.resolve()


def _download(url, workdir, *, public_only=False):
    out_tmpl = workdir / "source.%(ext)s"
    # Highest quality: best video + best audio, merged to mp4 (needs ffmpeg on PATH).
    from ingest import PUBLIC_YOUTUBE
    cmd = [
        sys.executable, "-m", "yt_dlp", *(PUBLIC_YOUTUBE if public_only else ()),
        "-f", "bestvideo*+bestaudio/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "-o", str(out_tmpl),
        url,
    ]
    run(cmd)
    vids = [p for p in sorted(workdir.glob("source.*"))
            if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".m4v")]
    if not vids:
        raise SystemExit("Download failed: no video file produced.")
    return vids[0].resolve()


def transcribe(video, workdir, model_size, device, compute_type, duration=None,
               chunked=False, source=None, media_kind="video",
               render_from_sections=False, identity=None, progress=None,
               resume=True):
    """Transcribe a short source in one pass or a long source in chunks."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    if chunked:
        return transcribe_chunked(
            video, workdir, model_size, device, compute_type, duration,
            source=source, media_kind=media_kind,
            render_from_sections=render_from_sections, identity=identity,
            progress=progress, resume=resume)

    runtime = Session(model_size, device, compute_type, _load_model)
    (seg_list, words, language, info_duration), metrics = runtime.run(lambda model: _transcribe_media(model, video))
    total = float(duration) if duration is not None else float(info_duration)
    transcript = {
        "video": str(video),
        "language": language,
        "duration": total,
        "model": model_size,
        "whisper_runtime": metrics,
        "segments": seg_list,
        "words": words,
        "transcript_version": TRANSCRIPT_VERSION,
        "chunked": False,
        "chunks": 0,
        "media_kind": media_kind,
        "render_from_sections": bool(render_from_sections),
        "source": str(source or video),
        "source_duration": total,
        "source_identity": str(identity or ""),
    }
    out = workdir / "transcript.json"
    _atomic_json(out, transcript)
    print(f"Wrote {out}  ({len(words)} words, {len(seg_list)} segments)", flush=True)
    return out


_atomic_json = studio_json.write

def _load_model(model_size, device, compute_type):
    project_cancel.check()
    from faster_whisper import WhisperModel
    print(f"Loading faster-whisper model={model_size} device={device} "
          f"compute={compute_type} (first run downloads the model)...", flush=True)
    return WhisperModel(model_size, device=device, compute_type=compute_type)


def _transcribe_media(model, media, offset=0.0):
    project_cancel.check()
    segments, info = model.transcribe(
        str(media), word_timestamps=True, vad_filter=True)
    language = getattr(info, "language", "")
    seg_list, words = [], []
    iterator = iter(segments)
    while True:
        project_cancel.check()
        seg = next(iterator, None)
        if seg is None:
            break
        project_cancel.check()
        start = float(seg.start) + offset
        end = float(seg.end) + offset
        seg_list.append({"start": start, "end": end, "text": seg.text})
        for w in (seg.words or []):
            words.append({
                "word": w.word,
                "start": float(w.start) + offset,
                "end": float(w.end) + offset,
            })
    project_cancel.check()
    info_duration = float(getattr(info, "duration", 0.0) or 0.0)
    print(f"Detected language: {language} "
          f"(p={getattr(info, 'language_probability', 0.0):.2f}), audio {info_duration:.1f}s", flush=True)
    for seg in seg_list:
        print(f"[{seg['start']:7.2f} -> {seg['end']:7.2f}] {seg['text']}", flush=True)
    return seg_list, words, language, info_duration


def _probe_duration(path):
    result = project_cancel.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, encoding="utf-8", errors="backslashreplace", check=True)
    return float(result.stdout.strip())


def _extract_chunk(source, start, end, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    project_cancel.run([
        "ffmpeg", "-v", "error", "-y",
        "-ss", f"{float(start):.3f}", "-t", f"{float(end) - float(start):.3f}",
        "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(path)], check=True)
    return path


def _part_matches(part, index, start, end, identity):
    if not isinstance(part, dict):
        return False
    if part.get("version") != TRANSCRIPT_VERSION:
        return False
    if part.get("index") != index:
        return False
    if str(part.get("identity") or "") != str(identity or ""):
        return False
    try:
        if abs(float(part.get("start")) - float(start)) > 1e-3:
            return False
        if abs(float(part.get("end")) - float(end)) > 1e-3:
            return False
    except (TypeError, ValueError):
        return False
    words = part.get("words") or []
    starts = [float(word.get("start", 0.0)) for word in words]
    return all(a <= b for a, b in zip(starts, starts[1:]))


def _read_part(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def merge_transcript_parts(parts, duration, overlap=OVERLAP_SECONDS):
    """Merge overlapping chunk transcripts into one global timeline."""
    ordered = sorted(parts, key=lambda part: int(part.get("index", 0)))
    last = len(ordered) - 1
    merged_words, merged_segments = [], []
    language = ""
    for position, part in enumerate(ordered):
        start = float(part.get("start") or 0.0)
        end = float(part.get("end") or 0.0)
        lower = start + float(overlap) / 2.0 if position > 0 else float("-inf")
        upper = end - float(overlap) / 2.0 if position < last else float("inf")
        merged_words.extend(
            word for word in (part.get("words") or [])
            if lower <= _midpoint(word) < upper)
        merged_segments.extend(
            seg for seg in (part.get("segments") or [])
            if lower <= _midpoint(seg) < upper)
        language = language or str(part.get("language") or "")

    words = _dedupe(sorted(merged_words, key=_start), "word")
    segments = _dedupe(sorted(merged_segments, key=_start), "text")
    return {"words": words, "segments": segments, "language": language,
            "chunks": len(ordered), "duration": float(duration)}


def _midpoint(item):
    return (float(item.get("start", 0.0)) + float(item.get("end", 0.0))) / 2.0


def _start(item):
    return float(item.get("start", 0.0))


def _dedupe(items, key):
    seen = set()
    out = []
    for item in items:
        marker = (round(_start(item), 3), str(item.get(key) or ""))
        if marker in seen:
            continue
        seen.add(marker)
        out.append(item)
    return out


def transcribe_chunked(video, workdir, model_size, device, compute_type,
                       duration=None, source=None, media_kind="video",
                       render_from_sections=False, identity=None,
                       progress=None, resume=True):
    """Transcribe a long source in 30-minute parts with overlap."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    source_path = Path(video)
    total = float(duration) if duration is not None else _probe_duration(source_path)
    if total <= 0:
        raise ValueError("Cannot transcribe an empty source.")
    identity = str(identity or "")

    bounds = []
    index = 0
    while index * CHUNK_SECONDS < total:
        start = index * CHUNK_SECONDS
        owned_end = min(total, start + CHUNK_SECONDS)
        end = min(total, owned_end + OVERLAP_SECONDS)
        bounds.append((index, float(start), float(end)))
        index += 1

    runtime = Session(model_size, device, compute_type, _load_model)
    parts = []
    part_dir = workdir / "audio_parts"
    part_dir.mkdir(parents=True, exist_ok=True)
    for done, (index, start, end) in enumerate(bounds, 1):
        project_cancel.check()
        part_path = workdir / (f"transcript.part{index:03d}.json")
        part = _read_part(part_path) if resume else None
        if not _part_matches(part, index, start, end, identity):
            wav = part_dir / f"part{index:03d}.wav"
            try:
                _extract_chunk(source_path, start, end, wav)
                (seg_list, words, language, _), metrics = runtime.run(
                    lambda model: _transcribe_media(model, wav, offset=start))
                part = {
                    "version": TRANSCRIPT_VERSION,
                    "index": index,
                    "start": start,
                    "end": end,
                    "identity": identity,
                    "whisper_runtime": metrics,
                    "language": language,
                    "segments": seg_list,
                    "words": words,
                }
                _atomic_json(part_path, part)
            finally:
                try:
                    wav.unlink(missing_ok=True)
                except OSError:
                    pass
        parts.append(part)
        if progress:
            progress(done, len(bounds))

    merged = merge_transcript_parts(parts, total, overlap=OVERLAP_SECONDS)
    completed = [part.get("whisper_runtime") or {} for part in parts]
    devices = sorted({item.get("device", "sin confirmar") for item in completed})
    compute = sorted({item.get("compute_type", "sin confirmar") for item in completed})
    timings = [item["elapsed_seconds"] for item in completed if "elapsed_seconds" in item]
    transcript = {
        "video": str(video),
        "language": merged["language"],
        "duration": total,
        "model": model_size,
        "whisper_runtime": {"requested": device, "model": model_size, "device": " / ".join(devices),
                            "compute_type": " / ".join(compute),
                            "elapsed_seconds": round(sum(timings), 4) if len(timings) == len(parts) else None,
                            "fallback": next((item["fallback"] for item in reversed(completed)
                                              if item.get("fallback")), None)},
        "segments": merged["segments"],
        "words": merged["words"],
        "transcript_version": TRANSCRIPT_VERSION,
        "chunked": True,
        "chunks": merged["chunks"],
        "media_kind": media_kind,
        "render_from_sections": bool(render_from_sections),
        "source": str(source or video),
        "source_duration": total,
        "source_identity": identity,
    }
    out = workdir / "transcript.json"
    _atomic_json(out, transcript)
    print(f"Wrote {out}  ({len(transcript['words'])} words, "
          f"{len(transcript['segments'])} segments, {merged['chunks']} chunks)",
          flush=True)
    return out


def write_media_transcript(video, workdir, duration, source=None,
                           media_kind="video", render_from_sections=True,
                           identity=None, language=""):
    """Write an empty transcript for a silent source scored from media alone."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    total = float(duration or 0.0)
    transcript = {
        "video": str(video),
        "language": language,
        "duration": total,
        "model": None,
        "segments": [],
        "words": [],
        "transcript_version": TRANSCRIPT_VERSION,
        "chunked": False,
        "chunks": 0,
        "media_kind": media_kind,
        "render_from_sections": bool(render_from_sections),
        "source": str(source or video),
        "source_duration": total,
        "source_identity": str(identity or ""),
    }
    out = workdir / "transcript.json"
    _atomic_json(out, transcript)
    print(f"Wrote {out}  (media-only source, no speech)", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--model", default="medium",
                    help="faster-whisper model: tiny/base/small/medium/large-v3")
    ap.add_argument("--device", choices=("cpu", "cuda", "auto"), default="auto")
    ap.add_argument("--compute-type", default="default")
    args = ap.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    video = resolve_source(args.url, workdir)
    print("Downloaded:", video, flush=True)
    transcribe(video, workdir, args.model, args.device, args.compute_type)
    print("STAGE 1 COMPLETE", flush=True)


if __name__ == "__main__":
    configure_utf8()
    main()
