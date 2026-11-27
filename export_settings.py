"""Workspace-wide visual template and effective options for new exports."""
from pathlib import Path

import branding
import render_clips
import signature

VERSION = 1
PROFILES = {"fast": ("ultrafast", "20"), "quality": ("veryfast", "20")}
VISUAL = ("aspect", "fit", "style", "layout", "camera_mode", "centers", "caption",
          "brand", "facecam_box", "facecam_height", "signature", "export_profile", "use_watermark")
DEFAULTS = {"version": VERSION, "enabled": False, "aspect": "9:16", "signature": False,
            "watermark": True, "style": "default", "fit": "cover", "layout": "single",
            "camera_mode": "auto", "brand_name": "", "export_profile": "quality"}


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / ".export-template.json"

    def _check(self):
        if self.path.is_symlink():
            raise ValueError("La configuración de plantilla está fuera del espacio de trabajo.")

    def read(self):
        from projects import read_json

        self._check()
        try:
            return self.validate(read_json(self.path), assets=False)
        except FileNotFoundError:
            return DEFAULTS.copy()

    def validate(self, data, *, assets=True):
        import re

        if not isinstance(data, dict) or data.keys() - DEFAULTS.keys():
            raise ValueError("Configuración de plantilla no válida.")
        value = {**DEFAULTS, **data}
        if value["version"] != VERSION or any(type(value[k]) is not bool for k in ("enabled", "signature", "watermark")):
            raise ValueError("Versión o controles de plantilla no válidos.")
        if (value["aspect"] not in render_clips.ASPECTS or value["style"] not in render_clips.CAPTION_STYLES
                or value["fit"] not in {"cover", "contain"} or value["layout"] not in {"single", "split", "stacked"}
                or value["camera_mode"] not in render_clips.smart_camera.MODES or value["export_profile"] not in PROFILES):
            raise ValueError("Formato, composición, subtítulos o calidad de plantilla no válidos.")
        brand = value["brand_name"]
        if not isinstance(brand, str) or (brand and not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", brand)):
            raise ValueError("Selecciona una marca guardada.")
        if brand and assets:
            path = self.root / "brands" / (brand + ".json")
            if path.resolve() != path:
                raise ValueError("La ruta de la marca guardada no es válida.")
            branding.load_brand(path)
        if assets and value["signature"] and value["aspect"] == "9:16":
            signature.load(signature.TEMPLATE_ID)
        return value

    def save(self, data):
        import json
        from watermark import _write

        value = self.validate(data)
        self._check()
        _write(self.path, json.dumps(value, ensure_ascii=False).encode("utf-8"))
        return value

    def resolve(self, options, *, aspect=None, profile=None):
        from projects import validate_options

        settings = self.read()
        use_global = settings["enabled"] and not options.get("template_override")
        effective = dict(options)
        if use_global:
            effective.update({key: settings[key] for key in (
                "aspect", "style", "fit", "layout", "camera_mode", "export_profile")})
            brand_path = self.root / "brands" / (settings["brand_name"] + ".json") if settings["brand_name"] else None
            if brand_path and brand_path.resolve() != brand_path:
                raise ValueError("La marca global debe apuntar a una marca local del espacio de trabajo.")
            effective.update(caption=None, use_watermark=settings["watermark"],
                             brand=str(brand_path) if brand_path else None,
                             signature={"template": signature.TEMPLATE_ID, "watermark": settings["watermark"]}
                             if settings["signature"] else None)
        if aspect is not None:
            effective["aspect"] = aspect
        if profile is not None:
            effective["export_profile"] = profile
        warning = None
        if effective.get("signature") and effective["aspect"] != "9:16":
            if use_global or aspect is not None:
                effective["signature"] = None
                warning = "Signature requiere 9:16. Este formato se exportará sin sus franjas."
            else:
                # Legacy project-level Signature explicitly selected its vertical template.
                effective["aspect"] = "9:16"
        if effective["aspect"] != "9:16" and effective["layout"] != "single":
            effective["layout"] = "single"
            warning = (warning + " " if warning else "") + "La composición se ajustó a un solo encuadre."
        return validate_options(effective), use_global, warning

    def summary(self, options, **overrides):
        effective, inherited, warning = self.resolve(options, **overrides)
        from watermark import Store as WatermarkStore, WatermarkError

        frame = effective["signature"]
        mark = effective["use_watermark"] and (not frame or frame["watermark"])
        try:
            marked = bool(mark and WatermarkStore(self.root).read()["enabled"])
        except WatermarkError as exc:
            marked = False
            warning = (warning + " " if warning else "") + str(exc)
        return {"inherited": inherited, "warning": warning,
                "brand_name": Path(effective["brand"]).stem if effective["brand"] else None,
                "watermark_enabled": marked,
                "camera_mode": effective["camera_mode"], "fit": effective["fit"],
                **{key: effective[key] for key in ("aspect", "style", "signature", "layout", "export_profile", "use_watermark")}}
