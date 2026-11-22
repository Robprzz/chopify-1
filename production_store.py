"""Persistent production jobs sharing the Automation SQLite workspace."""
import json
import time

from editorial_store import dump


ACTIVE = ("validating", "repairing", "revalidating", "analyzing", "generating", "validating_proposals")
TERMINAL = ("ready", "review", "rejected", "failed", "cancelled")
LEASE_SECONDS = 12


class Store:
    def __init__(self, editorial_store):
        self.connect = editorial_store.connect

    def migrate(self):
        with self.connect(write=True) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS production_config (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    paused INTEGER NOT NULL, max_repairs INTEGER NOT NULL
                )
            """)
            db.execute("INSERT OR IGNORE INTO production_config VALUES(1, 0, 2)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS production_jobs (
                    id INTEGER PRIMARY KEY, project TEXT NOT NULL, filename TEXT NOT NULL,
                    state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    owner TEXT, lease REAL, run INTEGER NOT NULL DEFAULT 1,
                    cancel_requested INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    active_filename TEXT, editorial_id INTEGER, technical TEXT, diagnosis TEXT,
                    code TEXT, error TEXT, finished REAL,
                    seconds REAL NOT NULL DEFAULT 0, repair_seconds REAL,
                    editorial_seconds REAL, UNIQUE(project, filename)
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS production_pending ON production_jobs(finished, id)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS production_events (
                    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, run INTEGER NOT NULL,
                    state TEXT NOT NULL, created REAL NOT NULL, code TEXT, message TEXT
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS production_events_job ON production_events(job_id, id)")

    @staticmethod
    def _event(db, job, run, state, code=None, error=None):
        db.execute("INSERT INTO production_events(job_id, run, state, created, code, message) VALUES (?,?,?,?,?,?)",
                   (job, run, state, time.time(), code, error))

    def enqueue(self, project, filename):
        return bool(self.enqueue_many(((project, filename),)))

    def enqueue_many(self, clips):
        now = time.time()
        added = 0
        with self.connect(write=True) as db:
            for project, filename in clips:
                row = db.execute("""INSERT OR IGNORE INTO production_jobs(project,filename,state,created,updated)
                                    VALUES (?,?,'generated',?,?) RETURNING id""",
                                 (project, filename, now, now)).fetchone()
                if row is not None:
                    self._event(db, row[0], 1, "generated")
                    added += 1
        return added

    def config(self, *, paused=None, max_repairs=None):
        if paused is not None and type(paused) is not bool:
            raise ValueError("El estado de pausa debe ser booleano.")
        if max_repairs is not None and (type(max_repairs) is not int or not 0 <= max_repairs <= 5):
            raise ValueError("Configura entre 0 y 5 intentos de reparación.")
        with self.connect(write=paused is not None or max_repairs is not None) as db:
            if paused is not None:
                db.execute("UPDATE production_config SET paused=? WHERE id=1", (int(paused),))
            if max_repairs is not None:
                db.execute("UPDATE production_config SET max_repairs=? WHERE id=1", (max_repairs,))
            row = db.execute("SELECT paused, max_repairs FROM production_config WHERE id=1").fetchone()
            return {"paused": bool(row[0]), "max_repairs": row[1]}

    def claim(self, owner):
        now = time.time()
        with self.connect(write=True) as db:
            stale = db.execute("SELECT * FROM production_jobs WHERE owner IS NOT NULL AND lease < ?", (now,)).fetchall()
            for row in stale:
                if row["cancel_requested"]:
                    db.execute("UPDATE production_jobs SET state='cancelled', finished=?, owner=NULL, lease=NULL WHERE id=?",
                               (now, row["id"]))
                    self._event(db, row["id"], row["run"], "cancelled")
                else:
                    db.execute("UPDATE production_jobs SET owner=NULL, lease=NULL WHERE id=?", (row["id"],))
                    self._event(db, row["id"], row["run"], row["state"], "interrupted",
                                "Studio recuperó el trabajo después de una interrupción.")
            if (db.execute("SELECT paused FROM production_config").fetchone()[0]
                    or db.execute("SELECT 1 FROM production_jobs WHERE owner IS NOT NULL").fetchone()):
                return None
            row = db.execute("SELECT * FROM production_jobs WHERE finished IS NULL ORDER BY id LIMIT 1").fetchone()
            if row is not None:
                db.execute("UPDATE production_jobs SET owner=?, lease=? WHERE id=?", (owner, now + LEASE_SECONDS, row["id"]))
                return dict(row)
            return None

    def renew(self, job, owner):
        with self.connect(write=True) as db:
            row = db.execute("""UPDATE production_jobs SET lease=? WHERE id=? AND owner=?
                                RETURNING cancel_requested""", (time.time() + LEASE_SECONDS, job, owner)).fetchone()
            return row is not None and not row["cancel_requested"]

    def change(self, job, owner, state, *, code=None, error=None, **fields):
        # Internal callers use only schema-checked column names.
        allowed = {"active_filename", "editorial_id", "technical", "diagnosis", "attempts",
                   "seconds", "repair_seconds", "editorial_seconds"}
        if not fields.keys() <= allowed or state not in (*ACTIVE, *TERMINAL):
            raise ValueError("Transición de producción no válida.")
        now = time.time()
        with self.connect(write=True) as db:
            row = db.execute("SELECT state, run, cancel_requested FROM production_jobs WHERE id=? AND owner=?",
                             (job, owner)).fetchone()
            if row is None:
                return
            if row["cancel_requested"] and state in TERMINAL:
                state, code, error = "cancelled", None, None
            fields.update(state=state, updated=now, code=code, error=error)
            if state in TERMINAL:
                fields.update(finished=now, owner=None, lease=None)
            sql = ", ".join(f"{field}=?" for field in fields)
            db.execute(f"UPDATE production_jobs SET {sql} WHERE id=?", (*fields.values(), job))
            if row["state"] != state or code:
                self._event(db, job, row["run"], state, code, error)

    def link_editorial(self, job, owner, editorial_id):
        with self.connect(write=True) as db:
            db.execute("UPDATE production_jobs SET editorial_id=? WHERE id=? AND owner=?",
                       (editorial_id, job, owner))

    def editorial_stage(self, job, run, state):
        if state not in {"generating", "validating_proposals"}:
            raise ValueError("Etapa editorial desconocida.")
        with self.connect(write=True) as db:
            row = db.execute("""UPDATE production_jobs SET state=?, updated=?
                WHERE id=? AND run=? AND finished IS NULL AND cancel_requested=0 AND state!=?
                RETURNING id""", (state, time.time(), job, run, state)).fetchone()
            if row:
                self._event(db, job, run, state)

    def release(self, job, owner):
        with self.connect(write=True) as db:
            db.execute("UPDATE production_jobs SET owner=NULL, lease=NULL WHERE id=? AND owner=?", (job, owner))

    def cancel(self, job):
        now = time.time()
        with self.connect(write=True) as db:
            row = db.execute("""UPDATE production_jobs SET cancel_requested=1,
                    state=CASE WHEN owner IS NULL THEN 'cancelled' ELSE state END,
                    finished=CASE WHEN owner IS NULL THEN ? ELSE finished END, updated=?
                    WHERE id=? AND finished IS NULL RETURNING run, state, editorial_id""",
                             (now, now, job)).fetchone()
            if row is None:
                raise ValueError("El trabajo ya terminó o no existe.")
            if row["state"] == "cancelled":
                self._event(db, job, row["run"], "cancelled")
            return row["editorial_id"]

    def retry(self, job):
        now = time.time()
        with self.connect(write=True) as db:
            row = db.execute("""UPDATE production_jobs SET state='generated', updated=?, run=run+1,
                    cancel_requested=0, editorial_id=NULL, finished=NULL, attempts=0,
                    code=NULL, error=NULL, technical=NULL, diagnosis=NULL,
                    seconds=0, repair_seconds=NULL, editorial_seconds=NULL
                   WHERE id=? AND state IN ('review','rejected','failed','cancelled') RETURNING run""",
                             (now, job)).fetchone()
            if not row:
                raise ValueError("Solo se pueden reintentar trabajos que requieren atención.")
            self._event(db, job, row["run"], "generated")

    def detail(self, job):
        with self.connect() as db:
            row = db.execute("SELECT * FROM production_jobs WHERE id=?", (job,)).fetchone()
            if row is None:
                raise ValueError("Trabajo de producción desconocido.")
            data = self._display(row)
            data["events"] = [dict(event) for event in db.execute("""
                SELECT run,state,created,code,message FROM production_events
                WHERE job_id=? ORDER BY id DESC LIMIT 100""", (job,))]
            return data

    @staticmethod
    def _display(row):
        data = dict(row)
        data["diagnosis"] = json.loads(data["diagnosis"]) if data["diagnosis"] else None
        data.pop("owner")
        data.pop("lease")
        return data

    def status(self, before=None):
        with self.connect() as db:
            counts = dict(db.execute("SELECT state, count(*) FROM production_jobs GROUP BY state").fetchall())
            report = db.execute("""SELECT
                    count(*) AS total,
                    count(*) FILTER (WHERE finished IS NULL) AS pending,
                    count(*) FILTER (WHERE technical='passed') AS technical,
                    count(*) FILTER (WHERE active_filename IS NOT NULL AND technical='passed') AS repaired,
                    count(*) FILTER (WHERE active_filename IS NOT NULL AND state='ready') AS manual_repairs_avoided,
                    avg(seconds) FILTER (WHERE finished IS NOT NULL AND state!='cancelled') AS processing,
                    avg(repair_seconds) AS repairs, avg(editorial_seconds) AS editorial
                FROM production_jobs""").fetchone()
            problems = [dict(row) for row in db.execute("""SELECT code, count(*) AS count FROM production_events
                WHERE code IS NOT NULL GROUP BY code ORDER BY count DESC, code LIMIT 8""")]
            jobs = db.execute("""SELECT * FROM production_jobs WHERE (? IS NULL OR id < ?)
                                  ORDER BY id DESC LIMIT 31""", (before, before)).fetchall()
        return {"config": self.config(), "counts": counts, "metrics": dict(report), "problems": problems,
                "jobs": [self._display(row) for row in jobs[:30]], "next": jobs[29]["id"] if len(jobs) > 30 else None}
