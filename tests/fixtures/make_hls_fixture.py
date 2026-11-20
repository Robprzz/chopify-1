"""Generate a short local HLS fixture for live pipeline tests."""
import subprocess
from pathlib import Path


def make_fixture(directory, seconds=8, size="320x180"):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    raw = directory / "raw.ts"
    # testsrc has a moving pattern; the analyzers sample at 2 fps and need motion.
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=" + str(seconds),
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "60", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", "-f", "mpegts", str(raw)], check=True)
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-i", str(raw), "-c", "copy",
        "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
        str(directory / "index.m3u8")], check=True)
    return directory / "index.m3u8"


def make_split_fixture(directory):
    """Two HLS inputs with a 160ms start offset and a simultaneous flash/tone."""
    for track in ("video", "audio"):
        (directory / track).mkdir(parents=True)
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=black:s=320x180:r=30:d=12",
        "-f", "lavfi", "-i", "aevalsrc=0.8*sin(2*PI*1000*t)*between(t\\,2\\,2.2):s=48000:d=12",
        "-vf", "drawbox=c=white:t=fill:enable='between(t,1.8333,2.0333)',setpts=PTS+5",
        "-map", "0:v", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
        "-g", "60", "-sc_threshold", "0", "-muxdelay", "0",
        "-f", "hls", "-hls_time", "2", "-hls_list_size", "0", str(directory / "video" / "index.m3u8"),
        "-map", "1:a", "-vn", "-c:a", "aac", "-muxdelay", "0",
        "-f", "hls", "-hls_time", "2", "-hls_list_size", "0", str(directory / "audio" / "index.m3u8"),
    ], check=True)
