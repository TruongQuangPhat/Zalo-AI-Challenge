"""Round 7: typing slips that still spell a syllable, repaired only in measured context."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # "giảm" typed with a "d" onset, read through the measured phrase "tăng hay giảm".
        ("doanh thu tháng 5/2025 tăng hay dảm mấy %", "doanh thu tháng 5/2025 tăng hay giảm mấy %"),
        ("có bao nhiêu đơng hàng hôm qua", "có bao nhiêu đơn hàng hôm qua"),  # final ng/n
        ("doanh thu tuần chước là bao nhiêu", "doanh thu tuần trước là bao nhiêu"),
        # A real word the vocabulary lacks, with no measured phrase around it, stays.
        ("chênh lệch tuyệt đối bao nhiêu", "chênh lệch tuyệt đối bao nhiêu"),
    ],
)
def test_syllable_shaped_slips_are_repaired_only_by_a_measured_phrase(text, expected):
    from src.semantics.typos import repaired
    from src.semantics.vocabulary import is_known

    assert repaired(text, is_known) == expected


def test_context_repair_never_touches_names_or_ambiguous_readings():
    from src.semantics.typos import repair_in_context, repaired
    from src.semantics.vocabulary import correction_words, is_known

    # A shop name word, or a capitalised word, is never repaired.
    assert repaired("doanh thu tăng hay dảm", is_known, {"dam"}) == "doanh thu tăng hay dảm"
    assert repaired("doanh thu tăng hay Dảm", is_known) == "doanh thu tăng hay Dảm"
    # Without a neighbour that forms a measured phrase, nothing is chosen.
    assert repair_in_context("dảm", None, None, correction_words()) is None


def test_typo_variant_leaves_data_names_and_known_words_alone():
    from scripts.perturb_train import typo_question
    from src.semantics.vocabulary import is_known

    text = "Tháng 7/2025 mặt hàng Xịt mũi chai 10ml bán được bao nhiêu chai?"
    general = {"mặt", "hàng", "chai", "bán", "được", "bao", "nhiêu", "mũi"}
    for index in range(40):
        new = typo_question(text, index, general, {"mũi chai"})
        assert "mũi chai 10ml" in new  # "chai" next to "mũi" is part of a data name
        changed = [b for a, b in zip(text.split(), new.split(), strict=True) if a != b]
        assert 1 <= len(changed) <= 2
        assert not any(is_known(w.strip("?").casefold()) for w in changed)


@pytest.mark.parametrize(
    ("text", "metric"),
    [
        ("Tồn kho Natri clorid 0,9% chai 500ml cuối ngày hôm nay còn bao nhiêu chai?", "stock"),
        # Only stock and lot questions read a percent glued to a number as part of a name.
        ("Tháng này bán Natri clorid 0,9% chai 500ml được bao nhiêu chai?", None),
        ("Hôm nay tồn kho chiếm bao nhiêu % tổng nhập?", None),
    ],
)
def test_percent_inside_an_item_name_is_not_a_percent_question(text, metric):
    from src.agent.entities import business_spec
    from src.data.split import Question

    spec = business_spec(Question("q", "b", text, "2026-01-31"))
    assert (spec and spec["metric"]) == ("stock_balance" if metric else None)


def test_unaccented_item_names_bind_only_when_typed_without_marks_and_distinctive(monkeypatch):
    from types import SimpleNamespace

    from src.agent import entities

    rows = [
        ("k1", "P1", "Bánh croissant", "cái", ("Bánh croissant",)),
        ("k2", "P2", "Bàn chải", "cái", ("Bàn chải",)),
        ("k3", "P3", "Sữa TH true MILK 180ml", "hộp", ("Sữa TH true MILK 180ml",)),
    ]
    monkeypatch.setattr(entities, "_candidate_rows", lambda prepared, kind: iter(rows))
    context = SimpleNamespace(prepared=SimpleNamespace(summary={"extensions": {"counts": {1: 1}}}))

    def keys(text):
        return [c["entity_key"] for c in entities.entity_candidates(context, text)]

    assert keys("ton kho banh croissant cuoi ngay hom nay") == ["k1"]  # a foreign word
    assert keys("ton kho sua th true milk 180ml hom nay") == ["k3"]  # a digit
    assert keys("hom nay ban chai nuoc suoi duoc may chai") == []  # everyday words
    assert keys("tồn kho bánh croissant hôm nay") == ["k1"]  # accented: the usual match
    assert keys("tồn kho banh croissánt hôm nay") == []  # marks that differ: not the item


def _lot_context(tmp_path, monkeypatch, sales, snapshot=()):
    import sqlite3
    from types import SimpleNamespace

    from src.agent import lot_book

    path = tmp_path / "facts.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE entities (entity_key, kind, source_code);"
            "CREATE TABLE orders (raw_order_id, status);"
            "CREATE TABLE stock_balances (product_key, balance);"
            "INSERT INTO entities VALUES ('k1','product','SP1'),('k2','product','SP2');"
            "INSERT INTO orders VALUES ('D1','completed'),('D2','cancelled'),('D3','returned');"
        )
        conn.executemany("INSERT INTO stock_balances VALUES (?,?)", snapshot)
    receipts = [
        {"lo": ("L1", None), "ma": ("SP1", None), "hsd": ("2027-01-31", None), "sl": (10, None)},
        {"lo": ("L2", None), "ma": ("SP1", None), "hsd": ("2026-06-30", None), "sl": (5, None)},
        {"lo": ("L3", None), "ma": ("SP2", None), "hsd": ("2026-05-01", None), "sl": (7, None)},
    ]
    books = {
        id(lot_book.RECEIPT_LOTS): [
            ({"lot": "lo", "code": "ma", "expiry": "hsd", "received": "sl"}, receipts, None)
        ],
        id(lot_book.LOT_SALES): [
            ({"order": "don", "lot": "lo", "sold": "sl", "returned": "tra"}, sales, None)
        ],
    }
    monkeypatch.setattr(lot_book, "_books", lambda context, roles: books[id(roles)])
    mode = "snapshot" if snapshot else "event_balance"
    prepared = SimpleNamespace(
        connect=lambda: sqlite3.connect(path), summary={"extensions": {"stock_mode": mode}}
    )
    return SimpleNamespace(prepared=prepared)


def _sale(order, lot, sold, returned=None):
    row = {"don": (order, None), "lo": (lot, None), "sl": (sold, None)}
    return row | ({"tra": (returned, None)} if returned is not None else {})


def test_lot_balance_is_received_minus_kept_sales_plus_returns(tmp_path, monkeypatch):
    from decimal import Decimal

    from src.agent.lot_book import lot_balances

    sales = [_sale("D1", "L1", 4), _sale("D2", "L1", 3), _sale("D3", "L2", 2, 1)]
    lots = {
        lot: balance
        for lot, _, _, balance in lot_balances(_lot_context(tmp_path, monkeypatch, sales))
    }
    # The cancelled order D2 takes nothing; D3 brought one back to L2.
    assert lots == {"L1": Decimal(6), "L2": Decimal(4), "L3": Decimal(7)}


@pytest.mark.parametrize(
    ("sales", "snapshot"),
    [
        ([_sale("D9", "L1", 1)], ()),  # an order the facts do not know
        ([_sale("D1", "L9", 1)], ()),  # a lot no receipt line holds
        ([_sale("D1", "L1", 11)], ()),  # more sold than received
        ([_sale("D1", "L1", 1)], [("k1", "13")]),  # the shop's stock list says otherwise
    ],
)
def test_lot_books_that_do_not_join_or_agree_abstain(tmp_path, monkeypatch, sales, snapshot):
    from src.agent.lot_book import lot_balances

    assert lot_balances(_lot_context(tmp_path, monkeypatch, sales, snapshot)) is None


def test_a_marked_typo_of_a_name_word_is_repaired_or_left_unknown():
    from src.semantics.typos import repaired
    from src.semantics.vocabulary import is_known, unknown_words

    names, accented = {"than", "khoa"}, {"thân", "khoa"}
    # "thán" is no name word as typed: repaired by its measured phrase "tháng 3".
    text = "xem giúp thán 3 năm 2026, 3 khách nào chi tiêu nhiều nhất"
    assert repaired(text, is_known, names, accented).startswith("xem giúp tháng 3")
    # A name word typed as the name writes it stays.
    assert repaired("khách thân", is_known, names, accented) == "khách thân"
    # Unknown-word check: a marked word that is only a name's bare spelling is unknown,
    # one whose bare spelling is also measured wording is understood.
    assert unknown_words("khách khóa", names, accented) == ["khóa"]
    assert unknown_words("khách khóa", names) == []
    assert unknown_words("khách thán", names, accented) == []
