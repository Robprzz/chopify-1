"""Additive SQLite storage for editorial jobs. Publication state lives elsewhere."""
from collections import Counter
from contextlib import closing, contextmanager
import json
from math import ceil
from statistics import median
import sqlite3
import time

from editorial_content import DEFAULT_CONFIG, STYLES, validate_config, validate_edits


MAX_PENDING = 16
STAGES = frozenset({"understanding", "generating", "validating_proposals", "autocorrecting"})


def editor_status(row, result):
    if row["state"] == "cancelled":
        return "unassessed"
    current = row["editorial_state"]
    if current:
        return current
    if row["state"] == "failed":
        return "review"
    if row["state"] != "done":
        return "unassessed"
    result = result or {}
    if result.get("editorial_score", 0) <= 0 or not result.get("recommended_variant"):
        return "review"
    return "valid" if (result.get("mode") == "semantic"
                       and any(item.get("style") == result["recommended_variant"] and item.get("score", 0) > 0
                               for item in result.get("variants", []))) else "extractive"


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
            columns = {row["name"] for row in db.execute("PRAGMA table_info(editorial_jobs)")}
            for name, datatype in (("editorial_state", "TEXT"), ("diagnostics", "TEXT"), ("previous_id", "INTEGER")):
                if name not in columns:
                    db.execute(f"ALTER TABLE editorial_jobs ADD COLUMN {name} {datatype}")
            db.execute("CREATE INDEX IF NOT EXISTS editorial_fingerprint ON editorial_jobs(fingerprint, id)")
            db.execute("CREATE INDEX IF NOT EXISTS editorial_clip ON editorial_jobs(clip_id, id)")
            db.execute("CREATE INDEX IF NOT EXISTS editorial_previous ON editorial_jobs(previous_id)")
            db.execute("CREATE INDEX IF NOT EXISTS editorial_state ON editorial_jobs(state, id)")
            db.execute("""CREATE INDEX IF NOT EXISTS editorial_production
                          ON editorial_jobs(json_extract(settings, '$.production[0]'),
                                            json_extract(settings, '$.production[1]'))""")

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

    def reusable_production(self, source, expected, known_digest):
        from editorial_semantic import eligible

        with self.connect() as db:
            rows = db.execute("""SELECT id,source,settings,result FROM editorial_jobs
                WHERE clip_id=? AND state='done' AND result IS NOT NULL ORDER BY id DESC""", (source["clip_id"],))
            for row in rows:
                settings = json.loads(row["settings"])
                digest = settings.get("model_digest")
                if (digest and (not known_digest or known_digest == digest)
                        and all(settings.get(key) == value for key, value in expected.items())
                        and json.loads(row["source"]) == source and eligible(json.loads(row["result"]))):
                    return row["id"]
        return None

    def submit(self, source, settings, fingerprint, force, previous_id=None):
        clip_id = source["clip_id"]
        with self.connect(write=True) as db:
            if "production" in settings:
                registered = db.execute("""SELECT id FROM editorial_jobs
                    WHERE json_extract(settings, '$.production[0]')=? AND json_extract(settings, '$.production[1]')=?
                    LIMIT 1""", tuple(settings["production"])).fetchone()
                if registered:
                    return registered["id"], True
            row = db.execute("""
                SELECT id FROM editorial_jobs WHERE fingerprint=? AND state IN ('queued','running')
                ORDER BY id DESC LIMIT 1
            """, (fingerprint,)).fetchone()
            if row:
                return row["id"], True
            if previous_id is not None:
                row = db.execute("SELECT id FROM editorial_jobs WHERE fingerprint=? AND previous_id=? AND state!='cancelled' ORDER BY id DESC LIMIT 1",
                                 (fingerprint, previous_id)).fetchone()
                if row:
                    return row["id"], True
            if not force:
                row = db.execute("""
                    SELECT id, result FROM editorial_jobs WHERE fingerprint=? AND state='done'
                    ORDER BY id DESC LIMIT 1
                """, (fingerprint,)).fetchone()
                if row and row["result"] and (not settings["model_digest"] or json.loads(row["result"]).get("provider") == "ollama"):
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
                INSERT INTO editorial_jobs(clip_id, fingerprint, source, settings, created, state, edits, selected, previous_id)
                VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)
            """, (clip_id, fingerprint, dump(source), dump(settings), time.time(), edits, selected, previous_id))
            return cursor.lastrowid, False

    def claim(self, owner):
        now = time.time()
        with self.connect(write=True) as db:
            db.execute("""
                UPDATE editorial_jobs SET state=CASE cancel_requested WHEN 1 THEN 'cancelled' ELSE 'queued' END,
                owner=NULL, lease=NULL, editorial_state=NULL, finished=CASE cancel_requested WHEN 1 THEN ? ELSE NULL END,
                revision=revision+1 WHERE state='running' AND lease < ?
            """, (now, now))
            if db.execute("SELECT 1 FROM editorial_jobs WHERE state='running'").fetchone():
                return None
            row = db.execute("SELECT * FROM editorial_jobs WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
            if row:
                settings = json.loads(row["settings"])
                db.execute("""UPDATE editorial_jobs SET state='running', owner=?, lease=?, revision=revision+1
                              WHERE id=?""", (owner, now + 15, row["id"]))
                return row["id"], json.loads(row["source"]), settings
            return None

    def renew(self, job, owner):
        with self.connect(write=True) as db:
            row = db.execute("""UPDATE editorial_jobs SET lease=? WHERE id=? AND owner=? AND state='running'
                              RETURNING cancel_requested""", (time.time() + 15, job, owner)).fetchone()
            return row is not None and not row[0]

    def stage(self, job, owner, stage):
        if stage not in STAGES:
            raise ValueError("Etapa editorial desconocida.")
        with self.connect(write=True) as db:
            db.execute("""UPDATE editorial_jobs SET editorial_state=?, revision=revision+1
                          WHERE id=? AND owner=? AND state='running' AND editorial_state IS NOT ?""",
                       (stage, job, owner, stage))

    def finish(self, job, owner, state, result, error, elapsed, diagnostics=None):
        with self.connect(write=True) as db:
            db.execute("""
                UPDATE editorial_jobs SET
                  state=CASE cancel_requested WHEN 1 THEN 'cancelled' ELSE ? END,
                  result=CASE cancel_requested WHEN 1 THEN NULL ELSE ? END,
                  error=?, elapsed=?, editorial_state=CASE cancel_requested WHEN 1 THEN NULL ELSE ? END, diagnostics=?, finished=CASE WHEN cancel_requested=1 OR ?!='queued' THEN ? ELSE NULL END,
                  owner=NULL, lease=NULL, revision=revision+1
                WHERE id=? AND owner=? AND state='running'
            """, (state, dump(result) if result else None, error, elapsed,
                  (result or {}).get("editorial_state") or ("review" if state == "failed" else None),
                  dump(diagnostics) if diagnostics else None, state, time.time(), job, owner))

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
                       editorial_state, diagnostics, previous_id,
                       json_extract(source, '$.transcript_id') AS transcript_id
                FROM editorial_jobs WHERE id=?
            """, (job,)).fetchone()
        if not row:
            raise ValueError("Trabajo editorial desconocido.")
        result = dict(row)
        for field in ("settings", "result", "edits", "diagnostics"):
            result[field] = json.loads(result[field]) if result[field] else None
        result["editorial_state"] = editor_status(row, result["result"])
        if result["previous_id"] is not None:
            with self.connect() as db:
                previous = db.execute("""SELECT json_extract(result, '$.editorial_score') AS score,
                    json_extract(result, '$.prompt_version') AS version FROM editorial_jobs WHERE id=?""",
                                      (result["previous_id"],)).fetchone()
            result["previous"] = dict(previous) if previous else None
        return result

    def prior_source(self, job):
        with self.connect() as db:
            row = db.execute("SELECT source FROM editorial_jobs WHERE id=?", (job,)).fetchone()
        if row is None:
            raise ValueError("Trabajo editorial desconocido.")
        return json.loads(row["source"])

    def reprocess_candidates(self, mode, clip_id=None):
        if not isinstance(mode, str) or mode not in {"failed", "zero"}:
            raise ValueError("Filtro editorial no disponible.")
        with self.connect() as db:
            condition = """state='failed'""" if mode == "failed" else """state='done' AND
              (json_extract(result, '$.editorial_score')=0 OR json_extract(result, '$.recommended_variant') IS NULL)"""
            rows = db.execute(f"""SELECT max(id) FROM editorial_jobs AS original
                WHERE ({condition}) AND (? IS NULL OR clip_id=?) AND json_extract(source, '$.project') IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM editorial_jobs AS newer WHERE newer.previous_id=original.id AND newer.state!='cancelled')
                GROUP BY clip_id, json_extract(settings, '$.platform')
                ORDER BY max(id) DESC LIMIT ?""", (clip_id, clip_id, MAX_PENDING)).fetchall()
        return [row[0] for row in rows]

    def history(self, clip_id, before):
        with self.connect() as db:
            rows = db.execute("""
                SELECT id, clip_id, state, created, elapsed, error, revision, editorial_state, result, previous_id,
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
            active = db.execute("""SELECT id, editorial_state, json_extract(settings, '$.model') AS model,
                json_extract(settings, '$.prompt_version') AS version, json_extract(settings, '$.semantic') AS semantic
                FROM editorial_jobs WHERE state='running' ORDER BY id LIMIT 1""").fetchone()
            summary = db.execute("""SELECT state, editorial_state, diagnostics, elapsed, result IS NOT NULL AS has_result,
                CASE WHEN editorial_state IS NULL THEN result END AS result
                FROM editorial_jobs WHERE state IN ('done','failed','cancelled')
                ORDER BY id DESC LIMIT 1000""").fetchall()
        outcomes, errors, calls, repairs = Counter(), Counter(), 0, 0
        elapsed_samples = []
        technical = finished = valid = restored = assessed = verified = tried_repair = 0
        for row in summary:
            result = json.loads(row["result"]) if row["result"] else None
            diagnostic = json.loads(row["diagnostics"]) if row["diagnostics"] else {}
            status_name = editor_status(row, result)
            outcomes[status_name] += 1
            technical += row["state"] == "done"
            finished += row["state"] in ("done", "failed")
            valid += status_name in ("valid", "valid_warnings")
            verified += row["editorial_state"] in ("valid", "valid_warnings")
            assessed += bool(row["editorial_state"] in ("valid", "valid_warnings", "review") and row["has_result"])
            tried_repair += bool(diagnostic.get("repair_calls"))
            restored += bool(diagnostic.get("verified_corrections"))
            calls += diagnostic.get("model_calls", 0)
            repairs += diagnostic.get("retries", 0)
            errors.update(item["code"] for item in diagnostic.get("issues", []))
            if row["elapsed"] is not None and row["state"] in ("done", "failed"):
                elapsed_samples.append(row["elapsed"])
        elapsed_samples.sort()

        def fraction(numerator, denominator):
            return round(numerator / denominator, 4) if denominator else None

        fidelity_codes = ("missing_evidence", "unsupported_entity", "unsupported_number", "attribution", "review")
        metrics = {"technical": technical, "finished": finished, "valid": valid, "repaired": restored,
                   "sample_size": len(summary), "assessed": assessed, "with_repair_attempts": tried_repair,
                   "completion_ratio": fraction(technical, finished),
                   "usable_ratio": fraction(verified, assessed),
                   "recovery_ratio": fraction(restored, tried_repair),
                   "review_ratio": fraction(outcomes["review"], finished),
                   "fidelity_flags": sum(errors[code] for code in fidelity_codes),
                   "editorial_states": dict(outcomes), "model_calls": calls, "retries": repairs,
                   "median_seconds": median(elapsed_samples) if elapsed_samples else None,
                   "p95_seconds": elapsed_samples[ceil(len(elapsed_samples) * .95) - 1] if len(elapsed_samples) >= 20 else None,
                   "top_issues": errors.most_common(6)}
        jobs = []
        for row in rows[:30]:
            job = dict(row)
            raw = job.pop("result")
            job["editorial_state"] = editor_status(row, json.loads(raw) if raw else None)
            jobs.append(job)
        return {"jobs": jobs, "next": rows[29]["id"] if len(rows) > 30 else None, "metrics": metrics,
                "counts": status, "active": dict(active) if active else None, "average_seconds": timing, "last_error": last[0] if last else None,
                "last_provider": provider[0] if provider else None}
