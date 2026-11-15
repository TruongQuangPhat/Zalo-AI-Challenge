"""Northern and southern names of the same goods ("má lợn" / "má heo").

A shop's catalog writes an item in one regional form; a question may use the other. When
a question names no catalog item but holds the other-region form of exactly one item, that
span is replaced by the catalog name, so every later step sees the shop's own spelling.
The pairs are northern/southern names of everyday goods and trade words.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from itertools import product

from src.data.profiling import normalized

# Each group lists forms of one word that name the same goods in different regions.
REGIONAL = (
    ("lợn", "heo"),
    ("rán", "chiên"),
    ("đậu phụ", "đậu hũ", "đậu hủ"),
    ("ngao", "nghêu"),
    ("cá tràu", "cá quả", "cá lóc"),
    ("lạc", "đậu phộng"),
    ("vừng", "mè"),
    ("ngô", "bắp"),
    ("dứa", "thơm", "khóm"),
    ("hoa quả", "trái cây"),
    ("quả", "trái"),
    ("pho mát", "phô mát", "phô mai"),
    ("dưa chuột", "dưa leo"),
    ("mướp đắng", "khổ qua"),
    ("đỗ", "đậu"),
    ("giò lụa", "chả lụa"),
    ("nem rán", "chả giò"),
    ("bánh đa", "bánh tráng"),
    ("mì chính", "bột ngọt"),
    ("xì dầu", "nước tương"),
    ("bát", "chén"),
    ("thìa", "muỗng"),
    ("cốc sạc", "củ sạc"),
    ("cốc", "ly"),
    ("giá đỡ", "kệ"),
    ("găng tay", "bao tay"),
    ("tất chân", "bao chân"),
    ("tất", "vớ"),
    ("xà phòng", "xà bông"),
    ("mũ", "nón"),
    ("ô", "dù"),
    ("áo phông", "áo thun"),
    ("ví", "bóp"),
    ("chăn", "mền"),
    ("màn", "mùng"),
    ("điều hoà", "điều hòa", "máy lạnh"),
    ("trứng", "hột"),
    ("xây trát", "xây tô"),
    ("trát tường", "tô tường"),
)
_FORMS = {form: group for group in REGIONAL for form in group}
_PHRASE = re.compile(
    r"(?<!\w)(" + "|".join(sorted(map(re.escape, _FORMS), key=len, reverse=True)) + r")(?!\w)"
)
_MAX_VARIANTS = 64


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", normalized(text))


def variants(name: str) -> set[str]:
    """Other-region spellings of ``name`` (lower case, accented), without ``name`` itself."""
    lower = name.casefold()
    parts = _PHRASE.split(lower)
    choices = [_FORMS.get(part, (part,)) if i % 2 else (part,) for i, part in enumerate(parts)]
    found = set()
    for combo in product(*choices):
        found.add("".join(combo))
        if len(found) > _MAX_VARIANTS:
            break
    found.discard(lower)
    return found


def align_names(text: str, names: Iterable[str]) -> str:
    """Write the catalog name where ``text`` names one item only in its other-region form."""
    names = [n for n in dict.fromkeys(names) if n and n.strip()]
    words = list(re.finditer(r"\w+", text))
    plain_words = [normalized(w[0]) for w in words]
    if not words:
        return text
    known = {tuple(_tokens(n)) for n in names}
    plain_text = " ".join(plain_words)
    if any(k and f" {' '.join(k)} " in f" {plain_text} " for k in known if len(k) >= 2):
        return text
    spans: dict[tuple[int, int], set[str]] = {}
    for name in names:
        for variant in variants(name):
            target = tuple(_tokens(variant))
            if len(target) < 2 or target in known:
                continue
            size = len(target)
            for i in range(len(plain_words) - size + 1):
                if tuple(plain_words[i : i + size]) == target:
                    spans.setdefault((i, i + size), set()).add(name)
    # One item per span; the longest span wins and others may not overlap it.
    chosen = []
    for (start, end), owners in sorted(spans.items(), key=lambda s: s[0][0] - s[0][1]):
        if len(owners) != 1 or any(start < e and s < end for s, e, _ in chosen):
            continue
        chosen.append((start, end, next(iter(owners))))
    if len(chosen) != 1:
        return text
    start, end, name = chosen[0]
    return text[: words[start].start()] + name + text[words[end - 1].end() :]
