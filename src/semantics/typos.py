"""General typing-error repair for question words.

A question word that is neither measured wording nor part of a shop name, and is not even
spelled like a Vietnamese syllable, is repaired when exactly one known word follows from a
general typing slip:

- Telex keys left in the word: "nhieuf" (huyền), "dduowcj" (được), "laix";
- a dropped "n" of a final "nh"/"ng": "tíh" (tính), "từg" (từng), "đag" (đang);
- a first consonant that cannot start the word: "kác" (các);
- two adjacent letters swapped or a doubled letter: "nheiu", "bna", "nhiềuu";
- two words typed without a space: "bánnhiều".

The known words are the measured question vocabulary (train questions and their variants).
Capitalised words inside a sentence are names and stay as written.
More than one possible repair means no repair: the word stays unknown and the model reads it.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection
from functools import lru_cache

from src.data.profiling import normalized

TONES = {"́": "s", "̀": "f", "̉": "r", "̃": "x", "̣": "j"}
_TONE_MARKS = {letter: mark for mark, letter in TONES.items()}
_TELEX_VOWELS = (
    ("uow", "uo"),
    ("dd", "d"),
    ("aa", "a"),
    ("aw", "a"),
    ("ee", "e"),
    ("oo", "o"),
    ("ow", "o"),
    ("uw", "u"),
)
_ONSETS = (
    ("x", "s"),
    ("s", "x"),
    ("n", "l"),
    ("l", "n"),
    ("ch", "tr"),
    ("tr", "ch"),
    ("d", "gi"),
    ("gi", "d"),
    ("d", "v"),
    ("v", "d"),
    ("z", "d"),
    ("z", "gi"),
    ("z", "v"),
    ("dz", "d"),
    ("dz", "v"),
    ("j", "gi"),
    ("k", "c"),
    ("c", "k"),
    ("r", "g"),
    ("g", "r"),
    ("w", "qu"),
    ("f", "ph"),
)


_NUCLEI = (
    "a e i o u y ai ao au ay eo eu ia ie iu oa oe oi ua ue ui uo uu uy ya ye "
    "ieu oai oay oeo uoi uou uya uye uyu yeu uay"
).split()
_SYLLABLE = re.compile(
    r"(ngh|ng|nh|ch|gh|gi|kh|ph|qu|th|tr|[bcdghklmnprstvx])?"
    r"(" + "|".join(sorted(_NUCLEI, key=len, reverse=True)) + r")"
    r"(ch|ng|nh|[cmnpt])?"
)


def is_syllable(plain: str) -> bool:
    """Whether an accentless word is spelled like one Vietnamese syllable."""
    found = _SYLLABLE.fullmatch(plain)
    if not found:
        return False
    onset, nucleus = found[1] or "", found[2]
    front = nucleus[0] in "eiy"
    # k, gh, ngh only before e/i/y; c, g, ng never before them (gi is its own onset).
    if onset in {"k", "gh", "ngh"} and not front:
        return False
    return not (onset in {"c", "g", "ng"} and front)


def tone(word: str) -> str | None:
    """Telex letter of the tone mark on ``word`` (s, f, r, x, j), or None."""
    marks = [TONES[c] for c in unicodedata.normalize("NFD", word) if c in TONES]
    return marks[0] if len(marks) == 1 else None


@lru_cache(maxsize=4)
def _index(words: frozenset[str]) -> tuple[frozenset[str], dict]:
    """Plain forms of the known words, and (plain, tone) -> accented known words."""
    by_sound: dict[tuple[str, str | None], set[str]] = {}
    for word in words:
        if word != normalized(word):
            by_sound.setdefault((normalized(word), tone(word)), set()).add(word)
    return frozenset(normalized(w) for w in words), by_sound


def _candidates(plain: str, mark: str | None) -> list[set[tuple[str, str | None]]]:
    """(plain word, tone) readings of a mistyped word, most likely slip first."""
    telex, nasal, swap, transposed, extra = set(), set(), set(), set(), set()
    # Telex: doubled vowels/đ and a trailing tone key.
    body, key = plain, mark
    if len(body) >= 3 and body[-1] in _TONE_MARKS and body[-2] not in _TONE_MARKS:
        body, key = body[:-1], body[-1]
    for typed, letter in _TELEX_VOWELS:
        body = body.replace(typed, letter)
    if body != plain:
        telex.add((body, key))
    if len(plain) >= 3 and plain[-1] == "h" and plain[-2:] not in {"ch", "nh"}:
        nasal.add((plain[:-1] + "nh", mark))
    if len(plain) >= 2 and plain[-1] == "g" and plain[-2:] != "ng":
        nasal.add((plain[:-1] + "ng", mark))
    for typed, meant in _ONSETS:
        if plain.startswith(typed) and len(plain) > len(typed):
            swap.add((meant + plain[len(typed) :], mark))
    if mark in {"r", "x"}:
        swap.add((plain, "x" if mark == "r" else "r"))
    if len(plain) >= 3:
        for i in range(len(plain) - 1):
            transposed.add((plain[:i] + plain[i + 1] + plain[i] + plain[i + 2 :], mark))
        # A doubled letter ("nhiềuu"); dropping any other letter turns real words into
        # function words ("lương" is not "luôn").
        for i in range(1, len(plain)):
            if plain[i] == plain[i - 1]:
                extra.add((plain[:i] + plain[i + 1 :], mark))
    tiers = [telex, nasal, swap, transposed, extra]
    for tier in tiers:
        tier.discard((plain, mark))
    return tiers


def _resolve(tier, plains, by_sound, accented: bool) -> set[str] | None:
    """Known words a tier of readings gives, or None when one reading is ambiguous."""
    outputs = set()
    for word, mark in tier:
        if word not in plains:
            continue
        if not accented and mark is None:
            outputs.add(word)
            continue
        forms = by_sound.get((word, mark), set())
        if len(forms) > 1:
            return None
        outputs |= forms
    return outputs


def repair(token: str, words: frozenset[str]) -> str | None:
    """The one known word (or two words) ``token`` stands for, else None."""
    if len(token) < 2 or re.search(r"\d", token):
        return None
    plains, by_sound = _index(words)
    plain = normalized(token)
    accented = token != plain
    if is_syllable(plain):
        # A well-formed word may be a real word the vocabulary lacks ("tuyệt đối"): it is
        # left to the model.
        return None
    for tier in _candidates(plain, tone(token)):
        outputs = _resolve(tier, plains, by_sound, accented)
        if outputs is None or len(outputs) > 1:
            return None
        if outputs:
            return outputs.pop()
    splits = {
        f"{token[:i]} {token[i:]}"
        for i in range(2, len(plain) - 1)
        if plain[:i] in plains and plain[i:] in plains
    }
    return splits.pop() if len(splits) == 1 else None


def repaired(question: str, known, names: Collection[str] = ()) -> str:
    """``question`` with each unknown word repaired; ``known(token)`` says a word is known
    wording, ``names`` holds the accentless words of the shop's names (never changed)."""
    from src.semantics.vocabulary import correction_words

    words = correction_words()

    def fix(match: re.Match) -> str:
        token = match[0]
        lower = token.casefold()
        # A capitalised word inside the sentence is a name ("Lương Chi Đức").
        if token != lower and match.start() > 0:
            return token
        if normalized(lower) in names or known(lower):
            return token
        return repair(lower, words) or token

    return re.sub(r"\w+", fix, unicodedata.normalize("NFC", question))
