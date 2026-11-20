"""Resolve live URLs and keep a bounded rolling buffer of HLS segments."""
from __future__ import annotations

import csv
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
        self.segment_problem = None
        self._validation_lock = threading.Lock()
        self._validated = set()
        self._skipped = set()
        self._has_valid_segment = False
        self.process = None
        self.started_at = 0.0
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
            except OSError:
                pass

    @property
    def alive(self):
        return self.process is not None and self.process.poll() is None

    def stop(self):
        self._stop.set()
        if self.process is not None and self.alive:
            ingest._kill_tree(self.process)
            self.process.wait()
        if self._cleaner is not None:
            self._cleaner.join(timeout=2.0)
        if self._diagnostics is not None:
            self._diagnostics.join(timeout=2.0)

    def segments(self):
        if not self.directory.exists():
            return []
        return sorted(p for p in self.directory.glob("segment[0-9]*.ts")
                      if re.fullmatch(r"segment\d{8}\.ts", p.name))

    def ready_segments(self):
        """Segments safe to decode: the newest one is still being written."""
        segments = self.segments()
        closed = segments if self.process is not None and self.process.poll() == 0 else segments[:-1]
        if not self.audio_url and not self.max_height:
            return closed
        with self._validation_lock:
            self._validated.intersection_update(closed)
            finished = self.process is not None and self.process.poll() == 0
            for path in closed:
                if self.segment_problem:
                    break
                if path in self._validated or path in self._skipped:
                    continue
                problem = self._check_segment(path)
                if not problem:
                    self._validated.add(path)
                    self._has_valid_segment = True
                elif self.audio_url and problem in (MISSING_TRACK, SYNC_ERROR) and (
                        (not self._has_valid_segment and len(self._skipped) < 2 and (not finished or path != closed[-1]))
                        or (finished and self._has_valid_segment and path == closed[-1])):
                    # Different playlist live edges can leave leading or trailing partial segments.
                    self._skipped.add(path)
                else:
                    self.segment_problem = problem
            if finished and not self._has_valid_segment and self._skipped and not self.segment_problem:
                self.segment_problem = self._check_segment(closed[-1]) if closed else MISSING_TRACK
            return [path for path in closed if path in self._validated]

    def _check_segment(self, path):
        try:
            result = self.runner(["ffprobe", "-v", "error", "-show_entries",
                                  "stream=codec_type,height,start_time,duration", "-of", "json", str(path)], timeout=5)
            tracks = {stream["codec_type"]: stream for stream in json.loads(result.stdout)["streams"]}
            if "video" not in tracks:
                return MISSING_TRACK if self.audio_url else CAPTURE_ERROR
            if self.max_height and int(tracks["video"]["height"]) > self.max_height:
                return HEIGHT_ERROR
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
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
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
        ready = self.ready_segments()
        try:
            with (self.directory / "segments.csv").open(encoding="utf-8", newline="") as handle:
                ranges = {row[0]: (float(row[1]), float(row[2])) for row in csv.reader(handle)
                          if len(row) == 3}
            return {path: ranges[path.name] for path in ready if path.name in ranges}
        except (OSError, ValueError):
            return {path: (self.index_to_time(self.index_of(path)),
                           self.index_to_time(self.index_of(path) + 1)) for path in ready}

    def cleanup(self):
        now = time.time()
        paths = self.ready_segments()
        keep = []
        for path in paths:
            if now - path.stat().st_mtime > self.retention:
                path.unlink(missing_ok=True)
            else:
                keep.append(path)
        total = sum(p.stat().st_size for p in keep)
        for path in keep:
            if total <= self.max_bytes:
                break
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total -= size

    def probe(self):
        paths = self.ready_segments()
        if not paths:
            raise FileNotFoundError("No segments yet; wait for the buffer to fill.")
        return ingest._probe_local(paths[-1], run=self.runner)
