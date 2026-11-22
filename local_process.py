"""Run a bounded local media child that can be interrupted from a Studio job."""
from contextlib import contextmanager
import subprocess



@contextmanager
def use_resources(lock, cancellation):
    while not lock.acquire(timeout=.25):
        cancellation.check()
    try:
        cancellation.check()
        yield
    finally:
        lock.release()


def run(command, timeout, cancellation):
    cancellation.check()
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
        def stop_child():
            try:
                child.kill()
            except OSError:
                pass

        try:
            with cancellation.lock:
                cancellation.interrupt = stop_child
                cancellation.check()
            stdout, stderr = child.communicate(timeout=timeout)
            cancellation.check()
            return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
        except BaseException:
            stop_child()
            raise
        finally:
            with cancellation.lock:
                cancellation.interrupt = None
