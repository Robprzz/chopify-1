"""Compare the pinned pre-2.4 engines and Automation 2.4 on a labelled local corpus."""
import argparse
from collections import Counter
import csv
import importlib.util
from itertools import combinations
import json
from math import ceil
from pathlib import Path
import re
from statistics import median
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import editorial_content as extractive
import editorial_semantic as current
from editorial_providers import Cancellation, Ollama, ProviderError

BASE = "43b4755efce066cdb20755b680edd2f4d5a94107"
CORPUS = ROOT / "docs/evidence/editorial-24/corpus.tsv"
FOREIGN_NAME = "Nadia Brisal"
LANGUAGE_COPY = {
    "es": ("¿Qué detalle cambia la forma de ver el tema: {anchor}?", "El fragmento pone el foco en {anchor}.",
           "Una pregunta para conversar acerca de {anchor}", "¿Qué parte del ejemplo sobre {anchor} te llamó la atención?",
           "El relato sobre {anchor} en contexto", "El diálogo da un poco más de contexto sobre {anchor}."),
    "en": ("What detail matters when revisiting {anchor}?", "The clip focuses on how the guest uses {anchor}.",
           "A recording habit worth discussing: {anchor}", "Which part of this approach to {anchor} would you try?",
           "The speaker explains their use of {anchor}", "Hear how {anchor} fit into the conversation after recording."),
    "zh": ("怎么用{anchor}判断浇水时机？", "先留意{anchor}的干燥程度，也要留意阳光。",
           "检查{anchor}时你关注哪一点？", "说说你检查{anchor}的习惯。",
           "访谈里的浇水线索：{anchor}", "这段访谈谈到如何观察{anchor}。"),
    "ja": ("地図は{anchor}でどう役立った？", "{anchor}で道を聞いたときの話です。",
           "{anchor}で道を聞くなら何を見ますか？", "地図の見方について話してみましょう。",
           "{anchor}から市場までの道順", "話し手は道順を地図で振り返ります。"),
    "ar": ("متى نفحص التربة قبل إضافة {anchor}؟", "يناقش المقطع صلة التربة بحصول الجذور على {anchor}.",
           "كيف تعرف أن النبات يحتاج إلى {anchor}؟", "شارك طريقتك في فحص التربة قبل الري.",
           "{anchor} والجذور في سياق العناية بالنبات", "يصف المقطع دور التربة والضوء والجذور."),
}


def pinned():
    code = subprocess.run(["git", "show", f"{BASE}:editorial_semantic.py"], cwd=ROOT,
                          capture_output=True, check=True).stdout
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("editorial_22", loader=None))
    exec(compile(code, f"{BASE}:editorial_semantic.py", "exec"), module.__dict__)
    return module


def source_for(row):
    transcript = row["transcript"].replace("|", "").strip()
    pieces = re.split(r"(?<=[.?!。！？])\s*|\|", row["transcript"])
    passages = [{"start": i * 2.1, "end": i * 2.1 + 1.95, "text": part.strip()}
                for i, part in enumerate(pieces) if part.strip()]
    from editorial_source import excerpts
    cues = [(part["start"], part["end"], part["text"]) for part in passages]
    legacy, filtered, sampled = excerpts(cues)
    old_semantic = [{"start": left, "end": right, "text": text[:265]} for left, right, text in cues]
    return {"clip_id": row["id"], "original_score": 8, "language_hint": row["lang"], "excerpts": legacy,
            "filtered": filtered, "sampled": sampled, "semantic_evidence": passages, "benchmark_22_evidence": old_semantic,
            "outside_clip": {"title": FOREIGN_CONTEXT} if row["fault"] == "external" else {},
            "transcript_coverage": {"cues": len(cues), "safe_cues": len(cues), "passages": len(passages),
                                    "source_chars": len(transcript)}}


FOREIGN_CONTEXT = "Programa con Nadia Brisal"


def copies(row, passages):
    anchor = row["anchor"]
    descriptions = [passages[0]["text"], passages[-1]["text"], " ".join(p["text"] for p in passages)]
    templates = LANGUAGE_COPY[row["lang"]]
    values = {}
    for i, style in enumerate(extractive.STYLES):
        title, caption = (part.format(anchor=anchor) for part in templates[i * 2:i * 2 + 2])
        if row["fault"] == "acute" and i == 0:
            title = "La luz que observa Ines Valcera"
        if row["fault"] == "split" and i == 0:
            title = "Iris Brisal muestra cómo se ensaya en el taller"
        if row["category"] == "caso sensible" and anchor == "rumor":
            title = ("El rumor que la voz entrevistada dice que oyó", "¿Qué dice la entrevista sobre ese rumor?",
                     "La voz entrevistada describe un rumor sin confirmar")[i]
            caption = ("La persona entrevistada cuenta cómo oyó el rumor.",
                       "¿Qué parte del relato del rumor dejó pendiente quien habla?",
                       "El clip recoge lo que dice la voz entrevistada sobre el rumor.")[i]
        values[style] = {"title": title, "caption": caption, "description": descriptions[i],
                         "call_to_action": "", "hashtags": ["#" + anchor] if row["lang"] == "es" else [],
                         "claims": [0], "strategy": ("Detalle", "Pregunta", "Contexto")[i]}
    return values


class Scripted:
    """Run the same transcript and scripted fault against both semantic implementations."""
    def __init__(self, row, source, prior):
        self.row, self.source, self.prior, self.calls = row, source, prior, 0
        self.clean = copies(row, source["semantic_evidence"])

    def bad(self, drafts):
        fault = self.row["fault"]
        first = drafts["curiosity"]
        if fault in ("external", "entity"):
            first["title"] = f"{FOREIGN_NAME} comenta el tema: {self.row['anchor']}"
        if fault == "number":
            first["title"] += " en 999 pasos"
        if fault == "generic":
            first["title"] = "Muy importante"
        if fault == "duplicate":
            first["caption"] = first["title"]
        if fault == "tags":
            first["hashtags"] *= 2
        if fault == "similar":
            drafts["interaction"].update(first)
        return drafts

    def generate(self, model, messages, schema, timeout, cancellation):
        cancellation.check()
        self.calls += 1
        data = json.loads(messages[-1]["content"])
        row = self.row
        if "passages" in data:
            passages = data["passages"]
            first = next(iter(passages.values()))["text"]
            refs = list(map(int, passages))
            entities = []
            if row["fault"] in ("external", "entity", "acute", "split"):
                name = {"acute": "Ines Valcera", "split": "Iris Brisal"}.get(row["fault"], FOREIGN_NAME)
                entities = [{"name": name, "kind": "person", "refs": refs}]
            return {"theme": row["anchor"], "secondary": [], "message": first, "intent": "explain",
                    "emotion": "neutral", "audience": row["category"], "standalone": True,
                    "language": row["lang"], "tone": "neutral", "value": "informational", "context_needed": "",
                    "uncertainty": "low", "ambiguities": [], "claims": [
                        {"text": first, "quote": first, "refs": refs, "kind": "attributed_fact", "ambiguity": ""}],
                    "entities": entities, "highlights": refs, "sensitive": row["category"] == "caso sensible"}
        if "requested_angles" in data:
            return self.bad({name: dict(self.clean[name]) for name in data["requested_angles"]})
        if "repair" in data:
            return {style: {field: self.clean[style][field] for field in fields} for style, fields in data["repair"].items()}
        if "clip_evidence" in data:
            system = messages[0]["content"]
            correction = {"external": "Menciona personas solo", "entity": "Menciona personas solo",
                          "number": "Quita las cifras", "tags": "Utiliza hashtags",
                          "similar": "Diferencia cada ángulo"}.get(row["fault"])
            corrected = bool(correction and correction in system and row["id"] in (
                "noticia-acceso", "debate-patio", "motivacion-bicicleta", "desarrollo-cocina"))
            drafts = {name: dict(value) for name, value in self.clean.items()}
            if not corrected:
                drafts = self.bad(drafts)
            variants = {style: {k: item[k] for k in (*extractive.COPY_FIELDS, "hashtags")}
                        for style, item in drafts.items()}
            for value in variants.values():
                value.update(score=8, evidence=list(range(len(data["clip_evidence"]))))
            return {"theme": row["anchor"], "secondary": [], "message": row["transcript"][:140],
                    "intent": "explain", "emotion": "neutral", "audience": row["category"],
                    "interest": row["anchor"], "standalone": row["eligible"] == "yes",
                    "people": [FOREIGN_NAME] if row["fault"] in ("external", "entity") and not corrected else [],
                    "events": [], "variants": variants}
        if "evidence_by_index" in data:
            return {style: {**dict.fromkeys(self.prior.CRITERIA, 8), "grounded": True, "sensational": False}
                    for style in extractive.STYLES}
        return {style: {**dict.fromkeys(current.CRITERIA, 8), "grounded": True,
                        "sensational": False, "uncertainty": "low", "fields": []}
                for style in data["variants_to_review"]}


class Counted:
    def __init__(self, provider):
        self.provider, self.calls = provider, 0

    def generate(self, *args):
        self.calls += 1
        return self.provider.generate(*args)


def normalize(value):
    return re.sub(r"[^\w]+", " ", value.casefold()).strip()


def grade(row, result):
    """Use corpus labels and reproducible checks independent of the model's scores."""
    variants = [v for v in (result or {}).get("variants", []) if v.get("valid") is not False]
    speech = row["transcript"]
    anchor = row["anchor"].casefold()
    rejected = row["eligible"] == "no"
    across = [normalize(v["title"]) for v in variants]
    metrics = dict.fromkeys(("fidelity", "title", "caption", "description", "hashtags", "originality",
                             "differentiation", "standalone", "usable"), False)
    fidelity_errors = 0
    for v in variants:
        title, caption, description = (v[k] for k in ("title", "caption", "description"))
        claim = " ".join(v[k] for k in extractive.COPY_FIELDS)
        grounded = (FOREIGN_NAME.casefold() not in claim.casefold() and
                    set(re.findall(r"\d+", claim)) <= set(re.findall(r"\d+", speech)))
        fidelity_errors += not grounded
        tags = [name.casefold() for name in v["hashtags"]]
        flags = {"fidelity": grounded, "title": anchor in title.casefold() and 5 <= len(title) <= 105,
                 "caption": len(caption) >= 9 and normalize(title) != normalize(caption),
                 "description": len(description) >= 24 and normalize(caption) != normalize(description),
                 "hashtags": len(tags) == len(set(tags)) and all(t.startswith("#") and anchor in t for t in tags),
                 "originality": len({normalize(s) for s in (title, caption, description)}) == 3
                 and normalize(title) not in normalize(speech) and normalize(caption) not in normalize(speech),
                 "standalone": not rejected and anchor in claim.casefold() and result.get("standalone", False),
                 "differentiation": len(across) > 1 and all(a != b for a, b in combinations(across, 2))}
        for key, value in flags.items():
            metrics[key] |= value
        metrics["usable"] |= not rejected and all(value for key, value in flags.items() if key != "differentiation")
    return metrics, fidelity_errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("simulated", "ollama"), default="simulated")
    parser.add_argument("--model", default="qwen3:4b")
    parser.add_argument("--timeout", type=int, default=90, choices=range(5, 181), metavar="5–180")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    ollama, digest = None, None
    if args.provider == "ollama":
        ollama = Ollama()
        try:
            digest = next((m["digest"] for m in ollama.models() if m["name"] == args.model and m["digest"]), None)
        except ProviderError as exc:
            parser.exit(2, str(exc) + "\n")
        if not digest:
            parser.exit(2, "El modelo solicitado debe estar instalado en Ollama local antes de la prueba.\n")
    prior = pinned()
    with CORPUS.open(encoding="utf-8", newline="") as stream:
        corpus = list(csv.DictReader(stream, delimiter="\t"))
    entries = []
    for row in corpus:
        source = source_for(row)
        for engine in ("extractive", "2.2", "2.4"):
            profile = extractive.DEFAULT_CONFIG["profiles"]["tiktok"]
            settings = {"platform": "tiktok", "profile": profile, "model": args.model, "timeout": args.timeout,
                        "weights": extractive.DEFAULT_CONFIG["weights"], "repair_attempts": 2}
            provider = Counted(ollama or Scripted(row, source, prior))
            start = time.perf_counter()
            cancellation = Cancellation()
            diagnostics, result, error = {}, None, None
            try:
                if engine == "extractive":
                    plan_provider = provider if ollama else extractive.Rules()
                    plan = plan_provider.generate(args.model if ollama else None, extractive.messages(source, "tiktok", profile),
                                                  extractive.plan_schema(len(source["excerpts"])), args.timeout, cancellation)
                    result = extractive.assemble(plan, source, settings, "ollama" if ollama else "rules",
                                                 args.model if ollama else None)
                elif engine == "2.2":
                    sampled = {**source, "semantic_evidence": source["benchmark_22_evidence"]}
                    result = prior.generate(provider, sampled, settings, cancellation, lambda _: None)
                else:
                    result = current.generate(provider, source, settings, cancellation, lambda _: None,
                                              telemetry=diagnostics)
            except ProviderError as exc:
                error = exc.code
            duration = time.perf_counter() - start
            flags, errors = grade(row, result)
            recommended = (result or {}).get("recommended_variant")
            example = next((v for v in (result or {}).get("variants", []) if v["style"] == recommended), None)
            entries.append({"id": row["id"], "engine": engine, "eligible": row["eligible"] == "yes", "criteria": flags,
                            "seconds": round(duration, 6), "calls": provider.calls, "error": error,
                            "accepted": bool(recommended and (result or {}).get("editorial_score", 0) > 0),
                            "issues": sorted({issue["code"] for issue in diagnostics.get("issues", [])}),
                            "repaired": bool(diagnostics.get("verified_corrections") or (engine == "2.2" and (result or {}).get("attempts", 0) > 1)), "fidelity_errors": errors,
                            "example": {k: example[k] for k in ("title", "caption", "description")} if example else None})
    totals = {}
    for engine in ("extractive", "2.2", "2.4"):
        subset = [line for line in entries if line["engine"] == engine]
        timings = sorted(line["seconds"] for line in subset)
        eligible = [line for line in subset if line["eligible"]]
        hits = Counter(k for line in eligible for k, passed in line["criteria"].items() if passed)
        totals[engine] = {"n": len(subset), "eligible": len(eligible), "criteria_passed": dict(hits),
                          "errors": dict(Counter(item["error"] for item in subset if item["error"])),
                          "ineligible_rejected": sum(not item["accepted"] and not item["eligible"] for item in subset),
                          "calls": sum(item["calls"] for item in subset), "repairs_with_usable_result": sum(
                              item["repaired"] and item["criteria"]["usable"] for item in subset),
                          "fidelity_errors": sum(item["fidelity_errors"] for item in subset),
                          "median_seconds": median(timings), "p95_seconds": timings[ceil(.95 * len(timings)) - 1]}
    report = {"provider": args.provider, "model": args.model if digest else None, "digest": digest,
              "comparison_base": BASE, "timeout_seconds": args.timeout, "totals": totals, "cases": entries}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(totals, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
