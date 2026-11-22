"""One-click launcher for the existing Chopify virtual environment."""
import argparse
import hmac
import http.client
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import webbrowser

import studio_settings as system
import studio_windows


CHECKOUT = Path(__file__).resolve().parent
PORT = 8765


def probe(port, key):
    """Distinguish this installation, an occupied port, and a free port."""
    nonce = secrets.token_hex(24)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=.65)
    connected = False
    try:
        conn.connect()
        connected = True
        conn.request("GET", "/api/health?nonce=" + nonce)
        with conn.getresponse() as response:
            if response.status == 200:
                data = json.loads(response.read(513))
                if (isinstance(data, dict) and isinstance(data.get("proof"), str)
                        and hmac.compare_digest(data["proof"], system.instance_proof(key, nonce))):
                    return "ours"
    except ConnectionRefusedError:
        if not connected:
            return "free"
    except (ValueError, TypeError, OSError, http.client.HTTPException):
        pass
    finally:
        conn.close()
    return "occupied"


def check_python(path):
    if not path.is_file():
        raise ValueError("Falta el entorno .venv de Chopify. Consulta la guía de primera configuración.")
    try:
        code = (
            "import importlib.util, json, sys; "
            "print(json.dumps([sys.version_info >= (3, 11) and sys.prefix != sys.base_prefix, "
            f"[name for name in {list(system.PACKAGES)!r} if importlib.util.find_spec(name) is None]]))"
        )
        check = subprocess.run([str(path), "-c", code], capture_output=True, timeout=8, check=True,
                               creationflags=system.HIDDEN)
        supported, missing = json.loads(check.stdout)
        if supported:
            return missing
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        pass
    raise ValueError("El entorno .venv no funciona con Python 3.11 o posterior.")


def launch(store, *, port=PORT, browser=None, timeout=22):
    checkout = store.checkout
    with system.lock_file(store.directory / "launch.lock", timeout + 8):
        prefs = store.read()
        key = store.identity()
        status = probe(port, key)
        if status == "occupied":
            raise ValueError(f"El puerto {port} está ocupado por otra instancia o aplicación. No se pudo verificar su identidad.")
        if status == "free":
            python = system.venv_python(checkout)
            missing = check_python(python)
            if not (checkout / "dashboard.py").is_file():
                raise ValueError("Falta dashboard.py. Comprueba la carpeta de instalación.")
            environment = os.environ.copy()
            # dashboard.main also applies the saved tools for direct starts.
            media = system.find_ffmpeg(checkout, prefs["ffmpeg_dir"])
            system.tool_path(media, environment)
            store.logs.mkdir(parents=True, exist_ok=True)
            log_path = store.logs / "studio.log"
            if log_path.is_file() and log_path.stat().st_size > 4 * 1024**2:
                log_path.replace(store.logs / "studio.previous.log")
            kwargs = {}
            if os.name == "nt":
                info = subprocess.STARTUPINFO()
                info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                info.wShowWindow = 0
                kwargs = {"startupinfo": info, "creationflags": subprocess.CREATE_NEW_CONSOLE}
            else:
                kwargs["start_new_session"] = True
            with log_path.open("ab") as log:
                if missing:
                    log.write(("Paquetes Python ausentes: " + ", ".join(missing) + "\n").encode())
                if not media:
                    log.write(b"FFmpeg y FFprobe pendientes de configurar.\n")
                log.flush()
                child = subprocess.Popen([str(python), str(checkout / "dashboard.py"), "--no-browser",
                                          "--managed-launch", "--port", str(port)], cwd=checkout,
                                         env=environment, stdin=subprocess.DEVNULL, stdout=log,
                                         stderr=subprocess.STDOUT, **kwargs)
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if probe(port, key) == "ours":
                        break
                    if child.poll() is not None:
                        raise ValueError("Studio no pudo iniciar. Abre studio.log en la carpeta de registros.")
                    time.sleep(.15)
                else:
                    if child.poll() is None:
                        child.terminate()
                    raise ValueError("Studio no confirmó el arranque. Revisa studio.log en la carpeta de registros.")
    url = f"http://127.0.0.1:{port}/"
    should_open = prefs["open_browser"] if browser is None else browser
    if should_open:
        if not webbrowser.open(url):
            raise ValueError(f"Studio está listo. Abre {url} en tu navegador predeterminado.")
    return url


def main():
    parser = argparse.ArgumentParser(description="Inicia Chopify Studio desde su entorno virtual.")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    store = system.Settings(CHECKOUT)
    try:
        launch(store, browser=False if args.no_browser else None)
    except (OSError, ValueError) as exc:
        # Never include subprocess arguments, environment, account data or service responses.
        text = str(exc) if isinstance(exc, ValueError) else "Chopify no pudo acceder a su instalación o iniciar Studio."
        try:
            store.logs.mkdir(parents=True, exist_ok=True)
            with (store.logs / "launcher.log").open("a", encoding="utf-8") as log:
                log.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
        except OSError:
            pass
        message = text + "\n\nRegistros\n" + str(store.logs)
        if os.name == "nt":
            studio_windows.message(message)
        elif sys.stderr:
            print(message, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
