"""Text normalisation for mixed Uzbek-Latin / Uzbek-Cyrillic / Russian input.

The whole point of this module: a file named ``Договор_текстиль_2024.docx``
sitting in ``D:\\Ишхона\\Шартномалар`` must be findable by someone who typed
"tekstil shartnoma" on a Latin keyboard. Everything is folded down to one
comparable Latin form before any matching happens.
"""
from __future__ import annotations

import re
import unicodedata

# Covers Russian and Uzbek Cyrillic alike. Multi-character values are fine;
# the table is applied character by character.
_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ғ": "g", "д": "d", "е": "e",
    "ё": "yo", "ж": "j", "з": "z", "и": "i", "й": "y", "к": "k", "қ": "q",
    "л": "l", "м": "m", "н": "n", "ң": "ng", "о": "o", "ў": "o", "ӯ": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "x",
    "ҳ": "h", "ц": "ts", "ч": "ch", "ҷ": "j", "ш": "sh", "щ": "sh",
    "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "і": "i", "ї": "yi", "є": "e", "ә": "a", "ө": "o", "ү": "u", "һ": "h",
}

# Uzbek Latin uses several apostrophe glyphs interchangeably; drop them all.
_APOSTROPHES = "\u02bb\u02bc\u2018\u2019\u0027\u0060\u00b4"

# Suffixes worth trimming so that "shartnomani" still matches "shartnoma".
# Longest first - the loop stops at the first hit.
_SUFFIXES = (
    # Uzbek case/possessive endings
    "larimizning", "laringizning", "larining", "larimiz", "laringiz",
    "lariga", "larida", "laridan", "larini", "larning", "lari", "larga",
    "larda", "lardan", "larni", "lar", "ning", "imiz", "ingiz", "iga",
    "ida", "idan", "ini", "imni", "miz", "ngiz", "ga", "da", "dan", "ni",
    "si", "im", "ing", "i",
    # Russian noun/adjective endings
    "ами", "ями", "ого", "ему", "ыми", "ими", "ов", "ев", "ам", "ям",
    "ах", "ях", "ой", "ый", "ий", "ая", "ое", "ые", "ие", "ом", "ем",
    "ах", "у", "ы", "а", "е", "о", "я", "ю", "и",
)

_WORD = re.compile(r"[0-9a-zA-Zа-яёА-ЯЁғқҳўҒҚҲЎ]+")


def translit(text: str) -> str:
    """Fold any mix of scripts down to lowercase ASCII-ish Latin."""
    text = unicodedata.normalize("NFKC", text)
    out = []
    for ch in text.lower():
        if ch in _APOSTROPHES:
            continue
        out.append(_CYR.get(ch, ch))
    return "".join(out)


def normalize(text: str) -> str:
    """Translit plus punctuation flattening - safe to use as a match key."""
    latin = translit(text)
    latin = re.sub(r"[_\-\.\,\(\)\[\]\{\}/\\+]+", " ", latin)
    return re.sub(r"\s+", " ", latin).strip()


def stem(word: str) -> str:
    """Crude but effective suffix trim. Never shortens below 4 characters."""
    if len(word) <= 4:
        return word
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            return word[: -len(suf)]
    return word


def tokens(text: str, *, do_stem: bool = True) -> list[str]:
    """Normalised word list, deduplicated, stopwords removed."""
    words = _WORD.findall(normalize(text))
    out, seen = [], set()
    for w in words:
        if w in STOPWORDS or len(w) < 2:
            continue
        w = stem(w) if do_stem else w
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


# Filler words that carry no search signal in either language.
STOPWORDS = {
    "va", "bilan", "uchun", "kerak", "edi", "men", "meni", "mening", "bu",
    "shu", "u", "ular", "qaysi", "qayer", "qayerda", "nima", "topib", "top",
    "ber", "bering", "yubor", "yubaring", "yuboring", "iltimos", "boshqa",
    "faylni", "fayl", "hujjat", "hujjatni", "menga", "sen", "siz", "ha",
    "yoq", "yo", "ok", "the", "and", "for", "file", "find", "send", "me",
    "i", "pozhalusta", "nuzhen", "nuzhno", "nado", "dai", "day", "mne",
    "kotory", "gde", "chto", "eto", "tot", "ta", "to", "na", "v", "s", "po",
    "iz", "ili", "a", "no", "da", "net", "esli", "kak", "vot", "tam",
}


def score(query: str, target: str) -> float:
    """How well ``target`` (a filename or path) answers ``query``.

    Returns roughly 0..1. Prefix matching is what makes morphology-rich
    languages work here: "shartnom" matches "shartnomasi" without a real
    stemmer being involved.
    """
    q = tokens(query)
    if not q:
        return 0.0
    t = tokens(target, do_stem=False)
    if not t:
        return 0.0
    t_stemmed = [stem(w) for w in t]

    hits = 0.0
    for qt in q:
        if qt in t_stemmed or qt in t:
            hits += 1.0
            continue
        # Partial credit when the query token is a prefix of a target token
        # (or the other way round) - covers truncation and extra suffixes.
        best = 0.0
        for tt in t:
            if len(qt) >= 4 and (tt.startswith(qt) or qt.startswith(tt)):
                best = max(best, 0.85)
            elif len(qt) >= 5 and qt in tt:
                best = max(best, 0.6)
        hits += best

    coverage = hits / len(q)
    # A short, focused filename that matches is a better hit than a long one
    # that happens to contain the words.
    brevity = min(1.0, 8.0 / max(len(t), 1)) * 0.15
    return min(1.0, coverage + coverage * brevity)


# Transliteration cannot bridge *translation*: "shartnoma" and "dogovor" are
# the same document in two languages. This table does the rest, and it is the
# single highest-value thing in the search path for a bilingual office.
_SYNONYM_GROUPS = [
    ("shartnoma", "dogovor", "kontrakt", "contract", "bitim", "soglashenie"),
    ("hisobot", "otchet", "report", "hisob"),
    ("ariza", "zayavlenie", "zayavka", "application", "murojaat"),
    ("buyruq", "prikaz", "order", "farmoyish", "rasporyajenie"),
    ("hisobvaraq", "schet", "invoice", "faktura", "schyot"),
    ("dalolatnoma", "akt", "act"),
    ("ishonchnoma", "doverennost"),
    ("guvohnoma", "svidetelstvo", "sertifikat", "certificate", "litsenziya"),
    ("pasport", "passport"),
    ("xat", "pismo", "letter", "maktub"),
    ("smeta", "xarajat", "rashod", "budget", "byudjet"),
    ("royxat", "spisok", "reestr", "ruyxat"),
    ("bayonnoma", "protokol", "protocol"),
    ("taklif", "predlojenie", "offer", "kommercheskoe"),
    ("nizom", "ustav", "reglament", "qoida", "polojenie"),
    ("buxgalteriya", "accounting", "hisobxona"),
    ("soliq", "nalog", "tax"),
    ("bank", "bankovskiy"),
    ("ish", "rabota", "ishxona", "ofis", "office", "kontora"),
    ("xodim", "sotrudnik", "kadr", "personal", "employee"),
    ("mijoz", "klient", "client", "zakazchik", "buyurtmachi"),
    ("zavod", "fabrika", "korxona", "predpriyatie", "factory"),
    ("loyiha", "proekt", "project"),
    ("rasm", "foto", "photo", "image", "surat", "skan", "scan"),
    ("taqdimot", "prezentatsiya", "presentation"),
    ("jadval", "tablitsa", "table", "grafik"),
]

# Flattened to a lookup: every member maps to the whole group.
SYNONYMS: dict[str, tuple[str, ...]] = {}
for _group in _SYNONYM_GROUPS:
    _stems = tuple(stem(w) for w in _group)
    for _w in _stems:
        SYNONYMS.setdefault(_w, _stems)


def expand(query: str) -> list[str]:
    """Query tokens plus their cross-language equivalents."""
    base = tokens(query)
    out, seen = [], set()
    for t in base:
        for variant in SYNONYMS.get(t, (t,)):
            if variant not in seen:
                seen.add(variant)
                out.append(variant)
    return out


def score_expanded(query: str, target: str) -> float:
    """Like :func:`score`, but a synonym hit counts almost as much as a
    literal one. Matching the user's own wording still ranks higher."""
    direct = score(query, target)
    groups = [SYNONYMS.get(t, (t,)) for t in tokens(query)]
    if not groups or all(len(g) == 1 for g in groups):
        return direct

    t_tokens = tokens(target, do_stem=False)
    t_stemmed = [stem(w) for w in t_tokens]
    hits = 0.0
    for group in groups:
        best = 0.0
        for variant in group:
            for tt in t_stemmed + t_tokens:
                if variant == tt:
                    best = max(best, 1.0)
                elif len(variant) >= 4 and (tt.startswith(variant) or variant.startswith(tt)):
                    best = max(best, 0.8)
        hits += best
    via_synonym = (hits / len(groups)) * 0.9  # slight penalty vs. a direct hit
    return max(direct, min(1.0, via_synonym))
