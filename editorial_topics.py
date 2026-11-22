"""Identify substantive subject matter before generating editorial copy."""
from collections import Counter
import re

from editorial_evidence import (ATTRIBUTION, CJK, SENSITIVE_TOPIC, fold, numeric_issues,
                                safe, supports_name)
from editorial_identity import perspective_error
from editorial_registry import named_mentions


SENTENCES = re.compile(r"(?<!\d)[.!?。！？;]+|[.!?。！？;]+(?!\d)")
FILLERS = re.compile(
    r"\b(?:muy importante|super importante|que tal|como estan|buenos dias|buenas tardes|buenas noches|"
    r"bienvenid[oa]s?|hola|herman[oa]s?|amig[oa]s?|saludos|bueno|pues|eh|este|osea|o sea|"
    r"ya sabes|ya tu sabes|no te lo pierdas|mira esto|increible|que opinas|gracias por (?:estar|venir)|"
    r"suban el volumen|comprueben el audio|me escuchan|compartan el directo|"
    r"hi everyone|how are you|hello|welcome|very important|watch this)\b")
STOP = frozenset("""
a al algo antes aqui asi con como cual cuando de del dicen dice donde el ella ellas ellos en era es esa
ese eso esta estas este esto fue ha hay la las le les lo los mas me mi muy nos o para pero por porque
puede que quien se si sin sobre son su sus tal tan te tiene todo tu un una uno y yo ya
clip fragmento tema mensaje video trata trata sobre material informacion general parte persona personas
conversacion relato cuenta habla explica segun testimonio alguien entrevistado entrevistada momento
contexto discusion narracion resumen otro otra otros caso noticia
a an and are as at be by clip for from has he how in is it its of on or our said says she that the their
them then there these they this to was were what when where who will with you your
""".split())


def clauses(value):
    return SENTENCES.split(value)


def terms(value):
    normalized = FILLERS.sub(" ", fold(value))
    words = {token for token in normalized.split() if len(token) >= 3 and any(c.isalpha() for c in token) and token not in STOP
             and not CJK.search(token)}
    for sequence in re.findall(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+", normalized):
        words.update(sequence)
        words.update(sequence[i:i + 2] for i in range(len(sequence) - 1))
    return words


def _stem(word):
    if len(word) > 4 and word.endswith("s"):
        word = word[:-1]
    return word


def _keywords(value):
    return {_stem(word) for word in terms(value)}


class TopicIndex:
    def __init__(self, passages):
        self.passages = passages
        self.signals = {}
        for i, row in enumerate(passages):
            tokens = set()
            for clause in clauses(row["text"]):
                tokens.update(_keywords(clause))
            if tokens:
                self.signals[i] = tokens
        self.frequency = Counter(token for tokens in self.signals.values() for token in tokens)

    def ranked_terms(self, refs, max_terms=10):
        local = {token for ref in refs for token in self.signals.get(ref, set())}
        return sorted(local, key=lambda token: (-self.frequency[token], token))[:max_terms]

    def references(self, text):
        tokens = _keywords(text)
        return [ref for ref, signals in self.signals.items() if signals & tokens]

    def candidates(self, limit=8):
        ranked = []
        seen = set()
        for ref in self.signals:
            for part in clauses(self.passages[ref]["text"]):
                clause = part.strip()
                if not safe(clause):
                    continue
                quote = clause if len(clause) <= 165 else clause[:166].rsplit(" ", 1)[0]
                identity = fold(quote)
                tokens = _keywords(quote)
                if tokens and identity not in seen:
                    seen.add(identity)
                    ranked.append((sum(min(self.frequency[token], 4) for token in tokens), ref, quote))
        ranked.sort(key=lambda row: (-row[0], row[1]))
        # Keep some chronological breadth so the hook cannot drown out the ending.
        spread = [ranked[0]] if ranked else []
        chronological = sorted(ranked, key=lambda row: row[1])
        if len(chronological) > limit:
            spread += [chronological[len(chronological) // 2], chronological[-1]]
        spread.extend(row for row in ranked if row not in spread)
        return [{"refs": [ref], "quote": quote} for _, ref, quote in sorted(spread[:limit], key=lambda row: row[1])]

    def subject_issue(self, value, grounded_refs, identity, entities=()):
        rows = [self.passages[ref] for ref in sorted(grounded_refs)]
        if perspective_error(value, rows, identity):
            return "narrative_perspective"
        if not terms(value) or (FILLERS.search(fold(value)) and len(terms(value)) <= 1):
            return "generic_theme"
        if not grounded_refs.intersection(self.references(value)):
            return "ungrounded_theme"
        if any(not supports_name(name, rows) for name in
               named_mentions(value, entities)):
            return "unsupported_entity"
        numbers = numeric_issues(value, rows, entities)
        return min(numbers) if numbers else None

    def assess(self, summary, window_summaries, grounded_refs, identity, entities=()):
        theme = summary["theme"]
        hits = self.references(theme)
        allowed = grounded_refs
        issues = {}
        issue = self.subject_issue(theme, allowed, identity, entities)
        if issue:
            issues["theme"] = issue
        # Challenge an isolated aside only if a grounded alternative recurs in other passages.
        elif len(hits) == 1:
            for alternate in window_summaries:
                support = self.references(alternate["theme"])
                if len(support) >= 3 and not (set(support) & set(hits)) and allowed.intersection(support):
                    issues["theme"] = "incidental_theme"
                    break
        message = summary["message"]
        issue = self.subject_issue(message, allowed, identity, entities)
        if issue:
            issues["message"] = "ungrounded_message" if issue == "ungrounded_theme" else issue
        elif SENSITIVE_TOPIC.search(message) and not ATTRIBUTION.search(message):
            issues["message"] = "attribution"
        return issues, sorted(allowed.intersection(hits))

    def classify_context(self, context, theme, theme_refs):
        # Match context at the thematic level only. A title cannot verify a complete claim.
        matches = []
        theme_terms = _keywords(theme)
        for key, value in context.items():
            overlap = _keywords(value) & theme_terms
            refs = [ref for ref in theme_refs if overlap & self.signals.get(ref, set())]
            matches.append({"key": key, "status": "topic_corroborated" if refs else "auxiliary", "refs": refs})
        return sorted(matches, key=lambda item: item["key"])
