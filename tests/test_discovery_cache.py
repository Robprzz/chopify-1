"""Regression coverage for the shared, resumable scoring stages."""
import pytest

import projects
import score_clips
import video_types


@pytest.fixture
def cached_project(tmp_path, monkeypatch):
    source = "https://example.com/gameplay"
    path = tmp_path / "project"
    projects.init_project(path, source, {"min_score": 0, "min_len": 4, "max_len": 20})
    video = path / "source.mp4"
    calls = []

    def download(*args):
        calls.append("download")
        video.write_bytes(b"video fixture")
        return video

    def transcribe(video, workdir, *args, **kwargs):
        calls.append("transcribe")
        projects.atomic_json(workdir / "transcript.json", {
            "video": str(video), "source": source, "duration": 30,
            "words": [{"word": "Victoria.", "start": 0, "end": 8},
                      {"word": "Derrota.", "start": 20, "end": 28}],
        })

    monkeypatch.setattr(projects.dt, "resolve_source", download)
    monkeypatch.setattr(projects.dt, "transcribe", transcribe)
    monkeypatch.setattr(projects.ingest, "probe_source", lambda source: {
        "url": True, "duration": 30, "estimated_size": 10,
    })
    monkeypatch.setattr(video_types, "detect_from_transcript", lambda *args: {
        "type": "gaming", "needs_confirmation": False,
    })
    monkeypatch.setattr(score_clips, "source_evidence",
                        lambda *args: (None, "mixed", 0.5, 1.0, False, None))
    return path, calls


def update_options(path, **options):
    state = projects.read_project(path)
    state["options"].update(options)
    projects.save_project(path, state)


def prepared_starts(path, **kwargs):
    projects.run_project(path, action="prepare", **kwargs)
    return [item["start"] for item in projects.read_json(path / "segments.json")]


def test_switching_modes_replaces_active_cache_and_reuses_media(cached_project, monkeypatch):
    import semantic

    path, calls = cached_project
    monkeypatch.setattr(semantic, "search", lambda *args: [{"index": 0, "similarity": 1.0}])
    assert prepared_starts(path) == [0, 20]
    media = {p: projects.file_identity(p) for p in (
        path / "source.mp4", path / "transcript.json")}

    for settings, expected in [
        ({"search": "derrota"}, [20]),
        ({"search": None, "semantic": "ganar una partida"}, [0]),
        ({"semantic": None}, [0, 20]),
        ({"search": "derrota"}, [20]),
    ]:
        update_options(path, **settings)
        assert prepared_starts(path) == expected
        candidates = projects.file_identity(path / "candidates.json")
        assert prepared_starts(path) == expected
        assert projects.file_identity(path / "candidates.json") == candidates

    assert calls == ["download", "transcribe"]
    assert {p: projects.file_identity(p) for p in media} == media


def test_discovery_repair_reselects_even_with_an_existing_selection(cached_project):
    path, calls = cached_project
    assert prepared_starts(path) == [0, 20]
    projects.atomic_json(path / "segments.json", [])
    assert prepared_starts(path) == []  # A saved manual edit survives a cache hit.

    (path / "candidates.json").unlink()
    assert prepared_starts(path) == [0, 20]
    discovery = projects.file_identity(path / "candidates.json")
    (path / "selection.json").write_text("{", encoding="utf-8")
    assert prepared_starts(path) == [0, 20]
    assert projects.file_identity(path / "candidates.json") == discovery

    score_clips.save_candidates(path, [], {})  # A standalone scorer replaced the file.
    assert prepared_starts(path) == [0, 20]
    assert calls == ["download", "transcribe"]


def test_editing_transcript_refreshes_candidates_without_retranscribing(cached_project):
    path, calls = cached_project
    assert prepared_starts(path) == [0, 20]
    edited = projects.edit_transcript(path, [
        {"word": "Remontada.", "start": 10, "end": 18},
    ])

    assert prepared_starts(path) == [10]
    assert projects.read_json(path / "transcript.json") == edited
    assert calls == ["download", "transcribe"]


def test_retry_after_partial_search_write_rebuilds_previous_mode(cached_project, monkeypatch):
    path, calls = cached_project
    assert prepared_starts(path) == [0, 20]
    update_options(path, search="derrota")

    def interrupted_run(workdir, **options):
        projects.atomic_json(workdir / "segments.json", [])
        raise OSError("interrupted before writing selection metadata")

    monkeypatch.setattr(score_clips, "run", interrupted_run)
    with pytest.raises(OSError, match="interrupted"):
        prepared_starts(path)

    update_options(path, search=None)
    assert prepared_starts(path) == [0, 20]
    assert calls == ["download", "transcribe"]


def test_version_saved_lengths_and_recipe_invalidate_discovery(cached_project, monkeypatch):
    path, calls = cached_project
    # Studio invokes the CLI with no explicit length flags.
    assert prepared_starts(path, explicit_keys=set()) == [0, 20]

    update_options(path, min_len=10)
    assert prepared_starts(path, explicit_keys=set()) == []
    first = projects.read_project(path)["cache"]["discovery"]

    # The effective lengths are overridden, so the changed profile must key discovery.
    monkeypatch.setitem(video_types.FORMATS["gaming"], "profile", "talk")
    assert prepared_starts(path, explicit_keys=set()) == []
    second = projects.read_project(path)["cache"]["discovery"]
    assert second != first

    monkeypatch.setattr(score_clips, "DISCOVERY_VERSION", score_clips.DISCOVERY_VERSION + 1)
    assert prepared_starts(path, explicit_keys=set()) == []
    assert projects.read_project(path)["cache"]["discovery"] != second
    assert calls == ["download", "transcribe"]


def test_selection_controls_reuse_deterministic_candidates(cached_project, monkeypatch):
    path, calls = cached_project
    assert prepared_starts(path) == [0, 20]
    segments = path / "segments.json"
    original = projects.read_json(segments)
    discovery = projects.file_identity(path / "candidates.json")
    monkeypatch.setattr(score_clips, "_ollama_chat", lambda *args: [
        {"index": 0, "adjustment": 1.0},
    ])

    update_options(path, llm="local-test-model")
    assert prepared_starts(path) == [0, 20]
    assert projects.read_json(segments) != original
    update_options(path, llm=None)
    assert prepared_starts(path) == [0, 20]
    assert projects.read_json(segments) == original

    update_options(path, min_score=0.1, max_clips=1)
    assert len(prepared_starts(path)) == 1
    assert projects.file_identity(path / "candidates.json") == discovery
    assert calls == ["download", "transcribe"]
