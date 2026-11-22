"""Immutable evidence references and attribution boundaries for editorial analysis."""
import re

from editorial_evidence import ATTRIBUTION, fold, present_name, probable_names, safe, supports_name

SENTENCES = re.compile(r"([!?。！？;]+|(?<!\d)\.+|\.+(?!\d))")
EVENT_BREAK = re.compile(r"[,;]\s*(?:por otro lado|por otra parte|en otro caso|en cambio|mientras que)\b", re.I)
EVENTS = {
    "poisoning": re.compile(r"\b(?:envenen\w*|intoxic\w*|poison\w*)\b"),
    "death": re.compile(r"\b(?:murio|murieron|muerte|fallec\w*|perdio la vida|died|death)\b"),
    "accusation": re.compile(r"\b(?:acus\w*|culpab\w*|alleg\w*)\b"),
    "homicide": re.compile(r"\b(?:asesin\w*|homicid\w*|murder\w*)\b"),
}
UNCERTAIN = re.compile(r"\b(?:hipotesis|rumor\w*|supuest\w*|posibl\w*|habria|no se confirm\w*|sin confirm\w*|"
                       r"presunt\w*|unconfirmed|alleg\w*|might|could)\b")


def _clauses(value):
    parts = SENTENCES.split(value)
    for i in range(0, len(parts), 2):
        question = i + 1 < len(parts) and any(mark in parts[i + 1] for mark in "?？")
        for clause in EVENT_BREAK.split(parts[i]):
            yield clause, question


def registry(passages, context):
    rows = {}
    for ref, passage in enumerate(passages):
        for part, (clause, question) in enumerate(_clauses(passage["text"])):
            quote = clause.strip()
            if not safe(quote):
                continue
            ident = f"p{ref}.{part}"
            rows[ident] = {"id": ident, "origin": "clip_transcript", "refs": [ref], "quote": quote,
                           "events": [key for key, pattern in EVENTS.items() if pattern.search(fold(quote))],
                           "modality": ("question" if question
                                        else "hypothesis" if UNCERTAIN.search(fold(quote)) else "statement")}
    for field, quote in sorted(context.items()):
        if safe(quote):
            ident = f"context:{field}"
            origin = "surrounding_transcript" if field in {"before", "after"} else "video_metadata"
            rows[ident] = {"id": ident, "origin": origin, "refs": [], "quote": quote, "modality": "uncorroborated"}
    return rows


def named_mentions(value, entities=()):
    candidates = probable_names(value) | {name for name in entities if present_name(name, value)}
    # Prefer the complete registered name when a capitalization heuristic
    # identifies just one of its words, e.g. Arpel inside Arpel 7.
    return sorted(name for name in candidates if not any(
        fold(name) != fold(other) and present_name(name, other) for other in candidates))


def findings(field, value, passages, allowed, entries, entities, *, require_attribution=True):
    names = named_mentions(value, entities)
    rows = [passages[i] for i in sorted(allowed)]
    details = []

    def add(code, subject, refs, reason, correction):
        details.append({"field": field, "code": code, "subject": subject, "fragment": value,
                        "evidence": [{"id": row["id"], "origin": row["origin"], "quote": row["quote"][:200],
                                      "refs": row["refs"]} for row in refs[:4]],
                        "reason": reason, "restriction": "transcript_grounding", "suggestion": correction})

    for name in names:
        if supports_name(name, rows):
            continue
        matches = [row for row in entries.values() if present_name(name, row["quote"])]
        in_clip = supports_name(name, passages)
        reason = ("outside_selected_evidence" if in_clip else
                  "auxiliary_only" if matches and all(not row["refs"] for row in matches) else "name_not_attested")
        add("unsupported_entity", name, matches, reason,
            "Selecciona una evidencia del clip que contenga el nombre completo o reformula sin identificar a la persona o el lugar.")

    for key, pattern in EVENTS.items():
        for clause, question in _clauses(value):
            if not pattern.search(fold(clause)):
                continue
            clause_names = [name for name in names if present_name(name, clause)]
            events = [row for row in entries.values() if key in row.get("events", ())]
            matching = [row for row in events if allowed.intersection(row["refs"])
                        and all(present_name(name, row["quote"]) for name in clause_names)]
            if not matching:
                add("event_attribution", clause.strip(), events, "event_and_subject_not_linked",
                    "Usa un pasaje que vincule explícitamente el hecho con su sujeto o describe ambos acontecimientos por separado.")
            elif all(row["modality"] in {"question", "hypothesis"} for row in matching) and not (
                    UNCERTAIN.search(fold(clause)) or question):
                add("event_uncertainty", clause.strip(), matching, "source_is_uncertain",
                    "Preserva la hipótesis, la pregunta o la incertidumbre de la fuente.")
            elif require_attribution and clause_names and not ATTRIBUTION.search(clause):
                add("attribution", clause.strip(), matching, "sensitive_statement_without_speaker",
                    "Atribuye explícitamente la declaración a quien habla en el clip.")
    return details
