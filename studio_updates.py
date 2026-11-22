"""Explicit fast-forward updates for the authorized Chopify checkout."""
import os
from pathlib import Path
import re
import subprocess
import tempfile

from studio_settings import HIDDEN, executable_on_path, lock_file


BRANCH = "feature/anqora-media-studio-v2"
REF = f"refs/heads/{BRANCH}"
URL = "https://github.com/Robprzz/chopify-1.git"
SHA = re.compile(r"[a-f0-9]{40}")


def find_git():
    if executable := executable_on_path("git"):
        return executable
    if os.name != "nt":
        return None
    appdata = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    candidates = [appdata / "Programs" / "Git" / "cmd" / "git.exe"]
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidates.append(Path(program_files) / "Git" / "cmd" / "git.exe")
    for install in sorted((appdata / "GitHubDesktop").glob("app-*"), reverse=True)[:10]:
        for folder in ("cmd", "bin", "mingw64/bin"):
            candidates.append(install / "resources" / "app" / "git" / folder / "git.exe")
    return next((path for path in candidates if path.is_file()), None)


class Repository:
    def __init__(self, checkout, lock, remote=URL):
        self.checkout = Path(checkout)
        self.lock = lock
        self.remote = remote

    def _git(self, *args, hooks=None, timeout=15, permitted=(0,)):
        executable = find_git()
        if not executable:
            raise ValueError("Git no está disponible. Comprueba su instalación en el equipo.")
        flags = ["-c", "protocol.ext.allow=never", "-c", "core.fsmonitor=false", "-c", "maintenance.auto=false"]
        if hooks:
            flags += ["-c", f"core.hooksPath={hooks}"]
        # Keep Git and any SSH client noninteractive. Errors use fixed messages.
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
        try:
            result = subprocess.run([str(executable), *flags, *args], cwd=self.checkout,
                                    env=env, capture_output=True, text=True, errors="replace",
                                    timeout=timeout, creationflags=HIDDEN)
        except (OSError, subprocess.SubprocessError):
            raise ValueError("Git no respondió. Revisa tu instalación o la conexión a GitHub.") from None
        if result.returncode not in permitted:
            raise ValueError("Git no pudo completar la operación. Revisa la rama y el acceso a GitHub.") from None
        return result.stdout.strip(), result.returncode

    def local(self):
        branch, _ = self._git("branch", "--show-current")
        installed, _ = self._git("rev-parse", "HEAD")
        if not SHA.fullmatch(installed):
            raise ValueError("No se pudo identificar el commit instalado.")
        dirty, _ = self._git("status", "--porcelain=v1", "--untracked-files=normal")
        return {"branch": branch or None, "installed": installed, "dirty": bool(dirty)}

    def advertised(self):
        address, _ = self._git("ls-remote", "--get-url", self.remote)
        if address != self.remote:
            raise ValueError("Git está redirigiendo la URL autorizada. Revisa su configuración antes de actualizar.")
        line, _ = self._git("ls-remote", "--exit-code", self.remote, REF, timeout=25)
        fields = line.split()
        if len(fields) != 2 or fields[1] != REF or not SHA.fullmatch(fields[0]):
            raise ValueError("GitHub no confirmó el commit publicado de la rama de desarrollo.")
        return fields[0]

    def check(self):
        info = self.local()
        remote = self.advertised()
        return {**info, "remote": remote, "available": info["installed"] != remote,
                "eligible": info["branch"] == BRANCH and not info["dirty"]}

    def apply(self, installed, advertised):
        if not all(isinstance(sha, str) and SHA.fullmatch(sha) for sha in (installed, advertised)):
            raise ValueError("Confirma los dos commits mostrados por Studio.")
        with lock_file(self.lock, 3), tempfile.TemporaryDirectory(prefix="chopify-empty-hooks-") as hooks:
            info = self.local()
            if info["branch"] != BRANCH or info["dirty"] or info["installed"] != installed:
                raise ValueError("La actualización exige la rama de desarrollo, un checkout limpio y la versión confirmada.")
            if self.advertised() != advertised:
                raise ValueError("La rama remota cambió. Comprueba la versión disponible otra vez.")
            if installed == advertised:
                return {"installed": installed}
            self._git("fetch", "--no-tags", "--no-recurse-submodules", self.remote, REF, hooks=hooks, timeout=80)
            fetched, _ = self._git("rev-parse", "FETCH_HEAD")
            if fetched != advertised:
                raise ValueError("El commit descargado no coincide con la versión confirmada.")
            _, code = self._git("merge-base", "--is-ancestor", installed, fetched, permitted=(0, 1))
            if code:
                raise ValueError("La rama diverge o contiene commits locales. Revisa el historial en GitHub Desktop.")
            if self.local() != info:
                raise ValueError("El checkout cambió durante la descarga. Conserva tus cambios y vuelve a comprobar.")
            self._git("merge", "--ff-only", "--no-edit", fetched, hooks=hooks, timeout=40)
            return {"installed": fetched}
