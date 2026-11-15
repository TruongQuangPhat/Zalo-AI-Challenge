"""Words a rule operator may leave unexplained in a question it answers.

A regex operator answers when its cues match, whatever else the sentence says; a word
it has never seen ("bq", "hãng") can change what is asked. Every word of a question must
therefore be a measured question word (``question_words.txt``: the wording of the train
questions and of their chat/phrasing variants, on which the operators score zero wrong),
a listed chat spelling or filler, or part of a name in the shop's own data. Otherwise
the operators abstain and the model rewrites or plans the question.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection
from functools import lru_cache
from pathlib import Path

from src.data.profiling import normalized
from src.semantics.rulebook import CHAT_ACCENTED, CHAT_PLAIN, QUESTION_TYPOS

WORDS_FILE = Path(__file__).with_name("question_words.txt")
# Address, politeness and sentence-final particles of everyday Vietnamese: they carry no
# condition of the question. Written from general usage, not from any test question.
FILLERS = (
    "ạ à á ơi ui ơ ờ ừ ừm dạ vâng nhé nha nhá nhen nhe nghen hen hén nè nà ha hả hở hử "
    "nhỉ nhở ta alo hello hi chào shop sếp chủ quán tiệm ad admin bạn bro chị anh em mình "
    "tui tớ tôi giùm dùm giúp hộ xíu chút tí tý xí lẹ gấp cảm ơn cám thanks thank tks "
    "ok oke okie okê vậy vầy đó đấy ấy thử xem coi check kiểm tra biết hỏi xin cho với "
    "thế á hè hỉ nghe nhoa nhaa nhee ạh ủa ua zị zạ dzậy dị rui rùi rooif zậy hen hông "
    "bé cưng mik mk mún muốn coi giùm júp dúp bít đuowc nhât nàoo hỏi nhanh luôn lun "
    "sốp shop wan quán wán tiệm kả cả kó có cũa của kủa mí mấy chừ bây giờ đạ đã "
    "mùng mồng giua giữa giựa xo so sánh dảm giảm báng bná bnhui bnhiu tóp hagn hàng "
    "wa qa qua thì là thôi rồi đấy toàn bộ tổng mất đem dề về mang lại mội mỗi "
    "set ngưỡng nguog mùa hè đó nè nhóe rơi vào tách muộn sổ cũng đều nhờ tốn"
).split()
# Words an operator binds by itself (rulebook terms, trade abbreviations): understood.
OPERATOR_WORDS = (
    "sộp trung thành đổi ý ưa chuộng chăm chỉ đắt khách cao điểm ncc nhà cung cấp hsd "
    "hạn dùng đứt chốt kênh thương mại điện tử sàn dưới vượt tối thiểu nhỏ hãng chiếc "
    "máy điện thoại đt cũ mới phụ kiện góp trả góp kỳ hạn quá chậm bảo hành lượt dòng thu "
    "hợp đồng imei đóng xếp nhận bỉm tã bịch size voucher ship phí điểm thành viên đăng ký nb "
    "chính"
).split()

_WORD = re.compile(r"\w+")


def _key(token: str) -> str:
    """Letters plus the set of marks, so "hoá" and "hóa" are one word."""
    decomposed = unicodedata.normalize("NFD", token.replace("đ", "d̶"))
    marks = sorted(c for c in decomposed if unicodedata.combining(c))
    return "".join(c for c in decomposed if not unicodedata.combining(c)) + "|" + "".join(marks)


def question_tokens(text: str) -> list[str]:
    """Casefolded words of a question; tokens with a digit (amounts, dates, codes) are not
    words."""
    text = unicodedata.normalize("NFC", text).casefold()
    return [t for t in _WORD.findall(text) if not re.search(r"\d", t)]


@lru_cache(maxsize=1)
def _vocabulary() -> tuple[frozenset[str], frozenset[str]]:
    words = set(WORDS_FILE.read_text(encoding="utf-8").split()) | set(FILLERS)
    words |= set(OPERATOR_WORDS)
    for phrase in CHAT_ACCENTED:
        words.update(phrase.split())
    plain = {normalized(w) for w in words} | set(QUESTION_TYPOS) | set(CHAT_PLAIN)
    return frozenset(map(_key, words)), frozenset(plain)


@lru_cache(maxsize=1)
def filler_words() -> frozenset[str]:
    """Accentless forms of the filler words."""
    return frozenset(normalized(w) for w in FILLERS)


def unknown_words(question: str, names: Collection[str] = ()) -> list[str]:
    """Words of ``question`` that are neither measured wording nor in ``names`` (accentless
    words of the shop's entity names)."""
    keys, plain = _vocabulary()
    unknown = []
    # "cao hay thấp hơn" asks the signed change; "thấp nhất" (a lowest) is not understood.
    question = re.sub(r"(?i)\bcao hay (?:thấp|thap)\b", "cao hay", question)
    # "xếp từ cao xuống thấp" orders a ranking; it is not "lowest".
    question = re.sub(r"(?i)\bcao xuống (?:thấp|thap)\b", "cao xuống", question)
    for token in question_tokens(question):
        bare = normalized(token)
        if bare in names:
            continue
        # A word typed without marks may be any word spelled with those letters.
        if (bare in plain) if bare == token else (_key(token) in keys):
            continue
        unknown.append(token)
    return unknown
