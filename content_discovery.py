"""Local editorial discovery over the existing YouTube monitor database."""
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

import projects
import studio_json
import youtube_monitor

WEIGHTS = {"affinity": 30, "freshness": 25, "conversation": 20, "narrative": 15, "verifiable": 10}
RIGHTS = {"pending", "cleared", "restricted"}
STATES = {"inbox", "saved", "discarded", "approved"}
ACTIONS = {"save": "saved", "discard": "discarded", "approve": "approved", "restore": "inbox", "edit": None}
CONVERSATION = ("debate", "entrevista", "preguntas", "preguntas y respuestas", "mesa redonda", "interview", "roundtable", "q&a")
NARRATIVE = ("historia", "crónica", "cronología", "testimonio", "paso a paso", "recorrido", "documental", "story", "timeline")
ALGORITHM = 1


class Conflict(ValueError):
    pass


def normalized(value):
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[^\W_]+", plain))


def matches(text, words):
    return [word for word in words if (key := normalized(word)) and f" {key} " in f" {text} "]


def evaluate(title, description, topics):
    """Measure signals in public metadata and state where evidence is missing."""
    headline = normalized(title)
    text = normalized(f"{title} {description}")
    found = [(topic["name"], matches(text, topic["keywords"])) for topic in topics]
    evidence = [(name, words) for name, words in found if words]
    affinity = (min(100, 50 + 15 * len({normalized(w) for _, words in evidence for w in words}))
                if evidence else 0 if topics and text else None)
    talk = matches(text, CONVERSATION)
    story = matches(text, NARRATIVE)
    question = "?" in title and bool(headline)
    return {
        "affinity": (affinity, "; ".join(f"{name}: {', '.join(words)}" for name, words in evidence)
                     or ("Sin coincidencias con el vocabulario configurado." if affinity == 0
                         else "Configura palabras clave para evaluar la afinidad temática.")),
        "conversation": (min(100, 55 + len(talk) * 15 + (10 if question else 0)) if talk or question else None,
                         ("Señales en los metadatos: " + ", ".join(talk + (["pregunta en el título"] if question else [])))
                         if talk or question else "Los metadatos no muestran un formato de conversación."),
        "narrative": (min(100, 60 + len(story) * 15) if story else None,
                      ("Marcadores narrativos: " + ", ".join(story)) if story else "Sin marcadores narrativos en los metadatos."),
        "verifiable": (None, "La verificación de afirmaciones requiere fuentes adicionales."),
    }


def date_bound(value, end=False):
    try:
        day = date.fromisoformat(value)
        if end:
            day += timedelta(days=1)
        return datetime.combine(day, time.min, timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Indica una fecha ISO válida.") from None


def topics_input(topics):
    if not isinstance(topics, list) or len(topics) > 30:
        raise ValueError("Configura hasta 30 temas.")
    entries = {}
    for topic in topics:
        if not isinstance(topic, dict):
            raise ValueError("Cada tema requiere un nombre y palabras clave.")
        name, keywords = topic.get("name"), topic.get("keywords")
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or not normalized(name)
                or not isinstance(keywords, list) or not 1 <= len(keywords) <= 30
                or any(not isinstance(word, str) or not 1 <= len(word.strip()) <= 60 or not normalized(word)
                       for word in keywords)):
            raise ValueError("Cada tema admite un nombre y entre 1 y 30 palabras clave válidas.")
        key = normalized(name)
        if key in entries:
            raise ValueError("Los nombres de los temas deben ser distintos.")
        entries[key] = {"name": name.strip(), "keywords": list({normalized(w): w.strip() for w in keywords}.values())}
    return list(entries.values())


class Discovery:
    def __init__(self, monitor, root):
        self.monitor, self.root = monitor, Path(root)
        with monitor.connect(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS discovery_rules (
                revision INTEGER PRIMARY KEY, weights TEXT NOT NULL, created_at REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS discovery_topics (
                revision INTEGER NOT NULL REFERENCES discovery_rules(revision), name TEXT NOT NULL, keywords TEXT NOT NULL,
                PRIMARY KEY(revision,name))""")
            db.execute("""CREATE TABLE IF NOT EXISTS discovery_evaluations (
                channel TEXT NOT NULL, video TEXT NOT NULL, revision INTEGER NOT NULL, metadata_revision INTEGER NOT NULL,
                algorithm INTEGER NOT NULL, affinity REAL, conversation REAL, narrative REAL, verifiable REAL,
                evidence TEXT NOT NULL, assessed_at REAL NOT NULL, PRIMARY KEY(channel,video),
                FOREIGN KEY(channel,video) REFERENCES videos(channel,id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS discovery_reviews (
                channel TEXT NOT NULL, video TEXT NOT NULL, state TEXT NOT NULL, notes TEXT NOT NULL,
                rights TEXT NOT NULL, rights_note TEXT NOT NULL, revision INTEGER NOT NULL,
                updated_at REAL NOT NULL, PRIMARY KEY(channel,video),
                FOREIGN KEY(channel,video) REFERENCES videos(channel,id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS discovery_actions (
                request_id TEXT PRIMARY KEY, channel TEXT NOT NULL, video TEXT NOT NULL, action TEXT NOT NULL,
                payload TEXT NOT NULL, occurred_at REAL NOT NULL, seconds INTEGER NOT NULL,
                FOREIGN KEY(channel,video) REFERENCES videos(channel,id))""")
            db.execute("CREATE INDEX IF NOT EXISTS discovery_review_state ON discovery_reviews(state,updated_at)")
            db.execute("INSERT OR IGNORE INTO discovery_rules VALUES (1,?,?)",
                       (studio_json.dumps(WEIGHTS), self.monitor.clock()))

    @staticmethod
    def _rules(db):
        row = db.execute("SELECT * FROM discovery_rules ORDER BY revision DESC LIMIT 1").fetchone()
        topics = [dict(name=item["name"], keywords=json.loads(item["keywords"])) for item in db.execute(
            "SELECT name,keywords FROM discovery_topics WHERE revision=? ORDER BY name", (row["revision"],))]
        return {"revision": row["revision"], "weights": json.loads(row["weights"]), "topics": topics}

    def rules(self, data=None):
        with self.monitor.connect(write=data is not None) as db:
            saved = self._rules(db)
            if data is None:
                return saved
            weights = data.get("weights")
            if (not isinstance(weights, dict) or set(weights) != set(WEIGHTS)
                    or any(type(weight) is not int or not 0 <= weight <= 100 for weight in weights.values())
                    or sum(weights.values()) != 100):
                raise ValueError("Los pesos deben ser porcentajes enteros que sumen 100.")
            topics = topics_input(data.get("topics"))
            if data.get("revision") != saved["revision"]:
                raise Conflict("Las reglas cambiaron. Recárgalas antes de guardar.")
            if weights == saved["weights"] and sorted(topics, key=lambda item: item["name"]) == saved["topics"]:
                return saved
            revision = saved["revision"] + 1
            db.execute("INSERT INTO discovery_rules VALUES (?,?,?)",
                       (revision, studio_json.dumps(weights), self.monitor.clock()))
            db.executemany("INSERT INTO discovery_topics VALUES (?,?,?)",
                           ((revision, topic["name"], studio_json.dumps(topic["keywords"])) for topic in topics))
            return self._rules(db)

    def _index(self, db, rules):
        rows = db.execute("""SELECT v.channel,v.id,v.title,v.description,v.metadata_revision FROM videos v
            LEFT JOIN discovery_evaluations e ON e.channel=v.channel AND e.video=v.id
            WHERE e.video IS NULL OR e.revision!=? OR e.algorithm!=? OR e.metadata_revision!=v.metadata_revision""",
            (rules["revision"], ALGORITHM)).fetchall()
        now = self.monitor.clock()
        for row in rows:
            signals = evaluate(row["title"], row["description"], rules["topics"])
            db.execute("""INSERT INTO discovery_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(channel,video) DO UPDATE SET revision=excluded.revision,
                metadata_revision=excluded.metadata_revision,algorithm=excluded.algorithm,
                affinity=excluded.affinity,conversation=excluded.conversation,narrative=excluded.narrative,
                verifiable=excluded.verifiable,evidence=excluded.evidence,assessed_at=excluded.assessed_at""",
                (row["channel"], row["id"], rules["revision"], row["metadata_revision"], ALGORITHM,
                 *(signals[key][0] for key in ("affinity", "conversation", "narrative", "verifiable")),
                 studio_json.dumps({key: signal[1] for key, signal in signals.items()}), now))

    def sources(self):
        with self.monitor.connect() as db:
            return [dict(row, url="https://www.youtube.com/channel/" + row["id"])
                    for row in db.execute("""SELECT c.id,c.name,c.active,c.mode,c.category,c.interval,c.authorized,
                        c.checked,c.next_check,c.error,c.errors,
                        (SELECT count(*) FROM videos v WHERE v.channel=c.id) AS videos
                        FROM channels c ORDER BY c.name COLLATE NOCASE,c.id""")]

    def source(self, data):
        fields = {"mode", "category", "interval", "active"}
        payload = {key: data[key] for key in fields if key in data}
        if "id" in data:
            if not isinstance(data["id"], str) or not youtube_monitor.CHANNEL.fullmatch(data["id"]):
                raise ValueError("Identificador de canal inválido.")
            payload["id"] = data["id"]
            self.monitor.action("settings", payload)
        else:
            payload.update(source=data.get("source"), mode=payload.get("mode", "discover"),
                           interval=payload.get("interval", 180))
            self.monitor.action("add", payload)
        return self.sources()

    def _filters(self, query):
        from video_types import VIDEO_TYPES
        conditions, values = [], []
        for key, column, allowed in [
            ("channel", "v.channel", youtube_monitor.CHANNEL.fullmatch),
            ("category", "c.category", lambda value: value in {"auto", *VIDEO_TYPES}),
            ("state", "coalesce(r.state,'inbox')", STATES.__contains__),
            ("rights", "coalesce(r.rights,'pending')", RIGHTS.__contains__),
        ]:
            value = query.get(key)
            if value:
                if not allowed(value):
                    raise ValueError("El filtro solicitado no es válido.")
                conditions.append(column + "=?")
                values.append(value)
        text = query.get("q", "").strip()
        if len(text) > 150:
            raise ValueError("La búsqueda admite hasta 150 caracteres.")
        if text:
            terms = normalized(text).split()
            for term in terms:
                conditions.append("""instr(discovery_text(v.title || ' ' || v.description || ' ' || c.name ||
                    ' ' || coalesce(r.notes,'') || ' ' ||
                    CASE WHEN e.affinity>0 THEN coalesce(json_extract(e.evidence,'$.affinity'),'') ELSE '' END),?)>0""")
                values.append(term)
        for key, op in (("from", ">="), ("to", "<")):
            if query.get(key):
                conditions.append("v.published" + op + "?")
                values.append(date_bound(query[key], end=key == "to"))
        if query.get("from") and query.get("to") and date_bound(query["from"]) >= date_bound(query["to"], True):
            raise ValueError("La fecha inicial debe preceder a la fecha final.")
        if query.get("library") == "1":
            conditions.append("r.state IN ('saved','approved')")
        return (" WHERE " + " AND ".join(conditions) if conditions else ""), values

    def browse(self, query):
        page, limit = int(query.get("page", 1)), int(query.get("limit", 18))
        if not 1 <= page <= 1_000_000 or not 1 <= limit <= 60:
            raise ValueError("Página o tamaño de página inválidos.")
        order = query.get("sort", "score")
        if order not in {"score", "date"}:
            raise ValueError("Orden no válido.")
        now = int(self.monitor.clock())
        at = int(query.get("at", now))
        if not 0 <= at <= now + 1:
            raise ValueError("Fecha de evaluación inválida.")
        where, values = self._filters(query)
        where += (" AND " if where else " WHERE ") + "(v.detected_at IS NULL OR v.detected_at<?)"
        values.append(at + 1)
        joins = """FROM videos v JOIN channels c ON c.id=v.channel
            JOIN discovery_evaluations e ON e.channel=v.channel AND e.video=v.id
            LEFT JOIN discovery_reviews r ON r.channel=v.channel AND r.video=v.id"""
        with self.monitor.connect(write=True) as db:
            db.create_function("discovery_text", 1, normalized, deterministic=True)
            rules = self._rules(db)
            self._index(db, rules)
            weights = {key: int(value) for key, value in rules["weights"].items()}
            numerator = "+".join(f"coalesce({key},0)*{weights[key]}" for key in WEIGHTS)
            assessed = "+".join(f"CASE WHEN {key} IS NULL THEN 0 ELSE {weights[key]} END" for key in WEIGHTS)
            total = db.execute(f"SELECT count(*) {joins}{where}", values).fetchone()[0]
            order_sql = "score DESC NULLS LAST, " if order == "score" else ""
            rows = db.execute(f"""WITH visible AS (
                SELECT v.channel,v.id,v.title,v.published,v.description,v.project,v.detected_at,
                       c.name AS channel_name,c.category,e.affinity,e.conversation,e.narrative,e.verifiable,
                       CASE WHEN v.published>0 AND v.published<=? THEN max(0,100*(1-(?-v.published)/1209600.0)) END AS freshness,
                       e.evidence,coalesce(r.state,'inbox') AS state,coalesce(r.rights,'pending') AS rights,
                       coalesce(r.notes,'') AS notes,coalesce(r.rights_note,'') AS rights_note,coalesce(r.revision,0) AS revision
                {joins}{where})
                SELECT *, ({assessed}) AS coverage, round(1.0*({numerator})/nullif(({assessed}),0),1) AS score
                FROM visible ORDER BY {order_sql} published DESC,channel,id LIMIT ? OFFSET ?""",
                (at, at, *values, limit, (page - 1) * limit)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            reasons = json.loads(item.pop("evidence"))
            reasons["freshness"] = ("Antigüedad desde la publicación. El valor baja linealmente a cero en 14 días."
                                    if row["freshness"] is not None else "Sin fecha de publicación válida para esta evaluación.")
            item["dimensions"] = {key: {"score": item.pop(key), "reason": reasons[key], "weight": rules["weights"][key]}
                                  for key in WEIGHTS}
            item["url"] = "https://www.youtube.com/watch?v=" + item["id"]
            items.append(item)
        return {"items": items, "total": total, "page": page, "limit": limit, "at": at}

    @staticmethod
    def _video(db, channel, video):
        if (not isinstance(channel, str) or not youtube_monitor.CHANNEL.fullmatch(channel)
                or not isinstance(video, str) or not youtube_monitor.VIDEO.fullmatch(video)):
            raise ValueError("Identidad de video inválida.")
        row = db.execute("SELECT * FROM videos WHERE channel=? AND id=?", (channel, video)).fetchone()
        if row is None:
            raise ValueError("La oportunidad no existe.")
        return row

    @staticmethod
    def _review(db, channel, video):
        row = db.execute("SELECT * FROM discovery_reviews WHERE channel=? AND video=?", (channel, video)).fetchone()
        return dict(row) if row else {"state": "inbox", "rights": "pending", "notes": "", "rights_note": "", "revision": 0}

    def review(self, data):
        action, channel, video = (data.get(key) for key in ("action", "channel", "video"))
        ident, elapsed = data.get("request_id"), data.get("seconds", 0)
        if (not isinstance(action, str) or action not in ACTIONS or not isinstance(ident, str)
                or not re.fullmatch(r"[a-f0-9-]{16,64}", ident) or type(elapsed) is not int or not 0 <= elapsed <= 3600):
            raise ValueError("La acción editorial o el tiempo registrado no son válidos.")
        keys = ("notes", "rights", "rights_note", "revision")
        payload = studio_json.dumps({key: data[key] for key in keys if key in data} | {"seconds": elapsed}, sort_keys=True)
        with self.monitor.connect(write=True) as db:
            self._video(db, channel, video)
            current = self._review(db, channel, video)
            prior = db.execute("SELECT * FROM discovery_actions WHERE request_id=?", (ident,)).fetchone()
            if prior:
                if (prior["channel"], prior["video"], prior["action"], prior["payload"]) != (channel, video, action, payload):
                    raise Conflict("El identificador de la acción ya fue utilizado.")
                return current
            if type(data.get("revision")) is not int or data["revision"] != current["revision"]:
                raise Conflict("La oportunidad cambió. Recarga antes de guardar.")
            notes, rights_note = (data.get(key, current[key]) for key in ("notes", "rights_note"))
            rights = data.get("rights", current["rights"])
            if (not isinstance(rights, str) or rights not in RIGHTS or not isinstance(notes, str)
                    or not isinstance(rights_note, str) or len(notes) > 4000 or len(rights_note) > 1000
                    or rights == "cleared" and not rights_note.strip()):
                raise ValueError("Revisa las notas y aporta una referencia si confirmas los derechos.")
            updated = {"state": ACTIONS[action] or current["state"], "notes": notes.strip(), "rights": rights,
                       "rights_note": rights_note.strip(), "revision": current["revision"] + 1}
            now = self.monitor.clock()
            db.execute("""INSERT INTO discovery_reviews VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(channel,video) DO UPDATE SET
                state=excluded.state,notes=excluded.notes,rights=excluded.rights,rights_note=excluded.rights_note,
                revision=excluded.revision,updated_at=excluded.updated_at""", (channel, video, *updated.values(), now))
            db.execute("INSERT INTO discovery_actions VALUES (?,?,?,?,?,?,?)", (ident, channel, video, action, payload, now, elapsed))
            return self._review(db, channel, video)

    def metrics(self):
        with self.monitor.connect(write=True) as db:
            self._index(db, self._rules(db))
            counts = {row[0]: row[1] for row in db.execute("SELECT state,count(*) FROM discovery_reviews GROUP BY state")}
            linked = {row[0] for row in db.execute("SELECT project FROM videos WHERE project IS NOT NULL")}
            return {
                "detected": db.execute("SELECT count(*) FROM videos").fetchone()[0],
                "evaluated": db.execute("SELECT count(*) FROM discovery_evaluations").fetchone()[0],
                "saved": counts.get("saved", 0) + counts.get("approved", 0),
                "discarded": counts.get("discarded", 0),
                "approved": counts.get("approved", 0),
                "produced": sum(self._project_exists(link) for link in linked),
                "review_seconds": db.execute("SELECT coalesce(sum(seconds),0) FROM discovery_actions").fetchone()[0],
            }

    def _project_exists(self, ident):
        return bool(isinstance(ident, str) and re.fullmatch(r"[a-f0-9]{12}", ident)
                    and (self.root / ident / "project.json").is_file())

    def _existing_youtube_project(self, video):
        for manifest in self.root.glob("*/project.json"):
            if (not re.fullmatch(r"[a-f0-9]{12}", manifest.parent.name)
                    or manifest.parent.is_symlink() or manifest.is_symlink()):
                continue
            try:
                url = urlsplit(projects.read_project(manifest.parent).get("source", ""))
                if url.scheme in {"https", "http"} and youtube_monitor.video_id(url) == video:
                    return True
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return False

    def prepare_submission(self, channel, video):
        """Future phase-3 contract. Validate and return a plan without dispatching."""
        with self.monitor.connect() as db:
            item = self._video(db, channel, video)
            review = self._review(db, channel, video)
            if review["state"] != "approved" or review["rights"] != "cleared" or not review["rights_note"]:
                raise ValueError("Hace falta aprobación editorial y una referencia de derechos.")
            ident = youtube_monitor.project_id(channel, video)
            if item["project"] or self._project_exists(ident):
                raise Conflict("El video ya tiene un vínculo con producción.")
        if self._existing_youtube_project(video):
            raise Conflict("El video ya tiene un vínculo con producción.")
        return {"channel": channel, "video": video, "project_id": ident, "review_revision": review["revision"],
                "url": "https://www.youtube.com/watch?v=" + video, "rights_basis": review["rights_note"]}
