"""Resolve live URLs and keep a bounded rolling buffer of HLS segments."""
from __future__ import annotations

from contextlib import contextmanager
import csv
from collections import deque
import json
import math
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import ingest

SEGMENT_SECONDS = 4
MAX_AV_SKEW = 0.35
DEFAULT_MAX_HEIGHT = 720
SYNC_ERROR = "Live synchronization failed: the captured audio and video timestamps do not align."
MISSING_TRACK = "Live capture failed: a completed segment is missing audio or video."
HEIGHT_ERROR = "The stream delivers video above the selected maximum resolution."
SCALING_WARNING = "The stream exceeds the selected maximum. FFmpeg is scaling the captured video to that limit."
NETWORK_ERROR = "Live capture lost access to a media input. Check the connection and whether the stream is still playable."
CAPTURE_ERROR = "FFmpeg could not capture playable live video. See buffer/ffmpeg.log for the capture category."
JS_WARNING = "yt-dlp found no supported JavaScript runtime. Install Deno with yt-dlp's EJS dependencies if YouTube formats are missing."
DIRECT_SUFFIXES = (".m3u8", ".ts")
DIRECT_SCHEMES = {"rtmp", "rtmps", "rtmp+https"}
PLATFORMS = (
    ("twitch.tv", "twitch"), ("youtube.com", "youtube"), ("youtu.be", "youtube"),
    ("kick.com", "kick"),
)


def safe_stderr(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    lines = []
    for line in (value or "").splitlines()[-8:]:
        if re.search(r"(?i)authorization|cookie|bearer|token|signature|password|secret|api.key", line):
            continue
        line = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s'\"]+", "[URL]", line)
        lines.append(line[-240:])
    return " ".join(lines)[-1200:]


class SegmentError(RuntimeError):
    def __init__(self, stage, message, code=None, stderr="", timeout=None):
        super().__init__(message)
        self.detail = {"stage": stage, "message": message, "exit_code": code,
                       "stderr": safe_stderr(stderr), "timeout_seconds": timeout}


def decode_segment(command, stage, timeout=12):
    try:
        return subprocess.run(command, capture_output=True, check=True, timeout=timeout).stdout
    except subprocess.TimeoutExpired as exc:
        raise SegmentError(stage, "FFmpeg agotó el tiempo de decodificación.", stderr=exc.stderr,
                           timeout=timeout) from None
    except subprocess.CalledProcessError as exc:
        code = exc.returncode
        if 2**31 <= code < 2**32:
            code -= 2**32  # Windows exposes signed FFmpeg exits as DWORDs.
        raise SegmentError(stage, "FFmpeg no pudo decodificar la pista solicitada.", code, exc.stderr) from None
    except OSError:
        raise SegmentError(stage, "No se pudo iniciar FFmpeg.") from None


@dataclass
class LiveSource:
    url: str = field(repr=False)
    kind: str
    platform: str
    resolved_by: str
    headers: list[str] = field(repr=False)
    page_url: str | None = field(default=None, repr=False)
    audio_url: str | None = field(default=None, repr=False)
    audio_headers: list[str] = field(default_factory=list, repr=False)
    warning: str | None = None

    def refresh(self, options, runner=None):
        """Re-resolve a page URL; a direct manifest cannot be refreshed."""
        if not self.page_url:
            return self
        return resolve_live_source(self.page_url, options, runner)


def _platform(url):
    host = (urlparse(url).hostname or "").lower()
    for needle, name in PLATFORMS:
        if host == needle or host.endswith("." + needle):
            return name
    return "generic"


def check_capture_tools():
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        raise ValueError(f"Live clipping requires {', '.join(missing)} on PATH. Install it and restart Studio.")


def _resolver_error(stderr, platform):
    message = (stderr or "").lower()
    if "no module named yt_dlp" in message:
        return "yt-dlp is missing from Studio's Python environment. Install the project requirements with that Python."
    if any(term in message for term in ("not currently live", "offline", "live event will begin")):
        return f"The {platform} channel or event is offline or has not started."
    if any(term in message for term in ("403", "401", "sign in", "private", "members-only", "login")):
        return f"Access to the {platform} stream was denied. Check that it is publicly playable from this network."
    if _missing_js_runtime(message):
        return JS_WARNING
    if "requested format is not available" in message:
        return f"No playable {platform} audio and video formats match the selected live resolution."
    if any(term in message for term in ("connection", "timed out", "unable to download", "network", "proxy", "certificate")):
        return f"Could not connect to the {platform} stream. Check the network and retry."
    return f"Could not resolve the {platform} stream. Check the URL, network, and yt-dlp version."


def _missing_js_runtime(message):
    return "no supported javascript runtime" in message.lower()


def _format_selector(height):
    limit = f"[height<={height}]"
    hls = "[protocol^=m3u8]"
    return f"best{hls}{limit}/bestvideo{hls}{limit}+bestaudio{hls}/best{limit}/bestvideo{limit}+bestaudio"


def _media_url(value):
    if isinstance(value, str) and not re.search(r"[\s\x00-\x1f\x7f]", value):
        try:
            parsed = urlparse(value)
            if parsed.scheme in ("http", "https") and parsed.hostname and parsed.username is None:
                return value
        except ValueError:
            pass
    raise ValueError("yt-dlp did not return a valid HTTP media URL for the selected live format.")


def _media_headers(info, overrides):
    names = {line.split(":", 1)[0].strip().lower() for line in overrides}
    headers = info.get("http_headers")
    if not isinstance(headers, dict):
        return list(overrides)
    return [*overrides, *(f"{name}: {value}" for name, value in headers.items()
                         if name.lower() not in names and re.fullmatch(r"[a-zA-Z0-9-]+", name)
                         and isinstance(value, str) and not re.search(r"[\x00-\x08\x0a-\x1f\x7f]", value))]


def _codec_present(value):
    if not isinstance(value, str) or value.strip().lower() in {"", "unknown", "n/a", "und", "null"}:
        return None
    return value.strip().lower() != "none"


def _track_kind(track, headers, runner):
    video = _codec_present(track.get("vcodec"))
    audio = _codec_present(track.get("acodec"))
    if video is None or audio is None:
        url = _media_url(track.get("url"))
        cmd = ["ffprobe", "-v", "error", "-rw_timeout", "5000000", "-probesize", "2000000",
               "-analyzeduration", "3000000", "-protocol_whitelist", "http,https,tcp,tls,crypto"]
        input_headers = _media_headers(track, headers)
        if input_headers:
            cmd += ["-headers", "".join(line + "\r\n" for line in input_headers)]
        cmd += ["-show_entries", "stream=codec_type:stream_disposition=attached_pic", "-of", "json", "-i", url]
        try:
            streams = json.loads(runner(cmd, timeout=10).stdout)["streams"]
            kinds = {stream["codec_type"] for stream in streams
                     if not stream.get("disposition", {}).get("attached_pic")}
        except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
            raise ValueError("FFprobe could not verify the selected live track. Check that the stream is still live and retry.") from None
        measured = ("video" in kinds, "audio" in kinds)
        if any(known is not None and known != actual for known, actual in zip((video, audio), measured)):
            raise ValueError("FFprobe found a live track that conflicts with the selected format.")
        video, audio = measured
    return {(True, False): "video", (False, True): "audio",
            (True, True): "combined", (False, False): "invalid"}[video, audio]


def resolve_live_source(url, options, runner=None):
    """Return a LiveSource; direct manifests pass through, pages use yt-dlp."""
    if not isinstance(url, str) or not url.strip() or len(url) > 2000:
        raise ValueError("Provide a live stream URL of at most 2000 characters.")
    url = url.strip()
    headers = list(options.get("live_headers") or [])
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError("Live source must be an http(s), HLS or RTMP URL with a host.")
    if (parsed.scheme in DIRECT_SCHEMES
            or (parsed.scheme in {"http", "https"} and parsed.path.lower().endswith(DIRECT_SUFFIXES))):
        return LiveSource(url, "rtmp" if parsed.scheme in DIRECT_SCHEMES else "hls",
                          _platform(url), "passthrough", headers, None)
    if not ingest.is_url(url):
        raise ValueError("Live source must be an http(s), HLS or RTMP URL.")
    platform = {"youtube": "YouTube", "twitch": "Twitch", "kick": "Kick"}.get(_platform(url), "linked")
    runner = runner or ingest._run
    height = options.get("live_max_height", DEFAULT_MAX_HEIGHT)
    if height not in (720, 1080):
        raise ValueError("Live capture resolution must be 720p or 1080p.")
    cmd = [sys.executable, "-m", "yt_dlp", "--dump-single-json", "--no-playlist",
           "--socket-timeout", "10", "-f", _format_selector(height), "--", url]
    try:
        result = runner(cmd, timeout=30)
    except subprocess.TimeoutExpired:
        raise ValueError(f"Timed out resolving the {platform} stream. Check your connection and retry.") from None
    except subprocess.CalledProcessError as exc:
        raise ValueError(_resolver_error(exc.stderr, platform)) from None
    except OSError:
        raise ValueError("Could not launch yt-dlp with Studio's Python.") from None
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError:
        raise ValueError("yt-dlp returned an unreadable stream response.") from None
    if not isinstance(data, dict):
        raise ValueError("yt-dlp returned an unreadable stream response.")
    if data.get("is_live") is False or data.get("live_status") in {"is_upcoming", "was_live", "post_live", "not_live"}:
        raise ValueError(f"The {platform} link is not currently live. Open an active broadcast.")
    warning = JS_WARNING if _missing_js_runtime(getattr(result, "stderr", "") or "") else None
    video, audio = data, None
    tracks = data.get("requested_formats")
    if tracks is not None:
        if not isinstance(tracks, list) or len(tracks) != 2 or not all(isinstance(track, dict) for track in tracks):
            raise ValueError("yt-dlp returned an unsupported set of live formats. Expected one video track and one audio track.")
        classified = {_track_kind(track, headers, runner): track for track in tracks}
        if classified.keys() != {"audio", "video"}:
            raise ValueError("yt-dlp returned an unsupported set of live formats. Expected one video track and one audio track.")
        video, audio = classified["video"], classified["audio"]
    resolved = _media_url(video.get("url"))
    protocol = str(video.get("protocol") or "").lower()
    kind = "hls" if "m3u8" in resolved or "hls" in protocol else "http"
    return LiveSource(resolved, kind, _platform(url), "yt-dlp", _media_headers(video, headers), url,
                      _media_url(audio.get("url")) if audio else None,
                      _media_headers(audio, headers) if audio else [], warning)


class SegmentBuffer:
    """ffmpeg pulls once; analyzers read the resulting 4-second segments."""

    def __init__(self, directory, url, options, runner=None, headers=None, *,
                 audio_url=None, audio_headers=(), scale_capture=False):
        self.directory = Path(directory)
        self.url = url
        self.options = options
        self.runner = runner or ingest._run
        self.retention = float(options.get("live_buffer", 300))
        self.max_bytes = int(options.get("live_buffer_max_mb", 512)) * 1024 * 1024
        self.headers = list(options.get("live_headers") or []) if headers is None else headers
        self.audio_url = audio_url
        self.audio_headers = audio_headers
        self.max_height = options.get("live_max_height")
        self.scale_capture = scale_capture
        self.capture_problem = None
        self.cleanup_problem = None
        self.segment_problem = None
        self._validation_lock = threading.RLock()
        self._pins = {}
        self._retired = False
        self._bad_streak = 0
        self.last_closed_at = 0.0
        self.last_closed_index = -1
        self.last_segment_error = None
        self.rejected_segments = 0
        self.stderr_tail = deque(maxlen=4)
        self._probe_cache = None
        self._validated = set()
        self._skipped = set()
        self._has_valid_segment = False
        self.process = None
        self.started_at = 0.0
        self.started_monotonic = 0.0
        self._stop = threading.Event()
        self._cleaner = None
        self._diagnostics = None

    @staticmethod
    def _input(url, headers):
        cmd = ["-rw_timeout", "15000000"]
        if ingest.is_url(url):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
            if headers:
                cmd += ["-headers", "".join(h + "\r\n" for h in headers)]
        return [*cmd, "-i", url]

    def ffmpeg_command(self):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
        if self.audio_url:
            # Separate renditions share the broadcaster's clock. Preserve their
            # relative starts even when one HLS playlist is a segment ahead.
            cmd += ["-copyts", "-start_at_zero"]
        cmd += self._input(self.url, self.headers)
        if self.audio_url:
            cmd += ["-isync", "0", *self._input(self.audio_url, self.audio_headers),
                    "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
        else:
            cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
        if self.scale_capture:
            cmd += ["-vf", f"scale=-2:min(ih\\,{self.max_height})", "-c:v", "libx264",
                    "-preset", "veryfast", "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SECONDS})",
                    "-c:a", "copy"]
        else:
            cmd += ["-c", "copy"]
        cmd += ["-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
                "-segment_list", str(self.directory / "segments.csv"), "-segment_list_type", "csv",
                "-segment_list_size", str(int(self.retention) + 2),
                "-reset_timestamps", "1", str(self.directory / "segment%08d.ts")]
        return cmd

    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.started_at = time.time()
        self.started_monotonic = time.monotonic()
        self.log_path = self.directory / "ffmpeg.log"
        self.process = subprocess.Popen(self.ffmpeg_command(), stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE)
        self._diagnostics = threading.Thread(target=self._read_diagnostics, daemon=True)
        self._diagnostics.start()
        self._cleaner = threading.Thread(target=self._clean_loop, daemon=True)
        self._cleaner.start()
        return self

    def _read_diagnostics(self):
        with self.process.stderr as stderr:
            while line := stderr.readline(8192):
                message = line.lower()
                summary = safe_stderr(line)
                if summary:
                    with self._validation_lock:
                        self.stderr_tail.append(summary)
                if any(word in message for word in (b"http error", b"server returned", b"connection", b"timed out", b"resolve hostname")):
                    self.capture_problem = NETWORK_ERROR
                elif b"matches no streams" in message:
                    self.capture_problem = MISSING_TRACK
                elif not self.capture_problem:
                    self.capture_problem = CAPTURE_ERROR
        if self.capture_problem:
            try:
                with self.log_path.open("a", encoding="utf-8") as log:
                    log.write(self.capture_problem + "\n")
            except OSError:
                pass

    def _clean_loop(self):
        while not self._stop.wait(5.0):
            try:
                self.cleanup()
                self.cleanup_problem = None
                with self._validation_lock:
                    files = self.segments()
                    if files and files[-1].stat().st_size > self.max_bytes and self.alive:
                        self.segment_problem = "El segmento abierto excedió el límite del búfer."
                        ingest._kill_tree(self.process)
            except OSError:
                self.cleanup_problem = "No se pudo limpiar el búfer; verifica permisos y archivos en uso."

    @property
    def alive(self):
        return self.process is not None and self.process.poll() is None

    def stop(self):
        self._stop.set()
        if self.process is not None and self.alive:
            ingest._kill_tree(self.process)
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.capture_problem = "No se pudo detener el proceso de captura en 4 s."
        if self._cleaner is not None:
            self._cleaner.join(timeout=2.0)
        if self._diagnostics is not None:
            self._diagnostics.join(timeout=2.0)

    def segments(self):
        if not self.directory.exists():
            return []
        return sorted(p for p in self.directory.glob("segment[0-9]*.ts")
                      if re.fullmatch(r"segment\d{8}\.ts", p.name))

    def _closed(self):
        paths = self.segments()
        if self.process is None:  # Inactive fixture/offline buffers.
            return {path: (self.index_to_time(self.index_of(path)),
                           self.index_to_time(self.index_of(path) + 1)) for path in paths[:-1]}
        try:
            with (self.directory / "segments.csv").open(encoding="utf-8", newline="") as handle:
                complete = [line for line in handle if line.endswith("\n")]
            ranges = {}
            for row in csv.reader(complete):
                if len(row) == 3:
                    start, end = map(float, row[1:])
                    if math.isfinite(start) and math.isfinite(end) and start < end:
                        ranges[row[0]] = (start, end)
            return {path: ranges[path.name] for path in paths if path.name in ranges}
        except (OSError, ValueError, csv.Error):
            return {}

    def ready_segments(self):
        return list(self.closed_ranges())

    @contextmanager
    def pin(self, paths=None):
        """Keep an immutable, closed snapshot alive while a consumer uses it."""
        with self._validation_lock:
            if self._retired:
                selected = {}
            elif paths is None:
                selected = self.closed_ranges()
            else:
                requested = set(paths)
                selected = {path: bounds for path, bounds in self.closed_ranges().items() if path in requested}
            for path in selected:
                self._pins[path] = self._pins.get(path, 0) + 1
        try:
            yield selected
        finally:
            with self._validation_lock:
                for path in selected:
                    self._pins[path] -= 1
                    if not self._pins[path]:
                        del self._pins[path]
                        if self._retired:
                            path.unlink(missing_ok=True)
                if self._retired and not self._pins:
                    self._retired_files()

    def _retired_files(self):
        (self.directory / "segments.csv").unlink(missing_ok=True)
        if re.fullmatch(r"capture-\d{6,}", self.directory.name):
            (self.directory / "ffmpeg.log").unlink(missing_ok=True)
            try:
                self.directory.rmdir()
            except OSError:
                pass

    def retire(self):
        """Called after capture has stopped; pinned files belong to their readers."""
        with self._validation_lock:
            self._retired = True
            for path in self.segments():
                if path not in self._pins:
                    path.unlink(missing_ok=True)
            if not self._pins:
                self._retired_files()

    def _check_segment(self, path):
        try:
            result = self.runner(["ffprobe", "-v", "error", "-protocol_whitelist", "file",
                                  "-f", "mpegts", "-show_entries", "stream=codec_type,codec_name,height,start_time,duration", "-of", "json", str(path)], timeout=5)
            tracks = {stream["codec_type"]: stream for stream in json.loads(result.stdout)["streams"]}
            if "video" not in tracks:
                return MISSING_TRACK if self.audio_url else CAPTURE_ERROR
            if self.max_height and int(tracks["video"]["height"]) > self.max_height:
                return HEIGHT_ERROR
            if self.process is not None and "audio" not in tracks and self.audio_url is None:
                return MISSING_TRACK
            if not self.audio_url:
                return None
            if not {"audio", "video"} <= tracks.keys():
                return MISSING_TRACK
            starts = [float(tracks[kind]["start_time"]) for kind in ("video", "audio")]
            ends = [start + float(tracks[kind]["duration"]) for start, kind in zip(starts, ("video", "audio"))]
            if (not all(map(math.isfinite, starts + ends))
                    or any(end <= start for start, end in zip(starts, ends))
                    or max(abs(starts[0] - starts[1]), abs(ends[0] - ends[1])) > MAX_AV_SKEW):
                return SYNC_ERROR
        except subprocess.TimeoutExpired as exc:
            self._probe_detail = {"exit_code": None, "timeout_seconds": 5, "stderr": safe_stderr(exc.stderr)}
            return "FFprobe agotó el tiempo al verificar las pistas."
        except subprocess.CalledProcessError as exc:
            self._probe_detail = {"exit_code": exc.returncode, "stderr": safe_stderr(exc.stderr)}
            return "FFprobe no pudo leer las pistas del segmento."
        except (OSError, ValueError, TypeError, KeyError):
            return SYNC_ERROR if self.audio_url else CAPTURE_ERROR
        return None

    @staticmethod
    def index_of(path):
        return int(Path(path).stem.split("segment")[1])

    @staticmethod
    def index_to_time(index):
        return float(index) * SEGMENT_SECONDS

    @staticmethod
    def time_to_index(t):
        return max(0, int(float(t) // SEGMENT_SECONDS))

    def window(self, start, end):
        """Closed segments whose capture times overlap [start, end)."""
        return [path for path, (left, right) in self.closed_ranges().items()
                if left < end and right > start]

    def closed_ranges(self):
        with self._validation_lock:
            if self._retired:
                return {}
            closed = self._closed()
            if closed:
                newest = next(reversed(closed))
                index = self.index_of(newest)
                if index > self.last_closed_index:
                    self.last_closed_index = index
                    self.last_closed_at = time.monotonic()
            if self.process is None and not self.audio_url and not self.max_height:
                return closed
            self._validated.intersection_update(closed)
            self._skipped.intersection_update(closed)
            for path, (start, end) in closed.items():
                if self.segment_problem:
                    break
                if path in self._validated or path in self._skipped:
                    continue
                self._probe_detail = {}
                problem = ("Segmento cerrado de más de 30 s; revisa el intervalo de fotogramas clave."
                           if end - start > 30 else self._check_segment(path))
                if not problem:
                    self._validated.add(path)
                    self._has_valid_segment = True
                    self._bad_streak = 0
                    continue
                self._skipped.add(path)
                self.rejected_segments += 1
                self._bad_streak += 1
                self.last_segment_error = {"segment": path.name, "message": problem, **self._probe_detail}
                if problem == HEIGHT_ERROR or self._bad_streak >= 3:
                    self.segment_problem = problem
            return {path: bounds for path, bounds in closed.items() if path in self._validated}

    def cleanup(self):
        now = time.time()
        with self._validation_lock:
            paths = self._closed()
            keep = []
            for path in paths:
                if not path.is_file():
                    continue
                stat = path.stat()
                if path not in self._pins and now - stat.st_mtime > self.retention:
                    path.unlink(missing_ok=True)
                else:
                    keep.append((path, stat.st_size))
            total = sum(size for _, size in keep)
            for path, size in keep:
                if total <= self.max_bytes:
                    break
                if path in self._pins:
                    continue
                path.unlink(missing_ok=True)
                total -= size

    def probe(self):
        with self.pin() as paths:
            if not paths:
                raise FileNotFoundError("No segments yet; wait for the buffer to fill.")
            if self._probe_cache is None:
                self._probe_cache = ingest._probe_local(next(reversed(paths)), run=self.runner)
            return self._probe_cache
