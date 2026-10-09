"""Text normalisation for mixed Uzbek-Latin / Uzbek-Cyrillic / Russian input.

The whole point of this module: a file named ``Договор_текстиль_2024.docx``
sitting in ``D:\\Ишхона\\Шартномалар`` must be findable by someone who typed
"tekstil shartnoma" on a Latin keyboard. Everything is folded down to one
comparable Latin form before any matching happens.

Scoring rules that matter to callers:

* A prefix match needs a matched prefix of at least ``MIN_PREFIX`` characters.
  Two-letter fragments such as the ``ma`` in ``Ma.pdf`` used to earn credit for
  any longer query that began with them (``malika`` scored 0.98 against it).
* ``prepare(query)`` normalises a query once. The scoring functions accept
  either a string or a prepared ``Query``, so a search over thousands of
  candidate names does not repeat that work for every candidate.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

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

# Prefix credit needs a shared prefix of at least MIN_PREFIX characters. A short
# *target* word cannot be a prefix of a longer query word: "ma" in "Ma.pdf" is
# not a match for "malika". A query word must also be MIN_QUERY_PREFIX long to
# earn credit as a prefix of a longer target word. That floor is kept at four
# because "tel" (three letters) scoring 0.98 against both Telegram Desktop and
# Telemost disabled the launcher's ambiguity prompt.
MIN_PREFIX = 3
MIN_QUERY_PREFIX = 4

# Suffixes worth trimming so that "shartnomani" still matches "shartnoma".
# Longest first - the loop stops at the first hit. Words are transliterated
# before they are stemmed, so every token here is Latin. The Russian endings
# that once sat in this list could never match and have been removed.
_SUFFIXES = (
    # Uzbek case/possessive endings
    "larimizning", "laringizning", "larining", "larimiz", "laringiz",
    "lariga", "larida", "laridan", "larini", "larning", "lari", "larga",
    "larda", "lardan", "larni", "lar", "ning", "imiz", "ingiz", "iga",
    "ida", "idan", "ini", "imni", "miz", "ngiz", "ga", "da", "dan", "ni",
    "si", "im", "ing", "i",
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


def translit_with_offsets(text: str) -> tuple[str, list[int]]:
    """Like :func:`translit`, but each output character also records which
    character of ``text`` produced it.

    Transliteration changes lengths (``ё`` becomes ``yo``, apostrophes vanish),
    so a position found in the folded form cannot be used to slice the original
    text directly. ``offsets[i]`` is the index in ``text`` of the character that
    produced ``folded[i]``.
    """
    chars: list[str] = []
    offsets: list[int] = []
    for index, raw in enumerate(text):
        for ch in unicodedata.normalize("NFKC", raw).lower():
            if ch in _APOSTROPHES:
                continue
            for out in _CYR.get(ch, ch):
                chars.append(out)
                offsets.append(index)
    return "".join(chars), offsets


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


def _prefix_related(query_word: str, target_word: str) -> bool:
    """Prefix credit between a query word and a target word.

    The query word may begin the target word when it is MIN_QUERY_PREFIX long.
    The target word may begin the query word when it is MIN_PREFIX long, which is
    the case that made "ma" in "Ma.pdf" match "malika".
    """
    if target_word.startswith(query_word) and len(query_word) >= MIN_QUERY_PREFIX:
        return True
    return query_word.startswith(target_word) and len(target_word) >= MIN_PREFIX


@dataclass(frozen=True)
class Query:
    """A query after normalisation. Build it with :func:`prepare` and reuse it.

    ``tokens`` are the stemmed query words, ``groups`` holds each word's
    cross-language variants (one tuple per word) and ``terms`` is the flattened,
    deduplicated list of those variants.
    """

    text: str
    tokens: tuple[str, ...]
    groups: tuple[tuple[str, ...], ...]
    terms: tuple[str, ...]


def prepare(query: "str | Query") -> Query:
    """Normalise a query once. A prepared query is returned unchanged."""
    if isinstance(query, Query):
        return query
    text = query or ""
    base = tokens(text)
    groups = tuple(SYNONYMS.get(t, (t,)) for t in base)
    terms: list[str] = []
    for group in groups:
        for variant in group:
            if variant not in terms:
                terms.append(variant)
    return Query(text=text, tokens=tuple(base), groups=groups, terms=tuple(terms))


def score(query: "str | Query", target: str) -> float:
    """How well ``target`` (a filename or path) answers ``query``.

    Returns roughly 0..1. Prefix matching is what makes morphology-rich
    languages work here: "shartnom" matches "shartnomasi" without a real
    stemmer being involved.
    """
    q = prepare(query).tokens
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
        # Partial credit when one token is a prefix of the other - covers
        # truncation and extra suffixes, but never a two-letter fragment.
        best = 0.0
        for tt in t:
            if _prefix_related(qt, tt):
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


def expand(query: "str | Query") -> list[str]:
    """Query tokens plus their cross-language equivalents."""
    return list(prepare(query).terms)


def score_expanded(query: "str | Query", target: str) -> float:
    """Like :func:`score`, but a synonym hit counts almost as much as a
    literal one. Matching the user's own wording still ranks higher."""
    q = prepare(query)
    direct = score(q, target)
    if not q.groups or all(len(g) == 1 for g in q.groups):
        return direct

    t_tokens = tokens(target, do_stem=False)
    t_stemmed = [stem(w) for w in t_tokens]
    hits = 0.0
    for group in q.groups:
        best = 0.0
        for variant in group:
            for tt in t_stemmed + t_tokens:
                if variant == tt:
                    best = max(best, 1.0)
                elif _prefix_related(variant, tt):
                    best = max(best, 0.8)
        hits += best
    via_synonym = (hits / len(q.groups)) * 0.9  # slight penalty vs. a direct hit
    return max(direct, min(1.0, via_synonym))


def snippet(text: str, term: str, width: int = 160) -> str:
    """A window of the original ``text`` around the first match of ``term``.

    The match is located in the folded text and mapped back to the original
    offset, so the window shows the words that matched even when the text
    contains characters that fold to a different length.
    """
    folded, offsets = translit_with_offsets(text)
    idx = folded.find(term) if term else -1
    if idx < 0:
        return text[:width].replace("\n", " ")
    start = max(0, offsets[idx] - width // 3)
    end = min(len(text), start + width)
    body = text[start:end].replace("\n", " ")
    return ("..." if start else "") + body + ("..." if end < len(text) else "")
