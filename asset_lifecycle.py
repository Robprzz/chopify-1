"""Recoverable local trash references. Original media and history stay on disk."""
import json
from pathlib import Path
import re
import tempfile
import time
import uuid


def _index(root):
    folder = Path(root).resolve()
    path = folder / ".anqora-trash.json"
    if path.is_symlink():
        raise ValueError("La papelera debe estar en la carpeta de proyectos.")
    return path


def entries(root):
    try:
        return json.loads(_index(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def hidden(root, project, filename=None, *, records=None):
    if filename is not None and not filename.endswith(".mp4"):
        filename = filename.removesuffix(".meta.json") if filename.endswith(".meta.json") else Path(filename).stem
        filename += ".mp4"
    return any(row["project"] == project and row["file"] in (None, filename)
               for row in (entries(root) if records is None else records))


def _write(root, items):
    target = _index(root)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
        draft = Path(temporary.name)
        try:
            temporary.write(json.dumps(items, ensure_ascii=False).encode("utf-8"))
            temporary.close()
            draft.replace(target)
        finally:
            temporary.close()
            draft.unlink(missing_ok=True)


def send_to_trash(root, project, media, title):
    if not isinstance(project, str) or not re.fullmatch(r"(?:live/)?[a-f0-9]{12}", project):
        raise ValueError("Identificador de proyecto no válido.")
    if project.startswith("live/") and media is None:
        raise ValueError("Selecciona un clip de la sesión Live.")
    items = entries(root)
    if hidden(root, project, media, records=items):
        raise ValueError("Este elemento ya está en la papelera.")
    row = {"id": uuid.uuid4().hex, "project": project, "file": media, "title": title,
           "removed": time.time()}
    _write(root, [*items, row])
    return row


def restore(root, ident):
    items = entries(root)
    row = next((item for item in items if item["id"] == ident), None)
    if row is None:
        raise ValueError("El elemento ya no está en la papelera.")
    _write(root, [item for item in items if item["id"] != ident])
    return row
