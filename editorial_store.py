"""Additive SQLite storage for editorial jobs. Publication state lives elsewhere."""
from contextlib import closing, contextmanager
import json
import sqlite3
import time

from editorial_content import DEFAULT_CONFIG, STYLES, validate_config, validate_edits


MAX_PENDING = 16
class QueueFull(ValueError):
    pass


class Conflict(ValueError):
    pass


def dump(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False)


class EditorialStore:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self, write=False):
        with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
            db.row_factory = sqlite3.Row
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    def migrate(self):
        with self.connect(write=True) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS editorial_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL,
                    revision INTEGER NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS editorial_jobs (
                    id INTEGER PRIMARY KEY, clip_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    source TEXT NOT NULL, settings TEXT NOT NULL, created REAL NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('queued','running','done','failed','cancelled')),
                    owner TEXT, lease REAL, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    result TEXT, error TEXT, elapsed REAL, finished REAL, selected TEXT,
                    edits TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 0
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS editorial_fingerprint ON editorial_jobs(fingerprint, id)")
            db.execute("CREATE INDEX IF NOT EXISTS editorial_clip ON editorial_jobs(clip_id, id)")
            db.execute("CREATE INDEX IF NOT EXISTS editorial_state ON editorial_jobs(state, id)")

    def config(self):
        with self.connect() as db:
            row = db.execute("SELECT payload, revision FROM editorial_config WHERE id = 1").fetchone()
        return {"settings": validate_config(json.loads(row["payload"])) if row else validate_config(DEFAULT_CONFIG),
                "revision": row["revision"] if row else 0}

    def save_config(self, settings, revision):
        normalized = validate_config(settings)
        if type(revision) is not int or revision < 0:
            raise ValueError("Revisión editorial inválida.")
        with self.connect(write=True) as db:
            row = db.execute("SELECT revision FROM editorial_config WHERE id = 1").fetchone()
            current = row["revision"] if row else 0
            if revision != current:
                raise Conflict("La configuración cambió en otra pestaña. Vuelve a cargarla.")
            db.execute("""INSERT INTO editorial_config VALUES (1, ?, ?) ON CONFLICT(id)
                          DO UPDATE SET payload=excluded.payload, revision=excluded.revision""",
                       (dump(normalized), current + 1))
        return {"settings": normalized, "revision": current + 1}

    def submit(self, source, settings, fingerprint, force):
        clip_id = source["clip_id"]
        with self.connect(write=True) as db:
            row = db.execute("""
                SELECT id FROM editorial_jobs WHERE fingerprint=? AND state IN ('queued','running')
                ORDER BY id DESC LIMIT 1
            """, (fingerprint,)).fetchone()
            if row:
                return row["id"], True
            if not force:
                row = db.execute("""
                    SELECT id, result FROM editorial_jobs WHERE fingerprint=? AND state='done'
                    ORDER BY id DESC LIMIT 1
                """, (fingerprint,)).fetchone()
                if row and (not settings["model_digest"] or json.loads(row["result"])["provider"] == "ollama"):
                    return row["id"], True
            count = db.execute("SELECT count(*) FROM editorial_jobs WHERE state IN ('queued','running')").fetchone()[0]
            if count >= MAX_PENDING:
                raise QueueFull("La cola editorial está llena. Vuelve a intentarlo cuando termine un trabajo.")
            row = db.execute("""
                SELECT selected, edits FROM editorial_jobs
                WHERE clip_id=? AND state='done' AND json_extract(settings, '$.platform')=?
                ORDER BY id DESC LIMIT 1
            """, (clip_id, settings["platform"])).fetchone()
            # Human copy remains available in the new version as well as the previous version.
            edits = row["edits"] if row else "{}"
            selected = row["selected"] if row else None
            cursor = db.execute("""
                INSERT INTO editorial_jobs(clip_id, fingerprint, source, settings, created, state, edits, selected)
                VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
            """, (clip_id, fingerprint, dump(source), dump(settings), time.time(), edits, selected))
            return cursor.lastrowid, False

    def claim(self, owner):
        now = time.time()
        with self.connect(write=True) as db:
            db.execute("""
                UPDATE editorial_jobs SET state=CASE cancel_requested WHEN 1 THEN 'cancelled' ELSE 'queued' END,
                owner=NULL, lease=NULL, finished=CASE cancel_requested WHEN 1 THEN ? ELSE NULL END,
                revision=revision+1 WHERE state='running' AND lease < ?
            """, (now, now))
            if db.execute("SELECT 1 FROM editorial_jobs WHERE state='running'").fetchone():
                return None
            row = db.execute("SELECT * FROM editorial_jobs WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
            if row:
                settings = json.loads(row["settings"])
                db.execute("""UPDATE editorial_jobs SET state='running', owner=?, lease=?, revision=revision+1
                              WHERE id=?""", (owner, now + settings["timeout"] + 25, row["id"]))
                return row["id"], json.loads(row["source"]), settings
            return None

    def finish(self, job, owner, state, result, error, elapsed):
        with self.connect(write=True) as db:
            db.execute("""
                UPDATE editorial_jobs SET
                  state=CASE cancel_requested WHEN 1 THEN 'cancelled' ELSE ? END,
                  result=CASE cancel_requested WHEN 1 THEN NULL ELSE ? END,
                  error=?, elapsed=?, finished=CASE WHEN cancel_requested=1 OR ?!='queued' THEN ? ELSE NULL END,
                  owner=NULL, lease=NULL, revision=revision+1
                WHERE id=? AND owner=? AND state='running'
            """, (state, dump(result) if result else None, error, elapsed, state, time.time(), job, owner))

    def cancel(self, job):
        with self.connect(write=True) as db:
            db.execute("""
                UPDATE editorial_jobs SET cancel_requested=1, revision=revision+1,
                  finished=CASE state WHEN 'queued' THEN ? ELSE finished END,
                  state=CASE state WHEN 'queued' THEN 'cancelled' ELSE state END
                WHERE id=? AND state IN ('queued','running')
            """, (time.time(), job))
        return self.detail(job)

    def cancel_requested(self, job):
        with self.connect() as db:
            return bool(db.execute("SELECT cancel_requested FROM editorial_jobs WHERE id=?", (job,)).fetchone()[0])

    def edit(self, job, style, fields, revision, select):
        if not isinstance(style, str) or style not in STYLES or type(revision) is not int or type(select) is not bool:
            raise ValueError("Edición editorial inválida.")
        if fields is not None:
            validate_edits(fields)
        with self.connect(write=True) as db:
            row = db.execute("SELECT edits, selected, revision FROM editorial_jobs WHERE id=? AND state='done'", (job,)).fetchone()
            if not row:
                raise ValueError("El trabajo todavía no tiene un resultado confirmado.")
            if row["revision"] != revision:
                raise Conflict("El resultado cambió en otra pestaña. Vuelve a cargarlo.")
            edits = json.loads(row["edits"])
            if not select:
                if fields is None:
                    edits.pop(style, None)
                else:
                    edits[style] = fields
            db.execute("UPDATE editorial_jobs SET edits=?, selected=?, revision=revision+1 WHERE id=?",
                       (dump(edits), style if select else row["selected"], job))
        return self.detail(job)

    def detail(self, job):
        with self.connect() as db:
            row = db.execute("""
                SELECT id, clip_id, created, state, settings, result, error, elapsed, finished, selected, edits, revision,
                       json_extract(source, '$.transcript_id') AS transcript_id
                FROM editorial_jobs WHERE id=?
            """, (job,)).fetchone()
        if not row:
            raise ValueError("Trabajo editorial desconocido.")
        result = dict(row)
        for field in ("settings", "result", "edits"):
            result[field] = json.loads(result[field]) if result[field] else None
        return result

    def history(self, clip_id, before):
        with self.connect() as db:
            rows = db.execute("""
                SELECT id, clip_id, state, created, elapsed, error, revision,
                       json_extract(result, '$.provider') AS provider,
                       json_extract(settings, '$.platform') AS platform
                FROM editorial_jobs WHERE (? IS NULL OR clip_id=?) AND (? IS NULL OR id < ?)
                ORDER BY id DESC LIMIT 31
            """, (clip_id, clip_id, before, before)).fetchall()
            status = {row[0]: row[1] for row in db.execute("SELECT state, count(*) FROM editorial_jobs GROUP BY state")}
            timing = db.execute("SELECT avg(elapsed) FROM editorial_jobs WHERE state='done'").fetchone()[0]
            last = db.execute("""SELECT error FROM editorial_jobs WHERE error IS NOT NULL
                                ORDER BY id DESC LIMIT 1""").fetchone()
            provider = db.execute("""SELECT json_extract(result, '$.provider') FROM editorial_jobs
                                    WHERE state='done' ORDER BY id DESC LIMIT 1""").fetchone()
        return {"jobs": [dict(row) for row in rows[:30]], "next": rows[29]["id"] if len(rows) > 30 else None,
                "counts": status, "average_seconds": timing, "last_error": last[0] if last else None,
                "last_provider": provider[0] if provider else None}
