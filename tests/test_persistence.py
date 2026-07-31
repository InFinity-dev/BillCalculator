"""T-P. 계산 결과 영속화 회귀.

DB 엔진 교체로 **금액이 1원도 달라지지 않음**을 증명한다.
기대값은 도메인 알고리즘을 손으로 전개해 명시한다.
"""

from datetime import date
from decimal import Decimal

import pytest

from extensions import db
from models import (
    CommonBill,
    CommonBillDetail,
    ElectricBill,
    ElectricBillDetail,
    ElectricBillMonth,
    ElectricReading,
    FinalInvoice,
    FinalInvoiceCharge,
    Floor,
    InvoiceCombination,
    Payment,
    Setting,
    Unit,
    WaterBill,
    WaterBillDetail,
)


def _electric_form(csrf, floor_id, rows, readings, **extra):
    data = {
        "_csrf_token": csrf,
        "billing_month": "2025-03",
        "floor_id": str(floor_id),
        "tv_distribution_mode": "INDIVIDUAL",
        "month_count": str(len(rows)),
    }
    for index, row in enumerate(rows):
        data["bill_month_{}".format(index)] = row["month"]
        data["bill_amount_{}".format(index)] = str(row.get("amount", 0))
        data["bill_welfare_{}".format(index)] = str(row.get("welfare", 0))
        data["bill_voucher_{}".format(index)] = str(row.get("voucher", 0))
        data["bill_tv_fee_{}".format(index)] = str(row.get("tv_fee", 0))
    for unit_id, (prev, curr) in readings.items():
        data["prev_{}".format(unit_id)] = str(prev)
        data["curr_{}".format(unit_id)] = str(curr)
    data.update(extra)
    return data


def _by_unit(bill_id):
    return {
        d.unit_id: d
        for d in ElectricBillDetail.query.filter_by(electric_bill_id=bill_id).all()
    }


# ---------------------------------------------------------------------------
# 전기
# ---------------------------------------------------------------------------
def test_electric_basic_allocation(app, client, csrf, floor, units):
    """사용량 비례 배분. 할인/TV 없음."""
    occupied = [u for u in units if not u.is_vacant]
    readings = {occupied[0].id: (0, 100), occupied[1].id: (0, 200), occupied[2].id: (0, 300)}

    response = client.post(
        "/calculate/electric",
        data=_electric_form(
            csrf, floor.id, [{"month": "2025-03", "amount": 60000}], readings
        ),
    )
    assert response.get_json()["success"] is True

    bill = ElectricBill.query.one()
    assert bill.total_amount == 60000
    assert bill.billing_months_count == 1

    details = _by_unit(bill.id)
    # total_usage = 600, original = 60000 (할인 없음)
    # 101호: 100/600 * 60000 = 10000, TV 보유 → +2500 = 12500
    # 102호: 200/600 * 60000 = 20000, TV 없음 →  20000
    # 103호: 300/600 * 60000 = 30000, TV 보유 → +2500 = 32500
    assert details[occupied[0].id].base_amount == Decimal("10000.00")
    assert details[occupied[0].id].tv_fee == Decimal("2500.00")
    assert details[occupied[0].id].charged_amount == 12500
    assert details[occupied[1].id].tv_fee == Decimal("0.00")
    assert details[occupied[1].id].charged_amount == 20000
    assert details[occupied[2].id].charged_amount == 32500


def test_electric_grossing_up_with_discounts(app, client, csrf, floor, units):
    """★ grossing-up: original = total + 복지총액 + 바우처총액."""
    occupied = [u for u in units if not u.is_vacant]
    readings = {occupied[0].id: (0, 100), occupied[1].id: (0, 100), occupied[2].id: (0, 100)}

    response = client.post(
        "/calculate/electric",
        data=_electric_form(
            csrf,
            floor.id,
            [{"month": "2025-03", "amount": 57000, "welfare": 2000, "voucher": 1000}],
            readings,
            tv_distribution_mode="INDIVIDUAL",
        ),
    )
    assert response.get_json()["success"] is True

    bill = ElectricBill.query.one()
    assert bill.total_amount == 57000
    assert bill.welfare_discount == 2000     # 입력값 우선 경로
    assert bill.voucher_discount == 1000

    details = _by_unit(bill.id)
    # original = 57000 + 2000 + 1000 = 60000, 세대별 usage 동일 → base = 20000
    # 101호(복지): 20000 - 2000 + 2500(TV) = 20500
    # 102호(바우처, TV없음): 20000 - 1000 = 19000
    # 103호: 20000 + 2500 = 22500
    for detail in details.values():
        assert detail.base_amount == Decimal("20000.00")
    assert details[occupied[0].id].welfare_discount == Decimal("2000.00")
    assert details[occupied[0].id].charged_amount == 20500
    assert details[occupied[1].id].voucher_discount == Decimal("1000.00")
    assert details[occupied[1].id].charged_amount == 19000
    assert details[occupied[2].id].charged_amount == 22500


def test_electric_discount_falls_back_to_setting(app, client, csrf, floor, units):
    """입력 할인이 0이면 설정값 × 개월수를 쓴다."""
    Setting.query.filter_by(setting_key="electric_welfare_amount").one().setting_value = "1500"
    db.session.commit()

    occupied = [u for u in units if not u.is_vacant]
    readings = {u.id: (0, 100) for u in occupied}

    client.post(
        "/calculate/electric",
        data=_electric_form(
            csrf, floor.id, [{"month": "2025-03", "amount": 30000}], readings
        ),
    )
    bill = ElectricBill.query.one()
    assert bill.welfare_discount == 1500     # 복지 대상 1세대 × 1500 × 1개월
    details = _by_unit(bill.id)
    assert details[occupied[0].id].welfare_discount == Decimal("1500.00")


def test_electric_zero_usage_falls_back_to_equal_split(app, client, csrf, floor, units):
    """★ 조용한 fallback: total_usage == 0 이면 균등 분할."""
    occupied = [u for u in units if not u.is_vacant]
    readings = {u.id: (0, 0) for u in occupied}

    client.post(
        "/calculate/electric",
        data=_electric_form(
            csrf, floor.id, [{"month": "2025-03", "amount": 30000}], readings
        ),
    )
    bill = ElectricBill.query.one()
    details = _by_unit(bill.id)
    for detail in details.values():
        assert detail.base_amount == Decimal("10000.00")


def test_electric_final_amount_clamped_to_zero(app, client, csrf, floor, units):
    """할인이 배분액을 초과하면 0으로 clamp 된다."""
    occupied = [u for u in units if not u.is_vacant]
    readings = {occupied[0].id: (0, 1), occupied[1].id: (0, 1000), occupied[2].id: (0, 1000)}

    client.post(
        "/calculate/electric",
        data=_electric_form(
            csrf,
            floor.id,
            [{"month": "2025-03", "amount": 1000, "welfare": 50000}],
            readings,
            tv_distribution_mode="INDIVIDUAL",
        ),
    )
    bill = ElectricBill.query.one()
    details = _by_unit(bill.id)
    assert details[occupied[0].id].final_amount == Decimal("0.00")
    assert details[occupied[0].id].charged_amount == 0


def test_electric_tv_equal_mode(app, client, csrf, floor, units):
    """EQUAL 모드는 TV 총액을 전 재실 세대가 균등 분담한다."""
    occupied = [u for u in units if not u.is_vacant]
    readings = {u.id: (0, 100) for u in occupied}

    client.post(
        "/calculate/electric",
        data=_electric_form(
            csrf,
            floor.id,
            [{"month": "2025-03", "amount": 30000, "tv_fee": 7500}],
            readings,
            tv_distribution_mode="EQUAL",
        ),
    )
    bill = ElectricBill.query.one()
    assert bill.tv_fee_total == 7500
    details = _by_unit(bill.id)
    # TV 미보유 세대(102호)도 균등 분담한다
    for detail in details.values():
        assert detail.tv_fee == Decimal("2500.00")


def test_electric_multi_month_bundle(app, client, csrf, floor, units):
    """★ N개월 묶음: electric_bill_months 행이 생성되고 TV 단가에 개월수가 곱해진다."""
    occupied = [u for u in units if not u.is_vacant]
    readings = {u.id: (0, 100) for u in occupied}

    client.post(
        "/calculate/electric",
        data=_electric_form(
            csrf,
            floor.id,
            [
                {"month": "2025-01", "amount": 10000},
                {"month": "2025-02", "amount": 20000},
                {"month": "2025-03", "amount": 30000},
            ],
            readings,
        ),
    )
    bill = ElectricBill.query.one()
    assert bill.total_amount == 60000
    assert bill.billing_months_count == 3

    months = ElectricBillMonth.query.filter_by(electric_bill_id=bill.id).order_by(
        ElectricBillMonth.billing_month
    ).all()
    assert [m.billing_month for m in months] == [
        date(2025, 1, 1), date(2025, 2, 1), date(2025, 3, 1)
    ]
    assert [m.amount for m in months] == [10000, 20000, 30000]

    details = _by_unit(bill.id)
    # INDIVIDUAL 모드: TV 단가 2500 × 3개월 = 7500
    assert details[occupied[0].id].tv_fee == Decimal("7500.00")


def test_electric_monthly_details_legacy_shape_preserved(app, client, csrf, floor, units):
    """템플릿이 소비하는 monthly_details 형태가 그대로 유지되어야 한다."""
    occupied = [u for u in units if not u.is_vacant]
    client.post(
        "/calculate/electric",
        data=_electric_form(
            csrf, floor.id,
            [{"month": "2025-02", "amount": 10000, "welfare": 100,
              "voucher": 200, "tv_fee": 300}],
            {u.id: (0, 100) for u in occupied},
        ),
    )
    bill = ElectricBill.query.one()
    assert bill.monthly_details == [
        {"month": "2025-02", "amount": 10000, "welfare": 100,
         "voucher": 200, "tv_fee": 300}
    ]


def test_electric_vacant_units_excluded(app, client, csrf, floor, units):
    occupied = [u for u in units if not u.is_vacant]
    vacant = next(u for u in units if u.is_vacant)

    client.post(
        "/calculate/electric",
        data=_electric_form(
            csrf, floor.id, [{"month": "2025-03", "amount": 30000}],
            {u.id: (0, 100) for u in occupied},
        ),
    )
    bill = ElectricBill.query.one()
    assert vacant.id not in _by_unit(bill.id)


def test_electric_negative_usage_is_persisted(app, client, csrf, floor, units):
    """백엔드에는 음수 사용량 검증이 없다. 현재 동작을 보존한다."""
    occupied = [u for u in units if not u.is_vacant]
    readings = {
        occupied[0].id: (200, 100),   # 음수
        occupied[1].id: (0, 300),
        occupied[2].id: (0, 300),
    }
    response = client.post(
        "/calculate/electric",
        data=_electric_form(
            csrf, floor.id, [{"month": "2025-03", "amount": 30000}], readings
        ),
    )
    assert response.get_json()["success"] is True
    bill = ElectricBill.query.one()
    assert _by_unit(bill.id)[occupied[0].id].usage_amount == Decimal("-100.00")


def test_electric_duplicate_month_rejected(app, client, csrf, floor, units):
    occupied = [u for u in units if not u.is_vacant]
    response = client.post(
        "/calculate/electric",
        data=_electric_form(
            csrf, floor.id,
            [{"month": "2025-03", "amount": 1000}, {"month": "2025-03", "amount": 2000}],
            {u.id: (0, 100) for u in occupied},
        ),
    )
    payload = response.get_json()
    assert payload["success"] is False
    assert "중복" in payload["message"]
    assert ElectricBill.query.count() == 0


def test_electric_duplicate_bill_rejected_without_overwrite(app, client, csrf, floor, units):
    occupied = [u for u in units if not u.is_vacant]
    form = _electric_form(
        csrf, floor.id, [{"month": "2025-03", "amount": 30000}],
        {u.id: (0, 100) for u in occupied},
    )
    assert client.post("/calculate/electric", data=form).get_json()["success"] is True

    response = client.post("/calculate/electric", data=form)
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["exists"] is True
    assert payload["already_exists"] is True   # FE 가 확인하는 키
    assert ElectricBill.query.count() == 1


def test_electric_overwrite_replaces_bill(app, client, csrf, floor, units):
    """★ 덮어쓰기가 실제로 동작해야 한다."""
    occupied = [u for u in units if not u.is_vacant]
    first = _electric_form(
        csrf, floor.id, [{"month": "2025-03", "amount": 30000}],
        {u.id: (0, 100) for u in occupied},
    )
    client.post("/calculate/electric", data=first)
    original_id = ElectricBill.query.one().id

    second = _electric_form(
        csrf, floor.id, [{"month": "2025-03", "amount": 90000}],
        {u.id: (0, 100) for u in occupied},
        overwrite="true",
    )
    response = client.post("/calculate/electric", data=second)
    assert response.get_json()["success"] is True

    bill = ElectricBill.query.one()
    assert bill.total_amount == 90000
    assert bill.id != original_id
    assert ElectricBillMonth.query.filter_by(electric_bill_id=original_id).count() == 0
    assert ElectricBillDetail.query.filter_by(electric_bill_id=original_id).count() == 0


def test_electric_missing_month_rejected(app, client, csrf, floor, units):
    """고지월은 DATE 컬럼이 되었으므로 비워둘 수 없다."""
    occupied = [u for u in units if not u.is_vacant]
    form = _electric_form(
        csrf, floor.id, [{"month": "", "amount": 30000}],
        {u.id: (0, 100) for u in occupied},
    )
    response = client.post("/calculate/electric", data=form)
    payload = response.get_json()
    assert payload["success"] is False
    assert "고지월" in payload["message"]
    assert ElectricBill.query.count() == 0


# ---------------------------------------------------------------------------
# 수도
# ---------------------------------------------------------------------------
def test_water_allocation_by_residents(app, client, csrf, floor, units):
    response = client.post(
        "/calculate/water",
        data={
            "_csrf_token": csrf,
            "billing_month": "2025-03",
            "total_amount": "60000",
            "welfare_discount_total": "0",
            "excluded_units": "[]",
        },
    )
    assert response.get_json()["success"] is True

    bill = WaterBill.query.one()
    assert bill.total_amount == 60000
    details = {d.unit_id: d for d in bill.details}
    occupied = [u for u in units if not u.is_vacant]
    # 총 인원 = 2 + 1 + 3 = 6
    assert details[occupied[0].id].base_amount == Decimal("20000.00")   # 2/6
    assert details[occupied[1].id].base_amount == Decimal("10000.00")   # 1/6
    assert details[occupied[2].id].base_amount == Decimal("30000.00")   # 3/6
    assert details[occupied[2].id].charged_amount == 30000


def test_water_excluded_unit_gets_zero_row(app, client, csrf, floor, units):
    """★ 제외 세대도 detail 행이 생성되어야 한다 (조회 화면이 표시함)."""
    occupied = [u for u in units if not u.is_vacant]
    excluded = occupied[1]

    client.post(
        "/calculate/water",
        data={
            "_csrf_token": csrf,
            "billing_month": "2025-03",
            "total_amount": "50000",
            "excluded_units": "[{}]".format(excluded.id),
        },
    )
    bill = WaterBill.query.one()
    details = {d.unit_id: d for d in bill.details}
    assert details[excluded.id].is_excluded is True
    assert details[excluded.id].charged_amount == 0
    assert details[excluded.id].base_amount == Decimal("0.00")
    # 나머지는 남은 인원(2+3=5)으로 배분
    assert details[occupied[0].id].base_amount == Decimal("20000.00")
    assert details[occupied[2].id].base_amount == Decimal("30000.00")


def test_water_welfare_does_not_multiply_by_months(app, client, csrf, floor, units):
    """★ 전기와의 비대칭 보존: 수도 복지는 개월수를 곱하지 않는다."""
    Setting.query.filter_by(setting_key="water_welfare_amount").one().setting_value = "3000"
    db.session.commit()

    client.post(
        "/calculate/water",
        data={
            "_csrf_token": csrf,
            "billing_month": "2025-03",
            "total_amount": "60000",
            "excluded_units": "[]",
        },
    )
    bill = WaterBill.query.one()
    assert bill.welfare_discount_total == 3000   # 수도복지 1세대 × 3000
    occupied = [u for u in units if not u.is_vacant]
    details = {d.unit_id: d for d in bill.details}
    assert details[occupied[2].id].welfare_discount == Decimal("3000.00")


def test_water_all_units_excluded(app, client, csrf, floor, units):
    occupied = [u for u in units if not u.is_vacant]
    ids = [u.id for u in occupied]
    response = client.post(
        "/calculate/water",
        data={
            "_csrf_token": csrf,
            "billing_month": "2025-03",
            "total_amount": "50000",
            "excluded_units": str(ids),
        },
    )
    assert response.get_json()["success"] is True
    bill = WaterBill.query.one()
    assert all(d.is_excluded for d in bill.details)


def test_water_invalid_excluded_json_includes_everyone(app, client, csrf, floor, units):
    """현재 동작 보존: 파싱 실패 시 제외 지정이 무시된다 (침묵 실패)."""
    response = client.post(
        "/calculate/water",
        data={
            "_csrf_token": csrf,
            "billing_month": "2025-03",
            "total_amount": "60000",
            "excluded_units": "not-json",
        },
    )
    assert response.get_json()["success"] is True
    bill = WaterBill.query.one()
    assert not any(d.is_excluded for d in bill.details)


def test_water_overwrite(app, client, csrf, floor, units):
    base = {
        "_csrf_token": csrf,
        "billing_month": "2025-03",
        "total_amount": "50000",
        "excluded_units": "[]",
    }
    client.post("/calculate/water", data=base)
    response = client.post("/calculate/water", data={**base, "total_amount": "70000",
                                                     "overwrite": "true"})
    assert response.get_json()["success"] is True
    assert WaterBill.query.one().total_amount == 70000


# ---------------------------------------------------------------------------
# 공동 공과금
#
# 주의: 프론트엔드와 백엔드의 필드 계약이 어긋나 있다(분석 문서 C-1).
# 그 수정은 이번 DB 단계의 Scope 가 아니므로, 여기서는 백엔드가 기대하는
# 올바른 필드명으로 직접 호출해 배분 로직만 검증한다.
# ---------------------------------------------------------------------------
def test_common_by_residents(app, client, csrf, floor, units):
    response = client.post(
        "/calculate/common",
        data={
            "_csrf_token": csrf,
            "billing_month": "2025-03",
            "description": "인터넷",
            "total_amount": "60000",
            "distribution_method": "BY_RESIDENTS",
        },
    )
    assert response.get_json()["success"] is True

    bill = CommonBill.query.one()
    assert bill.total_amount == 60000
    details = {d.unit_id: d for d in bill.details}
    occupied = [u for u in units if not u.is_vacant]
    assert details[occupied[0].id].amount == Decimal("20000.00")
    assert details[occupied[1].id].amount == Decimal("10000.00")
    assert details[occupied[2].id].amount == Decimal("30000.00")


def test_common_by_units(app, client, csrf, floor, units):
    client.post(
        "/calculate/common",
        data={
            "_csrf_token": csrf,
            "billing_month": "2025-03",
            "description": "관리비",
            "total_amount": "30000",
            "distribution_method": "BY_UNITS",
        },
    )
    bill = CommonBill.query.one()
    for detail in bill.details:
        assert detail.amount == Decimal("10000.00")
        assert detail.charged_amount == 10000


# ---------------------------------------------------------------------------
# 정산서
# ---------------------------------------------------------------------------
def _create_invoice(client, csrf, name, items, unit_additional_data=None):
    return client.post(
        "/invoice/create",
        json={
            "_csrf_token": csrf,
            "name": name,
            "memo": "",
            "items": items,
            "unit_additional_data": unit_additional_data or {},
        },
    )


def test_invoice_sums_charged_amounts(app, client, csrf, floor, units, electric_bill):
    response = _create_invoice(
        client,
        csrf,
        "2025년 3월",
        [
            {
                "type": "ELECTRIC",
                "id": electric_bill.id,
                "month": "2025-03-01",
                "description": "3월 전기",
            }
        ],
    )
    assert response.get_json()["success"] is True

    occupied = [u for u in units if not u.is_vacant]
    invoices = {i.unit_id: i for i in FinalInvoice.query.all()}
    for unit in occupied:
        assert invoices[unit.id].electric_amount == 33340
        assert invoices[unit.id].total_amount == 33340


def test_invoice_additional_charges_persisted(app, client, csrf, floor, units, electric_bill):
    occupied = [u for u in units if not u.is_vacant]
    target = occupied[0]

    _create_invoice(
        client,
        csrf,
        "기타 항목 포함",
        [{"type": "ELECTRIC", "id": electric_bill.id, "month": "2025-03-01",
          "description": "3월 전기"}],
        {
            str(target.id): {
                "charges": [
                    {"description": "수리비", "amount": 15000, "type": "charge",
                     "is_carryover": False},
                    {"description": "보증금 일부 환급", "amount": -5000, "type": "refund",
                     "is_carryover": False},
                ],
                "memo": "세면대 수리",
            }
        },
    )

    invoice = FinalInvoice.query.filter_by(unit_id=target.id).one()
    charges = sorted(invoice.charges, key=lambda c: c.sort_order)
    assert [c.description for c in charges] == ["수리비", "보증금 일부 환급"]
    assert [c.amount for c in charges] == [15000, -5000]
    assert all(c.is_carryover is False for c in charges)
    assert invoice.total_amount == 33340 + 15000 - 5000
    assert invoice.unit_memo == "세면대 수리"


def test_invoice_created_for_all_occupied_units(app, client, csrf, floor, units, electric_bill):
    _create_invoice(
        client, csrf, "전 세대",
        [{"type": "ELECTRIC", "id": electric_bill.id, "month": "2025-03-01",
          "description": "3월 전기"}],
    )
    occupied = [u for u in units if not u.is_vacant]
    assert FinalInvoice.query.count() == len(occupied)


def test_invoice_common_details_json_preserved(app, client, csrf, floor, units):
    client.post(
        "/calculate/common",
        data={
            "_csrf_token": csrf,
            "billing_month": "2025-03",
            "description": "인터넷",
            "total_amount": "30000",
            "distribution_method": "BY_UNITS",
        },
    )
    common = CommonBill.query.one()

    _create_invoice(
        client, csrf, "공동만",
        [{"type": "COMMON", "id": common.id, "month": "2025-03-01",
          "description": "인터넷"}],
    )
    invoice = FinalInvoice.query.first()
    assert invoice.common_details == [{"description": "인터넷", "amount": 10000}]


def test_invoice_rejects_unknown_item_type(app, client, csrf, floor, units, electric_bill):
    response = _create_invoice(
        client, csrf, "잘못된 유형",
        [{"type": "GAS", "id": electric_bill.id, "month": "2025-03-01"}],
    )
    payload = response.get_json()
    assert payload["success"] is False
    assert InvoiceCombination.query.count() == 0


# ---------------------------------------------------------------------------
# 납부 / 잔액
# ---------------------------------------------------------------------------
def test_payment_crud(app, client, csrf, units, combination):
    unit = next(u for u in units if not u.is_vacant)
    created = client.post(
        "/payments/add",
        json={
            "_csrf_token": csrf,
            "combination_id": combination.id,
            "unit_id": unit.id,
            "payment_date": "2025-04-10",
            "payment_amount": 17000,
            "payment_method": "계좌이체",
            "memo": "4월 입금",
        },
    ).get_json()
    assert created["success"] is True
    payment_id = created["id"]
    assert db.session.get(Payment, payment_id).payment_amount == 17000

    client.post(
        "/payments/update/{}".format(payment_id),
        json={
            "_csrf_token": csrf,
            "payment_date": "2025-04-11",
            "payment_amount": 15000,
            "payment_method": "현금",
            "memo": "정정",
        },
    )
    db.session.expire_all()
    assert db.session.get(Payment, payment_id).payment_amount == 15000

    client.post("/payments/delete/{}".format(payment_id), json={"_csrf_token": csrf})
    assert db.session.get(Payment, payment_id) is None


def test_all_balance_endpoints_agree(app, client, csrf, units, combination):
    """★ 네 곳의 잔액 계산이 동일한 값을 반환해야 한다 (H-3 회귀 방지)."""
    unit = next(u for u in units if not u.is_vacant)
    db.session.add(
        Payment(
            combination_id=combination.id,
            unit_id=unit.id,
            payment_date=date(2025, 4, 1),
            payment_amount=5000,
        )
    )
    db.session.commit()

    single = client.get("/payments/balance/{}".format(unit.id)).get_json()
    history = client.get("/payments/unit_history/{}".format(unit.id)).get_json()
    all_units = client.get("/payments/all_units_balance").get_json()
    report = client.get("/admin/validate_balances").get_json()

    expected = 17000 - 5000
    assert single["balance"] == expected
    assert sum(h["balance"] for h in history["history"]) == expected
    assert all_units["balances"][str(unit.id)]["balance"] == expected
    row = next(r for r in report["report"] if r["unit_id"] == unit.id)
    assert row["balance"] == expected
    assert row["total_billed"] == 17000
    assert row["total_paid"] == 5000


def test_previous_readings_endpoint(app, client, floor, units, electric_bill):
    response = client.get(
        "/get_previous_readings/{}/2025-04".format(floor.id)
    ).get_json()
    assert response["success"] is True
    assert response["found"] is True
    occupied = [u for u in units if not u.is_vacant]
    assert response["readings"][str(occupied[0].id)] == 200.0


def test_previous_readings_reports_absence(app, client, floor, units):
    response = client.get(
        "/get_previous_readings/{}/2025-04".format(floor.id)
    ).get_json()
    assert response["success"] is True
    assert response["found"] is False
    assert response["readings"] == {}
