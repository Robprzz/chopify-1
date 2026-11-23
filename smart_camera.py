"""Bounded crop motion and source-relative checks for local vertical exports."""
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
import json
import math
import re
from statistics import median
import subprocess


MODES = ("auto", "smart", "split", "stable")
class MotionError(ValueError):
    """An export failed an explicit visual or frame-clock requirement."""

    def __init__(self, message, intervals=None):
        super().__init__(message)
        self.intervals = intervals or []


RATES = tuple(map(Fraction, ("24000/1001", "24", "25", "30000/1001", "30", "48",
                            "50", "60000/1001", "60")))


@dataclass
class Framing:
    points: list
    coverage: float
    scene_cuts: int
    rapid_reversals: int
    subject_changes: int
    pairs: tuple | None
    reliable: bool
    reason: str
    peak_speed: float
    peak_acceleration: float

    def metrics(self):
        return {key: value for key, value in vars(self).items() if key not in ("points", "pairs")}


def plan(observations, width, crop, duration):
    """Follow spatial identity until a scene cut or sustained loss."""
    target = None
    pending = None
    lost = matched = changes = cuts = reversals = 0
    last_direction, last_motion = 0, None
    points = []
    pairs = []
    center = width / 2
    velocity = speed_peak = acceleration_peak = 0.
    dead = max(2., crop * .045)
    speed_limit, acceleration_limit = width * .22, width * .58
    last = None
    for t, faces, cut in observations:
        first = last is None
        dt = .2 if first else t - last
        last = t
        # Face confidence filters detections upstream. Once acquired, prefer the
        # same location; area and a momentary rival cannot select the speaker.
        if cut:
            cuts += 1
            target = pending = None
            lost = 0
        separated = sorted(faces)
        if len(separated) == 2 and separated[1] - separated[0] >= width * .22:
            pairs.append(separated)
        if target is None:
            target = min(faces, key=lambda x: abs(x - width / 2), default=None)
            found = target is not None
        else:
            closest = min(faces, key=lambda x: abs(x - target), default=None)
            found = closest is not None and abs(closest - target) <= width * .105
            if found:
                target, lost, pending = closest, 0, None
            else:
                lost += dt
                if closest is not None and (pending is None or abs(closest - pending) > width * .08):
                    pending, lost = closest, dt
                if lost >= 1.4 and closest is not None:
                    target, lost, pending = closest, 0, None
                    changes += 1
                    found = True
        if found:
            matched += 1
        aim = min(width - crop / 2, max(crop / 2, target if target is not None else center))
        if first or cut:
            center, velocity = aim, 0.
            last_direction, last_motion = 0, None
        else:
            delta = aim - center
            distance = max(0., abs(delta) - dead)
            desired = math.copysign(min(speed_limit, distance * 2.,
                                        math.sqrt(2 * acceleration_limit * distance)), delta)
            applied = min(acceleration_limit * dt, max(-acceleration_limit * dt, desired - velocity))
            average = velocity + applied / 2
            velocity += applied
            raw = center + average * dt
            center = min(width - crop / 2, max(crop / 2, raw))
            if center != raw:
                velocity = 0.
            speed_peak = max(speed_peak, abs(velocity))
            acceleration_peak = max(acceleration_peak, abs(applied / dt))
            if abs(velocity) > width * .035:
                direction = 1 if velocity > 0 else -1
                if last_direction == -direction and t - last_motion < 1.8:
                    reversals += 1
                last_direction, last_motion = direction, t
        points.append((t, center, cut, velocity))
    coverage = matched / len(observations) if observations else 0
    stable_pairs = None
    if pairs and len(pairs) >= len(observations) * .8 and not cuts:
        left, right = zip(*pairs)
        if max(left) - min(left) <= width * .08 and max(right) - min(right) <= width * .08:
            stable_pairs = (median(left) / width, median(right) / width)
    reason = ("detecciones insuficientes" if coverage < .58 else
              "demasiados cambios de sujeto" if changes > max(1, duration / 20) else
              "oscilaciones rápidas de encuadre" if reversals > max(2, duration / 8) else None)
    return Framing(points, round(coverage, 3), cuts, reversals, changes, stable_pairs, reason is None,
                   reason or "seguimiento estable",
                   round(speed_peak / width, 4), round(acceleration_peak / width, 4))


def write_commands(points, width, crop, destination, duration):
    """Emit per-frame interpolation commands; preserve discontinuities at real cuts."""
    def x(center):
        return min(width - crop, max(0., center - crop / 2))

    lines = []
    for (t, center, _, velocity), (next_t, next_center, cut, _) in zip(points, points[1:]):
        start, end = x(center), x(center if cut else next_center)
        tangent = velocity * (next_t - t) if not cut else 0.
        delta = end - start
        # At a clamped boundary restrict the initial tangent so the polynomial
        # stays between both permitted crop origins.
        tangent = math.copysign(min(abs(tangent), 2 * abs(delta)), delta) if tangent * delta > 0 else 0.
        if abs(delta) < .05:
            lines.append(f"{t:.6f} [enter] crop x {start:.6f};")
        else:
            lines.append(f"{t:.6f}-{next_t:.6f} [expr] crop x {start:.6f}+({tangent:.6f})*TI+({delta-tangent:.6f})*TI*TI;")
    if points:
        t, center, _, _ = points[-1]
        if t < duration:
            lines.append(f"{t:.6f} [enter] crop x {x(center):.6f};")
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def source_rate(source, start, timeout):
    """Measure presentation times; choose a rational delivery rate for VFR."""
    result = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", f"{start:.3f}%+#241",
        "-show_packets", "-show_streams", "-show_entries", "packet=pts_time:stream=avg_frame_rate,r_frame_rate",
        "-of", "json", str(source),
    ], check=True, capture_output=True, timeout=min(25, timeout or 25))
    data = json.loads(result.stdout)
    stamps = sorted({float(packet["pts_time"]) for packet in data.get("packets", []) if "pts_time" in packet})
    intervals = [b - a for a, b in zip(stamps, stamps[1:]) if 0 < b - a < .25]
    stream = data["streams"][0]
    known = [Fraction(stream[key]) for key in ("avg_frame_rate", "r_frame_rate")
             if stream.get(key) not in (None, "0/0", "0/1")]
    nominal = known[0] if known else Fraction(30)
    if not intervals:
        return nominal if 8 <= nominal <= 120 else Fraction(30), "insufficient_evidence"
    step = median(intervals)
    jitter = sum(abs(value - step) > max(.0005, step * .06) for value in intervals) / len(intervals)
    native = next((rate for rate in known if 8 <= rate <= 120 and abs(float(rate) * step - 1) < .009), None)
    if native and jitter <= .02:
        return native, "cfr"
    return min(RATES, key=lambda rate: abs(float(rate) * step - 1)), "vfr"


def freeze_spans(path, start, duration, timeout):
    """Read-only low-resolution analysis at native PTS, without frame synthesis."""
    result = subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-v", "info", "-threads", "2", "-ss", f"{start:.3f}",
        "-i", str(path), "-t", f"{duration:.3f}", "-an", "-sn", "-filter_threads", "1",
        "-vf", "scale=160:90:flags=area,freezedetect=n=-50dB:d=0.6",
        "-f", "null", "-",
    ], check=True, capture_output=True, encoding="utf-8", errors="replace",
       timeout=min(timeout or 180, max(30, duration * 2 + 10)))
    spans, pending = [], None
    for kind, value in re.findall(r"freeze_(start|end):\s*([\d.]+)", result.stderr):
        if kind == "start":
            pending = float(value)
        elif pending is not None:
            spans.append((pending, float(value)))
            pending = None
    if pending is not None and duration > pending:
        spans.append((pending, duration))
    return spans


def excess_freezes(output, source):
    """Only flag unexplained holds; corroborated static content is preserved."""
    unexplained = []
    for start, end in output:
        covered = sum(max(0., min(end, b + .12) - max(start, a - .12)) for a, b in source)
        if end - start - covered >= .65:
            unexplained.append([round(start, 3), round(end, 3)])
    return unexplained


def delivery_check(path, rate, duration, timeout):
    """Verify the encoded frame clock and the vertical delivery contract."""
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_packets", "-show_streams",
        "-show_entries", "packet=stream_index,pts_time,dts_time:stream=index,codec_type,width,height,sample_aspect_ratio,"
        "display_aspect_ratio,pix_fmt,codec_name,start_time,duration", "-of", "json", str(path),
    ], check=True, capture_output=True, timeout=min(timeout or 45, 45))
    data = json.loads(result.stdout)
    stream = next(item for item in data["streams"] if item["codec_type"] == "video")
    expected = (1080, 1920, "1:1", "9:16", "yuv420p", "h264")
    actual = tuple(stream.get(key) for key in ("width", "height", "sample_aspect_ratio", "display_aspect_ratio",
                                                "pix_fmt", "codec_name"))
    if actual != expected:
        raise MotionError(f"Control vertical: el MP4 tiene un formato inesperado: {actual}")
    video = [packet for packet in data["packets"] if packet["stream_index"] == stream["index"]]
    pts = sorted(float(packet["pts_time"]) for packet in video)
    dts = [float(packet["dts_time"]) for packet in video]
    step = float(1 / rate)
    deltas = [b - a for a, b in zip(pts, pts[1:])]
    if (not pts or abs(pts[0]) > step + .002
            or abs(float(stream["duration"]) - duration) > max(.12, step * 2)
            or any(abs(delta - step) > .001 for delta in deltas)
            or any(b <= a for a, b in zip(dts, dts[1:]))):
        raise MotionError("Control vertical: discontinuidad en los timestamps o duración inesperada.")
    metrics = {"frames": len(pts), "max_frame_interval": round(max(deltas, default=0), 6),
               "max_frame_jitter": round(max((abs(delta - step) for delta in deltas), default=0), 6)}
    audio = next((item for item in data["streams"] if item["codec_type"] == "audio"), None)
    if audio:
        v_start, a_start = float(stream["start_time"]), float(audio["start_time"])
        v_end = v_start + float(stream["duration"])
        a_end = a_start + float(audio["duration"])
        start_skew, end_skew = abs(v_start - a_start), abs(v_end - a_end)
        if audio["codec_name"] != "aac" or max(start_skew, end_skew) > .22:
            raise MotionError("Control vertical: audio no compatible o fuera de sincronía.")
        metrics.update(av_start_skew=round(start_skew, 4), av_end_skew=round(end_skew, 4))
    return metrics


def saved_motion(path):
    """Access the published render evidence without decoding the MP4 again."""
    from subtitle_quality import local_text, SIDECAR_LIMIT
    try:
        metadata = json.loads(local_text(Path(path).with_suffix(".meta.json"), SIDECAR_LIMIT))
    except (OSError, ValueError):
        return None
    value = metadata.get("motion") if isinstance(metadata, dict) else None
    return value if isinstance(value, dict) and value.get("version") == 1 else None
