"""Workspace-local PNG watermark settings and output-frame geometry."""
import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import tempfile

MAX_BYTES = 2 * 1024 * 1024
MAX_DIMENSION = 2048
POSITIONS = {"top-left", "top-right", "bottom-left", "bottom-right", "center"}
DEFAULTS = {"enabled": False, "image_id": None, "size": 15, "opacity": 100,
            "margin": 3, "position": "top-right"}


class WatermarkError(ValueError):
    pass


def _settings(data):
    if not isinstance(data, dict) or data.keys() != DEFAULTS.keys():
        raise WatermarkError("Los ajustes de marca de agua están incompletos.")
    if type(data["enabled"]) is not bool:
        raise WatermarkError("La activación de la marca de agua debe ser verdadera o falsa.")
    if not isinstance(data["position"], str) or data["position"] not in POSITIONS:
        raise WatermarkError("Elige una posición válida para la marca de agua.")
    for key, low, high in (("size", 5, 40), ("opacity", 1, 100), ("margin", 0, 20)):
        if type(data[key]) is not int or not low <= data[key] <= high:
            raise WatermarkError(f"El ajuste {key} debe ser un entero entre {low} y {high}.")
    image_id = data["image_id"]
    if image_id is not None and (not isinstance(image_id, str) or not re.fullmatch(r"[a-f0-9]{64}", image_id)):
        raise WatermarkError("El identificador de la imagen de marca de agua no es válido.")
    return data.copy()


def _dimensions(data):
    if len(data) < 33 or data[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
        raise WatermarkError("La marca de agua debe ser una imagen PNG válida.")
    width, height = struct.unpack_from(">II", data, 16)
    if not 1 <= width <= MAX_DIMENSION or not 1 <= height <= MAX_DIMENSION:
        raise WatermarkError("La imagen PNG debe medir como máximo 2048 × 2048.")
    return width, height


def _upload(encoded):
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_BYTES + 2) // 3):
        raise WatermarkError("La imagen PNG debe pesar como máximo 2 MiB.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise WatermarkError("La imagen PNG no se pudo leer.") from None
    if len(data) > MAX_BYTES:
        raise WatermarkError("La imagen PNG debe pesar como máximo 2 MiB.")
    width, height = _dimensions(data)
    import cv2
    import numpy as np

    try:
        pixels = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    except cv2.error:
        pixels = None
    if pixels is None or pixels.shape[:2] != (height, width):
        raise WatermarkError("La imagen PNG de marca de agua está dañada.")
    return data


def _write(path, data):
    # One temporary file per writer, closed before replace on Windows.
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        draft = Path(temp.name)
        try:
            temp.write(data)
            temp.close()
            draft.replace(path)
        finally:
            temp.close()
            draft.unlink(missing_ok=True)


@dataclass(frozen=True)
class Mark:
    image: Path
    dimensions: tuple[int, int]
    settings: dict

    def geometry(self, width, height):
        size = self.settings
        margin = round(min(width, height) * size["margin"] / 100)
        image_w, image_h = self.dimensions
        ratio = min(width * size["size"] / 100 / image_w, (height - 2 * margin) / image_h)
        w, h = max(1, round(image_w * ratio)), max(1, round(image_h * ratio))
        position = size["position"]
        if position == "center":
            return w, h, (width - w) // 2, (height - h) // 2
        x = margin if position.endswith("left") else width - w - margin
        y = margin if position.startswith("top") else height - h - margin
        return w, h, x, y


class Store:
    def __init__(self, root):
        self.directory = Path(root).resolve() / ".chopify-watermark"

    def _path(self, name):
        path = self.directory / name
        try:
            if not path.is_symlink() and path.resolve() == path:
                return path
        except (OSError, RuntimeError):
            pass
        raise WatermarkError("La ruta de marca de agua está fuera de la carpeta autorizada.")

    def read(self):
        path = self._path("settings.json")
        try:
            return _settings(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return DEFAULTS.copy()
        except (OSError, ValueError):
            raise WatermarkError("No se pudo leer la configuración de marca de agua.") from None

    def _image(self, image_id):
        path = self._path(f"{image_id}.png")
        try:
            with path.open("rb") as file:
                data = file.read(MAX_BYTES + 1)
        except OSError:
            raise WatermarkError("Falta la imagen PNG de marca de agua. Vuelve a cargarla o desactiva la marca.") from None
        if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != image_id:
            raise WatermarkError("La imagen PNG de marca de agua cambió o está dañada. Vuelve a cargarla.")
        return path, data

    def preview(self, image_id):
        if image_id != self.read()["image_id"]:
            raise FileNotFoundError
        return self._image(image_id)[1]

    def save(self, settings, encoded=None):
        settings = _settings(settings)
        if encoded is not None:
            data = _upload(encoded)
            settings["image_id"] = hashlib.sha256(data).hexdigest()
        elif settings["enabled"]:
            if not settings["image_id"]:
                raise WatermarkError("Carga una imagen PNG antes de activar la marca de agua.")
            self._image(settings["image_id"])
        config = self._path("settings.json")
        try:
            self.directory.mkdir(exist_ok=True)
            if encoded is not None:
                _write(self._path(f'{settings["image_id"]}.png'), data)
            _write(config, json.dumps(settings).encode("utf-8"))
        except OSError:
            raise WatermarkError("No se pudo guardar la marca de agua. Comprueba el espacio y los permisos de la carpeta de proyectos.") from None
        return settings

    def for_render(self):
        settings = self.read()
        if not settings["enabled"]:
            return None
        if not settings["image_id"]:
            raise WatermarkError("La marca de agua está activa. Carga una imagen PNG o desactívala.")
        path, data = self._image(settings["image_id"])
        return Mark(path, _dimensions(data), settings)
