"""
Stage 3 of the viral-clip pipeline.

Renders scored segments to clips in a chosen aspect ratio:
  - 16:9 (default) -> landscape, full frame kept
  - 9:16           -> vertical, bounded Smart Camera or stable framing
  - 1:1            -> square
Captions are word-by-word CapCut style, burnt in via the ffmpeg ASS engine,
with selectable style presets. Optional --tighten removes filler words and
long silences (captions are re-timed automatically). Every clip gets a PNG
poster and a .meta.json with a ready-to-post title, description and hashtags.

No external/paid APIs. ffmpeg + OpenCV only. Usage:
    python render_clips.py [workdir] [--aspect 16:9|9:16|1:1]
                           [--out DIR] [--style NAME]
                           [--tighten] [--loudnorm] [--preview]
Output directory (default ./clips) can also be set with $CHOPIFY_OUT.
"""
import sys
import os
import re
import json
import studio_json
import project_cancel
import argparse
import subprocess
import math
import tempfile
import uuid
from functools import lru_cache
from pathlib import Path

import shutil

import captions
import branding
import tighten  # local module: filler/silence removal
import watermark
import signature
import smart_camera
import render_progress
from unicode_io import configure_utf8

# aspect -> (target_w, target_h, caption_font_size, caption_margin_v)
ASPECTS = {
    "16:9": (1920, 1080, 72, 95),
    "9:16": (1080, 1920, 96, 300),
    "1:1":  (1080, 1080, 82, 120),
}
DEFAULT_ASPECT = "16:9"

# --preview drafts: small, fast, no poster/meta. Caption font/margins scale down
# proportionally from the full-size ASPECTS values (height is the anchor).
PREVIEW = {
    "16:9": (854, 480),
    "9:16": (480, 854),
    "1:1":  (480, 480),
}

# Caption presets. Colors are #RRGGBB here; build_ass accepts both hex and the
# ASS strings returned by branding.load_brand. Font size and margin are measured
# at 1080px output height, so None means "use this aspect's base value".
CAPTION_STYLES = {
    "default": {
        "font": "Arial Black", "weight": -1, "color": "#FFFFFF",
        "highlight": "#FFFF00", "emphasis_color": "#FFD400",
        "outline_color": "#000000", "uppercase": False, "words_per_cue": 3,
        "position": "bottom", "outline": 5, "shadow": 2, "background": "none",
        "background_color": "#000000", "background_opacity": 70,
        "animation": "none", "highlight_mode": "word", "emoji": False,
        "emphasis": False,
    },
    "clean": {
        "font": "Arial", "weight": 0, "color": "#FFFFFF",
        "highlight": "#FFB000", "emphasis_color": "#FFD400",
        "outline_color": "#101010", "uppercase": False, "words_per_cue": 3,
        "position": "bottom", "outline": 2, "shadow": 1, "background": "none",
        "animation": "none", "highlight_mode": "word",
    },
    "minimal": {
        "font": "Arial", "weight": 0, "color": "#FFFFFF",
        "highlight": "#D4EF83", "emphasis_color": "#FFD400",
        "outline_color": "#000000", "uppercase": False, "words_per_cue": 5,
        "position": "bottom", "outline": 8, "shadow": 0, "background": "box",
        "background_color": "#111111", "background_opacity": 65,
        "animation": "none", "highlight_mode": "word",
    },
    "bold": {
        "font": "Impact", "weight": -1, "color": "#FFFFFF",
        "highlight": "#FF7A00", "emphasis_color": "#FFD400",
        "outline_color": "#000000", "uppercase": True, "words_per_cue": 2,
        "position": "bottom", "outline": 8, "shadow": 2, "background": "none",
        "animation": "pop", "highlight_mode": "word",
    },
    "boxed": {
        "font": "Arial Black", "weight": -1, "color": "#FFFFFF",
        "highlight": "#FFFF00", "emphasis_color": "#FFD400",
        "outline_color": "#000000", "uppercase": True, "words_per_cue": 3,
        "position": "bottom", "outline": 10, "shadow": 0, "background": "box",
        "background_color": "#000000", "background_opacity": 80,
        "animation": "none", "highlight_mode": "word",
    },
    "karaoke": {
        "font": "Arial Black", "weight": -1, "color": "#FFFFFF",
        "highlight": "#FFFF00", "emphasis_color": "#FFD400",
        "outline_color": "#000000", "uppercase": False, "words_per_cue": 4,
        "position": "bottom", "outline": 5, "shadow": 2, "background": "none",
        "animation": "none", "highlight_mode": "karaoke",
    },
    "pop": {
        "font": "Impact", "weight": -1, "color": "#FFFFFF",
        "highlight": "#FF7A00", "emphasis_color": "#FFD400",
        "outline_color": "#000000", "uppercase": True, "words_per_cue": 2,
        "position": "bottom", "outline": 6, "shadow": 2, "background": "none",
        "animation": "pop", "highlight_mode": "word",
    },
    "neon": {
        "font": "Arial", "weight": 0, "color": "#FFFFFF",
        "highlight": "#00FFFF", "emphasis_color": "#00FFFF",
        "outline_color": "#00FFFF", "uppercase": True, "words_per_cue": 3,
        "position": "bottom", "outline": 3, "shadow": 0, "background": "none",
        "animation": "fade", "highlight_mode": "word",
    },
    "typewriter": {
        "font": "Courier New", "weight": 0, "color": "#FFFFFF",
        "highlight": "#00FF7F", "emphasis_color": "#FFD400",
        "outline_color": "#000000", "uppercase": False, "words_per_cue": 4,
        "position": "bottom", "outline": 3, "shadow": 1, "background": "none",
        "animation": "typewriter", "highlight_mode": "word",
    },
    "retro": {
        "font": "Georgia", "weight": 0, "color": "#FFF6D5",
        "highlight": "#FFB000", "emphasis_color": "#FFD400",
        "outline_color": "#2B1A0A", "uppercase": False, "words_per_cue": 3,
        "position": "bottom", "outline": 2, "shadow": 6, "background": "none",
        "animation": "none", "highlight_mode": "word",
    },
    "hormozi": {
        "font": "Arial Black", "weight": -1, "color": "#FFFFFF",
        "highlight": "#FF0000", "emphasis_color": "#FFD400",
        "outline_color": "#000000", "uppercase": False, "words_per_cue": 2,
        "position": "bottom", "outline": 5, "shadow": 2, "background": "none",
        "animation": "pop", "highlight_mode": "word",
    },
    "mrbeast": {
        "font": "Arial Black", "weight": -1, "color": "#FFFFFF",
        "highlight": "#00FF00", "emphasis_color": "#FFD400",
        "outline_color": "#000000", "uppercase": False, "words_per_cue": 2,
        "position": "bottom", "outline": 5, "shadow": 2, "background": "none",
        "animation": "pop", "highlight_mode": "word",
    },
    "podcast": {
        "font": "Arial", "weight": 0, "color": "#FFFFFF",
        "highlight": "#FFFF00", "emphasis_color": "#FFD400",
        "outline_color": "#000000", "uppercase": False, "words_per_cue": 4,
        "position": "bottom", "outline": 3, "shadow": 1, "background": "none",
        "animation": "none", "highlight_mode": "karaoke",
    },
}
DEFAULT_STYLE = "default"
ALIGNMENTS = {"bottom": 2, "center": 5, "top": 8}

# Automatic emphasis is deliberately small and deterministic. Two words per cue
# at most, so a line never turns into a wall of colour.
POWER_WORDS = frozenset("""
secret mistake mistakes never always free proven guarantee guaranteed fastest
best worst huge massive insane crazy shocking truth nobody everyone stop start
why how million billion thousand hundred percent money cash rich broke
""".split())

EMOJI_RULES = [
    (("money", "cash", "dollar", "profit", "revenue", "rich", "paid", "pay",
      "salary", "price", "cost", "income", "sale"), "💰"),
    (("grow", "growth", "scale", "audience", "follower", "subscriber", "view"), "📈"),
    (("idea", "secret", "tip", "hack", "strategy", "plan", "trick"), "💡"),
    (("time", "hour", "minute", "day", "week", "month", "year", "deadline"), "⏰"),
    (("love", "heart", "passion"), "❤️"),
    (("fire", "amazing", "incredible", "insane", "crazy"), "🔥"),
    (("win", "success", "best", "great", "awesome", "winner"), "🏆"),
    (("mistake", "wrong", "fail", "failure", "problem", "risk", "warning"), "⚠️"),
    (("think", "brain", "learn", "knowledge", "understand"), "🧠"),
    (("work", "job", "business", "client", "customer"), "💼"),
    (("video", "camera", "record", "content", "youtube", "tiktok", "podcast"), "🎬"),
]


def default_out():
    """Output dir: --out wins, then $CHOPIFY_OUT, then ./clips (no more hardcoded C:\\clips)."""
    return Path(os.environ.get("CHOPIFY_OUT") or "clips")


def get_style(name):
    if name not in CAPTION_STYLES:
        raise SystemExit(f"Unknown caption style '{name}'. "
                         f"Choose from: {', '.join(CAPTION_STYLES)}")
    return CAPTION_STYLES[name]


def resolve_caption(style_name=DEFAULT_STYLE, brand=None, caption=None):
    """Merge preset -> brand -> project override into one concrete style."""
    style = {"weight": -1, **branding.CAPTION_DEFAULTS, **get_style(style_name)}
    if brand:
        for key in branding.CAPTION_FIELDS:
            value = brand.get(key)
            if value is not None:
                style[key] = value
    if caption:
        style.update(branding.validate_caption(caption))
    return style


def ass_style_color(value):
    """Accept #RRGGBB and the ASS strings load_brand already produces."""
    if isinstance(value, str) and re.fullmatch(r"&H[0-9a-fA-F]{6,8}&", value):
        return value
    return branding.ass_color(value)


def ass_box_color(value, opacity):
    color = ass_style_color(value)[2:-1]
    bgr = color[2:] if len(color) == 8 else color
    alpha = round((100 - opacity) * 255 / 100)
    return f"&H{alpha:02X}{bgr}&"


def _plain_word(word):
    return re.sub(r"^[^\w$€£¥]+|[^\w%$€£¥]+$", "", str(word).strip())


def is_emphasized(word):
    plain = _plain_word(word)
    if not plain:
        return False
    if re.search(r"\d", plain) or re.search(r"[$€£¥]", plain):
        return True
    if plain.isupper() and len(plain) > 2 and any(c.isalpha() for c in plain):
        return True
    return plain.lower().strip(".,!?;:'\"") in POWER_WORDS


def _emoji_for(word):
    token = re.sub(r"[^a-z0-9']", "", str(word).lower())
    if not token:
        return None
    for stems, emoji in EMOJI_RULES:
        if any(token.startswith(stem) for stem in stems):
            return emoji
    return None


def _cue_highlight(group, enabled):
    """Indexes to emphasise, at most two per cue. Forced flags always count."""
    marked = [i for i, word in enumerate(group) if word.get("emphasis") is True]
    if enabled:
        marked += [i for i, word in enumerate(group)
                   if i not in marked and is_emphasized(word["word"])]
    return set(marked[:2])


def _cue_emoji(group):
    for index, word in enumerate(group):
        emoji = _emoji_for(word["word"])
        if emoji:
            return index, emoji
    return None, None


def _motion_tag(animation):
    if animation == "pop":
        return r"\fscx135\fscy135\t(0,120,\fscx100\fscy100)"
    if animation == "bounce":
        return (r"\fscx88\fscy118\t(0,90,\fscx112\fscy88)"
                r"\t(90,210,\fscx100\fscy100)")
    if animation == "fade":
        return r"\fad(150,0)"
    return ""

WORDS_PER_LINE = 3
DET_FPS = 5
DET_W = 640
YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
             "face_detection_yunet/face_detection_yunet_2023mar.onnx")


def sanitize(text, maxlen=70):
    text = (text or "").strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:maxlen].strip("-") or "clip"


def ffprobe_dims(path):
    from export_cache import identity

    return _probe_dimensions(*identity(path))


@lru_cache(maxsize=128)
def _probe_dimensions(path, _size, _mtime, _ctime):
    out = project_cancel.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:stream_side_data=rotation", "-of", "json", str(path)],
        capture_output=True, encoding="utf-8", errors="backslashreplace", check=True).stdout.strip()
    video = json.loads(out)["streams"][0]
    width, height = int(video["width"]), int(video["height"])
    rotation = next((item["rotation"] for item in video.get("side_data_list", []) if "rotation" in item), 0)
    # FFmpeg applies the display matrix before the first filter.
    return (height, width) if abs(rotation) % 180 == 90 else (width, height)


def crop_for(W, H, tw, th):
    """Largest centred crop of the WxH source matching the target aspect."""
    if min(W, H, tw, th) < 2:
        raise RenderFailure("El video fuente y el lienzo deben medir al menos 2 × 2 píxeles.")
    tar = tw / float(th)
    src = W / float(H)
    if abs(src - tar) < 1e-3:
        cw, ch = W, H
    elif src > tar:                      # source wider -> crop width
        ch = H
        cw = int(round(H * tar))
    else:                                # source taller -> crop height
        cw = W
        ch = int(round(W / tar))
    cw = max(2, cw - cw % 2)
    ch = max(2, ch - ch % 2)
    x0 = max(0, (W - cw) // 2)
    y0 = max(0, (H - ch) // 2)
    return cw, ch, x0, y0


def _make_detector(workdir, allow_download=True):
    import cv2
    model = Path(workdir).parent / "yunet.onnx"
    if allow_download and not model.exists():
        try:
            import urllib.request
            if project_cancel.active():
                with (tempfile.TemporaryDirectory(dir=model.parent) as tmp,
                      urllib.request.urlopen(YUNET_URL, timeout=10) as response):
                    pending = Path(tmp) / model.name
                    with pending.open("wb") as target:
                        while True:
                            project_cancel.check()
                            block = response.read(65536)
                            if not block:
                                break
                            target.write(block)
                    project_cancel.check()
                    pending.replace(model)
            else:
                urllib.request.urlretrieve(YUNET_URL, str(model))
        except Exception as e:
            print("  YuNet download failed -> Haar:", e, flush=True)
    if model.exists():
        try:
            return "yunet", cv2.FaceDetectorYN.create(str(model), "", (DET_W, 360), 0.6, 0.3, 5000)
        except Exception as e:
            print("  YuNet init failed -> Haar:", e, flush=True)
    return "haar", cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")


def detect_track(source, start, dur, W, workdir, allow_download=True, timeout=None):
    """Sample faces and visual cuts in a private CPU-only detection pass."""
    import cv2
    try:
        kind, detector = _make_detector(workdir, allow_download)
        with tempfile.TemporaryDirectory(prefix="anqora-camera-") as scratch:
            det_path = Path(scratch) / "detect.mp4"
            project_cancel.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-ss", f"{start:.3f}", "-i", str(source),
                 "-t", f"{dur:.3f}", "-filter_threads", "1", "-vf", f"fps={DET_FPS},scale={DET_W}:-2",
                 "-an", "-threads", "1", str(det_path)], check=True, capture_output=True, timeout=timeout)
            cap = cv2.VideoCapture(str(det_path))
            try:
                dw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or DET_W
                dh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 360
                if kind == "yunet":
                    detector.setInputSize((dw, dh))
                previous = previous_hist = None
                observations = []
                while True:
                    project_cancel.check()
                    ok, frame = cap.read()
                    if not ok:
                        return observations
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    thumb = cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA)
                    histogram = cv2.calcHist([thumb], [0], None, [32], [0, 256])
                    cut = (previous is not None and cv2.mean(cv2.absdiff(thumb, previous))[0] > 44
                           and cv2.compareHist(histogram, previous_hist, cv2.HISTCMP_CORREL) < .38)
                    if kind == "yunet":
                        _, found = detector.detect(frame)
                        boxes = found if found is not None else ()
                    else:
                        boxes = detector.detectMultiScale(gray, 1.1, 5, minSize=(36, 36))
                    faces = [(box[0] + box[2] / 2) * W / dw for box in boxes]
                    observations.append((len(observations) / DET_FPS, faces, bool(cut)))
                    previous, previous_hist = thumb, histogram
            finally:
                cap.release()
    except (AttributeError, OSError, cv2.error, subprocess.SubprocessError):
        return []


def cached_track(source, start, duration, width, workdir, allow_download, timeout):
    from export_cache import analysis, identity

    def detect():
        return detect_track(source, start, duration, width, workdir, allow_download, timeout)

    if duration > 300 or not Path(source).is_file():
        return detect()
    model = Path(workdir).parent / "yunet.onnx"
    if allow_download and not model.exists():
        return detect()
    model_identity = identity(model) if model.is_file() else "haar"
    return analysis(workdir, "camera", [identity(source), start, duration, width, model_identity, DET_FPS, DET_W],
                    detect, cache_empty=False)


def ass_time(t):
    h, remainder = divmod(max(0, round(t * 100)), 360000)
    m, remainder = divmod(remainder, 6000)
    seconds, hundredths = divmod(remainder, 100)
    return f"{h:d}:{m:02d}:{seconds:02d}.{hundredths:02d}"


def ass_escape(s):
    return s.replace("\\", "").replace("{", "(").replace("}", ")")


def build_ass(words, clip_start, clip_end, path, tw, th, font_size, margin_v,
              style_name=DEFAULT_STYLE, brand=None, caption=None, *, groups=None):
    st = resolve_caption(style_name, brand, caption)
    color = ass_style_color(st["color"])
    highlight = ass_style_color(st["highlight"])
    emphasis = ass_style_color(st["emphasis_color"])
    outline_color = ass_style_color(st["outline_color"])
    if st["font_size"] is not None:
        font_size = max(12, round(st["font_size"] * th / 1080))
    if st["margin"] is not None:
        margin_v = max(0, round(st["margin"] * th / 1080))
    outline_px = max(0, round(st["outline"] * th / 1080))
    shadow_px = max(0, round(st["shadow"] * th / 1080))
    alignment = ALIGNMENTS[st["position"]]
    if st["background"] == "box":
        border_style = 3
        outline_colour = ass_box_color(st["background_color"], st["background_opacity"])
        back_colour = outline_colour
    else:
        border_style = 1
        outline_colour = outline_color
        back_colour = "&H64000000"
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {tw}\nPlayResY: {th}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,{st['font']},{font_size},{color},{color},{outline_colour},"
        f"{back_colour},{st['weight']},0,0,0,100,100,0,0,{border_style},"
        f"{outline_px},{shadow_px},{alignment},80,80,{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    group_size = int(st["words_per_cue"] or WORDS_PER_LINE)
    if groups is None:
        groups = captions.plan_cues(words, clip_start, clip_end, group_size,
                                    **{k: st[k] for k in captions.TIMING_FIELDS})
    animation = st["animation"]
    events = []
    for group in groups:
        emphasised = _cue_highlight(group, bool(st["emphasis"]))
        emoji_index, emoji = _cue_emoji(group) if st["emoji"] else (None, None)
        for i, word in enumerate(group):
            s0 = word["left"] / 1000
            en = word["right"] / 1000
            if i + 1 < len(group) and group[i + 1]["left"] - word["right"] <= 150:
                en = group[i + 1]["left"] / 1000
            if ass_time(s0) == ass_time(en):
                continue
            parts = []
            for j, item in enumerate(group):
                if animation == "typewriter" and j > i:
                    continue
                token = ass_escape(item["word"].strip())
                if st["uppercase"]:
                    token = token.upper()
                if j == emoji_index:
                    token += f" {{\\fnSegoe UI Emoji}}{emoji}{{\\fn{st['font']}}}"
                if j == i and st["highlight_mode"] != "none":
                    word_color = highlight
                elif st["highlight_mode"] == "karaoke" and j < i:
                    word_color = highlight
                elif st["highlight_mode"] != "none" and j in emphasised:
                    word_color = emphasis
                else:
                    word_color = color
                motion = _motion_tag(animation) if j == i else ""
                separator = ("" if j == 0 else r"\N" if item["line"] != group[j - 1]["line"] else " ")
                parts.append(separator + "{" + f"\\c{word_color}" + motion + "}" + token)
            events.append(
                f"Dialogue: 0,{ass_time(s0)},{ass_time(en)},Cap,,0,0,0,,{''.join(parts)}")
    Path(path).write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def write_meta(seg, out_path, subtitle_track=None, motion=None):
    """Deterministic ready-to-post metadata saved as <clip>.meta.json."""
    hook = str(seg.get("hook", "clip")).replace("-", " ").strip()
    title = hook.capitalize()
    meta = {
        "title": f"{title} #shorts",
        "description": (f"{hook.capitalize()} - full video on the channel.\n"
                        "#shorts #reels #fyp #viral #podcast"),
        "hashtags": ["#shorts", "#reels", "#fyp", "#viral"],
        "score": seg.get("overall"),
        "start": seg.get("start"),
        "end": seg.get("end"),
    }
    if subtitle_track is not None:
        meta["subtitles"] = subtitle_track
    if motion is not None:
        meta["motion"] = motion
    studio_json.write(Path(out_path).with_suffix(".meta.json"), meta)
    return Path(out_path).with_suffix(".meta.json")


CAPTION_OPTION_MAP = {
    "words_per_cue": "words_per_cue",
    "uppercase": "uppercase",
    "caption_position": "position",
    "caption_animation": "animation",
    "caption_background": "background",
    "caption_outline": "outline",
    "caption_shadow": "shadow",
    "emoji": "emoji",
    "emphasis": "emphasis",
    "caption_font": "font",
    "caption_color": "color",
    "caption_highlight": "highlight",
    "caption_emphasis_color": "emphasis_color",
}


def add_caption_arguments(ap):
    """Add the shared caption flags to chopify.py and render_clips.py."""
    ap.add_argument("--caption-json", dest="caption_json", metavar="PATH",
                    help="load a full caption style JSON object")
    ap.add_argument("--words-per-cue", dest="words_per_cue", type=int, metavar="N",
                    help="words shown together in each caption cue (1-8)")
    ap.add_argument("--uppercase", action=argparse.BooleanOptionalAction, default=None,
                    help="force captions to uppercase (or --no-uppercase)")
    ap.add_argument("--caption-position", dest="caption_position",
                    choices=["bottom", "center", "top"], default=None)
    ap.add_argument("--caption-animation", dest="caption_animation",
                    choices=["none", "pop", "bounce", "fade", "typewriter"],
                    default=None, help="motion for the active word")
    ap.add_argument("--caption-background", dest="caption_background",
                    choices=["none", "box"], default=None,
                    help="draw a background box behind each cue")
    ap.add_argument("--caption-outline", dest="caption_outline", type=int,
                    metavar="N", help="outline width at 1080px height (0-12)")
    ap.add_argument("--caption-shadow", dest="caption_shadow", type=int,
                    metavar="N", help="shadow depth at 1080px height (0-12)")
    ap.add_argument("--emoji", action=argparse.BooleanOptionalAction, default=None,
                    help="add one relevant emoji per cue (or --no-emoji)")
    ap.add_argument("--emphasis", action=argparse.BooleanOptionalAction, default=None,
                    help="emphasise numbers, money and power words (or --no-emphasis)")
    ap.add_argument("--caption-font", dest="caption_font", metavar="FAMILY")
    ap.add_argument("--caption-color", dest="caption_color", metavar="#RRGGBB")
    ap.add_argument("--caption-highlight", dest="caption_highlight", metavar="#RRGGBB")
    ap.add_argument("--caption-emphasis-color", dest="caption_emphasis_color",
                    metavar="#RRGGBB")


def caption_from_args(args):
    """Build and validate a caption override from shared CLI flags."""
    supplied = getattr(args, "caption_json", None) is not None
    caption = {}
    path = getattr(args, "caption_json", None)
    if path:
        try:
            raw = Path(path).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ValueError(f"Could not read caption JSON: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Caption JSON is invalid: {exc}") from exc
        caption.update(branding.validate_caption(data))
    for dest, key in CAPTION_OPTION_MAP.items():
        value = getattr(args, dest, None)
        if value is not None:
            caption[key] = value
            supplied = True
    return branding.validate_caption(caption) if supplied else None


class RenderFailure(ValueError):
    def __init__(self, message, alternative=None):
        super().__init__(message)
        self.alternative = alternative


class WatermarkRenderFailure(RenderFailure, watermark.WatermarkError):
    pass


def sanitized_stderr(stderr, paths):
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "backslashreplace")
    for path, label in paths.items():
        stderr = (stderr or "").replace(path, label)
    return stderr


def ffmpeg_failure(stderr, returncode, *, layout, mark):
    code = returncode - 2**32 if 2**31 <= returncode < 2**32 else returncode
    details = stderr.lower()
    if re.search(r"parsed_(subtitles|ass)_|no such filter.{0,5}subtitles|unable to open _caption\.ass", details):
        return RenderFailure(f"FFmpeg no pudo incrustar los subtítulos ({code}). Consulta el registro.", "subtitles")
    if mark and ("parsed_colorchannelmixer_" in details or str(mark.image).lower() in details):
        return WatermarkRenderFailure(f"FFmpeg no pudo procesar la marca de agua ({code}). Consulta el registro.", "watermark")
    if layout in {"stacked", "split", "smart"} and re.search(
            r"parsed_(crop|scale|vstack|overlay|sendcmd)_|no such filter.{0,5}(crop|vstack|sendcmd)", details):
        return RenderFailure(f"FFmpeg rechazó la composición vertical ({code}). Consulta el registro.", "stable")
    return RenderFailure(f"FFmpeg no pudo exportar el MP4 ({code}). Consulta el registro para conocer el error.")


def render(*args, **kwargs):
    with tempfile.TemporaryDirectory(prefix="anqora-render-") as scratch:
        return _render(*args, **kwargs, scratch=Path(scratch))


def _render(seg, words, source, W, H, workdir, aspect, out_dir=None, style=DEFAULT_STYLE,
           do_tighten=False, loudnorm=False, preview=False, brand=None,
           layout="single", centers=(0.25, 0.75), caption=None,
           source_offset=0.0, fit="cover", facecam_box=None,
           facecam_height=0.34, mark=None, transcript_info=None, timeout=None, allow_model_download=True,
           retain_for_repair=False, camera_mode="auto", *, scratch, burn_subtitles=True, signature_frame=None, export_profile="quality", progress=None):
    from export_settings import PROFILES

    progress = progress or render_progress.Progress()
    preset, crf = PROFILES[export_profile]
    progress.update("filters")
    progress.check()
    out_dir = (Path(out_dir) if out_dir else default_out()).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    source = Path(source).resolve()
    if signature_frame:
        aspect = "9:16"
    resolved = resolve_caption(style, brand, caption)
    if camera_mode not in smart_camera.MODES:
        raise ValueError("Modo de cámara desconocido.")
    if fit not in {"cover", "contain"}:
        raise ValueError("Fit must be cover or contain.")
    if layout == "stacked" and fit == "contain":
        fit = "cover"
    if layout not in {"single", "split", "stacked"} or (
            layout in {"split", "stacked"} and aspect != "9:16"):
        raise ValueError("Split-screen and gaming stacked layouts require vertical 9:16 output.")
    if len(centers) != 2 or any(not math.isfinite(v) or not 0 <= v <= 1 for v in centers):
        raise ValueError("Speaker centers must be between 0 and 1.")
    full_w, full_h, font_size, margin_v = ASPECTS[aspect]
    if preview:
        # fast 480p-class draft: smaller frame, smaller captions, no poster/meta
        tw, th = PREVIEW[aspect]
        scale = th / float(full_h)
        font_size = max(12, round(font_size * scale))
        margin_v = max(10, round(margin_v * scale))
    else:
        tw, th = full_w, full_h
    canvas_h = th
    if signature_frame:
        top_band, bottom_band = signature_frame.bands(tw, canvas_h)
        th -= top_band + bottom_band
    start = float(seg["start"])
    end = float(seg["end"])
    dur = end - start
    offset = float(source_offset)
    if not math.isfinite(offset) or offset < 0:
        raise ValueError("Source offset must be a finite nonnegative number.")
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or dur <= 0:
        raise ValueError("Invalid clip start/end times.")
    if start < offset - 1e-9:
        raise ValueError("Clip start precedes its source section.")
    local_start = max(0.0, start - offset)
    cw, ch, x0, y0 = crop_for(W, H, tw, th)

    cap_words = words
    render_src = source
    if do_tighten:
        keeps = tighten.plan_cuts(words, start, end)
        render_src = scratch / "_tight_source.mp4"
        tighten.apply_cuts(source, keeps, start, render_src,
                           source_offset=offset)
        cap_words, dur = tighten.remap_words(words, start, keeps)
        print(f"  tightened: {len(keeps)} keep-ranges, "
              f"{(end - start) - dur:.1f}s removed", flush=True)
        start = 0.0
        local_start = 0.0
    caption_error = None
    try:
        groups = captions.plan_cues(cap_words, start, start + dur, resolved["words_per_cue"],
                                    **{k: resolved[k] for k in captions.TIMING_FIELDS})
    except ValueError as exc:
        if not retain_for_repair or preview:
            raise
        caption_error = str(exc)
        groups = []
    def write_caption(margin, override):
        if not burn_subtitles:
            return
        try:
            build_ass(cap_words, start, start + dur, scratch / "_caption.ass",
                      tw, th, font_size, margin, style, brand, override, groups=groups)
        except (OSError, UnicodeError) as exc:
            print(f"ASS: {exc}", file=sys.stderr, flush=True)
            raise RenderFailure("No se pudo crear el archivo ASS. Consulta el registro.", "subtitles") from exc

    if layout == "stacked" and aspect == "9:16" and camera_mode != "auto":
        layout = "single"
    if layout == "stacked":
        box, warning = resolve_facecam_box(source, W, H, workdir, facecam_box,
                                           allow_download=allow_model_download, timeout=timeout)
        if warning:
            print(f"  {warning}", flush=True)
        vf, seam_margin = stacked_graph(W, H, tw, th, facecam_height, box)
        caption_override = dict(caption or {})
        caption_override["margin"] = None
        write_caption(seam_margin, caption_override)
    else:
        write_caption(margin_v, caption)

    progress.update("camera")
    vertical = aspect == "9:16"
    needs_track = cw < W * .95 and fit == "cover"
    motion = framing = None
    requested_layout, requested_centers = layout, centers
    if vertical:
        rate, cadence = smart_camera.source_rate(render_src, local_start, timeout)
        try:
            color = smart_camera.source_color(render_src, timeout)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError):
            color = {"pix_fmt": "unknown", "color_range": "unknown"}
        print("Color de entrada:", studio_json.dumps(color), flush=True)
        motion = {"version": 1, "requested": camera_mode, "source_cadence": cadence, "fps": str(rate),
                  "source_color": color}
        if camera_mode != "auto":
            layout = "single"
        manual_split = camera_mode == "split" and (requested_layout == "split" or tuple(centers) != (.25, .75))
        auto_split = camera_mode == "split" and not manual_split
        if manual_split:
            layout = "split"
        elif layout == "single" and (needs_track or auto_split) and camera_mode != "stable":
            observations = cached_track(render_src, local_start, dur, W, workdir, False, timeout)
            framing = smart_camera.plan(observations, W, cw, dur)
            motion.update(framing.metrics())
            if camera_mode in {"auto", "split"} and framing.pairs:
                layout, centers = "split", framing.pairs
            elif auto_split:
                motion["fallback"] = {"reason": "no se verificaron dos posiciones estables"}
                framing = None
            elif framing.reliable:
                smart_camera.write_commands(framing.points, W, cw, scratch / "_crop.cmd", dur)
    if not vertical and layout == "single" and needs_track:
        framing = smart_camera.plan(
            cached_track(render_src, local_start, dur, W, workdir, allow_model_download, timeout), W, cw, dur)
        if framing.reliable:
            smart_camera.write_commands(framing.points, W, cw, scratch / "_crop.cmd", dur)

    progress.update("filters")
    blur_w, blur_h = (tw // 8) * 2, (th // 8) * 2
    stable = (f"[0:v]split=2[bg][fg];[bg]crop={cw}:{ch}:{x0}:{y0},"
              f"scale={blur_w}:{blur_h}:flags=fast_bilinear,boxblur=6:1,"
              f"scale={tw}:{th}:flags=fast_bilinear,setsar=1[back];"
              f"[fg]scale={tw}:{th}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[front];"
              "[back][front]overlay=(W-w)/2:(H-h)/2:shortest=1,setsar=1,subtitles=_caption.ass")
    if layout == "stacked":
        mode = "gaming facecam + gameplay (stacked)"
    elif fit == "contain":
        vf = (f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
              f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,setsar=1,subtitles=_caption.ass")
        mode = "contain (full frame)"
    elif layout == "split":
        panel_h = (th // 4) * 2
        pcw, pch, _, py = crop_for(W, H, tw, panel_h)
        positions = [max(0, min(W - pcw, round(W * c - pcw / 2))) for c in centers]
        vf = (f"[0:v]split=2[a][b];[a]crop={pcw}:{pch}:{positions[0]}:{py},"
              f"scale={tw}:{panel_h},setsar=1[top];[b]crop={pcw}:{pch}:{positions[1]}:{py},"
              f"scale={tw}:{panel_h},setsar=1[bottom];[top][bottom]vstack=inputs=2,"
              f"pad={tw}:{th}:0:0,setsar=1,subtitles=_caption.ass")
        mode = "split"
    elif needs_track and (not vertical or camera_mode != "stable") and framing and framing.reliable:
        init_x = max(0, min(int(round(framing.points[0][1] - cw / 2)), W - cw))
        vf = (f"sendcmd=f=_crop.cmd,crop={cw}:{ch}:{init_x}:{y0},"
              f"scale={tw}:{th},setsar=1,subtitles=_caption.ass")
        mode = "smart"
    elif vertical and (W > cw or H > ch):
        vf, mode = stable, "stable"
    else:
        vf = f"crop={cw}:{ch}:{x0}:{y0},scale={tw}:{th},setsar=1,subtitles=_caption.ass"
        mode = "full-frame"

    suffix = ".preview" if preview else ""
    prefix = sanitize(str(seg["id"])) + "-" if seg.get("id") else ""
    out = out_dir / (prefix + sanitize(seg.get("hook", "clip")) + suffix + ".mp4")
    if out.exists():
        out = out.with_name(f"{out.stem}-{uuid.uuid4().hex[:8]}{out.suffix}")
    def command(graph, destination):
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{local_start:.3f}", "-i", str(render_src)]
        if mark:
            w, h, x, y = mark.geometry(tw, th)
            cmd += ["-f", "image2", "-pattern_type", "none", "-i", str(mark.image)]
            base = (graph.removesuffix("subtitles=_caption.ass[v]") + "null[base]" if layout == "stacked"
                    else ("" if graph.startswith("[0:v]") else "[0:v]")
                    + graph.removesuffix(",subtitles=_caption.ass") + "[base]")
            graph = (base + f";[1:v]format=rgba,scale={w}:{h},setsar=1,"
                     f"colorchannelmixer=aa={mark.settings['opacity'] / 100:.2f}[mark];"
                     f"[base][mark]overlay=x={x}:y={y}:eof_action=repeat:format=auto,"
                     "setsar=1,subtitles=_caption.ass[v]")
        elif brand and brand.get("logo") and not signature_frame:
            width = max(20, round(brand["logo_width"] * tw / 1920))
            cmd += ["-i", brand["logo"]]
            base = (graph.removesuffix("[v]") if layout == "stacked"
                    else ("" if graph.startswith("[0:v]") else "[0:v]") + graph)
            graph = (base + f"[captioned];[1:v]scale={width}:-1[logo];"
                     "[captioned][logo]overlay=W-w-24:24:eof_action=repeat,setsar=1[v]")
        elif graph.startswith("[0:v]") and not graph.endswith("[v]"):
            graph += "[v]"
        if not burn_subtitles:
            graph = graph.replace(",subtitles=_caption.ass", "")
        if signature_frame:
            band_input = 2 if mark else 1
            for image in (signature_frame.header, signature_frame.footer):
                cmd += ["-f", "image2", "-pattern_type", "none", "-i", str(image)]
            graph = (("" if graph.startswith("[0:v]") else "[0:v]") + graph.removesuffix("[v]")
                     + f",pad={tw}:{canvas_h}:0:{top_band}[canvas];"
                     f"[{band_input}:v]scale={tw}:{top_band},setsar=1[header];"
                     f"[{band_input + 1}:v]scale={tw}:{bottom_band},setsar=1[footer];"
                     "[canvas][header]overlay=0:0:eof_action=repeat[branded_top];"
                     f"[branded_top][footer]overlay=0:{canvas_h - bottom_band}:eof_action=repeat,setsar=1[v]")
        cmd += ["-t", f"{dur:.3f}"]
        if graph.endswith("[v]"):
            cmd += ["-filter_complex_threads", "1", "-filter_complex", graph, "-map", "[v]", "-map", "0:a?"]
        else:
            cmd += ["-filter_threads", "1", "-vf", graph]
        if loudnorm:
            cmd += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
        if vertical:
            cmd += ["-r", str(rate), "-fps_mode", "cfr"]
        if timeout is not None or vertical:
            cmd += ["-threads", "2"]
        return cmd + ["-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p",
                      "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(destination)]

    with tempfile.TemporaryDirectory(prefix=".render-", dir=out_dir) as staging:
        temporary = Path(staging) / "video.mp4"

        def encode(graph):
            cmd = command(graph, temporary)
            paths = {arg: "<entrada local>" for idx, arg in enumerate(cmd) if idx and cmd[idx-1] == "-i"}
            paths[str(temporary)] = "<MP4 temporal>"
            print(">", " ".join(paths.get(arg, arg) for arg in cmd), flush=True)
            try:
                progress.update("encode")
                result = render_progress.encode(cmd, scratch, timeout or max(60, min(7200, dur * 12 + 60)),
                                                dur, progress)
                if result.stderr:
                    print(sanitized_stderr(result.stderr, paths), file=sys.stderr, flush=True)
            except subprocess.CalledProcessError as exc:
                print(sanitized_stderr(exc.stderr, paths) or "FFmpeg no devolvió stderr.", file=sys.stderr, flush=True)
                raise ffmpeg_failure(exc.stderr or "", exc.returncode, layout=mode if mode == "smart" else layout,
                                     mark=mark) from exc
            except subprocess.TimeoutExpired as exc:
                if exc.stderr:
                    print(sanitized_stderr(exc.stderr, paths), file=sys.stderr, flush=True)
                raise RenderFailure("FFmpeg superó el tiempo de render. Consulta el registro.") from exc
            except OSError as exc:
                raise RenderFailure("No se pudo iniciar FFmpeg. Comprueba su instalación.") from exc

        if burn_subtitles:
            ass = scratch / "_caption.ass"
            try:
                content = ass.read_text(encoding="utf-8-sig")
                if not all(section in content for section in ("[Script Info]", "[V4+ Styles]", "[Events]")):
                    raise ValueError("Encabezados ASS incompletos.")
            except (OSError, ValueError) as exc:
                print(f"ASS: {exc}", file=sys.stderr, flush=True)
                raise RenderFailure("No se pudo validar el archivo ASS. Consulta el registro.", "subtitles") from exc
        encode(vf)
        if motion is not None and not preview:
            progress.update("verification")
            source_spans = None

            def verify():
                nonlocal source_spans
                try:
                    evidence = smart_camera.delivery_check(temporary, rate, dur, timeout)
                    try:
                        spans = smart_camera.freeze_spans(temporary, 0, dur, timeout)
                    except subprocess.SubprocessError as exc:
                        print(sanitized_stderr(getattr(exc, "stderr", ""), {str(temporary): "<MP4 temporal>"}),
                              file=sys.stderr, flush=True)
                        raise smart_camera.DeliveryError("Control vertical: no se pudo decodificar el MP4 completo.") from exc
                except smart_camera.DeliveryError as exc:
                    diagnostic = out_dir / ".vertical-diagnostic"
                    try:
                        diagnostic.mkdir(exist_ok=True)
                        if diagnostic.resolve() == diagnostic:
                            temporary.replace(diagnostic / "last-rejected.mp4")
                            print("MP4 rechazado conservado en .vertical-diagnostic/last-rejected.mp4.", flush=True)
                    except OSError:
                        print("No se pudo conservar el MP4 rechazado.", flush=True)
                    alternative = "stable" if mode in {"smart", "split", "gaming facecam + gameplay (stacked)"} else None
                    raise RenderFailure(str(exc), alternative) from exc
                if spans and source_spans is None:
                    source_spans = smart_camera.freeze_spans(render_src, local_start, dur, timeout)
                excess = smart_camera.excess_freezes(spans, source_spans or [])
                if excess:
                    raise smart_camera.MotionError(
                        "Control de movimiento: pausas no explicadas por la fuente.", excess)
                return {**evidence, "freeze_spans": spans[:50], "source_freeze_spans": (source_spans or [])[:50],
                        "unexplained_freezes": []}

            try:
                motion.update(verify())
            except smart_camera.MotionError as exc:
                if mode not in {"smart", "split"}:
                    raise
                motion["fallback"] = {"reason": str(exc), "intervals": exc.intervals}
                encode(stable)
                progress.update("verification")
                mode = "stable"
                motion.update(verify())
            if mode == "stable" and camera_mode == "smart" and "fallback" not in motion:
                motion["fallback"] = {"reason": motion.get("reason", "encuadre sin sujeto verificable")}
            motion["applied"] = mode
        progress.check()
        staged = Path(staging) / out.name
        temporary.replace(staged)
        progress.update("metadata")
        srt = captions.write_srt(cap_words, start, start + dur, staged.with_suffix(".srt"), groups=groups)
        ass = scratch / "_caption.ass"
        track = None
        if not preview:
            if not burn_subtitles:
                caption_error = "Exportación autorizada sin subtítulos incrustados."
            from subtitle_quality import artifact_hash
            ass_output = staged.with_suffix(".ass")
            if burn_subtitles:
                shutil.copyfile(ass, ass_output)
            track = {
                "version": captions.TIMELINE_VERSION, "duration": dur,
                "words": sum(len(captions.cue_text(group).split()) for group in groups),
                "timing": {k: resolved[k] for k in captions.TIMING_FIELDS},
                "srt_hash": artifact_hash(srt), "ass_hash": artifact_hash(ass_output) if burn_subtitles else None,
                "media_hash": artifact_hash(staged), "tightened": bool(do_tighten), "burned_in": burn_subtitles,
            "watermark_applied": bool(mark),
                "render_options": {"style": style, "fit": fit, "layout": requested_layout, "centers": requested_centers,
                                   "camera_mode": camera_mode, "export_profile": export_profile,
                                   "caption": caption, "facecam_box": facecam_box, "facecam_height": facecam_height,
                                   **({"signature": {"template": signature.TEMPLATE_ID, "watermark": bool(mark)}}
                                      if signature_frame else {})},
                **(transcript_info or {}),
                **({"caption_error": caption_error} if caption_error else {}),
            }

        for tmpf in (scratch / "_caption.ass", scratch / "_crop.cmd"):
            try:
                tmpf.unlink()
            except OSError:
                pass

        if do_tighten:
            try:
                render_src.unlink()
            except OSError:
                pass

        def promote():
            progress.check()
            progress.update("write")
            for sidecar in (".srt", ".ass", ".png", ".meta.json"):
                item = staged.with_suffix(sidecar)
                if item.is_file():
                    item.replace(out.with_suffix(sidecar))
            staged.replace(out)

        if preview:
            promote()
            # drafts skip the poster and metadata - they exist to be eyeballed fast
            print(f"SAVED {out}  ({aspect} {tw}x{canvas_h} preview, {mode})", flush=True)
            progress.update("ready")
            return out

        # poster: a representative still saved next to the clip (<clip>.png)
        poster = staged.with_suffix(".png")
        project_cancel.run(["ffmpeg", "-y", "-ss", f"{dur * 0.4:.2f}", "-i", str(staged),
                        "-frames:v", "1", "-q:v", "2", str(poster)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
        meta_path = write_meta(seg, staged, track, motion)
        promote()
        progress.update("ready")

        print(f"SAVED {out}  ({aspect} {tw}x{canvas_h}, {mode})  "
              f"+ poster {poster.name} + {meta_path.name}", flush=True)
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir", nargs="?", default="work")
    ap.add_argument("--aspect", default=DEFAULT_ASPECT, choices=list(ASPECTS),
                    help="output aspect ratio (default 16:9)")
    ap.add_argument("--out", default=None,
                    help="output directory (default ./clips, or $CHOPIFY_OUT)")
    ap.add_argument("--style", default=DEFAULT_STYLE,
                    help="caption preset: " + ", ".join(CAPTION_STYLES))
    ap.add_argument("--tighten", action="store_true",
                    help="remove filler words (um, uh...) and long silences")
    ap.add_argument("--loudnorm", action="store_true",
                    help="normalize audio to -14 LUFS (platform standard)")
    ap.add_argument("--preview", action="store_true",
                    help="render fast 480p drafts (no poster/meta) to review "
                         "clip selection before the full render")
    ap.add_argument("--brand", help="path to reusable JSON brand preset")
    ap.add_argument("--camera-mode", choices=smart_camera.MODES, default="auto")
    ap.add_argument("--layout", choices=["single", "split"], default="single")
    ap.add_argument("--centers", type=float, nargs=2, default=[0.25, 0.75])
    add_caption_arguments(ap)
    args = ap.parse_args()
    from branding import load_brand
    brand = load_brand(args.brand) if args.brand else None
    try:
        caption = caption_from_args(args)
    except ValueError as exc:
        raise SystemExit(f"Caption: {exc}")

    workdir = Path(args.workdir).resolve()
    out_dir = (Path(args.out) if args.out else default_out()).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    transcript = json.loads((workdir / "transcript.json").read_text(encoding="utf-8-sig"))
    segments = json.loads((workdir / "segments.json").read_text(encoding="utf-8-sig"))
    words = transcript["words"]

    source = Path(transcript["video"])
    if not source.exists():
        cands = sorted(workdir.glob("source.*"))
        source = cands[0] if cands else source
    source = source.resolve()

    W, H = ffprobe_dims(source)
    print(f"Source {source} {W}x{H}; {len(segments)} clips -> {args.aspect} "
          f"(style={args.style}, tighten={args.tighten}) -> {out_dir}", flush=True)

    saved = []
    failed = 0
    for i, seg in enumerate(segments, 1):
        print(f"--- Clip {i}/{len(segments)}: "
              f"{seg.get('hook', '')[:60]!r} (overall {seg.get('overall')}) ---",
              flush=True)
        try:
            saved.append(str(render({**seg, "id": f"{i:03}"}, words, source, W, H, workdir, args.aspect,
                                    out_dir=out_dir, style=args.style,
                                    do_tighten=args.tighten, loudnorm=args.loudnorm,
                                    preview=args.preview, brand=brand,
                                    layout=args.layout, centers=args.centers, camera_mode=args.camera_mode,
                                    caption=caption)))
        except subprocess.CalledProcessError as e:
            print(f"ERROR rendering clip {i}: {e}", flush=True)
            failed += 1

    print("RENDERED", len(saved), "clips:", flush=True)
    for s in saved:
        print("  ", s, flush=True)
    if failed:
        raise SystemExit(1)


def stacked_graph(W, H, tw, th, facecam_height, box):
    """Return (filter graph fragment, ASS bottom margin) for facecam + gameplay."""
    fh = int(round(th * facecam_height)) // 2 * 2
    gh = th - fh
    if min(W, H, tw, fh, gh) < 2 or not all(math.isfinite(value) and 0 <= value <= 1 for value in box):
        raise RenderFailure("Las dimensiones de la composición vertical no son válidas.", "stable")
    fcw = max(2, int(round(box[2] * W)) // 2 * 2)
    fch = max(2, int(round(box[3] * H)) // 2 * 2)
    fcw, fch = min(W - W % 2, fcw), min(H - H % 2, fch)
    fcx = max(0, min((W - fcw) // 2 * 2, int(box[0] * W) // 2 * 2))
    fcy = max(0, min((H - fch) // 2 * 2, int(box[1] * H) // 2 * 2))
    gcw, gch, gcx, gcy = crop_for(W, H, tw, gh)
    graph = (f"[0:v]split=2[cam][game];"
             f"[cam]crop={fcw}:{fch}:{fcx}:{fcy},scale={tw}:{fh}[top];"
             f"[game]crop={gcw}:{gch}:{gcx}:{gcy},scale={tw}:{gh}[bottom];"
             f"[top][bottom]vstack=inputs=2[stack];"
             f"[stack]setsar=1,subtitles=_caption.ass[v]")
    return graph, th - fh - 12


DEFAULT_FACECAM_BOX = (0.0, 0.62, 0.26, 0.30)


def clamp_facecam_box(box):
    x, y, w, h = (max(0.0, min(1.0, float(v))) for v in box)
    x, y = min(x, 0.95), min(y, 0.95)
    return (round(x, 6), round(y, 6),
            round(max(0.05, min(w, 1 - x)), 6),
            round(max(0.05, min(h, 1 - y)), 6))


def _detect_facecam_box(frame_path, W, H, workdir, detector=None, allow_download=True):
    import cv2
    image = cv2.imread(str(frame_path))
    if image is None:
        raise ValueError("Could not read sampled frame")
    kind, det = detector or _make_detector(workdir, allow_download)
    ih, iw = image.shape[:2]
    if kind == "yunet":
        det.setInputSize((iw, ih))
        _, faces = det.detect(image)
        if faces is None or not len(faces):
            raise ValueError("No face found")
        best = max(faces, key=lambda f: f[2] * f[3])
        x, y, w, h = (float(best[0]), float(best[1]), float(best[2]), float(best[3]))
    else:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        faces = det.detectMultiScale(gray, 1.1, 5, minSize=(36, 36))
        if not len(faces):
            raise ValueError("No face found")
        x, y, w, h = (float(v) for v in max(faces, key=lambda f: f[2] * f[3]))
    pad = 0.25
    x, y = x - w * pad, y - h * pad
    w, h = w * (1 + 2 * pad), h * (1 + 2 * pad)
    return clamp_facecam_box((x / iw, y / ih, w / iw, h / ih)), None


def resolve_facecam_box(source, W, H, workdir, box, detector=None, allow_download=True, timeout=None):
    """Return (normalized box, warning). Explicit box wins; detection is best-effort."""
    if box is not None:
        return clamp_facecam_box(box), None
    if source is None:
        return DEFAULT_FACECAM_BOX, "No source for facecam detection; using default box."
    try:
        frame_path = Path(workdir) / "_facecam.jpg"
        project_cancel.run(["ffmpeg", "-y", "-v", "error", "-ss", "0.5",
                        "-i", str(source), "-frames:v", "1", str(frame_path)],
                       check=True, capture_output=True, timeout=timeout)
        return _detect_facecam_box(frame_path, W, H, workdir, detector, allow_download)
    except Exception as exc:
        return DEFAULT_FACECAM_BOX, f"Facecam detection failed ({exc}); using default box."


if __name__ == "__main__":
    configure_utf8()
    main()
