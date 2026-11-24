"""Studio and Telegram tests with a simulated bot. Never reach Telegram."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace
import urllib.error

import pytest

from editorial_store import EditorialStore
from production_store import Store as ProductionStore
from secure_vault import Vault
import subtitle_quality
import telegram_api
from telegram_api import TelegramError
import telegram_gateway as gateway
from telegram_store import DEFAULTS, Store, capture_source, normalize
from test_dashboard import request, server  # noqa: F401
from test_production import until
from test_studio_launcher import SimulatedDPAPI
from test_studio_navigation import chromium_page  # noqa: F401

TOKEN = "123456789:TEST_ONLY_01234567890123456789"
OWNER = 118
VIDEO = "https://www.youtube.com/watch?v=2dt0yxm4RUE"


class Bot:
    def __init__(self):
        self.calls = []
        self.error = None

    def call(self, method, payload=None, video=None):
        self.calls.append((method, payload, video))
        if self.error:
            raise self.error
        return [] if method == "getUpdates" else {"ok": True}


def message(text, owner=OWNER, kind="private", chat=None, update_id=1):
    return {"update_id": update_id, "message": {"text": text, "date": int(time.time()),
            "from": {"id": owner, "is_bot": False}, "chat": {"id": owner if chat is None else chat, "type": kind}}}


def callback(data, owner=OWNER, update_id=1):
    update = message("", owner=owner, update_id=update_id)
    private = update.pop("message")
    update["callback_query"] = {"id": "callback-fixture", "data": data, "from": private["from"], "message": private}
    return update


def installation(tmp_path):
    return SimpleNamespace(directory=tmp_path / "installed",
                           vault=Vault(tmp_path / "installed" / "protected", SimulatedDPAPI()))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    install = installation(tmp_path)
    bot = Bot()
    app = SimpleNamespace(root=tmp_path, automation_settings=SimpleNamespace(
        read=lambda: {"settings": {"minimum_clip_score": 6, "timezone": "Etc/UTC"}}))
    remote = gateway.Gateway(app, install, lambda token: bot)
    monkeypatch.setattr(remote, "start", lambda: None)
    remote.control("settings", {"settings": {"enabled": True}, "token": TOKEN})
    yield remote, bot, app
    remote.close()


def pair(remote, bot):
    code = remote.control("pair", {})["code"]
    remote.handle(bot, message("/pair " + code))
    assert remote.status()["paired"]


def seed(app):
    editor = EditorialStore(app.root / "editorial.sqlite")
    editor.migrate()
    production = ProductionStore(editor)
    production.migrate()
    original = app.root / "clip.mp4"
    original.write_bytes(b"local reviewed MP4 fixture")
    app.production = SimpleNamespace(store=production, editor=SimpleNamespace(store=editor),
                                     current_evidence=lambda job, evidence: evidence["current"],
                                     clip_path=lambda project, name: original)
    variant = {"style": "contextual", "valid": True, "score": 9, "title": "Una historia verificable",
               "caption": "Contexto", "description": "Descripción con contexto.", "call_to_action": "",
               "hashtags": ["#Historia"]}
    result = {"mode": "semantic", "editorial_score": 8.2, "standalone": True,
              "recommended_variant": "contextual", "variants": [variant]}
    now = time.time()
    with editor.connect(write=True) as db:
        db.execute("""INSERT INTO editorial_jobs(id,clip_id,fingerprint,source,settings,created,
                    state,result,revision) VALUES(1,'fixture','id','{}','{}',?,'done',?,5)""", (now, json.dumps(result)))
        db.execute("""INSERT INTO production_jobs(id,project,filename,state,created,updated,finished,editorial_id,technical)
                    VALUES(17,'test-project','clip.mp4','ready',?,?,?,1,'passed')""", (now, now, now))
        db.execute("""INSERT INTO production_evidence(job_id,run,file,detection,editorial,priority,evidence,report)
                      VALUES(17,1,'clip.mp4',7.5,8.2,8.1,?,?)""",
                   (json.dumps({"current": True}), json.dumps({"duration": 75., "speech_coverage": .95})))
    return editor, production, original


def test_pairing_requires_code_and_private_matching_ids_and_survives_restart(rig):
    remote, bot, app = rig
    code = remote.control("pair", {})["code"]
    for bad in [message("/start"), message("/pair " + code, kind="group"),
                message("/pair " + code, owner=2, chat=OWNER), message("/pair " + "0" * 16)]:
        remote.handle(bot, bad)
    assert not bot.calls and not remote.status()["paired"]
    remote.pairing = (code, time.monotonic() - 1)
    remote.handle(bot, message("/pair " + code))
    assert not remote.status()["paired"]
    pair(remote, bot)
    codes = len(bot.calls)
    remote.handle(bot, message("/help", owner=OWNER + 1))
    remote.handle(bot, callback("a:" + remote._clip_key((17, 1, 5)), owner=OWNER + 1))
    assert len(bot.calls) == codes
    restored = gateway.Gateway(app, remote.installation, lambda token: bot)
    assert restored.status()["paired"] and restored.status()["has_token"]
    remote.control("revoke", {})
    assert not restored.status()["paired"]
    assert TOKEN.encode() not in remote.store.path.read_bytes()
    assert TOKEN.encode() not in (remote.directory / "protected" / "telegram-bot.dpapi").read_bytes()
    assert TOKEN not in json.dumps(remote.status())


def test_authorized_start_and_one_time_stop(rig):
    remote, bot, app = rig
    pair(remote, bot)
    remote.control("settings", {"settings": {"enabled": True, "sources": [VIDEO]}})
    operations = []
    app.start_live = lambda url, options: operations.append((url, options["live_threshold"])) or SimpleNamespace(id="abcdef123456")
    app.live_list = lambda: [{"id": "abcdef123456", "status": "running"}]
    app.stop_live = operations.append
    for source in ("http://127.0.0.1:8765/anything", "https://www.youtube.com/watch?v=other000000", "file:///a"):
        remote.handle(bot, message("/capture " + source))
    assert not operations
    remote.handle(bot, message("/capture https://youtu.be/2dt0yxm4RUE"))
    assert operations == [(VIDEO, 6)]
    remote.handle(bot, message("/stop abcdef123456"))
    assert len(operations) == 1
    confirm = bot.calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    remote.handle(bot, callback(confirm, owner=OWNER + 1))
    assert len(operations) == 1
    remote.handle(bot, callback(confirm))
    remote.handle(bot, callback(confirm))
    assert operations[-1:] == ["abcdef123456"] and len(operations) == 2


def test_old_review_buttons_do_not_survive_rebinding_or_workspace_switch(rig, tmp_path):
    remote, bot, app = rig
    pair(remote, bot)
    _, production, _ = seed(app)
    old = "a:" + remote._clip_key((17, 1, 5))
    remote.control("revoke", {})
    pair(remote, bot)
    remote.handle(bot, callback(old))
    assert production.detail(17)["remote_review"] is None
    other = SimpleNamespace(root=tmp_path / "other-projects")
    restored = gateway.Gateway(other, remote.installation, lambda _: bot)
    assert not restored.status()["paired"] and not restored.status()["has_token"]


def test_versioned_decisions_and_real_studio_history(rig):
    remote, bot, app = rig
    pair(remote, bot)
    editor, store, _ = seed(app)
    remote.handle(bot, callback("p:" + remote._clip_key((17, 1, 5))))
    assert store.detail(17)["remote_review"]["decision"] == "postponed"
    remote.handle(bot, callback("a:" + remote._clip_key((17, 1, 5))))
    remote.handle(bot, callback("a:" + remote._clip_key((17, 1, 5))))
    remote.handle(bot, callback("r:" + remote._clip_key((17, 1, 5))))
    assert store.status()["jobs"][0]["remote_review"]["decision"] == "approved"
    assert [event["code"] for event in store.detail(17)["events"]].count("editorial_approved") == 1
    remote.handle(bot, callback("t:" + remote._clip_key((17, 1, 5))))
    remote.handle(bot, message("/edit Título editado desde Telegram"))
    job = editor.detail(1)
    assert job["revision"] == 6
    assert job["edits"]["contextual"]["title"] == "Título editado desde Telegram"
    assert store.detail(17)["remote_review"] is None
    remote.handle(bot, callback("a:" + remote._clip_key((17, 1, 5))))
    assert store.detail(17)["remote_review"] is None
    remote.handle(bot, callback("c:" + remote._clip_key((17, 1, 6))))
    assert store.detail(17)["remote_review"]["decision"] == "correction"
    store.retry(17)
    remote.handle(bot, callback("a:" + remote._clip_key((17, 1, 6))))
    assert store.detail(17)["run"] == 2 and store.detail(17)["remote_review"] is None


def test_immediate_delivery_uses_current_evidence_and_is_deduplicated(rig):
    remote, bot, app = rig
    pair(remote, bot)
    _, _, media = seed(app)
    remote.control("settings", {"settings": {"enabled": True, "alerts": False}})
    remote._scan(remote.store.state())
    remote._scan(remote.store.state())
    assert remote.store.diagnostics()["deliveries"] == {"queued": 1}
    job = remote.store.claim(12)
    remote.send_delivery(bot, OWNER, remote.generation, job)
    method, body, video = bot.calls[-1]
    assert method == "sendVideo" and video == media
    assert {"Detección", "Editorial", "Global", "Subtítulos", "#Historia"} <= set(body["caption"].replace(":", "").split())
    assert len(body["caption"].encode("utf-16-le")) // 2 < 1024
    remote._scan(remote.store.state())
    assert remote.store.diagnostics()["deliveries"] == {"sent": 1}
    assert remote.store.claim(1) is None


def test_telegram_scan_before_editorial_schema_is_initialized(rig):
    remote, bot, app = rig
    pair(remote, bot)
    store = ProductionStore(EditorialStore(app.root / "lazy-editorial.sqlite"))
    store.migrate()
    store.enqueue("fixture", "clip.mp4")
    app.production = SimpleNamespace(store=store)
    remote.control("settings", {"settings": {"enabled": True, "alerts": False}})
    remote._scan(remote.store.state())
    assert store.status()["jobs"][0]["remote_review"] is None
    assert remote.store.diagnostics()["deliveries"] == {}


def test_outbox_retries_explicit_errors_and_quarantines_ambiguous_deliveries(rig):
    remote, bot, _ = rig
    pair(remote, bot)
    remote.store.enqueue("retry", {"text": "test"})
    bot.error = TelegramError("Rate limit", retry=True, delay=4)
    remote.send_delivery(bot, OWNER, remote.generation, remote.store.claim(12))
    assert remote.store.diagnostics()["deliveries"] == {"queued": 1}
    assert remote.store.claim(12) is None
    with remote.store.connect(write=True) as db:
        db.execute("UPDATE outbox SET available=0")
    bot.error = TelegramError("Respuesta no confirmada", uncertain=True)
    remote.send_delivery(bot, OWNER, remote.generation, remote.store.claim(12))
    remote.store.enqueue("crashed", {"text": "test"})
    assert remote.store.claim(12)["key"] == "crashed"
    restored = Store(remote.store.path)
    restored.recover()
    assert restored.diagnostics()["deliveries"] == {"uncertain": 2}
    assert restored.claim(12) is None


def test_poll_recovery_cursor_and_duplicate_updates(rig):
    remote, bot, _ = rig
    pair(remote, bot)
    resumed = threading.Event()
    batch = [message("/help", update_id=42), message("/help", update_id=42)]
    offsets = []

    def respond(method, payload=None, video=None):
        if method != "getUpdates":
            bot.calls.append((method, payload, video))
            return True
        offsets.append(payload["offset"])
        if len(offsets) == 1:
            raise TelegramError("Simulated offline", retry=True)
        if len(offsets) == 2:
            return batch
        resumed.set()
        remote.close()
        return []

    remote.factory = lambda _: SimpleNamespace(call=respond)
    with remote.store.connect(write=True) as db:
        db.execute("UPDATE config SET offset=40")
    remote._poll()
    assert resumed.is_set()
    assert offsets == [40, 40, 43] and remote.store.state()["offset"] == 43
    assert sum("después" in call[1]["text"] for call in bot.calls if call[0] == "sendMessage") == 1
    assert any("recuperada" in row["message"] for row in remote.store.diagnostics()["log"])
    assert remote.store.diagnostics()["deliveries"] == {"queued": 1}


def test_background_workers_resume_unattempted_outbox_after_restart(rig):
    remote, bot, app = rig
    pair(remote, bot)
    remote.control("settings", {"settings": {"enabled": True, "alerts": False, "immediate": False}})
    remote.store.enqueue("before-restart", {"text": "Entrega pendiente del Studio anterior."})
    remote.close()
    def call(method, payload=None, video=None):
        if method == "getUpdates":
            time.sleep(.05)
        return bot.call(method, payload, video)
    restored = gateway.Gateway(app, remote.installation, lambda _: SimpleNamespace(call=call))
    restored.start()
    try:
        until(lambda: restored.store.diagnostics()["deliveries"], lambda counts: counts.get("sent") == 1, 8)
        assert [payload["chat_id"] for method, payload, _ in bot.calls if method == "sendMessage"] == [OWNER, OWNER]
    finally:
        restored.close()
        restored.threads[0].join(3)


def test_client_status_and_invalid_token_never_expose_raw_response(monkeypatch):
    class Connection:
        response = (401, {"ok": False, "error_code": 401, "description": TOKEN})
        def __init__(self, host, **kwargs):
            assert host == "api.telegram.org" and kwargs["context"].check_hostname
        def connect(self):
            pass
        def request(self, method, path, **kwargs):
            assert method == "POST" and path.startswith("/bot")
        def getresponse(self):
            return SimpleNamespace(status=self.response[0], read=lambda n: json.dumps(self.response[1]).encode())
        def close(self):
            pass
    monkeypatch.setattr(telegram_api.http.client, "HTTPSConnection", Connection)
    with pytest.raises(TelegramError) as err:
        telegram_api.Client(TOKEN).call("getMe")
    assert err.value.unauthorized and TOKEN not in str(err.value)
    Connection.response = (200, {"ok": True, "result": {"id": 1}})
    assert telegram_api.Client(TOKEN).call("getMe") == {"id": 1}
    with pytest.raises(ValueError):
        telegram_api.Client("wrong")


def test_client_streams_multipart_and_quarantines_unconfirmed_upload(tmp_path, monkeypatch):
    sent = []
    clip = tmp_path / "review.mp4"
    clip.write_bytes(b"video fixture contents")

    class Connection:
        sock = SimpleNamespace(settimeout=lambda seconds: None)
        def __init__(self, host, **kwargs):
            assert host == "api.telegram.org"
        def connect(self):
            pass
        def putrequest(self, method, url):
            assert method == "POST" and url.endswith("/sendVideo")
        def putheader(self, key, value):
            if key == "Content-Length":
                self.length = int(value)
        def endheaders(self, data):
            sent.append(data)
        def send(self, data):
            sent.append(data)
        def getresponse(self):
            assert sum(map(len, sent)) == self.length
            raise TimeoutError(TOKEN)
        def close(self):
            pass
    monkeypatch.setattr(telegram_api.http.client, "HTTPSConnection", Connection)
    with pytest.raises(TelegramError) as error:
        telegram_api.Client(TOKEN).call("sendVideo", {"chat_id": OWNER, "supports_streaming": True}, video=clip)
    assert error.value.uncertain and not error.value.retry and TOKEN not in str(error.value)
    assert b"video fixture contents" in sent


def test_preferences_quiet_across_midnight_and_source_validation():
    prefs = normalize({**DEFAULTS, "quiet_start": "23:00", "quiet_end": "06:30"})
    for hour, expected in [(3, True), (12, False), (23, True)]:
        assert gateway.quiet(prefs, "Etc/UTC", datetime(2026, 11, 23, hour)) is expected
    for unsafe in ("https://www.youtube.com.evil.test/watch?v=2dt0yxm4RUE",
                   "https://owner@www.youtube.com/watch?v=2dt0yxm4RUE",
                   "https://www.youtube.com:443/watch?v=2dt0yxm4RUE",
                   "https://127.0.0.1/a"):
        with pytest.raises(ValueError):
            capture_source(unsafe)
    with pytest.raises(ValueError):
        normalize({"minimum_score": 5.9})


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg and FFprobe required")
def test_large_video_copy_real_ffmpeg_preserves_original(tmp_path):
    source = tmp_path / "original.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "5", "-c:v", "libx264",
                    "-preset", "ultrafast", "-crf", "0", "-c:a", "aac", "-threads", "1", str(source)], check=True)
    checksum = hashlib.sha256(source.read_bytes()).digest()
    duration, _ = subtitle_quality.probe_media(source)
    limit = 240_000
    assert source.stat().st_size > limit
    with gateway.upload_copy(source, tmp_path, duration, limit=limit) as preview:
        assert preview != source and preview.stat().st_size < limit
        assert subtitle_quality.probe_media(preview)[0] == pytest.approx(duration, abs=.15)
    assert not preview.exists()
    assert hashlib.sha256(source.read_bytes()).digest() == checksum


def test_studio_http_panel_never_returns_bot_token(server, chromium_page, tmp_path, monkeypatch):
    web, base = server
    page, expect = chromium_page
    remote = gateway.Gateway(web.app, installation(tmp_path), lambda _: Bot())
    monkeypatch.setattr(remote, "start", lambda: None)
    web.app.telegram = remote
    with pytest.raises(urllib.error.HTTPError) as forbidden:
        request(base, "/api/telegram/pair", {})
    assert forbidden.value.code == 403
    page.goto(base)
    page.locator("#show-settings").click()
    page.locator("[data-setting=integrations]").click()
    expect(page.get_by_role("heading", name="Telegram Control Center")).to_be_visible()
    token_field = page.locator("#telegram-token")
    expect(token_field).to_have_attribute("type", "password")
    token_field.fill(TOKEN)
    page.locator("#telegram-enabled").check()
    page.get_by_role("button", name="Guardar ajustes").click()
    expect(page.locator("#telegram-state")).to_contain_text("Token protegido")
    expect(token_field).to_have_value("")
    assert TOKEN not in json.dumps(request(base, "/api/telegram/status"))
    page.get_by_role("button", name="Generar código de vinculación").click()
    expect(page.locator("#telegram-pair-code")).to_contain_text("/pair ")
