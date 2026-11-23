"""Background Telegram operations for a single explicitly paired Studio owner."""
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from editorial_content import COPY_FIELDS
from editorial_semantic import eligible
import projects
from secure_vault import Vault, VaultError
from studio_settings import lock_file
import subtitle_quality
from telegram_api import Client, TelegramError, VIDEO_LIMIT, valid_token
from telegram_store import DEFAULTS, MAX_ATTEMPTS, Store, capture_source, normalize

MENU = {"Estado": "/status", "En vivo": "/live", "Clips": "/clips",
        "Revisión": "/pending", "Cola": "/queue", "Estadísticas": "/stats", "Ayuda": "/help"}
ACTIONS = {"a": "approved", "r": "rejected", "c": "correction", "p": "postponed"}
DECISIONS = {"approved": "Aprobado editorialmente", "rejected": "Rechazado",
             "correction": "Corrección solicitada", "postponed": "Pospuesto"}
HELP = ("/status · /live · /clips · /pending · /queue · /stats\n"
        "/capture ENLACE (autorizado en Studio)\n/stop ID (pide confirmación)\n"
        "/edit TEXTO (después de tocar Editar)\n/start abre el menú. Las decisiones quedan en Automation.")


def buttons(*rows):
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]}


def review_buttons(key):
    return buttons((("Aprobar", "a:" + key), ("Rechazar", "r:" + key), ("Pedir corrección", "c:" + key)),
                   (("Editar título", "t:" + key), ("Editar descripción", "d:" + key)),
                   (("Detalles", "i:" + key), ("Ver video", "v:" + key), ("Posponer", "p:" + key)))


def quiet(prefs, zone, now=None):
    start, end = prefs["quiet_start"], prefs["quiet_end"]
    if not start:
        return False
    clock = (now or datetime.now(ZoneInfo(zone))).strftime("%H:%M")
    return start <= clock < end if start < end else clock >= start or clock < end


@contextmanager
def upload_copy(original, directory, duration, limit=None):
    limit = VIDEO_LIMIT if limit is None else limit
    if original.stat().st_size <= limit:
        yield original
        return
    if not 0 < duration <= 600 or shutil.disk_usage(directory).free < 128 * 1024**2:
        raise ValueError("Espacio o duración insuficientes para una copia de revisión.")
    rate = min(1200, int(limit * 8 * .78 / (duration * 1000)) - 64)
    if rate < 60:
        raise ValueError("El clip es demasiado largo para comprimirlo con calidad de revisión.")
    with tempfile.TemporaryDirectory(dir=directory, prefix="telegram-review-") as work:
        clip = Path(work) / "review.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
                        "-f", "mov", "-threads", "1", "-i", str(original), "-map", "0:v:0", "-map", "0:a:0",
                        "-vf", "scale=720:720:force_original_aspect_ratio=decrease:force_divisible_by=2",
                        "-filter_threads", "1", "-c:v", "libx264", "-threads", "1", "-preset", "veryfast",
                        "-b:v", f"{rate}k", "-maxrate", f"{rate}k", "-bufsize", f"{2 * rate}k",
                        "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(clip)],
                       timeout=150, check=True, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        seconds, tracks = subtitle_quality.probe_media(clip)
        if clip.stat().st_size > limit or abs(seconds - duration) > .4 or "audio" not in tracks:
            raise ValueError("La copia de revisión no conservó el video completo dentro del límite.")
        yield clip


class Gateway:
    def __init__(self, app, installation, client_factory=Client):
        self.app, self.factory = app, client_factory
        self.installation = installation
        self.store = None
        self.vault = None
        self.token = None
        self.connection = "disabled"
        self.generation = 0
        self.pairing = self.confirmation = self.editing = None
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.threads = []
        self.observed = {}
        self.had_outage = False
        self.logged_outage = None
        self.startup = True
        if installation:
            scope = hashlib.sha256(os.path.normcase(str(Path(app.root).resolve())).encode()).hexdigest()[:24]
            self.directory = installation.directory / "telegram" / scope
            self.vault = Vault(self.directory / "protected", installation.vault.protector)
            try:
                self.store = Store(self.directory / "telegram.sqlite")
            except (OSError, sqlite3.Error):
                self.connection = "local_error"
                return
            try:
                self.token = (self.vault.read("telegram", "bot") or {}).get("token")
                if not valid_token(self.token):
                    self.token = None
            except VaultError:
                self.connection = "vault_error"
                self.store.record("No se pudo cargar el Bot Token protegido.")
            if self.store.state()["prefs"]["enabled"] and self.token:
                self.connection = "connecting"

    def start(self):
        if self.store and self._enabled() and not any(thread.is_alive() for thread in self.threads):
            thread = threading.Thread(target=self._run, name="telegram-poll", daemon=True)
            self.threads.append(thread)
            thread.start()

    def close(self):
        self.stopping.set()
        self.wake.set()

    def _enabled(self):
        return not self.stopping.is_set() and self.token and self.store.state()["prefs"]["enabled"]

    def status(self):
        data = self.store.state() if self.store else {"prefs": DEFAULTS, "chat_id": None}
        with self.lock:
            pairing = self.pairing
            expires = max(0, int(pairing[1] - time.monotonic())) if pairing else 0
            return {"settings": data["prefs"], "has_token": bool(self.token), "paired": data["chat_id"] is not None,
                    "connection": self.connection, "pairing_seconds": expires,
                    "managed": self.store is not None, "installed": self.installation is not None,
                    **(self.store.diagnostics() if self.store else {"log": [], "deliveries": {}})}

    def control(self, action, body):
        if self.store is None:
            raise ValueError("Inicia Studio con una instalación configurada para usar Telegram.")
        if action == "settings":
            if not body.keys() <= {"settings", "token"}:
                raise ValueError("Preferencias de Telegram inválidas.")
            with self.lock:
                prefs = normalize(body.get("settings", self.store.state()["prefs"]))
                token = body.get("token", self.token)
                if token is not None and not valid_token(token):
                    raise ValueError("Formato de Bot Token inválido.")
                if prefs["enabled"] and not token:
                    raise ValueError("Guarda primero un Bot Token protegido.")
                rotate = token != self.token
                if rotate:
                    if token and not self.vault.available():
                        raise VaultError("No se puede guardar el Bot Token sin DPAPI.")
                    # Cancel the old recipient before a new credential can
                    # survive a crash on disk.
                    self.store.configure(self.store.state()["prefs"], rotate=True)
                    self.generation += 1
                    self.reset_interaction()
                    if token:
                        self.vault.write("telegram", "bot", {"token": token})
                    else:
                        self.vault.delete("telegram", "bot")
                self.store.configure(prefs)
                self.token = token
                self.generation += 1
                self.connection = "connecting" if prefs["enabled"] else "disabled"
                self.wake.set()
                self.start()
            return self.status()
        if body:
            raise ValueError("Acción Telegram inválida.")
        if action == "pair":
            with self.lock:
                if not self._enabled() or self.store.state()["chat_id"] is not None:
                    raise ValueError("Activa Telegram y revoca la cuenta existente antes de vincular.")
                self.pairing = (secrets.token_hex(8), time.monotonic() + 300)
                return {"code": self.pairing[0], "expires_in": 300}
        if action == "revoke":
            with self.lock:
                self.store.revoke()
                self.reset_interaction()
                self.generation += 1
                self.store.record("Vinculación revocada desde Studio.")
            return self.status()
        if action == "test":
            if not self.token:
                raise ValueError("Guarda primero el Bot Token.")
            self.factory(self.token).call("getMe")
            self.store.record("getMe confirmado por Telegram.")
            return {"ok": True}
        if action == "notify":
            with self.lock:
                if not self._enabled() or self.store.state()["chat_id"] is None:
                    raise ValueError("Vincula una cuenta y activa Telegram.")
                if not self.store.enqueue("test", {"text": "Anqora Media Studio · notificación de prueba."}, 20, cooldown=10):
                    raise ValueError("La cola está llena o ya se solicitó una prueba hace menos de 10 segundos.")
                self.wake.set()
            return {"queued": True}
        raise ValueError("Acción Telegram desconocida.")

    def reset_interaction(self):
        self.pairing = self.confirmation = self.editing = None
        self.observed.clear()
        self.startup = True

    def _run(self):
        while not self.stopping.is_set():
            try:
                with lock_file(self.installation.directory / "telegram.lock", timeout=1):
                    self.store.recover()
                    sender = threading.Thread(target=self._deliver, name="telegram-outbox", daemon=True)
                    sender.start()
                    self._poll()
                    sender.join()
                    return
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                if self.connection != "local_error":
                    self.connection = "local_error"
                    try:
                        self.store.record("Esperando acceso exclusivo a Telegram en esta instalación.")
                    except sqlite3.Error:
                        pass
                if self.stopping.wait(3):
                    return

    def _poll(self):
        delay = 1
        client = None
        previous_token = None
        while not self.stopping.is_set():
            try:
                with self.lock:
                    generation = self.generation
                    token = self.token if self._enabled() else None
                    offset = self.store.state()["offset"]
                if not token:
                    self.wake.wait(1)
                    self.wake.clear()
                    continue
                if token != previous_token:
                    client = self.factory(token)
                    previous_token = token
                updates = client.call("getUpdates", {"offset": offset, "timeout": 20, "limit": 20,
                                                      "allowed_updates": ["message", "callback_query"]})
                if not isinstance(updates, list):
                    raise TelegramError("Telegram devolvió una lista de mensajes inválida.", retry=True)
                with self.lock:
                    if generation != self.generation:
                        continue
                    if self.had_outage:
                        self.store.record("Conexión con Telegram recuperada.")
                        data = self.store.state()
                        if data["chat_id"] is not None and data["prefs"]["alerts"]:
                            self.store.enqueue("connection-recovered", {"text": "Conexión de Telegram recuperada."}, 15, cooldown=900)
                    self.connection, self.had_outage, self.logged_outage = "online", False, None
                delay = 1
                for update in updates:
                    if self.stopping.is_set():
                        return
                    with self.lock:
                        if generation != self.generation:
                            break
                        ident = update.get("update_id") if isinstance(update, dict) else None
                        if type(ident) is not int or not 0 <= ident < 2**53 or not self.store.claim_update(ident):
                            continue
                    try:
                        self.handle(client, update, generation)
                    except TelegramError as exc:
                        self.store.record("No se pudo responder a un mensaje · " + str(exc))
                    except (ValueError, OSError, sqlite3.Error, TypeError, KeyError, RuntimeError):
                        self.store.record("No se pudo procesar una operación de Telegram.")
            except (TelegramError, sqlite3.Error, OSError) as exc:
                message = str(exc) if isinstance(exc, TelegramError) else "No se pudo leer la configuración local de Telegram."
                with self.lock:
                    if generation != self.generation:
                        continue
                    self.connection = "invalid_token" if getattr(exc, "unauthorized", False) else "reconnecting"
                    self.had_outage = True
                    if self.logged_outage != message:
                        try:
                            self.store.record(message)
                        except sqlite3.Error:
                            pass
                        self.logged_outage = message
                self.wake.wait(max(delay, getattr(exc, "delay", 0), 30 if getattr(exc, "unauthorized", False) else 0))
                self.wake.clear()
                delay = min(delay * 2, 60)

    @staticmethod
    def identity(update):
        callback = update.get("callback_query")
        if callback is not None and not isinstance(callback, dict):
            return None
        message = callback.get("message") if callback is not None else update.get("message")
        if not isinstance(message, dict):
            return None
        user = callback.get("from") if callback is not None else message.get("from")
        chat = message.get("chat")
        if not isinstance(user, dict) or not isinstance(chat, dict) or chat.get("type") != "private" or user.get("is_bot") is not False:
            return None
        ident, chat_id = user.get("id"), chat.get("id")
        if type(ident) is not int or not 0 < ident < 2**52 or type(chat_id) is not int or chat_id != ident:
            return None
        return ident, chat_id, message, callback

    def handle(self, client, update, generation=None):
        identity = self.identity(update)
        if identity is None:
            return
        user, chat, message, callback = identity
        text = message.get("text", "")
        if not isinstance(text, str) or len(text) > 4096:
            return
        words = text.strip().split(maxsplit=1)
        command = words[0].lower() if words else ""
        value = words[1].strip() if len(words) > 1 else ""
        with self.lock:
            if generation is not None and generation != self.generation:
                return
            owner = self.store.state()
            if owner["user_id"] != user or owner["chat_id"] != chat:
                valid = (not callback and command == "/pair" and self.pairing and owner["user_id"] is None
                         and time.monotonic() < self.pairing[1] and re.fullmatch(r"[a-f0-9]{16}", value)
                         and secrets.compare_digest(value, self.pairing[0]))
                if not valid or not self.store.pair(user, chat):
                    return
                self.pairing = None
                self.store.record("Cuenta privada vinculada mediante código temporal.")
                command = "/start"
            stamp = message.get("date")
            if type(stamp) is int and time.time() - stamp > 180 and command in {"/capture", "/stop", "/edit"}:
                return
        if callback:
            ident, data = callback.get("id"), callback.get("data")
            if not isinstance(ident, str) or len(ident) > 128 or not isinstance(data, str) or len(data) > 64:
                return
            try:
                client.call("answerCallbackQuery", {"callback_query_id": ident})
            except TelegramError:
                pass
            try:
                if generation is not None:
                    self._delivery_guard(generation, chat)
                self._callback(client, chat, data)
            except (ValueError, OSError, sqlite3.Error):
                self._say(client, chat, "La acción no pudo aplicarse. Abre el clip o la sesión actual.")
        else:
            try:
                if generation is not None:
                    self._delivery_guard(generation, chat)
                self._command(client, chat, command, value)
            except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
                if isinstance(exc, TelegramError):
                    raise
                self._say(client, chat, "No se pudo completar la operación. Revisa la configuración y el estado en Studio.")

    @staticmethod
    def _say(client, chat, text, keyboard=None):
        body = {"chat_id": chat, "text": text[:3800], "link_preview_options": {"is_disabled": True}}
        if keyboard:
            body["reply_markup"] = keyboard
        return client.call("sendMessage", body)

    def _command(self, client, chat, command, value):
        if command == "/start":
            return self._say(client, chat, "Chopify · Centro de operaciones",
                             buttons(tuple(MENU.items())[:3], tuple(MENU.items())[3:6], tuple(MENU.items())[6:]))
        if command == "/help":
            return self._say(client, chat, HELP)
        if command == "/capture":
            source = capture_source(value)
            if source not in self.store.state()["prefs"]["sources"]:
                return self._say(client, chat, "Autoriza este enlace primero en Telegram Control Center de Studio.")
            minimum = self.app.automation_settings.read()["settings"]["minimum_clip_score"]
            session = self.app.start_live(source, projects.validate_options({"live_threshold": max(6, minimum)}))
            self.store.record("Captura Live iniciada por Telegram.")
            return self._say(client, chat, f"Sesión Live iniciada · {session.id}")
        if command == "/stop":
            return self._confirm_stop(client, chat, value)
        if command == "/edit":
            return self._edit(client, chat, value)
        if command in {"/clips", "/pending"}:
            return self._list_clips(client, chat, pending=command == "/pending")
        if command in {"/status", "/live", "/queue", "/stats"}:
            return self._status_command(client, chat, command)
        return self._say(client, chat, "Usa /help para ver los comandos disponibles.")

    def _active_sessions(self):
        return [item for item in self.app.live_list() if item["status"] in {"starting", "running"}]

    def _status_command(self, client, chat, command):
        sessions = self._active_sessions()
        if command == "/live":
            return self._say(client, chat, "\n".join(f"{s['id']} · {self._source_label(s)} · {s['status']} · {int(s['stream_time'] or 0)} s"
                                                    for s in sessions[:10]) or "No hay sesiones Live activas.")
        with self.app.production.store.connect() as db:
            counts = dict(db.execute("SELECT state,COUNT(*) FROM production_jobs GROUP BY state"))
            pending = db.execute("SELECT COUNT(*) FROM production_jobs WHERE finished IS NULL").fetchone()[0]
        with self.app.live_production.connect() as db:
            live_counts = dict(db.execute("SELECT state,COUNT(*) FROM jobs GROUP BY state"))
        if command == "/stats":
            text = "Automation · " + ", ".join(f"{key} {value}" for key, value in sorted(counts.items()))
            text += "\nLive · " + ", ".join(f"{key} {value}" for key, value in sorted(live_counts.items()))
        elif command == "/queue":
            text = (f"Automation: {pending} trabajos sin terminar.\nLive: "
                    + ", ".join(f"{k} {live_counts.get(k, 0)}" for k in ("collecting", "queued", "transcribing", "editing", "quality")))
        else:
            text = f"Studio disponible · {len(sessions)} sesiones Live · {pending} trabajos Automation pendientes."
        return self._say(client, chat, text)

    def _confirm_stop(self, client, chat, ident):
        if not re.fullmatch(r"[0-9a-f]{12}", ident) or not any(item["id"] == ident for item in self._active_sessions()):
            return self._say(client, chat, "Indica el ID de una sesión activa de /live.")
        nonce = secrets.token_hex(8)
        self.confirmation = (nonce, ident, time.monotonic() + 90)
        return self._say(client, chat, f"¿Detener la sesión {ident}?",
                         buttons((("Confirmar detención", "stop:" + nonce), ("Cancelar", "cancel:" + nonce))))

    def _callback(self, client, chat, data):
        if data in MENU.values():
            return self._command(client, chat, data, "")
        if data.startswith(("stop:", "cancel:")):
            action, nonce = data.split(":", 1)
            pending = self.confirmation
            if not pending or pending[0] != nonce or pending[2] < time.monotonic():
                return self._say(client, chat, "La confirmación expiró. Usa /stop de nuevo.")
            self.confirmation = None
            if action == "stop":
                self.app.stop_live(pending[1])
                self.store.record("Captura Live detenida por Telegram tras confirmación.")
                return self._say(client, chat, "Sesión Live detenida.")
            return self._say(client, chat, "Detención cancelada.")
        match = re.fullmatch(r"([arcptdiv]):(\d{1,19}):(\d{1,12}):(\d{1,12}):([a-f0-9]{12})", data, re.ASCII)
        if not match or match[5] != self.store.state()["binding"]:
            return
        action = match[1]
        version = tuple(map(int, match.groups()[1:4]))
        if any(number >= 2**63 for number in version):
            return
        row, variant, _ = self._review(version)
        if action == "i":
            return self._say(client, chat, self._summary(row, variant), review_buttons(self._clip_key(version)))
        if action == "v":
            if self.store.enqueue("preview:" + ":".join(map(str, version)), {"version": version, "manual": True}, 16, cooldown=120):
                return self._say(client, chat, "Vista previa en cola. Respeta la frecuencia y el horario configurados.")
            return self._say(client, chat, "Vista previa ya solicitada o cola llena.")
        if action in ("t", "d"):
            self.editing = (version, "title" if action == "t" else "description", time.monotonic() + 180)
            return self._say(client, chat, "Envía /edit seguido del texto nuevo. La solicitud expira en 3 minutos.")
        decision = ACTIONS[action]
        applied = self.app.production.store.remote_review(*version, decision, f"telegram:{chat}")
        return self._say(client, chat, DECISIONS[decision] + " · guardado en Automation."
                         if applied else "Esta versión ya tiene una decisión editorial registrada.")

    def _edit(self, client, chat, value):
        pending = self.editing
        if not pending or pending[2] < time.monotonic():
            return self._say(client, chat, "Abre el clip y toca Editar título o Editar descripción.")
        version, field, _ = pending
        if not value or len(value) > COPY_FIELDS[field]:
            return self._say(client, chat, f"Introduce de 1 a {COPY_FIELDS[field]} caracteres.")
        row, variant, _ = self._review(version)
        fields = {key: variant[key] for key in (*COPY_FIELDS, "hashtags")}
        fields[field] = value
        store = self.app.production.store
        editor = self.app.production.editor.store
        updated = editor.edit(row["editorial_id"], variant["style"], fields, version[2], False,
                              guard=lambda db: store.guard_review(db, *version))
        self.editing = None
        with store.connect(write=True) as db:
            store._event(db, version[0], version[1], "ready", "editorial_edit",
                         f"Texto editado en Telegram · versión {updated['revision']}.")
        return self._say(client, chat, "Edición guardada. Revisa la nueva versión antes de aprobarla.",
                         review_buttons(self._clip_key((*version[:2], updated["revision"]))))

    def _clip_key(self, version):
        return ":".join(map(str, (*version, self.store.state()["binding"])))

    def _review(self, version):
        row = self.app.production.store.review_version(*version)
        minimum = self.app.automation_settings.read()["settings"]["minimum_clip_score"]
        if (row["detection"] is None or row["detection"] < max(6, minimum)
                or not self.app.production.current_evidence(row, json.loads(row["evidence"]))):
            raise ValueError("La evidencia técnica cambió.")
        result, edits = json.loads(row["result"]), json.loads(row["edits"])
        if not eligible(result) or result.get("standalone") is not True:
            raise ValueError("El resultado editorial necesita reevaluación.")
        style = row["selected"] or result["recommended_variant"]
        variant = next((item for item in result["variants"] if item["style"] == style and item.get("valid") is not False), None)
        if variant is None:
            raise ValueError("Selecciona en Studio una variante editorial válida.")
        return row, {**variant, **edits.get(style, {})}, self.app.production.clip_path(row["project"], row["file"])

    def _list_clips(self, client, chat, pending):
        entries = []
        for job in self.app.production.store.review_candidates(limit=20):
            version = (job["id"], job["run"], job["revision"])
            try:
                row, variant, _ = self._review(version)
            except (ValueError, OSError):
                continue
            review = row["remote_review"]
            if pending and review and review["decision"] in {"approved", "rejected"}:
                continue
            entries.append(((f"#{job['id']} · {variant['title'][:38]}", "i:" + self._clip_key(version)),))
            if len(entries) == 8:
                break
        heading = "Selecciona un clip para revisar." if entries else "No hay clips elegibles para esta vista."
        if pending:
            with self.app.production.store.connect() as db:
                other = db.execute("SELECT COUNT(*) FROM production_jobs WHERE state IN ('review','failed')").fetchone()[0]
            if other:
                heading += f"\n{other} trabajos requieren abrir Studio para revisar sus controles."
        return self._say(client, chat, heading,
                         buttons(*entries) if entries else None)

    @staticmethod
    def _source_label(item):
        source = item.get("source") or {}
        return source.get("label") or source.get("platform") or "Live"

    def _summary(self, row, variant, compact=False):
        report = json.loads(row["report"])
        review = row["remote_review"]
        source = row["project"]
        age = "sin fecha verificable"
        if source.startswith("live/"):
            try:
                directory = self.app.root / source
                source = self._source_label(projects.read_json(directory / "live_session.json"))
                with self.app.live_production.connect() as db:
                    stamp = db.execute("SELECT created FROM jobs WHERE session=? AND result=? AND state='done'",
                                       (directory.name, row["filename"])).fetchone()
                if stamp:
                    age = f"{max(0, int((time.time() - stamp[0]) // 60))} min desde detección"
            except (OSError, ValueError, sqlite3.Error):
                source = "Live"
        else:
            try:
                item = projects.read_project(projects.project_path(self.app.root, source))["source"]
                source = urlsplit(item).hostname if item.startswith(("https://", "http://")) else Path(item).name
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if age == "sin fecha verificable" and row["finished"]:
            age += f" · procesado hace {max(0, int((time.time() - row['finished']) // 60))} min"
        coverage = report.get("speech_coverage")
        quality = f"voz cubierta {coverage:.0%}" if isinstance(coverage, (int, float)) else "cobertura sin dato"
        duration = report.get("duration")
        seconds = f"{duration:.1f} s" if isinstance(duration, (int, float)) else "duración sin dato"
        footer = (f"{' '.join(variant['hashtags'])}\nDetección {row['detection']:.2f}/10 · Editorial {row['editorial']:.2f}/10"
                  f" · Global (prioridad) {row['priority']:.2f}/10\n{seconds} · {(source or 'Fuente local')[:90]} · {age}"
                  f"\nCalidad técnica aprobada · Subtítulos: {quality}"
                  + (f"\n{DECISIONS[review['decision']]}" if review else ""))
        caption = variant["description"]
        if compact:
            # Telegram measures captions in UTF-16 units. Reserve headroom for
            # non-BMP characters even without a parse_mode.
            fixed = len((variant["title"] + footer).encode("utf-16-le")) // 2 + 5
            room = max(0, 1000 - fixed)
            preview = caption.encode("utf-16-le")[:room * 2].decode("utf-16-le", errors="ignore")
            caption = preview + ("…" if len(preview) < len(caption) else "")
        return f"{variant['title']}\n{caption}\n{footer}"

    def _scan(self, state):
        prefs = state["prefs"]
        if prefs["immediate"]:
            for row in self.app.production.store.review_candidates(since=state["since"], limit=100):
                if (row["detection"] is not None and row["detection"] >= prefs["minimum_score"]
                        and row["priority"] >= prefs["minimum_priority"]):
                    version = [row["id"], row["run"], row["revision"]]
                    self.store.enqueue("clip:%s:%s:%s" % tuple(version), {"version": version}, row["priority"])
        if prefs["alerts"]:
            self._alerts()

    def _alerts(self):
        if self.startup:
            self.store.enqueue("startup", {"text": "Anqora Media Studio iniciado. Usa /status para consultar operaciones."},
                               12, cooldown=300)
            self.startup = False
        active = {}
        with self.app.live_lock:
            sessions = list(self.app.live_sessions.values())
        for session in sessions:
            if session.status not in {"running", "starting", "failed"}:
                continue
            data = projects.read_json(session.dir / "live_session.json")
            health = data.get("health") or {}
            ident = session.id
            capture = health.get("capture") or {}
            previous = self.observed.get(ident)
            degraded = data["status"] == "failed" or (health.get("restarts", 0) > 0 and
                       (not health.get("ffmpeg_alive") or not capture.get("available")))
            restarted = health.get("restarts", 0)
            active[ident] = (degraded, restarted)
            if degraded or (previous and previous[1] < restarted):
                self.store.enqueue("capture-" + ident, {"text": f"Live {ident}: captura interrumpida."}, 14, cooldown=900)
            if previous and not degraded and (previous[0] or previous[1] < restarted):
                self.store.enqueue("recovered-" + ident, {"text": f"Live {ident}: reconexión recuperada."}, 13, cooldown=900)
            transcript = (health.get("analyzers") or {}).get("transcript") or {}
            if transcript.get("state") == "degraded" and transcript.get("errors", 0) >= 2:
                self.store.enqueue("whisper-" + ident, {"text": f"Live {ident}: error persistente de transcripción."}, 13, cooldown=900)
            note = str(health.get("production_message") or "").casefold()
            if "cola" in note and "llena" in note:
                self.store.enqueue("queue", {"text": "La cola de producción Live está saturada."}, 12, cooldown=900)
            if "almacenamiento" in note or "disco" in note or any("espacio libre" in str(e).lower() for e in data.get("errors", [])):
                self.store.enqueue("disk", {"text": "Chopify tiene espacio de almacenamiento insuficiente."}, 15, cooldown=900)
        self.observed = active
        since = time.time() - 300
        with self.app.live_production.connect() as db:
            failed = db.execute("SELECT 1 FROM jobs WHERE state='error' AND updated>=? LIMIT 1", (since,)).fetchone()
        with self.app.production.store.connect() as db:
            failed = failed or db.execute("SELECT 1 FROM production_jobs WHERE state='failed' AND updated>=? LIMIT 1",
                                         (since,)).fetchone()
        if failed:
            self.store.enqueue("production-error", {"text": "Chopify registró un fallo de producción. Consulta /queue y Studio."},
                               13, cooldown=900)

    def _deliver(self):
        next_scan = 0.
        while not self.stopping.wait(1):
            try:
                with self.lock:
                    data = self.store.state()
                    if not self._enabled() or data["chat_id"] is None:
                        continue
                    generation, token = self.generation, self.token
                if time.monotonic() >= next_scan:
                    next_scan = time.monotonic() + 3
                    with self.lock:
                        if generation == self.generation:
                            self._scan(data)
                if self.connection == "invalid_token":
                    continue
                timezone = self.app.automation_settings.read()["settings"]["timezone"]
                if quiet(data["prefs"], timezone):
                    continue
                with self.lock:
                    if generation != self.generation:
                        continue
                    row = self.store.claim(data["prefs"]["max_per_hour"])
                if row:
                    self.send_delivery(self.factory(token), data["chat_id"], generation, row)
            except (OSError, sqlite3.Error, ValueError, RuntimeError):
                continue

    def send_delivery(self, client, chat, generation, delivery):
        try:
            data = delivery["payload"]
            if "version" in data:
                version = data["version"]
                row, variant, original = self._review(version)
                prefs = self.store.state()["prefs"]
                if not data.get("manual") and (not prefs["immediate"] or row["detection"] < prefs["minimum_score"] or
                        row["priority"] < prefs["minimum_priority"] or row["remote_review"] or json.loads(row["edits"])):
                    self.store.finish(delivery, "cancelled")
                    return
                report = json.loads(row["report"])
                with upload_copy(original, self.directory, report.get("duration") or 0) as media:
                    self._review(version)
                    self._delivery_guard(generation, chat)
                    client.call("sendVideo", {"chat_id": chat, "supports_streaming": True,
                                             "caption": self._summary(row, variant, compact=True),
                                             "reply_markup": review_buttons(self._clip_key(version))}, video=media)
            else:
                self._delivery_guard(generation, chat)
                self._say(client, chat, data["text"])
            self.store.finish(delivery, "sent")
        except TelegramError as exc:
            if exc.retry and delivery["attempts"] < MAX_ATTEMPTS:
                self.store.finish(delivery, "queued", str(exc), max(exc.delay, 2 ** delivery["attempts"]))
            else:
                self.store.finish(delivery, "uncertain" if exc.uncertain else "failed", str(exc))
            self.store.record(str(exc))
        except (ValueError, OSError, TypeError, KeyError, subprocess.SubprocessError):
            self.store.finish(delivery, "failed", "No se pudo validar el clip, generar la copia o confirmar al destinatario.")
            self.store.record("Una entrega quedó bloqueada por validación local.")

    def _delivery_guard(self, generation, chat):
        with self.lock:
            if generation != self.generation or not self._enabled() or self.store.state()["chat_id"] != chat:
                raise ValueError("La configuración de Telegram cambió.")
