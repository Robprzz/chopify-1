"""Local configuration, inbox cursor, and durable Telegram outbox."""
from contextlib import closing, contextmanager
import json
import re
import secrets
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

DEFAULTS = {"enabled": False, "immediate": True, "alerts": True, "minimum_score": 6.0,
            "minimum_priority": 0.0, "quiet_start": "", "quiet_end": "", "max_per_hour": 12,
            "sources": []}
MAX_PENDING = 100
MAX_ATTEMPTS = 4


def capture_source(value):
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError("Enlace de captura inválido.")
    url = urlsplit(value)
    host, parts = url.hostname, url.path.strip("/").split("/")
    if (url.scheme != "https" or url.netloc.lower() != host
            or url.fragment or url.username or url.password):
        raise ValueError("Usa una página HTTPS pública de YouTube, Twitch o Kick.")
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        if host == "youtu.be" and len(parts) == 1:
            video = parts[0]
        elif parts == ["watch"]:
            video = next(iter(parse_qs(url.query, max_num_fields=12).get("v", [])), "")
        elif len(parts) == 2 and parts[0] == "live":
            video = parts[1]
        else:
            video = ""
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
            return "https://www.youtube.com/watch?v=" + video
    if host in {"www.twitch.tv", "twitch.tv", "kick.com", "www.kick.com"} and len(parts) == 1 and not url.query:
        if re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", parts[0]) and parts[0].lower() not in {"directory", "settings", "login"}:
            return "https://" + host.removeprefix("www.") + "/" + parts[0].lower()
    raise ValueError("Autoriza una página de video de YouTube o un canal de Twitch/Kick.")


def normalize(config):
    if not isinstance(config, dict) or config.keys() - DEFAULTS.keys():
        raise ValueError("Preferencias de Telegram inválidas.")
    result = {**DEFAULTS, **config}
    for key in ("enabled", "immediate", "alerts"):
        if type(result[key]) is not bool:
            raise ValueError("Configuración de Telegram inválida.")
    for key, minimum in (("minimum_score", 6), ("minimum_priority", 0)):
        value = result[key]
        if type(value) not in (int, float) or not minimum <= value <= 10:
            raise ValueError("Revisa los umbrales de notificación.")
    if type(result["max_per_hour"]) is not int or not 1 <= result["max_per_hour"] <= 60:
        raise ValueError("La frecuencia debe ser de 1 a 60 mensajes por hora.")
    for key in ("quiet_start", "quiet_end"):
        if type(result[key]) is not str or (result[key] and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", result[key])):
            raise ValueError("Usa HH:MM para el horario silencioso.")
    if bool(result["quiet_start"]) != bool(result["quiet_end"]) or (
            result["quiet_start"] and result["quiet_start"] == result["quiet_end"]):
        raise ValueError("Indica dos horas distintas para el horario silencioso.")
    sources = result["sources"]
    if not isinstance(sources, list) or len(sources) > 20:
        raise ValueError("Autoriza hasta 20 enlaces de captura.")
    result["sources"] = list(dict.fromkeys(map(capture_source, sources)))
    return result


class Store:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS config (
                    id INTEGER PRIMARY KEY CHECK(id=1), prefs TEXT NOT NULL, user_id INTEGER, chat_id INTEGER,
                    offset INTEGER NOT NULL DEFAULT 0, since REAL NOT NULL DEFAULT 0, binding TEXT
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    key TEXT PRIMARY KEY, payload TEXT NOT NULL, priority REAL NOT NULL, state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, available REAL NOT NULL, finished REAL, error TEXT
                );
                CREATE INDEX IF NOT EXISTS telegram_deliveries ON outbox(state,available,priority);
                CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY, stamp REAL NOT NULL, message TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS alert(key TEXT PRIMARY KEY, notified REAL NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO config(id,prefs) VALUES(1,?)", (json.dumps(DEFAULTS),))

    @contextmanager
    def connect(self, write=False):
        with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
            db.row_factory = sqlite3.Row
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    def state(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM config WHERE id=1").fetchone()
            return {**dict(row), "prefs": json.loads(row["prefs"])}

    def configure(self, prefs, rotate=False):
        with self.connect(write=True) as db:
            db.execute("UPDATE config SET prefs=? WHERE id=1", (json.dumps(prefs),))
            if rotate:
                self._revoke(db, reset_offset=True)

    def pair(self, user_id, chat_id):
        with self.connect(write=True) as db:
            return bool(db.execute("""UPDATE config SET user_id=?,chat_id=?,since=?,binding=?
                                      WHERE id=1 AND user_id IS NULL""",
                                   (user_id, chat_id, time.time(), secrets.token_hex(6))).rowcount)

    def revoke(self):
        with self.connect(write=True) as db:
            self._revoke(db, reset_offset=False)

    @staticmethod
    def _revoke(db, reset_offset):
        db.execute("UPDATE config SET user_id=NULL,chat_id=NULL,since=0,binding=NULL,offset=CASE WHEN ? THEN 0 ELSE offset END",
                   (reset_offset,))
        db.execute("UPDATE outbox SET state='cancelled',finished=? WHERE state IN ('queued','sending')", (time.time(),))
        db.execute("DELETE FROM alert")

    def claim_update(self, update):
        with self.connect(write=True) as db:
            return bool(db.execute("UPDATE config SET offset=? WHERE id=1 AND offset<=?", (update + 1, update)).rowcount)

    def enqueue(self, key, payload, priority=0, *, cooldown=0):
        now = time.time()
        with self.connect(write=True) as db:
            if cooldown:
                previous = db.execute("SELECT notified FROM alert WHERE key=?", (key,)).fetchone()
                if previous and previous[0] > now - cooldown:
                    return False
            if db.execute("SELECT count(*) FROM outbox WHERE state IN ('queued','sending')").fetchone()[0] >= MAX_PENDING:
                return False
            entry = f"{key}:{int(now * 1000)}" if cooldown else key
            added = db.execute("INSERT OR IGNORE INTO outbox(key,payload,priority,state,available) VALUES(?,?,?,'queued',?)",
                               (entry, json.dumps(payload), priority, now)).rowcount
            if added and cooldown:
                db.execute("INSERT OR REPLACE INTO alert VALUES(?,?)", (key, now))
                db.execute("DELETE FROM alert WHERE notified<?", (now - 86400,))
            return bool(added)

    def recover(self):
        with self.connect(write=True) as db:
            db.execute("""UPDATE outbox SET state='uncertain',finished=?,error='Envío interrumpido sin confirmación.'
                          WHERE state='sending'""", (time.time(),))

    def claim(self, maximum):
        now = time.time()
        with self.connect(write=True) as db:
            if db.execute("SELECT count(*) FROM outbox WHERE state IN ('sent','uncertain') AND finished>?",
                          (now - 3600,)).fetchone()[0] >= maximum:
                return None
            row = db.execute("""SELECT * FROM outbox WHERE state='queued' AND available<=?
                                ORDER BY priority DESC,available LIMIT 1""", (now,)).fetchone()
            if row:
                db.execute("UPDATE outbox SET state='sending',attempts=attempts+1 WHERE key=?", (row["key"],))
                return {**dict(row), "payload": json.loads(row["payload"]), "attempts": row["attempts"] + 1}
        return None

    def finish(self, row, state, error=None, delay=0):
        with self.connect(write=True) as db:
            now = time.time()
            db.execute("""UPDATE outbox SET state=?,error=?,available=?,finished=? WHERE key=? AND state='sending'""",
                       (state, error, now + delay, None if state == "queued" else now, row["key"]))
            # Clip delivery keys remain as deduplication evidence. Old operational alerts are disposable.
            db.execute("DELETE FROM outbox WHERE finished<? AND key NOT LIKE 'clip:%'", (now - 30 * 86400,))

    def record(self, message):
        with self.connect(write=True) as db:
            db.execute("INSERT INTO log(stamp,message) VALUES(?,?)", (time.time(), message))
            db.execute("DELETE FROM log WHERE id <= (SELECT max(id)-100 FROM log)")

    def diagnostics(self):
        with self.connect() as db:
            return {"log": [dict(row) for row in db.execute("SELECT stamp,message FROM log ORDER BY id DESC LIMIT 30")],
                    "deliveries": dict(db.execute("SELECT state,count(*) FROM outbox GROUP BY state").fetchall())}
