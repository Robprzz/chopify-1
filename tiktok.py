"""Local, single-account TikTok Login Kit and Content Upload integration."""
import hashlib
import http.client
import json
import math
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from fractions import Fraction
from pathlib import Path

import projects
import live_clips

API = "https://open.tiktokapis.com"
INBOX_INIT = "/v2/post/publish/inbox/video/init/"
POST_STATUS = "/v2/post/publish/status/fetch/"
UPLOAD_HOSTS = frozenset({
    "open-upload.tiktokapis.com",
    "open-upload-i18n.tiktokapis.com",
    "upload.us.tiktokapis.com",
})
UPLOAD_URL_LIFETIME = 3600
UPLOAD_URL_ERRORS = {"upload_url_missing", "upload_url_invalid", "upload_host_unexpected"}
LEGACY_UPLOAD_URL_ERROR = "TikTok no devolvió una dirección oficial de subida."
INIT_REJECTIONS = {
    "invalid_param", "spam_risk_too_many_pending_share", "spam_risk_user_banned_from_posting",
    "url_ownership_unverified", "access_token_invalid", "scope_not_authorized", "rate_limit_exceeded",
}
CONTENT_ERROR_CODES = INIT_REJECTIONS | {
    "ok", "internal_error", "invalid_publish_id", "token_not_authorized_for_specified_publish_id",
}
FAIL_REASONS = {
    "file_format_check_failed", "duration_check_failed", "frame_rate_check_failed",
    "picture_size_check_failed", "internal", "video_pull_failed", "photo_pull_failed",
    "publish_cancelled", "auth_removed", "spam_risk_too_many_posts",
    "spam_risk_user_banned_from_posting", "spam_risk_text", "spam_risk",
}
AUTHORIZE = "https://www.tiktok.com/v2/auth/authorize/"
CALLBACK = "/api/tiktok/callback/"
SCOPES = ("user.info.basic", "video.upload")
PRIVACY = {"PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"}
MAX_SIZE = 4 * 1024**3
CHUNK_SIZE = 32 * 1024**2
ID_PATTERN = r"[a-zA-Z0-9_.~-]{1,64}"
FINAL_STATUSES = {"FAILED", "PUBLISH_COMPLETE"}
POST_STATUSES = FINAL_STATUSES | {"PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD", "SEND_TO_USER_INBOX"}


class TikTokError(ValueError):
    """A safe diagnostic suitable for Studio and its local history."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _safe_code(value, default):
    return value if isinstance(value, str) and re.fullmatch(ID_PATTERN, value) else default


def _decode(raw):
    try:
        data = json.loads(raw) if raw else {}
    except (ValueError, UnicodeError):
        raise TikTokError("TikTok devolvió una respuesta ilegible.") from None
    if not isinstance(data, dict):
        raise TikTokError("TikTok devolvió una respuesta inesperada.")
    return data


def _error_code(data, default):
    error = data.get("error")
    code = error.get("code") if isinstance(error, dict) else error
    return _safe_code(code, default)


def _content_error_code(data, default):
    code = _error_code(data, default)
    return code if code in CONTENT_ERROR_CODES else default


def _validate_upload_url(url):
    if url is None or url == "":
        raise TikTokError("TikTok no incluyó la URL de subida (upload_url).", "upload_url_missing")
    try:
        if (not isinstance(url, str) or not re.fullmatch(r"[!-~]{1,256}", url)
                or re.search(r'["<>#^\x60{|}\\]|%(?![0-9a-fA-F]{2})', url)):
            raise ValueError
        address = urllib.parse.urlsplit(url)
        if (address.scheme != "https" or not address.hostname or not address.path
                or address.username is not None or address.netloc.endswith(":")
                or address.port not in (None, 443)):
            raise ValueError
    except ValueError:
        raise TikTokError("TikTok devolvió una URL de subida inválida.", "upload_url_invalid") from None
    if address.hostname not in UPLOAD_HOSTS:
        raise TikTokError("TikTok devolvió un dominio de subida no reconocido.",
                          "upload_host_unexpected")


class TikTokAPI:
    def __init__(self, key, secret):
        self.key = key
        self.secret = secret
        self.opener = urllib.request.build_opener(_NoRedirect())

    def send(self, request):
        try:
            with self.opener.open(request, timeout=60) as response:
                return response.status, response.read(128 * 1024)
        except urllib.error.HTTPError as exc:
            code = f"HTTP_{exc.code}"
            posting = request.selector in (INBOX_INIT, POST_STATUS)
            with exc:
                if request.get_method() != "PUT" and (not posting or 400 <= exc.code < 500):
                    try:
                        raw = exc.read(128 * 1024)
                        extract_code = _content_error_code if posting else _error_code
                        code = extract_code(_decode(raw), code)
                    except (TikTokError, OSError, http.client.HTTPException):
                        pass
            raise TikTokError(f"TikTok rechazó la solicitud ({code}).", code) from None
        except (OSError, ValueError, http.client.HTTPException):
            raise TikTokError("No se pudo completar la comunicación con TikTok.") from None

    def oauth(self, path, **fields):
        body = urllib.parse.urlencode({
            "client_key": self.key, "client_secret": self.secret, **fields,
        }).encode("ascii")
        request = urllib.request.Request(API + "/v2/oauth/" + path + "/", data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded",
        })
        _, raw = self.send(request)
        data = _decode(raw)
        if data.get("error"):
            code = _error_code(data, "oauth_error")
            raise TikTokError(f"TikTok rechazó OAuth ({code}).", code)
        return data

    def authorized(self, path, access_token, payload=None, *, method="POST"):
        body = None if method == "GET" else json.dumps(payload or {}).encode()
        request = urllib.request.Request(API + path, data=body, method=method, headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        })
        _, raw = self.send(request)
        data = _decode(raw)
        extract_code = _content_error_code if path in (INBOX_INIT, POST_STATUS) else _error_code
        code = extract_code(data, "invalid_response")
        if code != "ok" or not isinstance(data.get("data"), dict):
            raise TikTokError(f"TikTok rechazó la solicitud ({code}).", code)
        return data["data"]

    def upload(self, url, media, size, chunk_size, count):
        _validate_upload_url(url)
        offset = 0
        for index in range(count):
            length = size - offset if index == count - 1 else chunk_size
            chunk = media.read(length)
            if len(chunk) != length:
                raise TikTokError("El archivo de subida está incompleto.")
            request = urllib.request.Request(url, data=chunk, method="PUT", headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(length),
                "Content-Range": f"bytes {offset}-{offset + length - 1}/{size}",
            })
            status, _ = self.send(request)
            if status != (201 if index == count - 1 else 206):
                raise TikTokError(f"TikTok devolvió un estado inesperado de subida ({status}).")
            offset += length


def _inspect_mp4(path):
    try:
        probe = subprocess.run([
            "ffprobe", "-v", "error", "-protocol_whitelist", "file",
            "-select_streams", "v:0", "-show_entries",
            "stream=codec_name,width,height,avg_frame_rate:format=format_name,duration",
            "-of", "json", str(path),
        ], capture_output=True, check=True, timeout=30)
        data = json.loads(probe.stdout)
        video, *_ = data["streams"]
        width, height = video["width"], video["height"]
        fps = Fraction(video["avg_frame_rate"])
        duration = float(data["format"]["duration"])
        valid = ("mp4" in data["format"]["format_name"].split(",")
                 and video["codec_name"] in {"h264", "hevc", "vp8", "vp9"}
                 and 360 <= width <= 4096 and 360 <= height <= 4096
                 and 23 <= fps <= 60 and math.isfinite(duration) and 0 < duration <= 600)
    except (OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError, ZeroDivisionError):
        raise TikTokError("No se pudo leer este MP4 con ffprobe.") from None
    if not valid:
        raise TikTokError("Exporta un MP4 compatible con TikTok, de 23 a 60 fps y hasta 10 minutos.")
    return {"width": width, "height": height, "duration": round(duration, 2),
            "vertical_9_16": width * 16 == height * 9}


class TikTokStudio:
    def __init__(self, root, busy):
        self.root = Path(root).resolve()
        self.busy = busy
        self.history = self.root / ".tiktok-publications"
        self.api = TikTokAPI(os.environ.get("CHOPIFY_TIKTOK_CLIENT_KEY", ""),
                             os.environ.get("CHOPIFY_TIKTOK_CLIENT_SECRET", ""))
        self.scopes = SCOPES
        if os.environ.get("CHOPIFY_TIKTOK_CREATOR_INFO") == "1":
            self.scopes += ("video.publish",)
        self.lock = threading.RLock()
        self.pending = None
        self.token = None
        self.account = None
        self.oauth_error = ""
        self.review = None
        self.sending = False

    @staticmethod
    def redirect_uri(port):
        return f"http://127.0.0.1:{port}{CALLBACK}"

    def connect(self, port):
        with self.lock:
            if not self.api.key or not self.api.secret:
                raise TikTokError("Configura las credenciales de TikTok en el entorno de Studio.")
            if self.account or self.sending:
                raise TikTokError("Desconecta la cuenta actual antes de cambiarla.")
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
            self.pending = (state, verifier, time.monotonic() + 600)
            self.oauth_error = ""
            args = urllib.parse.urlencode({
                "client_key": self.api.key, "response_type": "code",
                "redirect_uri": self.redirect_uri(port), "scope": ",".join(self.scopes),
                "state": state, "code_challenge_method": "S256",
                "code_challenge": hashlib.sha256(verifier.encode()).hexdigest(),
            })
            return {"url": AUTHORIZE + "?" + args}

    def callback(self, values, port):
        with self.lock:
            state = values.get("state", [])
            if (not self.pending or len(state) != 1
                    or len(state[0]) > 128 or not state[0].isascii()
                    or not secrets.compare_digest(state[0], self.pending[0])):
                raise TikTokError("La sesión OAuth no coincide. Vuelve a conectar TikTok.")
            _, verifier, expires = self.pending
            self.pending = None
            code = values.get("code", [])
            try:
                if expires < time.monotonic() or values.get("error") or len(code) != 1 or not code[0]:
                    raise TikTokError("La autorización se canceló o caducó. Vuelve a conectar TikTok.")
                data = self.api.oauth(
                    "token", grant_type="authorization_code", code=code[0],
                    redirect_uri=self.redirect_uri(port), code_verifier=verifier,
                )
                token = self._token_record(data)
                required = set(SCOPES)
                if not required.issubset(token["scopes"]):
                    raise TikTokError("TikTok no concedió los permisos de perfil y subida.")
                profile = self.api.authorized("/v2/user/info/?fields=open_id,display_name",
                                              token["access_token"], method="GET").get("user")
                if (not isinstance(profile, dict) or profile.get("open_id") != token["open_id"]
                        or not isinstance(profile.get("display_name"), str)
                        or not profile["display_name"]):
                    raise TikTokError("TikTok no devolvió el perfil de la cuenta autorizada.")
            except TikTokError as exc:
                self.oauth_error = str(exc)
            else:
                self.token = token
                key = hashlib.sha256((self.api.key + ":" + token["open_id"]).encode()).hexdigest()
                self.account = {"name": profile["display_name"][:150], "key": key}

    @staticmethod
    def _token_record(data):
        try:
            values = {key: data[key] for key in ("access_token", "refresh_token", "open_id")}
            if not all(isinstance(v, str) and v for v in values.values()):
                raise ValueError
            scopes = set(data["scope"].split(","))
            now = time.monotonic()
            access = int(data["expires_in"])
            refresh = int(data["refresh_expires_in"])
            if access <= 0 or refresh <= 0:
                raise ValueError
            return {**values, "scopes": scopes, "expires": now + access, "refresh_expires": now + refresh}
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            raise TikTokError("TikTok devolvió credenciales incompletas.") from None

    def _access(self, scope="video.upload"):
        if not self.account or not self.token:
            raise TikTokError("Conecta tu cuenta de TikTok.")
        now = time.monotonic()
        if self.token["expires"] - now < 60:
            if self.token["refresh_expires"] <= now:
                self.token = None
                self.account = None
                self.review = None
                raise TikTokError("La sesión de TikTok caducó. Vuelve a conectar la cuenta.")
            try:
                data = self.api.oauth("token", grant_type="refresh_token",
                                      refresh_token=self.token["refresh_token"])
            except TikTokError as exc:
                if exc.code != "invalid_grant":
                    raise
                self.token = self.account = self.review = None
                raise TikTokError("La sesión de TikTok caducó. Vuelve a conectar la cuenta.") from None
            updated = self._token_record(data)
            if updated["open_id"] != self.token["open_id"]:
                raise TikTokError("TikTok devolvió una cuenta distinta durante la renovación.")
            self.token = updated
        if scope not in self.token["scopes"]:
            raise TikTokError(f"TikTok no ha concedido el permiso {scope}.")
        return self.token["access_token"]

    def disconnect(self):
        with self.lock:
            if self.sending:
                raise TikTokError("Espera a que termine la subida antes de desconectar.")
            if self.token:
                try:
                    self.api.oauth("revoke", token=self.token["access_token"])
                except TikTokError as exc:
                    if exc.code not in {"access_token_invalid", "invalid_grant"}:
                        raise
            self.pending = self.token = self.account = self.review = None
            self.oauth_error = ""
            return {"ok": True}

    def privacy(self):
        with self.lock:
            data = self.api.authorized("/v2/post/publish/creator_info/query/",
                                       self._access("video.publish"))
            options = data.get("privacy_level_options")
            if not isinstance(options, list):
                raise TikTokError("TikTok no devolvió opciones de privacidad.")
            return {"options": [item for item in options if isinstance(item, str) and item in PRIVACY]}

    def _clip(self, project, filename):
        if isinstance(project, str) and project.startswith("live/"):
            try:
                media = live_clips.live_clip_path(self.root, project[5:], filename)
                state = projects.read_json(media.parent.parent / "live_session.json")
                if media.suffix == ".mp4" and media.is_file() and live_clips.finished_record(state, media):
                    return media
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                pass
            raise TikTokError("Selecciona un MP4 terminado de una sesión Live de Studio.")
        path = projects.project_path(self.root, project).resolve()
        clips = path / "clips"
        if (path.parent != self.root or clips.resolve() != clips or self.busy(project)
                or not isinstance(filename, str) or Path(filename).name != filename
                or "/" in filename or "\\" in filename or not filename.endswith(".mp4")):
            raise TikTokError("Selecciona un MP4 de una exportación terminada de Studio.")
        media = (clips / filename).resolve()
        if media.parent != clips or not media.is_file():
            raise TikTokError("La exportación seleccionada ya no está disponible.")
        return media

    def exports(self):
        result = []
        for project in sorted(self.root.glob("*/project.json")):
            project_id = project.parent.name
            if not re.fullmatch(r"[a-f0-9]{12}", project_id) or self.busy(project_id):
                continue
            try:
                source = projects.read_json(project).get("source", "")
                name = Path(urllib.parse.urlsplit(source).path).name or project_id
            except (AttributeError, TypeError, OSError, ValueError):
                name = project_id
            for clip in sorted((project.parent / "clips").glob("*.mp4")):
                try:
                    self._clip(project_id, clip.name)
                except TikTokError:
                    continue
                result.append({"project": project_id, "project_name": name, "filename": clip.name})
        return [*result, *live_clips.live_exports(self.root)]

    @staticmethod
    def _description(path):
        try:
            meta = projects.read_json(path.with_suffix(".meta.json"))
            title = meta["title"].removesuffix(" #shorts")
            tags = [tag for tag in meta["hashtags"] if isinstance(tag, str)
                    and tag.lower() not in {"#shorts", "#reels"}]
            return " ".join([title, *tags])[:2200]
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return path.stem[:2200]

    def inspect(self, project, filename):
        path = self._clip(project, filename)
        before = path.stat()
        if not 0 < before.st_size <= MAX_SIZE:
            raise TikTokError("El MP4 debe ocupar entre 1 byte y 4 GB.")
        media_info = _inspect_mp4(path)
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        after = path.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise TikTokError("La exportación cambió. Vuelve a seleccionar el clip.")
        with self.lock:
            self.review = {
                "id": secrets.token_hex(16), "project": project, "filename": filename,
                "size": after.st_size, "digest": digest,
                "owner": self.account["key"] if self.account else None,
                "expires": time.monotonic() + 900,
            }
            return {
                **media_info, "review_id": self.review["id"],
                "description": self._description(path),
                "url": f"/media/{project}/clips/{urllib.parse.quote(filename, safe='')}",
            }

    def _history_path(self, job):
        if not isinstance(job, str) or not re.fullmatch(r"[a-f0-9]{32}", job):
            raise TikTokError("Identificador de publicación inválido.")
        return self.history / f"{job}.json"

    def _public_record(self, record):
        data = {key: value for key, value in record.items()
                if key not in {"owner", "digest", "upload_not_started"}}
        if not self.sending:
            data["status"] = {"PREPARING": "LOCAL_ERROR", "STARTING": "CHECK_INBOX",
                              "UPLOADING": "CHECK_STATUS"}.get(data["status"], data["status"])
        return data

    def _save(self, record):
        record["updated"] = time.time()
        self.history.mkdir(mode=0o700, exist_ok=True)
        projects.atomic_json(self._history_path(record["id"]), record)

    @staticmethod
    def _record_error(record, message, code=None):
        record["last_error"] = message
        entry = {"at": time.time(), "message": message}
        if code:
            entry["code"] = code
        record["errors"] = [*record.get("errors", [])[-19:], entry]

    def _account_history(self):
        if self.account:
            for path in self.history.glob("*.json"):
                try:
                    row = projects.read_json(path)
                    if row["owner"] == self.account["key"] and isinstance(row.get("status"), str):
                        yield row
                except (OSError, ValueError, TypeError, KeyError):
                    continue

    @staticmethod
    def _can_retry(record):
        status = record["status"]
        if status in FINAL_STATUSES or status in {"PREPARING", "LOCAL_ERROR", "INIT_REJECTED"}:
            return True
        if status in {"STARTING", "CHECK_INBOX"} and not record.get("publish_id"):
            # No PUT could have started without a saved publish_id. Let any orphan session expire.
            updated = record.get("updated")
            return isinstance(updated, (int, float)) and time.time() - updated >= UPLOAD_URL_LIFETIME
        if status in {"CHECK_STATUS", "PROCESSING_UPLOAD"}:
            # Older versions used this exact message only before reading or sending the file.
            return record.get("upload_not_started", False) or any(
                error.get("message") == LEGACY_UPLOAD_URL_ERROR for error in record.get("errors", [])
            )
        return False

    def state(self):
        with self.lock:
            entries = [self._public_record(row) for row in self._account_history()]
            entries.sort(key=lambda row: row.get("created", 0), reverse=True)
            return {
                "configured": bool(self.api.key and self.api.secret),
                "account": self.account["name"] if self.account else None,
                "creator_info": bool(self.token and "video.publish" in self.token["scopes"]),
                "oauth_error": self.oauth_error,
                "history": entries,
            }

    def submit(self, job, description, approved):
        if approved is not True:
            raise TikTokError("Confirma explícitamente el envío de este clip a TikTok.")
        if not isinstance(description, str) or len(description) > 2200:
            raise TikTokError("La descripción debe tener como máximo 2200 caracteres.")
        path = self._history_path(job)
        with self.lock:
            if not self.account:
                raise TikTokError("Conecta tu cuenta de TikTok.")
            if path.exists():
                existing = projects.read_json(path)
                if existing["owner"] == self.account["key"]:
                    return self._public_record(existing)
            review = self.review
            if (not review or review["id"] != job or review["owner"] != self.account["key"]
                    or review["expires"] < time.monotonic()):
                raise TikTokError("Vuelve a revisar este clip con la cuenta de TikTok conectada.")
            if self.sending:
                raise TikTokError("Ya hay una subida en curso.")
            for existing in self._account_history():
                same_clip = (existing["digest"] == review["digest"] if "digest" in existing else
                             (existing.get("project"), existing.get("filename")) ==
                             (review["project"], review["filename"]))
                if same_clip and not self._can_retry(existing):
                    self.review = None
                    return self._public_record(existing)
            access = self._access()
            media = self._clip(review["project"], review["filename"])
            record = {
                "id": job, "owner": review["owner"], "project": review["project"],
                "filename": review["filename"], "digest": review["digest"], "description": description,
                "created": time.time(), "status": "PREPARING", "last_error": "",
            }
            self._save(record)
            self.review = None
            self.sending = True

        try:
            self._transfer(media, review, record, access)
        except (TikTokError, OSError) as exc:
            with self.lock:
                code = exc.code if isinstance(exc, TikTokError) else None
                self._record_error(record, str(exc) if isinstance(exc, TikTokError)
                                   else "Error local al leer o guardar la subida.", code)
                if record.get("publish_id"):
                    record["status"] = "CHECK_STATUS"
                    if code in UPLOAD_URL_ERRORS:
                        record["upload_not_started"] = True
                elif record["status"] == "STARTING":
                    record["status"] = "INIT_REJECTED" if code in INIT_REJECTIONS else "CHECK_INBOX"
                else:
                    record["status"] = "LOCAL_ERROR"
                self._save(record)
        finally:
            with self.lock:
                self.sending = False
        return self._public_record(record)

    def _transfer(self, path, review, record, access):
        size = review["size"]
        chunk_size = min(size, CHUNK_SIZE)
        count = size // chunk_size
        with path.open("rb") as source, tempfile.TemporaryFile() as snapshot:
            digest = hashlib.sha256()
            remaining = size
            while remaining:
                piece = source.read(min(remaining, 1024 * 1024))
                if not piece:
                    break
                snapshot.write(piece)
                digest.update(piece)
                remaining -= len(piece)
            if remaining or source.read(1) or digest.hexdigest() != review["digest"]:
                raise TikTokError("El MP4 cambió desde la previsualización. Selecciónalo de nuevo.")
            snapshot.seek(0)
            with self.lock:
                record["status"] = "STARTING"
                self._save(record)
            result = self.api.authorized(INBOX_INIT, access, {
                "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                                "chunk_size": chunk_size, "total_chunk_count": count},
            })
            publish_id = result.get("publish_id")
            if (not isinstance(publish_id, str) or not 0 < len(publish_id) <= 64
                    or not publish_id.isprintable() or not publish_id.strip()):
                raise TikTokError("TikTok no devolvió un identificador de publicación válido.",
                                  "publish_id_invalid")
            with self.lock:
                record.update(publish_id=publish_id, status="UPLOADING")
                self._save(record)
            self.api.upload(result.get("upload_url"), snapshot, size, chunk_size, count)
            with self.lock:
                record["status"] = "PROCESSING_UPLOAD"
                self._save(record)

    def refresh(self, job):
        with self.lock:
            path = self._history_path(job)
            if not path.is_file():
                raise TikTokError("Esa publicación no está en el historial.")
            record = projects.read_json(path)
            if not self.account or self.account["key"] != record["owner"]:
                raise TikTokError("Conecta la misma cuenta a la que enviaste el clip.")
            if self.sending:
                raise TikTokError("Espera a que termine la subida antes de consultar su estado.")
            if (not record.get("publish_id") or record["status"] in FINAL_STATUSES
                    or time.time() - record.get("checked_at", 0) < 5):
                return self._public_record(record)
            record["checked_at"] = time.time()
            try:
                data = self.api.authorized(POST_STATUS, self._access(),
                                           {"publish_id": record["publish_id"]})
                status = _safe_code(data.get("status"), "")
                if status not in POST_STATUSES:
                    raise TikTokError("TikTok no devolvió un estado de publicación reconocido.")
                record["status"] = status
                if status == "FAILED":
                    reason = _safe_code(data.get("fail_reason"), "")
                    self._record_error(record, reason if reason in FAIL_REASONS else "FAILED")
                else:
                    record["last_error"] = ""
            except TikTokError as exc:
                self._record_error(record, str(exc), exc.code)
            self._save(record)
            return self._public_record(record)
