"""Cooperative per-job cancellation, with process-tree isolation for media tools."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading


class Cancelled(BaseException):
    """Never let a stage treat cancellation as an ordinary retry or CUDA error."""


MESSAGE = "Proyecto cancelado. Los archivos y checkpoints válidos se conservaron."
_current = ContextVar("project_cancel", default=None)


class Job:
    def __init__(self, folder, ident):
        if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{32}", ident):
            raise ValueError("Identificador de ejecución no válido.")
        self.marker = Path(folder) / (".cancel-" + ident)

    def requested(self):
        return self.marker.exists()

    def request(self):
        try:
            with self.marker.open("x"):
                pass
        except FileExistsError:
            pass

    def clear(self):
        self.marker.unlink(missing_ok=True)

    def check(self):
        if self.requested():
            raise Cancelled(MESSAGE)


@contextmanager
def scope(job):
    token = _current.set(job)
    try:
        yield
    finally:
        _current.reset(token)


def active():
    return _current.get() is not None


def check():
    job = _current.get()
    if job is not None:
        job.check()


def spawn(command, **kwargs):
    check()
    if active():
        if os.name == "nt":
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def terminate(child, *, grouped):
    if not grouped and child.poll() is not None:
        return
    if grouped and os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
    elif grouped:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return
    if child.poll() is None:
        child.kill()


@contextmanager
def on_request(interrupt):
    job = _current.get()
    if job is None:
        yield
        return
    done = threading.Event()

    def observe():
        while not done.wait(.12):
            if job.requested():
                interrupt()
                return

    observer = threading.Thread(target=observe, daemon=True)
    observer.start()
    try:
        yield
    finally:
        done.set()
        observer.join()
        job.check()


@contextmanager
def watch(child):
    with on_request(lambda: terminate(child, grouped=True)):
        yield


def ollama(path, payload, timeout):
    """Interrupt this project's request using the existing local transport."""
    from editorial_providers import Cancellation, Ollama
    import studio_json

    normalized = json.loads(studio_json.dumps(payload))
    request = Cancellation()
    with on_request(request.cancel):
        return Ollama()._request(path, timeout, request, normalized)


def run(command, **kwargs):
    if not active():
        return subprocess.run(command, **kwargs)
    input_data = kwargs.pop("input", None)
    capture = kwargs.pop("capture_output", False)
    timeout = kwargs.pop("timeout", None)
    required = kwargs.pop("check", False)
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if input_data is not None:
        kwargs["stdin"] = subprocess.PIPE
    with spawn(command, **kwargs) as child, watch(child):
        try:
            stdout, stderr = child.communicate(input_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate(child, grouped=True)
            stdout, stderr = child.communicate()
            raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr) from None
        except BaseException:
            terminate(child, grouped=True)
            raise
        check()
        if required and child.returncode:
            raise subprocess.CalledProcessError(child.returncode, command, stdout, stderr)
        return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
