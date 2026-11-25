"""One-click launcher for the existing Chopify virtual environment."""
import argparse
import errno
import hmac
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import webbrowser

import studio_settings as system
import studio_windows
from unicode_io import configure_utf8, utf8_environment


CHECKOUT = Path(__file__).resolve().parent
PORT = 8765
STARTUP_TIMEOUT = 60
REFUSED = {errno.ECONNREFUSED, 10061}  # POSIX and Winsock WSAECONNREFUSED.
CONNECT_TIMEOUT = 3.0  # Allow Windows loopback to report refusal after TCP retries.
HTTP_TIMEOUT = .65


def probe(port, key):
    """Verify identity and distinguish a listener still awaiting an HTTP response."""
    nonce = secrets.token_hex(24)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=HTTP_TIMEOUT)
    try:
        try:
            conn.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            conn.sock.settimeout(CONNECT_TIMEOUT)
            code = conn.sock.connect_ex(("127.0.0.1", port))
        except ConnectionRefusedError:
            return "free"
        except OSError as exc:
            if exc.errno in REFUSED or getattr(exc, "winerror", None) in REFUSED:
                return "free"
            return "occupied"
        if code:
            return "free" if code in REFUSED else "occupied"
        conn.sock.settimeout(HTTP_TIMEOUT)
        conn.request("GET", "/api/health?nonce=" + nonce)
        with conn.getresponse() as response:
            if response.status != 200:
                return "occupied"
            length = response.getheader("Content-Length", "")
            if not length.isascii() or not length.isdecimal() or len(length) > 3 or int(length) > 512:
                return "occupied"
            expected = int(length)
            body = response.read(expected)
            if len(body) != expected:
                return "occupied"
            data = json.loads(body)
            if not isinstance(data, dict) or not isinstance(proof := data.get("proof"), str):
                return "occupied"
            if not proof.isascii() or not hmac.compare_digest(proof, system.instance_proof(key, nonce)):
                return "occupied"
            return "ours"
    except TimeoutError:
        return "pending"
    except (OSError, ValueError, http.client.HTTPException):
        return "occupied"
    finally:
        conn.close()


def wait_for_studio(port, key, timeout, child=None):
    """Wait for the challenge response, including when the socket is already bound."""
    deadline = time.monotonic() + timeout
    while True:
        status = probe(port, key)
        if status in {"occupied", "ours"} or (status == "free" and child is None):
            return status
        if child is not None and (code := child.poll()) is not None:
            raise ValueError(f"El proceso de Studio terminó antes de iniciar (código {code}). Revisa studio.log.")
        if time.monotonic() >= deadline:
            if child is None:
                raise ValueError(f"El puerto {port} acepta conexiones o aún no responde a la comprobación. "
                                 f"Se agotó la espera de {timeout:g} s sin iniciar otro proceso. Revisa launcher.log.")
            if status == "pending":
                raise ValueError(f"El proceso de Studio se inició, pero la comprobación HTTP en el puerto {port} "
                                 f"no respondió en {timeout:g} s. Revisa studio.log.")
            raise ValueError(f"El proceso de Studio no abrió el puerto {port} en {timeout:g} s. Revisa studio.log.")
        time.sleep(.15)


def check_python(path):
    if not path.is_file():
        raise ValueError("Falta python.exe en el entorno .venv de Anqora. Consulta la guía de primera configuración.")
    try:
        code = (
            "import importlib.util, json, sys; "
            "print(json.dumps([sys.version_info >= (3, 11) and sys.prefix != sys.base_prefix, "
            f"[name for name in {list(system.PACKAGES)!r} if importlib.util.find_spec(name) is None]]))"
        )
        check = subprocess.run([str(path), "-X", "utf8", "-c", code], env=utf8_environment(),
                               capture_output=True, timeout=8, check=True,
                               creationflags=system.HIDDEN)
        supported, missing = json.loads(check.stdout)
        if supported:
            return missing
    except subprocess.TimeoutExpired:
        raise ValueError("El intérprete de .venv no respondió durante su comprobación.") from None
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        pass
    raise ValueError("El entorno .venv no funciona con Python 3.11 o posterior.")


def write_launcher_log(store, text):
    try:
        store.logs.mkdir(parents=True, exist_ok=True)
        with (store.logs / "launcher.log").open("a", encoding="utf-8") as log:
            log.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
        return True
    except OSError:
        return False


def stop_starting_child(child):
    try:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
    except ProcessLookupError:
        pass


def launch(store, *, port=PORT, browser=None, timeout=STARTUP_TIMEOUT):
    checkout = store.checkout
    with system.lock_file(store.directory / "launch.lock", timeout + 15):
        prefs = store.read()
        key = store.identity()
        write_launcher_log(store, f"Verificando el puerto local {port} antes de iniciar el servidor.")
        status = wait_for_studio(port, key, timeout)
        if status == "free":
            python = system.venv_python(checkout)
            missing = check_python(python)
            essential = [system.PACKAGES[name] for name in missing if name in {"numpy", "cv2"}]
            if essential:
                raise ValueError("Faltan dependencias de arranque en .venv: " + ", ".join(essential) + ".")
            if not (checkout / "dashboard.py").is_file():
                raise ValueError("Falta dashboard.py. Comprueba la carpeta de instalación.")
            log_path = store.logs / "studio.log"
            try:
                store.logs.mkdir(parents=True, exist_ok=True)
                if log_path.is_file() and log_path.stat().st_size > 4 * 1024**2:
                    log_path.replace(store.logs / "studio.previous.log")
                log = log_path.open("ab", buffering=0)
            except OSError:
                raise ValueError("No se pudo crear studio.log. Comprueba el espacio y el acceso a la carpeta de registros.") from None
            kwargs = {"creationflags": system.HIDDEN} if os.name == "nt" else {"start_new_session": True}
            with log:
                log.write(time.strftime("\n--- Inicio de Studio %Y-%m-%d %H:%M:%S ---\n").encode())
                if missing:
                    log.write(("Paquetes Python ausentes para procesamiento: " + ", ".join(missing) + "\n").encode())
                try:
                    child = subprocess.Popen([str(python), "-X", "utf8", "-u", str(checkout / "dashboard.py"),
                                              "--no-browser", "--managed-launch", "--port", str(port)],
                                             cwd=checkout, env=utf8_environment(), stdin=subprocess.DEVNULL,
                                             stdout=log, stderr=subprocess.STDOUT, **kwargs)
                except OSError as exc:
                    code = getattr(exc, "winerror", None) or exc.errno
                    raise ValueError(f"El sistema no pudo ejecutar el proceso de Studio (código {code}).") from None
                write_launcher_log(store, f"Proceso de arranque creado (PID {child.pid}).")
                try:
                    status = wait_for_studio(port, key, timeout, child)
                finally:
                    if status != "ours":
                        try:
                            stop_starting_child(child)
                        except (OSError, subprocess.TimeoutExpired):
                            write_launcher_log(store, f"No se pudo detener el proceso de arranque (PID {child.pid}).")
        if status == "occupied":
            raise ValueError(f"El puerto {port} está ocupado o no se pudo verificar. "
                             "Comprueba si otra aplicación lo usa. Studio necesita una respuesta HTTP válida "
                             "con la identidad de esta instalación.")
        write_launcher_log(store, f"Identidad de Studio verificada en el puerto local {port}.")
    url = f"http://127.0.0.1:{port}/"
    should_open = prefs["open_browser"] if browser is None else browser
    if should_open:
        try:
            opened = webbrowser.open(url)
        except (OSError, webbrowser.Error):
            opened = False
        if not opened:
            raise ValueError(f"Studio está listo, pero no se pudo abrir el navegador. Abre {url} manualmente.")
    return url


def main():
    configure_utf8()
    parser = argparse.ArgumentParser(description="Inicia Anqora Media Studio desde su entorno virtual.")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    store = None
    try:
        store = system.Settings(CHECKOUT)
        launch(store, browser=False if args.no_browser else None)
    except (OSError, ValueError) as exc:
        # Never include subprocess arguments, environment, account data or service responses.
        text = str(exc) if isinstance(exc, ValueError) else "Anqora no pudo acceder a su instalación o iniciar Studio."
        if store is None:
            message = text + "\n\nNo se pudo determinar la carpeta de registros."
        else:
            saved = write_launcher_log(store, text)
            message = text + "\n\nCarpeta de registros\n" + str(store.logs)
            if not saved:
                message += "\nNo se pudo escribir launcher.log."
        if os.name == "nt":
            studio_windows.message(message)
        elif sys.stderr:
            print(message, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
