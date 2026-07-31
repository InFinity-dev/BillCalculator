"""T-C. UNIQUE / CHECK / FK 제약.

DB 가 실제로 규칙을 강제하는지 확인한다.
과도한 제약으로 정상 도메인 값을 막고 있지 않은지도 함께 검증한다.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import (
    CommonBill,
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
    Setting,
    Unit,
    WaterBill,
)


def _expect_integrity_error(callable_):
    with pytest.raises(IntegrityError):
        callable_()
        db.session.commit()
    db.session.rollback()


# ---------------------------------------------------------------------------
# UNIQUE
# ---------------------------------------------------------------------------
def test_duplicate_floor_number_rejected(app, floor):
    def act():
        db.session.add(Floor(floor_number=floor.floor_number, name="중복"))

    _expect_integrity_error(act)


def test_duplicate_unit_name_in_same_floor_rejected(app, floor, units):
    def act():
        db.session.add(Unit(floor_id=floor.id, unit_name="101호"))

    _expect_integrity_error(act)


def test_same_unit_name_on_different_floor_allowed(app, floor, units):
    """과도한 제약이 아님을 확인한다."""
    other = Floor(floor_number=2, name="2층")
    db.session.add(other)
    db.session.commit()
    db.session.add(Unit(floor_id=other.id, unit_name="101호"))
    db.session.commit()
    assert Unit.query.filter_by(unit_name="101호").count() == 2


def test_duplicate_electric_bill_floor_month_rejected(app, floor, units, electric_bill):
    def act():
        db.session.add(
            ElectricBill(billing_month=date(2025, 3, 1), floor_id=floor.id, total_amount=1)
        )

    _expect_integrity_error(act)


def test_duplicate_water_bill_month_rejected(app):
    db.session.add(WaterBill(billing_month=date(2025, 3, 1), total_amount=1000))
    db.session.commit()

    def act():
        db.session.add(WaterBill(billing_month=date(2025, 3, 1), total_amount=2000))

    _expect_integrity_error(act)


def test_duplicate_electric_detail_rejected(app, floor, units, electric_bill):
    unit = next(u for u in units if not u.is_vacant)

    def act():
        db.session.add(
            ElectricBillDetail(
                electric_bill_id=electric_bill.id,
                unit_id=unit.id,
                usage_amount=Decimal("1.00"),
                base_amount=Decimal("1.00"),
                final_amount=Decimal("1.00"),
                charged_amount=10,
            )
        )

    _expect_integrity_error(act)


def test_duplicate_final_invoice_rejected(app, units, combination):
    unit = next(u for u in units if not u.is_vacant)

    def act():
        db.session.add(
            FinalInvoice(
                combination_id=combination.id, unit_id=unit.id, total_amount=100
            )
        )

    _expect_integrity_error(act)


def test_duplicate_electric_bill_month_rejected(app, floor, units, electric_bill):
    def act():
        db.session.add(
            ElectricBillMonth(
                electric_bill_id=electric_bill.id,
                billing_month=date(2025, 3, 1),
                amount=1,
            )
        )

    _expect_integrity_error(act)


def test_duplicate_setting_key_rejected(app):
    def act():
        db.session.add(Setting(setting_key="tv_fee", setting_value="1"))

    _expect_integrity_error(act)


def test_multiple_common_bills_in_same_month_allowed(app):
    """같은 달에 인터넷/관리비 등 여러 항목이 정상이다."""
    db.session.add(
        CommonBill(billing_month=date(2025, 3, 1), description="인터넷", total_amount=30000)
    )
    db.session.add(
        CommonBill(billing_month=date(2025, 3, 1), description="관리비", total_amount=50000)
    )
    db.session.commit()
    assert CommonBill.query.count() == 2


# ---------------------------------------------------------------------------
# CHECK
# ---------------------------------------------------------------------------
def test_billing_month_is_normalized_by_model(app, floor):
    bill = ElectricBill(
        billing_month=date(2025, 3, 17), floor_id=floor.id, total_amount=1000
    )
    db.session.add(bill)
    db.session.commit()
    assert bill.billing_month == date(2025, 3, 1)


def test_billing_month_check_rejects_raw_non_first_day(app, floor):
    """모델 validator 를 우회한 raw SQL 도 DB CHECK 가 막아야 한다."""
    with pytest.raises(IntegrityError):
        db.session.execute(
            text(
                "INSERT INTO electric_bills "
                "(billing_month, floor_id, total_amount, welfare_discount, "
                " voucher_discount, tv_fee_total, tv_distribution_mode, "
                " billing_months_count, created_at, updated_at) "
                "VALUES ('2025-03-17', :f, 0, 0, 0, 0, 'INDIVIDUAL', 1, "
                "        '2025-03-17 00:00:00', '2025-03-17 00:00:00')"
            ),
            {"f": floor.id},
        )
        db.session.commit()
    db.session.rollback()


def test_negative_residents_count_rejected(app, floor):
    def act():
        db.session.add(Unit(floor_id=floor.id, unit_name="X", residents_count=-1))

    _expect_integrity_error(act)


def test_zero_residents_count_allowed(app, floor):
    db.session.add(Unit(floor_id=floor.id, unit_name="Y", residents_count=0))
    db.session.commit()
    assert Unit.query.filter_by(unit_name="Y").one().residents_count == 0


def test_invalid_tv_distribution_mode_rejected(app, floor):
    def act():
        db.session.add(
            ElectricBill(
                billing_month=date(2025, 6, 1),
                floor_id=floor.id,
                total_amount=1000,
                tv_distribution_mode="WRONG",
            )
        )

    _expect_integrity_error(act)


def test_invalid_distribution_method_rejected(app):
    def act():
        db.session.add(
            CommonBill(
                billing_month=date(2025, 6, 1),
                total_amount=1000,
                distribution_method="WRONG",
            )
        )

    _expect_integrity_error(act)


def test_invalid_item_type_rejected(app, combination, floor, units, electric_bill):
    def act():
        db.session.add(
            InvoiceCombinationItem(
                combination_id=combination.id,
                item_type="WRONG",
                billing_month=date(2025, 3, 1),
                electric_bill_id=electric_bill.id,
            )
        )

    _expect_integrity_error(act)


def test_negative_charged_amount_rejected(app, floor, units, electric_bill):
    other_floor = Floor(floor_number=9, name="9층")
    db.session.add(other_floor)
    db.session.commit()
    unit = Unit(floor_id=other_floor.id, unit_name="901호")
    db.session.add(unit)
    db.session.commit()

    def act():
        db.session.add(
            ElectricBillDetail(
                electric_bill_id=electric_bill.id,
                unit_id=unit.id,
                usage_amount=Decimal("1.00"),
                base_amount=Decimal("1.00"),
                final_amount=Decimal("1.00"),
                charged_amount=-10,
            )
        )

    _expect_integrity_error(act)


def test_negative_payment_amount_rejected(app, units, combination):
    unit = next(u for u in units if not u.is_vacant)

    def act():
        db.session.add(
            Payment(
                combination_id=combination.id,
                unit_id=unit.id,
                payment_date=date(2025, 4, 1),
                payment_amount=-1,
            )
        )

    _expect_integrity_error(act)


def test_negative_reading_rejected(app, floor, units, electric_bill):
    """TEXT 컬럼에 대한 CAST 기반 CHECK 가 실제로 동작하는지."""
    other = Floor(floor_number=8, name="8층")
    db.session.add(other)
    db.session.commit()
    unit = Unit(floor_id=other.id, unit_name="801호")
    db.session.add(unit)
    db.session.commit()

    def act():
        db.session.add(
            ElectricReading(
                electric_bill_id=electric_bill.id,
                unit_id=unit.id,
                previous_reading=Decimal("-1.00"),
                current_reading=Decimal("10.00"),
            )
        )

    _expect_integrity_error(act)


# --- 도메인상 허용되어야 하는 값 (과도 제약 방지) -------------------------------
def test_negative_usage_amount_allowed(app, floor, units, electric_bill):
    """현월 검침이 전월보다 작으면 사용량이 음수가 된다. 도메인이 허용하는 흐름이다."""
    other = Floor(floor_number=7, name="7층")
    db.session.add(other)
    db.session.commit()
    unit = Unit(floor_id=other.id, unit_name="701호")
    db.session.add(unit)
    db.session.commit()

    db.session.add(
        ElectricBillDetail(
            electric_bill_id=electric_bill.id,
            unit_id=unit.id,
            usage_amount=Decimal("-5.50"),
            base_amount=Decimal("-1000.00"),
            final_amount=Decimal("0.00"),
            charged_amount=0,
        )
    )
    db.session.commit()
    stored = ElectricBillDetail.query.filter_by(unit_id=unit.id).one()
    assert stored.usage_amount == Decimal("-5.50")
    assert stored.base_amount == Decimal("-1000.00")


def test_refund_charge_can_be_negative(app, units, combination):
    invoice = FinalInvoice.query.filter_by(combination_id=combination.id).first()
    db.session.add(
        FinalInvoiceCharge(
            final_invoice_id=invoice.id,
            description="초과납부 환급",
            amount=-50000,
            is_carryover=True,
        )
    )
    db.session.commit()
    assert invoice.charges[0].amount == -50000


def test_final_invoice_total_can_be_negative(app, units, combination):
    """환급이 청구를 초과하면 총액이 음수가 될 수 있다."""
    unit = next(u for u in units if not u.is_vacant)
    other = InvoiceCombination(invoice_name="환급 정산")
    db.session.add(other)
    db.session.flush()
    db.session.add(
        FinalInvoice(combination_id=other.id, unit_id=unit.id, total_amount=-10000)
    )
    db.session.commit()
    assert FinalInvoice.query.filter_by(combination_id=other.id).one().total_amount == -10000


# ---------------------------------------------------------------------------
# 다형 참조 CHECK
# ---------------------------------------------------------------------------
def _item(combination, **kwargs):
    base = {
        "combination_id": combination.id,
        "billing_month": date(2025, 3, 1),
    }
    base.update(kwargs)
    return InvoiceCombinationItem(**base)


def test_polymorphic_electric_ok(app, combination, floor, units, electric_bill):
    db.session.add(
        _item(combination, item_type="ELECTRIC", electric_bill_id=electric_bill.id)
    )
    db.session.commit()
    assert InvoiceCombinationItem.query.count() == 1


def test_polymorphic_all_null_rejected(app, combination):
    def act():
        db.session.add(_item(combination, item_type="ELECTRIC"))

    _expect_integrity_error(act)


def test_polymorphic_type_mismatch_rejected(app, combination):
    water = WaterBill(billing_month=date(2025, 3, 1), total_amount=1000)
    db.session.add(water)
    db.session.commit()

    def act():
        db.session.add(_item(combination, item_type="ELECTRIC", water_bill_id=water.id))

    _expect_integrity_error(act)


def test_polymorphic_two_fks_rejected(app, combination, floor, units, electric_bill):
    water = WaterBill(billing_month=date(2025, 3, 1), total_amount=1000)
    db.session.add(water)
    db.session.commit()

    def act():
        db.session.add(
            _item(
                combination,
                item_type="ELECTRIC",
                electric_bill_id=electric_bill.id,
                water_bill_id=water.id,
            )
        )

    _expect_integrity_error(act)


def test_polymorphic_water_ok(app, combination):
    water = WaterBill(billing_month=date(2025, 3, 1), total_amount=1000)
    db.session.add(water)
    db.session.commit()
    db.session.add(_item(combination, item_type="WATER", water_bill_id=water.id))
    db.session.commit()
    assert InvoiceCombinationItem.query.count() == 1


def test_polymorphic_common_ok(app, combination):
    common = CommonBill(
        billing_month=date(2025, 3, 1), description="인터넷", total_amount=30000
    )
    db.session.add(common)
    db.session.commit()
    db.session.add(_item(combination, item_type="COMMON", common_bill_id=common.id))
    db.session.commit()
    assert InvoiceCombinationItem.query.count() == 1


# ---------------------------------------------------------------------------
# FK
# ---------------------------------------------------------------------------
def test_unit_with_missing_floor_rejected(app):
    def act():
        db.session.add(Unit(floor_id=99999, unit_name="없는층"))

    _expect_integrity_error(act)


def test_payment_with_missing_unit_rejected(app, combination):
    def act():
        db.session.add(
            Payment(
                combination_id=combination.id,
                unit_id=99999,
                payment_date=date(2025, 4, 1),
                payment_amount=1000,
            )
        )

    _expect_integrity_error(act)


def test_invoice_item_with_missing_bill_rejected(app, combination):
    def act():
        db.session.add(
            _item(combination, item_type="ELECTRIC", electric_bill_id=99999)
        )

    _expect_integrity_error(act)
