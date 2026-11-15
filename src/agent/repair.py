"""Repair-book rules (rulebook section "Sửa xe") over a one-ticket-per-row ledger.

Every row is one ticket (warranty tickets included); money is the ticket total. Vehicles
are identified by plate, mechanics by the shop's mechanic list. Any doubt abstains: an
unresolved nickname, an unknown column, a tie the rule does not break.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from src.data.catalog import BusinessContext
from src.data.split import Question
from src.jsonio import ContractError
from src.semantics.rulebook import plain

PLATE = re.compile(r"\b(\d{2}\s?[A-Z]{1,2}\d?)\s?-?\s?(\d{3}\.?\d{2}|\d{4,5})\b", re.I)
HONORIFICS = {"a", "anh", "chi", "c", "em", "e", "co", "chu", "bac", "thay", "tho"}


@dataclass(frozen=True)
class Ticket:
    day: str
    amount: Decimal
    status: str
    plate: str | None
    mechanic: str | None  # full name from the mechanic list, None if unresolved
    nickname: str
    warranty: bool
    model: str = ""  # vehicle model as written, plate removed


def _sections(readme: str) -> dict[str, dict[str, str]]:
    found, section = {}, None
    for line in readme.splitlines():
        if line.startswith("#"):
            match = re.match(r"^## Cột trong `([^`]+)`", line)
            section = Path(match[1]).name if match else None
            if section:
                found.setdefault(section, {})
        elif section and (bullet := re.match(r"^\s*-\s+\*\*(.+?)\*\*\s*:\s*(.+)$", line)):
            found[section][plain(bullet[1])] = plain(bullet[2])
    return found


def _column(profile: dict, meanings: dict[str, str], pattern: str) -> int | None:
    hits = [
        c["position"]
        for c in profile["columns"]
        if re.search(pattern, meanings.get(plain(c["display_name"]), ""))
    ]
    return hits[0] if len(hits) == 1 else None


def _mechanics(context: BusinessContext, sections: dict) -> dict[str, Decimal | None]:
    """Full name -> commission percent, from the table README calls the mechanic list."""
    result: dict[str, Decimal | None] = {}
    for profile in context.catalog["tables"]:
        meanings = sections.get(Path(profile["source_ref"]["file"]).name, {})
        name = _column(profile, meanings, r"\bho ten tho\b|\bten tho\b")
        rate = _column(profile, meanings, r"phan tram hoa hong")
        if name is None or profile["structure_status"] != "resolved":
            continue
        header = set(profile.get("header_rows", []))
        for row in context.source_table(profile["source_id"]).iter_rows():
            if row.row in header or name not in row.cells:
                continue
            full = str(row.cells[name].raw_value or "").strip()
            if not full:
                continue
            pct = None
            if rate is not None and rate in row.cells:
                try:
                    pct = Decimal(str(row.cells[rate].raw_value).strip().rstrip("%"))
                except ArithmeticError:
                    pct = None
            result[full] = pct
    return result


def _resolve(nickname: str, mechanics: dict) -> str | None:
    """ "a Đức", "A.Đức", "Đức" -> the one mechanic whose given name is Đức."""
    words = [w for w in re.split(r"[\s.]+", plain(nickname)) if w]
    while len(words) > 1 and words[0] in HONORIFICS:
        words = words[1:]
    if not words:
        return None
    exact = [m for m in mechanics if plain(m) == " ".join(words)]
    if len(exact) == 1:
        return exact[0]
    given = [m for m in mechanics if plain(m).split()[-1] == words[-1]]
    return given[0] if len(given) == 1 and len(words) == 1 else None


def tickets(context: BusinessContext) -> tuple[list[Ticket], dict] | None:
    """Tickets of the prepared ledger plus the mechanic list; None if not a repair book."""
    from src.data.normalization.relational import data_rows, relational_mapping

    prepared = context.prepared
    if prepared is None or prepared.summary.get("shape") != "sales_ledger":
        return None
    sections = _sections(context.catalog["readme"])
    try:
        mapping = relational_mapping(context)
    except ContractError:
        return None
    ledgers = [s for s in mapping.sources if s.role == "ledger"]
    if not ledgers:
        return None
    with closing(prepared.connect()) as conn:
        events = {
            key: (day[:10], Decimal(amount), status)
            for key, day, amount, status in conn.execute(
                "SELECT event_key,occurred_at,net_amount,status FROM sales_events "
                "WHERE net_amount IS NOT NULL"
            )
        }
    mechanics = _mechanics(context, sections)
    result = []
    for source in ledgers:
        meanings = sections.get(Path(source.profile["source_ref"]["file"]).name, {})
        plate_at = _column(source.profile, meanings, r"bien so")
        mechanic_at = _column(source.profile, meanings, r"\btho\b")
        note_at = source.fields.get("note")
        if plate_at is None or mechanic_at is None:
            return None
        for table, row in data_rows(context, source):
            event = events.get(row.identity(table))
            if event is None:
                continue
            day, amount, status = event

            def text(position, cells=row.cells):
                cell = cells.get(position) if position is not None else None
                return str(cell.raw_value).strip() if cell and cell.raw_value else ""

            plate = PLATE.search(text(plate_at))
            nickname = text(mechanic_at)
            result.append(
                Ticket(
                    day=day,
                    amount=amount,
                    status=status,
                    plate=re.sub(r"\W", "", (plate[1] + plate[2]).upper()) if plate else None,
                    mechanic=_resolve(nickname, mechanics) if nickname else None,
                    nickname=nickname,
                    warranty=bool(re.search(r"\bbao hanh\b", plain(text(note_at)))) and amount == 0,
                    model=plain(PLATE.sub(" ", text(plate_at))).strip(),
                )
            )
    return result, mechanics


def _money(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _percent(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _named_mechanic(text: str, mechanics: dict) -> str | None:
    found = [m for m in mechanics if re.search(r"\b" + re.escape(plain(m)) + r"\b", text)]
    return found[0] if len(found) == 1 else None


def _single_best(totals: dict) -> str | None:
    if not totals:
        return None
    best = max(totals.values())
    winners = [k for k, v in totals.items() if v == best]
    return winners[0] if len(winners) == 1 else None  # the rules name no tie-break


def answer(question: Question, context: BusinessContext, text: str):
    """(op, value, answer type) for one repair question, or None to abstain."""
    from src.agent.plan import _literal_windows
    from src.semantics.answerability import preflight

    loaded = tickets(context)
    if loaded is None:
        return None
    all_tickets, mechanics = loaded
    expressions = _literal_windows(question)
    if len(expressions) != 1:
        return None
    check = preflight(context, "revenue", expressions[0], as_of_date=question.as_of_date)
    if check["decision"] == "unanswerable":
        return "repair", check["reason"], "unanswerable"
    window = check.get("effective_window")
    if check["decision"] != "eligible_to_execute" or not window:
        return None
    rows = [
        t
        for t in all_tickets
        if window["start"] <= t.day <= window["end"] and t.status in {"completed", "shipping"}
    ]
    if not rows:
        return None
    paid = [t for t in rows if t.amount > 0]
    named = _named_mechanic(text, mechanics)
    if any(t.mechanic is None for t in rows) and (named or re.search(r"\btho\b", text)):
        return None  # an unresolved nickname could belong to any mechanic
    commission = re.search(r"\b(?:hoa hong|hue hong)\b", text)
    if re.search(r"\b(?:bang|tung tho|moi tho|cac tho|xep)\b", text):
        if not (commission and re.search(r"\bbang\b", text)) or named:
            return None
        totals = defaultdict(Decimal)
        for t in rows:
            totals[t.mechanic] += t.amount
        # Every listed mechanic worked in the period, so no row is left to guess.
        if set(totals) != set(mechanics) or any(mechanics[m] is None for m in totals):
            return None
        pay = sorted(((m, totals[m] * mechanics[m] / 100) for m in totals), key=lambda x: -x[1])
        table = {"columns": ["tho", "hoa_hong"], "rows": [[m, _money(v)] for m, v in pay]}
        return "repair_commission_table", table, "table"

    if commission:
        totals = defaultdict(Decimal)
        for t in rows:
            totals[t.mechanic] += t.amount
        pay = {m: totals[m] * mechanics[m] / 100 for m in totals if mechanics.get(m) is not None}
        if len(pay) != len(totals):
            return None
        if named:
            return "repair_commission", _money(pay.get(named, Decimal(0))), "number"
        if re.search(r"\b(?:nao|mo|ai)\b", text) and re.search(
            r"\b(?:cao nhat|nhieu nhat)\b", text
        ):
            best = _single_best(pay)
            return ("repair_commission", best, "string") if best else None
        return None
    if re.search(r"\bcham chi\b", text):
        days = defaultdict(set)
        for t in rows:
            days[t.mechanic].add(t.day)
        best = _single_best({m: len(d) for m, d in days.items()})
        return ("repair_workdays", best, "string") if best else None
    if re.search(r"\btho\b", text) or named:
        if named and re.search(r"\b(?:tien|doanh thu|dthu)\b", text):
            return (
                "repair_mechanic_revenue",
                _money(sum((t.amount for t in rows if t.mechanic == named), Decimal(0))),
                "number",
            )
        if named and re.search(r"\b(?:luot|phieu)\b", text):
            return "repair_mechanic_visits", sum(1 for t in rows if t.mechanic == named), "number"
        if re.search(r"\b(?:nao|mo|ai)\b", text) and re.search(r"\bnhat\b", text):
            if re.search(r"\b(?:luot|phieu)\b", text):
                best = _single_best(Counter(t.mechanic for t in rows))
                return ("repair_top_visits", best, "string") if best else None
            if re.search(r"\b(?:tien|doanh thu|dthu)\b", text):
                totals = defaultdict(Decimal)
                for t in rows:
                    totals[t.mechanic] += t.amount
                best = _single_best(totals)
                return ("repair_top_revenue", best, "string") if best else None
        return None
    models = {w for t in all_tickets for w in t.model.split() if len(w) >= 2 and not w.isdigit()}
    if re.search(
        r"\b(?:dong xe|xe [a-z]+ khac nhau|lam gi|thay|sua dien|va|nhot|ruot|bdtq)\b", text
    ) or any(re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", text) for w in models):
        return None  # vehicle models and job wording need the shop's own abbreviations
    if re.search(r"\bbao hanh\b", text):
        if re.search(r"%|\b(?:ti le|ty le|chiem|phan tram)\b", text):
            if not paid:
                return None
            share = Decimal(sum(t.warranty for t in rows)) * 100 / len(paid)
            return "repair_warranty_rate", _percent(share), "percent"
        if re.search(r"\b(?:bao nhieu|may|bn|dem|so)\b", text):
            return "repair_warranty_count", sum(t.warranty for t in rows), "number"
        return None
    weekday = re.search(
        r"\b(?:moi|cac ngay|ngay) (thu (?:hai|ba|tu|nam|sau|bay|[2-7])|chu nhat)\b", text
    )
    if (
        weekday
        and re.search(r"\b(?:trung binh|binh quan|tb)\b", text)
        and re.search(r"\b(?:luot|phieu)\b", text)
        and "weekday_average" in context.rules.rules
    ):
        names = {"hai": 0, "ba": 1, "tu": 2, "nam": 3, "sau": 4, "bay": 5}
        word = weekday[1].split()[-1]
        index = 6 if weekday[1] == "chu nhat" else int(word) - 2 if word.isdigit() else names[word]
        start, end = date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
        count = sum(
            1 for k in range((end - start).days + 1) if (start + timedelta(k)).weekday() == index
        )
        hits = sum(1 for t in rows if date.fromisoformat(t.day).weekday() == index)
        return (
            ("repair_weekday_average", _percent(Decimal(hits) / count), "number") if count else None
        )
    if re.search(r"\b(?:trung binh|binh quan|tb)\b", text) and re.search(r"\bphieu\b", text):
        if re.search(r"\bthu \d|\bmoi thu\b|\bngay thu\b", text) or not paid:
            return None
        return "repair_paid_average", _money(sum(t.amount for t in paid) / len(paid)), "number"
    if re.search(r"\bkhac nhau\b|\bchiec xe\b", text) and not re.search(r"\blan\b", text):
        if any(t.plate is None for t in rows):
            return None
        return "repair_vehicles", len({t.plate for t in rows}), "number"
    if re.search(r"\b(?:khach quen|tu 2 lan|2 lan tro len|>= ?2)\b", text):
        if any(t.plate is None for t in paid):
            return None
        visits = Counter(t.plate for t in paid)
        return "repair_repeat_vehicles", sum(1 for n in visits.values() if n >= 2), "number"
    if re.search(r"\b(?:phieu|luot)\b", text) and re.search(
        r"\b(?:bao nhieu|may|bn|dem|so)\b", text
    ):
        if re.search(r"\b(?:trung binh|binh quan|tb)\b", text):
            return None
        return "repair_tickets", len(rows), "number"
    return None
