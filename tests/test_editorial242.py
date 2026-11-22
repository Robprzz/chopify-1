"""Partial reconstruction from the reported shape; no transcript of the real clip."""
import json

import pytest
import editorial_semantic as engine
from editorial_providers import Cancellation, ProviderError
from editorial_registry import findings, registry
from test_editorial241 import SETTINGS, passages
from test_production import comprehension


ROWS = passages("El locutor saluda a su audiencia.",
                "El locutor habla de la investigación de Julia Salcedo y la torre Vental 7.",
                "En otro caso se investiga el posible envenenamiento de una madre y su hijo.")
CONTEXT = {"title": "Julia Salcedo y la torre Vental 7", "after": "Se menciona a Elena Palomar."}
NAMES = {"Julia Salcedo", "Vental 7", "Elena Palomar"}


def test_registry_separates_origins_and_attributes_events_to_the_same_clause():
    entries = registry(ROWS, CONTEXT)
    assert entries == registry(ROWS, dict(reversed(list(CONTEXT.items()))))
    assert entries["context:after"]["origin"] == "surrounding_transcript"
    assert entries["context:title"]["origin"] == "video_metadata"
    out = findings("caption", "La voz dice que Julia Salcedo fue envenenada.", ROWS, {1, 2}, entries, NAMES)
    assert any(row["reason"] == "event_and_subject_not_linked" for row in out)
    out = findings("caption", "La voz habla de un envenenamiento.", ROWS, {2}, entries, NAMES)
    assert any(row["reason"] == "source_is_uncertain" for row in out)
    assert not findings("caption", "La voz habla de un posible envenenamiento.", ROWS, {2}, entries, NAMES)


def test_scope_and_auxiliary_only_are_distinct_diagnostics():
    entries = registry(ROWS, CONTEXT)
    issues = findings("theme", "Julia Salcedo y Elena Palomar", ROWS, {0}, entries, NAMES)
    assert {(item["subject"], item["reason"]) for item in issues} == {
        ("Julia Salcedo", "outside_selected_evidence"), ("Elena Palomar", "auxiliary_only")}
    assert all(item["field"] == "theme" and item["evidence"] and item["suggestion"] for item in issues)


class ReplayedModel:
    def __init__(self, extra="", patch="stable"):
        self.extra, self.patch, self.calls = extra, patch, []

    def generate(self, _model, messages, _schema, _timeout, cancellation):
        cancellation.check()
        data = json.loads(messages[-1]["content"])
        self.calls.append(data)
        heading = "Investigación de Julia Salcedo y la torre Vental 7"
        message = "El locutor habla de la investigación de Julia Salcedo y la torre Vental 7."
        if "passages" in data:
            response = comprehension()
            response.update(theme=heading + self.extra, message=message + self.extra, secondary=[],
                            claims=[{"evidence_id": "p0.0", "kind": "attributed_fact"}],
                            entities=[{"name": "Julia Salcedo", "kind": "person", "refs": [0]}])
            return response
        assert "theme_repair" in data
        values = {**data["current_summary"], "theme": heading, "message": message}
        if self.patch == "repeat":
            # Shape of the observed faulty response. No verbatim transcript was supplied.
            return {**data["current_summary"], "focus": {"quote": "cita no documentada"}}
        return {**values, "evidence_ids": ["p1.0" if self.patch == "stable" else "context:after"]}


def analyze(double):
    metrics = {}
    run = engine.Run(double, SETTINGS, Cancellation(), lambda _: None, metrics)
    return engine.analyze(run, {"outside_clip": CONTEXT}, ROWS, engine.windows(ROWS)), metrics


def test_attested_name_recovers_scope_without_reasking_model():
    result, metrics = analyze(ReplayedModel())
    assert result["topic_verified"] and result["topic_refs"] == [1]
    assert metrics["model_calls"] == 1 and metrics["theme_repair_calls"] == 0
    assert "p1.0" in metrics["theme_evidence_recovered"]
    assert result["entities"][0]["refs"] == [1]


def test_repair_selects_id_without_repeating_a_quote():
    result, metrics = analyze(ReplayedModel(" con Elena Palomar"))
    assert result["topic_verified"] and "Elena Palomar" not in result["message"]
    assert metrics["repair_trace"][0]["model_response"]["evidence_ids"] == ["p1.0"]


@pytest.mark.parametrize("patch,error", [("repeat", "focus"), ("bad-id", "evidence_ids")])
def test_identical_bad_focus_and_unoffered_ids_do_not_become_evidence(patch, error):
    extra = " con Elena Palomar"
    model = ReplayedModel(extra, patch)
    if patch == "bad-id":
        # The candidate remains unsupported even though the reply contains an ID.
        model.patch = "repeat"
        generate = model.generate
        def replace(*args):
            value = generate(*args)
            if "focus" in value:
                value.pop("focus")
                value["evidence_ids"] = ["context:after"]
            return value
        model.generate = replace
    result, metrics = analyze(model)
    assert result["topic_verified"]
    assert "Elena Palomar" not in result["message"] and "Elena Palomar" not in result["theme"]
    assert "p1.0" == metrics["conservative_reformulation"]
    assert metrics["theme_repair_calls"] == 1
    assert error in metrics["repair_trace"][0]["schema_errors"]
    assert metrics["repair_stops"]["theme"]["reason"] == "repeated_repair"


def test_window_rejects_unknown_evidence_id_as_a_contract_error():
    class Unknown(ReplayedModel):
        def generate(self, *args):
            value = super().generate(*args)
            value["claims"][0]["evidence_id"] = "p99.9"
            return value
    with pytest.raises(ProviderError) as caught:
        analyze(Unknown())
    assert caught.value.code == "response"


def test_recovered_topic_reaches_three_independently_reviewed_variants():
    class EditorialDouble(ReplayedModel):
        def generate(self, model, messages, schema, timeout, cancellation):
            data = json.loads(messages[-1]["content"])
            if "requested_angles" in data:
                assert "Elena Palomar" not in str(data)
                claim = next(item["id"] for item in data["available_claims"] if item.get("evidence_id") == "p1.0")
                copy = [
                    ("¿Qué menciona el locutor sobre Julia Salcedo?", "La investigación se comenta junto a una referencia a la torre.",
                     "El fragmento recoge cómo introduce el locutor la investigación de Julia Salcedo."),
                    ("Preguntas sobre la investigación", "¿Qué detalle de la torre destaca el locutor?",
                     "La voz habla de la investigación y menciona la torre Vental 7."),
                    ("La torre Vental 7 en el relato", "El fragmento sitúa la mención a la torre dentro de la charla.",
                     "El locutor comenta la investigación de Julia Salcedo en este tramo del diálogo."),
                ]
                return {style: {"title": title, "caption": caption, "description": desc, "call_to_action": "",
                                "hashtags": ["#Investigación"], "claims": [claim], "strategy": "Centrado en el diálogo."}
                        for style, (title, caption, desc) in zip(engine.legacy.STYLES, copy)}
            if "variants_to_review" in data:
                assert all("envenenamiento" not in str(entry) for entry in data["variants_to_review"].values())
                review = {key: 8 for key in engine.CRITERIA}
                return {style: {**review, "grounded": True, "sensational": False, "uncertainty": "low", "fields": []}
                        for style in data["variants_to_review"]}
            return super().generate(model, messages, schema, timeout, cancellation)

    source = {"clip_id": "partial-reconstruction", "excerpts": ROWS, "original_score": 8, "outside_clip": CONTEXT}
    result = engine.generate(EditorialDouble(" con Elena Palomar"), source, SETTINGS, Cancellation(), lambda _: None)
    assert sum(variant["valid"] for variant in result["variants"]) == 3
    assert result["diagnostics"]["model_calls"] == 4
