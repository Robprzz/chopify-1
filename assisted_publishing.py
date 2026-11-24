"""Bounded local draft preparation. External dispatch remains in Calendar."""
import json
import sqlite3

from publishing_platforms import PLATFORMS


class Preparation:
    def __init__(self, calendar):
        self.calendar = calendar
        with calendar.connect(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS publishing_assist(
                id INTEGER PRIMARY KEY CHECK(id=1), mode TEXT NOT NULL, platforms TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS publishing_prepared(
                project TEXT NOT NULL, file TEXT NOT NULL, digest TEXT NOT NULL, platform TEXT NOT NULL,
                result TEXT NOT NULL, PRIMARY KEY(project,file,digest,platform))""")

    def settings(self, payload=None):
        with self.calendar.connect(write=payload is not None) as db:
            if payload is not None:
                mode, platforms = payload.get("mode"), payload.get("platforms")
                if (mode not in ("manual", "assisted", "authorized") or not isinstance(platforms, list)
                        or any(platform not in PLATFORMS for platform in platforms)):
                    raise ValueError("Elige un modo y plataformas válidos.")
                db.execute("INSERT OR REPLACE INTO publishing_assist VALUES(1,?,?)",
                           (mode,json.dumps(list(dict.fromkeys(platforms)))))
            row = db.execute("SELECT mode,platforms FROM publishing_assist WHERE id=1").fetchone()
            return {"mode":row[0], "platforms":json.loads(row[1])} if row else {"mode":"assisted","platforms":[]}

    def prepare_one(self, catalog, copies):
        from publishing_calendar import Conflict

        settings = self.settings()
        if settings["mode"] == "manual" or not settings["platforms"]:
            return
        with self.calendar.connect() as db:
            seen = {(row["project"],row["file"],row["digest"],row["platform"]) for row in
                    db.execute("SELECT project,file,digest,platform FROM publishing_prepared")}
        for item in catalog():
            project, file = item["project"], item["filename"]
            try:
                clip = self.calendar._clip(None, project, file)
            except (OSError, ValueError):
                continue
            digest = clip["hashes"][0]
            for platform in settings["platforms"]:
                identity = (project,file,digest,platform)
                if identity in seen:
                    continue
                try:
                    self.calendar._verify(clip, platform)
                    self.calendar.drafts({"clips":[project+"/"+file],"platforms":[platform]}, copies)
                    result = "prepared"
                except Conflict:
                    result = "already_planned"
                except (OSError, ValueError):
                    result = "needs_attention"
                except sqlite3.Error:
                    return
                with self.calendar.connect(write=True) as db:
                    db.execute("INSERT OR IGNORE INTO publishing_prepared VALUES(?,?,?,?,?)", (*identity,result))
                return

    def history(self):
        with self.calendar.connect() as db:
            return [dict(row) for row in db.execute("SELECT project,file,platform,result FROM publishing_prepared ORDER BY rowid DESC LIMIT 50")]
