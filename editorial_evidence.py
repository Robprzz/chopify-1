"""Local evidence boundaries for the semantic editorial workflow."""
import math
import re
import unicodedata

from editorial_source import INSTRUCTIONS, SENSITIVE

WINDOW_BYTES = 3200
MAX_WINDOWS = 20
STARTER = frozenset("""
a al ante aprovecha antes así aprende atención cómo conoce con cuéntanos cuál cuándo
de deja descubre dos el en estas este explica explica aquí escucha explora fíjate
hay hoy la las le lo los mira más menos mi muestra observa para participe participa
por pregunta qué quién quiénes revisa según si sin sobre su sus te tu un una usa
the this what how why when where can could should do did watch discover learn
listen ask tell share in on as about from an and because here now two
""".split())
CLAUSES = re.compile(r"[.!?。！？]+\s*")
SENSITIVE_TOPIC = re.compile(
    r"\b(?:fallec\w*|muer(?:te|to|ta)|murió|asesin\w*|acus\w*|culpabl\w*|rumor\w*|"
    r"diagnóstic\w*|tratamient\w*|enfermedad\w*|menor\w*|death|died|accus\w*|"
    r"kill\w*|diagnos\w*|patient\w*|medical|alleg\w*)\b", re.I)
ATTRIBUTION = re.compile(
    r"\b(?:según|dice|afirma|relata|describe|explica|cuenta|sostiene|habla|menciona|"
    r"discute|comenta|testimonio|relato|interlocut\w*|entrevist\w*|voz|clip|fragmento|"
    r"according|says|claims|describes|speaker|interview|clip)\b", re.I)
CLAUSE_WORDS = re.compile(r"[^\W\d_][^\W_]*(?:[’'-][^\W_]+)*", re.UNICODE)
NUMBERS = re.compile(r"\b\d+(?:[.,]\d+)*\b")
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


def safe(text):
    return isinstance(text, str) and bool(text.strip()) and not (
        INSTRUCTIONS.search(text) or SENSITIVE.search(text) or re.search(r"[\x00-\x08\x0b-\x1f<>\ud800-\udfff]", text))


def fold(value):
    normalized = unicodedata.normalize("NFKD", value.casefold())
    # Preserve ñ as a distinct Spanish letter while tolerating accent differences.
    normalized = normalized.replace("n\u0303", "ñ")
    return " ".join(re.findall(r"[^\W_]+", "".join(c for c in normalized
                                                    if not unicodedata.combining(c))))


def contains(needle, haystack):
    left, right = fold(needle), fold(haystack)
    return bool(left) and (left in right if CJK.search(left) else f" {left} " in f" {right} ")


def cited_text(rows):
    """Join adjacent text only; never create a name from distant cues or speakers."""
    spans = []
    previous = None
    for row in sorted(rows, key=lambda row: row["start"]):
        if (previous and row["start"] - previous["end"] <= .8
                and row.get("speaker") == previous.get("speaker")
                and not re.search(r"[.!?。！？]\s*$", previous["text"])):
            spans[-1] += " " + row["text"]
        else:
            spans.append(row["text"])
        previous = row
    return spans


def supports(value, rows):
    return any(contains(value, text) for text in cited_text(rows))


def supports_name(name, rows):
    return any(contains(name, clause) for span in cited_text(rows) for clause in CLAUSES.split(span))


def windows(rows):
    """Send every safe passage. Stop clearly if a pathological track exceeds the bound."""
    chunks, chunk, size = [], {}, 0
    for index, row in enumerate(rows):
        cost = len(row["text"].encode("utf-8")) + len(row.get("speaker", "").encode("utf-8")) + 58
        if chunk and size + cost > WINDOW_BYTES:
            chunks.append(chunk)
            chunk, size = {}, 0
        chunk[str(index)] = row
        size += cost
    if chunk:
        chunks.append(chunk)
    return chunks


def insufficient(source, rows):
    letters = sum(c.isalpha() for row in rows for c in row["text"])
    words = sum(len(CLAUSE_WORDS.findall(row["text"])) for row in rows)
    coverage = source.get("transcript_coverage", {})
    ratio = coverage.get("speech_coverage")
    return (letters < 16 or (words < 4 and not any(CJK.search(r["text"]) for r in rows))
            or (isinstance(ratio, (int, float)) and math.isfinite(ratio) and ratio < .60))


def probable_names(value):
    """Conservative guard for multi-token proper names and internal capitalized words."""
    names = set()
    for clause in CLAUSES.split(value):
        words = CLAUSE_WORDS.findall(clause)
        run = []
        for index, word in enumerate(words):
            proper = word[0].isupper() and word.casefold() not in STARTER and fold(word) not in STARTER
            if proper:
                run.append((index, word))
            else:
                if len(run) > 1:
                    names.add(" ".join(item for _, item in run))
                elif run and run[0][0] > 0:
                    names.add(run[0][1])
                run = []
        if len(run) > 1:
            names.add(" ".join(item for _, item in run))
        elif run and run[0][0] > 0:
            names.add(run[0][1])
    return names


def present_name(name, value):
    return contains(name, value)
