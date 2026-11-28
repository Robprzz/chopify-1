"""Anonymous import of an MP4 explicitly advertised by a public Instagram post."""
from contextlib import closing
import http.client
from html.parser import HTMLParser
import ipaddress
import math
import os
from pathlib import Path
import re
import project_cancel
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import urljoin, urlsplit

HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
POST = re.compile(r"/(reel|p)/([a-zA-Z0-9_-]{1,64})/?")
PROFILE_REEL = re.compile(
    r"/[a-zA-Z0-9_](?:[a-zA-Z0-9_.]{0,28}[a-zA-Z0-9_])?/(reel)/([a-zA-Z0-9_-]{1,64})/?"
)
DEFAULTS = {"max_mib": 256, "max_seconds": 1200}
IPV6_UNICAST = ipaddress.ip_network("2000::/3")
CANCEL_FILE = ".instagram-cancel"
DOWNLOAD_STAGES = {"prepare", "all", "source", "instagram download"}
RETRY_STATUSES = {"failed", "cancelled", "interrupted"}
HTTP_ERRORS = {
    401: "Instagram exige iniciar sesión para acceder a este video.",
    403: "Instagram restringe el acceso a este video.",
    404: "Instagram no encontró la publicación. Puede ser privada o haberse eliminado.",
    410: "La publicación de Instagram ya no está disponible.",
    429: "Instagram limitó temporalmente las solicitudes. Reintenta más tarde.",
}


class ImportTimeout(ValueError):
    pass


class Cancelled(ValueError):
    pass


def candidate(value):
    apparent_host = r"(?i)^(?:www\.|m\.)?instagram\.com(?:[/.:@]|$)"
    try:
        parts = urlsplit(value)
        return (parts.hostname in HOSTS or bool(re.match(apparent_host, parts.netloc))
                or bool(re.match(apparent_host, value)))
    except ValueError:
        return False


def target(url):
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.netloc.lower() != parts.hostname
                or any(ord(char) < 33 or ord(char) >= 127 for char in url)):
            raise ValueError
        return parts
    except ValueError:
        raise ValueError("Instagram requiere una URL HTTPS sin puerto ni credenciales.") from None


def canonical(url):
    kind, shortcode = post_parts(target(url))
    return f"https://www.instagram.com/{kind}/{shortcode}/"


def post_parts(parts):
    if parts.hostname in HOSTS:
        if post := POST.fullmatch(parts.path) or PROFILE_REEL.fullmatch(parts.path):
            return post.groups()
    raise ValueError("Usa el enlace directo de un Reel o publicación de Instagram.")


def post_identity(url):
    return post_parts(target(url))[1]


def response_url(base, reference):
    # urljoin can remove control characters before target() sees them.
    if any(ord(char) < 33 or ord(char) >= 127 for char in reference):
        raise ValueError
    return urljoin(base, reference)


def login_page(parts):
    return parts.hostname in HOSTS and parts.path.rstrip("/") == "/accounts/login"


def confirm_page(url, identity, values, canonical_links):
    reference = values.get("og:url")
    link = values.get("og:video:secure_url") or values.get("og:video:url") or values.get("og:video")
    if not reference:
        if not link:
            raise ValueError("Instagram no expone un MP4 público descargable y la página no identifica la publicación.")
        raise ValueError("Instagram devolvió metadatos incompatibles: falta el identificador de la publicación (og:url).")
    try:
        parts = target(response_url(url, reference))
    except ValueError:
        raise ValueError("Instagram devolvió metadatos incompatibles para la publicación (og:url inválido).") from None
    if login_page(parts):
        raise ValueError("Instagram exige iniciar sesión para acceder a este video.")
    try:
        actual = post_parts(parts)[1]
    except ValueError:
        raise ValueError("Instagram devolvió metadatos incompatibles para la publicación (og:url inválido).") from None
    if actual != identity:
        raise ValueError("Instagram devolvió metadatos de una publicación distinta.")
    for reference in canonical_links:
        try:
            canonical_identity = post_identity(response_url(url, reference)) if reference else None
        except ValueError:
            canonical_identity = None
        if canonical_identity != identity:
            raise ValueError("Instagram devolvió un enlace canónico incompatible con la publicación solicitada.")
    if not link:
        raise ValueError("Instagram no expone un MP4 público descargable en esta publicación.")
    return link


def settings(values):
    if not isinstance(values, dict) or values.keys() - DEFAULTS.keys():
        raise ValueError("Límites de importación no válidos.")
    result = {**DEFAULTS, **values}
    if (type(result["max_mib"]) is not int or not 1 <= result["max_mib"] <= 1024
            or type(result["max_seconds"]) is not int or not 1 <= result["max_seconds"] <= 7200):
        raise ValueError("Usa un máximo de 1–1024 MiB y 1–7200 segundos.")
    return result


def permitted(host, media):
    return (host.endswith((".cdninstagram.com", ".fbcdn.net")) if media else host in HOSTS)


def addresses(host):
    values = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    if not values or any(
            not ip.is_global or ip.is_multicast or (ip.version == 6 and ip not in IPV6_UNICAST)
            or getattr(ip, "sixtofour", None) is not None or getattr(ip, "teredo", None) is not None
            for ip in map(ipaddress.ip_address, values)):
        raise ValueError("Instagram devolvió un destino de red no permitido.")
    return values


class PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self):
        timed_out = False
        for address in addresses(self.host)[:8]:
            try:
                sock = socket.create_connection((address, 443), timeout=self.timeout)
            except TimeoutError:
                timed_out = True
                continue
            except OSError:
                continue
            try:
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
            except OSError:
                sock.close()
                raise
            return
        if timed_out:
            raise TimeoutError("Instagram HTTPS timeout")
        raise OSError("Instagram HTTPS unavailable")


def transfer(url, *, media, cancel):
    original = post_identity(url) if not media else None
    for hop in range(4):
        cancel()
        try:
            parts = target(url)
        except ValueError:
            if hop:
                raise ValueError("Instagram devolvió una redirección inesperada a un destino inválido.") from None
            raise
        if not permitted(parts.hostname, media):
            raise ValueError("Instagram devolvió una redirección inesperada a un destino no permitido.")
        if not media:
            if login_page(parts):
                raise ValueError("Instagram exige iniciar sesión para acceder a este video.")
            try:
                identity = post_parts(parts)[1]
            except ValueError:
                raise ValueError("Instagram devolvió una redirección inesperada fuera de la publicación solicitada.") from None
            if identity != original:
                raise ValueError("Instagram devolvió una redirección inesperada a una publicación distinta.")
        conn = PinnedHTTPS(parts.hostname, timeout=8, context=ssl.create_default_context())
        try:
            try:
                # Post requests use the public path. CDN queries may carry a required signature.
                conn.request("GET", parts.path + (("?" + parts.query) if media and parts.query else ""),
                             headers={"User-Agent": "AnqoraStudio/5.1.3", "Accept-Encoding": "identity"})
                response = conn.getresponse()
            except TimeoutError:
                raise ImportTimeout("Se agotó el tiempo de conexión con Instagram. Puedes reintentar.") from None
            except (OSError, http.client.HTTPException):
                raise ValueError("La conexión con Instagram se interrumpió. Puedes reintentar.") from None
            if response.status == 200:
                return conn, response
            with closing(response):
                if response.status not in {301, 302, 303, 307, 308}:
                    raise ValueError(HTTP_ERRORS.get(response.status, f"Instagram no pudo entregar el video (HTTP {response.status})."))
                redirect = response.getheader("Location")
                if not redirect:
                    raise ValueError("Instagram devolvió una redirección incompleta.")
                try:
                    url = response_url(url, redirect)
                except ValueError:
                    raise ValueError("Instagram devolvió una redirección inesperada a un destino inválido.") from None
        except BaseException:
            conn.close()
            raise
        conn.close()
    raise ValueError("Instagram devolvió demasiadas redirecciones.")


class VideoMetadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = {}
        self.canonical_links = []
        self.metadata_open = True

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.metadata_open = False
        if self.metadata_open and tag in {"meta", "link"}:
            attr = dict(attrs)
            if tag == "link":
                if "canonical" in (attr.get("rel") or "").lower().split():
                    self.canonical_links.append(attr.get("href") or "")
                return
            key, value = attr.get("property") or attr.get("name"), attr.get("content")
            if key and (key.startswith("og:") or key in {"author","video:actor","video:duration"}) and value:
                self.values.setdefault(key, value)


def clean_interrupted(folder):
    for entry in folder.glob(".instagram-*"):
        if re.fullmatch(r"\.instagram-[a-z0-9_]{8}", entry.name) and entry.is_dir():
            if entry.resolve() != entry or getattr(entry.lstat(), "st_file_attributes", 0) & 1024:
                continue
            (entry / "video.mp4").unlink(missing_ok=True)
            try:
                entry.rmdir()
            except OSError:
                pass


def import_video(url, folder, limits, progress):
    max_bytes = limits["max_mib"] * 1024**2
    folder = Path(folder).resolve()
    deadline = time.monotonic() + 300

    def cancel():
        project_cancel.check()
        if (folder / CANCEL_FILE).exists():
            raise Cancelled("Importación de Instagram cancelada.")
        if time.monotonic() > deadline:
            raise ImportTimeout("Se agotó el tiempo de importación de Instagram (cinco minutos).")

    stage = "validación del enlace"
    try:
        url = canonical(url)
        stage = "consulta de la página"
        clean_interrupted(folder)
        conn, response = transfer(url, media=False, cancel=cancel)
        with closing(conn), closing(response):
            page = bytearray()
            for chunk in chunks(response, cancel):
                page.extend(chunk)
                if len(page) > 4 * 1024**2:
                    raise ValueError("La página de Instagram supera el límite de lectura.")
        stage = "validación de los metadatos"
        parser = VideoMetadata()
        parser.feed(page.decode("utf-8", "replace"))
        values = parser.values
        link = confirm_page(url, post_identity(url), values, parser.canonical_links)
        meta = {"platform": "instagram", "title": values.get("og:title", "")[:300],
                "author": (values.get("author") or values.get("video:actor") or values.get("og:video:actor", ""))[:200]}
        hint = values.get("video:duration") or values.get("og:video:duration") or ""
        if re.fullmatch(r"[0-9]{1,8}", hint):
            if int(hint) > limits["max_seconds"]:
                raise ValueError("El video de Instagram supera el límite de duración.")
            meta["duration"] = int(hint)
        try:
            allowed = permitted(target(link).hostname, True)
        except ValueError:
            allowed = False
        if not allowed:
            raise ValueError("Instagram devolvió un destino de video no permitido.")
        from ingest import _run, check_disk
        stage = "descarga del MP4"
        conn, response = transfer(link, media=True, cancel=cancel)
        with closing(conn), closing(response), tempfile.TemporaryDirectory(prefix=".instagram-", dir=folder) as staging:
            if response.getheader("Content-Type", "").split(";")[0].strip().lower() != "video/mp4":
                raise ValueError("Instagram no entregó un archivo MP4 compatible.")
            length = response.getheader("Content-Length")
            total = int(length) if length is not None and length.isdecimal() and len(length) <= 16 else None
            if length is not None and (total is None or total <= 0 or total > max_bytes):
                raise ValueError("El MP4 excede el límite de tamaño o no tiene un tamaño válido.")
            if total is None and not response.chunked:
                raise ValueError("Instagram no indicó cómo verificar que el MP4 esté completo.")
            check_disk(folder, total if total is not None else max_bytes)
            temp = Path(staging) / "video.mp4"
            done = 0
            last_report = 0
            progress(meta, done, total)
            with temp.open("wb") as output:
                for chunk in chunks(response, cancel):
                    done += len(chunk)
                    if done > max_bytes or (total is not None and done > total):
                        raise ValueError("El MP4 supera el tamaño permitido.")
                    output.write(chunk)
                    if time.monotonic() - last_report > .35:
                        progress(meta, done, total)
                        last_report = time.monotonic()
            if not done or (total is not None and done != total):
                raise ValueError("La descarga de Instagram quedó incompleta.")
            stage = "verificación del MP4"
            info = verify_mp4(temp, limits["max_seconds"], _run)
            cancel()
            ready = folder / "instagram.mp4"
            os.replace(temp, ready)
            return ready, {**meta, **info, "source": url, "size": done, "estimated_size": done,
                           "long": False, "url": True, "media_kind": "video", "render_from_sections": False}
    except ValueError:
        print(f"Importación de Instagram interrumpida durante la etapa: {stage}.", file=sys.stderr, flush=True)
        raise
    except OSError:
        print(f"Importación de Instagram interrumpida durante la etapa: {stage}.", file=sys.stderr, flush=True)
        raise ValueError("Studio no pudo guardar el video de Instagram en el proyecto. Comprueba el espacio y los permisos.") from None
    except Exception as exc:
        print(f"Error interno de Python en la etapa {stage} de Instagram ({type(exc).__name__}).",
              file=sys.stderr, flush=True)
        raise ValueError("Error interno de Python al importar desde Instagram. Consulta el Registro del trabajo y reintenta.") from None


def chunks(response, cancel):
    while True:
        cancel()
        try:
            data = response.read1(64 * 1024)
        except TimeoutError:
            cancel()
            raise ImportTimeout("Se agotó el tiempo de espera al descargar desde Instagram. Puedes reintentar.") from None
        except (OSError, http.client.HTTPException):
            raise ValueError("La descarga de Instagram se interrumpió antes de terminar. Puedes reintentar.") from None
        if not data:
            return
        yield data


def verify_mp4(path, max_seconds, run):
    import json
    try:
        result = run(["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-enable_drefs", "0", "-use_absolute_path", "0", "-f", "mov",
                      "-show_entries", "format=duration,format_name:stream=codec_type,width,height",
                      "-of", "json", str(path)], timeout=15)
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
        if ("mp4" not in data["format"]["format_name"].split(",") or min(video["width"],video["height"]) < 2
                or not math.isfinite(duration) or duration <= 0):
            raise ValueError
    except subprocess.TimeoutExpired:
        raise ImportTimeout("Se agotó el tiempo de verificación del MP4 de Instagram.") from None
    except (OSError, subprocess.SubprocessError, ValueError, StopIteration, TypeError, KeyError):
        raise ValueError("Instagram no entregó un MP4 de video válido.") from None
    if duration > max_seconds:
        raise ValueError("El video de Instagram supera el límite de duración.")
    return {"duration": duration, "width": video["width"], "height": video["height"], "has_video": True,
            "has_audio": any(stream["codec_type"] == "audio" for stream in data["streams"])}
