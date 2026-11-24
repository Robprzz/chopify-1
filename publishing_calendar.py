"""Local editorial plans, explicit submission linkage and restart recovery."""
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import re
import sqlite3
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clip_workspace import combined, copy_fields, digest_for, media_stamp, verify_media
from production_selection import stamp

PLATFORMS = ("tiktok", "youtube", "facebook", "instagram")
PRIORITIES = ("urgent", "high", "normal", "low")
TERMINAL = {"cancelled", "published"}
DEFAULTS = {"timezone": "America/Santo_Domingo", "daily": 3, "interval": 60,
            "hours": ["09:00", "13:00", "19:00"]}


class Conflict(ValueError):
    pass


def zone_for(value):
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError("Elige una zona IANA válida.")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Elige una zona IANA válida, como America/Santo_Domingo.") from None


def instant(local, zone):
    if not isinstance(local, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", local):
        raise ValueError("Indica la fecha y hora en el calendario.")
    plain = datetime.fromisoformat(local)
    if not 1970 <= plain.year <= 2100:
        raise ValueError("Fecha fuera del intervalo admitido.")
    variants = {candidate.timestamp() for candidate in (plain.replace(tzinfo=zone, fold=fold) for fold in (0, 1))
                if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == plain}
    if len(variants) != 1:
        raise ValueError("La hora es ambigua o no existe en esta zona. Elige otra hora o usa UTC.")
    return variants.pop()


def config(data):
    if data.keys() != DEFAULTS.keys():
        raise ValueError("Completa las preferencias del calendario.")
    zone_for(data["timezone"])
    for key, low, high in (("daily", 1, 24), ("interval", 1, 1440)):
        if type(data[key]) is not int or not low <= data[key] <= high:
            raise ValueError("Revisa la cantidad y el intervalo de las programaciones.")
    hours = data["hours"]
    if (not isinstance(hours, list) or not 1 <= len(hours) <= 24
            or not all(isinstance(x, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", x) for x in hours)):
        raise ValueError("Indica horarios completos en formato HH:MM.")
    return {**data, "hours": sorted(set(hours))}


class Calendar:
    def __init__(self, root, production, tiktok, settings):
        self.path = root / ".chopify-publishing.sqlite3"
        self.production, self.tiktok, self.settings = production, tiktok, settings
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS calendar_settings(id INTEGER PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS plans(
                    id TEXT PRIMARY KEY, job INTEGER, run INTEGER, editorial_revision INTEGER,
                    project TEXT NOT NULL, file TEXT NOT NULL, integrity TEXT NOT NULL,
                    platform TEXT NOT NULL, scheduled REAL, zone TEXT NOT NULL, priority TEXT NOT NULL,
                    title TEXT NOT NULL, description TEXT NOT NULL, hashtags TEXT NOT NULL, duration REAL,
                    status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1, attempts INTEGER NOT NULL DEFAULT 0,
                    approval REAL, consent REAL, external_id TEXT UNIQUE, publish_id TEXT,
                    confirmation TEXT, confirmed_at REAL, error TEXT
                );
                CREATE INDEX IF NOT EXISTS plans_schedule ON plans(scheduled);
                CREATE INDEX IF NOT EXISTS plans_media ON plans(platform,project,file);
                CREATE TABLE IF NOT EXISTS calendar_events(
                    id INTEGER PRIMARY KEY, plan TEXT NOT NULL, at REAL NOT NULL, action TEXT NOT NULL, detail TEXT
                );
                CREATE INDEX IF NOT EXISTS calendar_events_plan ON calendar_events(plan,id);
            """)
            if "caption" not in {row[1] for row in db.execute("PRAGMA table_info(plans)")}:
                db.execute("ALTER TABLE plans ADD COLUMN caption TEXT NOT NULL DEFAULT ''")
            db.execute("""CREATE TABLE IF NOT EXISTS inbox_schedule(
                plan TEXT PRIMARY KEY, owner TEXT NOT NULL, revision INTEGER NOT NULL, state TEXT NOT NULL)""")

    def recover_dispatches(self):
        with self.connect(write=True) as db:
            rows = db.execute("""SELECT p.* FROM plans p JOIN inbox_schedule s ON p.id=s.plan
                                 WHERE s.state='claimed'""").fetchall()
            for row in rows:
                db.execute("UPDATE inbox_schedule SET state='uncertain' WHERE plan=?", (row["id"],))
                if not row["external_id"]:
                    self._transition(db, row, "requires_action", "Entrega interrumpida. Comprueba el buzón antes de reintentar.")

    def deliver_due(self):
        with self.connect(write=True) as db:
            row = db.execute("""SELECT p.*,s.owner FROM plans p JOIN inbox_schedule s ON p.id=s.plan
                WHERE s.state='pending' AND s.revision=p.revision AND p.status='scheduled' AND p.scheduled<=?
                ORDER BY p.scheduled LIMIT 1""", (time.time(),)).fetchone()
            if not row:
                return
            if time.time() - row["scheduled"] > 600:
                db.execute("UPDATE inbox_schedule SET state='expired' WHERE plan=?", (row["id"],))
                self._transition(db, row, "requires_action", "La hora autorizada pasó. Confirma otra hora o prepara el envío manual.")
                return
            db.execute("UPDATE inbox_schedule SET state='claimed' WHERE plan=?", (row["id"],))
        try:
            with self.tiktok.lock:
                if not self.tiktok.account or self.tiktok.account["key"] != row["owner"] or self.tiktok.sending:
                    raise ValueError("La cuenta autorizada no está disponible. Revisa la entrega manualmente.")
            review = self.tiktok.inspect(row["project"], row["file"])

            def consent(item):
                if item["owner"] != row["owner"]:
                    raise Conflict("Cambió la cuenta con consentimiento.")
                self.reserve_tiktok(row["id"], row["revision"], item)

            self.tiktok.submit(review["review_id"], combined({**dict(row),
                "hashtags": json.loads(row["hashtags"])}), True, on_consent=consent)
            with self.connect(write=True) as db:
                current = self._get(db, row["id"])
                if not current["external_id"]:
                    self._transition(db, current, "requires_action", "TikTok ya tiene un intento para este archivo. Revisa su historial.")
                db.execute("UPDATE inbox_schedule SET state='done' WHERE plan=?", (row["id"],))
        except (OSError, ValueError, KeyError, sqlite3.Error):
            with self.connect(write=True) as db:
                current = self._get(db, row["id"])
                if current["status"] not in TERMINAL and not current["external_id"]:
                    self._transition(db, current, "requires_action", "No se pudo completar la entrega autorizada. Comprueba la cuenta y el historial.")
                db.execute("UPDATE inbox_schedule SET state='uncertain' WHERE plan=?", (row["id"],))

    @contextmanager
    def connect(self, write=False):
        with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            db.row_factory = sqlite3.Row
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    def preferences(self, db):
        row = db.execute("SELECT data FROM calendar_settings WHERE id=1").fetchone()
        if row:
            return json.loads(row[0])
        return {**DEFAULTS, "timezone": self.settings.read()["settings"]["timezone"]}

    def _clip(self, job, project=None, filename=None):
        run = None
        if job is not None:
            if type(job) is not int or job < 1:
                raise ValueError("Identificador de producción inválido.")
            with self.production.store.connect() as db:
                row = db.execute("""SELECT p.project,p.filename,p.run,e.file FROM production_jobs p
                    LEFT JOIN production_evidence e ON e.job_id=p.id AND e.run=p.run WHERE p.id=?""", (job,)).fetchone()
            if not row or (project and row["project"] != project):
                raise ValueError("Clip desconocido.")
            project, filename = row["project"], filename or row["file"] or row["filename"]
            run = row["run"]
        path = self.tiktok._clip(project, filename)
        digest = digest_for(path, media_stamp(path))
        return {"project": project, "file": filename, "run": run, "editorial_revision": None,
                "integrity": json.dumps(stamp(path)), "approval": None, "hashes": [digest],
                "duration": None, "status": "draft", "title": path.stem[:100],
                "caption": "", "description": "", "hashtags": []}

    def _verify(self, clip, platform):
        path = self.tiktok._clip(clip["project"], clip["file"])
        return verify_media(path, media_stamp(path), platform)

    @staticmethod
    def _event(db, ident, action, detail=None):
        db.execute("INSERT INTO calendar_events(plan,at,action,detail) VALUES(?,?,?,?)",
                   (ident, time.time(), action, detail))

    def _slot(self, db, prefs, when, platform, ident):
        zone = zone_for(prefs["timezone"])
        day = datetime.fromtimestamp(when, zone).date()
        start = datetime.combine(day, datetime.min.time(), zone).timestamp()
        end = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone).timestamp()
        values = (platform, ident)
        base = """SELECT count(*) FROM plans WHERE platform=? AND id!=? AND status!='cancelled' AND scheduled"""
        if db.execute(base + ">=? AND scheduled<?", (*values, start, end)).fetchone()[0] >= prefs["daily"]:
            raise Conflict("Se alcanzó la cantidad diaria de esa plataforma.")
        if db.execute(base + " BETWEEN ? AND ? AND ABS(scheduled-?)<?",
                      (*values, when - prefs["interval"] * 60, when + prefs["interval"] * 60,
                       when, prefs["interval"] * 60)).fetchone()[0]:
            raise Conflict("La programación no respeta el intervalo mínimo de esa plataforma.")

    def _get(self, db, ident):
        row = db.execute("SELECT * FROM plans WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise ValueError("Programación desconocida.")
        return row

    @staticmethod
    def _expect(row, revision):
        if type(revision) is not int or row["revision"] != revision:
            raise Conflict("La programación cambió. Ábrela de nuevo para continuar.")

    @staticmethod
    def _fingerprint(clip):
        return json.dumps({"stamp": json.loads(clip["integrity"]), "sha": clip.get("hashes", [])}, sort_keys=True)

    def _same_version(self, row, clip):
        previous = json.loads(row["integrity"])
        current = json.loads(self._fingerprint(clip))
        return (row["project"], row["file"]) == (clip["project"], clip["file"]) and (
            previous["stamp"] == current["stamp"] and
            (not previous["sha"] or bool(set(previous["sha"]) & set(current["sha"]))))

    def _validate_plan(self, db, row):
        clip = self._clip(row["job"], row["project"], row["file"])
        if not self._same_version(row, clip):
            raise Conflict("La versión del clip cambió. Actualiza el borrador y revísalo.")
        self._verify(clip, row["platform"])
        return clip

    def _duplicate(self, db, ident, clip, platform):
        for row in db.execute("SELECT project,file,integrity FROM plans WHERE platform=? AND id!=? AND status!='cancelled'",
                              (platform, ident)):
            previous = json.loads(row["integrity"])
            if ((clip["project"], clip["file"]) == (row["project"], row["file"])
                    or set(clip.get("hashes", [])) & set(previous["sha"])):
                raise Conflict("Ese clip ya tiene una programación en esta plataforma.")

    def save(self, data):
        if data.keys() - {"id", "revision", "job", "project", "file", "platform", "local", "zone", "priority",
                          "title", "caption", "description", "hashtags", "ready", "refresh_version", "inbox_consent"}:
            raise ValueError("Hay campos de planificación desconocidos.")
        ident = data.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9-]{36}", ident):
            raise ValueError("Identificador de programación inválido.")
        owner = None
        if data.get("inbox_consent") is True:
            if data.get("platform") != "tiktok" or not data.get("local"):
                raise ValueError("El consentimiento corresponde a una entrega fechada al buzón.")
            with self.tiktok.lock:
                if not self.tiktok.account or not self.tiktok.token or "video.upload" not in self.tiktok.token["scopes"]:
                    raise ValueError("Conecta una cuenta de TikTok con permiso para enviar al buzón.")
                owner = self.tiktok.account["key"]
        with self.connect(write=True) as db:
            old = db.execute("SELECT * FROM plans WHERE id=?", (ident,)).fetchone()
            if old and data.get("revision") is None:
                return self._public(old)  # create replay
            if old:
                self._expect(old, data.get("revision"))
                self._unclaimed(db, ident)
                if old["status"] in TERMINAL or old["external_id"]:
                    raise Conflict("La programación ya tiene un resultado o una operación enlazada.")
            platform, priority = data.get("platform"), data.get("priority", "normal")
            if platform not in PLATFORMS or priority not in PRIORITIES:
                raise ValueError("Elige la plataforma y la prioridad.")
            job = old["job"] if old else data.get("job")
            refresh = data.get("refresh_version") is True
            if old and job is None and refresh:
                with self.production.store.connect() as production:
                    match = production.execute("SELECT id FROM production_jobs WHERE project=? AND filename=?",
                                               (old["project"], old["file"])).fetchone()
                    job = match["id"] if match else None
            clip = self._clip(job, old["project"] if old else data.get("project"), old["file"] if old else data.get("file"))
            if old and not refresh and not self._same_version(old, clip):
                raise Conflict("La versión cambió. Actualiza la referencia antes de guardar.")
            self._duplicate(db, ident, clip, platform)
            prefs = self.preferences(db)
            zone = zone_for(data.get("zone", prefs["timezone"]))
            when = instant(data["local"], zone) if data.get("local") else None
            status = "draft"
            if when is not None:
                clip["duration"] = self._verify(clip, platform)["duration"]
                if when <= time.time():
                    raise ValueError("Elige una hora futura o abre la revisión inmediata.")
                self._slot(db, prefs, when, platform, ident)
                status = "scheduled"
            elif data.get("ready") is True:
                clip["duration"] = self._verify(clip, platform)["duration"]
                status = "ready"
            text = copy_fields({**clip, **data}, platform)
            title, description, hashtags = text["title"], text["description"], text["hashtags"]
            now = time.time()
            fields = (job, clip["run"], clip["editorial_revision"], clip["project"], clip["file"],
                      self._fingerprint(clip), platform, when, zone.key, priority, title.strip(), description,
                      json.dumps(hashtags, ensure_ascii=False), clip["duration"], status, now, old["approval"] if old else None)
            if old:
                db.execute("""UPDATE plans SET job=?,run=?,editorial_revision=?,project=?,file=?,integrity=?,
                    platform=?,scheduled=?,zone=?,priority=?,title=?,description=?,hashtags=?,duration=?,status=?,
                    updated=?,approval=?,error=NULL,revision=revision+1 WHERE id=?""", (*fields, ident))
            else:
                db.execute("""INSERT INTO plans(job,run,editorial_revision,project,file,integrity,platform,scheduled,
                    zone,priority,title,description,hashtags,duration,status,updated,approval,id,created)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (*fields, ident, now))
            db.execute("UPDATE plans SET caption=? WHERE id=?", (text["caption"], ident))
            db.execute("DELETE FROM inbox_schedule WHERE plan=?", (ident,))
            if owner:
                db.execute("INSERT INTO inbox_schedule VALUES(?,?,?,'pending')",
                           (ident, owner, self._get(db, ident)["revision"]))
                db.execute("UPDATE plans SET consent=? WHERE id=?", (now, ident))
                self._event(db, ident, "consent", "Entrega al buzón, cuenta y versión vinculadas.")
            elif old:
                db.execute("UPDATE plans SET consent=NULL WHERE id=?", (ident,))
            self._event(db, ident, "updated" if old else "created", status)
            return self._public(self._get(db, ident))

    @staticmethod
    def _public(row):
        result = dict(row)
        result.pop("integrity")
        result["hashtags"] = json.loads(row["hashtags"])
        return result

    @staticmethod
    def _unclaimed(db, ident):
        if db.execute("SELECT 1 FROM inbox_schedule WHERE plan=? AND state='claimed'", (ident,)).fetchone():
            raise Conflict("Hay un envío programado en curso. Consulta su resultado antes de cambiarlo.")

    def _transition(self, db, row, state, reason=None, **fields):
        values = {"status": state, "error": reason, "updated": time.time(), **fields}
        sql = ", ".join(f"{name}=?" for name in values)
        db.execute(f"UPDATE plans SET {sql}, revision=revision+1 WHERE id=?", (*values.values(), row["id"]))
        self._event(db, row["id"], state, reason)

    def reserve_tiktok(self, ident, revision, review):
        with self.connect(write=True) as db:
            row = self._get(db, ident)
            self._expect(row, revision)
            if row["platform"] != "tiktok" or row["status"] in TERMINAL or row["external_id"]:
                raise Conflict("Esta programación ya tiene un envío o no admite TikTok.")
            clip = self._validate_plan(db, row)
            if ((row["project"], row["file"]) != (review["project"], review["filename"])
                    or review["digest"] not in clip.get("hashes", [])):
                raise Conflict("Selecciona en TikTok el archivo exacto de la programación.")
            # This callback runs only after TikTok validates the account, consent,
            # file review and duplicates, for explicit browser or persisted consent.
            self._transition(db, row, "requires_action", "Consentimiento registrado. Esperando evidencia del envío.",
                             consent=time.time(), external_id=review["id"], attempts=row["attempts"] + 1)

    def _recover(self, db, row, receipts, now, sending):
        if row["status"] in TERMINAL:
            return
        if row["external_id"]:
            receipt = receipts.get(row["external_id"])
            key = receipt.get("status") if receipt else None
            if key == "PUBLISH_COMPLETE":
                self._transition(db, row, "published", confirmation="api", publish_id=receipt.get("publish_id"),
                                 confirmed_at=receipt.get("checked_at") or receipt.get("updated"))
                return
            if key in {"FAILED", "INIT_REJECTED", "LOCAL_ERROR"}:
                state, reason = "failed", "El historial de TikTok registra una operación fallida."
            elif key in {"PROCESSING_UPLOAD", "UPLOADING", "STARTING"} and (sending or key == "PROCESSING_UPLOAD"):
                state, reason = "processing", None
            elif key == "SEND_TO_USER_INBOX":
                state, reason = "requires_action", "En el buzón de TikTok. Completa la revisión en la plataforma."
            else:
                state, reason = "requires_action", "Resultado sin confirmar. Consulta el historial o el buzón antes de actuar."
            publish_id = receipt.get("publish_id") if receipt else row["publish_id"]
            if (row["status"], row["error"], row["publish_id"]) != (state, reason, publish_id):
                self._transition(db, row, state, reason, publish_id=publish_id)
            return
        if row["scheduled"] is not None and row["scheduled"] <= now and row["status"] == "scheduled":
            if db.execute("SELECT 1 FROM inbox_schedule WHERE plan=? AND state IN ('pending','claimed')",
                          (row["id"],)).fetchone():
                return
            try:
                self._validate_plan(db, row)
                reason = "Horario vencido. Revisa manualmente el siguiente paso."
            except (OSError, ValueError):
                reason = "Horario vencido y archivo pendiente de comprobar."
            self._transition(db, row, "requires_action", reason)

    def _receipts(self):
        if not self.tiktok.lock.acquire(blocking=False):
            return None
        try:
            return {item["id"]: item for item in self.tiktok._account_history()}, self.tiktok.sending
        finally:
            self.tiktok.lock.release()

    def snapshot(self, ident=None, *, around=None):
        provider = self._receipts()
        receipts, sending = provider or ({}, False)
        now = time.time()
        with self.connect(write=True) as db:
            for row in db.execute("""SELECT * FROM plans WHERE status NOT IN ('cancelled','published')
                             AND (external_id IS NOT NULL OR (status='scheduled' AND scheduled<=?))""", (now,)).fetchall():
                if not row["external_id"] or provider:
                    self._recover(db, row, receipts, now, sending)
            prefs = self.preferences(db)
            if ident:
                row = self._get(db, ident)
                item = self._public(row)
                delivery = db.execute("SELECT state FROM inbox_schedule WHERE plan=?", (ident,)).fetchone()
                item["inbox_state"] = delivery[0] if delivery else None
                try:
                    current = self._clip(row["job"], row["project"], row["file"])
                    item["stale"] = not self._same_version(row, current)
                    item["media_available"] = not item["stale"]
                    item["can_link_production"] = row["job"] is None
                except (OSError, ValueError):
                    item.update(stale=True, media_available=False)
                item["history"] = [dict(event) for event in db.execute(
                    "SELECT at,action,detail FROM calendar_events WHERE plan=? ORDER BY id DESC LIMIT 100", (ident,))]
                return item
            zone = zone_for(prefs["timezone"])
            target = date.fromisoformat(around) if around else datetime.fromtimestamp(now, zone).date()
            if not 1970 <= target.year <= 2100:
                raise ValueError("Fecha fuera del intervalo admitido.")
            start = datetime.combine(target - timedelta(days=42), datetime.min.time(), zone).timestamp()
            end = datetime.combine(target + timedelta(days=43), datetime.min.time(), zone).timestamp()
            rows = db.execute("""SELECT * FROM plans WHERE scheduled>=? AND scheduled<?
                UNION SELECT * FROM plans WHERE id IN (
                    SELECT id FROM plans WHERE scheduled>? AND status NOT IN ('cancelled','published')
                    ORDER BY scheduled LIMIT 40
                )
                UNION SELECT * FROM plans WHERE id IN (
                    SELECT id FROM plans WHERE scheduled IS NULL ORDER BY created DESC LIMIT 120
                )
                UNION SELECT * FROM plans WHERE id IN (
                    SELECT id FROM plans WHERE status IN ('requires_action','failed','processing')
                    ORDER BY updated DESC LIMIT 80
                )
                ORDER BY scheduled,created DESC""", (start, end, now))
            return {"preferences": prefs, "plans": [self._public(row) for row in rows]}

    def statistics(self, cutoff):
        with self.connect() as db:
            row = db.execute("""SELECT
                count(*) FILTER (WHERE scheduled>=? AND status!='cancelled') AS scheduled,
                count(*) FILTER (WHERE confirmed_at>=? AND confirmation='manual') AS manual,
                count(*) FILTER (WHERE confirmed_at>=? AND confirmation='api') AS api,
                count(*) FILTER (WHERE updated>=? AND status='failed') AS errors
                FROM plans""", (cutoff, cutoff, cutoff, cutoff)).fetchone()
            return dict(row)

    def action(self, action, data):
        if action == "save":
            return self.save(data)
        if action == "preferences":
            normalized = config(data)
            with self.connect(write=True) as db:
                db.execute("INSERT OR REPLACE INTO calendar_settings VALUES(1,?)", (json.dumps(normalized),))
            return normalized
        if action == "suggest":
            platform = data.get("platform")
            priority = data.get("priority", "normal")
            if platform not in PLATFORMS or priority not in PRIORITIES:
                raise ValueError("Elige la plataforma y la prioridad.")
            with self.connect() as db:
                prefs = self.preferences(db)
                zone = zone_for(prefs["timezone"])
                now = datetime.now(zone)
                observed = [datetime.fromtimestamp(row[0], zone).strftime("%H:%M") for row in db.execute(
                    "SELECT scheduled FROM plans WHERE platform=? AND status='published' AND scheduled IS NOT NULL", (platform,))]
                for offset in range(14):
                    hours = prefs["hours"]
                    if len(observed) >= 5 and priority not in {"urgent", "high"}:
                        hours = sorted(hours, key=lambda hour: (-observed.count(hour), hour))
                    for hour in hours:
                        local = f"{now.date() + timedelta(days=offset)}T{hour}"
                        try:
                            when = instant(local, zone)
                            if when <= now.timestamp():
                                continue
                            self._slot(db, prefs, when, platform, data.get("id") or "")
                            return {"local": local, "zone": zone.key, "basis": "heuristic",
                                    "history_samples": len(observed)}
                        except ValueError:
                            continue
            raise Conflict("No hay franjas disponibles en los próximos 14 días.")
        if action not in {"cancel", "confirm", "prepare", "retry"}:
            raise ValueError("Acción de calendario desconocida.")
        with self.connect(write=True) as db:
            row = self._get(db, data.get("id"))
            self._expect(row, data.get("revision"))
            self._unclaimed(db, row["id"])
            if row["status"] in TERMINAL:
                raise Conflict("La programación ya está cerrada.")
            if action == "cancel":
                if row["external_id"]:
                    raise Conflict("Consulta primero el resultado de la operación externa.")
                db.execute("DELETE FROM inbox_schedule WHERE plan=?", (row["id"],))
                self._transition(db, row, "cancelled")
            elif action == "retry":
                if row["status"] != "failed" or row["publish_id"]:
                    raise Conflict("La operación requiere comprobación externa antes de otro intento.")
                self._validate_plan(db, row)
                self._transition(db, row, "requires_action", "Abre una revisión nueva con consentimiento.",
                                 external_id=None, consent=None)
            else:
                self._validate_plan(db, row)
                if row["external_id"]:
                    raise Conflict("Ya hay una operación de TikTok registrada.")
                if action == "confirm":
                    if row["platform"] == "tiktok" or data.get("confirmed") is not True:
                        raise ValueError("Se requiere confirmación manual para una plataforma sin conexión.")
                    self._transition(db, row, "published", confirmation="manual", confirmed_at=time.time())
                else:
                    if row["platform"] != "tiktok":
                        self._transition(db, row, "requires_action", "Paquete preparado. Completa la publicación manual.")
                    return self._public(self._get(db, row["id"]))
            return self._public(self._get(db, row["id"]))
