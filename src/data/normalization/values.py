"""Strict, source-aware scalar parsers; raw values remain in the immutable source."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext

from src.data.ingestion import Cell
from src.data.profiling import normalized
from src.jsonio import ContractError


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


def normalize_status(value) -> str:
    statuses = {
        "completed": {"completed", "hoan thanh", "da hoan thanh", "xong"},
        "cancelled": {"cancelled", "huy", "da huy", "khach huy"},
        "returned": {"returned", "tra hang", "doi tra", "hoan tra", "hoan hang", "da hoan hang"},
        # Active recorded order: revenue/count exclude cancellation and returns.
        # This alias does not certify a physical shipment for future shipping metrics.
        "shipping": {
            "shipping",
            "in_delivery",
            "dang giao",
            "dang giao hang",
            "cho giao",
            "dang giao dich",
        },
    }
    token = normalized(str(value)).strip()
    for status, labels in statuses.items():
        if token in labels:
            return status
    raise ParseError("status_ambiguous", "unknown order status; no default was applied")
