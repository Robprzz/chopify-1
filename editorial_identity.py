"""The Studio's editorial voice, separate from the identity of a clip's speakers."""
import re
import unicodedata

from editorial_evidence import ATTRIBUTION, cited_text, fold, safe, supports


DEFAULT_IDENTITY = {
    "name": "Anqora Media Studio",
    "organization": "Medio de comunicación digital",
    "perspective": "third_person",
    "language": "es",
    "style": "Informativo, natural y atractivo",
    "tone": "Adaptable al contenido",
    "objectivity": "high",
    "creativity": "moderate_high",
}
CHOICES = {
    "perspective": ("third_person", "impersonal"),
    "language": ("es", "en", "source"),
    "objectivity": ("high", "balanced"),
    "creativity": ("low", "moderate", "moderate_high"),
}
FIRST_PERSON = re.compile(
    r"\b(?:yo|nosotros|nosotras|mi|mis|nuestro|nuestra|nuestros|nuestras|"
    r"me (?=acuerdo|parece|fui|dijeron|contaron|pas[oó]|encontr[eé])|"
    r"(?:vamos|voy) a|estuve|estuvimos|estoy|estamos|fui|fuimos|hice|hicimos|contar[eé]|(?:visit|investig|recorr|grab|descubr|encontr|cont)"
    r"(?:é|í|amos|imos)|(?:i|we) (?:went|visited|saw|were|was|will|are|investigated|found|want|have|think|used|made|heard)|"
    r"i['’](?:m|ll|ve|d)|we['’](?:re|ll|ve|d)|my|our)\b", re.I)
QUOTES = re.compile(r'«([^»]+)»|“([^”]+)”|"([^"]+)"')
ROLE = {
    "third_person": "Tell the story as an outside observer in third person.",
    "impersonal": "Prefer impersonal narration as an outside observer.",
}


def validate_identity(identity):
    if not isinstance(identity, dict) or identity.keys() != DEFAULT_IDENTITY.keys():
        raise ValueError("Identidad editorial incompleta.")
    for key, value in identity.items():
        if key in CHOICES:
            correct = isinstance(value, str) and value in CHOICES[key]
        else:
            correct = safe(value) and len(value) <= 100
        if not correct:
            raise ValueError("Revisa los valores de la identidad editorial.")
    return {key: value.strip() for key, value in identity.items()}


def voice_rules(identity, *, source_language=False):
    language = {"es": "Spanish", "en": "English", "source": "the source language"}[identity["language"]]
    if source_language:
        language = "the source language"
    return (f" Write editorial text in {language}. {ROLE[identity['perspective']]} "
            "Never speak as a protagonist or credit the outlet with the speaker's actions. "
            "Preserve first-person wording only in exact, explicitly attributed quotations. "
            "Use the organization's style, tone, objectivity and creativity preferences in editorial_identity. "
            "Those preferences cannot override attribution, fidelity or the ban on deceptive sensationalism.")


def perspective_error(value, passages, identity=DEFAULT_IDENTITY):
    def literal(text):
        return " ".join(unicodedata.normalize("NFC", text).casefold().split())

    def attributed_quote(match):
        quote = next(part for part in match.groups() if part is not None)
        before = value[max(0, match.start() - 100):match.start()]
        after = value[match.end():match.end() + 80]
        if (ATTRIBUTION.search(before) or ATTRIBUTION.search(after)) and any(
                re.search(r"(?<!\w)" + re.escape(literal(quote)) + r"(?!\w)", literal(span))
                for span in cited_text(passages)):
            return " "
        return match[0]

    narrative = QUOTES.sub(attributed_quote, value)
    if FIRST_PERSON.search(narrative):
        return True
    # The outlet is a voice setting and cannot be introduced as a participant.
    return bool(identity.get("name") and f" {fold(identity['name'])} " in f" {fold(narrative)} "
                and not supports(identity["name"], passages))
