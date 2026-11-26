"""Recoverable local trash and explicitly confirmed purges of owned media."""
import json
import os
from pathlib import Path
import re
import shutil
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
    return send_many(root, [(project, media, title)])[0]


def send_many(root, assets):
    items = entries(root)
    added = []
    for project, media, title in assets:
        row = _trash_row(root, project, media, title, items + added)
        added.append(row)
    _write(root, [*items, *added])
    return added


def _trash_row(root, project, media, title, items):
    if not isinstance(project, str) or not re.fullmatch(r"(?:live/)?[a-f0-9]{12}", project):
        raise ValueError("Identificador de proyecto no válido.")
    if hidden(root, project, media, records=items):
        raise ValueError("Este elemento ya está en la papelera.")
    row = {"id": uuid.uuid4().hex, "project": project, "file": media, "title": title,
           "removed": time.time()}
    return row


def restore(root, ident):
    items = entries(root)
    row = next((item for item in items if item["id"] == ident), None)
    if row is None:
        raise ValueError("El elemento ya no está en la papelera.")
    _write(root, [item for item in items if item["id"] != ident])
    return row


def _safe_path(root, path):
    import stat

    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError("La ruta está fuera del espacio local.") from None
    current = root
    for part in relative.parts:
        if part in (".", ".."):
            raise ValueError("Ruta de papelera no válida.")
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise ValueError("La papelera encontró un enlace o punto de reanálisis.")
        if not stat.S_ISDIR(info.st_mode) and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise ValueError("La papelera encontró un archivo compartido o especial.")
    return path


def _targets(root, row):
    root = Path(root).resolve()
    code = row["project"]
    if not re.fullmatch(r"(?:live/)?[a-f0-9]{12}", code):
        raise ValueError("Identificador de papelera inválido.")
    project = root / code
    media = row["file"]
    if media is None:
        targets = [_safe_path(root, project)]
        if not targets[0].is_dir():
            raise ValueError("La carpeta del proyecto no está disponible.")
    else:
        if not media.endswith(".mp4") or any(c in media for c in ("/", chr(92), ":", chr(0))) or Path(media).name != media:
            raise ValueError("Nombre de clip inválido.")
        folder = _safe_path(root, project / "clips")
        stem = Path(media).stem
        targets = [file for file in folder.iterdir() if file.name in {
            media, stem + ".png", stem + ".srt", stem + ".ass", stem + ".meta.json"}
            or re.fullmatch(re.escape(stem) + r"\.(?:cover|mark)-[a-f0-9]{64}\.png", file.name)]
        if any(item.is_dir() for item in targets):
            raise ValueError("Un activo del clip no es un archivo.")
    files = []
    for target in targets:
        _safe_path(root, target)
        if target.is_dir():
            for base, directories, names in os.walk(target):
                for name in directories:
                    _safe_path(root, Path(base) / name)
                for name in names:
                    files.append(_safe_path(root, Path(base) / name))
        else:
            files.append(target)
    # Other projects may use an exported file as their original recording.
    for manifest in root.glob("*/project.json"):
        if media is None and manifest.parent == project:
            continue
        for index in (manifest, manifest.with_name("transcript.json")):
            if not index.is_file():
                continue
            data = json.loads(index.read_text(encoding="utf-8-sig"))
            for key in ("source", "video"):
                original = data.get(key)
                if not isinstance(original, str) or "://" in original:
                    continue
                path = Path(original)
                candidates = [path.resolve()] if path.is_absolute() else [
                    (root / path).resolve(), (index.parent / path).resolve(), path.resolve()]
                if any(any(candidate == target or (target.is_dir() and candidate.is_relative_to(target))
                           for target in targets) for candidate in candidates):
                    raise ValueError("Otro proyecto utiliza uno de estos archivos como fuente.")
    return targets, files


def inspect_purge(root, selected):
    root = Path(root).resolve()
    targets = []
    summaries = []
    for row in selected:
        owned, files = _targets(root, row)
        targets.extend(owned)
        summaries.append({"id": row["id"], "title": row["title"], "project": row["project"],
                          "file": row["file"], "bytes": sum(path.stat().st_size for path in files),
                          "files": [str(path.relative_to(root)) for path in files]})
    return summaries, [path for path in dict.fromkeys(targets)
                       if not any(parent in targets for parent in path.parents)]


def confirmation_for(selected):
    if len(selected) == 1:
        return selected[0]["title"] if selected[0]["file"] is None else "ELIMINAR"
    projects = sorted(row["title"] for row in selected if row["file"] is None)
    return " / ".join(["ELIMINAR", *projects])


def _staged_move_is_owned(row, relative):
    project = Path(row["project"])
    if not re.fullmatch(r"(?:live/)?[a-f0-9]{12}", row["project"]):
        return False
    if row["file"] is None:
        return relative == project
    if relative.parent != project / "clips":
        return False
    name = row["file"]
    stem = Path(name).stem
    return (relative.name in {name, stem+".png", stem+".ass", stem+".srt", stem+".meta.json"}
            or re.fullmatch(re.escape(stem)+r"\.(?:cover|mark)-[a-f0-9]{64}\.png", relative.name))


def _recover_stage(root, stage, remaining):
    _safe_path(root, stage)
    journal = _safe_path(root, stage / "journal.json")
    data = json.loads(journal.read_text(encoding="utf-8"))
    rows, moves = data["rows"], list(map(Path, data["moves"]))
    if any(path.is_absolute() or ".." in path.parts or not any(_staged_move_is_owned(row, path) for row in rows)
           for path in moves):
        raise ValueError("El diario de eliminación contiene rutas inválidas.")
    numbered = [(root / target, stage / str(i)) for i, target in enumerate(moves)]
    pending = {row["id"] for row in rows} & remaining
    if pending:
        for original, staged in reversed(numbered):
            if staged.exists():
                _safe_path(root, original.parent)
                _safe_path(root, staged)
                if original.exists() or original.is_symlink():
                    raise ValueError("No se puede reemplazar un archivo durante la recuperación.")
                staged.replace(original)
        journal.unlink()
        stage.rmdir()
    else:
        for _, staged in numbered:
            if staged.exists():
                _safe_path(root, staged)
        names = {"journal.json", *(item.name for _, item in numbered)}
        if any(item.name not in names for item in stage.iterdir()):
            raise ValueError("La carpeta de recuperación contiene archivos desconocidos.")
        # shutil does not follow directory symlinks or Windows junctions.
        shutil.rmtree(stage)


def recover(root):
    root = Path(root).resolve()
    remaining = {row["id"] for row in entries(root)}
    for stage in root.glob(".anqora-purge-*"):
        try:
            _recover_stage(root, stage, remaining)
        except (OSError, ValueError, KeyError, TypeError):
            # Preserve unrecognised or locked content for local inspection.
            continue


def purge(root, ids, confirmation):

    root = Path(root).resolve()
    items = entries(root)
    selected = [row for row in items if row["id"] in ids]
    if not selected or len(selected) != len(set(ids)):
        raise ValueError("Uno de los elementos ya no está en la papelera.")
    if confirmation is not True and confirmation != confirmation_for(selected):
        raise ValueError("Confirma la eliminación permanente.")
    summaries, targets = inspect_purge(root, selected)
    stage = Path(tempfile.mkdtemp(prefix=".anqora-purge-", dir=root))
    journal = stage / "journal.json"
    try:
        with journal.open("x", encoding="utf-8") as handle:
            json.dump({"rows":selected, "moves":[path.relative_to(root).as_posix() for path in targets]},
                      handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        journal.unlink(missing_ok=True)
        stage.rmdir()
        raise
    moved = []
    try:
        for i, target in enumerate(targets):
            _safe_path(root, target)
            destination = stage / str(i)
            target.replace(destination)
            moved.append((target, destination))
        removed_projects = {row["project"] for row in selected if row["file"] is None}
        _write(root, [row for row in items if row["id"] not in ids and row["project"] not in removed_projects])
    except (OSError, ValueError):
        for target, destination in reversed(moved):
            destination.replace(target)
        journal.unlink()
        stage.rmdir()
        raise
    try:
        _recover_stage(root, stage, set())
    except (OSError, ValueError):
        return {"items": summaries, "released": False,
                "message": "Activos retirados. Hay archivos pendientes de limpieza local."}
    return {"items": summaries, "released": True}


def describe(root):
    items = []
    for row in entries(root):
        try:
            details, _ = inspect_purge(root, [row])
            items.append({**row, **details[0]})
        except (OSError, ValueError) as exc:
            items.append({**row, "blocked": str(exc)})
    return items
