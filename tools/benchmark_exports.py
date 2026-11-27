"""Compare two checkouts on the same generated source and export settings."""
import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile


WORKER = """
import json, sys, time
from pathlib import Path
import clip_workspace, projects, publishing_calendar
from types import SimpleNamespace

root, media, length, profile = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
options = {'camera_mode': 'stable'}
if profile == 'fast':
    options['export_profile'] = profile
state = projects.create_project(root, str(media), options)
folder = root / state['id']
words = [{'word': w, 'start': t, 'end': t + .25}
         for t, w in ((.2,'Prueba'), (.55,'de'), (.9,'exportación.'))]
projects.atomic_json(folder/'transcript.json', {'duration': 20, 'video': str(media), 'words': words})
projects.atomic_json(folder/'segments.json', [{'start': 0, 'end': length, 'overall': 8, 'hook': 'Prueba'}])
start = time.perf_counter()
state = projects.run_project(folder, action='render')
fresh = time.perf_counter() - start
output = Path(state['last_outputs'][0])
service = SimpleNamespace(_clip=lambda *args: output)
calendar = publishing_calendar.Calendar(root, None, service, None)
clips = clip_workspace.Clips(root, calendar, service, None)
start = time.perf_counter()
same_output = clips.reexport(state['id'], output.name, '16:9') if profile == 'quality' else None
again = time.perf_counter() - start
print('BENCH ' + json.dumps({'fresh_s': fresh, 'same_clip_s': again if same_output is not None else None,
      'reused': same_output == output.name if same_output is not None else None, 'size': output.stat().st_size,
      'stages': state.get('automation_render_metrics', {}).get(output.name, {}).get('stages')}))
"""


def measure(checkout, workspace, media, duration, profile, repeat):
    readings = []
    for index in range(repeat):
        root = workspace / f"{checkout.name}-{duration}-{profile}-{index}"
        output = subprocess.run([sys.executable, "-c", WORKER, str(root), str(media),
                                 str(duration), profile], cwd=checkout, check=True,
                                capture_output=True, text=True, encoding="utf-8")
        readings.append(json.loads(next(line[6:] for line in output.stdout.splitlines() if line.startswith("BENCH "))))
    return {"duration_s": duration, "profile": profile, "fresh_median_s": statistics.median(r["fresh_s"] for r in readings),
            "same_clip_median_s": statistics.median(r["same_clip_s"] for r in readings) if profile == "quality" else None,
            "runs": readings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path, help="Path to the baseline Git checkout or archive")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if args.runs < 1 or args.runs > 10:
        parser.error("Use between 1 and 10 runs.")
    base = args.baseline.resolve()
    current = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="anqora-export-bench-") as tmp:
        work = Path(tmp)
        media = work / "same-source.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "20", "-c:v",
                        "libx264", "-threads", "2", "-c:a", "aac", str(media)], check=True)
        data = []
        for duration in (6, 18):
            for label, checkout, profile in (("baseline", base, "quality"), ("current", current, "quality"),
                                             ("current_fast", current, "fast")):
                row = measure(checkout, work, media, duration, profile, args.runs)
                data.append({"checkout": label, **row})
        print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
