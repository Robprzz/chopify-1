"""Fictitious fixture with a nominal reference outside the drafted claim selection."""
import json

import editorial_semantic as engine
from editorial_providers import Cancellation
from editorial_registry import registry
from test_editorial241 import SETTINGS, passages
from test_production import comprehension


ROWS = passages("El locutor pregunta por las grabaciones de las cámaras.",
                *["Se menciona otro asunto del programa."] * 9,
                "La voz relata el posible envenenamiento de otra persona.",
                "La locutora menciona a Julia Salcedo durante la investigación.")
STYLE = "curiosity"
DRAFT = {"title": "Las grabaciones y el caso de Julia Salcedo",
         "caption": "¿Qué interrogantes plantea el locutor sobre las cámaras?",
         "description": "El fragmento recoge una pregunta concreta sobre las grabaciones.",
         "hashtags": ["#Grabaciones", "#GrabacionesFalsas", "#Viral", "#JuliaSalcedo", "#grabaciones"],
         "claims": [0], "strategy": "Una pregunta sobre un detalle citado.", "call_to_action": ""}


def analysis():
    return {**comprehension(), "topic_verified": True, "theme": "Las grabaciones y Julia Salcedo",
            "message": "El fragmento plantea preguntas sobre las cámaras.",
            "ledger": [{"id": 0, "kind": "attributed_fact", "quote": ROWS[0]["text"], "refs": [0],
                        "ambiguity": "", "start": 0, "end": 2}],
            "topic_refs": [0], "evidence_registry": list(registry(ROWS, {"title": "Dana Arce"}).values()),
            "entities": [{"name": "Julia Salcedo", "kind": "person"}],
            "unsupported_names": ["Dana Arce"], "sensitive": True}


def test_name_only_recovery_cannot_support_unrelated_sensitive_claim():
    study = analysis()
    entries = {row["id"]: row for row in study["evidence_registry"]}
    item = {**DRAFT, "caption": "El locutor afirma que Julia Salcedo sufrió un envenenamiento.",
            "hashtags": [], "claims": [0, 1]}
    study["ledger"].append({"kind": "hypothesis", "refs": [10], "quote": ROWS[10]["text"]})
    names = {STYLE: engine._name_references(item, study, ROWS, entries)}
    assert names[STYLE]["Julia Salcedo"]["id"] == "p11.0"
    problems = engine.check({STYLE: item}, study, ROWS, SETTINGS["profile"], name_references=names)
    assert problems[STYLE]["caption"] == "event_attribution"


class Reviewer:
    def __init__(self, creative=8):
        self.calls = []
        self.creative = creative

    def generate(self, model, messages, schema, timeout, cancellation):
        data = json.loads(messages[1]["content"])
        self.calls.append(data)
        if "repair" in data:
            return {STYLE: {"description": DRAFT["description"]}}
        reviewed = data["variants_to_review"][STYLE]
        assert reviewed["name_only_references"][0]["id"] == "p11.0"
        assert reviewed["clip_passages"] == ROWS[:1]
        report = {name: 8 for name in engine.CRITERIA}
        report.update({name: self.creative for name in engine.CREATIVE_CRITERIA})
        return {STYLE: {**report, "grounded": True, "sensational": False, "uncertainty": "low", "fields": []}}


def execute(provider, **changes):
    seed = {"analysis": analysis(), "drafts": {STYLE: {**DRAFT, **changes}}, "quality_review": {}}
    return engine.generate(provider, {"clip_id": "editorial-fixture", "original_score": 8, "excerpts": ROWS},
                           SETTINGS, Cancellation(), lambda _: None, seed=seed)


def test_draft_recovers_name_and_removes_tags_without_extra_model_calls():
    provider = Reviewer()
    result = execute(provider)
    assert len(provider.calls) == 1
    variant = result["variants"][0]
    assert variant["valid"]
    assert variant["title"] == DRAFT["title"] and variant["caption"] == DRAFT["caption"]
    assert variant["hashtags"] == ["#Grabaciones", "#JuliaSalcedo"]
    assert variant["fidelity"] == 8 and variant["creative_quality"] == 8


def test_repair_gets_exact_entity_and_allowed_variant_evidence():
    provider = Reviewer()
    result = execute(provider, description="El fragmento analiza las grabaciones de Dana Arce.")
    repair = provider.calls[0]
    rejection = repair["rejections"][0]
    assert rejection["subject"] == "Dana Arce" and rejection["field"] == "description"
    assert rejection["reason"] == "auxiliary_only" and rejection["suggestion"]
    assert repair["allowed_evidence"][STYLE]["claims"][0]["id"] == 0
    assert repair["allowed_evidence"][STYLE]["name_only"][0]["id"] == "p11.0"
    assert result["variants"][0]["valid"] and len(provider.calls) == 2


def test_factual_but_creatively_weak_variant_is_never_recommended():
    provider = Reviewer(creative=6)
    seed = {"analysis": analysis(), "drafts": {STYLE: DRAFT.copy()}, "quality_review": {}}
    result = engine.generate(provider, {"clip_id": "fixture", "original_score": 8, "excerpts": ROWS},
                             {**SETTINGS, "repair_attempts": 0}, Cancellation(), lambda _: None, seed=seed)
    assert result["recommended_variant"] is None
    assert result["variants"][0]["fidelity"] == 8
    assert result["variants"][0]["creative_quality"] == 6


def test_cosmetic_repair_does_not_retry_the_same_unsupported_name():
    class Cosmetic(Reviewer):
        def generate(self, model, messages, schema, timeout, cancellation):
            data = json.loads(messages[1]["content"])
            self.calls.append(data)
            copy = data["current_proposals"][STYLE]["description"]
            return {STYLE: {"description": copy.upper() + "!"}}

    provider = Cosmetic()
    result = execute(provider, description="El fragmento analiza las grabaciones de Dana Arce.")
    assert result["recommended_variant"] is None
    assert len(provider.calls) == 1
    assert result["diagnostics"]["repair_stops"]["copy"]["reason"] == "repeated_repair"
