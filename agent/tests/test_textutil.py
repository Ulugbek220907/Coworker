"""Text matching: prefix floor, dead suffixes, prepared queries and snippet offsets."""
from __future__ import annotations

from coworker import textutil as t


# ---------------------------------------------------------- Ma.pdf regression

def test_ma_pdf_does_not_earn_prefix_credit_for_longer_queries():
    # The two-letter token "ma" used to match any longer query that began with
    # it ("malika" scored 0.98 against "Ma.pdf"). Prefix credit now needs three.
    for query in ("malika", "mashina", "matn", "mahmud"):
        assert t.score(query, "Ma.pdf") == 0.0, query


def test_two_letter_name_still_matches_itself_exactly():
    assert t.score("ma", "Ma.pdf") == 1.0


def test_short_target_word_is_never_a_prefix_of_a_longer_query_word():
    assert t.MIN_PREFIX == 3
    assert not t._prefix_related("malika", "ma")   # two letters: too short to count
    assert t._prefix_related("malika", "mal")      # three letters: enough
    assert not t._prefix_related("malika", "m")


def test_query_word_must_be_four_letters_to_begin_a_longer_target_word():
    # Kept on purpose: at three letters "tel" scored 0.98 against both Telegram Desktop
    # and Telemost, which disables the launcher's ambiguity prompt.
    assert t.MIN_QUERY_PREFIX == 4
    assert not t._prefix_related("tel", "telegram")
    assert not t._prefix_related("ish", "ishxona")
    assert t.score("tel", "telegram.exe") == 0.0
    assert t._prefix_related("tele", "telegram")


def test_legitimate_prefix_matches_keep_their_credit():
    assert t.score("shartnom", "shartnomasi.docx") >= 0.8
    assert t.score("shartnomani", "shartnoma.docx") >= 0.8
    assert t.score("mahmud", "mahmudov.docx") >= 0.8


# ------------------------------------------------------------ dead suffixes

def test_suffix_table_holds_only_latin_endings():
    # Words are transliterated before they are stemmed, so Cyrillic endings could never match.
    assert all(s.isascii() for s in t._SUFFIXES)


def test_stemming_still_trims_uzbek_endings():
    assert t.stem("shartnomani") == "shartnoma"
    assert t.stem("hisobotlar") == "hisobot"
    assert t.stem("kitobi") == "kitob"


def test_stemming_never_shortens_below_four_characters():
    assert t.stem("ish") == "ish"
    assert t.stem("nima") == "nima"


# ------------------------------------------------------------ prepared query

def test_prepared_query_scores_the_same_as_the_string():
    text = "tekstil shartnoma"
    prepared = t.prepare(text)
    for target in ("Договор_текстиль_2024.docx", "tekstil-shartnoma.pdf", "Ma.pdf", "notes.txt"):
        assert t.score(prepared, target) == t.score(text, target)
        assert t.score_expanded(prepared, target) == t.score_expanded(text, target)


def test_prepare_is_idempotent_and_expand_accepts_it():
    prepared = t.prepare("dogovor")
    assert t.prepare(prepared) is prepared
    assert t.expand(prepared) == t.expand("dogovor")


def test_prepared_query_holds_tokens_groups_and_terms():
    q = t.prepare("shartnoma")
    assert q.tokens == ("shartnoma",)
    assert "dogovor" in q.terms and "contract" in q.terms
    assert q.groups[0] == t.SYNONYMS["shartnoma"]


def test_synonyms_still_bridge_languages():
    assert t.score_expanded("shartnoma", "Dogovor_2024.docx") >= 0.8


def test_cyrillic_query_matches_latin_file_name():
    assert t.score("Договор", "dogovor.docx") >= 0.9


def test_empty_query_scores_zero():
    assert t.score("", "anything.docx") == 0.0
    assert t.score_expanded("", "anything.docx") == 0.0


# ------------------------------------------------------------ offsets, snippet

def test_folded_text_maps_back_to_original_offsets():
    text = "Ёлка: Договор на поставку"
    folded, offsets = t.translit_with_offsets(text)
    assert len(folded) == len(offsets)
    # "ё" folds to "yo", so the folded form is longer than the original text.
    assert len(folded) > len(text)
    assert offsets[folded.index("dogovor")] == text.index("Договор")


def test_snippet_shows_the_matched_words_after_a_lengthening_fold():
    text = "Ё" * 80 + " Договор на поставку товаров"
    window = t.snippet(text, "dogovor", width=40)
    assert "Договор" in window


def test_snippet_falls_back_to_the_start_when_nothing_matches():
    assert t.snippet("короткий текст", "absent", width=8) == "короткий"


def test_snippet_adds_ellipses_only_when_text_is_cut():
    text = "a" * 10 + " target " + "b" * 10
    assert t.snippet(text, "target", width=60) == text
    cut = t.snippet("x" * 300 + " shartnoma " + "y" * 300, "shartnoma", width=60)
    assert cut.startswith("...") and cut.endswith("...")
    assert "shartnoma" in cut
