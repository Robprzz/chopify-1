"""Rolling signal bus and hype scoring for live sessions."""
from __future__ import annotations

import math
import re
import socket
import statistics
import tempfile
import threading
import time
from codecs import getincrementaldecoder
from collections import defaultdict, deque
from contextlib import nullcontext
from pathlib import Path

import numpy as np

import download_and_transcribe as dt
import evidence
from live_capture import SegmentError, decode_segment
import score_clips
import visual

DEFAULT_WEIGHTS = {"audio": 0.30, "visual": 0.25,
                   "transcript": 0.25, "chat": 0.20}
SIGNALS = tuple(DEFAULT_WEIGHTS)
# Minimum dynamic range used as the denominator when a signal is steady.
SCALES = {"audio": 0.05, "visual": 0.05, "transcript": 1.0, "chat": 2.0}

# Best-effort ad-slate detection: flat fields with strong magenta/cyan tint.
AD_MIN_SATURATION = 0.35
AD_MAX_TEXTURE = 12.0


def looks_like_ad_slate(frame):
    """Best-effort check for a flat, saturated Twitch ad slate."""
    import cv2
    image = np.asarray(frame, dtype=np.uint8)
    if image.ndim != 3 or image.shape[2] != 3:
        return False
    small = cv2.resize(image, (80, 45), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    texture = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV)
    saturation = float(np.mean(hsv[:, :, 1])) / 255.0
    return texture < AD_MAX_TEXTURE and saturation > AD_MIN_SATURATION


class SignalBus:
    """Thread-safe, time-tagged samples with bounded retention."""

    def __init__(self, retention=300.0):
        self._lock = threading.Lock()
        self._samples = defaultdict(lambda: deque(maxlen=4096))
        self.retention = float(retention)
        self._revision = 0

    def add(self, name, t, value):
        if name not in SIGNALS:
            raise ValueError(f"Unknown signal: {name}")
        if not math.isfinite(float(t)) or not math.isfinite(float(value)):
            return
        with self._lock:
            self._samples[name].append((float(t), float(value)))
            self._revision += 1
            cutoff = float(t) - self.retention
            queue = self._samples[name]
            while queue and queue[0][0] <= cutoff:
                queue.popleft()

    def prune(self, now, retention=None):
        cutoff = float(now) - float(retention if retention is not None else self.retention)
        with self._lock:
            for queue in self._samples.values():
                while queue and queue[0][0] <= cutoff:
                    queue.popleft()

    def values(self, name, t, window):
        with self._lock:
            return [value for stamp, value in self._samples.get(name, ())
                    if t - float(window) < stamp <= t + 1e-9]

    def recent_times(self, t, window, previous_revision):
        with self._lock:
            if self._revision == previous_revision:
                return self._revision, []
            times = {stamp for queue in self._samples.values() for stamp, _ in queue if t - window <= stamp <= t}
            return self._revision, sorted(times)[-36:]

    def available(self, t, window):
        return {name for name in SIGNALS if self.values(name, t, window)}


def normalize(value, baseline, scale):
    """Map a raw signal onto 0-10 relative to its rolling baseline."""
    if not math.isfinite(scale) or scale <= 0:
        return 0.0
    return max(0.0, min(10.0, 10.0 * (float(value) - float(baseline)) / float(scale)))


class HypeDetector:
    """Combine available signals into a single 0-10 score."""

    def __init__(self, weights=None):
        self.weights = dict(weights or DEFAULT_WEIGHTS)

    def evaluate(self, bus, t, freshness=None):
        signals = {}
        for name in SIGNALS:
            history = bus.values(name, t, 60.0)
            recent = bus.values(name, t, freshness) if freshness is not None else history
            if len(history) < 2 or not recent:
                continue
            baseline = statistics.median(history[:-1])
            scale = max(SCALES[name], max(history) - baseline)
            signals[name] = round(normalize(recent[-1], baseline, scale), 4)
        if not signals:
            return {"score": 0.0, "signals": {}}
        total_weight = sum(self.weights[name] for name in signals)
        score = sum(self.weights[name] * value
                    for name, value in signals.items()) / total_weight
        return {"score": round(score, 4), "signals": signals}


CHAT_WORDS = ("let's go", "no way", "insane", "clutch", "hype", "w", "gg", "crazy")


def transcript_score(words):
    """Return 0-10 from hook/reaction cues in a rolling word window."""
    if not words:
        return 0.0
    text = " ".join(str(w.get("word", "")) for w in words).lower()
    hits = 0
    for cues in (score_clips.HOOK_CUES, score_clips.SHOCK_CUES,
                 score_clips.REACTION_PHRASES, score_clips.GAMEPLAY_CUES):
        hits += sum(1 for cue in cues if cue in text)
    return normalize(min(3.0, float(hits)), 0.0, 3.0)


class BaseAnalyzer:
    name = "base"
    delay = 1.0

    def __init__(self, buffer, bus):
        self.buffer = buffer
        self.bus = bus
        self.error = None
        self.available = True
        self.last_index = -1
        self.processed = self.failed = self.retries = self.dropped = 0
        self.last_error = None
        self.through = None
        self._bounds = None
        self._lock = threading.Lock()
        self._stop = None

    def status(self, now):
        with self._lock:
            return {"state": ("disabled" if not self.available else "degraded" if self.error else
                              "waiting" if self.through is None else "ok"),
                    "processed": self.processed, "errors": self.failed, "retries": self.retries,
                    "dropped": self.dropped, "last_index": self.last_index, "last_error": self.last_error,
                    "lag_seconds": round(max(0, now - self.through), 2) if self.through is not None else None}

    def _new_segments(self):
        ready = getattr(self.buffer, "ready_segments", None)
        segments = ready() if callable(ready) else self.buffer.segments()
        return [p for p in segments if self.buffer.index_of(p) > self.last_index]

    def _decode(self, path):
        raise NotImplementedError

    def _publish(self, index, payload):
        raise NotImplementedError

    def run(self, stop_event):
        self._stop = stop_event
        while not stop_event.is_set():
            try:
                paths = self._new_segments()
            except OSError:
                paths = []
            # Keep up with the live edge. Each analyzer advances independently.
            with self._lock:
                self.dropped += max(0, len(paths) - 24)
            for path in paths[-24:]:
                if stop_event.is_set():
                    return
                index = self.buffer.index_of(path)
                fallback = self.buffer.index_to_time(index)
                pin = getattr(self.buffer, "pin", None)
                with (pin([path]) if pin else nullcontext({path: (fallback, fallback + 4)})) as selected:
                    with self._lock:
                        self.last_index = index
                        if not selected:
                            self.dropped += 1
                    if not selected:
                        continue
                    self._bounds = selected[path]
                    for attempt in range(2):
                        if stop_event.is_set():
                            return
                        try:
                            payload = self._decode(path)
                            if stop_event.is_set():
                                return
                            with self._lock:
                                self._publish(index, payload)
                                self.through = self._bounds[1]
                                self.processed += 1
                                self.error = None
                            break
                        except Exception as exc:
                            # Never include shell commands, URLs, or model exception text.
                            detail = getattr(exc, "detail", {"stage": self.name,
                                             "message": f"Fallo de análisis ({type(exc).__name__})."})
                            with self._lock:
                                self.last_error = {**detail, "segment": path.name}
                                self.error = detail["message"]
                                if attempt:
                                    self.failed += 1
                                else:
                                    self.retries += 1
            stop_event.wait(self.delay)


class AudioAnalyzer(BaseAnalyzer):
    name = "audio"
    delay = 0.5

    def _decode(self, path):
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-threads", "1", "-protocol_whitelist", "file,pipe",
               "-i", str(path), "-map", "0:a:0", "-t", "30",
               "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"]
        raw = decode_segment(cmd, self.name)
        if not raw:
            raise SegmentError(self.name, "La pista de audio no produjo muestras.")
        return np.frombuffer(raw, dtype=np.float32).copy()

    def _publish(self, index, samples):
        rows = evidence.features_from_samples(samples)
        if rows is None:
            return
        values = getattr(rows, "rms", None)
        if values is None:
            values = np.asarray(rows).reshape(-1)
        for i, value in enumerate(values):
            t = self._bounds[0] + i
            self.bus.add("audio", t, float(value))


class VisualAnalyzer(BaseAnalyzer):
    name = "visual"
    delay = 0.5

    def __init__(self, buffer, bus):
        super().__init__(buffer, bus)
        self.ad_like = False

    def _mark_ad(self, frame):
        if looks_like_ad_slate(frame):
            self.ad_like = True

    def _decode(self, path):
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-threads", "1", "-protocol_whitelist", "file,pipe",
               "-i", str(path), "-map", "0:v:0", "-t", "30", "-threads", "1", "-vf",
               "fps=2,scale=160:90", "-pix_fmt", "rgb24",
               "-f", "rawvideo", "pipe:1"]
        raw = decode_segment(cmd, self.name)
        frame_size = 160 * 90 * 3
        count = len(raw) // frame_size
        if not count:
            raise SegmentError(self.name, "La pista de video no produjo fotogramas.")
        return [np.frombuffer(raw[i * frame_size:(i + 1) * frame_size],
                              dtype=np.uint8).reshape(90, 160, 3)
                for i in range(count)]

    def _publish(self, index, frames):
        if not frames:
            return
        self.ad_like = False
        for frame in frames:
            self._mark_ad(frame)
        features = visual.features_from_frames(frames)
        for i, value in enumerate(features.motion):
            t = self._bounds[0] + i / 2.0
            self.bus.add("visual", t, float(value))


LIVE_TRANSCRIPT_MODEL = "small"


class TranscriptAnalyzer(BaseAnalyzer):
    name = "transcript"
    delay = 1.0

    def __init__(self, buffer, bus, model=None, device="cpu", enabled=True, shared=None):
        super().__init__(buffer, bus)
        self.shared = shared or {"lock": threading.Lock(), "model": model}
        self.device = device
        self.enabled = enabled
        if not enabled:
            self.available = False
        self.words = []
        self.performance = None

    def status(self, now):
        return {**super().status(now), "performance": self.performance}

    def _decode(self, path):
        start, end = self._bounds
        with tempfile.TemporaryDirectory() as directory:
            wav = Path(directory) / "transcript.wav"
            decode_segment(["ffmpeg", "-nostdin", "-y", "-v", "error", "-xerror", "-threads", "1", "-protocol_whitelist", "file,pipe",
                            "-i", str(path), "-map", "0:a:0", "-t", "30", "-ac", "1",
                            "-ar", "16000", str(wav)], self.name)
            lock = self.shared["lock"]
            while not lock.acquire(timeout=.2):
                if self._stop and self._stop.is_set():
                    return [], start, end, None
            try:
                if self._stop and self._stop.is_set():
                    return [], start, end, None
                resources = self.shared.get("resources")
                owns_slot = self.device != "cpu" and resources is not None and resources.acquire(blocking=False)
                try:
                    # A busy production/editorial slot should not stop live speech analysis.
                    busy = self.device != "cpu" and resources is not None and not owns_slot
                    mode = "cpu" if busy else self.device
                    runtimes = self.shared.setdefault("runtimes", {})
                    if mode not in runtimes:
                        shared = self.shared

                        def load(name, device, compute):
                            if device == "cuda":
                                return dt._load_model(name, device, compute)
                            if shared["model"] is None:
                                shared["model"] = dt._load_model(name, device, compute)
                            return shared["model"]

                        runtimes[mode] = dt.Session(LIVE_TRANSCRIPT_MODEL, mode, "default", load)
                    runtime = runtimes[mode]
                    try:
                        (_, words, _, _), performance = runtime.run(
                            lambda model: dt._transcribe_media(model, wav, offset=start))
                    finally:
                        # Do not retain CUDA weights across segments while others need VRAM.
                        if runtime.device == "cuda":
                            runtime.model = None
                    if busy:
                        performance.update(requested=self.device, fallback="GPU ocupada por otro trabajo local. Este segmento usó CPU.")
                    performance["rtf"] = round(performance["inference_seconds"] / max(.001, end - start), 4)
                finally:
                    if owns_slot:
                        resources.release()
            finally:
                lock.release()
        return words, start, end, performance

    def snapshot(self):
        with self._lock:
            return self.through, list(self.words)

    def _publish(self, index, payload):
        words, start, end, self.performance = payload
        self.words.extend(words)
        self.words = [w for w in self.words if w["end"] >= start - max(120., self.buffer.retention)][-6000:]
        for i in range(math.ceil(end - start)):
            t = start + i
            window = [w for w in self.words if t - 10.0 <= w["start"] <= t]
            if window:
                self.bus.add("transcript", t, transcript_score(window))
        self.through = end

    def run(self, stop_event):
        if not self.enabled:
            return
        super().run(stop_event)


class ChatAnalyzer(BaseAnalyzer):
    name = "chat"
    delay = 1.0

    def __init__(self, buffer, bus, platform="generic", channel="", enabled=True):
        super().__init__(buffer, bus)
        self.platform = platform
        self.channel = channel
        self.enabled = enabled
        self.counts = []
        if not enabled or platform != "twitch" or not channel:
            self.available = False

    def _decode(self, path):
        return self._read_twitch() if self.platform == "twitch" else []

    def _read_twitch(self):
        import ssl
        context = ssl.create_default_context()
        with context.wrap_socket(socket.create_connection(
                ("irc.chat.twitch.tv", 6697), timeout=10), server_hostname="irc.chat.twitch.tv") as sock:
            sock.settimeout(10)
            sock.sendall(b"NICK justinfan12345\r\nUSER justinfan12345 0 * :justinfan12345\r\n")
            sock.sendall(f"JOIN #{self.channel}\r\n".encode())
            sock.settimeout(2)
            messages = 0
            decoder = getincrementaldecoder("utf-8")(errors="backslashreplace")
            deadline = time.time() + 2.0
            while time.time() < deadline:
                try:
                    chunk = sock.recv(4096)
                except (socket.timeout, OSError):
                    break
                if not chunk:
                    break
                line = decoder.decode(chunk)
                messages += len(re.findall(r"PRIVMSG", line))
                for word in CHAT_WORDS:
                    messages += line.lower().count(word)
            return [float(messages)]

    def _publish(self, index, payload):
        t = self._bounds[0]
        for value in payload:
            self.bus.add("chat", t, value)

    def run(self, stop_event):
        if not self.available:
            return
        try:
            super().run(stop_event)
        except OSError as exc:
            self.error = f"Chat unavailable: {exc}"
            self.available = False


def start_analyzers(buffer, bus, options, platform, channel, model=None, shared=None):
    """Start analyzer threads; return (thread, analyzer) pairs."""
    pairs = []
    specs = [
        (AudioAnalyzer(buffer, bus), {}),
        (VisualAnalyzer(buffer, bus), {}),
        (TranscriptAnalyzer(buffer, bus, model, options.get("device", "cpu"),
                            not options.get("live_no_transcript", False), shared=shared), {}),
        (ChatAnalyzer(buffer, bus, platform, channel,
                      not options.get("live_no_chat", False)), {}),
    ]
    for analyzer, kwargs in specs:
        thread = threading.Thread(target=analyzer.run,
                                  args=(buffer._stop,), kwargs=kwargs,
                                  name=f"live-{analyzer.name}", daemon=True)
        thread.start()
        pairs.append((thread, analyzer))
    return pairs
