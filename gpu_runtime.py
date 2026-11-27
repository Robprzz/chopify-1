"""Discover the NVIDIA wheel DLLs of the active Windows virtual environment."""
import os
from pathlib import Path
import re
import sys
import sysconfig
import threading


PACKAGES = ("cuda_runtime", "cublas", "cudnn")
_handles = {}
_lock = threading.Lock()


def wheel_directories(prefix, purelib):
    prefix = Path(prefix).resolve()
    library = Path(purelib).resolve()
    if not library.is_relative_to(prefix):
        return {}
    directories = {}
    for name in PACKAGES:
        path = library / "nvidia" / name / "bin"
        if path.is_dir() and path.resolve() == path:
            directories[name] = path
    return directories


def configure():
    """Register only local wheel directories for the lifetime of this process."""
    if sys.platform != "win32":
        return None
    directories = (wheel_directories(sys.prefix, sysconfig.get_path("purelib"))
                   if sys.prefix != sys.base_prefix else {})
    missing = [name for name in PACKAGES if name not in directories]
    if missing:
        return "Faltan las bibliotecas NVIDIA en .venv: " + ", ".join(missing) + "."
    with _lock:
        for directory in directories.values():
            if directory not in _handles:
                try:
                    _handles[directory] = os.add_dll_directory(str(directory))
                except OSError:
                    return "Windows no pudo registrar las bibliotecas NVIDIA de .venv."
        paths = [str(path) for path in directories.values()]
        known = {path.casefold() for path in paths}
        current = os.environ.get("PATH", "")
        updated = os.pathsep.join(paths + [
            part for part in current.split(os.pathsep) if part and part.strip('"').casefold() not in known])
        if current != updated:
            os.environ["PATH"] = updated
    return None


def cuda_error(error):
    """Recognize CUDA-library/driver/VRAM failures, excluding ordinary I/O."""
    if isinstance(error, OSError):
        return getattr(error, "winerror", None) in {126, 127, 193, 1114} and bool(
            re.search(r"cublas|cudnn|cudart|ctranslate2|_ext", str(error), re.I))
    if not isinstance(error, (RuntimeError, ImportError, ValueError)):
        return False
    message = str(error).lower()
    return bool(re.search(
        r"^(?:cuda (?:error|failed|driver|runtime|out of memory)|driver unavailable\b|"
        r"no cuda (?:device|driver)|(?:cublas|cudnn)(?:_| (?:failed|error|status)))|"
        r"dll load failed while importing _ext\b|cannot (?:load|resolve) symbol (?:cublas|cudnn|cuda)|"
        r"(?:cublas|cudnn|cudart)[^/\\\s]*\.(?:dll|so)\b|"
        r"requested .*compute type.*(?:target device|backend)", message))


def candidate():
    """A device count is preflight only. Inference must still succeed."""
    if issue := configure():
        return issue
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return None
    except (RuntimeError, OSError, ImportError) as exc:
        if not cuda_error(exc):
            raise
    return "CUDA no disponible en CTranslate2."
