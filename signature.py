"""Versioned, bundled artwork and measured geometry for vertical clips."""
from dataclasses import dataclass
from pathlib import Path

from watermark import _dimensions


TEMPLATE_ID = "anqora-signature-1"
ASSETS = Path(__file__).resolve().parent / "assets" / "signature"


def validate(settings):
    if settings is None:
        return None
    if (not isinstance(settings, dict) or settings.keys() != {"template", "watermark"}
            or settings["template"] != TEMPLATE_ID or type(settings["watermark"]) is not bool):
        raise ValueError("La configuración de Signature no es válida.")
    return settings.copy()


@dataclass(frozen=True)
class Frame:
    header: Path
    footer: Path
    header_size: tuple[int, int]
    footer_size: tuple[int, int]

    def bands(self, width, height):
        def scaled(size):
            return 2 * round(width * size[1] / (2 * size[0]))

        top, bottom = scaled(self.header_size), scaled(self.footer_size)
        if min(top, bottom) < 2 or top + bottom >= height // 2:
            raise ValueError("Las franjas de Signature no dejan suficiente espacio para el video.")
        return top, bottom


def load(template):
    if template is None:
        return None
    if template != TEMPLATE_ID:
        raise ValueError("Selecciona una plantilla Signature válida.")
    header = ASSETS / f"{TEMPLATE_ID}-header.png"
    footer = ASSETS / f"{TEMPLATE_ID}-footer.png"
    try:
        # Only the IHDR is needed here. FFmpeg decodes the bundled PNGs for render.
        with header.open("rb") as file:
            header_size = _dimensions(file.read(33))
        with footer.open("rb") as file:
            footer_size = _dimensions(file.read(33))
        frame = Frame(header, footer, header_size, footer_size)
        frame.bands(1080, 1920)
    except (OSError, ValueError):
        raise ValueError("Faltan las franjas PNG oficiales de Signature o su geometría no es válida.") from None
    return frame
