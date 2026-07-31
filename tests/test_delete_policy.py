"""T-D. 삭제 정책 (FK ON DELETE).

핵심 원칙: 회계성 데이터(계산/정산/납부)는 마스터 삭제로 연쇄 삭제되지 않는다.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import (
    ElectricBill,
    ElectricBillDetail,
    ElectricBillMonth,
    ElectricReading,
    FinalInvoice,
    FinalInvoiceCharge,
    Floor,
    InvoiceCombination,
    InvoiceCombinationItem,
    Payment,
    Unit,
    WaterBill,
    WaterBillDetail,
)


# ---------------------------------------------------------------------------
# CASCADE (구조적 종속)
# ---------------------------------------------------------------------------
def test_empty_floor_delete_ok(app):
    f = Floor(floor_number=5, name="5층")
    db.session.add(f)
    db.session.commit()
    db.session.delete(f)
    db.session.commit()
    assert Floor.query.filter_by(floor_number=5).count() == 0


def test_floor_delete_cascades_units_without_history(app):
    f = Floor(floor_number=6, name="6층")
    db.session.add(f)
    db.session.flush()
    db.session.add_all(
        [Unit(floor_id=f.id, unit_name="601호"), Unit(floor_id=f.id, unit_name="602호")]
    )
    db.session.commit()

    db.session.delete(f)
    db.session.commit()
    assert Unit.query.filter_by(floor_id=f.id).count() == 0


def test_electric_bill_delete_cascades_children(app, floor, units, electric_bill):
    bill_id = electric_bill.id
    db.session.delete(electric_bill)
    db.session.commit()

    assert ElectricBillMonth.query.filter_by(electric_bill_id=bill_id).count() == 0
    assert ElectricReading.query.filter_by(electric_bill_id=bill_id).count() == 0
    assert ElectricBillDetail.query.filter_by(electric_bill_id=bill_id).count() == 0


def test_water_bill_delete_cascades_details(app, units):
    bill = WaterBill(billing_month=date(2025, 3, 1), total_amount=50000)
    db.session.add(bill)
    db.session.flush()
    unit = next(u for u in units if not u.is_vacant)
    db.session.add(
        WaterBillDetail(
            water_bill_id=bill.id,
            unit_id=unit.id,
            base_amount=Decimal("10000.00"),
            final_amount=Decimal("10000.00"),
            charged_amount=10000,
        )
    )
    db.session.commit()

    bill_id = bill.id
    db.session.delete(bill)
    db.session.commit()
    assert WaterBillDetail.query.filter_by(water_bill_id=bill_id).count() == 0


def test_combination_delete_cascades_invoices_and_charges(app, units, combination):
    invoice = FinalInvoice.query.filter_by(combination_id=combination.id).first()
    db.session.add(
        FinalInvoiceCharge(
            final_invoice_id=invoice.id, description="수리비", amount=10000
        )
    )
    db.session.commit()

    combo_id = combination.id
    invoice_id = invoice.id
    db.session.delete(combination)
    db.session.commit()

    assert FinalInvoice.query.filter_by(combination_id=combo_id).count() == 0
    assert FinalInvoiceCharge.query.filter_by(final_invoice_id=invoice_id).count() == 0
    assert InvoiceCombinationItem.query.filter_by(combination_id=combo_id).count() == 0


# ---------------------------------------------------------------------------
# RESTRICT (회계 데이터 보호)
# ---------------------------------------------------------------------------
def test_unit_with_final_invoice_cannot_be_deleted(app, units, combination):
    unit = next(u for u in units if not u.is_vacant)
    with pytest.raises(IntegrityError):
        db.session.delete(unit)
        db.session.commit()
    db.session.rollback()
    assert db.session.get(Unit, unit.id) is not None


def test_unit_with_payment_cannot_be_deleted(app, units, combination):
    unit = next(u for u in units if not u.is_vacant)
    db.session.add(
        Payment(
            combination_id=combination.id,
            unit_id=unit.id,
            payment_date=date(2025, 4, 1),
            payment_amount=17000,
        )
    )
    db.session.commit()

    with pytest.raises(IntegrityError):
        db.session.delete(unit)
        db.session.commit()
    db.session.rollback()
    assert db.session.get(Unit, unit.id) is not None


def test_unit_with_electric_detail_cannot_be_deleted(app, floor, units, electric_bill):
    unit = next(u for u in units if not u.is_vacant)
    with pytest.raises(IntegrityError):
        db.session.delete(unit)
        db.session.commit()
    db.session.rollback()
    assert db.session.get(Unit, unit.id) is not None


def test_floor_delete_with_accounting_history_is_atomic(app, floor, units, electric_bill):
    """가장 중요한 케이스: 실패 시 아무것도 삭제되지 않아야 한다."""
    unit_count_before = Unit.query.count()
    detail_count_before = ElectricBillDetail.query.count()

    with pytest.raises(IntegrityError):
        db.session.delete(floor)
        db.session.commit()
    db.session.rollback()

    assert db.session.get(Floor, floor.id) is not None
    assert Unit.query.count() == unit_count_before
    assert ElectricBillDetail.query.count() == detail_count_before


def test_electric_bill_in_invoice_cannot_be_deleted(app, floor, units, electric_bill, combination):
    db.session.add(
        InvoiceCombinationItem(
            combination_id=combination.id,
            item_type="ELECTRIC",
            billing_month=date(2025, 3, 1),
            electric_bill_id=electric_bill.id,
        )
    )
    db.session.commit()

    with pytest.raises(IntegrityError):
        db.session.delete(electric_bill)
        db.session.commit()
    db.session.rollback()
    assert db.session.get(ElectricBill, electric_bill.id) is not None


def test_combination_with_payment_cannot_be_deleted(app, units, combination):
    """★ 신규 정책: 입금 기록이 있는 정산서는 삭제할 수 없다."""
    unit = next(u for u in units if not u.is_vacant)
    db.session.add(
        Payment(
            combination_id=combination.id,
            unit_id=unit.id,
            payment_date=date(2025, 4, 1),
            payment_amount=17000,
        )
    )
    db.session.commit()

    with pytest.raises(IntegrityError):
        db.session.delete(combination)
        db.session.commit()
    db.session.rollback()
    assert db.session.get(InvoiceCombination, combination.id) is not None
    assert Payment.query.count() == 1


def test_combination_deletable_after_payments_removed(app, units, combination):
    unit = next(u for u in units if not u.is_vacant)
    payment = Payment(
        combination_id=combination.id,
        unit_id=unit.id,
        payment_date=date(2025, 4, 1),
        payment_amount=17000,
    )
    db.session.add(payment)
    db.session.commit()

    db.session.delete(payment)
    db.session.commit()
    db.session.delete(combination)
    db.session.commit()
    assert InvoiceCombination.query.count() == 0


# ---------------------------------------------------------------------------
# 라우트 레벨: 원시 SQL 에러가 아닌 한국어 메시지
# ---------------------------------------------------------------------------
def test_delete_unit_route_returns_friendly_message(app, client, csrf, units, combination):
    unit = next(u for u in units if not u.is_vacant)
    response = client.post(
        "/units/{}/delete".format(unit.id), data={"_csrf_token": csrf}
    )
    payload = response.get_json()
    assert payload["success"] is False
    assert "정산서" in payload["message"]
    assert "공실" in payload["message"]
    assert db.session.get(Unit, unit.id) is not None


def test_delete_floor_route_returns_friendly_message(app, client, csrf, floor, units, electric_bill):
    response = client.post(
        "/floors/{}/delete".format(floor.id), data={"_csrf_token": csrf}
    )
    payload = response.get_json()
    assert payload["success"] is False
    assert "전기요금" in payload["message"]
    assert db.session.get(Floor, floor.id) is not None


def test_delete_invoice_route_returns_friendly_message(app, client, csrf, units, combination):
    unit = next(u for u in units if not u.is_vacant)
    db.session.add(
        Payment(
            combination_id=combination.id,
            unit_id=unit.id,
            payment_date=date(2025, 4, 1),
            payment_amount=1000,
        )
    )
    db.session.commit()

    response = client.post(
        "/invoice/delete/{}".format(combination.id), data={"_csrf_token": csrf}
    )
    payload = response.get_json()
    assert payload["success"] is False
    assert "납부 내역" in payload["message"]
    assert db.session.get(InvoiceCombination, combination.id) is not None


def test_delete_bill_route_blocks_when_in_invoice(app, client, csrf, floor, units, electric_bill, combination):
    db.session.add(
        InvoiceCombinationItem(
            combination_id=combination.id,
            item_type="ELECTRIC",
            billing_month=date(2025, 3, 1),
            electric_bill_id=electric_bill.id,
        )
    )
    db.session.commit()

    response = client.post(
        "/bills/delete/electric/{}".format(electric_bill.id),
        data={"_csrf_token": csrf},
    )
    payload = response.get_json()
    assert payload["success"] is False
    assert "정산서" in payload["message"]
    assert db.session.get(ElectricBill, electric_bill.id) is not None
