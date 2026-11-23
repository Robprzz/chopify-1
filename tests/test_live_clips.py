import pytest

import live_clips


def test_plan_window_defaults():
    start, end = live_clips.plan_window(
        100.0, 300, 30, 5, 15, 300, 110.0)
    assert start == 70.0
    assert end == 105.0


def test_plan_window_respects_buffer():
    start, end = live_clips.plan_window(1000.0, 60, 120, 5, 15, 300, 1010.0)
    assert start == 945.0
    assert end == 1005.0


def test_plan_window_min_length():
    start, end = live_clips.plan_window(10.0, 300, 5, 0, 15, 300, 12.0)
    assert start == 0.0
    assert end == 10.0


def test_plan_window_max_length():
    start, end = live_clips.plan_window(500.0, 600, 300, 60, 15, 120, 560.0)
    assert end - start == 120.0


def test_remap_words():
    words = [{"word": "hi", "start": 12.0, "end": 12.5, "score": 1.0},
             {"word": "bye", "start": 13.0, "end": 13.5},
             {"word": "late", "start": 20.0, "end": 20.2}]
    result = live_clips.remap_words(words, 10.0, 14.0)
    assert result == [
        {"word": "hi", "start": 2.0, "end": 2.5, "score": 1.0},
        {"word": "bye", "start": 3.0, "end": 3.5}]


def test_writer_forwards_facecam_options(tmp_path, monkeypatch):
    import projects

    options = projects.validate_options({
        "layout": "stacked", "aspect": "9:16",
        "facecam_box": [0, 0.6, 0.3, 0.3], "facecam_height": 0.4})
    segment = tmp_path / "segment00000000.ts"
    segment.write_bytes(b"x")
    writer = live_clips.LiveClipWriter(tmp_path / "s")
    monkeypatch.setattr(live_clips, "stitch", lambda p, d, runner=None: d)
    seen = {}

    def fake_render(seg, words, source, W, H, workdir, aspect, **kw):
        seen.update(kw)

    writer.write(live_clips.Trigger(20.0, 9.0, {}), options,
                 {"width": 640, "height": 360}, [], [segment],
                 renderer=fake_render)
    assert seen["layout"] == "stacked"
    assert seen["facecam_box"] == [0, 0.6, 0.3, 0.3]
    assert seen["facecam_height"] == 0.4


def test_writer_passes_fit_to_renderer(tmp_path, monkeypatch):
    import projects

    options = projects.validate_options({"fit": "contain"})
    segment = tmp_path / "segment00000000.ts"
    segment.write_bytes(b"x")
    writer = live_clips.LiveClipWriter(tmp_path / "session")
    monkeypatch.setattr(live_clips, "stitch", lambda paths, dest, runner=None: dest)
    seen = {}

    def fake_render(seg, words, source, W, H, workdir, aspect, **kwargs):
        seen.update(kwargs)

    record = writer.write(live_clips.Trigger(20.0, 9.0, {}), options,
                          {"width": 640, "height": 360}, [], [segment],
                          renderer=fake_render)
    assert record.error is None
    assert seen["fit"] == "contain"


def test_stitch_builds_concat_list(tmp_path):
    paths = [tmp_path / f"segment{i:08d}.ts" for i in range(2, 5)]
    for p in paths:
        p.write_bytes(b"x")
    calls = []
    entries = []

    def inspect(cmd, **kwargs):
        calls.append(cmd)
        entries.append(live_clips.Path(cmd[cmd.index("-i") + 1]).read_text())

    live_clips.stitch(paths, paths[-1].parent / "window.mp4", runner=inspect)
    assert calls and "concat" in calls[0]
    assert "segment00000002.ts" in entries[0]
    assert "segment00000004.ts" in entries[0]
    assert not (tmp_path / "window.concat.txt").exists()


def test_archive_keeps_a_manual_clip_after_it_leaves_session_summary(tmp_path):
    import projects
    root = tmp_path / "live" / "abcdef123456"
    (root / "clips").mkdir(parents=True)
    (root / "records").mkdir()
    ident = "112233445566"
    media = root / "clips" / (ident + "-clip.mp4")
    media.write_bytes(b"fixture")
    record = {"id": ident, "manual": True, "score": None, "files": {"mp4": str(media)}}
    projects.atomic_json(root / "records" / (ident + ".json"), record)
    projects.atomic_json(root / "live_session.json", {"triggers": []})
    assert live_clips.finished_record({"triggers": []}, media) == record
    assert list(live_clips.live_exports(tmp_path))[0]["filename"] == media.name
