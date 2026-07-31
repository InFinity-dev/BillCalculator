"""T-Y. 스냅샷 불변성.

계산 시점의 세대 속성이 박제되어, 이후 세대 마스터가 바뀌어도
과거 정산 내역이 왜곡되지 않아야 한다. 이 설계 자산은 반드시 유지된다.
"""

from decimal import Decimal

import pytest

from extensions import db
from models import CommonBillDetail, ElectricBillDetail, WaterBillDetail

SNAPSHOT_KEYS = {
    "unit_name",
    "electric_welfare",
    "electric_voucher",
    "has_tv",
    "water_welfare",
    "residents_count",
    "is_vacant",
}


def _electric_form(csrf, floor_id, units):
    data = {
        "_csrf_token": csrf,
        "billing_month": "2025-03",
        "floor_id": str(floor_id),
        "tv_distribution_mode": "INDIVIDUAL",
        "month_count": "1",
        "bill_month_0": "2025-03",
        "bill_amount_0": "60000",
        "bill_welfare_0": "0",
        "bill_voucher_0": "0",
        "bill_tv_fee_0": "0",
    }
    for unit in units:
        data["prev_{}".format(unit.id)] = "0"
        data["curr_{}".format(unit.id)] = "100"
    return data


@pytest.fixture()
def calculated(app, client, csrf, floor, units):
    occupied = [u for u in units if not u.is_vacant]
    client.post("/calculate/electric", data=_electric_form(csrf, floor.id, occupied))
    client.post(
        "/calculate/water",
        data={
            "_csrf_token": csrf,
            "billing_month": "2025-03",
            "total_amount": "60000",
            "excluded_units": "[]",
        },
    )
    client.post(
        "/calculate/common",
        data={
            "_csrf_token": csrf,
            "billing_month": "2025-03",
            "description": "인터넷",
            "total_amount": "30000",
            "distribution_method": "BY_RESIDENTS",
        },
    )
    return occupied


def test_snapshot_present_on_all_three_detail_tables(app, calculated):
    for model in (ElectricBillDetail, WaterBillDetail, CommonBillDetail):
        rows = model.query.all()
        assert rows
        for row in rows:
            assert row.unit_snapshot is not None
            assert set(row.unit_snapshot) == SNAPSHOT_KEYS


def test_snapshot_captures_values_at_calculation_time(app, calculated):
    unit = calculated[0]
    detail = ElectricBillDetail.query.filter_by(unit_id=unit.id).one()
    assert detail.unit_snapshot["unit_name"] == "101호"
    assert detail.unit_snapshot["residents_count"] == 2
    assert detail.unit_snapshot["electric_welfare"] is True
    assert detail.unit_snapshot["is_vacant"] is False


def test_renaming_unit_does_not_change_past_snapshot(app, calculated):
    """★ 핵심: 세대명을 바꿔도 과거 스냅샷은 그대로다."""
    unit = calculated[0]
    unit.unit_name = "101호(개명)"
    db.session.commit()
    db.session.expire_all()

    detail = ElectricBillDetail.query.filter_by(unit_id=unit.id).one()
    assert detail.unit_snapshot["unit_name"] == "101호"


def test_changing_residents_does_not_change_past_snapshot(app, calculated):
    unit = calculated[2]
    original = WaterBillDetail.query.filter_by(unit_id=unit.id).one().unit_snapshot[
        "residents_count"
    ]
    unit.residents_count = 99
    db.session.commit()
    db.session.expire_all()

    assert (
        WaterBillDetail.query.filter_by(unit_id=unit.id).one().unit_snapshot[
            "residents_count"
        ]
        == original
    )


def test_marking_vacant_does_not_change_past_snapshot(app, calculated):
    unit = calculated[1]
    unit.is_vacant = True
    db.session.commit()
    db.session.expire_all()

    detail = CommonBillDetail.query.filter_by(unit_id=unit.id).one()
    assert detail.unit_snapshot["is_vacant"] is False


def test_changing_unit_does_not_change_past_amounts(app, calculated):
    """세대 속성 변경이 과거 청구액에 영향을 주지 않아야 한다."""
    unit = calculated[0]
    before = {
        d.id: d.charged_amount for d in ElectricBillDetail.query.all()
    }

    unit.residents_count = 50
    unit.has_tv = False
    unit.electric_welfare = False
    db.session.commit()
    db.session.expire_all()

    after = {d.id: d.charged_amount for d in ElectricBillDetail.query.all()}
    assert after == before


def test_snapshot_survives_json_round_trip(app, calculated):
    """JSON 컬럼이 dict 로 정확히 복원되는지 (SQLite TEXT 직렬화 확인)."""
    detail = ElectricBillDetail.query.first()
    db.session.expire_all()
    reloaded = db.session.get(ElectricBillDetail, detail.id)
    assert isinstance(reloaded.unit_snapshot, dict)
    assert isinstance(reloaded.unit_snapshot["residents_count"], int)
    assert isinstance(reloaded.unit_snapshot["has_tv"], bool)
