"""Real local server and Git tests, plus platform seams for the Windows launcher."""
from contextlib import contextmanager
import hashlib
import hmac
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

import dashboard
from editorial_providers import ProviderError
from secure_vault import DPAPI, Vault, VaultError
import studio_launcher
import studio_ollama
import studio_settings
import studio_windows
from studio_updates import BRANCH, Repository
import tiktok
from test_dashboard import request
from test_studio_navigation import chromium_page  # noqa: F401
from test_tiktok import FakeTikTok


CHECKOUT = Path(dashboard.__file__).resolve().parent
APP_KEY = "fixture-app-key-306db"
APP_SECRET = "fixture-app-secret-705aa"
NEW_REFRESH = "fixture-rotated-refresh-21a9"


class SimulatedDPAPI:
    """Test double only. Production uses Windows DPAPI."""
    available = True
    key = b"test-only-dpapi"

    def transform(self, value, label, protect):
        mask = hashlib.sha256(self.key + label).digest()

        def scramble(data):
            return bytes(value ^ mask[i % len(mask)] for i, value in enumerate(data))

        if protect:
            ciphertext = scramble(value)
            return hmac.digest(self.key, label + ciphertext, "sha256") + ciphertext
        tag, ciphertext = value[:32], value[32:]
        if not hmac.compare_digest(tag, hmac.digest(self.key, label + ciphertext, "sha256")):
            raise VaultError("Simulated protection rejected the data.")
        return scramble(ciphertext)


@contextmanager
def serving(store, *, port=0):
    app = dashboard.make_server(store.projects(store.read()), port, installation=store)
    thread = threading.Thread(target=app.serve_forever)
    thread.start()
    try:
        yield app, f"http://127.0.0.1:{app.server_port}", thread
    finally:
        if thread.is_alive():
            app.shutdown()
        thread.join(timeout=5)
        app.app.close()
        app.server_close()


def until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.08)
    pytest.fail("Condition was not reached")


def test_settings_subprocess_paths_and_different_installation_proofs(tmp_path, monkeypatch):
    checkout = tmp_path / "Estudio año 22 & medios"
    checkout.mkdir()
    directory = tmp_path / "Herramientas & vídeos" / "bin"
    directory.mkdir(parents=True)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(studio_settings, "ffmpeg_directory", lambda path: {
        "directory": str(directory), "versions": {"ffmpeg": "ffmpeg version 9.0.2", "ffprobe": "ffprobe version 9.0.2"}
    } if path == directory else None)
    store = studio_settings.Settings(checkout, tmp_path / "settings")
    assert not store.read()["setup_complete"]
    with pytest.raises(ValueError):
        store.save({"ffmpeg_dir": "./somewhere"})
    location = str(tmp_path / "Proyectos & prácticas")
    store.save({"ffmpeg_dir": str(directory), "projects_dir": location, "setup_complete": True})
    loaded = studio_settings.Settings(checkout, tmp_path / "settings")
    assert loaded.read()["setup_complete"]
    assert loaded.projects(loaded.read()) == Path(location)
    ffmpeg = studio_settings.find_ffmpeg(checkout, loaded.read()["ffmpeg_dir"])
    assert ffmpeg["source"] == "saved"
    child_env = {"PATH": "original"}
    studio_settings.tool_path(ffmpeg, child_env)
    assert child_env["PATH"] == str(directory) + os.pathsep + "original"
    assert os.environ["PATH"] == ""
    assert store.identity() == loaded.identity()
    assert store.identity() != studio_settings.Settings(tmp_path / "otra", tmp_path / "settings").identity()


def test_ffmpeg_probes_both_installed_bins(tmp_path, monkeypatch):
    # Use the OS executables with an empty PATH to exercise the absolute path.
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and FFprobe are media test prerequisites")
    destination = tmp_path / "FFmpeg & versiones"
    destination.mkdir()
    if os.name == "nt":
        pytest.skip("Windows builds may require adjacent distribution DLLs")
    for name in ("ffmpeg", "ffprobe"):
        (destination / name).symlink_to(shutil.which(name))
    monkeypatch.setenv("PATH", "")
    report = studio_settings.ffmpeg_directory(destination)
    assert report["versions"]["ffprobe"].startswith("ffprobe version ")
    assert report["versions"]["ffmpeg"].startswith("ffmpeg version ")


def test_health_rejects_other_services_and_confirms_only_this_instance(tmp_path):
    store = studio_settings.Settings(CHECKOUT, tmp_path / "user")
    store.save({"projects_dir": str(tmp_path / "projects")})
    with serving(store) as (server, base, _):
        port = server.server_port
        assert studio_launcher.probe(port, store.identity()) == "ours"
        assert studio_launcher.probe(port, b"not-this-key") == "identity"
        foreign_installation = studio_settings.Settings(tmp_path / "otra", tmp_path / "user")
        with pytest.raises(ValueError, match="identidad.*no coincide"):
            studio_launcher.launch(foreign_installation, port=port, browser=False)
        assert studio_launcher.probe(port, store.identity()) == "ours"
        with pytest.raises(HTTPError) as bad:
            request(base, "/api/health?nonce=malformed")
        assert bad.value.code == 400

    class Other(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"proof":"fake","server":"Chopify Studio"}')

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Other) as other:
        thread = threading.Thread(target=other.serve_forever)
        thread.start()
        try:
            with pytest.raises(ValueError, match="ocupado.*respuesta HTTP"):
                studio_launcher.launch(store, port=other.server_port, browser=False)
            assert studio_launcher.probe(other.server_port, store.identity()) == "http"
        finally:
            other.shutdown()
            thread.join()


def test_reuses_an_existing_studio_listener_as_soon_as_it_is_ready(tmp_path, monkeypatch):
    store = studio_settings.Settings(CHECKOUT, tmp_path / "user")
    store.save({"projects_dir": str(tmp_path / "projects"), "open_browser": True})
    app = dashboard.make_server(store.projects(store.read()), port=0, installation=store)
    allow_http = threading.Event()
    serving_started = threading.Event()

    def delayed_http():
        allow_http.wait()
        serving_started.set()
        app.serve_forever()

    thread = threading.Thread(target=delayed_http)
    thread.start()
    original_probe = studio_launcher.probe
    states = []
    browsers = []

    def probe_then_resume(port, key):
        result = original_probe(port, key)
        states.append(result)
        if result == "pending":
            allow_http.set()
        return result

    def open_after_ready(url):
        assert states[-1] == "ours"
        browsers.append(url)
        return True

    monkeypatch.setattr(studio_launcher, "probe", probe_then_resume)
    monkeypatch.setattr(studio_launcher.webbrowser, "open", open_after_ready)
    monkeypatch.setattr(studio_launcher.subprocess, "Popen", lambda *a, **kw: pytest.fail("Duplicate instance"))
    try:
        url = studio_launcher.launch(store, port=app.server_port, timeout=4)
        assert states == ["pending", "ours"]
        assert browsers == [url]
        store.save({"open_browser": False})
        assert studio_launcher.launch(store, port=app.server_port, timeout=4) == url
        assert browsers == [url]
    finally:
        allow_http.set()
        assert serving_started.wait(3)
        app.shutdown()
        thread.join(timeout=3)
        app.app.close()
        app.server_close()


def test_unresponsive_listener_times_out_without_starting_another_process(tmp_path, monkeypatch):
    store = studio_settings.Settings(CHECKOUT, tmp_path / "user")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        monkeypatch.setattr(studio_launcher.subprocess, "Popen", lambda *a, **kw: pytest.fail("Unexpected process"))
        with pytest.raises(ValueError, match="sin iniciar otro proceso.*launcher.log"):
            studio_launcher.launch(store, port=listener.getsockname()[1], browser=False, timeout=.2)
        assert listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
        assert "Verificando el puerto local" in (store.logs / "launcher.log").read_text(encoding="utf-8")
        assert not (store.logs / "studio.log").exists()


def test_windows_refused_socket_is_free(monkeypatch):
    error = OSError("Winsock connection refused")
    error.winerror = 10061

    def refused(_conn):
        raise error

    monkeypatch.setattr(studio_launcher.http.client.HTTPConnection, "connect", refused)
    assert studio_launcher.probe(8765, b"fixture") == "free"


def test_child_exit_reports_code_and_keeps_its_output(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout Á"
    checkout.mkdir()
    script = checkout / "dashboard.py"
    script.write_text("print('Diagnóstico antes de terminar: 治')\nraise SystemExit(31)\n", encoding="utf-8")
    store = studio_settings.Settings(checkout, tmp_path / "state")
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    monkeypatch.setattr(studio_settings, "venv_python", lambda _root: Path(sys.executable))
    monkeypatch.setattr(studio_launcher, "check_python", lambda _path: [])
    monkeypatch.setattr(studio_launcher.webbrowser, "open", lambda _url: pytest.fail("Child did not answer"))
    with pytest.raises(ValueError, match="terminó antes de iniciar.*31.*studio.log"):
        studio_launcher.launch(store, port=port, timeout=5)
    assert "Diagnóstico antes de terminar: 治" in (store.logs / "studio.log").read_text(encoding="utf-8")
    assert "Proceso de arranque creado" in (store.logs / "launcher.log").read_text(encoding="utf-8")
    assert studio_launcher.probe(port, store.identity()) == "free"


def test_timeout_without_listener_stops_only_the_child_it_started(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "dashboard.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    store = studio_settings.Settings(checkout, tmp_path / "state")
    monkeypatch.setattr(studio_settings, "venv_python", lambda _root: Path(sys.executable))
    monkeypatch.setattr(studio_launcher, "check_python", lambda _path: [])
    monkeypatch.setattr(studio_launcher, "probe", lambda *_: "free")
    original_popen = subprocess.Popen
    started = []

    def record(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        started.append(child)
        return child

    monkeypatch.setattr(studio_launcher.subprocess, "Popen", record)
    with pytest.raises(ValueError, match="no abrió el puerto.*studio.log"):
        studio_launcher.launch(store, timeout=.3, browser=False)
    child, = started
    assert child.poll() is not None


def test_missing_bootstrap_dependency_reports_venv_before_launch(tmp_path, monkeypatch):
    store = studio_settings.Settings(CHECKOUT, tmp_path / "state")
    monkeypatch.setattr(studio_launcher, "probe", lambda *_: "free")
    monkeypatch.setattr(studio_launcher, "check_python", lambda _path: ["cv2", "yt_dlp"])
    monkeypatch.setattr(studio_launcher.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("Unexpected process"))
    with pytest.raises(ValueError, match="dependencias de arranque en \\.venv: opencv-python"):
        studio_launcher.launch(store)
    assert not (store.logs / "studio.log").exists()


def test_windows_subprocess_uses_hidden_flag_and_reports_start_failure(tmp_path, monkeypatch):
    store = studio_settings.Settings(CHECKOUT, tmp_path / "state")

    def denied(command, **kwargs):
        assert kwargs["creationflags"] == 0x08000000 and "startupinfo" not in kwargs
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["stderr"] == subprocess.STDOUT
        assert kwargs["env"]["PYTHONUTF8"] == "1" and kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
        assert kwargs["cwd"] == CHECKOUT
        assert command[1:] == ["-X", "utf8", "-u", str(CHECKOUT / "dashboard.py"),
                               "--no-browser", "--managed-launch", "--port", "8765"]
        raise PermissionError(13, "fixture")

    # Simulate only the process arguments; keep pathlib and file locking native.
    monkeypatch.setattr(studio_launcher, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(studio_launcher, "probe", lambda *_: "free")
    monkeypatch.setattr(studio_launcher, "check_python", lambda _path: [])
    monkeypatch.setattr(studio_settings, "venv_python", lambda _root: CHECKOUT / "python.exe")
    monkeypatch.setattr(studio_settings, "HIDDEN", 0x08000000)
    monkeypatch.setattr(studio_launcher.subprocess, "Popen", denied)
    with pytest.raises(ValueError, match="ejecutar el proceso de Studio.*13"):
        studio_launcher.launch(store)
    assert (store.logs / "studio.log").is_file()


def test_desktop_shortcut_bootstraps_python_utf8_and_recognizes_previous_target(tmp_path, monkeypatch):
    checkout = tmp_path / "Chopify 治 العربية"
    pythonw = checkout / ".venv" / "Scripts" / "pythonw.exe"
    pythonw.parent.mkdir(parents=True)
    pythonw.touch()
    monkeypatch.setattr(studio_windows, "venv_python", lambda root, *, windowless: pythonw)

    def powershell(script, env):
        assert env["CS_DESKTOP_ARGS"] == '-X utf8 "' + str(checkout / "studio_launcher.py") + '"'
        assert env["CS_DESKTOP_PREVIOUS_ARGS"] == '"' + str(checkout / "studio_launcher.py") + '"'
        assert "$env:CS_DESKTOP_PREVIOUS_ARGS" in script
        assert env["CS_DESKTOP_TARGET"] == str(pythonw)
        assert env["CS_DESKTOP_WORKDIR"] == str(checkout)
        assert env["CS_DESKTOP_ICON"] == str(checkout / "assets" / "studio" / "anqora.ico") + ",0"
        assert "-not $ours" in script
        return "created"

    monkeypatch.setattr(studio_windows, "_powershell", powershell)
    assert studio_windows.shortcut(checkout, "create") == "created"


@pytest.mark.parametrize("configured_ffmpeg", [False, True])
def test_real_launcher_first_and_second_start(tmp_path, monkeypatch, configured_ffmpeg):
    if os.name == "nt" or sys.prefix == sys.base_prefix:
        pytest.skip("This subprocess test runs with the Linux test virtual environment")
    checkout = tmp_path / "Chopify versión & edición"
    checkout.mkdir()
    for source in CHECKOUT.glob("*.py"):
        shutil.copy2(source, checkout / source.name)
    shutil.copytree(CHECKOUT / "assets" / "studio", checkout / "assets" / "studio")
    (checkout / ".venv").symlink_to(sys.prefix, target_is_directory=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "user state"))
    monkeypatch.setenv("CHOPIFY_TIKTOK_CLIENT_KEY", APP_KEY)
    monkeypatch.setenv("CHOPIFY_TIKTOK_CLIENT_SECRET", APP_SECRET)
    monkeypatch.setenv("CHOPIFY_TIKTOK_PROFILE", "production")
    store = studio_settings.Settings(checkout)
    prefs = {"projects_dir": str(tmp_path / "saved work & media"), "open_browser": True}
    if configured_ffmpeg:
        media = tmp_path / "Herramientas vídeo"
        media.mkdir()
        for executable in ("ffmpeg", "ffprobe"):
            if not (path := shutil.which(executable)):
                pytest.skip("FFmpeg and FFprobe required")
            (media / executable).symlink_to(path)
        prefs["ffmpeg_dir"] = str(media)
    monkeypatch.setenv("PATH", "")
    store.save(prefs)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    pages = []
    monkeypatch.setattr(studio_launcher.webbrowser, "open", lambda url: pages.append(url) or True)
    key = store.identity()
    try:
        first = studio_launcher.launch(store, port=port)
        assert studio_launcher.launch(store, port=port) == first
        assert pages == [first, first]
        assert studio_launcher.probe(port, key) == "ours"
        log = (store.logs / "studio.log").read_text(encoding="utf-8")
        assert log.count("Anqora Media Studio:") == 1
        assert ("FFmpeg y FFprobe disponibles." if configured_ffmpeg else
                "FFmpeg y FFprobe pendientes de configurar.") in log
        assert APP_KEY not in log and APP_SECRET not in log
        base = first.rstrip("/")
        status = request(base, "/api/system/status")
        assert status["projects_dir"] == store.read()["projects_dir"]
        assert status["integrations"]["source"] == "environment"
        assert all(value not in json.dumps(status) for value in (APP_KEY, APP_SECRET))
        assert APP_KEY not in (store.logs / "launcher.log").read_text(encoding="utf-8")

        def failed_browser(_url):
            raise studio_launcher.webbrowser.Error("fixture")

        monkeypatch.setattr(studio_launcher.webbrowser, "open", failed_browser)
        with pytest.raises(ValueError, match="no se pudo abrir el navegador"):
            studio_launcher.launch(store, port=port)
        assert studio_launcher.probe(port, key) == "ours"
    finally:
        if studio_launcher.probe(port, key) == "ours":
            base = f"http://127.0.0.1:{port}"
            token = request(base, "/api/config")["token"]
            request(base, "/api/system/shutdown", {"confirmed": True, "mode": "cancel"}, token)
            until(lambda: studio_launcher.probe(port, key) == "free")


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI must be verified using a Windows user profile")
def test_native_windows_dpapi_round_trip(tmp_path):
    vault = Vault(tmp_path)
    if not vault.available():
        pytest.skip("Windows profile does not support the DPAPI probe")
    vault.write("sandbox", "application", {"key": APP_KEY, "secret": APP_SECRET})
    assert vault.read("sandbox", "application")["secret"] == APP_SECRET
    assert APP_SECRET.encode() not in (tmp_path / "sandbox-application.dpapi").read_bytes()
    vault.delete("sandbox", "application")


def test_unavailable_secure_storage_permits_session_only(tmp_path, monkeypatch):
    monkeypatch.delenv("CHOPIFY_TIKTOK_CLIENT_KEY", raising=False)
    monkeypatch.delenv("CHOPIFY_TIKTOK_CLIENT_SECRET", raising=False)
    protector = SimulatedDPAPI()
    protector.available = False
    vault = Vault(tmp_path / "protected", protector)
    service = tiktok.TikTokStudio(tmp_path, lambda _: False, vault)
    with pytest.raises(VaultError, match="no está disponible"):
        service.change_credentials("production", "save", APP_KEY, APP_SECRET)
    service.change_credentials("production", "temporary", APP_KEY, APP_SECRET)
    assert service.integrations()["source"] == "session"
    assert not vault.directory.exists()
    assert not tiktok.TikTokStudio(tmp_path, lambda _: False, vault).state()["configured"]


def test_secure_credentials_and_oauth_rotation_survive_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("CHOPIFY_TIKTOK_CLIENT_KEY", raising=False)
    monkeypatch.delenv("CHOPIFY_TIKTOK_CLIENT_SECRET", raising=False)
    store = studio_settings.Settings(CHECKOUT, tmp_path / "user")
    store.vault = Vault(store.directory / "encrypted", SimulatedDPAPI())
    store.save({"projects_dir": str(tmp_path / "projects"), "tiktok_profile": "sandbox"})
    fake = FakeTikTok()
    revoked = False

    def send(_api, req):
        url = urlsplit(req.full_url)
        if url.path == "/v2/oauth/token/" and b"grant_type=refresh_token" in req.data:
            if revoked:
                # A raw provider message may contain credentials and must be ignored.
                return 200, json.dumps({"error": "invalid_grant", "error_description": APP_SECRET}).encode()
            status, raw = fake(req)
            token = json.loads(raw)
            token["refresh_token"] = NEW_REFRESH
            return status, json.dumps(token).encode()
        return fake(req)

    monkeypatch.setattr(tiktok.TikTokAPI, "send", send)
    with serving(store) as (server, base, _):
        token = server.app.token
        payload = {"profile": "sandbox", "action": "save", "key": APP_KEY, "secret": APP_SECRET}
        with pytest.raises(HTTPError) as rejected:
            request(base, "/api/system/credentials", payload, token, "https://foreign.invalid")
        assert rejected.value.code == 403
        saved = request(base, "/api/system/credentials", payload, token)
        assert saved["protected_available"] and saved["source"] == "protected"
        assert not saved["profiles"]["production"]["protected"]
        redirect = request(base, "/api/tiktok/connect", {}, token)
        assert redirect["url"].startswith(tiktok.AUTHORIZE_START)
        assert APP_KEY not in json.dumps(redirect)
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("GET", redirect["url"])
        response = conn.getresponse()
        assert response.status == 303
        params = parse_qs(urlsplit(response.getheader("Location")).query)
        assert params["client_key"] == [APP_KEY]
        conn.close()
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("GET", tiktok.CALLBACK + "?" + urlencode({"state": params["state"][0], "code": "fake-approval"}))
        assert conn.getresponse().status == 303
        conn.close()
        assert request(base, "/api/system/status")["integrations"]["authorization_saved"]
        # Simulate an access token expiring before the next start.
        server.app.tiktok.token["expires"] = time.monotonic() - 2
        server.app.tiktok._persist()
        for payload in store.vault.directory.glob("*.dpapi"):
            ciphertext = payload.read_bytes()
            assert all(secret.encode() not in ciphertext for secret in (APP_KEY, APP_SECRET, fake.token))
    with serving(store) as (server, base, _):
        auth = request(base, "/api/system/status")["integrations"]
        assert auth["authorization"] == "unverified"
        server.app.system.restore_in_background()
        auth = until(lambda: (current if (current := request(base, "/api/system/status")["integrations"])
                                                ["authorization"] == "verified" else None))
        assert auth["account"] == "Cuenta de prueba"
        stored = store.vault.read("sandbox", "authorization")
        assert stored["refresh_token"] == NEW_REFRESH
        assert stored["access_until"] > time.time()
        assert all(secret not in json.dumps(auth) for secret in (APP_KEY, APP_SECRET, NEW_REFRESH, fake.token))
        revoked = True
        with pytest.raises(HTTPError) as error:
            request(base, "/api/system/verify", {"renew": True}, server.app.token)
        reply = error.value.read().decode()
        assert error.value.code == 400 and "caducó" in reply
        assert APP_SECRET not in reply
        assert store.vault.read("sandbox", "authorization") is None
        assert store.vault.read("sandbox", "application")["secret"] == APP_SECRET


def test_env_overrides_temporary_and_protected_pairs_without_profile_mix(tmp_path, monkeypatch):
    vault = Vault(tmp_path, SimulatedDPAPI())
    vault.write("production", "application", {"key": "prod-key", "secret": "prod-secret"})
    monkeypatch.setenv("CHOPIFY_TIKTOK_CLIENT_KEY", APP_KEY)
    monkeypatch.setenv("CHOPIFY_TIKTOK_CLIENT_SECRET", APP_SECRET)
    monkeypatch.setenv("CHOPIFY_TIKTOK_PROFILE", "sandbox")
    service = tiktok.TikTokStudio(tmp_path, lambda _: False, vault, "sandbox")
    service.change_credentials("sandbox", "temporary", "other-key", "other-secret")
    assert service.api.key == APP_KEY and service.integrations()["source"] == "environment"
    service.select_profile("production")
    assert service.api.key == "prod-key" and service.integrations()["source"] == "protected"


def test_ollama_manager_handles_missing_offline_and_unavailable_model(tmp_path, monkeypatch):
    class Provider:
        online = False

        def _request(self, path, *_):
            assert path == "/api/version"
            if not self.online:
                raise ProviderError("offline", "Ollama no disponible.")
            return {"version": "0.99.1"}

        def models(self):
            return [{"name": "qwen3:4b", "digest": "a" * 64}, {"name": "unverified", "digest": None}]

    provider = Provider()
    manager = studio_ollama.Manager(tmp_path / "ollama.log", provider=provider)
    monkeypatch.setattr(studio_ollama, "find_ollama", lambda: None)
    assert not manager.ensure(True)["installed"]
    executable = tmp_path / "Ollama & programas" / "ollama.exe"
    monkeypatch.setattr(studio_ollama, "find_ollama", lambda: executable)

    def refused(*_, **_kwargs):
        raise ConnectionRefusedError

    monkeypatch.setattr(studio_ollama.socket, "create_connection", refused)
    assert not manager.ensure(False)["server"]

    class Child:
        done = False

        def poll(self):
            return 0 if self.done else None

        def terminate(self):
            self.done = True

        def wait(self, timeout):
            assert self.done

    child = Child()

    def start(args, env, **kwargs):
        assert args == [str(executable), "serve"]
        assert env["OLLAMA_HOST"] == "127.0.0.1:11434"
        assert env["OLLAMA_NO_CLOUD"] == "1"
        provider.online = True
        return child

    monkeypatch.setattr(studio_ollama.subprocess, "Popen", start)
    state = manager.ensure(True, "unknown:3b")
    assert state["server"] and state["privacy"] == "local_cloud_disabled"
    assert state["models"] == ["qwen3:4b"] and not state["model_available"]
    other = studio_ollama.Manager(provider=provider)
    assert other.ensure(True)["privacy"] == "external_unverified"
    other.close()
    assert not child.done
    manager.close()
    assert child.done


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True, stderr=subprocess.DEVNULL).strip()


def test_update_preserves_local_changes_and_uses_confirmed_fast_forward(tmp_path):
    if not shutil.which("git"):
        pytest.skip("Git is required for assisted-update tests")
    origin = tmp_path / "remote & simulated"
    origin.mkdir()
    git(origin, "init", "-b", BRANCH)
    git(origin, "config", "user.email", "test@example.invalid")
    git(origin, "config", "user.name", "Chopify local fixture")
    (origin / "code.py").write_text("first\n")
    git(origin, "add", "code.py")
    git(origin, "commit", "-m", "first")
    clone = tmp_path / "clone & saved"
    git(tmp_path, "clone", str(origin), str(clone))
    repo = Repository(clone, tmp_path / "update.lock", str(origin))
    first = repo.local()["installed"]
    (origin / "code.py").write_text("second\n")
    git(origin, "commit", "-am", "second")
    informed = repo.check()
    assert informed["available"] and informed["eligible"]
    (clone / "keep.txt").write_text("local work")
    assert repo.check()["dirty"]
    with pytest.raises(ValueError, match="checkout limpio"):
        repo.apply(first, informed["remote"])
    assert (clone / "keep.txt").read_text() == "local work"
    (clone / "keep.txt").unlink()
    with pytest.raises(ValueError, match="rama remota cambió"):
        repo.apply(first, "a" * 40)
    assert repo.apply(first, informed["remote"])["installed"] == informed["remote"]
    assert repo.local()["branch"] == BRANCH
    assert (clone / "code.py").read_text() == "second\n"


def test_shutdown_waits_for_active_work_and_keeps_persistent_queue(tmp_path):
    store = studio_settings.Settings(CHECKOUT, tmp_path / "user")
    store.save({"projects_dir": str(tmp_path / "projects")})
    with serving(store) as (server, base, thread):
        app = server.app
        release = threading.Event()
        active = app.executor.submit(release.wait, 6)
        app.futures["active"] = active
        app.production.store.config(paused=True)
        app.production.enqueue("a" * 12, "next.mp4")
        try:
            with pytest.raises(HTTPError) as busy:
                request(base, "/api/system/update", {"confirmed": True}, app.token)
            assert busy.value.code == 400 and not app.closing
            reply = request(base, "/api/system/shutdown", {"mode": "wait", "confirmed": True}, app.token)
            assert reply == {"mode": "wait"} and thread.is_alive()
            with pytest.raises(HTTPError) as new_work:
                request(base, "/api/system/save", {"setup_complete": True}, app.token)
            assert new_work.value.code == 409
        finally:
            release.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
    with serving(store) as (server, base, _):
        snapshot = request(base, "/api/production/status")
        assert snapshot["config"]["paused"]
        assert snapshot["jobs"][0]["filename"] == "next.mp4"
        assert snapshot["jobs"][0]["state"] == "generated"


def test_system_wizard_and_persisted_preferences_in_chromium(tmp_path, chromium_page, monkeypatch):
    store = studio_settings.Settings(CHECKOUT, tmp_path / "user")
    store.save({"projects_dir": str(tmp_path / "projects & estudio")})
    page, expect = chromium_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    with serving(store) as (server, base, _):
        page.goto(base)
        expect(page.get_by_role("heading", name="Primera configuración")).to_be_visible()
        expect(page.locator("#wizard-progress")).to_contain_text("Paso 1")
        page.get_by_role("button", name="Siguiente").click()
        expect(page.locator("#wizard-progress")).to_contain_text("FFmpeg y FFprobe")
        page.get_by_role("button", name="Terminar configuración").click()
        expect(page.get_by_role("heading", name="Primera configuración")).to_be_hidden(timeout=12000)
        expect(page.get_by_text("Configuración inicial guardada.")).to_be_visible()
        page.locator('#system-general input[name="open_browser"]').uncheck()
        page.get_by_role("button", name="Guardar preferencias generales").click()
        expect(page.get_by_text("Preferencias guardadas para el próximo inicio.")).to_be_visible()
        page.locator("#system-view").screenshot(path=str(tmp_path / "system-settings-chromium.png"))
    with serving(store) as (_, base, _):
        page.goto(base)
        page.locator("#show-settings").click()
        page.locator("#show-system").click()
        expect(page.get_by_role("heading", name="Primera configuración")).to_be_hidden()
        expect(page.locator('#system-general input[name="open_browser"]')).not_to_be_checked()
        assert not errors
