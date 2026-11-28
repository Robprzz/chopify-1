"""FFmpeg encoding progress and timings, without exposing command-line paths."""
import math
import project_cancel
import subprocess
import tempfile
import threading
import time


class Cancelled(ValueError):
    pass


class Progress:
    def __init__(self, emit=None, cancelled=None):
        self.emit = emit
        self.cancelled = cancelled or (lambda: False)
        self.stage = None
        self.since = time.monotonic()
        self.times = {}

    def update(self, stage, fraction=None, eta=None):
        now = time.monotonic()
        if stage != self.stage:
            if self.stage is not None:
                self.times[self.stage] = round(self.times.get(self.stage, 0) + now - self.since, 3)
            self.stage, self.since = stage, now
        if self.emit:
            self.emit({"stage": stage, "fraction": fraction, "eta": eta, "times": dict(self.times)})

    def check(self):
        project_cancel.check()
        if self.cancelled():
            raise Cancelled("Exportación cancelada. Se conservaron los clips terminados.")


def encode(cmd, cwd, timeout, duration, progress):
    """Read ffmpeg machine progress and keep stderr separately for diagnosis."""
    if progress.emit is None:
        return project_cancel.run(cmd, check=True, cwd=cwd, stderr=subprocess.PIPE, encoding="utf-8",
                              errors="backslashreplace", timeout=timeout)
    progress.check()
    with tempfile.TemporaryFile() as errors:
        proc = project_cancel.spawn([cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]],
                                cwd=cwd, stderr=errors, stdout=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="backslashreplace")
        timestamps = []
        failure = None

        def read():
            with proc.stdout:
                for line in proc.stdout:
                    key, _, value = line.strip().partition("=")
                    if key == "out_time_us":
                        try:
                            timestamps[:] = [max(0., int(value) / 1e6)]
                        except ValueError:
                            continue

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        started = time.monotonic()
        emitted = None
        try:
            while proc.poll() is None:
                progress.check()
                elapsed = time.monotonic() - started
                if elapsed > timeout:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                if timestamps and emitted != timestamps[0]:
                    emitted = timestamps[0]
                    fraction = min(1., emitted / duration)
                    eta = elapsed * (1 - fraction) / fraction if fraction >= .05 and elapsed >= 2 else None
                    progress.update("encode", fraction, round(eta) if eta is not None and math.isfinite(eta) else None)
                try:
                    proc.wait(timeout=.20)
                except subprocess.TimeoutExpired:
                    pass
        except BaseException as exc:
            failure = exc
            project_cancel.terminate(proc, grouped=project_cancel.active())
            proc.wait()
        finally:
            reader.join()
        errors.seek(max(0, errors.tell() - 65536))
        stderr = errors.read().decode("utf-8", "backslashreplace")
        if failure:
            if isinstance(failure, subprocess.TimeoutExpired):
                failure.stderr = stderr
            raise failure
        if proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, cmd, stderr=stderr)
        progress.update("encode", 1., 0)
        return subprocess.CompletedProcess(cmd, proc.returncode, stderr=stderr)
