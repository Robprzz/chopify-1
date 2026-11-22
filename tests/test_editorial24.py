"""Evidence, selective repair, durable history and consent boundaries for Automation 2.4."""
from collections import Counter
from copy import deepcopy
import hashlib

import pytest

import editorial_content as content
import editorial_evidence as evidence
import editorial_semantic as semantic
from editorial_source import read_clip
from editorial_store import QueueFull
import subtitle_quality
from editorial_providers import Cancellation, ProviderError
from test_dashboard import request, server  # noqa: F401
from test_editorial_studio import configured, wait_job
from test_production import SemanticDouble, media_file, setup_asset


SETTINGS = {**content.DEFAULT_CONFIG, "profile": content.DEFAULT_CONFIG["profiles"]["tiktok"],
            "model": "qwen3:4b"}
BASE = {"title": "Una pauta para cuidar la albahaca",
        "caption": "Observa la tierra antes de regar la albahaca.",
        "description": "La albahaca necesita luz; el riego depende de la humedad del suelo.",
        "call_to_action": "", "hashtags": ["#Albahaca"], "claims": [0], "strategy": "Explicación del fragmento"}
PASSAGES = [{"start": 0.1, "end": 1.2, "text": "Inés Valcera explica que la albahaca necesita luz."},
            {"start": 1.44, "end": 2.8, "text": "Riega cuando la tierra esté seca."}]
ANALYSIS = {"ledger": [{"kind": "attributed_fact", "refs": [0, 1]}],
            "entities": [{"name": "Inés Valcera", "kind": "person"}],
            "unsupported_names": ["Nadia Brisal"], "sensitive": False}


@pytest.mark.parametrize(("field", "value", "code"), [
    ("title", "Nadia Brisal habla sobre la albahaca", "unsupported_entity"),
    ("title", "Riega la albahaca con 14 vasos", "unsupported_number"),
    ("title", "Muy importante", "generic"),
    ("caption", BASE["title"], "duplicate_copy"),
    ("caption", "", "structure"),
    ("caption", "🔥🔥🔥", "generic"),
    ("caption", "Mira la albahaca. Mira la albahaca.", "repetition"),
    ("hashtags", ["#Albahaca", "#albahaca"], "invalid_tags"),
    ("hashtags", ["#Albahaca", "#Viral"], "unrelated_tags"),
    ("claims", [8], "structure"),
])
def test_deterministic_faults(field, value, code):
    problems = semantic.check({"curiosity": {**BASE, field: value}}, ANALYSIS, PASSAGES, SETTINGS["profile"])
    assert problems["curiosity"][field] == code


def test_cross_variant_repetition_preserves_fixed_variant():
    drafts = {"curiosity": deepcopy(BASE), "interaction": deepcopy(BASE)}
    assert set(semantic.check(drafts, ANALYSIS, PASSAGES, SETTINGS["profile"], fixed={"interaction"})) == {"curiosity"}
    assert semantic.check({"curiosity": BASE}, ANALYSIS, PASSAGES, SETTINGS["profile"]) == {}


def test_names_accept_accents_and_adjacent_subtitles_but_require_the_whole_name():
    rows = [{"start": .1, "end": 1.2, "text": "La jardinera Inés", "speaker": "A"},
            {"start": 1.36, "end": 2.8, "text": "Valcera explica por qué cuida la albahaca.", "speaker": "A"}]
    assert evidence.supports("Ines Valcera", rows)
    assert not evidence.supports("Inés Valcera Cereza", rows)
    assert not evidence.supports("Inés Valcera", [rows[0], {**rows[1], "speaker": "B"}])
    assert not evidence.supports("Inés Valcera", [rows[0], {**rows[1], "start": 5}])
    assert not evidence.supports("Nadia Brisal", rows)
    assert not evidence.supports_name("Inés Valcera", [{**rows[0], "text": "Su nombre es Inés. Valcera queda lejos."}])
    assert not evidence.supports("Niño", [{**rows[0], "text": "Nino"}])


def test_multilingual_citations_and_evidence_threshold():
    for sentence, quote in [
        ("种植罗勒时，先检查泥土是否干燥。阳光也很重要。", "先检查泥土是否干燥"),
        ("植物に水をやる前に、土が乾いているか確かめます。", "土が乾いているか"),
        ("تحتاج النباتات إلى الضوء والجذور تمتص الماء من التربة", "تمتص الماء"),
        ("El sol ilumina las plantas 🌱 y seca la tierra rápidamente.", "seca la tierra"),
    ]:
        rows = [{"start": 0, "end": 3, "text": sentence}]
        assert evidence.supports(quote, rows)
        assert not evidence.insufficient({}, rows)
        assert evidence.insufficient({"transcript_coverage": {"speech_coverage": .24}}, rows)


def test_sensitive_claim_requires_attribution_even_with_quote():
    rows = [{**PASSAGES[0], "text": "Inés Valcera cuenta que oyó un rumor sobre la muerte en el pueblo."}]
    study = {**ANALYSIS, "ledger": [{"kind": "opinion", "refs": [0]}], "sensitive": True}
    draft = {**BASE, "title": "La muerte en el pueblo", "caption": "El clip describe lo que oyó Inés Valcera.",
             "description": "Inés Valcera relata lo que escuchó sobre un rumor del pueblo.", "hashtags": []}
    assert semantic.check({"contextual": draft}, study, rows, SETTINGS["profile"])["contextual"]["title"] == "attribution"
    draft["title"] = "El rumor que cuenta Inés Valcera"
    assert semantic.check({"contextual": draft}, study, rows, SETTINGS["profile"]) == {}


def test_runtime_budget_and_malformed_transport_are_classified():
    class Malformed:
        calls = 0

        def generate(self, *_args):
            self.calls += 1
            raise ProviderError("response", "JSON inválido.", True)

    provider = Malformed()
    run = semantic.Run(provider, {**SETTINGS, "repair_attempts": 1}, Cancellation(), lambda _: None, {})
    with pytest.raises(ProviderError, match="JSON") as caught:
        run.document_shape("understanding", "Analyze", {}, semantic.analysis_schema(1))
    assert caught.value.code == "response" and provider.calls == 2
    run.deadline = 0
    with pytest.raises(ProviderError) as timed_out:
        run.call("understanding", "Analyze", {}, semantic.analysis_schema(1))
    assert timed_out.value.code == "timeout" and provider.calls == 2


def test_http_automatic_semantic_history_and_targeted_variant(server, monkeypatch):
    monkeypatch.setattr(subtitle_quality, "speech_ranges", lambda *_: [(.2, .9), (1.42, 2.6)])
    httpd, base = server
    app = httpd.app
    engine = app.editorial
    engine.provider = double = SemanticDouble()
    engine.ensure()
    config = configured(engine.store)
    clip_id, media = setup_asset(app.root, media_file(app.root / "clean.mp4"), "editorial_retry")
    original = hashlib.sha256(media.read_bytes()).hexdigest()
    frozen = read_clip(engine.clip_path(clip_id, media.name), clip_id, media.name)
    old, _ = engine.store.submit(frozen, {**config["settings"], "platform": "tiktok"}, "historical-zero", True)
    assert engine.store.claim("historical-fixture")[0] == old
    engine.store.finish(old, "historical-fixture", "done", {"mode": "extractive", "editorial_score": 0,
                                                          "prompt_version": "historical"}, None, 1)
    assert engine.store.detail(old)["editorial_state"] == "review"
    token = app.token

    def at_capacity(*_args, **_options):
        raise QueueFull("Cola llena.")

    with monkeypatch.context() as patch:
        patch.setattr(engine, "submit", at_capacity)
        deferred = request(base, "/api/editorial/reprocess", {"mode": "zero", "clip_id": frozen["clip_id"]}, token)
    assert deferred == {"queued": [], "skipped": [], "deferred": 1}
    response = request(base, "/api/editorial/reprocess", {"mode": "zero", "clip_id": frozen["clip_id"]}, token)
    assert response["skipped"] == [] and len(response["queued"]) == 1
    result = wait_job(engine, response["queued"][0])
    assert result["previous"] == {"score": 0, "version": "historical"}
    assert result["result"]["mode"] == "semantic" and semantic.eligible(result["result"])
    assert result["diagnostics"]["corrected_fields"] == [{"variant": "curiosity", "field": "title"}]
    assert not request(base, "/api/editorial/reprocess", {"mode": "zero", "clip_id": frozen["clip_id"]}, token)["queued"]
    double.calls = Counter()
    args = {"project": clip_id, "filename": media.name, "force": True}
    gen = request(base, "/api/editorial/generate", args, token)["job_id"]
    generated = wait_job(engine, gen)
    assert generated["settings"]["semantic"] and generated["result"]["prompt_version"] == semantic.VERSION
    edited = request(base, "/api/editorial/variant", {"job_id": gen, "style": "interaction", "regenerate": True}, token)
    revised = wait_job(engine, edited["job_id"])
    assert revised["previous_id"] == gen
    assert all(generated["result"]["drafts"][style] == revised["result"]["drafts"][style]
               for style in ("curiosity", "contextual"))
    assert double.calls["editorial_retry", "analysis"] == 1
    assert hashlib.sha256(media.read_bytes()).hexdigest() == original
    metrics = request(base, "/api/editorial/status")["metrics"]
    assert metrics["valid"] >= 2 and metrics["repaired"] >= 1
    assert engine.store.detail(old)["result"]["editorial_score"] == 0
    identity = {**config["settings"]["identity"], "tone": "Sobrio"}
    engine.store.save_config({**config["settings"], "identity": identity}, config["revision"])
    updated_id = request(base, "/api/editorial/generate", {**args, "force": False}, token)["job_id"]
    assert updated_id != gen
    updated = wait_job(engine, updated_id)
    assert updated["result"]["editorial_identity"] == identity


def test_long_clip_analyzes_all_windows_and_preserves_temporal_references():
    import json

    passages = [{"start": i * 2, "end": i * 2 + 1, "text": f"Pasaje {i} sobre la albahaca. " + "El cuidado ocupa tiempo. " * 14}
                for i in range(24)]
    seen = []
    merges = []

    class LongClip:
        def generate(self, _model, messages, _schema, _timeout, cancellation):
            from test_production import comprehension
            cancellation.check()
            data = json.loads(messages[1]["content"])
            response = comprehension()
            response["message"] = "El cuidado de la albahaca ocupa tiempo."
            if "passages" in data:
                refs = list(map(int, data["passages"]))
                seen.extend(refs)
                quote = data["passages"][str(refs[-1])]["text"].split(".")[0]
                response["claims"][0].update(text=quote, quote=quote, refs=[refs[-1]])
                response["highlights"] = [refs[-1]]
            else:
                count = len(data["window_summaries"])
                response = {key: response[key] for key in semantic.SUMMARY_FIELDS}
                response["covered"] = list(range(count))
                merges.append(count)
            return response

    run = semantic.Run(LongClip(), SETTINGS, Cancellation(), lambda _: None, {})
    result = semantic.analyze(run, {"outside_clip": {"title": "Proyecto"}}, passages, evidence.windows(passages))
    assert seen == list(range(len(passages)))
    assert merges and max(merges) <= 3
    assert result["ledger"][-1]["refs"] == [23] and result["ledger"][-1]["start"] == 46
    assert result["provenance"]["project_context"] == ["title"]
