"""Offline question metadata proposals; never reads answers, EDA QA, or public data."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

from scripts.audit_dataset import digest, inventory, outside_inputs, save_json, validate_manifest
from src.contracts import ANSWER_TYPES
from src.data.split import Question, load_split
from src.jsonio import ContractError, read_jsonl

VERSION = "question-taxonomy-v1"
METHOD = "lexical-proposal-v1"
INTENTS = {"aggregate", "ranking", "comparison", "lookup", "listing", "unknown"}
TEMPORAL = {
    "explicit_period",
    "relative_period",
    "multiple_periods",
    "snapshot",
    "lunar",
    "prior_history",
    "unknown",
}
# Broad metric families for error slicing, not executable metric IDs or gold SQL.
METRICS = {
    "revenue": (
        r"doanh thu|\bdthu\b|\brevenue\b|tien ban|"
        r"ban.*(?:bao nhieu|bn|bnhiu|nhieu|nhiu).*tien|thu ve.*tien",
        ["sales"],
    ),
    "order_count": (r"(?:bao nhieu|may|so|nhieu|tong).*\b(?:don|hoa don|bill|order)\b", ["orders"]),
    "average_order_value": (
        r"(?:trung binh|binh quan|gia tri).*\b(?:mot don|moi don|tren.*don)\b",
        ["sales", "orders"],
    ),
    "customer_spend": (
        r"khach.*(?:chi tieu|chi nhieu tien|mua nhieu tien)|ai.*chi tieu",
        ["sales", "customers"],
    ),
    "quantity_sold": (
        r"ban chay|luong ban|so luong.*ban|ban.*(?:bao nhieu|may|nhieu|so).*"
        r"\b(?:ly|coc|chai|thung|vien|hop|tan|kg|phan|suat|cay)\b|"
        r"so (?:ly|coc|chai|thung|vien|hop|tan|kg|phan|suat|cay).*ban",
        ["sales", "units"],
    ),
    "customer_count": (
        r"(?:bao nhieu|so|nhieu).*\bkhach\b|khach.*(?:mua lai|quay lai)",
        ["orders", "customers"],
    ),
    "product_count": (
        r"(?:bao nhieu|may|so).*\b(?:mat hang|loai hang|loai thuoc|mon)\b",
        ["products", "sales"],
    ),
    "cancellation": (r"\bhuy\b", ["orders"]),
    "returns": (r"tra hang|hang tra|don tra", ["orders", "returns"]),
    "gross_profit": (r"lai gop|loi nhuan gop", ["sales", "line_costs", "units"]),
    "net_profit": (r"lai rong|loi nhuan rong", ["sales", "line_costs", "expenses"]),
    "cost_of_goods": (r"gia von", ["sales", "line_costs", "units"]),
    "receivable_balance": (
        r"con no|dang no|tong no|tien no|du no|no qua|no lau|con thieu|con phai thu",
        ["orders", "payments"],
    ),
    "debt_collection": (
        r"thu no|tra no|khach.*da tra|thu duoc.*no|thu hoi|tra bot no",
        ["orders", "payments"],
    ),
    "new_debt": (r"no them|ghi no|mua chiu|ban chiu|phat sinh no", ["orders", "payments"]),
    "inventory_quantity": (r"\bton\b|con hang|con bao nhieu|con may", ["inventory"]),
    "inventory_value": (
        r"gia tri.*\b(?:ton|kho)\b|von.*\bton\b|tien.*\bton\b",
        ["inventory", "catalog_costs", "units"],
    ),
    "stockout": (r"het hang|ton.*(?:bang|=)\s*0", ["inventory", "products"]),
    "dead_stock": (
        r"(?:hang|thuoc|mon) e\b|khong ban duoc|khong ban.*trong|khong co.*ban",
        ["sales", "products"],
    ),
    "purchase_value": (r"tien nhap|gia tri nhap|nhap.*tien|nhap nhieu nhat", ["purchases"]),
    "purchase_quantity": (
        r"luong nhap|nhap.*(?:bao nhieu|bn|tan|kg|thung)|nhap tru ban|chenh.*nhap",
        ["purchases", "units"],
    ),
    "supplier": (r"nha cung cap|\bncc\b", ["purchases", "suppliers"]),
    "sale_price": (
        r"gia ban|ban gia|don gia|gia.*ngay|tang gia|ban.*gia bao nhieu",
        ["sales", "price_history", "units"],
    ),
    "purchase_price": (r"gia nhap", ["purchases", "units"]),
    "expiry": (r"het han|han dung|han su dung|gan het han", ["lots", "lot_allocations"]),
    "lot_quantity": (r"\blo\b", ["lots", "lot_allocations"]),
    "prescription": (r"ke don|hoat chat", ["products", "drug_attributes", "sales"]),
    "delivery_count": (
        r"dot giao|lan giao|chuyen giao|da giao|chua giao",
        ["deliveries", "orders"],
    ),
    "entity_attribute": (
        r"dia chi|gioi tinh|so dien thoai|ten day du|ngay sinh",
        ["entity_attributes"],
    ),
    "expense": (r"chi phi|khoan chi|tien chi", ["expenses"]),
    "unknown": (r"(?!)", []),
}
FACTS = {fact for _, facts in METRICS.values() for fact in facts} | {"unknown"}
FIELDS = {
    "id",
    "business_id",
    "schema_version",
    "taxonomy_version",
    "method",
    "intents",
    "metrics",
    "temporal_features",
    "required_facts",
    "source_verification",
    "expected_output_type",
    "dimension_status",
    "review_status",
    "notes",
}


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.casefold().replace("đ", "d"))
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def propose(question: Question) -> dict:
    text = normalize(question.question)
    for old, new in {
        "hqua": "hom qua",
        "nhaats": "nhat",
        "nhiu": "nhieu",
        "bnhiu": "bao nhieu",
    }.items():
        text = re.sub(r"\b" + old + r"\b", new, text)
    metrics = [name for name, (pattern, _) in METRICS.items() if re.search(pattern, text)]
    if "purchase_price" in metrics:
        metrics = [m for m in metrics if m != "purchase_quantity"]
    if "sale_price" in metrics and not re.search(r"luong ban|so .*ban ra", text):
        metrics = [m for m in metrics if m not in {"quantity_sold", "revenue"}]
    if "average_order_value" in metrics:
        metrics = [m for m in metrics if m not in {"revenue", "order_count"}]
    intents = []
    ranking_text = re.sub(r"gan (?:nhat|day)", "", text)
    if re.search(r"nhat|top\s*\d|ban chay|xep hang", ranking_text):
        intents.append("ranking")
    if re.search(
        r"so voi|so vs|so sanh|tang|giam|chenh|hon.*bao nhieu",
        re.sub(r"giam dau|giam gia", "", text),
    ):
        intents.append("comparison")
    if re.search(r"liet ke|danh sach|nhung .*nao|top\s*\d", text):
        intents.append("listing")
    if re.search(r"bao nhieu|bnhiu|\bbn\b|tong|ti le|ty le|ti trong|ty trong|%|trung binh", text):
        intents.append("aggregate")
    if re.search(r"\bnao\b|la gi|dia chi|gioi tinh|gia ban", text) and "ranking" not in intents:
        intents.append("lookup")
    if not intents and metrics:
        intents.append("aggregate")
    temporal = []
    if re.search(r"\d{1,2}[/.-]\d{1,2}|\b20\d{2}\b|(?:thang|quy)\s*\d", text):
        temporal.append("explicit_period")
    if re.search(
        r"hom nay|hnay|bua nay|hom qua|tuan|gan (?:nhat|day)|thang nay|thang truoc|"
        r"thang roi|nam nay|dau nam|nam ngoai|\d+ ngay (?:toi|qua)",
        text,
    ):
        temporal.append("relative_period")
    if re.search(r"so voi|so vs|so sanh|cung ky|tang gia|tang lien tuc|thang lien tiep", text):
        temporal.append("multiple_periods")
    if set(metrics) & {
        "receivable_balance",
        "inventory_quantity",
        "inventory_value",
        "stockout",
        "expiry",
        "lot_quantity",
    }:
        temporal.append("snapshot")
    if re.search(r"\btet\b|am lich|thang chap|thang gieng", text):
        temporal.append("lunar")
    if set(metrics) & {"dead_stock", "debt_collection", "receivable_balance"} or "tang gia" in text:
        temporal.append("prior_history")
    output = "unknown"
    if re.search(r"phan tram|ti le|ty le|ti trong|ty trong|%", text):
        output = "percent"
    if "listing" in intents:
        output = "unknown"  # A list vs a table/multiple measures needs a semantic review.
    facts = sorted({fact for metric in metrics for fact in METRICS[metric][1]})
    values = {
        "intents": intents or ["unknown"],
        "metrics": metrics or ["unknown"],
        "temporal_features": temporal or ["unknown"],
        "required_facts": facts or ["unknown"],
        "expected_output_type": output,
    }
    return {
        "id": question.id,
        "business_id": question.business_id,
        "schema_version": "question-annotation-v1",
        "taxonomy_version": VERSION,
        "method": METHOD,
        **values,
        "source_verification": "not_checked",
        "dimension_status": {
            key: "unknown" if value == ["unknown"] or value == "unknown" else "proposed"
            for key, value in values.items()
        },
        "review_status": "proposed",
        "notes": ["Lexical proposal only; dependencies are needs, not verified availability."],
    }


def select_pilot(questions: list[Question], manifest: dict, count: int = 100) -> list[Question]:
    """Round-robin businesses and semantic slices; no gold or held-out feedback."""
    devs = [set(p["partitions"]["dev"]["businesses"]) for p in manifest["protocols"].values()]
    eligible = set.intersection(*devs)
    buckets: dict[str, dict[str, list[Question]]] = {}
    for question in questions:
        if question.business_id not in eligible:
            continue
        text = normalize(question.question)
        shape = "other"
        for label, pattern in [
            ("comparison", r"so voi|so sanh|chenh|tang|giam"),
            ("ranking", r"nhat|top\s*\d"),
            ("listing", r"liet ke|danh sach"),
            ("relative", r"hom nay|tuan nay|thang nay"),
        ]:
            if re.search(pattern, text):
                shape = label
                break
        buckets.setdefault(question.business_id, {}).setdefault(shape, []).append(question)
    queues = {}
    for business, shapes in sorted(buckets.items()):
        for values in shapes.values():
            values.sort(
                key=lambda q: hashlib.sha256((str(manifest["seed"]) + q.id).encode()).hexdigest()
            )
        queue = []
        while any(shapes.values()):
            for shape in sorted(shapes):
                if shapes[shape]:
                    queue.append(shapes[shape].pop(0))
        queues[business] = queue
    selected = []
    keys = sorted(queues)
    while len(selected) < count:
        progress = False
        for key in keys:
            if queues[key]:
                selected.append(queues[key].pop(0))
                progress = True
                if len(selected) == count:
                    break
        if not progress:
            raise ContractError("not enough dev questions for pilot")
    return selected


def validate_annotations(records: list[dict], questions: list[Question]) -> dict:
    expected = {q.id: q for q in questions}
    seen = set()
    statuses: Counter = Counter()
    unresolved = {
        key: 0
        for key in [
            "intents",
            "metrics",
            "temporal_features",
            "required_facts",
            "expected_output_type",
        ]
    }
    for record in records:
        if set(record) != FIELDS:
            raise ContractError("annotation fields differ; labels/answers/plans are forbidden")
        qid = record["id"]
        if qid not in expected or qid in seen:
            raise ContractError("unknown/duplicate annotation ID")
        seen.add(qid)
        if record["business_id"] != expected[qid].business_id:
            raise ContractError("annotation business mismatch")
        if (
            record["taxonomy_version"] != VERSION
            or record["schema_version"] != "question-annotation-v1"
        ):
            raise ContractError("annotation version mismatch")
        for key, allowed in [
            ("intents", INTENTS),
            ("metrics", set(METRICS)),
            ("temporal_features", TEMPORAL),
            ("required_facts", FACTS),
        ]:
            values = record[key]
            if (
                not isinstance(values, list)
                or not values
                or not all(isinstance(v, str) for v in values)
            ):
                raise ContractError(f"invalid annotation array: {key}")
            if len(set(values)) != len(values) or not set(values) <= allowed:
                raise ContractError(f"invalid annotation enum: {key}")
            if "unknown" in values and len(values) != 1:
                raise ContractError("unknown cannot coexist with known labels")
        if record["expected_output_type"] not in ANSWER_TYPES | {"unknown"}:
            raise ContractError("invalid expected output type")
        if record["source_verification"] != "not_checked":
            raise ContractError("unverified dependencies cannot assert source availability")
        if record["review_status"] not in {"proposed", "reviewed"}:
            raise ContractError("invalid review status")
        if record["method"] not in {METHOD, "manual-question-review-v1"}:
            raise ContractError("invalid annotation method")
        if (record["review_status"] == "reviewed") != (
            record["method"] == "manual-question-review-v1"
        ):
            raise ContractError("manual review provenance mismatch")
        if (
            not isinstance(record["notes"], list)
            or not record["notes"]
            or not all(isinstance(n, str) and n.strip() for n in record["notes"])
        ):
            raise ContractError("annotation needs reasons/provenance")
        if set(record["dimension_status"]) != set(unresolved):
            raise ContractError("dimension statuses incomplete")
        for key, status in record["dimension_status"].items():
            unknown = record[key] == ["unknown"] or record[key] == "unknown"
            if status not in {"proposed", "reviewed", "unknown", "ambiguous"}:
                raise ContractError("invalid dimension status")
            if unknown != (status in {"unknown", "ambiguous"}):
                raise ContractError("unresolved dimension/value mismatch")
            if status == "reviewed" and record["review_status"] != "reviewed":
                raise ContractError("dimension claims missing manual review")
            unresolved[key] += status in {"unknown", "ambiguous"}
        statuses[record["review_status"]] += 1
    if seen != set(expected):
        raise ContractError("missing annotations")
    return {"records": len(records), "review_status": dict(statuses), "unresolved": unresolved}


def annotate(
    data_dir: Path, manifest_path: Path, output: Path, reviews: Path | None = None
) -> dict:
    if data_dir.resolve().name != "train":
        raise ContractError("annotation is train-only")
    outside_inputs(output, data_dir, data_dir.parent / "explore")
    split = load_split(data_dir)
    manifest = json.loads(manifest_path.read_text())
    before = inventory(data_dir)
    validate_manifest(manifest, split, before)
    pilot = select_pilot(list(split.questions), manifest)
    review_map = {}
    if reviews:
        outside_inputs(reviews, data_dir, data_dir.parent / "explore")
        if reviews.resolve() == (output / "annotations.jsonl").resolve():
            raise ContractError("reviews and output must differ")
        records = [record for _, record in read_jsonl(reviews)]
        validate_annotations(records, pilot)
        if any(r["review_status"] != "reviewed" for r in records):
            raise ContractError("pilot reviews incomplete")
        review_map = {record["id"]: record for record in records}
    output.mkdir(parents=True, exist_ok=False)
    records = [review_map.get(q.id, propose(q)) for q in split.questions]
    result = validate_annotations(records, list(split.questions))
    if inventory(data_dir) != before:
        raise ContractError("source inputs changed during annotation")
    result.update(
        {
            "taxonomy_version": VERSION,
            "pilot_questions": len(pilot),
            "source_fingerprint": manifest["source_fingerprint"],
            "manifest_sha256": digest(manifest_path),
            "annotator_sha256": digest(Path(__file__)),
            "preparation_sha256": digest(Path(__file__).with_name("audit_dataset.py")),
            "review_sha256": digest(reviews) if reviews else None,
            "gold_types": "not extracted; no answer file parsed",
            "inputs_unchanged": True,
        }
    )
    for name, values in [
        ("annotations.jsonl", records),
        ("pilot.jsonl", [{"question": q.question, "annotation": propose(q)} for q in pilot]),
    ]:
        (output / name).write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in values)
        )
    save_json(output / "verification.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--reviews", type=Path)
    args = parser.parse_args()
    try:
        result = annotate(args.data_dir, args.manifest, args.output_dir, args.reviews)
    except (ContractError, OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Question annotation: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
