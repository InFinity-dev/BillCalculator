"""골든 마스터용 대표 데이터셋.

계산 분기를 가능한 한 모두 통과하도록 구성한다.

- 3개 층 (지하 1층 포함 → 음수 floor_number)
- 재실 6 / 공실 2
- 복지 2, 바우처 1, TV 미보유 1, 수도복지 1
- 전기: 단일월 정산 1건 + 3개월 묶음 1건, TV INDIVIDUAL / EQUAL 각 1건
- 수도: 제외 세대 1개 포함
- 공동: BY_RESIDENTS 1건 + BY_UNITS 1건
- 정산서 2건 (기타 항목 부과/환급, 이월 항목 포함)
- 납부: 완납 / 미납 / 초과납부

데이터는 전부 **HTTP 라우트를 통해** 만든다. 계산 경로 전체를 통과시키기 위함이다.
"""

from datetime import date

import balance as balance_service
from extensions import db
from models import (
    CommonBill,
    CommonBillDetail,
    ElectricBill,
    ElectricBillDetail,
    ElectricBillMonth,
    FinalInvoice,
    FinalInvoiceCharge,
    Floor,
    InvoiceCombination,
    Payment,
    Unit,
)

FLOOR_SPECS = [
    {"floor_number": -1, "name": "B1층", "contract": "C-B1"},
    {"floor_number": 1, "name": "1층", "contract": "C-01"},
    {"floor_number": 2, "name": "2층", "contract": None},
]

UNIT_SPECS = [
    # (floor_number, unit_name, residents, has_tv, e_welfare, e_voucher, w_welfare, vacant)
    (-1, "B101호", 1, True, False, False, False, False),
    (-1, "B102호", 2, False, False, True, False, False),
    (1, "101호", 2, True, True, False, False, False),
    (1, "102호", 3, True, False, False, True, False),
    (1, "103호", 1, True, False, False, False, True),
    (2, "201호", 4, True, True, False, False, False),
    (2, "202호", 2, True, False, False, False, False),
    (2, "203호", 1, True, False, False, False, True),
]


def build(client, csrf):
    """데이터셋을 생성한다. 생성된 주요 객체를 dict 로 반환한다."""
    floors = _create_floors(client, csrf)
    units = _create_units(client, csrf, floors)

    electric_single = _calc_electric_single(client, csrf, floors, units)
    electric_bundle = _calc_electric_bundle(client, csrf, floors, units)
    water = _calc_water(client, csrf, units)
    common_residents, common_units = _calc_common(client, csrf)

    first = _create_invoice_one(client, csrf, electric_single, water, common_residents)
    _create_payments(client, csrf, first, units)
    second = _create_invoice_two(client, csrf, electric_bundle, common_units, units)

    return {
        "floors": floors,
        "units": units,
        "electric_single": electric_single,
        "electric_bundle": electric_bundle,
        "water": water,
        "invoices": [first, second],
    }


def _create_floors(client, csrf):
    for spec in FLOOR_SPECS:
        client.post(
            "/floors/add",
            data={
                "_csrf_token": csrf,
                "floor_number": str(spec["floor_number"]),
                "name": spec["name"],
                "electric_contract_number": spec["contract"] or "",
            },
        )
    return {f.floor_number: f for f in Floor.query.all()}


def _create_units(client, csrf, floors):
    for (number, name, residents, has_tv, ew, ev, ww, vacant) in UNIT_SPECS:
        client.post(
            "/units/add",
            data={
                "_csrf_token": csrf,
                "floor_id": str(floors[number].id),
                "unit_name": name,
                "memo": "",
                "residents_count": str(residents),
                "has_tv": "true" if has_tv else "false",
                "electric_welfare": "true" if ew else "false",
                "electric_voucher": "true" if ev else "false",
                "water_welfare": "true" if ww else "false",
                "is_vacant": "true" if vacant else "false",
            },
        )
    return {u.unit_name: u for u in Unit.query.all()}


def _electric_form(csrf, floor_id, month, rows, readings, mode):
    data = {
        "_csrf_token": csrf,
        "billing_month": month,
        "floor_id": str(floor_id),
        "tv_distribution_mode": mode,
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
    return data


def _calc_electric_single(client, csrf, floors, units):
    """1층, 단일 고지월, TV INDIVIDUAL, 복지 할인 입력값 경로."""
    floor = floors[1]
    readings = {
        units["101호"].id: ("1200.50", "1450.75"),
        units["102호"].id: ("980.00", "1310.25"),
    }
    client.post(
        "/calculate/electric",
        data=_electric_form(
            csrf,
            floor.id,
            "2025-03",
            [{"month": "2025-03", "amount": 143000, "welfare": 7000}],
            readings,
            "INDIVIDUAL",
        ),
    )
    return ElectricBill.query.filter_by(floor_id=floor.id).one()


def _calc_electric_bundle(client, csrf, floors, units):
    """2층, 3개월 묶음, TV EQUAL, 바우처 할인."""
    floor = floors[2]
    readings = {
        units["201호"].id: ("500.00", "1120.40"),
        units["202호"].id: ("310.25", "690.75"),
    }
    client.post(
        "/calculate/electric",
        data=_electric_form(
            csrf,
            floor.id,
            "2025-03",
            [
                {"month": "2025-01", "amount": 88000, "voucher": 3000, "tv_fee": 5000},
                {"month": "2025-02", "amount": 91500, "tv_fee": 5000},
                {"month": "2025-03", "amount": 104200, "tv_fee": 5000},
            ],
            readings,
            "EQUAL",
        ),
    )
    return ElectricBill.query.filter_by(floor_id=floor.id).one()


def _calc_water(client, csrf, units):
    """제외 세대 1개 포함."""
    from models import WaterBill

    client.post(
        "/calculate/water",
        data={
            "_csrf_token": csrf,
            "billing_month": "2025-03",
            "total_amount": "187300",
            "welfare_discount_total": "0",
            "excluded_units": "[{}]".format(units["B102호"].id),
        },
    )
    return WaterBill.query.one()


def _calc_common(client, csrf):
    client.post(
        "/calculate/common",
        data={
            "_csrf_token": csrf,
            "billing_month": "2025-03",
            "description": "인터넷",
            "total_amount": "55000",
            "distribution_method": "BY_RESIDENTS",
        },
    )
    client.post(
        "/calculate/common",
        data={
            "_csrf_token": csrf,
            "billing_month": "2025-03",
            "description": "계단 청소비",
            "total_amount": "48000",
            "distribution_method": "BY_UNITS",
        },
    )
    bills = CommonBill.query.order_by(CommonBill.id).all()
    return bills[0], bills[1]


def _create_invoice_one(client, csrf, electric_single, water_bill, common_residents):
    client.post(
        "/invoice/create",
        json={
            "_csrf_token": csrf,
            "name": "2025년 3월 정산 (1차)",
            "memo": "3월 정산입니다.",
            "items": [
                {"type": "ELECTRIC", "id": electric_single.id,
                 "month": "2025-03-01", "description": "1층 3월 전기"},
                {"type": "WATER", "id": water_bill.id,
                 "month": "2025-03-01", "description": "3월 수도"},
                {"type": "COMMON", "id": common_residents.id,
                 "month": "2025-03-01", "description": "인터넷"},
            ],
            "unit_additional_data": {},
        },
    )
    return InvoiceCombination.query.order_by(InvoiceCombination.id).all()[0]


def _create_payments(client, csrf, combination, units):
    """완납 / 미납 / 초과납부 세 가지 상태를 만든다."""
    invoices = {
        inv.unit_id: inv
        for inv in FinalInvoice.query.filter_by(combination_id=combination.id).all()
    }
    plans = [
        ("101호", "full"),
        ("102호", "partial"),
        ("201호", "over"),
    ]
    for name, kind in plans:
        unit = units[name]
        invoice = invoices[unit.id]
        if kind == "full":
            amount = invoice.total_amount
        elif kind == "partial":
            amount = invoice.total_amount // 2
        else:
            amount = invoice.total_amount + 10000
        client.post(
            "/payments/add",
            json={
                "_csrf_token": csrf,
                "combination_id": combination.id,
                "unit_id": unit.id,
                "payment_date": "2025-04-05",
                "payment_amount": amount,
                "payment_method": "계좌이체",
                "memo": kind,
            },
        )


def _create_invoice_two(client, csrf, electric_bundle, common_units, units):
    """기타 항목(부과/환급) + 이월 항목을 포함한 2차 정산."""
    balances = balance_service.all_unit_balances()
    additional = {}

    unpaid_unit = units["102호"]
    unpaid_balance = balances[unpaid_unit.id]["balance"]
    additional[str(unpaid_unit.id)] = {
        "charges": [
            {"description": "[이월] 전월 미납금", "amount": unpaid_balance,
             "type": "charge", "is_carryover": True},
            {"description": "현관 수리비", "amount": 22000,
             "type": "charge", "is_carryover": False},
        ],
        "memo": "현관문 손잡이 교체",
    }

    over_unit = units["201호"]
    over_balance = balances[over_unit.id]["balance"]
    additional[str(over_unit.id)] = {
        "charges": [
            {"description": "[이월] 전월 초과납부 환급", "amount": over_balance,
             "type": "refund", "is_carryover": True},
        ],
        "memo": "",
    }

    normal_unit = units["202호"]
    additional[str(normal_unit.id)] = {
        "charges": [
            {"description": "보증금 일부 환급", "amount": -15000,
             "type": "refund", "is_carryover": False},
        ],
        "memo": "",
    }

    client.post(
        "/invoice/create",
        json={
            "_csrf_token": csrf,
            "name": "2025년 3월 정산 (2차)",
            "memo": "",
            "items": [
                {"type": "ELECTRIC", "id": electric_bundle.id,
                 "month": "2025-03-01", "description": "2층 1~3월 전기"},
                {"type": "COMMON", "id": common_units.id,
                 "month": "2025-03-01", "description": "계단 청소비"},
            ],
            "unit_additional_data": additional,
        },
    )
    return InvoiceCombination.query.order_by(InvoiceCombination.id).all()[1]


# ---------------------------------------------------------------------------
# 결과 스냅샷
# ---------------------------------------------------------------------------
def _key(unit_id, units_by_id):
    return units_by_id[unit_id].unit_name


def snapshot():
    """비교 가능한 형태로 계산 결과 전체를 직렬화한다.

    id 는 실행마다 달라질 수 있으므로 **자연키(세대명/정산월/정산서명)** 로 기록한다.
    """
    units_by_id = {u.id: u for u in Unit.query.all()}
    bills_by_id = {b.id: b for b in ElectricBill.query.all()}
    combos_by_id = {c.id: c for c in InvoiceCombination.query.all()}

    electric = sorted(
        (
            {
                "floor": bills_by_id[d.electric_bill_id].floor_ref.name,
                "billing_month": bills_by_id[d.electric_bill_id].billing_month.isoformat(),
                "unit": _key(d.unit_id, units_by_id),
                "usage_amount": str(d.usage_amount),
                "base_amount": str(d.base_amount),
                "welfare_discount": str(d.welfare_discount),
                "voucher_discount": str(d.voucher_discount),
                "tv_fee": str(d.tv_fee),
                "final_amount": str(d.final_amount),
                "charged_amount": d.charged_amount,
            }
            for d in ElectricBillDetail.query.all()
        ),
        key=lambda r: (r["floor"], r["billing_month"], r["unit"]),
    )

    from models import WaterBillDetail

    water = sorted(
        (
            {
                "unit": _key(d.unit_id, units_by_id),
                "base_amount": str(d.base_amount),
                "welfare_discount": str(d.welfare_discount),
                "final_amount": str(d.final_amount),
                "charged_amount": d.charged_amount,
                "is_excluded": bool(d.is_excluded),
            }
            for d in WaterBillDetail.query.all()
        ),
        key=lambda r: r["unit"],
    )

    common_bills = {b.id: b for b in CommonBill.query.all()}
    common = sorted(
        (
            {
                "description": common_bills[d.common_bill_id].description,
                "method": common_bills[d.common_bill_id].distribution_method,
                "unit": _key(d.unit_id, units_by_id),
                "amount": str(d.amount),
                "charged_amount": d.charged_amount,
            }
            for d in CommonBillDetail.query.all()
        ),
        key=lambda r: (r["description"], r["unit"]),
    )

    invoices = sorted(
        (
            {
                "invoice": combos_by_id[i.combination_id].invoice_name,
                "unit": _key(i.unit_id, units_by_id),
                "electric_amount": i.electric_amount,
                "water_amount": i.water_amount,
                "common_amount": i.common_amount,
                "total_amount": i.total_amount,
                "charges": [
                    {
                        "description": c.description,
                        "amount": c.amount,
                        "is_carryover": bool(c.is_carryover),
                        "sort_order": c.sort_order,
                    }
                    for c in sorted(i.charges, key=lambda c: c.sort_order)
                ],
            }
            for i in FinalInvoice.query.all()
        ),
        key=lambda r: (r["invoice"], r["unit"]),
    )

    months = sorted(
        (
            {
                "floor": bills_by_id[m.electric_bill_id].floor_ref.name,
                "billing_month": m.billing_month.isoformat(),
                "amount": m.amount,
                "welfare_discount": m.welfare_discount,
                "voucher_discount": m.voucher_discount,
                "tv_fee": m.tv_fee,
            }
            for m in ElectricBillMonth.query.all()
        ),
        key=lambda r: (r["floor"], r["billing_month"]),
    )

    balances = balance_service.all_unit_balances()
    balance_by_name = {
        units_by_id[uid].unit_name: info["balance"] for uid, info in balances.items()
    }

    aggregates = {
        "sum_electric_charged": sum(d.charged_amount for d in ElectricBillDetail.query.all()),
        "sum_water_charged": sum(d.charged_amount for d in WaterBillDetail.query.all()),
        "sum_common_charged": sum(d.charged_amount for d in CommonBillDetail.query.all()),
        "sum_final_invoice_total": sum(i.total_amount for i in FinalInvoice.query.all()),
        "sum_payments": sum(p.payment_amount for p in Payment.query.all()),
        "sum_charges": sum(c.amount for c in FinalInvoiceCharge.query.all()),
    }

    return {
        "electric_bill_months": months,
        "electric_bill_details": electric,
        "water_bill_details": water,
        "common_bill_details": common,
        "final_invoices": invoices,
        "balances": dict(sorted(balance_by_name.items())),
        "aggregates": aggregates,
    }
