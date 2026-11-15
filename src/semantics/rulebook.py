"""Recognize a bounded rule vocabulary with source evidence and explicit failures.

Only complete, recognized clauses become executable policies. Unknown text in a
relevant rule, conflicting definitions, or an unresolved industry never falls
back silently to training defaults.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.data.profiling import normalized
from src.jsonio import ContractError
from src.semantics.business_rules import CATALOGUE, CLAUSE_PARAMETERS, CLAUSES, EXECUTION

VERSION = "runtime-rules-v9"


class SemanticError(ContractError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def plain(text: str) -> str:
    return re.sub(r"\s+", " ", normalized(text).translate(str.maketrans("", "", "*`"))).strip(" .:")


# Telex typos of function words ("soos don", "ngayf naof"): an untyped tone or vowel key.
# Derived from train question wording and checked against catalog names, so product
# and customer names never change. Questions only; rulebook text keeps plain().
QUESTION_TYPOS = {
    "bans": "ban",
    "bawngf": "bang",
    "bieets": "biet",
    "bij": "bi",
    "binhf": "binh",
    "bowts": "bot",
    "chieems": "chiem",
    "chiuj": "chiu",
    "caonhat": "cao nhat",
    "conf": "con",
    "coongj": "cong",
    "cos": "co",
    "cuar": "cua",
    "cungf": "cung",
    "cuoois": "cuoi",
    "cuwar": "cua",
    "ddax": "da",
    "ddeems": "dem",
    "ddeens": "den",
    "ddieenj": "dien",
    "ddoof": "do",
    "ddooir": "doi",
    "ddos": "do",
    "ddown": "don",
    "doanhthu": "doanh thu",
    "dduwowcj": "duoc",
    "duocj": "duoc",
    "ee": "e",
    "ees": "e",
    "gaanf": "gan",
    "giamr": "giam",
    "gias": "gia",
    "giumf": "gium",
    "giups": "giup",
    "hangf": "hang",
    "heets": "het",
    "hoir": "hoi",
    "hoom": "hom",
    "hown": "hon",
    "huyr": "huy",
    "its": "it",
    "khachs": "khach",
    "khoong": "khong",
    "laf": "la",
    "laij": "lai",
    "leej": "le",
    "leen": "len",
    "maays": "may",
    "mawtj": "mat",
    "mooix": "moi",
    "mootj": "mot",
    "naof": "nao",
    "nawm": "nam",
    "nayf": "nay",
    "ngayf": "ngay",
    "ngaynao": "ngay nao",
    "nhaapj": "nhap",
    "nhaats": "nhat",
    "nhaf": "nha",
    "nhieeu": "nhieu",
    "nhieeuf": "nhieu",
    "nhir": "nhi",
    "nhoms": "nhom",
    "nowj": "no",
    "phaanf": "phan",
    "quaan": "quan",
    "quys": "quy",
    "sangs": "sang",
    "soos": "so",
    "tawng": "tang",
    "thangs": "thang",
    "thoaij": "thoai",
    "thuoocj": "thuoc",
    "thuoocs": "thuoc",
    "tieenf": "tien",
    "tieeu": "tieu",
    "tinhs": "tinh",
    "tir": "ti",
    "tooi": "toi",
    "toonf": "ton",
    "toongr": "tong",
    "trar": "tra",
    "trawm": "tram",
    "treen": "tren",
    "trowr": "tro",
    "truwowcs": "truoc",
    "tuwf": "tu",
    "vaayj": "vay",
    "vaf": "va",
    "vaof": "vao",
    "veef": "ve",
    "vowis": "voi",
}


# Chat abbreviations and regional (Central/Southern) spellings of common function words,
# written from everyday Vietnamese, never copied from test questions. Accent-bearing
# forms are matched before accents are dropped ("nhiêu" is not "nhiều"; "mô" is not "mở").
CHAT_ACCENTED = {
    "đh": "đơn hàng",
    "o đờ": "đơn",
    "nhìu": "nhiều",
    "nhứt": "nhất",
    "kòn": "còn",
    "jùm": "giùm",
    "dùm": "giùm",
    "dúp": "giúp",
    "júp": "giúp",
    "kủa": "của",
    "bít": "biết",
    "đơng": "đơn",
    "mổi": "mỗi",
    "gồi": "rồi",
    "giãm": "giảm",
    "tíh": "tính",
    "nhửng": "những",
    "nhiêuf": "nhiều",
    "đag": "đang",
    "bánnhiều": "bán nhiều",
    "ngừi": "người",
    "mí": "mấy",
    "chéc": "check",
    "tỉ nệ": "tỉ lệ",
    "giửa": "giữa",
    "dzạ": "vậy",
    "báng": "bán",
    "đượt": "được",
    "đươcj": "được",
    "chên": "trên",
    "xố": "số",
    "nượt": "lượt",
    "xửa": "sửa",
    "dá trị": "giá trị",
    "vâu chơ": "voucher",
    "nhân diên": "nhân viên",
    "lựơt": "lượt",
    "dảm": "giảm",
    "mội": "mỗi",
    "zô": "vô",
    "dới": "với",
    "dậy": "vậy",
    "zậy": "vậy",
    "rứa": "vậy",
    "mô": "nào",
    "ni": "này",
    "nì": "này",
    "hông": "không",
    "hổng": "không",
    # Dropped or doubled letters of money/total words ("tiềng" is not "tiếng", hours).
    # Trade words for buying stock in ("đánh hàng", "cất hàng" = nhập hàng).
    "đánh hàng": "nhập hàng",
    "cất hàng": "nhập hàng",
    "tiềng": "tiền",
    "tền": "tiền",
    "tổg": "tổng",
    "cộg": "cộng",
    "bìh": "bình",
    "bìn": "bình",
}
CHAT_PLAIN = {
    "dc": "duoc",
    "bnhiu": "bao nhieu",
    "bnhieu": "bao nhieu",
    "bn": "bao nhieu",
    "nhiu": "nhieu",
    "hnay": "hom nay",
    "hqua": "hom qua",
    "hkia": "hom kia",
    "cko": "cho",
    "mik": "minh",
    "ko": "khong",
    "hok": "khong",
    "khum": "khong",
    "vs": "voi",
    "trc": "truoc",
    "dthu": "doanh thu",
    "jum": "gium",
    "lun": "luon",
    "thui": "thoi",
    "nhta": "nhat",
    "nhut": "nhat",
    # English shop words typed into Vietnamese questions.
    "revenue": "doanh thu",
    "order": "don",
    "orders": "don",
    "bill": "don",
    "ck": "chuyen khoan",
    "lne": "len",
    "tog": "tong",
    "cog": "cong",
    "bih": "binh",
    "bq": "binh quan",
    "trog": "trong",
    "nhieuf": "nhieu",
    "nheiu": "nhieu",
    "chayj": "chay",
    "duco": "duoc",
    "laix": "lai",
    "roa": "ra",
    "tih": "tinh",
    "nhug": "nhung",
    "xau": "sau",
    "dduowcj": "duoc",
    "bna": "ban",
    "duowc": "duoc",
    "duowcj": "duoc",
    "dduowc": "duoc",
    "xo": "so",
}
CHAT_PHRASES = [
    (r"\b(\d+) best sellers?\b", r"\1 mat hang ban chay nhat"),
    (r"\bbest sellers?\b", "ban chay nhat"),
    (r"\btien tuoi\b", "tien mat"),
    (r"\b(?:hom|bua) nao\b", "ngay nao"),
    # "Riêng hôm nay" is just today; "doanh thu đạt bao nhiêu" asks the revenue.
    (r"\b(?:tinh )?rieng (?:ngay )?(hom nay|hnay)\b", r"\1"),
    (r"\bdoanh thu dat (?=bao nhieu|bn|bnhiu|may)", "doanh thu "),
    (r"\bbannhieu\b", "ban nhieu"),
    # "total" is also a brand ("Bay thợ hồ Total"): only before a quantity question.
    (r"\btotal (?=(?:bao nhieu|bn|nhieu|may|tien|don)\b)", "tong cong "),
    (r"\b(?:khong|ko|k) can (?:don(?: bac si)?|toa)\b", "khong ke don"),
]
_CHAT = re.compile(
    r"(?<!\w)("
    + "|".join(sorted(map(re.escape, CHAT_ACCENTED), key=len, reverse=True))
    + r")(?!\w)(?!\s+(?:tô|hình|tả|lông|long)\b)"  # "mô tô", "ni lông" are nouns
)
# "nhiêu" alone is the Southern "bao nhiêu"; "bao nhiêu" itself stays as written.
_BARE_NHIEU = re.compile(r"(?<!\w)(?<!bao )nhiêu(?!\w)")


def question_plain(text: str) -> str:
    """plain() for question text: Telex typos and chat/regional spellings of function
    words become standard words; names and numbers are never rewritten."""
    text = unicodedata.normalize("NFC", text).casefold()
    text = _BARE_NHIEU.sub("bao nhiêu", _CHAT.sub(lambda m: CHAT_ACCENTED[m[1]], text))
    # "th8", "th 8/2025": month abbreviations.
    text = re.sub(r"(?<!\w)th\s?(?=\d{1,2}(?:\D|$))", "tháng ", text)
    text = re.sub(r"(?<!\w)th(?=\s+(?:này|nay|trước|truoc|trc|rồi|roi)(?!\w))", "tháng", text)
    text = re.sub(r"(?<!\w)th(?=\s+(?:này|nay|trước|truoc|trc|rồi|roi)(?!\w))", "tháng", text)
    text = re.sub(r"[a-z]+", lambda m: QUESTION_TYPOS.get(m[0], m[0]), plain(text))
    # Single letters and "sp" also occur in product names ("Sino SP D20", "Galaxy Z"):
    # only a closing "z"/"j" is the chat "vậy"/"gì".
    text = re.sub(
        r"(?<![\w/-])(z|j)(\s*[?.!]*)$", lambda m: {"z": "vay", "j": "gi"}[m[1]] + m[2], text
    )
    text = re.sub(r"(?<![\w/-])[a-z]+(?![\w/-])", lambda m: CHAT_PLAIN.get(m[0], m[0]), text)
    # Multi-word chat phrases of one standard meaning.
    for phrase, standard in CHAT_PHRASES:
        text = re.sub(phrase, standard, text)
    return text


@dataclass(frozen=True)
class Rule:
    rule_id: str
    status: str
    operator: str | None
    parameters: dict[str, Any]
    source_refs: list[dict]
    reason: str | None = None
    requirements: str = "metric-specific fields, grain, coverage and source authority"
    execution: str = "bounded: only registered contracts and validated plans"
    understood: bool = False


@dataclass(frozen=True)
class RuleProfile:
    split: str
    sha256: str
    parser_sha256: str
    industry: str | None
    rules: dict[str, Rule]
    clauses: list[dict] = field(default_factory=list)

    def require(self, rule_id: str) -> Rule:
        rule = self.rules.get(rule_id)
        if rule is None or rule.status != "resolved":
            raise SemanticError(
                "rule_unresolved", f"{rule_id}: {rule.reason if rule else 'rule not found'}"
            )
        return rule

    def summary(self) -> dict:
        return {
            "version": VERSION,
            "split": self.split,
            "sha256": self.sha256,
            "parser_sha256": self.parser_sha256,
            "industry": self.industry,
            "rules": {key: asdict(value) for key, value in sorted(self.rules.items())},
            "clauses": self.clauses,
        }


# Rule labels are semantic selectors, never business or question identifiers.
LABELS = {
    "dedup": "hai dong cua cung mot don giong nhau o moi o",
    "header_inheritance": "o de trong o cac dong sau cua cung mot nhom dong",
    "revenue_status": "don tinh vao doanh thu",
    "order_count_status": "so don",
    "time_axis": "don thuoc ky nao",
    "revenue_basis": "doanh thu mot don",
    "week": "tuan",
    "shift": "ca sang, ca chieu",
    "explicit_month": "thang m/y",
    "current_month": "thang nay",
    "last_n_days": "n ngay gan nhat",
    "inclusive_range": "tu x den y, nhom x–y",
    "year_to_date": "tu dau nam, tu dau nam toi nay",
    "missing_year": "thang m, quy q khong ghi nam",
    "tet": "tet nam y, dip tet, mua tet",
    "return_policy": "don tra mot phan",
    "difference": "chenh lech, hon / kem, tang / giam bao nhieu (so, khong phai %)",
    "percent_change": "phan tram thay doi, tang truong",
    "cancellation_rate": "ti le don huy trong ky",
    "customer_share": "doanh thu theo nhom khach",
    "item_group": "nhom hang",
    "item_role": "mat hang",
    "average_order_value": "gia tri trung binh mot don",
}

LABELS.update({key: label for key, (label, _) in CLAUSES.items() if not label.startswith("-")})

# Deliberately small wording variants for the same finite clauses. The value
# still has to match a supported meaning in full, including qualifications.
LABEL_ALIASES = {
    "revenue_status": {"don duoc tinh vao doanh thu"},
    "order_count_status": {"dem so don", "so luong don hang"},
    "revenue_basis": {"cach tinh doanh thu mot don"},
    "time_axis": {"ngay xac dinh ky cua don"},
}


def _unhandled_qualifications(text: str, section: str) -> set[str]:
    """Flag explicit policy amendments outside parsed clauses; never invent policy.

    This catches a bounded set of paragraph/section exceptions. It is not a
    document-wide entailment checker and cannot certify arbitrary paraphrases.
    """
    exception = (
        r"\b(ngoai le|ngoai tru|tuy nhien|khong ap dung|thay vi|rieng|"
        r"khong duoc tinh|khong tinh vao|khong tinh doanh thu|khong theo)\b"
    )
    if not re.search(exception, text + " " + plain(section)):
        return set()
    targets = {
        "revenue_status": r"\b(don dang giao|don huy|don tra hang)\b",
        "order_count_status": r"\b(so don|dem don)\b",
        "revenue_basis": r"\b(doanh thu|tien ban|tien hang)\b",
        "time_axis": r"\b(ngay dat|ngay giao|ngay thu tien)\b",
        "week": r"\btuan\b",
        "shift": r"\bca (sang|chieu)\b",
        "item_role": r"\bphi phuc vu\b",
        "current_month": r"\bthang nay\b",
        "rounding": r"\blam tron\b",
        "ordered_results": r"\b(thu tu|nhieu so)\b",
        "coverage": r"\b(khoang ngay|pham vi du lieu)\b",
        "difference": r"\b(chenh lech|ky hoi|ky so)\b",
        "percent_change": r"\b(tang truong|phan tram thay doi)\b",
        "purchase_amount": r"\b(phieu nhap|tien nhap|nhap dau ky)\b",
        "debt_balance": r"\b(con no|tong don|cong no)\b",
        "debt_cod": r"\b(cod|san tmdt)\b",
        "debt_collection": r"\b(thu no|tra no|no them|ngay mua)\b",
        "gross_profit": r"\b(lai gop|gia von)\b",
        "quantity_ranking": r"\b(ban chay|so luong|don vi co ban)\b",
        "lot_expiry": r"\b(lo|han dung|het han)\b",
        "customer_identity": r"\b(ma khach|xac dinh khach|khach le)\b",
    }
    return {key for key, pattern in targets.items() if re.search(pattern, text)}


def _decode(rule_id: str, value: str) -> tuple[str, dict] | None:
    """Finite supported meanings. Full matches reject unhandled qualifications."""
    if rule_id in CATALOGUE:
        signature = hashlib.sha256(value.encode()).hexdigest()
        meaning = CATALOGUE[rule_id]["meanings"].get(signature)
        return (rule_id, deepcopy(meaning)) if meaning is not None else None
    if rule_id in CLAUSES:
        return (
            (rule_id, deepcopy(CLAUSE_PARAMETERS[rule_id]))
            if value == CLAUSES[rule_id][1]
            else None
        )
    patterns = {
        "shift": (
            r"theo gio tao don: ca sang 06:00[–-]13:59, ca chieu 14:00[–-]21:59\. "
            r"so ca chieu voi ca sang: moc la ca sang",
            "order_clock_intervals",
            {"morning": [21600, 50400], "afternoon": [50400, 79200]},
        ),
        "item_role": (
            r"mon, hang, san pham cung nghia voi mat hang\. dong phi phuc vu khong phai mat hang: "
            r"khong xep hang, khong dem\. van tinh vao doanh thu, ke ca mau so ti trong nhom hang",
            "service_fee_in_revenue_not_product_group",
            {},
        ),
        "cancellation_rate": (
            r"phan tram don huy trong moi don dat trong ky \(theo ngay dat, moi trang thai: "
            r"ca don huy, don tra, don dang giao\)\. lam tron 2 chu so thap phan",
            "cancelled_over_all_placed_percent",
            {},
        ),
        "customer_share": (
            r"nhom theo cot nhom khach\. don khong ghi khach \(khach vang lai\) khong "
            r"thuoc nhom nao, ke ca nhom le\. ti trong cua nhom chia cho doanh thu "
            r"ca shop cung ky, tinh ca don vang lai",
            "group_over_whole_business_percent",
            {},
        ),
        "item_group": (
            r"theo cot nhom hang, dung ten nhom cau hoi neu; khong gop nhom khac ten "
            r"du ten gan nhau",
            "exact_source_group",
            {},
        ),
        "average_order_value": (
            r"lam tron ve dong, nua dong lam tron len",
            "revenue_over_eligible_orders",
            {"decimal_places": 0, "mode": "half_up"},
        ),
        "percent_change": (
            r"so voi ky truoc; giam thi ra so am",
            "current_minus_baseline_over_baseline_percent",
            {},
        ),
        "difference": (
            r"ky hoi [−-] ky so: it hon ra so am, giu don vi cua so do",
            "current_minus_baseline",
            {},
        ),
        "dedup": (
            r"loi xuat file, tinh mot lan(?:\. hai dong giong nhau cua hai don khac nhau "
            r"la hai lan ban, tinh ca hai)?",
            "exact_within_order",
            {},
        ),
        "header_inheritance": (
            r"giong o do o dong dau nhom(?:\: khach, ghi chu, trang thai cua don ap cho "
            r"moi dong cua don\. khong phai thieu du lieu, khong co nghia la khach le "
            r"hay don hoan thanh)?",
            "inherit_order_headers",
            {},
        ),
        "time_axis": (
            r"theo ngay dat, khong theo ngay giao hay ngay thu tien"
            r"(?:\. don tra hang sau ky, ke ca tra sau hom nay, "
            r"van bi loai khoi doanh thu ky dat)?",
            "ordered_at",
            {"return_status_cutoff": "recorded_even_after_period_and_as_of"},
        ),
        "revenue_basis": (
            r"tong thanh tien cac dong hang \(da tru giam gia tren tung dong\)\. "
            r"khong tru giam gia tren ca don \(khuyen mai theo tong tien don, voucher shop\), "
            r"khong cong thue vat, khong gom phi giao hang\. tien hang, tien ban "
            r"cua don ban cung nghia voi doanh thu",
            "line_net_amount",
            {"excluded": ["order_discount", "vat", "shipping_fee"]},
        ),
        "explicit_month": (
            r"ca thang, tu ngay 1 den het ngay cuoi thang",
            "whole_month",
            {},
        ),
        "current_month": (
            r"tu ngay 1 den het hom nay\. so voi thang truoc thi lay cung so ngay "
            r"cua thang truoc: tu ngay 1 den het ngay trung so voi hom nay; "
            r"thang truoc khong co ngay do thi den het thang truoc",
            "month_to_date",
            {"comparison": "previous_month_same_days"},
        ),
        "last_n_days": (
            r"n ngay lien nhau ke ca hom nay, ngay cuoi la hom nay",
            "last_n_including_today",
            {},
        ),
        "inclusive_range": (r"gom ca x va y", "inclusive", {}),
        "year_to_date": (r"tu 01/01 cua nam nay den het hom nay", "year_to_date", {}),
        "missing_year": (
            r"cua nam nay neu khong sau hom nay; sau hom nay thi cua nam truoc\. "
            r"quy theo duong lich, moi quy 3 thang tinh tu thang 1",
            "latest_started_period",
            {},
        ),
        "tet": (
            r"tu ngay duong cua 23 thang chap \(nam am y [−-] 1\) den het ngay duong "
            r"cua mung 10 thang gieng \(nam am y\)\. so voi truoc tet = cung so ngay "
            r"ngay truoc 23 thang chap\. tra lich am trong calendar\.csv cung thu muc",
            "lunar_tet",
            {"start_month": 12, "start_day": 23, "end_month": 1, "end_day": 10},
        ),
        "return_policy": (r"bo ca don", "whole_order_return", {}),
        "recorded_status": (
            r"rieng trang thai huy, tra hang cua don lay theo trang thai ghi tren don, "
            r"ke ca khi phieu tra ghi ngay sau hom nay",
            "recorded_order_status",
            {},
        ),
        "coverage": (
            r"ky chi co mot phan nam trong khoang ngay co du lieu thi tinh tren phan co du lieu",
            "intersect_available",
            {},
        ),
        "as_of": (
            r"hom nay cua moi cau la truong as_of_date trong questions\.jsonl",
            "question_as_of",
            {},
        ),
        "outside_coverage": (
            r"khong co du lieu khac voi bang 0: ky nam hoan toan ngoai khoang ngay co "
            r"du lieu cua shop, hoac shop khong ghi loai thong tin cau hoi can, "
            r"thi khong tra loi duoc, khong phai 0",
            "unanswerable_outside",
            {},
        ),
        "ordered_results": (
            r"thi ghi list theo dung thu tu cau hoi neu",
            "ordered_scalar_results",
            {"ordering": "question_order"},
        ),
        "rounding": (
            r"lam tron: tien ra so le lam tron ve dong; trung binh cua mot khoan tien "
            r"cung la tien, lam tron ve dong\. phan tram va so trung binh khac lam tron "
            r"2 chu so thap phan, tru khi mot luat khac trong rulebook ghi khac",
            "output_rounding",
            {"money_places": 0, "other_places": 2, "unspecified_tie": "abstain"},
        ),
    }
    if rule_id in patterns:
        pattern, operator, parameters = patterns[rule_id]
        return (operator, parameters) if re.fullmatch(pattern, value) else None
    if rule_id == "revenue_status":
        match = re.fullmatch(
            r"don khong huy va khong tra hang\. don co tra hang, ke ca chi tra mot phan, "
            r"la don tra hang: bo ca don\. don dang giao (van tinh|khong tinh)",
            value,
        )
        if match:
            statuses = ["completed", "shipping"] if match[1] == "van tinh" else ["completed"]
            return "include_statuses", {"statuses": statuses}
    if rule_id == "order_count_status" and re.fullmatch(
        r"chi tinh don khong huy, khong tra; ca khi dem so don co mot mat hang, "
        r"so don cua mot khach\. khach da mua trong ky la khach co it nhat mot don "
        r"nhu vay trong ky",
        value,
    ):
        return "include_statuses", {"statuses": ["completed", "shipping"]}
    if rule_id == "week":
        match = re.fullmatch(
            r"bat dau (thu hai|chu nhat)\. tuan nay: tu (thu hai|chu nhat) cua tuan chua "
            r"hom nay den het hom nay\. tuan truoc, tuan roi: ca tuan lien truoc, "
            r"(thu hai den chu nhat|chu nhat den thu bay)",
            value,
        )
        if match and match[1] == match[2]:
            expected = "thu hai den chu nhat" if match[1] == "thu hai" else "chu nhat den thu bay"
            if match[3] == expected:
                return "calendar_week", {"first_weekday": 0 if match[1] == "thu hai" else 6}
    return None


def _units(text: str):
    """Join Markdown soft wraps, retaining exact physical quotes and line spans."""
    paragraph, start = [], 0
    for number, raw in enumerate([*text.splitlines(), ""], 1):
        heading = raw.lstrip()
        boundary = not heading or heading.startswith(("#", "|", "- "))
        # Preserve the established plain-label format too. Two complete labelled
        # declarations can occupy consecutive physical lines without a blank.
        # An incomplete clause or arbitrary sentence never becomes a boundary.
        labels = {
            key: LABELS[key] + ":" for key in ("dedup", "header_inheritance", "return_policy")
        }
        if paragraph and any(plain(raw).startswith(label) for label in labels.values()):
            previous = plain("\n".join(paragraph))
            boundary = boundary or any(
                previous.startswith(label) and _decode(key, previous[len(label) :].strip())
                for key, label in labels.items()
            )
        if boundary and paragraph:
            yield start, "\n".join(paragraph)
            paragraph = []
        if not heading:
            continue
        if heading.startswith(("#", "|")):
            yield number, raw
        else:
            if not paragraph:
                start = number
            paragraph.append(raw)


def parse_rulebook(
    text: str, split: str, *, industry: str | None = None, source_sha256: str | None = None
) -> RuleProfile:
    digest = source_sha256 or hashlib.sha256(text.encode()).hexdigest()
    candidates: dict[str, list[tuple[str | None, str, dict]]] = {}
    clauses = []
    section, scope = "", None

    def add(rule_id: str, value: str, line: int, quote: str) -> None:
        ref = {
            "split": split,
            "file": "rulebook.md",
            "sha256": digest,
            "section": section,
            "scope": scope or "general",
            "line": line,
            "line_end": line + len(quote.splitlines()) - 1,
            "quote": quote,
        }
        candidates.setdefault(rule_id, []).append((scope, value, ref))

    for number, raw in _units(text):
        before = sum(len(entries) for entries in candidates.values())
        heading = raw.lstrip()
        if heading.startswith("## "):
            section, scope = heading[3:].strip(), None
        elif heading.startswith("### "):
            scope = plain(heading[4:])
        elif heading.startswith("# "):
            section, scope = heading[2:].strip(), None
        parts = raw.strip().strip("|").split("|")
        is_row = raw.lstrip().startswith("|") and len(parts) >= 2
        label = plain(parts[0]) if is_row else ""
        # Capture every substantive unit, even when no executable interpretation
        # is known. This ledger is evidence, never permission to run an operator.
        substantive = bool(raw.strip()) and not heading.startswith("#")
        substantive = substantive and not (
            is_row and (label in {"luat", "cach noi", "cach ghi", "type", "tu"} or "---" in label)
        )
        full_value = " | ".join(plain(p) for p in parts[1:]) if is_row else plain(raw)
        complete_prose = False
        for rule_id, definition in CATALOGUE.items():
            selector = definition["selector"]
            if (is_row and label == selector and (scope or "general") == definition["scope"]) or (
                not is_row and selector.startswith("@") and _decode(rule_id, full_value) is not None
            ):
                add(rule_id, full_value, number, raw)
                complete_prose = not is_row
        if raw.lstrip().startswith("|") and len(parts) >= 2:
            label = plain(parts[0])
            value = plain(parts[1]) if len(parts) == 2 else ""
            for rule_id, expected in LABELS.items():
                if label in {expected, *LABEL_ALIASES.get(rule_id, ())}:
                    add(rule_id, value, number, raw)
            if label in {LABELS["revenue_status"], *LABEL_ALIASES["revenue_status"]}:
                # Classification and query-time filtering refer to the same clause.
                value = "bo ca don" if _decode("revenue_status", value) else value
                add("return_policy", value, number, raw)
        else:
            cleaned = plain(raw)
            for rule_id in ["dedup", "header_inheritance", "return_policy"]:
                label = LABELS[rule_id]
                if cleaned.startswith(label + ":"):
                    add(rule_id, cleaned[len(label) + 1 :].strip(), number, raw)
            if cleaned.startswith("hom nay cua moi cau la truong"):
                sentences = cleaned.split(". ")
                add("as_of", sentences[0], number, raw)
                if "rieng trang thai" in cleaned:
                    add(
                        "recorded_status",
                        "rieng trang thai" + cleaned.split("rieng trang thai", 1)[1],
                        number,
                        raw,
                    )
                # The anchor and status fragments cannot authorize unrelated
                # text between them. The released full paragraph is recognized
                # above; the shorter equivalent anchor/status form is explicit.
                short_form = (
                    len(sentences) == 3
                    and _decode("as_of", sentences[0]) is not None
                    and sentences[1] == "moi cach noi tuong doi tinh theo ngay nay"
                    and _decode("recorded_status", sentences[2]) is not None
                )
                if not complete_prose and not short_form:
                    for key in {"as_of", "recorded_status"} | _unhandled_qualifications(
                        "ngoai le " + cleaned, section
                    ):
                        add(key, "unhandled paragraph: " + cleaned, number, raw)
            if cleaned.startswith("- ky chi co mot phan"):
                add("coverage", cleaned[2:], number, raw)
            if cleaned.startswith("- khong co du lieu khac voi bang 0"):
                add("outside_coverage", cleaned[2:], number, raw)
            for rule_id, (label, _) in CLAUSES.items():
                if label.startswith("-") and cleaned.startswith(label):
                    add(rule_id, cleaned[len(label) :].strip(), number, raw)
            if cleaned.startswith("- lam tron:"):
                add("rounding", cleaned[2:], number, raw)
            if "cau hoi nhieu so " in cleaned:
                add("ordered_results", cleaned.split("cau hoi nhieu so ", 1)[1], number, raw)
                if not complete_prose and not cleaned.startswith("cau hoi nhieu so "):
                    for key in {"ordered_results"} | _unhandled_qualifications(
                        "ngoai le " + cleaned, section
                    ):
                        add(key, "unhandled paragraph: " + cleaned, number, raw)
        if not heading.startswith("#") and before == sum(
            len(entries) for entries in candidates.values()
        ):
            for rule_id in _unhandled_qualifications("ngoai le " + plain(raw), section):
                add(rule_id, "unhandled qualification: " + plain(raw), number, raw)
        if substantive:
            ids = sorted(
                key
                for key, entries in candidates.items()
                if any(r["line"] == number for _, _, r in entries)
            )
            clauses.append(
                {
                    "line": number,
                    "line_end": number + len(raw.splitlines()) - 1,
                    "section": section,
                    "scope": scope or "general",
                    "quote": raw,
                    "rule_ids": ids,
                    "status": "mapped" if ids else "unmapped",
                }
            )
    rules = {}
    for rule_id in [
        *dict.fromkeys([*LABELS, *CLAUSES]),
        "recorded_status",
        "coverage",
        "outside_coverage",
        "as_of",
        "rounding",
        "ordered_results",
        *[key for key in CATALOGUE if key in candidates],
    ]:
        entries = candidates.get(rule_id, [])
        if rule_id in CLAUSES and not entries:
            continue
        specific = [entry for entry in entries if entry[0] == plain(industry or "")]
        unknown_scope = industry is None and any(entry[0] is not None for entry in entries)
        selected = specific or [entry for entry in entries if entry[0] is None]
        meanings = [_decode(rule_id, value) for _, value, _ in selected]
        if unknown_scope:
            status, reason = "ambiguous", "industry override requires a verified industry"
        elif entries and not selected:
            status, reason = "not_applicable", "clause belongs to another verified industry"
        elif not meanings or any(value is None for value in meanings):
            status, reason = "unsupported", "missing or unrecognized rule clause"
        elif any(value != meanings[0] for value in meanings):
            status, reason = "ambiguous", "conflicting rule definitions"
        else:
            status, reason = "resolved", None
        meaning = meanings[0] if status == "resolved" else None
        rules[rule_id] = Rule(
            rule_id,
            status,
            meaning[0] if meaning else None,
            meaning[1] if meaning else {},
            [ref for _, _, ref in (entries if unknown_scope or not selected else selected)],
            reason,
            CATALOGUE[rule_id]["requirements"]
            if rule_id in CATALOGUE
            else "metric-specific fields, grain, coverage and source authority",
            CATALOGUE[rule_id]["execution"]
            if rule_id in CATALOGUE
            else EXECUTION.get(rule_id, "bounded: only registered contracts and validated plans"),
            bool(entries) and all(_decode(rule_id, value) is not None for _, value, _ in entries),
        )
    for clause in clauses:
        clause["understood"] = bool(clause["rule_ids"]) and all(
            _decode(key, value) is not None
            for key in clause["rule_ids"]
            for _, value, ref in candidates[key]
            if ref["line"] == clause["line"]
        )
    return RuleProfile(
        split,
        digest,
        hashlib.sha256(
            Path(__file__).read_bytes() + Path(__file__).with_name("business_rules.py").read_bytes()
        ).hexdigest(),
        industry,
        rules,
        clauses,
    )
