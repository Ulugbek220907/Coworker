"""Query text for the two search paths: FTS5 when it exists, a scan otherwise.

Both paths split the query into the same word tokens and require every token to
match, so they return the same rows. The scan compares casefolded text instead of
using SQLite's LIKE, because LIKE folds only ASCII and the owner writes Uzbek and
Russian.
"""
from __future__ import annotations

import re
from typing import Iterable

_WORD = re.compile(r"\w+")


def tokens(query: str) -> list[str]:
    """The casefolded word tokens of a query, in order."""
    return _WORD.findall(query.casefold())


def fts_query(query: str) -> str | None:
    """A FTS5 MATCH expression that is always valid: quoted, prefix-matched, ANDed.

    Tokens are word characters only, so quoting cannot break the syntax.
    Returns None when the query has no words.
    """
    terms = tokens(query)
    if not terms:
        return None
    return " ".join(f'"{term}"*' for term in terms)


def contains_all(text: str, terms: Iterable[str]) -> bool:
    """The scan-path equivalent of an FTS match: every term appears in the text."""
    folded = text.casefold()
    return all(term in folded for term in terms)
