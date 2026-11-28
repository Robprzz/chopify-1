"""Strict JSON for local Studio state and numeric analysis results."""
import json
from pathlib import Path
import tempfile


def _numpy_value(value):
    import numpy as np

    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def dumps(data, *, ensure_ascii=False, indent=None, sort_keys=False):
    return json.dumps(data, default=_numpy_value, allow_nan=False,
                      ensure_ascii=ensure_ascii, indent=indent, sort_keys=sort_keys)


def write(path, data, *, indent=2):
    path = Path(path)
    payload = dumps(data, indent=indent).encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(payload)
            handle.close()
            temporary.replace(path)
        finally:
            handle.close()
            temporary.unlink(missing_ok=True)
