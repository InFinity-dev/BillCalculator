"""DB 정합성 검증.

두 층위를 검사한다.

1. SQLite 엔진 수준 — ``PRAGMA foreign_key_check`` / ``PRAGMA integrity_check``
2. 애플리케이션 도메인 수준 — 스키마 제약만으로는 잡히지 않는 규칙

실행::

    flask check-db
"""

from decimal import Decimal

from sqlalchemy import text

from extensions import db
from models import (
    CommonBill,
    CommonBillDetail,
    ElectricBill,
    ElectricBillDetail,
    ElectricBillMonth,
    FinalInvoice,
    FinalInvoiceCharge,
    InvoiceCombinationItem,
    Payment,
    WaterBillDetail,
)

SNAPSHOT_KEYS = (
    "unit_name",
    "electric_welfare",
    "electric_voucher",
    "has_tv",
    "water_welfare",
    "residents_count",
    "is_vacant",
)


class Check:
    """검사 하나의 결과."""

    def __init__(self, name, ok, detail=None, rows=None):
        self.name = name
        self.ok = ok
        self.detail = detail
        self.rows = rows or []

    def as_dict(self):
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "rows": self.rows[:50],
            "row_count": len(self.rows),
        }


# ---------------------------------------------------------------------------
# 엔진 수준
# ---------------------------------------------------------------------------
def check_foreign_keys():
    rows = db.session.execute(text("PRAGMA foreign_key_check")).fetchall()
    return Check(
        "foreign_key_check",
        not rows,
        "참조 무결성 위반 {}건".format(len(rows)) if rows else "위반 없음",
        [dict(zip(("table", "rowid", "parent", "fkid"), r)) for r in rows],
    )


def check_integrity():
    result = db.session.execute(text("PRAGMA integrity_check")).scalar()
    return Check("integrity_check", result == "ok", result)


def check_foreign_keys_enabled():
    value = db.session.execute(text("PRAGMA foreign_keys")).scalar()
    return Check(
        "foreign_keys_enabled",
        bool(value),
        "PRAGMA foreign_keys = {}".format(value),
    )


# ---------------------------------------------------------------------------
# 도메인 수준
# ---------------------------------------------------------------------------
def check_billing_month_normalized():
    """모든 billing_month 가 그 달 1일인지."""
    bad = []
    for table in (
        "electric_bills",
        "electric_bill_months",
        "water_bills",
        "common_bills",
        "invoice_combination_items",
    ):
        rows = db.session.execute(
            text(
                "SELECT id, billing_month FROM {} "
                "WHERE substr(billing_month, 9, 2) <> '01'".format(table)
            )
        ).fetchall()
        bad.extend({"table": table, "id": r[0], "billing_month": r[1]} for r in rows)
    return Check(
        "billing_month_normalized",
        not bad,
        "정규화되지 않은 정산월 {}건".format(len(bad)) if bad else "전부 1일",
        bad,
    )


def check_polymorphic_items():
    """invoice_combination_items 의 item_type 과 FK 가 일치하는지."""
    bad = []
    for item in InvoiceCombinationItem.query.all():
        fks = [item.electric_bill_id, item.water_bill_id, item.common_bill_id]
        filled = [f for f in fks if f is not None]
        expected = {"ELECTRIC": 0, "WATER": 1, "COMMON": 2}.get(item.item_type)
        if len(filled) != 1 or expected is None or fks[expected] is None:
            bad.append(
                {
                    "id": item.id,
                    "item_type": item.item_type,
                    "electric_bill_id": item.electric_bill_id,
                    "water_bill_id": item.water_bill_id,
                    "common_bill_id": item.common_bill_id,
                }
            )
    return Check(
        "invoice_item_polymorphic_reference",
        not bad,
        "다형 참조 위반 {}건".format(len(bad)) if bad else "위반 없음",
        bad,
    )


def check_snapshots():
    """unit_snapshot 이 필수 키를 모두 갖고 있는지."""
    bad = []
    for model, label in (
        (ElectricBillDetail, "electric_bill_details"),
        (WaterBillDetail, "water_bill_details"),
        (CommonBillDetail, "common_bill_details"),
    ):
        for row in model.query.all():
            snapshot = row.unit_snapshot
            if snapshot is None:
                bad.append({"table": label, "id": row.id, "issue": "snapshot_missing"})
                continue
            if not isinstance(snapshot, dict):
                bad.append({"table": label, "id": row.id, "issue": "snapshot_not_object"})
                continue
            missing = [k for k in SNAPSHOT_KEYS if k not in snapshot]
            if missing:
                bad.append(
                    {
                        "table": label,
                        "id": row.id,
                        "issue": "missing_keys",
                        "keys": missing,
                    }
                )
    return Check(
        "unit_snapshot_complete",
        not bad,
        "스냅샷 문제 {}건".format(len(bad)) if bad else "전부 정상",
        bad,
    )


def check_charged_amount_rounding():
    """charged_amount 가 10원 단위인지 (10원 올림 정책)."""
    bad = []
    for model, label in (
        (ElectricBillDetail, "electric_bill_details"),
        (WaterBillDetail, "water_bill_details"),
        (CommonBillDetail, "common_bill_details"),
    ):
        for row in model.query.all():
            if row.charged_amount % 10 != 0:
                bad.append(
                    {"table": label, "id": row.id, "charged_amount": row.charged_amount}
                )
    return Check(
        "charged_amount_multiple_of_10",
        not bad,
        "10원 단위가 아닌 청구액 {}건".format(len(bad)) if bad else "전부 10원 단위",
        bad,
    )


def check_final_invoice_totals():
    """final_invoices.total_amount 가 구성요소 합과 일치하는지.

    total = electric + water + common + Σ(모든 기타 항목, 이월 포함)
    """
    bad = []
    for invoice in FinalInvoice.query.all():
        expected = (
            invoice.electric_amount
            + invoice.water_amount
            + invoice.common_amount
            + sum(c.amount for c in invoice.charges)
        )
        if invoice.total_amount != expected:
            bad.append(
                {
                    "final_invoice_id": invoice.id,
                    "stored_total": invoice.total_amount,
                    "computed_total": expected,
                    "difference": invoice.total_amount - expected,
                }
            )
    return Check(
        "final_invoice_total_consistent",
        not bad,
        "청구서 합계 불일치 {}건".format(len(bad)) if bad else "전부 일치",
        bad,
    )


def check_duplicate_invoice_sources():
    """같은 고지 항목이 여러 번 청구된 과거 기록을 찾는다."""
    bad = []
    for item_type, column in (
        ("ELECTRIC", "electric_bill_id"),
        ("WATER", "water_bill_id"),
        ("COMMON", "common_bill_id"),
    ):
        rows = db.session.execute(text(
            "SELECT {0}, COUNT(*) FROM invoice_combination_items "
            "WHERE {0} IS NOT NULL GROUP BY {0} HAVING COUNT(*) > 1".format(column)
        )).fetchall()
        bad.extend(
            {"type": item_type, "bill_id": bill_id, "use_count": count}
            for bill_id, count in rows
        )
    return Check(
        "invoice_source_used_once",
        not bad,
        "중복 청구된 고지 항목 {}건".format(len(bad)) if bad else "중복 없음",
        bad,
    )


def check_payment_invoice_pairs():
    """입금이 실제 해당 세대의 정산서에 연결되어 있는지 확인한다."""
    rows = db.session.execute(text(
        "SELECT p.id, p.combination_id, p.unit_id FROM payments p "
        "LEFT JOIN final_invoices f ON f.combination_id = p.combination_id "
        "AND f.unit_id = p.unit_id WHERE f.id IS NULL"
    )).fetchall()
    bad = [
        {"payment_id": payment_id, "combination_id": combination_id, "unit_id": unit_id}
        for payment_id, combination_id, unit_id in rows
    ]
    return Check(
        "payments_have_matching_invoice",
        not bad,
        "청구서와 연결되지 않은 입금 {}건".format(len(bad)) if bad else "전부 연결됨",
        bad,
    )


def check_negative_electric_usage():
    bad = [
        {"detail_id": row.id, "electric_bill_id": row.electric_bill_id,
         "unit_id": row.unit_id, "usage_amount": str(row.usage_amount)}
        for row in ElectricBillDetail.query.all() if row.usage_amount < 0
    ]
    return Check(
        "electric_usage_non_negative",
        not bad,
        "음수 전기 사용량 {}건".format(len(bad)) if bad else "전부 0 이상",
        bad,
    )


def check_common_bill_allocations():
    """이전 화면 오류로 생성된 0원 고지와 배분 누락을 찾는다."""
    bad = [
        {"common_bill_id": bill.id, "total_amount": bill.total_amount,
         "detail_count": len(bill.details)}
        for bill in CommonBill.query.all()
        if bill.total_amount <= 0 or not bill.details
    ]
    return Check(
        "common_bills_allocated",
        not bad,
        "0원 또는 배분 누락 공동 공과금 {}건".format(len(bad)) if bad else "전부 배분됨",
        bad,
    )


def check_electric_month_count():
    """billing_months_count 가 실제 고지월 행 수와 일치하는지."""
    bad = []
    for bill in ElectricBill.query.all():
        actual = ElectricBillMonth.query.filter_by(electric_bill_id=bill.id).count()
        if bill.billing_months_count != actual:
            bad.append(
                {
                    "electric_bill_id": bill.id,
                    "billing_months_count": bill.billing_months_count,
                    "actual_month_rows": actual,
                }
            )
    return Check(
        "electric_billing_months_count",
        not bad,
        "고지월 개수 불일치 {}건".format(len(bad)) if bad else "전부 일치",
        bad,
    )


def check_orphans():
    """FK 로 막히지만, 과거 데이터가 이전되어 온 경우를 위해 한 번 더 확인한다."""
    queries = {
        "orphan_electric_bill_months": (
            "SELECT m.id FROM electric_bill_months m "
            "LEFT JOIN electric_bills b ON b.id = m.electric_bill_id "
            "WHERE b.id IS NULL"
        ),
        "orphan_final_invoice_charges": (
            "SELECT c.id FROM final_invoice_charges c "
            "LEFT JOIN final_invoices f ON f.id = c.final_invoice_id "
            "WHERE f.id IS NULL"
        ),
        "orphan_payments": (
            "SELECT p.id FROM payments p "
            "LEFT JOIN invoice_combinations c ON c.id = p.combination_id "
            "WHERE c.id IS NULL"
        ),
    }
    bad = []
    for name, sql in queries.items():
        rows = db.session.execute(text(sql)).fetchall()
        bad.extend({"check": name, "id": r[0]} for r in rows)
    return Check(
        "no_orphan_rows",
        not bad,
        "고아 레코드 {}건".format(len(bad)) if bad else "고아 레코드 없음",
        bad,
    )


def check_carryover_flags():
    """이월 항목이 명시적 플래그로 관리되고 있는지 (참고용 리포트).

    문자열 규칙과 플래그가 어긋나는 항목을 보고한다.
    이는 오류가 아니라 **사후 검토 대상**이다. 사용자가 의도적으로
    '환급' 이라는 이름의 일반 청구를 만들었을 수 있기 때문이다.
    """
    legacy_keywords = ("미납", "초과납부", "환급", "이월")
    mismatched = []
    for charge in FinalInvoiceCharge.query.all():
        looks_carryover = any(k in (charge.description or "") for k in legacy_keywords)
        if looks_carryover != bool(charge.is_carryover):
            mismatched.append(
                {
                    "charge_id": charge.id,
                    "final_invoice_id": charge.final_invoice_id,
                    "description": charge.description,
                    "is_carryover": bool(charge.is_carryover),
                    "legacy_keyword_match": looks_carryover,
                }
            )
    return Check(
        "carryover_flag_review",
        True,  # 정보성 검사. 실패로 처리하지 않는다.
        "문자열 규칙과 플래그가 다른 항목 {}건 (검토 대상)".format(len(mismatched))
        if mismatched
        else "문자열 규칙과 플래그가 모두 일치",
        mismatched,
    )


def check_money_columns_are_integers():
    """Money 컬럼이 실제로 정수로 저장되어 있는지."""
    bad = []
    checks = (
        ("electric_bills", ("total_amount", "welfare_discount", "voucher_discount", "tv_fee_total")),
        ("electric_bill_months", ("amount", "welfare_discount", "voucher_discount", "tv_fee")),
        ("electric_bill_details", ("charged_amount",)),
        ("water_bills", ("total_amount", "welfare_discount_total")),
        ("water_bill_details", ("charged_amount",)),
        ("common_bills", ("total_amount",)),
        ("common_bill_details", ("charged_amount",)),
        ("final_invoices", ("electric_amount", "water_amount", "common_amount", "total_amount")),
        ("final_invoice_charges", ("amount",)),
        ("payments", ("payment_amount",)),
    )
    for table, columns in checks:
        for column in columns:
            rows = db.session.execute(
                text(
                    "SELECT id, {col} FROM {tbl} "
                    "WHERE typeof({col}) NOT IN ('integer', 'null')".format(
                        col=column, tbl=table
                    )
                )
            ).fetchall()
            bad.extend(
                {"table": table, "column": column, "id": r[0], "value": r[1]} for r in rows
            )
    return Check(
        "money_columns_integer",
        not bad,
        "정수가 아닌 금액 {}건".format(len(bad)) if bad else "전부 정수 저장",
        bad,
    )


def check_decimal_columns_parse():
    """ExactDecimal 컬럼이 Decimal 로 파싱되는지."""
    bad = []
    checks = (
        ("electric_readings", ("previous_reading", "current_reading")),
        (
            "electric_bill_details",
            ("usage_amount", "base_amount", "welfare_discount", "voucher_discount", "tv_fee", "final_amount"),
        ),
        ("water_bill_details", ("base_amount", "welfare_discount", "final_amount")),
        ("common_bill_details", ("amount",)),
    )
    for table, columns in checks:
        for column in columns:
            rows = db.session.execute(
                text("SELECT id, {col} FROM {tbl}".format(col=column, tbl=table))
            ).fetchall()
            for row_id, value in rows:
                if value is None:
                    continue
                try:
                    Decimal(str(value))
                except Exception:  # noqa: BLE001
                    bad.append(
                        {"table": table, "column": column, "id": row_id, "value": value}
                    )
    return Check(
        "decimal_columns_parseable",
        not bad,
        "파싱 불가 값 {}건".format(len(bad)) if bad else "전부 파싱 가능",
        bad,
    )


ALL_CHECKS = (
    check_foreign_keys_enabled,
    check_integrity,
    check_foreign_keys,
    check_orphans,
    check_billing_month_normalized,
    check_polymorphic_items,
    check_snapshots,
    check_charged_amount_rounding,
    check_final_invoice_totals,
    check_duplicate_invoice_sources,
    check_payment_invoice_pairs,
    check_negative_electric_usage,
    check_common_bill_allocations,
    check_electric_month_count,
    check_money_columns_are_integers,
    check_decimal_columns_parse,
    check_carryover_flags,
)


def run_all_checks():
    return [fn() for fn in ALL_CHECKS]


def format_report(checks):
    lines = []
    failed = 0
    for check in checks:
        mark = "OK  " if check.ok else "FAIL"
        if not check.ok:
            failed += 1
        lines.append("[{}] {} — {}".format(mark, check.name, check.detail))
        if not check.ok:
            for row in check.rows[:10]:
                lines.append("        {}".format(row))
            if len(check.rows) > 10:
                lines.append("        ... 외 {}건".format(len(check.rows) - 10))
    lines.append("")
    lines.append("검사 {}건 중 {}건 실패".format(len(checks), failed))
    return "\n".join(lines)
