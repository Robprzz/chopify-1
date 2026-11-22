"""UTF-8 process boundaries for Studio and the local media pipeline."""
import os
import sys


UTF8_ENV = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def utf8_environment(base=None):
    return {**(os.environ if base is None else base), **UTF8_ENV}


def configure_utf8():
    """Cover direct CLI starts and shortcuts created before the UTF-8 flag."""
    os.environ.update(UTF8_ENV)
    for stream, errors in ((sys.stdin, "strict"), (sys.stdout, "strict"), (sys.stderr, "backslashreplace")):
        if reconfigure := getattr(stream, "reconfigure", None):
            reconfigure(encoding="utf-8", errors=errors)
