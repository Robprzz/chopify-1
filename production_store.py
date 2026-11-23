"""Persistent production jobs sharing the Automation SQLite workspace."""
import json
import time

from editorial_store import dump
from production_selection import duplicate, local_day, priority, score, text_similarity


ACTIVE = ("validating", "repairing", "revalidating", "analyzing", "understanding", "generating", "validating_proposals", "autocorrecting")
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

            db.execute("""CREATE TABLE IF NOT EXISTS production_evidence (
                job_id INTEGER NOT NULL, run INTEGER NOT NULL, file TEXT NOT NULL,
                detection REAL, editorial REAL, context REAL, diversity REAL, priority REAL,
                evidence TEXT NOT NULL, report TEXT NOT NULL, duplicate TEXT,
                PRIMARY KEY(job_id, run))""")

    def cached_technical(self, job, file, current_stamp, original_stamp):
        with self.connect() as db:
            rows = db.execute("""SELECT evidence, report FROM production_evidence
                WHERE job_id=? AND file=? ORDER BY run DESC""", (job, file))
            for row in rows:
                evidence = json.loads(row["evidence"])
                if (current_stamp and original_stamp and evidence["file_stamp"] == current_stamp
                        and evidence["original_stamp"] == original_stamp):
                    return json.loads(row["report"]), evidence
        return None

    def record_evidence(self, job, owner, file, detection, evidence, report, current):
        with self.connect(write=True) as db:
            row = db.execute("SELECT run FROM production_jobs WHERE id=? AND owner=? AND cancel_requested=0",
                             (job, owner)).fetchone()
            if not row:
                return None
            run = row["run"]
            past = db.execute("""SELECT duplicate, evidence FROM production_evidence
                WHERE job_id=? AND run < ? ORDER BY run DESC LIMIT 1""", (job, run)).fetchone()
            dismissed = {}
            if past and json.loads(past["evidence"])["sha"] == evidence["sha"]:
                dismissed.update(json.loads(past["evidence"]).get("dismissed", {}))
                override = db.execute("""SELECT 1 FROM production_events
                    WHERE job_id=? AND run=? AND code='duplicate_override'""", (job, run)).fetchone()
                if override and past["duplicate"]:
                    pair = json.loads(past["duplicate"])
                    dismissed[str(pair["job_id"])] = pair["compared_sha"]
            evidence["dismissed"] = dismissed
            related, overlap = None, 0
            rows = db.execute("""SELECT p.id, p.project, p.filename, e.file, e.evidence
                FROM production_evidence e JOIN production_jobs p ON p.id=e.job_id AND p.run=e.run
                WHERE e.duplicate IS NULL AND p.id!=? AND p.technical='passed'
                    AND p.state IN ('ready','review') ORDER BY p.id""", (job,))
            for other in rows:
                candidate = json.loads(other["evidence"])
                match = duplicate(evidence, candidate)
                similarity = 0 if match else text_similarity(evidence["tokens"], candidate["tokens"])
                if not match and similarity <= overlap:
                    continue
                if dismissed.get(str(other["id"])) == candidate["sha"] or not current(other, candidate):
                    continue
                if match:
                    related = {**match, "job_id": other["id"], "project": other["project"],
                               "filename": other["filename"], "validated_filename": other["file"],
                               "compared_sha": candidate["sha"]}
                    break
                overlap = similarity
            diversity = round(10 * (1 - overlap), 3) if evidence["tokens"] else 5
            db.execute("""INSERT INTO production_evidence(job_id,run,file,detection,diversity,evidence,report,duplicate)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(job_id,run) DO UPDATE SET
                  file=excluded.file,detection=excluded.detection,diversity=excluded.diversity,
                  evidence=excluded.evidence,report=excluded.report,duplicate=excluded.duplicate""",
                (job, run, file, detection, diversity, dump(evidence), dump(report), dump(related) if related else None))
            return related

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

    def change(self, job, owner, state, *, code=None, error=None, editorial=None, context=None, **fields):
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
            if editorial is not None:
                evidence = db.execute("SELECT detection, diversity FROM production_evidence WHERE job_id=? AND run=?",
                                      (job, row["run"])).fetchone()
                if evidence:
                    value = (priority(evidence["detection"], editorial, context, evidence["diversity"])
                             if state == "ready" else None)
                    db.execute("UPDATE production_evidence SET editorial=?,context=?,priority=? WHERE job_id=? AND run=?",
                               (editorial, context, value, job, row["run"]))
            if row["state"] != state or code:
                self._event(db, job, row["run"], state, code, error)

    def link_editorial(self, job, owner, editorial_id):
        with self.connect(write=True) as db:
            db.execute("UPDATE production_jobs SET editorial_id=? WHERE id=? AND owner=?",
                       (editorial_id, job, owner))

    def editorial_stage(self, job, run, state):
        if state not in {"understanding", "generating", "validating_proposals", "autocorrecting"}:
            raise ValueError("Etapa editorial desconocida.")
        with self.connect(write=True) as db:
            row = db.execute("""UPDATE production_jobs SET state=?, updated=?
                WHERE id=? AND run=? AND finished IS NULL AND cancel_requested=0 AND state!=?
                RETURNING id""", (state, time.time(), job, run, state)).fetchone()
            if row:
                self._event(db, job, run, state)

    def ready_evidence(self):
        with self.connect() as db:
            return db.execute("""SELECT p.id,p.run,p.project,p.filename,e.file,e.evidence
                FROM production_jobs p JOIN production_evidence e ON p.id=e.job_id AND p.run=e.run
                WHERE p.state='ready'""").fetchall()

    def invalidate(self, jobs):
        if not jobs:
            return
        with self.connect(write=True) as db:
            for job in jobs:
                message = "La exportación o sus subtítulos cambiaron. Solicita una reevaluación."
                updated = db.execute("""UPDATE production_jobs SET state='review',technical=NULL,
                    code='input_changed',error=?,updated=? WHERE id=? AND run=? AND state='ready' RETURNING id""",
                    (message, time.time(), job["id"], job["run"])).fetchone()
                if updated:
                    self._event(db, job["id"], job["run"], "review", "input_changed", message)

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

    def retry(self, job, *, distinct=False):
        now = time.time()
        with self.connect(write=True) as db:
            row = db.execute("""UPDATE production_jobs SET state='generated', updated=?, run=run+1,
                    cancel_requested=0, editorial_id=NULL, finished=NULL, attempts=0,
                    code=NULL, error=NULL, technical=NULL, diagnosis=NULL,
                    seconds=0, repair_seconds=NULL, editorial_seconds=NULL
                   WHERE id=? AND ((?=1 AND code='duplicate') OR
                     (?=0 AND (state IN ('review','rejected','failed','cancelled') OR state='ready')))
                   RETURNING run""",
                             (now, job, distinct, distinct)).fetchone()
            if not row:
                raise ValueError("Solo se pueden reintentar trabajos que requieren atención.")
            self._event(db, job, row["run"], "generated", "duplicate_override" if distinct else None,
                        "El operador solicitó revisar el clip como distinto." if distinct else None)

    @staticmethod
    def _selection(db, data):
        row = db.execute("""SELECT detection,editorial,context,diversity,priority,duplicate
            FROM production_evidence WHERE job_id=? AND run=?""", (data["id"], data["run"])).fetchone()
        data.update(dict(row) if row else dict.fromkeys(("detection", "editorial", "context", "diversity", "priority", "duplicate")))
        if data["duplicate"]:
            data["duplicate"] = json.loads(data["duplicate"])
        if data["detection"] is None and data["diagnosis"]:
            data["detection"] = data["diagnosis"].get("detection")
        if not row and data["editorial_id"]:
            historic = db.execute("""SELECT json_extract(result, '$.original_score'),
                json_extract(result, '$.editorial_score') FROM editorial_jobs WHERE id=?""",
                (data["editorial_id"],)).fetchone()
            if historic:
                if data["detection"] is None:
                    data["detection"] = score(historic[0])
                data["editorial"] = score(historic[1])
        return data

    def detail(self, job):
        with self.connect() as db:
            row = db.execute("SELECT * FROM production_jobs WHERE id=?", (job,)).fetchone()
            if row is None:
                raise ValueError("Trabajo de producción desconocido.")
            data = self._selection(db, self._display(row))
            data["events"] = [dict(event) for event in db.execute("""
                SELECT run,state,created,code,message FROM production_events
                WHERE job_id=? ORDER BY id DESC LIMIT 100""", (job,))]
            data["duplicate_history"] = [json.loads(item[0]) for item in db.execute(
                "SELECT duplicate FROM production_evidence WHERE job_id=? AND duplicate IS NOT NULL ORDER BY run DESC", (job,))]
            return data

    @staticmethod
    def _display(row):
        data = dict(row)
        data["diagnosis"] = json.loads(data["diagnosis"]) if data["diagnosis"] else None
        data.pop("owner")
        data.pop("lease")
        return data

    def status(self, before=None, *, settings=None, current=None, view="all", now=None):
        settings = settings or {"timezone": "America/Santo_Domingo", "minimum_clip_score": 6, "daily_publication_limit": 3}
        if view not in {"all", "recommended", "review", "duplicate", "discarded"}:
            raise ValueError("Filtro de producción desconocido.")
        day, start, end = local_day(time.time() if now is None else now, settings["timezone"])
        with self.connect() as db:
            db.execute("BEGIN")
            counts = dict(db.execute("SELECT state, count(*) FROM production_jobs GROUP BY state").fetchall())
            report = db.execute("""SELECT
                    count(*) AS total,
                    count(*) FILTER (WHERE finished IS NULL) AS pending,
                    count(*) FILTER (WHERE code='duplicate') AS duplicates,
                    count(*) FILTER (WHERE technical='passed') AS technical,
                    count(*) FILTER (WHERE active_filename IS NOT NULL AND technical='passed') AS repaired,
                    count(*) FILTER (WHERE active_filename IS NOT NULL AND state='ready') AS manual_repairs_avoided,
                    avg(seconds) FILTER (WHERE finished IS NOT NULL AND state!='cancelled') AS processing,
                    avg(repair_seconds) AS repairs, avg(editorial_seconds) AS editorial
                FROM production_jobs""").fetchone()
            problems = [dict(row) for row in db.execute("""SELECT code, count(*) AS count FROM production_events
                WHERE code IS NOT NULL GROUP BY code ORDER BY count DESC, code LIMIT 8""")]
            ranked = db.execute("""SELECT p.id,p.project,p.filename,e.file,e.evidence
                FROM production_jobs p JOIN production_evidence e ON p.id=e.job_id AND p.run=e.run
                WHERE p.state='ready' AND p.technical='passed' AND p.finished>=? AND p.finished<?
                    AND e.detection>=? AND e.priority IS NOT NULL
                ORDER BY e.priority DESC,e.detection DESC,e.editorial DESC,p.id""",
                (start, end, settings["minimum_clip_score"])).fetchall()
            recommended = []
            for row in ranked:
                if current is None or current(row, json.loads(row["evidence"])):
                    recommended.append(row["id"])
                    if len(recommended) >= min(3, settings["daily_publication_limit"]):
                        break
            clauses = {
                "all": "1",
                "recommended": "p.id IN (SELECT value FROM json_each(?))",
                "review": "(p.state IN ('review','failed') OR (p.state='ready' AND e.priority IS NULL))",
                "duplicate": "p.code='duplicate'",
                "discarded": """((p.state IN ('rejected','cancelled') AND (p.code IS NULL OR p.code!='duplicate'))
                    OR (p.state='ready' AND e.detection < ?))""",
            }
            args = [before, before]
            if view == "recommended":
                args.append(dump(recommended))
            if view == "discarded":
                args.append(settings["minimum_clip_score"])
            jobs = db.execute(f"""SELECT p.* FROM production_jobs p
                LEFT JOIN production_evidence e ON p.id=e.job_id AND p.run=e.run
                WHERE (? IS NULL OR p.id < ?) AND ({clauses[view]})
                ORDER BY p.id DESC LIMIT 31""", args).fetchall()
            display = []
            for row in jobs[:30]:
                job = self._selection(db, self._display(row))
                if job["state"] == "ready" and job["priority"] is not None and current:
                    ev = db.execute("SELECT file,evidence FROM production_evidence WHERE job_id=? AND run=?",
                                    (job["id"], job["run"])).fetchone()
                    if not current({**job, "file": ev["file"]}, json.loads(ev["evidence"])):
                        job.update(state="review", technical=None, code="input_changed",
                                   error="La exportación o sus subtítulos cambiaron. Solicita una reevaluación.")
                        if job["id"] in recommended:
                            recommended.remove(job["id"])
                display.append(job)
            for job in display:
                job["recommendation_rank"] = recommended.index(job["id"]) + 1 if job["id"] in recommended else None
                below_minimum = (job["state"] == "ready" and job["detection"] is not None
                                 and job["detection"] < settings["minimum_clip_score"])
                job["decision"] = ("recommended" if job["recommendation_rank"] else "duplicate" if job["code"] == "duplicate"
                    else "discarded" if below_minimum or job["state"] in {"rejected", "cancelled"} else "review"
                    if job["state"] in {"review", "failed"} or (job["state"] == "ready" and job["priority"] is None)
                    else "eligible" if job["state"] == "ready" else "pending")
                if below_minimum:
                    job["decision_reason"] = "La detección no alcanza el mínimo vigente."
                elif job["decision"] == "review" and job["state"] == "ready":
                    job["decision_reason"] = "El historial requiere reevaluación para aplicar los controles de Automation 3.0."
                elif job["decision"] == "eligible":
                    job["decision_reason"] = "Cumple los controles y queda disponible para revisión manual."
                elif job["decision"] == "recommended":
                    job["decision_reason"] = "Prioridad por detección, calidad editorial, contexto y diferenciación."
            if view == "recommended":
                display.sort(key=lambda job: job["recommendation_rank"] or float("inf"))
        return {"config": self.config(), "counts": counts, "metrics": dict(report), "problems": problems,
                "jobs": display, "next": jobs[29]["id"] if len(jobs) > 30 else None,
                "selection_day": day, "recommended_count": len(recommended),
                "daily_limit": settings["daily_publication_limit"]}
