"""Recognize a bounded rule vocabulary with source evidence and explicit failures.

Only complete, recognized clauses become executable policies. Unknown text in a
relevant rule, conflicting definitions, or an unresolved industry never falls
back silently to training defaults.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.data.profiling import normalized
from src.jsonio import ContractError

VERSION = "runtime-rules-v7"


class SemanticError(ContractError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def plain(text: str) -> str:
    return re.sub(r"\s+", " ", normalized(text).translate(str.maketrans("", "", "*`"))).strip(" .:")


@dataclass(frozen=True)
class Rule:
    rule_id: str
    status: str
    operator: str | None
    parameters: dict[str, Any]
    source_refs: list[dict]
    reason: str | None = None


@dataclass(frozen=True)
class RuleProfile:
    split: str
    sha256: str
    parser_sha256: str
    industry: str | None
    rules: dict[str, Rule]

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
    }
    return {key for key, pattern in targets.items() if re.search(pattern, text)}


def _decode(rule_id: str, value: str) -> tuple[str, dict] | None:
    """Finite supported meanings. Full matches reject unhandled qualifications."""
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
            r"theo ngay dat, khong theo ngay giao hay ngay thu tien",
            "ordered_at",
            {},
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


def parse_rulebook(
    text: str, split: str, *, industry: str | None = None, source_sha256: str | None = None
) -> RuleProfile:
    digest = source_sha256 or hashlib.sha256(text.encode()).hexdigest()
    candidates: dict[str, list[tuple[str | None, str, dict]]] = {}
    section, scope = "", None

    def add(rule_id: str, value: str, line: int, quote: str) -> None:
        ref = {
            "split": split,
            "file": "rulebook.md",
            "sha256": digest,
            "section": section,
            "scope": scope or "general",
            "line": line,
            "quote": quote,
        }
        candidates.setdefault(rule_id, []).append((scope, value, ref))

    for number, raw in enumerate(text.splitlines(), 1):
        before = sum(len(entries) for entries in candidates.values())
        heading = raw.lstrip()
        if heading.startswith("## "):
            section, scope = heading[3:].strip(), None
        elif heading.startswith("### "):
            scope = plain(heading[4:])
        elif heading.startswith("# "):
            section, scope = heading[2:].strip(), None
        parts = raw.strip().strip("|").split("|")
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
            if cleaned.startswith("- ky chi co mot phan"):
                add("coverage", cleaned[2:], number, raw)
            if cleaned.startswith("- khong co du lieu khac voi bang 0"):
                add("outside_coverage", cleaned[2:], number, raw)
            if cleaned.startswith("- lam tron:"):
                add("rounding", cleaned[2:], number, raw)
            if "cau hoi nhieu so " in cleaned:
                add("ordered_results", cleaned.split("cau hoi nhieu so ", 1)[1], number, raw)
        if not heading.startswith(("#", "|")) and before == sum(
            len(entries) for entries in candidates.values()
        ):
            for rule_id in _unhandled_qualifications(plain(raw), section):
                add(rule_id, "unhandled qualification: " + plain(raw), number, raw)
    rules = {}
    for rule_id in [
        *LABELS,
        "recorded_status",
        "coverage",
        "outside_coverage",
        "as_of",
        "rounding",
        "ordered_results",
    ]:
        entries = candidates.get(rule_id, [])
        specific = [entry for entry in entries if entry[0] == plain(industry or "")]
        unknown_scope = industry is None and any(entry[0] is not None for entry in entries)
        selected = specific or [entry for entry in entries if entry[0] is None]
        meanings = [_decode(rule_id, value) for _, value, _ in selected]
        if unknown_scope:
            status, reason = "ambiguous", "industry override requires a verified industry"
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
            [ref for _, _, ref in (entries if unknown_scope else selected)],
            reason,
        )
    return RuleProfile(
        split, digest, hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), industry, rules
    )
