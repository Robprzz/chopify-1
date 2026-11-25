"""Per-user installation preferences, local dependency detection and service identity."""
from contextlib import contextmanager
import hashlib
import hmac
import importlib.metadata
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

from secure_vault import PROFILES, Vault, atomic_bytes


DEFAULTS = {"projects_dir": "", "ffmpeg_dir": "", "ollama_auto_start": False,
            "open_browser": True, "tiktok_profile": "production", "setup_complete": False}
PACKAGES = {"yt_dlp": "yt-dlp", "faster_whisper": "faster-whisper", "cv2": "opencv-python", "numpy": "numpy"}
HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NONCE = re.compile(r"[a-f0-9]{48}")


def user_directory():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Chopify Studio"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "chopify-studio"


@contextmanager
def lock_file(path, timeout=15):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if not stream.tell():
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + timeout
        if os.name == "nt":
            import msvcrt

            def acquire():
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(stream, fcntl.LOCK_UN)
        while True:
            try:
                acquire()
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError("Otra operación de Chopify sigue en curso. Vuelve a intentarlo.") from None
                time.sleep(.1)
        try:
            yield
        finally:
            release()


def local_folder(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ValueError("Indica una carpeta local válida.")
    if not value:
        return ""
    path = Path(value).expanduser()
    if not path.is_absolute() or str(path).startswith(("//", "\\\\")):
        raise ValueError("Usa una ruta local absoluta.")
    resolved = path.resolve()
    if str(resolved).startswith(("//", "\\\\")):
        raise ValueError("Usa una carpeta local en este equipo.")
    return str(resolved)


class Settings:
    def __init__(self, checkout, directory=None):
        self.checkout = Path(checkout).resolve()
        ident = hashlib.sha256(os.path.normcase(str(self.checkout)).encode()).hexdigest()[:24]
        base = (Path(directory) if directory else user_directory()).resolve()
        if base.is_relative_to(self.checkout):
            raise ValueError("Los ajustes de instalación deben guardarse fuera del repositorio.")
        self.directory = base / ident
        self.path = self.directory / "settings.json"
        self.logs = self.directory / "logs"
        self.vault = Vault(self.directory / "protected")

    @staticmethod
    def normalize(data, defaults=None):
        if not isinstance(data, dict) or not data.keys() <= DEFAULTS.keys():
            raise ValueError("Se recibieron ajustes de sistema desconocidos.")
        result = {**(defaults or DEFAULTS), **data}
        for field in ("projects_dir", "ffmpeg_dir"):
            result[field] = local_folder(result[field])
        for field in ("ollama_auto_start", "open_browser", "setup_complete"):
            if type(result[field]) is not bool:
                raise ValueError("Las preferencias de arranque deben ser booleanas.")
        if result["tiktok_profile"] not in PROFILES:
            raise ValueError("Perfil de TikTok no válido.")
        return result

    def read(self):
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError
            return self.normalize(json.loads(raw))
        except FileNotFoundError:
            return DEFAULTS.copy()
        except (OSError, ValueError, TypeError):
            raise ValueError("No se pudieron leer los ajustes locales de Chopify.") from None

    def save(self, changes):
        with lock_file(self.directory / "settings.lock", 3):
            settings = self.normalize(changes, self.read())
            atomic_bytes(self.path, json.dumps(settings, ensure_ascii=False, indent=2).encode())
        return settings

    def projects(self, prefs):
        return Path(prefs["projects_dir"] or self.checkout / "projects")

    def identity(self):
        path = self.directory / "instance.key"
        with lock_file(self.directory / "identity.lock", 3):
            if not path.exists():
                atomic_bytes(path, secrets.token_bytes(32))
            with path.open("rb") as handle:
                key = handle.read(33)
            if len(key) != 32:
                raise ValueError("No se pudo verificar la identidad de esta instalación de Studio.")
            return key


def instance_proof(key, nonce):
    if not isinstance(nonce, str) or not NONCE.fullmatch(nonce):
        raise ValueError("Identificador de comprobación no válido.")
    return hmac.new(key, ("Chopify Studio HTTP 1/" + nonce).encode(), hashlib.sha256).hexdigest()


def venv_python(checkout, *, windowless=False):
    checkout = Path(checkout)
    if os.name == "nt":
        return checkout / ".venv" / "Scripts" / ("pythonw.exe" if windowless else "python.exe")
    return checkout / ".venv" / "bin" / "python"


def dependency_report():
    packages = {}
    for module, name in PACKAGES.items():
        try:
            packages[module] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[module] = None
    return {"version": ".".join(map(str, sys.version_info[:3])), "supported": sys.version_info >= (3, 11),
            "venv": sys.prefix != sys.base_prefix, "interpreter": sys.executable, "packages": packages}


def _version(executable):
    try:
        result = subprocess.run([str(executable), "-version"], capture_output=True, encoding="utf-8",
                                errors="backslashreplace",
                                timeout=5, check=True, creationflags=HIDDEN)
        return result.stdout.splitlines()[0][:200]
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def ffmpeg_checks(path, *, require_pair=False):
    suffix = ".exe" if os.name == "nt" else ""
    path = Path(path)
    if not path.is_absolute():
        return None
    files = {name: path / (name + suffix) for name in ("ffmpeg", "ffprobe")}
    exists = {name: file.is_file() for name, file in files.items()}
    if require_pair and not all(exists.values()):
        return None
    tools = {}
    for name, file in files.items():
        version = _version(file) if exists[name] else None
        valid = bool(version and version.lower().startswith(name + " version "))
        tools[name] = {"filename": file.name, "exists": exists[name], "version": version if valid else None}
    return {"directory": str(path), "tools": tools}


def ffmpeg_directory(path):
    if checks := ffmpeg_checks(path, require_pair=True):
        versions = {name: tool["version"] for name, tool in checks["tools"].items()}
        if all(versions.values()):
            return {"directory": checks["directory"], "versions": versions}
    return None


def ffmpeg_locations(checkout):
    yield Path(checkout) / "tools" / "ffmpeg" / "bin"
    if os.name != "nt":
        return
    appdata = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    yield appdata / "Microsoft" / "WinGet" / "Links"
    yield appdata / "Programs" / "FFmpeg" / "bin"
    for folder in sorted((appdata / "Microsoft" / "WinGet" / "Packages").glob("Gyan.FFmpeg_*"))[:20]:
        yield from sorted(folder.glob("*/bin"))[:10]
    yield Path(os.environ.get("SystemDrive", "C:") + "\\") / "ffmpeg" / "bin"
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        yield Path(program_files) / "ffmpeg" / "bin"


def find_ffmpeg(checkout, saved="", *, search_path=None):
    candidates = [("saved", Path(saved))] if saved else []
    # Search explicit PATH directories, never the working directory or an empty PATH element.
    for entry in (os.environ.get("PATH", "") if search_path is None else search_path).split(os.pathsep):
        candidate = Path(entry.strip('"'))
        if candidate.is_absolute():
            candidates.append(("path", candidate))
    candidates.extend(("conventional", path) for path in ffmpeg_locations(checkout))
    seen = set()
    for origin, path in candidates:
        if path in seen:
            continue
        seen.add(path)
        if tools := ffmpeg_directory(path):
            return {**tools, "source": origin}
    return None


def tool_path(report, environment):
    if report:
        path = report["directory"]
        others = [item for item in environment.get("PATH", "").split(os.pathsep) if item and item != path]
        environment["PATH"] = os.pathsep.join([path, *others])


def executable_on_path(name):
    # Windows shutil.which may search the working directory first; pass absolute candidates.
    suffix = ".exe" if os.name == "nt" else ""
    for part in os.environ.get("PATH", "").split(os.pathsep):
        directory = Path(part.strip('"'))
        if directory.is_absolute():
            file = directory / (name + suffix)
            if file.is_file() and os.access(file, os.X_OK):
                return file
    return None
