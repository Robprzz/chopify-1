"""Resolve live URLs and keep a bounded rolling buffer of HLS segments."""
from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import ingest

SEGMENT_SECONDS = 4
DIRECT_SUFFIXES = (".m3u8", ".ts")
DIRECT_SCHEMES = {"rtmp", "rtmps", "rtmp+https"}
PLATFORMS = (
    ("twitch.tv", "twitch"), ("youtube.com", "youtube"), ("youtu.be", "youtube"),
    ("kick.com", "kick"),
)


@dataclass
class LiveSource:
    url: str
    kind: str
    platform: str
    resolved_by: str
    headers: list[str]
    page_url: str | None = None

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
    return f"Could not resolve the {platform} stream. Check the URL, network, and yt-dlp version."


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
    cmd = [sys.executable, "-m", "yt_dlp", "--dump-single-json", "--no-playlist",
           "--no-warnings", "--socket-timeout", "10", "-f", "best[protocol^=m3u8]/best", "--", url]
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
    resolved = data.get("url")
    if not isinstance(resolved, str) or not urlparse(resolved).hostname or not ingest.is_url(resolved):
        raise ValueError("yt-dlp did not return a playable video stream for this broadcast.")
    http_headers = data.get("http_headers") or {}
    if isinstance(http_headers, dict):
        overrides = {line.split(":", 1)[0].strip().lower() for line in headers}
        headers += [f"{key}: {value}" for key, value in http_headers.items()
                    if key.lower() not in overrides and isinstance(value, str)
                    and re.fullmatch(r"[a-zA-Z0-9-]+", key) and "\r" not in value and "\n" not in value]
    protocol = str(data.get("protocol") or "").lower()
    kind = "hls" if "m3u8" in resolved or "hls" in protocol else "http"
    return LiveSource(resolved, kind, _platform(url), "yt-dlp", headers, url)


class SegmentBuffer:
    """ffmpeg pulls once; analyzers read the resulting 4-second segments."""

    def __init__(self, directory, url, options, runner=None, headers=None):
        self.directory = Path(directory)
        self.url = url
        self.options = options
        self.runner = runner or ingest._run
        self.retention = float(options.get("live_buffer", 300))
        self.max_bytes = int(options.get("live_buffer_max_mb", 512)) * 1024 * 1024
        self.headers = list(options.get("live_headers") or []) if headers is None else headers
        self.process = None
        self.started_at = 0.0
        self._stop = threading.Event()
        self._cleaner = None

    def ffmpeg_command(self):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-rw_timeout", "15000000"]
        if ingest.is_url(self.url):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
            if self.headers:
                cmd += ["-headers", "".join(h + "\r\n" for h in self.headers)]
        cmd += ["-i", self.url, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                "-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
                "-segment_list", str(self.directory / "segments.csv"), "-segment_list_type", "csv",
                "-segment_list_size", str(int(self.retention) + 2),
                "-reset_timestamps", "1", str(self.directory / "segment%08d.ts")]
        return cmd

    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.started_at = time.time()
        self.log_path = self.directory / "ffmpeg.log"
        self._log_handle = self.log_path.open("a", encoding="utf-8")
        try:
            self.process = subprocess.Popen(self.ffmpeg_command(), stdout=subprocess.DEVNULL,
                                            stderr=self._log_handle)
        except OSError:
            self._log_handle.close()
            raise
        self._cleaner = threading.Thread(target=self._clean_loop, daemon=True)
        self._cleaner.start()
        return self

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
        if getattr(self, "_log_handle", None) is not None:
            self._log_handle.close()
            self._log_handle = None

    def segments(self):
        if not self.directory.exists():
            return []
        return sorted(p for p in self.directory.glob("segment[0-9]*.ts")
                      if re.fullmatch(r"segment\d{8}\.ts", p.name))

    def ready_segments(self):
        """Segments safe to decode: the newest one is still being written."""
        segments = self.segments()
        return segments if self.process is not None and self.process.poll() == 0 else segments[:-1]

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
