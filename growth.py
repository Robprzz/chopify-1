"""Local analytics with explicit CSV provenance."""
import csv
from datetime import datetime, timezone
import io
import re
import statistics

from publishing_platforms import PLATFORMS

FIELDS = ("post_id", "published_at", "observed_at", "views", "likes", "comments", "shares")


class Growth:
    def __init__(self, calendar):
        self.calendar = calendar
        with calendar.connect(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS growth_samples(
                platform TEXT NOT NULL, account TEXT NOT NULL, post_id TEXT NOT NULL,
                published_at TEXT NOT NULL, observed_at TEXT NOT NULL,
                views INTEGER NOT NULL, likes INTEGER NOT NULL, comments INTEGER NOT NULL, shares INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'user_csv', imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(platform,account,post_id,observed_at))""")

    @staticmethod
    def timestamp(value):
        date = datetime.fromisoformat(value)
        if date.utcoffset() is None:
            raise ValueError("Incluye la zona horaria en cada fecha.")
        return date.astimezone(timezone.utc).isoformat()

    def parse(self, payload):
        platform, account, content = (payload.get(key) for key in ("platform", "account", "csv"))
        if platform not in PLATFORMS or not isinstance(account, str) or not 0 < len(account.strip()) <= 120:
            raise ValueError("Selecciona una plataforma y escribe el identificador de cuenta.")
        if not isinstance(content, str) or len(content) > 250_000:
            raise ValueError("El CSV debe ocupar como máximo 250 000 caracteres.")
        rows = []
        reader = csv.DictReader(io.StringIO(content.removeprefix("\ufeff")), strict=True)
        try:
            if set(reader.fieldnames or ()) != set(FIELDS) or len(reader.fieldnames) != len(FIELDS):
                raise ValueError("El CSV necesita estas columnas: " + ", ".join(FIELDS))
            for index, row in enumerate(reader, 2):
                try:
                    if index > 1001 or None in row or None in row.values():
                        raise ValueError
                    ident = row["post_id"].strip()
                    if not ident or len(ident) > 150:
                        raise ValueError
                    published, observed = map(self.timestamp, (row["published_at"], row["observed_at"]))
                    counts = [row[key] for key in FIELDS[3:]]
                    if observed < published or any(not re.fullmatch(r"[0-9]{1,12}", count) for count in counts):
                        raise ValueError
                    rows.append((platform, account.strip(), ident, published, observed, *map(int, counts)))
                except (TypeError, ValueError):
                    raise ValueError(f"Fila {index} inválida. Revisa identificador, fechas y contadores.") from None
        except csv.Error:
            raise ValueError("El CSV contiene una estructura inválida.") from None
        if not rows:
            raise ValueError("Añade al menos una fila al CSV.")
        return rows

    def ingest(self, payload):
        rows = self.parse(payload)
        if payload.get("confirm") is not True:
            return {"rows": len(rows), "preview": [dict(zip(FIELDS, row[2:])) for row in rows[:10]]}
        with self.calendar.connect(write=True) as db:
            before = db.total_changes
            db.executemany("""INSERT OR IGNORE INTO growth_samples
                (platform,account,post_id,published_at,observed_at,views,likes,comments,shares)
                VALUES(?,?,?,?,?,?,?,?,?)""", rows)
            return {"imported": db.total_changes - before, "rows": len(rows)}

    def snapshot(self):
        with self.calendar.connect() as db:
            rows = [dict(row) for row in db.execute("""SELECT * FROM growth_samples g WHERE observed_at=
                (SELECT MAX(observed_at) FROM growth_samples WHERE platform=g.platform AND account=g.account
                AND post_id=g.post_id) ORDER BY published_at DESC""")]
        groups = {}
        trend = {}
        for row in rows:
            key = (row["platform"], row["account"])
            totals = groups.setdefault(key, dict(platform=key[0], account=key[1], posts=0, **dict.fromkeys(FIELDS[3:], 0)))
            totals["posts"] += 1
            for field in FIELDS[3:]:
                totals[field] += row[field]
            day = row["published_at"][:10]
            trend[day] = trend.get(day, 0) + 1
        return {"groups": list(groups.values()), "frequency": sorted(trend.items()),
                "top": sorted(rows, key=lambda row: -row["views"])[:10], "source": "user_csv"}

    @staticmethod
    def historical_hours(db, platform, account, zone):
        if not account:
            return {}
        buckets = {}
        for row in db.execute("""SELECT published_at,likes+comments+shares AS interactions FROM growth_samples g
            WHERE platform=? AND account=? AND observed_at=(SELECT MAX(observed_at) FROM growth_samples WHERE
            platform=g.platform AND account=g.account AND post_id=g.post_id)""", (platform, account)):
            hour = datetime.fromisoformat(row["published_at"]).astimezone(zone).hour
            buckets.setdefault(hour, []).append(row["interactions"])
        qualified = {hour: (statistics.median(values), len(values)) for hour, values in buckets.items() if len(values) >= 5}
        return qualified if len(qualified) >= 2 else {}
