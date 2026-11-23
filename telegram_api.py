"""Minimal outbound client for the official Telegram Bot API."""
import http.client
import json
from pathlib import Path
import re
import secrets
import ssl
import time

HOST = "api.telegram.org"
VIDEO_LIMIT = 49_000_000
METHODS = {"getMe", "getUpdates", "sendMessage", "sendVideo", "answerCallbackQuery"}


class TelegramError(RuntimeError):
    def __init__(self, message, *, retry=False, uncertain=False, unauthorized=False, delay=0):
        super().__init__(message)
        self.retry, self.uncertain, self.unauthorized, self.delay = retry, uncertain, unauthorized, delay


def valid_token(token):
    return isinstance(token, str) and bool(re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}", token))


class Client:
    def __init__(self, token):
        if not valid_token(token):
            raise ValueError("Formato de Bot Token inválido.")
        self._token = token

    def call(self, method, payload=None, video=None):
        if method not in METHODS or (video is not None and method != "sendVideo"):
            raise ValueError("Método Telegram no permitido.")
        url = f"/bot{self._token}/{method}"
        payload = payload or {}
        upload = method.startswith("send")
        dispatched = False
        conn = http.client.HTTPSConnection(HOST, timeout=32 if method == "getUpdates" else 45,
                                          context=ssl.create_default_context())
        try:
            conn.connect()
            if video is None:
                body = json.dumps(payload, ensure_ascii=False).encode()
                dispatched = True
                conn.request("POST", url, body=body, headers={"Content-Type": "application/json"})
            else:
                with Path(video).open("rb") as handle:
                    handle.seek(0, 2)
                    size = handle.tell()
                    handle.seek(0)
                    if not 0 < size <= VIDEO_LIMIT:
                        raise ValueError("El video de revisión supera el límite de Telegram.")
                    boundary = secrets.token_hex(16)
                    fields = b""
                    for key, value in payload.items():
                        value = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list, bool)) else str(value)
                        fields += (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode()
                    fields += (f'--{boundary}\r\nContent-Disposition: form-data; name="video"; filename="clip.mp4"\r\n'
                               'Content-Type: video/mp4\r\n\r\n').encode()
                    close = f"\r\n--{boundary}--\r\n".encode()
                    conn.putrequest("POST", url)
                    conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
                    conn.putheader("Content-Length", str(len(fields) + size + len(close)))
                    dispatched = True
                    conn.endheaders(fields)
                    deadline = time.monotonic() + 150
                    remaining = size
                    while remaining:
                        left = deadline - time.monotonic()
                        if left <= 0:
                            raise TimeoutError("Se agotó el tiempo de subida.")
                        conn.sock.settimeout(min(45, left))
                        data = handle.read(min(256 * 1024, remaining))
                        if not data:
                            raise OSError("El archivo cambió durante el envío.")
                        conn.send(data)
                        remaining -= len(data)
                    conn.sock.settimeout(30)
                    conn.send(close)
            response = conn.getresponse()
            status = response.status
            raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                raise ValueError("Respuesta demasiado grande.")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Respuesta inválida.")
        except (OSError, http.client.HTTPException, ValueError):
            uncertain = upload and dispatched
            raise TelegramError("No se pudo confirmar la respuesta de Telegram.", retry=not uncertain,
                                uncertain=uncertain) from None
        finally:
            conn.close()
        code = data.get("error_code", status)
        if status == 200 and data.get("ok") is True:
            return data.get("result")
        if code in (401, 404):
            raise TelegramError("Bot Token rechazado por Telegram.", unauthorized=True)
        if code == 429:
            delay = (data.get("parameters") or {}).get("retry_after", 60)
            raise TelegramError("Telegram limitó temporalmente la frecuencia.", retry=True,
                                delay=min(3600, max(1, delay)) if type(delay) is int else 60)
        if code == 409:
            raise TelegramError("Telegram detectó otro proceso de polling o un webhook activo.", retry=True, delay=30)
        if type(code) is int and code >= 500:
            raise TelegramError("Telegram devolvió un error temporal.", retry=not upload, uncertain=upload)
        raise TelegramError("Telegram rechazó la operación.")
