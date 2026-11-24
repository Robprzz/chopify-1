"""Automation 2.4.1 topic, editorial voice, numeric grounding and repair regressions."""
import json

import pytest

import editorial_content as content
from editorial_evidence import number_uses, numeric_issues
from editorial_identity import DEFAULT_IDENTITY, perspective_error
from editorial_providers import Cancellation, ProviderError
import editorial_semantic as semantic
from editorial_topics import TopicIndex
from test_dashboard import request, running_server
from test_production import comprehension
from test_studio_navigation import chromium_page  # noqa: F401


SETTINGS = {**content.DEFAULT_CONFIG, "profile": content.DEFAULT_CONFIG["profiles"]["tiktok"],
            "model": "qwen3:4b", "repair_attempts": 3}
BUILDING = "Edificio Cedral 7"
QUOTE = f"Una persona relata que visitó el {BUILDING} para conocer el lugar"
SENSITIVE = "El testimonio distingue lo que observó de los rumores sobre un fallecimiento sin causa confirmada"
METADATA = f"{BUILDING} y el caso de Nelia Forjar"


def passages(*text):
    return [{"start": 2.0 * index, "end": 2.0 * index + 1.8, "text": value}
            for index, value in enumerate(text)]


@pytest.mark.parametrize("greeting", ["Hola, buenos días, ¿qué tal, hermano?", "Muy importante. Qué tal, hermano?",
                                      "Bueno, pues, o sea, ya tú sabes.", "Hello, how are you? Welcome."])
def test_greetings_and_filler_are_not_topics(greeting):
    index = TopicIndex(passages(greeting, "La biblioteca reduce el ruido con paneles acústicos."))
    summary = {"theme": greeting, "message": "La biblioteca reduce el ruido."}
    faults, _ = index.assess(summary, [], {0, 1}, DEFAULT_IDENTITY)
    assert faults["theme"] == "generic_theme"


def test_recurring_topic_beats_an_incidental_podcast_aside():
    index = TopicIndex(passages(
        "El equipo comenta el café de la mañana.",
        "La biblioteca amplía el horario de lectura.",
        "La biblioteca habilita la entrada lateral.",
        "El nuevo horario permite usar la biblioteca por la tarde.",
        "El podcast también menciona un partido.",
    ))
    theme = {"theme": "El café de la mañana", "message": "El equipo habló del café."}
    alternatives = [{"theme": "Horario de la biblioteca"}, {"theme": "El partido"}]
    assert index.assess(theme, alternatives, set(range(5)), DEFAULT_IDENTITY)[0] == {"theme": "incidental_theme"}
    assert index.assess({"theme": "Horario de la biblioteca", "message": "La biblioteca habilita la entrada lateral."},
                        alternatives, set(range(5)), DEFAULT_IDENTITY) == ({}, [1, 2, 3])


@pytest.mark.parametrize("value", ["Vamos a visitar la torre.", "Les voy a contar lo que pasó.",
                                  "Yo estuve en el lugar.", "Fuimos a investigar.",
                                  "Anqora Media Studio investigó el caso.", "We went to the building."])
def test_outlet_cannot_claim_the_speakers_actions(value):
    assert perspective_error(value, passages(f"El interlocutor relata una visita al {BUILDING}."))


def test_cited_attributed_first_person_is_preserved():
    quote = f"Yo estuve en el {BUILDING}"
    rows = passages(f"La invitada dice: «{quote}» durante la entrevista.")
    assert not perspective_error(f"El testimonio de la invitada sobre el {BUILDING}", rows)
    assert not perspective_error(f"La invitada dice: «{quote}».", rows)
    assert perspective_error(f"«{quote}».", rows)
    assert perspective_error(f"La invitada dice: «Yo viví en el {BUILDING}».", rows)
    assert perspective_error("La invitada dice: «Fui».", passages("Fuiste tú quien habló."))


def test_numeric_name_is_not_a_count_date_or_address():
    rows = passages(f"La voz menciona el {BUILDING}. Vio 3 personas el 14 de mayo. "
                    "La dirección es calle Las Flores 18. El expediente A-12 registra 2,5%.")
    known = {BUILDING, "Edificio Cedral 8"}
    assert "entity" in [kind for kind, _, _ in number_uses(BUILDING, known)]
    assert numeric_issues("La visita al edificio Cedral 7", rows, known) == set()
    assert numeric_issues("Cedral 7", rows, known) == set()
    assert numeric_issues("Edificio Cedral 8", rows, known) == {"unsupported_numeric_entity"}
    assert numeric_issues("7 personas llegaron", rows, known) == {"unsupported_number"}
    assert numeric_issues("Edificio Cedral 7%", rows, known) == {"unsupported_number"}
    assert numeric_issues("Edificio Cedral 7.8", rows, known) == {"unsupported_number"}
    assert numeric_issues("18 de mayo", rows, known) == {"unsupported_number"}
    assert numeric_issues("Calle Los Álamos 18", rows, known) == {"unsupported_number"}
    assert numeric_issues("Expediente B-12", rows, known) == {"unsupported_number"}
    assert numeric_issues("2.5% de los casos", rows, known) == {"unsupported_number"}
    assert not numeric_issues("3 personas el 14 de mayo", rows, known)
    assert not numeric_issues("Calle Las Flores 18", rows, known)
    assert not numeric_issues("Expediente A-12 y 2,5%", rows, known)



def test_metadata_overlap_does_not_corroborate_a_named_person():
    index = TopicIndex(passages(QUOTE, SENSITIVE))
    summary = {"theme": METADATA, "message": f"La persona relata una visita al {BUILDING}."}
    issues, _ = index.assess(summary, [], {0, 1}, DEFAULT_IDENTITY, {BUILDING, "Nelia Forjar"})
    assert issues == {"theme": "unsupported_entity"}
    context = index.classify_context({"title": METADATA, "description": "Audiencia del canal"},
                                     f"Visita al {BUILDING}", [0])
    assert context == [{"key": "description", "status": "auxiliary", "refs": []},
                       {"key": "title", "status": "topic_corroborated", "refs": [0]}]

def synthetic_source():
    # These passages are fabricated for the test. The counts reproduce only the failure's shape.
    rows = passages(
        "Muy importante. Qué tal, hermano?",
        *[f"{QUOTE}. {SENSITIVE}. Durante la charla también se comprueba el sonido del micrófono y continúa el relato."
          for _ in range(15)],
        f"{SENSITIVE}. La voz vuelve a mencionar el {BUILDING}.",
    )
    return {"clip_id": "incidente-sintetico", "original_score": 7.24, "language_hint": "es",
            "semantic_evidence": rows, "excerpts": rows, "outside_clip": {
                "title": METADATA, "description": "Una hipótesis sobre una persona ausente."},
            "transcript_coverage": {"cues": 154, "safe_cues": 154, "passages": 17, "speech_coverage": .93}}


class IncidentModel:
    def __init__(self, *, time_out=False):
        self.calls = []
        self.draft = None
        self.edits = 0
        self.time_out = time_out

    def generate(self, _model, messages, _schema, _timeout, cancellation):
        cancellation.check()
        data = json.loads(messages[1]["content"])
        self.calls.append(data)
        if "passages" in data:
            assert "Exclude greetings" in messages[0]["content"]
            sources = data["passages"]
            first = int(next(iter(sources)))
            text = sources[str(first)]["text"].split(".")[0]
            response = comprehension()
            response.update(theme="Muy importante. Qué tal, hermano?", message="Vamos a visitar la torre.",
                            secondary=[METADATA], uncertainty="high", sensitive=True,
                            claims=[{"text": text, "quote": text, "kind": "attributed_fact", "refs": [first],
                                     "ambiguity": ""}],
                            entities=[{"name": "Nelia Forjar", "kind": "person", "refs": [first]}],
                            highlights=[first])
            return response
        if "window_summaries" in data:
            return {**{key: comprehension()[key] for key in semantic.SUMMARY_FIELDS},
                    "theme": "Muy importante. Qué tal, hermano?", "message": "Vamos a visitar la torre.",
                    "secondary": [METADATA], "uncertainty": "high", "covered": list(range(len(data["window_summaries"])))}
        if "theme_repair" in data:
            if self.time_out:
                raise ProviderError("timeout", "Ollama superó el tiempo máximo.")
            quote = next(item for item in data["transcript_quotes"] if BUILDING in item["quote"])
            return {"theme": f"El testimonio sobre el {BUILDING}",
                    "message": f"La persona relata una visita al {BUILDING}.",
                    "secondary": [METADATA], "standalone": True, "context_needed": "", "uncertainty": "low",
                    "ambiguities": [], "focus": {**quote, "text": quote["quote"],
                                                "kind": "attributed_fact", "ambiguity": ""}}
        if "requested_angles" in data:
            assert "outside_clip" not in data and "Nelia Forjar" not in str(data)
            assert data["understanding"]["theme"] == f"El testimonio sobre el {BUILDING}"
            claims = [data["available_claims"][0]["id"]]
            self.draft = {
                "curiosity": {"title": f"¿Qué observó la visita al {BUILDING}?",
                              "caption": f"Yo estuve en el {BUILDING}.",
                              "description": "El testimonio separa las observaciones de los rumores que menciona.",
                              "call_to_action": "", "hashtags": [], "claims": claims, "strategy": "Abrir con un detalle."},
                "interaction": {"title": "Un rumor en el testimonio de la visita",
                                 "caption": "¿Qué detalles distingue el testimonio de la visita?",
                                 "description": "La conversación revisa qué pudo ver la persona y qué oyó después.",
                                 "call_to_action": "", "hashtags": [], "claims": claims, "strategy": "Delimitar la discusión."},
                "contextual": {"title": f"Una visita al {BUILDING}",
                                "caption": "El fragmento recoge la experiencia de una persona en el lugar.",
                                "description": f"La persona cuenta cómo llegó al {BUILDING} para conocerlo.",
                                "call_to_action": "", "hashtags": [], "claims": claims, "strategy": "Enmarcar la visita."},
            }
            return self.draft
        if "repair" in data:
            self.edits += 1
            assert list(data["repair"]) == ["curiosity"] and list(data["repair"]["curiosity"]) == ["caption"]
            if self.edits == 1:
                return {"curiosity": {"caption": "access_token=dato-de-prueba", "unexpected": "secreto"}}
            return {"curiosity": {"caption": f"El relato describe qué vio la persona al llegar al {BUILDING}."}}
        review = {key: 8 for key in semantic.CRITERIA}
        return {style: {**review, "grounded": True, "sensational": False, "uncertainty": "low", "fields": []}
                for style in data["variants_to_review"]}


def test_high_coverage_incident_repairs_theme_before_drafting():
    source = synthetic_source()
    model = IncidentModel()
    result = semantic.generate(model, source, SETTINGS, Cancellation(), lambda _: None)
    assert result["transcript_coverage"]["safe_cues"] == 154
    assert result["analysis"]["topic_verified"] and result["recommended_variant"]
    assert result["main_topic"] == f"El testimonio sobre el {BUILDING}"
    assert result["secondary_topics"] == []
    evidence = result["analysis"]["provenance"]["context_assessment"]
    assert evidence[0] == {"key": "description", "status": "auxiliary", "refs": []}
    assert evidence[1]["status"] == "topic_corroborated" and evidence[1]["refs"]
    assert "interpretation" in result["analysis"]["provenance"]
    scope = [next(key for key in ("passages", "window_summaries", "theme_repair",
                                  "requested_angles", "repair", "variants_to_review") if key in message)
             for message in model.calls]
    assert scope.count("passages") >= 2
    assert scope.index("theme_repair") < scope.index("requested_angles")
    log = result["diagnostics"]
    assert log["theme_repair_calls"] == 1 and log["repair_calls"] == 2
    assert log["repair_trace"][0]["outcome"] == "verified"
    assert log["repair_trace"][1]["schema_errors"] == {"curiosity": {"caption": "unsafe", "extra_fields": "structure"}}
    assert log["repair_trace"][2]["remaining_before"] >= log["repair_trace"][2]["remaining_after"]
    assert log["corrected_fields"] == [{"variant": "curiosity", "field": "caption"}]
    assert "dato-de-prueba" not in str(log)
    assert result["drafts"]["interaction"] == model.draft["interaction"]
    assert result["drafts"]["contextual"] == model.draft["contextual"]


def test_high_coverage_does_not_allow_drafting_if_theme_repair_cannot_run():
    double = IncidentModel()
    result = semantic.generate(double, synthetic_source(), {**SETTINGS, "repair_attempts": 0},
                               Cancellation(), lambda _: None)
    assert result["main_topic"] == "Tema por verificar" and not result["variants"]
    assert result["editorial_score"] == 0 and result["editorial_state"] == "review"
    assert result["diagnostics"]["repair_trace"] == []
    stop = result["diagnostics"]["repair_stops"]["theme"]
    assert stop["reason"] == "attempt_limit" and stop["remaining_seconds"] > 0
    assert not any("requested_angles" in input_data for input_data in double.calls)


def test_repair_diagnostics_distinguish_local_budget_from_provider_timeout():
    metrics = {}
    run = semantic.Run(None, SETTINGS, Cancellation(), lambda _: None, metrics)
    run.deadline = 0
    assert run.repair("theme", {"theme": "generic_theme"}) is None
    assert metrics["repair_stops"]["theme"]["reason"] == "time_budget"
    model = IncidentModel(time_out=True)
    result = semantic.generate(model, synthetic_source(), {**SETTINGS, "repair_attempts": 1}, Cancellation(), lambda _: None)
    assert result["diagnostics"]["repair_trace"][0]["outcome"] == "timeout"
    assert not result["recommended_variant"]


def test_editorial_identity_default_migration_and_validation():
    old = {k: v for k, v in content.DEFAULT_CONFIG.items() if k not in {"identity", "repair_attempts"}}
    assert content.validate_config(old)["identity"] == DEFAULT_IDENTITY
    with pytest.raises(ValueError, match="identidad editorial"):
        content.validate_config({**old, "identity": {**DEFAULT_IDENTITY, "perspective": "first_person"}})


def test_chromium_identity_is_saved_through_http_and_survives_restart(tmp_path, chromium_page):
    page, expect = chromium_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    with running_server(tmp_path) as (_, base):
        page.goto(base)
        page.locator("#show-settings").click()
        page.locator("#show-ai-settings").click()
        page.get_by_text("Identidad editorial", exact=True).click()
        expect(page.get_by_label("Nombre editorial")).to_have_value("Anqora Media Studio")
        page.get_by_label("Nombre editorial").fill("Estudio de pruebas")
        page.get_by_label("Perspectiva").select_option("impersonal")
        page.get_by_role("button", name="Guardar configuración de AI Engine").click()
        expect(page.locator("#notice")).to_contain_text("Ajustes de AI Engine guardados")
        status = request(base, "/api/editorial/status")
        assert status["config"]["settings"]["identity"]["perspective"] == "impersonal"
    with running_server(tmp_path) as (_, base):
        page.goto(base)
        page.locator("#show-settings").click()
        page.locator("#show-ai-settings").click()
        page.get_by_text("Identidad editorial", exact=True).click()
        expect(page.get_by_label("Nombre editorial")).to_have_value("Estudio de pruebas")
    assert not errors


def test_language_preference_localizes_only_verified_subject():
    class LocalizedModel:
        def __init__(self):
            self.calls = []

        def generate(self, _model, messages, _schema, _timeout, cancellation):
            cancellation.check()
            data = json.loads(messages[1]["content"])
            self.calls.append(data)
            if "passages" in data:
                assert "Write editorial text in the source language" in messages[0]["content"]
                return comprehension()
            if "localize_theme" in data:
                assert "Write editorial text in English" in messages[0]["content"]
                return {"theme": "Watering basil", "message": "Check the soil before watering basil.", "secondary": []}
            if "requested_angles" in data:
                assert data["understanding"]["theme"] == "Watering basil"
                from test_production import proposal
                return {style: {**values, "title": f"Basil and {focus}", "caption": f"The discussion focuses on {focus} for basil.",
                                "description": f"The speaker offers a practical explanation about {focus} and dry soil.",
                                "call_to_action": "", "hashtags": ["#Basil"], "strategy": "Care routine.", "claims": [0]}
                        for (style, values), focus in zip(proposal()["variants"].items(), ("growing", "watering", "light"))}
            assert "variants_to_review" in data
            review = {key: 8 for key in semantic.CRITERIA}
            return {style: {**review, "grounded": True, "sensational": False, "uncertainty": "low", "fields": []}
                    for style in data["variants_to_review"]}

    model = LocalizedModel()
    rows = passages("La albahaca necesita luz. Riega cuando notes la tierra seca.")
    source = {"clip_id": "basil", "original_score": 8, "semantic_evidence": rows, "excerpts": rows}
    result = semantic.generate(model, source, {**SETTINGS, "identity": {**DEFAULT_IDENTITY, "language": "en"}},
                               Cancellation(), lambda _: None)
    assert result["main_topic"] == "Watering basil" and result["analysis"]["theme"] == "Cuidado de la albahaca"
    assert result["analysis"]["ledger"][0]["quote"] == "La albahaca necesita luz."
    assert result["recommended_variant"]
