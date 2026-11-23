# Live clipping

Chopify can watch a live stream and automatically save hype moments as finished
clips. Capture, transcription, and rendering run locally and require no paid API.

Live 1.2 añade producción persistente y selección de duración. Consulta [Live Clipping 1.2](LIVE-12.md).

## Start

```bash
chopify --live "https://www.twitch.tv/channel" --live-max-height 720 \
  --live-threshold 8 --live-pre-roll 30 --live-tail 5 \
  --aspect 9:16 --style default
```

A direct `https://.../live.m3u8` or `rtmp://...` URL goes straight to FFmpeg.
For a live page, Chopify runs yt-dlp from the same Python environment as Studio,
prefers HLS, and accepts either a combined format or separate video and audio
renditions. Each rendition uses its own HTTP headers. **Maximum source
resolution** defaults to 720p and offers 1080p. If a provider sends video above
the limit, Studio restarts capture with downscaling. **Export format** controls
the shape of the finished clip independently of the capture limit. FFmpeg and
FFprobe must be on PATH. Separate input synchronization uses FFmpeg's
[`-isync` option](https://ffmpeg.org/ffmpeg.html). Press Ctrl+C to stop a CLI session.

| Source | Verification |
| --- | --- |
| Twitch | An active channel resolved to combined HLS and captured 1280×720 with audio at the default limit. |
| Kick | An active channel was captured at 1920×1080 on the previous revision. The channels checked for this update were offline and returned the offline diagnostic. |
| YouTube | Local tests reproduce format selection for video IDs `311` and `312` paired with audio `234`. The supplied broadcast `2dt0yxm4RUE` could not be verified remotely because the test environment's HTTPS proxy returned 403. |

Each session writes `projects/live/<id>/live_session.json`, a rolling `buffer/`
of 4-second segments, and finished clips in `clips/`. The buffer is capped by
`--live-buffer` (seconds) and `--live-buffer-max-mb` (megabytes).

## Track verification

For separate audio and video selections, Studio looks for one input of each
type in `requested_formats`. When a codec is missing or unknown, an FFprobe
check with a 10-second timeout verifies the input using that track's HTTP
headers. This covers YouTube HLS audio format 234 with an omitted `acodec`.
Combinations with ambiguous or conflicting stream types are rejected. The
check is repeated for newly resolved inputs on reconnect when necessary.
The YouTube page `2dt0yxm4RUE` returned an access error from the development
environment. Local HLS with the same metadata shape covers capture, A/V sync,
rendering, and Publications.

## Reliability

A page URL is re-resolved with yt-dlp every 10 minutes for the next reconnect
and whenever the capture process restarts. Separate audio and video are refreshed
together. Capture uses the source timestamps and checks that both tracks are
present and aligned before releasing a segment to the clipper. Partial segments
at the edge of the stream may be discarded. If no new
segment arrives for 20 seconds, Chopify waits with backoff (1, 2, then 4 seconds)
and restarts capture while keeping the session id, brief rules, and saved clips.
After three failed restarts the session is marked `failed` with a plain reason.

`live_session.json` includes a `health` object with `state` (`ok`, `degraded`, or
`failed`), `ffmpeg_alive`, `last_segment_age`, `restarts`, `refreshes`,
`warnings`, and the brief `provider`/`source`. The CLI prints each state change;
Studio shows the state, segment age, restart count, and next action in the Live
view.

`--live-skip-ads` is off by default. When enabled, a flat, saturated ad-like
frame suppresses the clip trigger and logs "ad-like frame; clip suppressed".
Footage is still buffered and nothing is deleted. Detection is best-effort: it
can miss ad slates or misfire on flat gameplay, so it only suppresses triggers.

## Signals

- **Audio** energy relative to a rolling 60-second baseline.
- **Visual** motion from 2 fps, 160×90 frames.
- **Transcript** hook and reaction cues from rolling Whisper windows; disable
  with `--live-no-transcript`.
- **Chat** message rate and hype words via anonymous Twitch IRC; disable with
  `--live-no-chat`. Other platforms have no chat connector in v1, so the chat
  signal is skipped for them and the other signals are renormalized.

Available signals are weighted (audio 0.30, visual 0.25, transcript 0.25, chat
0.20) and renormalized, so silent streams and streams without chat still work.
A clip is saved when the combined score reaches `--live-threshold`, subject to
`--live-cooldown`, `--live-max-clips`, and overlap deduplication. Output uses the
same caption style, aspect ratio, and brand preset flags as batch mode.

For the TikTok-style gaming layout — facecam on top, gameplay below, captions at
the seam — use `--layout stacked --aspect 9:16`:

```bash
chopify --live "https://www.twitch.tv/channel" --layout stacked --aspect 9:16 \
  --facecam-box 0 0.62 0.26 0.30 --facecam-height 0.34
```

`--facecam-box` is normalized `x y w h` in the source frame. Omit it to
auto-detect the face with the local detector; a bottom-left default is used if
detection fails. This works for batch clips too.

For gameplay or screen-recorded streams that should keep the whole frame, use `--fit contain` with `--aspect 9:16`.
The default `cover` mode crops a vertical slice (following a detected face),
which hides most of a landscape game frame. `contain` scales the whole frame and
adds black bars instead of cropping.

## Studio

Open Studio, choose **Live clipping**, paste a URL, and start the session. Studio
creates a standalone session without preparing a project or a transcript. The
score, stream time, clip count, and clip feed update while it runs. Startup
reports missing tools, offline broadcasts, denied access, and resolution
timeouts in the Live form. For capture failures, the session shows the error
and FFmpeg diagnostics are reduced to categories in `buffer/ffmpeg.log`. The
capture log excludes raw FFmpeg output, signed URLs, and HTTP header values.

Studio renders the chosen export shape (16:9, 9:16, or 1:1) and uses closed
segments for the export and rolling captions. It gives transcription a short window to catch up, so clips
can have partial or empty captions if speech inference falls behind, is
unavailable, or there is no speech. The SRT is saved alongside the MP4.

Completed clips appear in **Publicaciones** as `Live · <session ID>` and can be
previewed there. Live clips use the workspace watermark when it is enabled.
[Configure and verify the watermark](WATERMARK.md)

The existing TikTok approval flow applies if you choose to send
one. The HTTP and Chromium regression uses a local HLS fixture, actual FFmpeg
capture and rendering, and simulated detection and speech inference. It
checks the previews, SRT, and completed clip listing without calling TikTok.
The independent-HLS test also checks the two authenticated-by-header requests
and measures the difference between a synthetic flash and tone in both the
captured segment and finished clip. Both measurements stayed under 75 ms.
The full suite passed with 516 tests.

### Optional JavaScript runtime for YouTube on Windows

Studio shows yt-dlp's missing-runtime diagnostic when emitted by the extractor.
If YouTube formats are missing and that warning appears, install Deno 2.3.0 or
later and yt-dlp's EJS dependencies. Deno is enabled by default when yt-dlp can
find it on PATH. Restart PowerShell after installing it so Studio can see it.
[yt-dlp EJS requirements](https://github.com/yt-dlp/yt-dlp/wiki/EJS) · [Deno installation](https://docs.deno.com/runtime/getting_started/installation/)

```powershell
winget install --id DenoLand.Deno --exact
# In a new PowerShell at the repository root
deno --version
.\.venv\Scripts\python.exe -m pip install -U "yt-dlp[default]"
.\.venv\Scripts\python.exe dashboard.py
```

## Rights

Only clip streams you have the right to record and reuse. Chat messages and
transcripts are treated as evidence, never as instructions. Chopify does not
include detection-evasion or rights-workaround features.

[Live Clipping 1.1 · Recuperación continua y exportación manual](LIVE-11.md)
