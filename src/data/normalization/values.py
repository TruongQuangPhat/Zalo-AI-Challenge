"""Strict, source-aware scalar parsers; raw values remain in the immutable source."""

from __future__ import annotations

import re
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext

from src.data.ingestion import Cell
from src.data.profiling import normalized
from src.jsonio import ContractError

STATUS_ALIASES: ContextVar[dict[str, str] | None] = ContextVar("status_aliases", default=None)
"""Normalized status token -> canonical status, set only around a build by ``_store``."""


class ParseError(ContractError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def cell_value(cell: Cell | None):
    if cell is None:
        raise ParseError("source_missing", "required source value is missing")
    if cell.native_type == "error" or str(cell.raw_value).startswith(("#N/A", "#VALUE!")):
        raise ParseError("native_error", "source contains an error")
    if cell.native_type == "formula" and cell.metadata.get("cache_status") != "present":
        raise ParseError("formula_cache", "formula has no usable cached value")
    if cell.raw_value is None or cell.raw_value == "":
        raise ParseError("source_missing", "required source value is missing")
    return cell.raw_value


def exact_add(left: Decimal, right: Decimal) -> Decimal:
    precision = (
        max(left.adjusted(), right.adjusted(), 0)
        - min(left.as_tuple().exponent, right.as_tuple().exponent, 0)
        + 3
    )
    with localcontext() as context:
        context.prec = precision
        return left + right


def parse_number(value, convention: str | None = None) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float) or value is None:
        raise ParseError("number_type", "numbers require exact source tokens, not bool/float/null")
    token = str(value).strip()
    if len(token) > 256:
        raise ParseError("number_budget", "numeric token is too long")
    patterns = {
        "comma_grouped": (r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", ",", "."),
        "dot_grouped": (r"[+-]?(?:\d+|\d{1,3}(?:\.\d{3})+)(?:,\d+)?", ".", ","),
        "decimal_dot": (r"[+-]?\d+(?:\.\d+)?", "", "."),
        "decimal_comma": (r"[+-]?\d+(?:,\d+)?", "", ","),
    }
    if convention is None:
        if re.fullmatch(r"[+-]?\d+", token):
            convention = "decimal_dot"
        elif re.fullmatch(r"[+-]?\d+[.,]\d{1,2}", token):
            convention = "decimal_comma" if "," in token else "decimal_dot"
        else:
            raise ParseError("number_ambiguous", "separator convention is not established")
    if convention not in patterns:
        raise ParseError("number_convention", "unsupported separator convention")
    pattern, group, decimal = patterns[convention]
    if not re.fullmatch(pattern, token):
        raise ParseError("number_format", "value does not match the source convention")
    token = token.replace(group, "") if group else token
    token = token.replace(decimal, ".")
    try:
        number = Decimal(token)
    except InvalidOperation as exc:
        raise ParseError("number_format", "invalid numeric token") from exc
    if not number.is_finite():
        raise ParseError("number_format", "non-finite number")
    return number


def parse_money(value, convention: str | None = None) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float) or value is None:
        raise ParseError("money_type", "money requires an exact source token")
    token = str(value).strip().casefold()
    token = re.sub(r"\s*(?:vnd|vnđ|đ|₫)$", "", token).strip()
    compact = re.fullmatch(r"([+-]?\d+)tr(\d{1,3})", token)
    if compact:
        sign = -1 if compact[1].startswith("-") else 1
        with localcontext() as context:
            context.prec = len(token) + 10
            return Decimal(compact[1]) * 1000000 + sign * Decimal("0." + compact[2]) * 1000000
    suffix = re.fullmatch(r"(.+?)\s*(trieu|triệu|tr|nghin|nghìn|ngan|ngàn|k)", token)
    if suffix:
        factor = 1000000 if suffix[2] in {"trieu", "triệu", "tr"} else 1000
        number = parse_number(suffix[1], convention)
        with localcontext() as context:
            context.prec = len(number.as_tuple().digits) + 10
            return number * factor
    return parse_number(token, convention)


def number_evidence(value: str) -> str | None:
    """Only unambiguous punctuation establishes a source-wide convention."""
    value = re.sub(r"\s*(?:VND|VNĐ|đ|₫)$", "", value.strip(), flags=re.I).strip()
    if re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3}){2,}(?:\.\d+)?", value) or re.fullmatch(
        r"[+-]?\d{1,3}(?:,\d{3})+\.\d+", value
    ):
        return "comma_grouped"
    if re.fullmatch(r"[+-]?\d{1,3}(?:\.\d{3}){2,}(?:,\d+)?", value) or re.fullmatch(
        r"[+-]?\d{1,3}(?:\.\d{3})+,\d+", value
    ):
        return "dot_grouped"
    return None


_SUMMARY = re.compile(
    r"^(?:tong|cong)(?:\s+cong\b|\s+(?:ngay|thang|ky|nam)(?:\s*[:-]?\s*\d|\s*$)|\s*[:-]?\s*\d)"
)


def summary_label(value) -> bool:
    """A subtotal label: "Cộng ngày 01/05", "Tổng 01/05", "Tổng cộng", "Tổng tháng 5".

    A word after "tổng/cộng" that is not a period word followed by a number is a name
    ("Tống Thắng Khánh"), not a subtotal.
    """
    return value is not None and bool(_SUMMARY.match(normalized(str(value)).strip()))


# Purchase receipt statuses (plain text); anything else is never guessed.
PURCHASE_RECEIVED = frozenset({"received", "da nhap", "da nhap hang", "da nhap kho"})
PURCHASE_CANCELLED = frozenset({"cancelled", "huy", "da huy"})


def annotation_label(value) -> bool:
    """A footnote written into the table: "Ghi chú: số liệu ghi tay", "Lưu ý: …"."""
    return value is not None and bool(
        re.match(r"(?:ghi chu|chu thich|luu y)\s*:", normalized(str(value)).strip())
    )


def label_cell(value) -> bool:
    """A subtotal or footnote label: never an order ID, date or status by itself."""
    return summary_label(value) or annotation_label(value)


def strip_unit(value, unit):
    """ "2 ly" -> "2" only when the word is the line's own selling unit; else unchanged."""
    if not isinstance(value, str) or unit in {None, ""}:
        return value
    match = re.fullmatch(r"([+-]?\d+(?:[.,]\d+)?)\s*(\D.*)", value.strip())

    def same(a: str) -> bool:
        return re.sub(r"\W", "", normalized(a)) == re.sub(r"\W", "", normalized(str(unit)))

    return match[1] if match and same(match[2]) else value


def parse_datetime(value, *, year: int | None = None, date_system: str | None = None) -> datetime:
    if isinstance(value, bool) or isinstance(value, float) or value is None:
        raise ParseError("date_type", "date requires a source token and date-role evidence")
    token = str(value).strip()
    try:
        if date_system is not None and re.fullmatch(r"\d+(?:\.\d+)?", token):
            serial = parse_number(token, "decimal_dot")
            days = int(serial)
            if date_system not in {"1900", "1904"} or (date_system == "1900" and days == 60):
                raise ParseError("date_system", "unknown date system or Excel's fictitious day")
            base = datetime(1904, 1, 1) if date_system == "1904" else datetime(1899, 12, 31)
            with localcontext() as context:
                context.prec = len(serial.as_tuple().digits) + 20
                fraction = serial - days
                micros = fraction * 86400000000
            if micros != micros.to_integral_value():
                raise ParseError("date_precision", "serial cannot be represented in microseconds")
            return base + timedelta(
                days=days - (date_system == "1900" and days > 60), microseconds=int(micros)
            )
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ].+)?", token):
            return datetime.fromisoformat(token.replace("Z", "+00:00"))
        vietnamese = re.fullmatch(r"(\d{1,2})\s+thg\s+(\d{1,2})\s+(\d{4})", token, re.I)
        if vietnamese:
            return datetime(int(vietnamese[3]), int(vietnamese[2]), int(vietnamese[1]))
        if re.fullmatch(r"\d{1,2}/\d{1,2}", token):
            if year is None:
                raise ParseError("date_year", "missing year without sheet/source evidence")
            token += f"/{year}"
        for pattern in ["%d/%m/%Y", "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"]:
            try:
                return datetime.strptime(token, pattern)
            except ValueError:
                continue
    except ParseError:
        raise
    except (ValueError, OverflowError) as exc:
        raise ParseError("date_format", "invalid date") from exc
    raise ParseError("date_format", "unsupported or ambiguous date")


def parse_date(value, *, year: int | None = None, date_system: str | None = None) -> date:
    return parse_datetime(value, year=year, date_system=date_system).date()


STATUSES: dict[str, set[str]] = {
    "completed": {
        "completed",
        "hoan thanh",
        "da hoan thanh",
        "xong",
        "thanh cong",
        "da lay",
        "da xong",
        "da giao",
        "ok",
    },
    "cancelled": {
        "cancelled",
        "huy",
        "da huy",
        "khach huy",
        "cancel",
        "khong giao duoc",
        "huy don",
        "da huy don",
    },
    "returned": {
        "returned",
        "tra hang",
        "doi tra",
        "hoan tra",
        "hoan hang",
        "da hoan hang",
        "da tra hang",
        "tra lai",
        "hang tra ve",
        "khach mang tra",
        "khach mang tra lai",
        "khach tra hang",
    },
    # Active recorded order: revenue/count exclude cancellation and returns.
    # This alias does not certify a physical shipment for future shipping metrics.
    "shipping": {
        "shipping",
        "in_delivery",
        "dang giao",
        "dang giao hang",
        "cho giao",
        "dang giao dich",
        "dang gui",
        "dang xu ly",
        "dang van chuyen",
        "dang ship",
        "chua giao",
        "chua giao hang",
    },
}


# Status-column values only. In a free-text note "khách trả" may be how the customer paid,
# so ``note_status`` keeps it ambiguous; as a delivery/order status it is a return.
COLUMN_STATUSES = {"khach tra": "returned"}


def normalize_status(value) -> str:
    token = normalized(str(value)).strip()
    for status, labels in STATUSES.items():
        if token in labels:
            return status
    if token in COLUMN_STATUSES:
        return COLUMN_STATUSES[token]
    aliases = STATUS_ALIASES.get() or {}
    # "not_status" (a note that says nothing about the outcome) is no status-column value.
    if aliases.get(token, "not_status") != "not_status":
        return aliases[token]
    raise ParseError("status_ambiguous", "unknown order status; no default was applied")


# Words that make a free-text note possibly about the order's outcome; a note with none of
# them does not mention a status (rulebook: such an order is completed). "Trả tiền/nợ/trước"
# is how the customer paid, and a warranty note ("bảo hành") is a ticket kind, not an outcome.
_PAID = r"(?! (?:tien|no|truoc|sau|ck|chuyen khoan|bang|qua|du|het|gop|coc|mat))"
_NOTE_CUE = re.compile(
    r"(?<!\w)(?:tra" + _PAID + r"|huy|hoan|doi|cancel|return|refund|bom|boom|"
    r"khong (?:lay|nhan)|giao|ship)(?!\w)"
)


def note_status(note) -> str:
    """Status of an order whose shop writes it in the note column (no status column).

    Empty note or no status wording: completed. A note that is exactly a status word, or
    contains wording of exactly one status, takes it; model aliases come next. Anything
    else that mentions an outcome stays ``status_ambiguous`` (never defaulted).
    """
    if note is None or not str(note).strip():
        return "completed"
    token = normalized(str(note)).strip()
    for status, labels in STATUSES.items():
        if token in labels:
            return status
    aliases = STATUS_ALIASES.get() or {}
    if token in aliases:
        return "completed" if aliases[token] == "not_status" else aliases[token]
    # "kiểm tra lại" is a check, not "trả lại" (a return): drop it before looking for status
    # wording.
    token = re.sub(r"(?<!\w)kiem tra(?!\w)", " ", token)
    found = {
        status
        for status, labels in STATUSES.items()
        for label in labels
        if len(label) > 2 and re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)" + _PAID, token)
    }
    if len(found) == 1:
        return found.pop()
    if found or _NOTE_CUE.search(token):
        raise ParseError("status_ambiguous", "note may state an order status")
    return "completed"


def serial_system(tokens: list[str], declared_end: str | None) -> str | None:
    """Resolve CSV serial epochs only when a declared source bound rejects the other.

    The column must already be mapped as a date. No calendar/question bounds or
    numeric-ID guesses enter this evidence check. Ambiguity remains blocking.
    """
    if not tokens:
        return None
    if declared_end is None:
        raise ParseError("date_system", "CSV date serial has no epoch evidence")
    end = parse_date(declared_end)
    valid = []
    for system in ["1900", "1904"]:
        try:
            if all(parse_date(token, date_system=system) <= end for token in tokens):
                valid.append(system)
        except ParseError:
            pass
    if len(valid) != 1:
        raise ParseError("date_system", "source bounds do not identify one CSV serial epoch")
    return valid[0]


def consistent_grouping(tokens: list[str]) -> str | None:
    """User-approved 2026-11-04 policy: consistent groups of three mean thousands.

    This is an explicit export convention, not an answer/price heuristic. Retain
    the basis and ambiguity warning in prepared metadata. Mixed formats abstain.
    """
    cleaned = [
        re.sub(r"\s*(?:vnd|vnđ|đ|₫)$", "", str(t).strip(), flags=re.I).strip()
        for t in tokens
        if str(t).strip()
    ]
    candidates = []
    for style, pattern in [
        ("comma_grouped", r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?"),
        ("dot_grouped", r"[+-]?\d{1,3}(?:\.\d{3})+(?:,\d+)?"),
    ]:
        if not any(re.fullmatch(pattern, t) for t in cleaned):
            continue
        try:
            for t in cleaned:
                parse_money(t, style)
        except ParseError:
            continue
        candidates.append(style)
    return candidates[0] if len(candidates) == 1 else None
