"""Local provider transport with fixed destination and bounded reads."""
import http.client
import json
import re
import socket
import threading
import time
from typing import Protocol


OLLAMA_URL = "http://127.0.0.1:11434"
MAX_RESPONSE = 512 * 1024
MODEL_NAME = re.compile(r"[\w./:-]{1,128}", re.ASCII)


class ProviderError(Exception):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class Cancelled(Exception):
    pass


class Cancellation:
    def __init__(self):
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.interrupt = None

    def cancel(self):
        with self.lock:
            self.event.set()
            if self.interrupt:
                self.interrupt()

    def check(self):
        if self.event.is_set():
            raise Cancelled


class Provider(Protocol):
    name: str
    def models(self) -> list[dict]: ...
    def generate(self, model, messages, schema, timeout, cancellation) -> dict: ...


def local_model(name):
    return (isinstance(name, str) and bool(MODEL_NAME.fullmatch(name))
            and not re.search(r"(?:[-:/]cloud)(?:$|[-:/])", name, re.I))


def _remote(info):
    details = info.get("details")
    capabilities = info.get("capabilities")
    return bool(info.get("remote_host") or info.get("remote_model")
                or (isinstance(capabilities, list) and "cloud" in capabilities)
                or (isinstance(details, dict) and str(details.get("format", "")).lower() == "cloud"))


def _close(connection):
    if connection.sock:
        try:
            connection.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    connection.close()


class Ollama:
    name = "ollama"

    def _connection(self, timeout):
        return http.client.HTTPConnection("127.0.0.1", 11434, timeout=timeout)

    def _request(self, path, timeout, cancellation, payload=None):
        cancellation.check()
        conn = self._connection(min(timeout, 3))
        expired = threading.Event()
        transport = None

        def expire():
            expired.set()
            _close_socket(transport, conn)

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        with cancellation.lock:
            cancellation.check()
            cancellation.interrupt = lambda: _close(conn)
        timer.start()
        try:
            conn.connect()
            cancellation.check()
            if conn.sock:
                conn.sock.settimeout(timeout)
            body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
            conn.request("GET" if body is None else "POST", path, body, {"Content-Type": "application/json"})
            # Keep a reference even after getresponse detaches a connection:close socket.
            transport = conn.sock
            with cancellation.lock:
                cancellation.check()
                cancellation.interrupt = lambda: _close_socket(transport, conn)
            with conn.getresponse() as response:
                if response.status != 200:
                    if response.status == 404:
                        raise ProviderError("model_missing", "Ollama no encontró el modelo seleccionado.")
                    raise ProviderError("http", f"Ollama devolvió HTTP {response.status}.", response.status in (502, 503))
                data = response.read(MAX_RESPONSE + 1)
            cancellation.check()
            if expired.is_set():
                raise TimeoutError
            if len(data) > MAX_RESPONSE:
                raise ProviderError("response", "Ollama excedió el tamaño de respuesta admitido.")
            reply = json.loads(data)
            if not isinstance(reply, dict):
                raise ValueError
            return reply
        except (OSError, http.client.HTTPException) as exc:
            cancellation.check()
            if expired.is_set() or isinstance(exc, TimeoutError):
                raise ProviderError("timeout", "Ollama superó el tiempo máximo.") from None
            raise ProviderError("offline", "Ollama no está disponible en 127.0.0.1:11434.") from None
        except (UnicodeError, ValueError):
            cancellation.check()
            raise ProviderError("response", "Ollama devolvió una respuesta ilegible.", True) from None
        finally:
            timer.cancel()
            with cancellation.lock:
                cancellation.interrupt = None
            conn.close()

    def models(self):
        values = self._request("/api/tags", 3, Cancellation()).get("models")
        if not isinstance(values, list):
            raise ProviderError("response", "Ollama no devolvió una lista de modelos válida.")
        return [{"name": item["name"], "digest": item.get("digest") if
                 isinstance(item.get("digest"), str) and re.fullmatch(r"[a-f0-9]{64}", item["digest"]) else None}
                for item in values[:256] if isinstance(item, dict) and local_model(item.get("name"))
                and not _remote(item)]

    def generate(self, model, messages, schema, timeout, cancellation):
        if not local_model(model):
            raise ProviderError("model_missing", "Selecciona un modelo local instalado.")
        start = time.monotonic()
        info = self._request("/api/show", min(timeout, 3), cancellation, {"model": model})
        if _remote(info):
            raise ProviderError("remote", "Automation admite únicamente modelos locales.")
        timeout -= time.monotonic() - start
        if timeout <= 0:
            raise ProviderError("timeout", "Ollama superó el tiempo máximo.")
        reply = self._request("/api/chat", timeout, cancellation, {
            "model": model, "messages": messages, "stream": False, "think": False,
            "format": schema, "keep_alive": 0,
            "options": {"num_ctx": 6144, "num_predict": 1600, "temperature": 0},
        })
        try:
            if reply.get("done") is not True or reply.get("done_reason") == "length":
                raise ValueError
            data = json.loads(reply["message"]["content"])
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (KeyError, TypeError, ValueError):
            raise ProviderError("response", "Ollama devolvió JSON incompleto o inválido.", True) from None


def _close_socket(transport, connection):
    if transport:
        try:
            transport.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    _close(connection)
