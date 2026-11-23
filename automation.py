"""Validated, workspace-local settings for Chopify Automation."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_SETTINGS = {
    "live_clipping": True,
    "ai_captions": True,
    "subtitle_correction": True,
    "force_vertical_format": False,
    "duplicate_prevention": True,
    "minimum_clip_score": 6.0,
    "daily_publication_limit": 3,
    "ai_engine": "local",
    "ai_provider": "ollama",
    "require_publication_consent": True,
    "timezone": "America/Santo_Domingo",
}

LOCKED_SETTINGS = {
    "duplicate_prevention": True,
    "ai_engine": "local",
    "ai_provider": "ollama",
    "require_publication_consent": True,
}


class SettingsConflict(Exception):
    """A different revision was committed while settings were being edited."""


class SettingsUnavailable(Exception):
    """The local configuration could not be read or saved safely."""


def validate_settings(settings):
    if not isinstance(settings, dict) or settings.keys() != DEFAULT_SETTINGS.keys():
        raise ValueError("Envía los ajustes de Automation completos con los campos admitidos.")
    for key, default in DEFAULT_SETTINGS.items():
        if isinstance(default, bool) and type(settings[key]) is not bool:
            raise ValueError(f"El ajuste {key} debe ser verdadero o falso.")
    for key, required in LOCKED_SETTINGS.items():
        if settings[key] != required:
            raise ValueError("Automation requiere IA local con Ollama, prevención de duplicados y aprobación de cada envío.")
    score = settings["minimum_clip_score"]
    if type(score) not in (int, float) or not 6 <= score <= 10:
        raise ValueError("La puntuación mínima debe estar entre 6 y 10.")
    limit = settings["daily_publication_limit"]
    if type(limit) is not int or not 1 <= limit <= 3:
        raise ValueError("El límite diario debe ser un número entero entre 1 y 3.")
    timezone = settings["timezone"]
    if not isinstance(timezone, str) or not 1 <= len(timezone) <= 128:
        raise ValueError("Introduce una zona horaria IANA, como America/Santo_Domingo.")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        try:
            ZoneInfo("UTC")
        except ZoneInfoNotFoundError:
            raise ValueError("Instala la dependencia tzdata con el Python de Studio para validar la zona horaria.") from None
        raise ValueError("Introduce una zona horaria IANA, como America/Santo_Domingo.") from None
    except ValueError:
        raise ValueError("Introduce una zona horaria IANA, como America/Santo_Domingo.") from None
    return {**settings, "minimum_clip_score": float(score)}


class SettingsStore:
    def __init__(self, root):
        self.path = Path(root).resolve() / ".chopify-automation.sqlite3"

    @staticmethod
    def _snapshot(row=None):
        if row is None:
            return {"revision": 0, "settings": DEFAULT_SETTINGS.copy()}
        revision, payload = row
        try:
            return {"revision": revision, "settings": validate_settings(json.loads(payload))}
        except (ValueError, TypeError):
            raise SettingsUnavailable("La configuración de Automation guardada no es válida. Revisa la base local.") from None

    def read(self):
        try:
            if not self.path.exists():
                return self._snapshot()
            # Opening an existing file also lets SQLite recover a hot journal after a crash.
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=2)) as db:
                table = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'automation_settings'"
                ).fetchone()
                if not table:
                    return self._snapshot()
                row = db.execute("SELECT revision, payload FROM automation_settings WHERE id = 1").fetchone()
                return self._snapshot(row)
        except (OSError, sqlite3.Error):
            raise SettingsUnavailable("No se pudo leer la configuración de Automation. Comprueba el acceso a la carpeta de proyectos.") from None

    def save(self, settings, revision):
        if type(revision) is not int or revision < 0:
            raise ValueError("La revisión de la configuración debe ser un número entero no negativo.")
        settings = validate_settings(settings)
        try:
            with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("""
                    CREATE TABLE IF NOT EXISTS automation_settings (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        revision INTEGER NOT NULL CHECK (revision > 0),
                        payload TEXT NOT NULL
                    )
                """)
                row = db.execute("SELECT revision, payload FROM automation_settings WHERE id = 1").fetchone()
                current = self._snapshot(row)
                if row is not None and settings == current["settings"]:
                    return current  # An identical retry needs no new revision.
                if revision != current["revision"]:
                    raise SettingsConflict("Los ajustes cambiaron en otra pestaña. Recarga la configuración antes de guardar.")
                revision += 1
                db.execute("""
                    INSERT INTO automation_settings (id, revision, payload) VALUES (1, ?, ?)
                    ON CONFLICT (id) DO UPDATE SET revision = excluded.revision, payload = excluded.payload
                """, (revision, json.dumps(settings, allow_nan=False)))
                return {"revision": revision, "settings": settings}
        except (OSError, sqlite3.Error):
            raise SettingsUnavailable("No se pudo guardar la configuración de Automation. Comprueba que la carpeta de proyectos sea escribible.") from None
