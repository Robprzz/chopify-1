"""Persistent render identities and validation of already promoted artifacts."""
from pathlib import Path
import stat

VERSION = 1


def identity(path):
    path = Path(path).resolve(strict=True)
    info = path.stat()
    return [str(path), info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def recipe(source, segment, words, options, brand, mark, frame, preview=False, section=None,
           recovery=None, retain=False):
    from captions import TIMELINE_VERSION
    from export_settings import VISUAL
    from projects import digest

    relevant = [word for word in words
                if word["end"] > segment["start"] and word["start"] < segment["end"]]
    logo = identity(brand["logo"]) if brand and brand.get("logo") else None
    bands = [identity(frame.header), identity(frame.footer)] if frame else None
    mark_settings = [dict(mark.settings), identity(mark.image)] if mark else None
    visual = {key: options[key] for key in VISUAL if key not in {"brand", "template_override", "use_watermark"}}
    return digest([VERSION, TIMELINE_VERSION, identity(source), section, preview, retain, recovery,
                   {key: segment.get(key) for key in ("start", "end", "hook", "overall")},
                   relevant, visual, options["tighten"], options["loudnorm"], brand, logo, bands, mark_settings])


def artifacts(path, preview=False):
    files = [path, path.with_suffix(".srt")]
    if not preview:
        files += [path.with_suffix(".png"), path.with_suffix(".meta.json")]
        if path.with_suffix(".ass").exists():
            files.append(path.with_suffix(".ass"))
    if any(item.is_symlink() or not stat.S_ISREG(item.stat().st_mode) for item in files):
        raise OSError("Artefacto de render fuera de la ubicación autorizada.")
    return [identity(item) for item in files]


def verified(record, folder, preview=False, *, project=None):
    if not isinstance(record, dict):
        return None
    try:
        path = Path(record["path"])
        if path.parent == folder and path.resolve() == path and artifacts(path, preview) == record.get("artifacts"):
            from asset_lifecycle import hidden
            base = folder.parent.parent
            if not hidden(base.parent if project and project.startswith("live/") else base,
                          project or folder.parent.name, path.name):
                return path
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return None


def nvenc_probe():
    """Try a single synthetic frame on request; profile selection stays software."""
    import re
    import subprocess

    try:
        run = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
                              "-i", "color=size=128x128:rate=1", "-frames:v", "1",
                              "-c:v", "h264_nvenc", "-f", "null", "-"],
                             capture_output=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "message": "No se pudo comprobar NVENC con el FFmpeg actual."}
    if not run.returncode:
        return {"available": True, "message": "NVENC codificó un cuadro de prueba. Las exportaciones siguen usando libx264."}
    error = run.stderr.decode("utf-8", "backslashreplace")
    versions = re.search(r"Required:\s*(\d+\.\d+).*?Found:\s*(\d+\.\d+)", error, re.I | re.S)
    message = (f"FFmpeg requiere NVENC {versions[1]} y el controlador ofrece {versions[2]}."
               if versions else "El FFmpeg y el controlador actuales no pudieron inicializar NVENC.")
    return {"available": False, "message": message + " Se usa libx264."}


def analysis(workdir, name, key, analyze, *, cache_empty=True):
    """Keep bounded, versioned inspection records across Studio subprocesses."""
    from projects import digest, read_json
    import json
    from watermark import _write

    directory = Path(workdir).resolve()
    path = directory / f".render-{name}.json"
    if name not in {"dimensions", "camera"} or path.is_symlink() or not directory.is_dir():
        return analyze()
    stamp = digest([VERSION, key])
    try:
        cached = read_json(path) if path.stat().st_size < 4_000_000 else {}
        if not isinstance(cached, dict):
            cached = {}
    except (OSError, ValueError):
        cached = {}
    if stamp in cached:
        return cached[stamp]
    result = analyze()
    if cache_empty or result:
        try:
            _write(path, json.dumps({**dict(list(cached.items())[-15:]), stamp: result}, allow_nan=False).encode("utf-8"))
        except (OSError, ValueError):
            pass
    return result


def dimensions(source, workdir):
    import render_clips

    return analysis(workdir, "dimensions", identity(source), lambda: render_clips.ffprobe_dims(source))


def record(path, preview, segment, retain, section, recovery=None):
    return {"path": str(path), "artifacts": artifacts(path, preview),
            "segment": {key: segment.get(key) for key in ("start", "end", "overall", "hook")},
            "retained": bool(retain), "section": section, "recovery": recovery}


def legacy_project(state, source, segment, words, options, brand, mark, frame, preview, offset, section, recovery, folder):
    """Migrate a legacy entry only when its inputs and media checksum match."""
    from captions import TIMELINE_VERSION
    from projects import digest, file_identity, read_json
    from clip_workspace import digest_for, media_stamp

    if (preview or recovery or options["export_profile"] != "quality" or options["fit"] != "cover"
            or options["facecam_box"] is not None or options["facecam_height"] != .34):
        return None
    legacy = {key: value for key, value in options.items() if key not in
              {"export_profile", "template_override", "strict_selection", "use_watermark"}}
    if frame is None:
        legacy.pop("signature", None)
    identity_logo = file_identity(brand["logo"]) if brand and brand.get("logo") else None
    bands = [file_identity(frame.header), file_identity(frame.footer)] if frame else []
    mark_key = [mark.settings] if mark else []
    key = digest([segment, words, file_identity(source), legacy, brand, identity_logo, preview, offset, section,
                  TIMELINE_VERSION, *mark_key, *bands])
    cached = state["renders"].get(key)
    if not cached:
        return None
    path = Path(cached)
    try:
        from asset_lifecycle import hidden
        if (path.parent != folder or path.resolve() != path
                or hidden(folder.parent.parent, folder.parent.name, path.name)):
            return None
        artifacts(path)
        stamp = read_json(path.with_suffix(".meta.json"))["subtitles"]["media_hash"]
        if stamp and digest_for(path, media_stamp(path)) == stamp:
            return path
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None
