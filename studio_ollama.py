"""Diagnose and optionally start an Ollama child restricted to local inference."""
import os
from pathlib import Path
import socket
import subprocess
import threading
import time

from editorial_providers import Cancellation, Ollama, ProviderError
from studio_settings import HIDDEN, executable_on_path


def find_ollama():
    path = executable_on_path("ollama")
    if not path and os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        installed = base / "Programs" / "Ollama" / "ollama.exe"
        if installed.is_file():
            return installed
    return path


class Manager:
    def __init__(self, log=None, provider=None):
        self.provider = provider or Ollama()
        self.child = None
        self.log = log
        self.lock = threading.RLock()
        self.last_error = ""
        self.closed = False

    def _owned(self):
        return self.child is not None and self.child.poll() is None

    def status(self, model=""):
        installed = find_ollama()
        try:
            version = self.provider._request("/api/version", 2, Cancellation()).get("version")
            if not isinstance(version, str) or not 1 <= len(version) <= 64:
                raise ProviderError("response", "El servicio del puerto 11434 no devolvió una versión de Ollama.")
            models = [item["name"] for item in self.provider.models() if item.get("digest")]
            error = "" if not model or model in models else "El modelo configurado no está instalado localmente."
            online = True
        except ProviderError as exc:
            version, models, online = None, [], False
            error = self.last_error or str(exc)
        owned = self._owned()
        return {"installed": bool(installed), "server": online, "version": version, "models": models,
                "model": model or None, "model_available": model in models if model else None,
                "owned": owned, "privacy": "local_cloud_disabled" if owned else "external_unverified" if online else "offline",
                "last_error": error}

    def ensure(self, allowed, model=""):
        with self.lock:
            status = self.status(model)
            if self.closed or not allowed or status["server"] or self._owned():
                return status
            executable = find_ollama()
            if not executable:
                self.last_error = "No se encontró la instalación local de Ollama."
                return self.status(model)
            try:
                with socket.create_connection(("127.0.0.1", 11434), timeout=.3):
                    self.last_error = "El puerto 11434 está ocupado por un servicio que no se pudo verificar."
                    return self.status(model)
            except ConnectionRefusedError:
                pass
            except OSError:
                self.last_error = "No se pudo confirmar que el puerto local de Ollama esté disponible."
                return self.status(model)
            try:
                if self.log:
                    self.log.parent.mkdir(parents=True, exist_ok=True)
                env = {**os.environ, "OLLAMA_HOST": "127.0.0.1:11434", "OLLAMA_NO_CLOUD": "1"}
                with self.log.open("ab") if self.log else open(os.devnull, "ab") as log:
                    self.child = subprocess.Popen([str(executable), "serve"], stdin=subprocess.DEVNULL,
                                                  stdout=log, stderr=subprocess.STDOUT, env=env, creationflags=HIDDEN)
            except OSError:
                self.last_error = "No se pudo iniciar Ollama localmente."
                return self.status(model)
            self.last_error = ""
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and self._owned():
            status = self.status(model)
            if status["server"]:
                return status
            time.sleep(.25)
        self.last_error = "Ollama no confirmó su disponibilidad. Consulta el registro local."
        return self.status(model)

    def close(self):
        with self.lock:
            self.closed = True
            if self._owned():
                try:
                    self.child.terminate()
                    self.child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait(timeout=3)
                except OSError:
                    pass
