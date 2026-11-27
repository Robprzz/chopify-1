"""Clip copy, local artwork and reproducible derivatives."""
from functools import lru_cache
import hashlib
import json
import math
import subprocess
import tempfile
import time
import uuid

import projects
import branding
import export_cache
import export_settings
import render_progress
import render_clips
import subtitle_quality
import subtitle_repair
import watermark
import signature


# Conservative product limits, including the text sent as one TikTok field.
LIMITS = {"tiktok": (150, 2200), "instagram": (150, 2200),
          "facebook": (150, 2200), "youtube": (100, 5000)}


def copy_fields(data, platform):
    if platform not in LIMITS:
        raise ValueError("Elige una plataforma disponible.")
    title_limit, text_limit = LIMITS[platform]
    result = {}
    for key, limit in (("title", title_limit), ("caption", text_limit), ("description", text_limit)):
        value = data.get(key, "")
        if not isinstance(value, str) or len(value.strip()) > limit or (key == "title" and not value.strip()):
            raise ValueError(f"Revisa el campo {key}. Su máximo es de {limit} caracteres.")
        result[key] = value.strip()
    tags = data.get("hashtags", [])
    if (not isinstance(tags, list) or len(tags) > 12
            or any(not isinstance(tag, str) or not tag.startswith("#") or len(tag) > 41
                   or not tag[1:] or not all(c.isalnum() or c == "_" for c in tag[1:]) for tag in tags)):
        raise ValueError("Usa hasta 12 hashtags válidos.")
    result["hashtags"] = tags
    if platform != "youtube" and len(combined(result)) > text_limit:
        raise ValueError(f"El texto combinado y sus etiquetas admiten hasta {text_limit} caracteres.")
    return result


def combined(value):
    return "\n\n".join(dict.fromkeys(part for part in
        (value.get("caption"), value.get("description"), " ".join(value.get("hashtags", []))) if part))


def media_stamp(path):
    info = path.stat()
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns


@lru_cache(maxsize=128)
def digest_for(path, identity):
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if media_stamp(path) != identity:
        raise ValueError("El MP4 cambió durante la lectura.")
    return digest


@lru_cache(maxsize=128)
def media_info(path, identity):
    try:
        probe = subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file",
                                "-f", "mov", "-show_entries",
                                "stream=codec_type,duration,start_time,width,height:format=duration",
                                "-of", "json", str(path)], capture_output=True, timeout=12, check=True)
        data = json.loads(probe.stdout)
        duration = float(data["format"]["duration"])
        if not (math.isfinite(duration) and duration > 0):
            raise ValueError
        tracks = {item["codec_type"]: item for item in reversed(data["streams"])}
        video = tracks["video"]
        width, height = int(video["width"]), int(video["height"])
        if width <= 0 or height <= 0:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        raise ValueError("No se pudo leer la resolución o la duración del MP4.") from None
    return {"duration": duration, "width": width, "height": height,
            "format": "9:16" if height > width else "1:1" if height == width else "16:9",
            "tracks": tracks}


@lru_cache(maxsize=64)
def verify_media(path, identity, platform):
    info = media_info(path, identity)
    if info["duration"] > 600:
        raise ValueError("La verificación de publicación admite clips de hasta diez minutos.")
    tracks = info["tracks"]
    if "audio" in tracks:
        try:
            audio, video = tracks["audio"], tracks["video"]
            delta_start = abs(float(audio["start_time"]) - float(video["start_time"]))
            delta_end = abs(float(audio["duration"]) - float(video["duration"]))
        except (KeyError, TypeError, ValueError):
            raise ValueError("No se pudo verificar el tiempo de las pistas de audio y video.") from None
        if not all(math.isfinite(value) and value <= .5 for value in (delta_start, delta_end)):
            raise ValueError("Las pistas de audio y video no están sincronizadas.")
    if platform == "tiktok":
        from tiktok import _inspect_mp4
        _inspect_mp4(path)
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-threads", "2",
                        "-protocol_whitelist", "file,pipe", "-f", "mov", "-i", str(path),
                        "-map", "0:v:0", "-map", "0:a:0?", "-f", "null", "-"], capture_output=True,
                       check=True, timeout=min(180, max(25, info["duration"] * 2)))
    except (OSError, subprocess.SubprocessError):
        raise ValueError("El MP4 no superó la comprobación de reproducción completa.") from None
    if media_stamp(path) != identity:
        raise ValueError("El MP4 cambió durante la verificación.")
    return info


def fallback(path):
    try:
        data = json.loads(subtitle_quality.local_text(path.with_suffix(".meta.json"), 128000))
    except (ValueError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    tags = data.get("hashtags") or []
    tags = [tag for tag in tags if isinstance(tag, str) and tag.startswith("#")
            and 1 < len(tag) <= 41 and all(c.isalnum() or c == "_" for c in tag[1:])][:12]
    return {"title": str(data.get("title") or path.stem)[:100], "caption": "",
            "description": str(data.get("description") or "")[:1200], "hashtags": tags}


class Clips:
    def __init__(self, root, calendar, service, editorial):
        self.root, self.calendar, self.service, self.editorial = root, calendar, service, editorial
        with calendar.connect(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS clip_copy (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, file TEXT NOT NULL,
                platform TEXT NOT NULL, copy TEXT NOT NULL, created REAL NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS clip_copy_key ON clip_copy(project,file,platform,id)")
            db.execute("""CREATE TABLE IF NOT EXISTS clip_art (
                project TEXT NOT NULL,file TEXT NOT NULL,cover TEXT,mark TEXT,PRIMARY KEY(project,file))""")
            db.execute("""CREATE TABLE IF NOT EXISTS clip_renders (
                key TEXT PRIMARY KEY, record TEXT NOT NULL)""")

    def read(self, project, filename, platform):
        if platform not in LIMITS:
            raise ValueError("Elige una plataforma disponible.")
        path = self.service._clip(project, filename)
        with self.calendar.connect() as db:
            history = db.execute("""SELECT id,copy,created FROM clip_copy WHERE project=? AND file=? AND platform=?
                                    ORDER BY id DESC LIMIT 10""", (project, filename, platform)).fetchall()
            art = db.execute("SELECT cover,mark FROM clip_art WHERE project=? AND file=?", (project, filename)).fetchone()
        versions = [{"id": x["id"], **json.loads(x["copy"]), "created": x["created"]} for x in history]
        cover = self.cover_url(project, filename, art["cover"]) if art and art["cover"] else None
        details = media_info(path, media_stamp(path))
        try:
            meta = projects.read_json(path.with_suffix(".meta.json"))
            options = subtitle_repair._options_for_clip(path.parent.parent, meta.get("subtitles") or {}, project)
            template = export_settings.Store(self.root)
            effective = template.summary(options)
            if not effective["inherited"]:
                effective = template.summary(options, aspect=details["format"])
        except FileNotFoundError:
            effective = {"aspect": details["format"], "inherited": False, "warning": None}
        return {"platform": platform, "export": effective,
                "copy": versions[0] if versions else (self.initial_copy(project, filename, platform) or fallback(path)),
                "versions": versions, "cover": cover, "mark": self.mark_preview(project, filename, art),
                **{key: value for key, value in details.items() if key != "tracks"}}

    def initial_copy(self, project, filename, platform):
        self.editorial._migrate()
        profile = "facebook" if platform == "instagram" else platform
        with self.editorial.store.connect() as db:
            row = db.execute("""SELECT result,selected,edits FROM editorial_jobs
                WHERE clip_id=? AND state='done' AND json_extract(settings,'$.platform')=?
                AND editorial_state IN ('valid','extractive') ORDER BY id DESC LIMIT 1""",
                (f"{project}/{filename}", profile)).fetchone()
        if not row:
            return None
        result = json.loads(row["result"])
        style = row["selected"] or result.get("recommended_variant")
        variant = next((item for item in result.get("variants", []) if item["style"] == style), None)
        if not variant or variant.get("valid") is False:
            return None
        draft = {**variant, **json.loads(row["edits"] or "{}").get(style, {})}
        try:
            return copy_fields(draft, platform)
        except ValueError:
            return None

    @staticmethod
    def cover_url(project, filename, digest):
        from urllib.parse import urlencode
        return "/api/clip-cover?" + urlencode({"project": project, "file": filename, "id": digest})

    def save(self, project, filename, platform, body):
        if platform not in LIMITS:
            raise ValueError("Elige una plataforma disponible.")
        media = self.service._clip(project, filename)
        media_info(media, media_stamp(media))
        with self.calendar.connect(write=True) as db:
            if "restore" in body:
                record = db.execute("SELECT copy FROM clip_copy WHERE id=? AND project=? AND file=? AND platform=?",
                                    (body["restore"], project, filename, platform)).fetchone()
                if not record:
                    raise ValueError("La versión del caption no está disponible.")
                value = json.loads(record["copy"])
            else:
                value = copy_fields(body, platform)
            previous = db.execute("""SELECT copy FROM clip_copy WHERE project=? AND file=? AND platform=?
                                     ORDER BY id DESC LIMIT 1""", (project, filename, platform)).fetchone()
            if previous is None or json.loads(previous["copy"]) != value:
                db.execute("INSERT INTO clip_copy(project,file,platform,copy,created) VALUES(?,?,?,?,?)",
                           (project, filename, platform, json.dumps(value, ensure_ascii=False), time.time()))
        return self.read(project, filename, platform)

    def artwork(self, project, filename, platform, body):
        self.read(project, filename, platform)
        path = self.service._clip(project, filename)
        with self.calendar.connect(write=True) as db:
            if "cover" in body:
                digest = None
                if body["cover"] is not None:
                    image = watermark._upload(body["cover"])
                    digest = hashlib.sha256(image).hexdigest()
                    watermark._write(path.with_name(f"{path.stem}.cover-{digest}.png"), image)
                db.execute("""INSERT INTO clip_art(project,file,cover) VALUES(?,?,?)
                           ON CONFLICT(project,file) DO UPDATE SET cover=excluded.cover""", (project, filename, digest))
            if "mark" in body:
                mode = body["mark"]
                value = None
                if mode == "none":
                    value = {"mode": "none"}
                elif mode == "custom":
                    current = db.execute("SELECT mark FROM clip_art WHERE project=? AND file=?", (project, filename)).fetchone()
                    previous = json.loads(current[0] or "{}") if current else {}
                    upload = body.get("image")
                    if upload:
                        image = watermark._upload(upload)
                        digest = hashlib.sha256(image).hexdigest()
                        watermark._write(path.with_name(f"{path.stem}.mark-{digest}.png"), image)
                    else:
                        digest = previous.get("image_id")
                        if not digest:
                            raise ValueError("Selecciona un PNG para esta marca de agua.")
                    settings = watermark._settings({**watermark.DEFAULTS, **body.get("settings", {}),
                                                    "enabled": True, "image_id": digest})
                    value = {"mode": "custom", **settings}
                elif mode != "global":
                    raise ValueError("Selecciona un modo de marca de agua.")
                db.execute("""INSERT INTO clip_art(project,file,mark) VALUES(?,?,?)
                           ON CONFLICT(project,file) DO UPDATE SET mark=excluded.mark""",
                           (project, filename, json.dumps(value)))
        return self.read(project, filename, platform)

    def mark_preview(self, project, filename, art):
        from urllib.parse import urlencode
        value = json.loads(art["mark"]) if art and art["mark"] else None
        if value is None:
            default = watermark.Store(self.root).read()
            if default["enabled"] and default["image_id"]:
                value = {"mode": "global", **default, "url": "/watermark-image/" + default["image_id"]}
        if value and value["mode"] == "custom":
            value["url"] = "/api/clip-mark?" + urlencode({"project": project, "file": filename, "id": value["image_id"]})
        return value

    def image(self, project, filename, digest, kind):
        path = self.service._clip(project, filename)
        with self.calendar.connect() as db:
            row = db.execute("SELECT cover,mark FROM clip_art WHERE project=? AND file=?", (project, filename)).fetchone()
        expected = (row["cover"] if kind == "cover" else json.loads(row["mark"] or "{}").get("image_id")) if row else None
        if not digest or expected != digest:
            raise FileNotFoundError
        cover = path.with_name(f"{path.stem}.{kind}-{digest}.png")
        if not cover.is_file() or cover.resolve() != cover:
            raise FileNotFoundError
        return cover

    @staticmethod
    def _changed_transcript(project, base, track):
        previous = track.get("transcript_hash")
        if not previous or project.startswith("live/"):
            return False
        try:
            return previous != projects.digest(projects.read_json(base / "transcript.json")["words"])
        except FileNotFoundError:
            return False  # The already completed clip can still be reused.

    def reexport(self, project, filename, aspect, profile=None, progress=None):
        if aspect not in render_clips.ASPECTS:
            raise ValueError("Selecciona un formato disponible.")
        progress = progress or render_progress.Progress()
        progress.update("preparation")
        progress.check()
        path = self.service._clip(project, filename)
        stamp = media_stamp(path)
        details = media_info(path, stamp)
        meta = projects.read_json(path.with_suffix(".meta.json"))
        track = meta.get("subtitles") or {}
        base = path.parent.parent
        options = subtitle_repair._options_for_clip(base, track, project)
        options, inherited, _ = export_settings.Store(self.root).resolve(options, aspect=aspect, profile=profile)
        frame_options = options["signature"]
        frame = signature.load(frame_options["template"]) if frame_options else None
        with self.calendar.connect() as db:
            row = db.execute("SELECT mark FROM clip_art WHERE project=? AND file=?", (project, filename)).fetchone()
        saved = json.loads(row[0]) if row and row[0] else None
        global_mark = watermark.Store(self.root)
        existing_options = track.get("render_options") or {}
        if (not inherited and profile is None and saved is None and aspect == details["format"]
                and not options["brand"] and not track.get("watermark_applied")
                and frame_options == existing_options.get("signature")
                and options["export_profile"] == existing_options.get("export_profile", "quality")
                and not self._changed_transcript(project, base, track)
                and (not options["use_watermark"] or not global_mark.read()["enabled"])):
            progress.update("verification")
            expected = track.get("media_hash")
            if expected:
                if digest_for(path, stamp) != expected:
                    raise ValueError("El MP4 existente cambió. Comprueba el archivo antes de exportar.")
            else:
                verify_media(path, stamp, "youtube")
            progress.update("reuse")
            progress.update("ready")
            return filename

        progress.update("inspection")
        duration = details["duration"]
        options["tighten"] = bool(track.get("tightened"))
        span = float(meta["end"]) - float(meta["start"]) if options["tighten"] else duration
        if options["tighten"]:
            duration = span
        source, start = subtitle_repair.clean_source(path, project, meta, span)
        # Reuse original words rather than turning publication copy into subtitles.
        if project.startswith("live/"):
            cues = subtitle_quality.read_srt(path.with_suffix(".srt")) if path.with_suffix(".srt").is_file() else []
            words = [{"start": start+a, "end": start+b, "word": text} for a, b, text in cues]
        else:
            transcript = projects.read_json(base / "transcript.json")
            shift = start - meta["start"]
            words = [{**word, "start": word["start"]+shift, "end": word["end"]+shift}
                     for word in transcript.get("words", [])]
        if not saved:
            use_mark = options["use_watermark"] and (not frame or frame_options["watermark"])
            mark = global_mark.for_render() if use_mark else None
        elif saved["mode"] == "none":
            mark = None
        else:
            image = path.with_name(f"{path.stem}.mark-{saved['image_id']}.png")
            if image.resolve() != image or media_stamp(image)[0] > watermark.MAX_BYTES:
                raise ValueError("La imagen de marca de agua no está disponible.")
            data = image.read_bytes()
            if hashlib.sha256(data).hexdigest() != saved["image_id"]:
                raise ValueError("La imagen de marca de agua cambió.")
            mark = watermark.Mark(image, watermark._dimensions(data), saved)
        brand = branding.load_brand(options["brand"]) if options["brand"] else None
        if not project.startswith("live/") and aspect == details["format"] and not (options["tighten"] and mark):
            state = projects.read_project(base)
            for key, record in (state.get("render_artifacts") or {}).items():
                if (record.get("path") != str(path) or record.get("section") or record.get("recovery")
                        or not isinstance(record.get("segment"), dict)):
                    continue
                if (export_cache.recipe(source, record["segment"], words, options, brand, mark, frame,
                                        retain=record.get("retained", False)) == key
                        and export_cache.verified(record, path.parent) == path):
                    progress.update("reuse")
                    progress.update("ready")
                    return filename
        segment = {"start": start, "end": start + duration, "overall": meta.get("score"),
                   "hook": str(meta.get("title") or path.stem)}
        key = export_cache.recipe(source, segment, words, options, brand, mark, frame)
        with self.calendar.connect() as db:
            cached = db.execute("SELECT record FROM clip_renders WHERE key=?", (key,)).fetchone()
        if cached and (output := export_cache.verified(json.loads(cached[0]), path.parent, project=project)):
            progress.update("reuse")
            progress.update("ready")
            return output.name
        export_id = uuid.uuid4().hex[:12]
        with tempfile.TemporaryDirectory(dir=path.parent, prefix=".clip-version-") as tmp:
            output = render_clips.render(
                {"id": export_id, **segment}, words, source, *export_cache.dimensions(source, base), base, aspect,
                out_dir=tmp, camera_mode=options["camera_mode"], brand=brand, do_tighten=options["tighten"],
                layout=options["layout"], centers=options["centers"], loudnorm=options["loudnorm"],
                style=options["style"], fit=options["fit"], caption=options["caption"], mark=mark,
                export_profile=options["export_profile"], progress=progress, signature_frame=frame,
                timeout=600, allow_model_download=False)
            progress.check()
            progress.update("promotion")
            if project.startswith("live/"):
                from live_clips import finished_record
                record = finished_record(projects.read_json(base / "live_session.json"), path) or {}
                records = base / "records"
                records.mkdir(exist_ok=True)
                projects.atomic_json(records / f"{export_id}.json", {"id": export_id, **{
                    key: value for key, value in record.items() if key not in ("id", "files")},
                    "files": {**record.get("files", {}), "mp4": str(path.parent / output.name)}})
            for suffix in (".meta.json", ".ass", ".srt", ".png", ".mp4"):
                artifact = output.with_suffix(suffix)
                if artifact.is_file():
                    artifact.replace(path.parent / artifact.name)
        delivered = path.parent / output.name
        record = {"path": str(delivered), "artifacts": export_cache.artifacts(delivered)}
        with self.calendar.connect(write=True) as db:
            db.execute("INSERT OR REPLACE INTO clip_renders (key,record) VALUES (?,?)", (key, json.dumps(record)))
        progress.update("ready")
        return output.name
