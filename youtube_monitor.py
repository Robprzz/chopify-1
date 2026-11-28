"""Local, opt-in monitoring of public YouTube Atom feeds."""
from contextlib import closing, contextmanager
from datetime import datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
import http.client
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit
from uuid import uuid4
import xml.etree.ElementTree as ET

CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
VIDEO = re.compile(r"[A-Za-z0-9_-]{11}\Z")
HANDLE = re.compile(r"@[^\s/@?#\\\x00-\x20]{2,80}\Z")
HOSTS = {"www.youtube.com", "youtube.com", "m.youtube.com"}
ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
MEDIA = "{http://search.yahoo.com/mrss/}"
MODES = {"discover", "produce"}
MAX_CHANNELS = 32
MAX_PENDING = 240


def project_id(channel, video):
    return sha256(f"YouTube channel {channel} video {video}".encode()).hexdigest()[:12]


def video_id(url):
    host = url.hostname or ""
    parts = url.path.strip("/").split("/")
    if host == "youtu.be":
        video = parts[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        if parts == ["watch"]:
            video = next(iter(parse_qs(url.query).get("v", [])), "")
        else:
            video = parts[1] if len(parts) == 2 and parts[0] in {"live", "shorts", "embed"} else ""
    else:
        return None
    return video if VIDEO.fullmatch(video) else None


class FeedError(ValueError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


def locator(value):
    if not isinstance(value, str):
        raise ValueError("Indica una URL de canal, @handle o identificador UC.")
    value = value.strip()
    if CHANNEL.fullmatch(value) or HANDLE.fullmatch(value):
        return value
    url = urlsplit(value)
    if (url.scheme != "https" or url.netloc.lower() not in HOSTS or "\\" in value
            or not url.path):
        raise ValueError("Usa una URL HTTPS de YouTube con /channel/UC… o /@handle.")
    path = [unquote(part) for part in url.path.strip("/").split("/")]
    if len(path) in {1, 2} and HANDLE.fullmatch(path[0]) and (len(path) == 1 or path[1] in {"featured", "videos", "streams"}):
        return path[0]
    if len(path) in {2, 3} and path[0] == "channel" and CHANNEL.fullmatch(path[1]):
        if len(path) == 2 or path[2] in {"featured", "videos", "streams"}:
            return path[1]
    raise ValueError("El enlace no identifica un canal de YouTube compatible.")


def fetch(path):
    # All derived inputs are path-only requests to this fixed HTTPS origin.
    deadline = time.monotonic() + 10
    try:
        with closing(http.client.HTTPSConnection("www.youtube.com", timeout=6)) as conn:
            conn.request("GET", path, headers={"User-Agent": "AnqoraStudio/1.0", "Accept": "*/*"})
            with closing(conn.getresponse()) as response:
                if response.status == 429:
                    retry = response.getheader("Retry-After", "")
                    try:
                        wait = int(retry) if re.fullmatch(r"\d{1,6}", retry) else int(
                            parsedate_to_datetime(retry).timestamp() - time.time())
                    except (ValueError, OverflowError, TypeError):
                        wait = 0
                    raise FeedError("YouTube limitó temporalmente las consultas (429).", max(0, wait))
                if response.status != 200:
                    raise FeedError(f"YouTube devolvió HTTP {response.status}.")
                data = bytearray()
                while len(data) <= 2_000_000 and time.monotonic() < deadline:
                    block = response.read1(65536)
                    if not block:
                        return bytes(data)
                    data.extend(block)
    except (OSError, http.client.HTTPException) as error:
        raise FeedError("No se pudo consultar YouTube. Comprueba la conexión.") from error
    raise FeedError("La página o el feed superó el tamaño o tiempo de lectura permitido.")


class ChannelPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.channel = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        url = attrs.get("href") if tag == "link" and attrs.get("rel") == "canonical" else (
            attrs.get("content") if tag == "meta" and attrs.get("property") == "og:url" else None)
        if tag == "meta" and attrs.get("itemprop") == "channelId" and CHANNEL.fullmatch(attrs.get("content") or ""):
            self.channel = attrs["content"]
        if url:
            try:
                value = locator(url)
            except ValueError:
                return
            if CHANNEL.fullmatch(value):
                self.channel = value


class Atom:
    def __init__(self, request=fetch):
        self.request = request

    def resolve(self, value):
        ident = locator(value)
        if CHANNEL.fullmatch(ident):
            return ident
        html = self.request("/" + quote(ident, safe="@")).decode("utf-8", "replace")
        page = ChannelPage()
        page.feed(html)
        if page.channel:
            return page.channel
        match = re.search(r'"channelMetadataRenderer"\s*:\s*(\{)', html)
        if match:
            try:
                data, _ = json.JSONDecoder().raw_decode(html[match.start(1):])
                channel = data.get("externalId", "")
                if CHANNEL.fullmatch(channel):
                    return channel
            except (ValueError, TypeError):
                pass
        raise FeedError("YouTube no mostró el identificador estable del canal. Usa su URL /channel/UC….")

    def latest(self, channel):
        if not CHANNEL.fullmatch(channel):
            raise ValueError("Identificador de canal inválido.")
        raw = self.request("/feeds/videos.xml?channel_id=" + channel)
        if re.search(br"<!\s*(?:DOCTYPE|ENTITY)", raw, re.I):
            raise FeedError("El feed público no tiene un formato Atom válido.")
        try:
            root = ET.fromstring(raw)
            if root.tag != ATOM + "feed" or root.findtext(YT + "channelId") != channel:
                raise ValueError
            items = []
            for entry in root.findall(ATOM + "entry")[:40]:
                video = entry.findtext(YT + "videoId") or ""
                if not VIDEO.fullmatch(video) or entry.findtext(YT + "channelId") != channel:
                    continue
                try:
                    date = datetime.fromisoformat(entry.findtext(ATOM + "published") or "")
                except ValueError:
                    continue
                if date.tzinfo is None:
                    continue
                items.append({"id": video, "title": (entry.findtext(ATOM + "title") or video)[:240],
                              "published": date.timestamp(), "description": entry.findtext(
                                  MEDIA + "group/" + MEDIA + "description", default="")[:1800]})
            return (root.findtext(ATOM + "title") or channel)[:160], items
        except (ET.ParseError, ValueError, OverflowError):
            raise FeedError("El feed público no tiene un formato Atom válido.") from None


def options(data):
    def integer(key, default, low, high):
        value = data.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"Revisa el intervalo de valores para {key}.")
        return value

    active, authorized, render = (data.get(key, default) for key, default in
                                   (("active", True), ("authorized", False), ("render", False)))
    if any(type(value) is not bool for value in (active, authorized, render)):
        raise ValueError("Los controles del canal deben ser verdaderos o falsos.")
    reason = data.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 500 or (authorized and not reason.strip()):
        raise ValueError("Describe en hasta 500 caracteres tu autorización de uso.")
    template = data.get("template", "inherit")
    if template not in {"inherit", "project", "signature"}:
        raise ValueError("Selecciona una plantilla de canal disponible.")
    category = data.get("category", "auto")
    from video_types import VIDEO_TYPES
    if not isinstance(category, str) or category not in {"auto", *VIDEO_TYPES}:
        raise ValueError("Selecciona una categoría disponible.")
    mode = data.get("mode", "produce")
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError("Modalidad de monitoreo inválida.")
    return {"mode": mode, "active": active, "authorized": authorized, "render": render, "reason": reason.strip(),
            "interval": integer("interval", 15, 5, 1440), "maximum": integer("maximum", 3, 1, 5),
            "preferred": integer("preferred", 60, 15, 300), "template": template, "category": category}


class Monitor:
    def __init__(self, root, dispatch, idle, feed=None, clock=time.time, start=True):
        self.path = Path(root) / ".youtube-monitor.sqlite"
        self.dispatch, self.idle = dispatch, idle
        self.feed, self.clock = feed or Atom(), clock
        self.stop, self.wake = threading.Event(), threading.Event()
        self.network = threading.Lock()
        self.owner = uuid4().hex
        with self.connect(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS channels (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL,
                authorized INTEGER NOT NULL, reason TEXT NOT NULL, interval INTEGER NOT NULL,
                maximum INTEGER NOT NULL, preferred INTEGER NOT NULL, template TEXT NOT NULL,
                category TEXT NOT NULL, render INTEGER NOT NULL, baseline REAL, checked REAL,
                next_check REAL NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
                error TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS videos (
                channel TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL, published REAL NOT NULL,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, project TEXT,
                next_try REAL NOT NULL DEFAULT 0, error TEXT,
                PRIMARY KEY (channel, id), FOREIGN KEY(channel) REFERENCES channels(id))""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(videos)")}
            if "lease_owner" not in columns:
                db.execute("ALTER TABLE videos ADD COLUMN lease_owner TEXT")
            if "lease_until" not in columns:
                db.execute("ALTER TABLE videos ADD COLUMN lease_until REAL NOT NULL DEFAULT 0")
            if "mode" not in {row["name"] for row in db.execute("PRAGMA table_info(channels)")}:
                db.execute("ALTER TABLE channels ADD COLUMN mode TEXT NOT NULL DEFAULT 'produce' "
                           "CHECK(mode IN ('discover','produce'))")
            for key, definition in (("description", "TEXT NOT NULL DEFAULT ''"),
                                    ("detected_at", "REAL"),
                                    ("metadata_revision", "INTEGER NOT NULL DEFAULT 0")):
                if key not in columns:
                    db.execute(f"ALTER TABLE videos ADD COLUMN {key} {definition}")
            if db.execute("PRAGMA user_version").fetchone()[0] < 3:
                db.execute("PRAGMA user_version=3")
            db.execute("CREATE INDEX IF NOT EXISTS monitor_published ON videos(published DESC, channel, id)")
            db.execute("CREATE INDEX IF NOT EXISTS monitor_work ON videos(state, next_try)")
        self.worker = threading.Thread(target=self._loop, daemon=True, name="studio-youtube-monitor")
        if start:
            self.worker.start()

    @contextmanager
    def connect(self, write=False):
        if self.path.is_symlink():
            raise ValueError("La base del monitor debe estar dentro del espacio de trabajo.")
        with closing(sqlite3.connect(self.path, timeout=4)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def _channel(db, ident):
        row = db.execute("SELECT * FROM channels WHERE id=?", (ident,)).fetchone()
        if not row:
            raise ValueError("Canal no registrado.")
        return row

    def snapshot(self):
        with self.connect() as db:
            rows = []
            for channel in db.execute("SELECT * FROM channels ORDER BY name COLLATE NOCASE"):
                items = [dict(row) for row in db.execute(
                    "SELECT * FROM videos WHERE channel=? ORDER BY published DESC,id LIMIT 8", (channel["id"],))]
                count = db.execute("SELECT count(*) FROM videos WHERE channel=? AND project IS NOT NULL",
                                   (channel["id"],)).fetchone()[0]
                rows.append({**dict(channel), "videos": items, "projects": count,
                             "url": "https://www.youtube.com/channel/" + channel["id"]})
        return {"channels": rows}

    def action(self, action, data):
        ident = data.get("id")
        if action == "add":
            with self.network:
                ident = self.feed.resolve(data.get("source"))
            if not CHANNEL.fullmatch(ident):
                raise ValueError("YouTube no devolvió un identificador de canal válido.")
            config = options(data)
            with self.connect(write=True) as db:
                if db.execute("SELECT 1 FROM channels WHERE id=?", (ident,)).fetchone():
                    raise ValueError("Este canal ya está monitoreado.")
                if db.execute("SELECT count(*) FROM channels").fetchone()[0] >= MAX_CHANNELS:
                    raise ValueError("El monitor admite hasta 32 canales.")
                columns = ",".join(config.keys())
                db.execute(f"INSERT INTO channels (id,name,{columns}) VALUES (?,?,{','.join('?' for _ in config)})",
                           (ident, ident, *config.values()))
        else:
            with self.connect(write=True) as db:
                old = self._channel(db, ident)
                if action == "settings":
                    config = options({key: data.get(key, bool(old[key]) if key in {"active", "authorized", "render"} else old[key])
                                      for key in options({})})
                    db.execute("UPDATE channels SET " + ",".join(key + "=?" for key in config) + " WHERE id=?",
                               (*config.values(), ident))
                    if old["interval"] != config["interval"] and not old["errors"]:
                        db.execute("UPDATE channels SET next_check=min(next_check, ?) WHERE id=?",
                                   (self.clock() + config["interval"] * 60, ident))
                elif action == "check":
                    if old["mode"] == "discover" and not old["active"]:
                        raise ValueError("Activa la fuente antes de actualizarla.")
                    if old["errors"] and self.clock() < old["next_check"]:
                        raise ValueError("YouTube pidió esperar tras el último error. Consulta el próximo intento.")
                    if old["checked"] is not None and self.clock() - old["checked"] < 30:
                        raise ValueError("Espera 30 segundos desde la última consulta.")
                    db.execute("UPDATE channels SET next_check=0 WHERE id=?", (ident,))
                elif action == "import":
                    if old["mode"] == "discover":
                        raise ValueError("Este canal está en Solo descubrir.")
                    if not old["authorized"] or not old["active"]:
                        raise ValueError("Activa el canal y registra tu autorización antes de importar.")
                    item = db.execute("SELECT * FROM videos WHERE channel=? ORDER BY published DESC,id LIMIT 1", (ident,)).fetchone()
                    if not item:
                        raise ValueError("Todavía no se detectó un video público para importar.")
                    if item["state"] in {"reference", "unapproved", "failed", "deferred", "discovered"}:
                        db.execute("UPDATE videos SET state='pending', attempts=0, next_try=0,error=NULL WHERE channel=? AND id=?",
                                   (ident, item["id"]))
                else:
                    raise ValueError("Acción del monitor no válida.")
        self.wake.set()
        return self.snapshot()

    def refresh_active(self):
        now = self.clock()
        with self.connect(write=True) as db:
            changed = db.execute("""UPDATE channels SET next_check=0
                WHERE active=1 AND next_check!=0 AND (errors=0 OR next_check<=?)
                  AND (next_check<=? OR next_check>?) AND (checked IS NULL OR checked<=?)""",
                (now, now, now + 30, now - 30)).rowcount
        self.wake.set()
        return {"scheduled": changed}

    def poll(self):
        now = self.clock()
        with self.connect(write=True) as db:
            row = db.execute("""SELECT * FROM channels WHERE next_check<=?
                AND (active=1 OR (mode='produce' AND next_check=0)) ORDER BY next_check,id LIMIT 1""",
                (now,)).fetchone()
            if not row:
                return
            db.execute("UPDATE channels SET next_check=? WHERE id=?", (now + 30, row["id"]))
        try:
            with self.network:
                name, items = self.feed.latest(row["id"])
        except (FeedError, OSError) as error:
            attempt = row["errors"] + 1
            wait = max(min(14400, row["interval"] * 60 * 2 ** min(5, attempt)),
                       min(86400, getattr(error, "retry_after", 0)))
            with self.connect(write=True) as db:
                db.execute("UPDATE channels SET checked=?,next_check=?,errors=?,error=? WHERE id=?",
                           (now, now + wait, attempt, str(error), row["id"]))
            return
        with self.connect(write=True) as db:
            current = self._channel(db, row["id"])
            empty = current["baseline"] is None
            total = db.execute("""SELECT count(*) FROM videos v JOIN channels c ON c.id=v.channel
                                WHERE c.mode='produce' AND v.state IN ('pending','queued')""").fetchone()[0]
            for item in items:
                state = ("discovered" if current["mode"] == "discover" else
                         "reference" if empty or item["published"] <= current["baseline"] else
                         "unapproved" if not current["authorized"] else "pending" if total < MAX_PENDING else "deferred")
                description = item.get("description") or ""
                added = db.execute("""INSERT OR IGNORE INTO videos
                    (channel,id,title,published,state,description,detected_at) VALUES(?,?,?,?,?,?,?)""",
                    (row["id"], item["id"], item["title"], item["published"], state, description, now))
                if not added.rowcount:
                    db.execute("""UPDATE videos SET title=?,description=CASE WHEN ?='' THEN description ELSE ? END,metadata_revision=metadata_revision+1
                        WHERE channel=? AND id=? AND (title!=? OR (?!='' AND description!=?))""",
                        (item["title"], description, description, row["id"], item["id"], item["title"], description, description))
                total += added.rowcount if state == "pending" else 0
            db.execute("UPDATE channels SET name=?,baseline=coalesce(baseline,?),checked=?,next_check=?,errors=0,error=NULL WHERE id=?",
                       (name, now, now, now + current["interval"] * 60, row["id"]))

    def work(self):
        if not self.idle():
            return
        now = self.clock()
        with self.connect(write=True) as db:
            available = MAX_PENDING - db.execute("""SELECT count(*) FROM videos v JOIN channels c ON c.id=v.channel
                            WHERE c.mode='produce' AND v.state IN ('pending','queued')""").fetchone()[0]
            if available > 0:
                db.execute("""UPDATE videos SET state='pending' WHERE rowid IN (
                    SELECT v.rowid FROM videos v JOIN channels c ON c.id=v.channel
                    WHERE v.state='deferred' AND c.mode='produce' AND c.active=1 AND c.authorized=1
                    ORDER BY v.published,v.rowid LIMIT ?)""", (available,))
            item = db.execute("""SELECT v.*, c.authorized, c.reason, c.maximum, c.preferred, c.template,
                c.category, c.render, c.active FROM videos v JOIN channels c ON c.id=v.channel
                WHERE c.mode='produce' AND (v.state='queued' OR (v.state='pending' AND c.active=1 AND c.authorized=1)) AND v.next_try<=? AND v.lease_until<=?
                ORDER BY v.state='queued' DESC,v.published,v.id LIMIT 1""", (now, now)).fetchone()
            if not item:
                return
            db.execute("UPDATE videos SET lease_owner=?,lease_until=? WHERE channel=? AND id=?",
                       (self.owner, now + 60, item["channel"], item["id"]))
        project = project_id(item["channel"], item["id"])
        try:
            state, error = self.dispatch(dict(item), project)
        except (ValueError, OSError, sqlite3.Error):
            state, error = "retry", "No se pudo preparar el proyecto local."
        attempt = item["attempts"]
        next_try = now
        if state == "retry":
            attempt += 1
            state = "failed" if attempt >= 3 else "pending"
            next_try += (60, 300, 1800)[min(attempt - 1, 2)]
        elif state == "queued":
            next_try += 4
        elif state == "paused":
            state = "pending"
        with self.connect(write=True) as db:
            db.execute("""UPDATE videos SET state=?,error=?,attempts=?,project=?,next_try=?,lease_owner=NULL,lease_until=0
                       WHERE channel=? AND id=? AND lease_owner=?""",
                       (state, error, attempt, project if state in {"queued", "ready", "restricted"} or item["project"] else None,
                        next_try, item["channel"], item["id"], self.owner))

    def _loop(self):
        while not self.stop.is_set():
            try:
                self.poll()
                self.work()
            except (OSError, ValueError, sqlite3.Error):
                pass
            self.wake.wait(4)
            self.wake.clear()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.worker.is_alive():
            self.worker.join(12)
