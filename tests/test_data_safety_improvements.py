"""승인된 데이터 안전성·미리보기 정책의 통합 회귀 테스트."""

from datetime import date, timedelta
from decimal import Decimal

from extensions import db
from models import (
    CommonBill,
    ElectricBill,
    ElectricBillDetail,
    FinalInvoice,
    InvoiceCombination,
    Payment,
    WaterBill,
    WaterBillDetail,
)
from tests.invoice_helper import post_invoice


def _electric_form(csrf, floor, units, *, amount="57000", welfare="0", voucher="0", preview=False):
    data = {
        "_csrf_token": csrf,
        "billing_month": "2025-03",
        "floor_id": str(floor.id),
        "tv_distribution_mode": "INDIVIDUAL",
        "bill_month_0": "2025-03",
        "bill_amount_0": amount,
        "bill_welfare_0": welfare,
        "bill_voucher_0": voucher,
        "bill_tv_fee_0": "0",
    }
    for unit in units:
        data["prev_{}".format(unit.id)] = "0"
        data["curr_{}".format(unit.id)] = "100"
    if preview:
        data["preview"] = "true"
    return data


def test_targetless_electric_discount_is_shared_across_billed_floor(app, client, csrf, floor, units):
    occupied = [unit for unit in units if not unit.is_vacant]
    for unit in occupied:
        unit.electric_welfare = False
    db.session.commit()

    response = client.post(
        "/calculate/electric",
        data=_electric_form(csrf, floor, occupied, welfare="3000"),
    ).get_json()
    assert response["success"] is True
    assert any("전체" in warning for warning in response["warnings"])
    bill = ElectricBill.query.one()
    details = ElectricBillDetail.query.filter_by(electric_bill_id=bill.id).all()
    assert bill.welfare_discount == 3000
    assert sum(row.welfare_discount for row in details) == Decimal("3000.00")
    assert {row.welfare_discount for row in details} == {Decimal("1000.00")}


def test_targetless_water_discount_is_shared_across_included_units(app, client, csrf, floor, units):
    occupied = [unit for unit in units if not unit.is_vacant]
    for unit in occupied:
        unit.water_welfare = False
    db.session.commit()
    response = client.post("/calculate/water", data={
        "_csrf_token": csrf, "billing_month": "2025-03", "total_amount": "57000",
        "welfare_discount_total": "3000", "excluded_units": "[]",
    }).get_json()
    assert response["success"] is True
    bill = WaterBill.query.one()
    details = WaterBillDetail.query.filter_by(water_bill_id=bill.id).all()
    assert sum(row.welfare_discount for row in details) == Decimal("3000.00")
    assert {row.welfare_discount for row in details} == {Decimal("1000.00")}


def test_water_discount_overage_stops_at_zero_without_redistribution(
    app, client, csrf, floor, units
):
    result = client.post("/calculate/water", data={
        "_csrf_token": csrf,
        "billing_month": "2025-03",
        "total_amount": "1000",
        "welfare_discount_total": "50000",
        "excluded_units": "[]",
    }).get_json()

    assert result["success"] is True
    assert any("재배분하지 않았습니다" in warning for warning in result["warnings"])
    target = next(unit for unit in units if unit.water_welfare)
    detail = WaterBillDetail.query.filter_by(unit_id=target.id).one()
    assert detail.final_amount == Decimal("0.00")


def test_calculation_preview_uses_same_engine_without_persisting(app, client, csrf, floor, units):
    occupied = [unit for unit in units if not unit.is_vacant]
    data = _electric_form(csrf, floor, occupied, amount="30000", preview=True)
    for unit in occupied:
        data["curr_{}".format(unit.id)] = "0"
    result = client.post("/calculate/electric", data=data).get_json()
    assert result["success"] is True
    assert any("사용량이 0" in warning for warning in result["preview"]["warnings"])
    assert result["preview"]["charged_total"] == 35000
    assert ElectricBill.query.count() == 0


def test_common_duplicate_requires_confirmation_and_request_is_idempotent(app, client, csrf, floor, units):
    base = {
        "_csrf_token": csrf, "billing_month": "2025-03", "description": "인터넷",
        "total_amount": "30000", "distribution_method": "BY_UNITS",
        "request_token": "request-one",
    }
    assert client.post("/calculate/common", data=base).get_json()["success"] is True
    replay = client.post("/calculate/common", data=base).get_json()
    assert replay["success"] is True and replay["idempotent"] is True
    assert CommonBill.query.count() == 1

    second = dict(base, request_token="request-two")
    blocked = client.post("/calculate/common", data=second).get_json()
    assert blocked["success"] is False
    assert blocked["duplicate_requires_confirmation"] is True
    second["duplicate_action"] = "add"
    assert client.post("/calculate/common", data=second).get_json()["success"] is True
    assert CommonBill.query.count() == 2


def test_mixed_settlement_months_are_valid_in_one_invoice(app, client, csrf, floor, units):
    bills = []
    for month, description in (("2025-02", "인터넷"), ("2025-03", "관리비")):
        result = client.post("/calculate/common", data={
            "_csrf_token": csrf, "billing_month": month, "description": description,
            "total_amount": "30000", "distribution_method": "BY_UNITS",
        }).get_json()
        assert result["success"] is True
        bills.append(CommonBill.query.filter_by(description=description).one())
    response = post_invoice(client, {
        "_csrf_token": csrf, "name": "혼합월 정산", "memo": "",
        "items": [
            {"type": "COMMON", "id": bills[0].id, "month": "2025-02", "description": "인터넷"},
            {"type": "COMMON", "id": bills[1].id, "month": "2025-03", "description": "관리비"},
        ],
        "unit_additional_data": {},
    })
    assert response.get_json()["success"] is True
    combination = InvoiceCombination.query.one()
    assert {item.billing_month.strftime("%Y-%m") for item in combination.items} == {"2025-02", "2025-03"}


def test_invoice_identity_snapshot_survives_unit_rename(app, client, csrf, floor, units):
    client.post("/calculate/common", data={
        "_csrf_token": csrf, "billing_month": "2025-03", "description": "인터넷",
        "total_amount": "30000", "distribution_method": "BY_UNITS",
    })
    bill = CommonBill.query.one()
    response = post_invoice(client, {
        "_csrf_token": csrf, "name": "스냅샷 정산", "memo": "",
        "items": [{"type": "COMMON", "id": bill.id, "month": "2025-03", "description": "인터넷"}],
        "unit_additional_data": {},
    })
    combination_id = response.get_json()["id"]
    unit = units[0]
    old_name = unit.unit_name
    unit.unit_name = "변경된 세대명"
    unit.floor.name = "변경된 층명"
    db.session.commit()
    invoice = FinalInvoice.query.filter_by(combination_id=combination_id, unit_id=unit.id).one()
    assert invoice.unit_name_snapshot == old_name
    html = client.get("/invoice/print/{}".format(combination_id)).get_data(as_text=True)
    assert old_name in html
    assert "변경된 세대명" not in html


def test_invoice_create_rejects_changes_after_preview(app, client, csrf, floor, units):
    client.post("/calculate/common", data={
        "_csrf_token": csrf, "billing_month": "2025-03", "description": "인터넷",
        "total_amount": "30000", "distribution_method": "BY_UNITS",
    })
    bill = CommonBill.query.one()
    payload = {
        "_csrf_token": csrf, "name": "확인 전 이름", "memo": "",
        "items": [{"type": "COMMON", "id": bill.id, "month": "2025-03", "description": "인터넷"}],
        "unit_additional_data": {},
    }
    preview = client.post("/invoice/preview", json=payload).get_json()
    payload["name"] = "확인 후 변경"
    payload["preview_token"] = preview["preview_token"]
    result = client.post("/invoice/create", json=payload).get_json()
    assert result["success"] is False
    assert "변경" in result["message"]
    assert InvoiceCombination.query.count() == 0


def test_future_overpayment_requires_second_confirmation(app, client, csrf, units, combination):
    unit = next(unit for unit in units if not unit.is_vacant)
    payload = {
        "_csrf_token": csrf, "combination_id": combination.id, "unit_id": unit.id,
        "payment_date": (date.today() + timedelta(days=1)).isoformat(),
        "payment_amount": 20000, "payment_method": "계좌이체", "memo": "경고 테스트",
    }
    first = client.post("/payments/add", json=payload).get_json()
    assert first["success"] is False
    assert first["requires_confirmation"] is True
    assert len(first["warnings"]) == 2
    assert Payment.query.count() == 0
    payload["acknowledge_warnings"] = True
    assert client.post("/payments/add", json=payload).get_json()["success"] is True
    assert Payment.query.count() == 1
