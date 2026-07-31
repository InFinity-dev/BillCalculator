"""T-N. Money / ExactDecimal 타입 및 10원 올림 정책."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import Numeric, text

import app as app_module
from db_types import ExactDecimal, Money, has_fraction, money_to_int
from extensions import db
from models import ElectricBill, ElectricReading, Floor, Unit


# --- round_up_to_10 : 도메인 정책. 구현이 바뀌면 안 된다 -----------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        (0, 0),
        (1, 10),
        (10, 10),
        (11, 20),
        (Decimal("4999.01"), 5000),
        (Decimal("12345.67"), 12350),
        (Decimal("33333.33"), 33340),
        (9.99, 10),
        (Decimal("0.001"), 10),
    ],
)
def test_round_up_to_10(value, expected):
    assert app_module.round_up_to_10(value) == expected


def test_round_up_to_10_result_is_multiple_of_10():
    for raw in range(0, 2000, 7):
        assert app_module.round_up_to_10(Decimal(raw) / 3) % 10 == 0


# --- Money ------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        (Decimal("12500.00"), 12500),
        (12500, 12500),
        ("12,500", 12500),
        (Decimal("0"), 0),
        (None, 0),
        ("", 0),
    ],
)
def test_money_to_int(value, expected):
    assert money_to_int(value) == expected


@pytest.mark.parametrize(
    "value,expected", [(Decimal("12500.00"), False), (Decimal("12500.01"), True),
                       (12500, False), (None, False)]
)
def test_has_fraction(value, expected):
    assert has_fraction(value) is expected


def test_money_roundtrip_is_int(app, floor):
    bill = ElectricBill(
        billing_month=date(2025, 5, 1),
        floor_id=floor.id,
        total_amount=Decimal("123450.00"),
    )
    db.session.add(bill)
    db.session.commit()
    db.session.expire_all()

    stored = db.session.get(ElectricBill, bill.id)
    assert stored.total_amount == 123450
    assert isinstance(stored.total_amount, int)


def test_money_stored_as_sqlite_integer(app, floor):
    """부동소수점이 아니라 INTEGER 로 저장되어야 한다."""
    bill = ElectricBill(
        billing_month=date(2025, 5, 1), floor_id=floor.id, total_amount=Decimal("999.00")
    )
    db.session.add(bill)
    db.session.commit()

    typeof = db.session.execute(
        text("SELECT typeof(total_amount) FROM electric_bills WHERE id = :i"),
        {"i": bill.id},
    ).scalar()
    assert typeof == "integer"


def test_money_none_is_preserved():
    money = Money()
    assert money.process_bind_param(None, None) is None
    assert money.process_result_value(None, None) is None


# --- ExactDecimal -----------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        (Decimal("12345.67"), Decimal("12345.67")),
        (Decimal("-12.34"), Decimal("-12.34")),
        (Decimal("0.005"), Decimal("0.01")),   # ROUND_HALF_UP (MySQL DECIMAL 과 동일)
        (Decimal("0.004"), Decimal("0.00")),
        (100, Decimal("100.00")),
    ],
)
def test_exact_decimal_bind_and_result(value, expected):
    column = ExactDecimal()
    stored = column.process_bind_param(value, None)
    assert column.process_result_value(stored, None) == expected


def test_exact_decimal_roundtrip_through_db(app, floor, units, electric_bill):
    reading = ElectricReading.query.filter_by(electric_bill_id=electric_bill.id).first()
    reading.previous_reading = Decimal("1234.56")
    db.session.commit()
    db.session.expire_all()

    stored = db.session.get(ElectricReading, reading.id)
    assert stored.previous_reading == Decimal("1234.56")
    assert isinstance(stored.previous_reading, Decimal)


def test_exact_decimal_stored_as_text(app, floor, units, electric_bill):
    """REAL(부동소수점)이 아니라 TEXT 로 저장되어야 한다."""
    typeof = db.session.execute(
        text(
            "SELECT typeof(previous_reading) FROM electric_readings "
            "WHERE electric_bill_id = :i LIMIT 1"
        ),
        {"i": electric_bill.id},
    ).scalar()
    assert typeof == "text"


def test_exact_decimal_no_float_drift(app, floor, units, electric_bill):
    """float 경유 시 오차가 생기는 값이 정확히 보존되는지."""
    reading = ElectricReading.query.filter_by(electric_bill_id=electric_bill.id).first()
    reading.current_reading = Decimal("0.10")
    reading.previous_reading = Decimal("0.03")
    db.session.commit()
    db.session.expire_all()

    stored = db.session.get(ElectricReading, reading.id)
    assert stored.current_reading - stored.previous_reading == Decimal("0.07")


# --- 스키마 전반 -------------------------------------------------------------
def test_no_numeric_columns_anywhere(app):
    """모델에 Numeric 이 남아 있으면 SQLite 에서 부동소수점 저장이 된다."""
    offenders = []
    for table in db.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, Numeric) and not isinstance(
                column.type, (Money, ExactDecimal)
            ):
                offenders.append("{}.{}".format(table.name, column.name))
    assert offenders == []
