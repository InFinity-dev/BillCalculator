"""월별 정산·입금 흐름을 통과하는 재현 가능한 화면 테스트 데이터."""

import json
from datetime import date
from decimal import Decimal

import balance
from extensions import db
from models import CommonBill, ElectricBill, FinalInvoice, Floor, InvoiceCombination, Setting, Unit, WaterBill

DEMO_VERSION = 1
METADATA_KEY = "__demo_dataset__"

# (층, 세대명, 인원, TV, 전기 복지, 바우처, 수도 복지, 공실)
UNIT_SPECS = (
    (-1, "B101호", 1, True, False, False, False, False),
    (-1, "B102호", 2, False, False, True, False, False),
    (1, "101호", 2, True, True, False, False, False),
    (1, "102호", 3, True, False, False, True, False),
    (1, "103호", 1, True, False, False, False, True),
    (2, "201호", 4, True, True, False, False, False),
    (2, "202호", 2, True, False, True, False, False),
    (2, "203호", 1, False, False, False, False, True),
)


def month_after(start, offset):
    number = start.year * 12 + start.month - 1 + offset
    year, month = divmod(number, 12)
    return date(year, month + 1, 1)


def _post(client, csrf, route, *, data=None, payload=None):
    if payload is not None:
        response = client.post(route, json=dict(payload, _csrf_token=csrf))
    else:
        response = client.post(route, data=dict(data, _csrf_token=csrf))
    result = response.get_json(silent=True)
    if response.status_code != 200 or not result or not result.get("success"):
        message = (result or {}).get("message", "HTTP {}".format(response.status_code))
        raise RuntimeError("더미 데이터 생성 실패 ({}): {}".format(route, message))


def _set_vacancy(client, csrf, unit, vacant):
    data = {key: str(bool(getattr(unit, key))).lower() for key in (
        "has_tv", "electric_welfare", "electric_voucher", "water_welfare"
    )}
    data.update(unit_name=unit.unit_name, residents_count=str(unit.residents_count),
                is_vacant=str(vacant).lower(), memo="[테스트] 입주·퇴거 이력")
    _post(client, csrf, "/units/{}/update".format(unit.id), data=data)


def _electric(client, csrf, month, index, floors, readings):
    items = []
    for floor in floors.values():
        occupied = Unit.query.filter_by(floor_id=floor.id, is_vacant=False).order_by(Unit.id).all()
        data = {
            "billing_month": month, "floor_id": str(floor.id), "month_count": "1",
            "tv_distribution_mode": "EQUAL" if index % 2 else "INDIVIDUAL",
            "bill_month_0": month,
            "bill_amount_0": str(85000 + index * 3500 + (floor.floor_number + 1) * 12000),
            "bill_welfare_0": str(sum(u.electric_welfare for u in occupied) * 3000),
            "bill_voucher_0": str(sum(u.electric_voucher for u in occupied) * 2000),
            "bill_tv_fee_0": str(sum(u.has_tv for u in occupied) * 2500),
        }
        for unit in occupied:
            previous = readings[unit.id]
            current = previous + Decimal(110 + index * 7 + unit.id * 13)
            data["prev_{}".format(unit.id)] = str(previous)
            data["curr_{}".format(unit.id)] = str(current)
            readings[unit.id] = current
        _post(client, csrf, "/calculate/electric", data=data)
        bill = ElectricBill.query.filter_by(floor_id=floor.id, billing_month=date.fromisoformat(month + "-01")).one()
        items.append({"type": "ELECTRIC", "id": bill.id, "month": month,
                      "description": "{} {} 전기".format(floor.name, month)})
    return items


def _additional(units, index):
    result = {}
    for unit in units.values():
        if unit.is_vacant:
            continue
        charges = []
        previous = balance.unit_balance(unit.id)["balance"]
        if previous:
            charges.append({"description": "전월 미납 이월" if previous > 0 else "전월 초과납부 이월",
                            "amount": previous, "is_carryover": True})
        if unit.unit_name == "102호" and index == 1:
            charges.append({"description": "현관 수리비", "amount": 22000, "is_carryover": False})
        if unit.unit_name == "101호" and index == 2:
            charges.append({"description": "수리비 환급", "amount": -15000, "is_carryover": False})
        if charges:
            result[str(unit.id)] = {"charges": charges, "memo": "[테스트] 이월·기타 항목"}
    return result


def build_demo(application, start, months):
    """빈 DB에만 생성한다. 앱 컨텍스트와 스키마 준비는 호출자가 담당한다."""
    if Floor.query.count() or InvoiceCombination.query.count():
        raise RuntimeError("더미 데이터는 빈 테스트 DB에만 생성할 수 있습니다.")
    csrf = "demo-seed-csrf"
    with application.test_client() as client:
        with client.session_transaction() as session:
            session["_csrf_token"] = csrf
        _post(client, csrf, "/settings/import", payload={"settings": {
            "water_welfare_amount": "1500",
            "water_customer_number": "DEMO-WATER-001",
            "invoice_default_memo": "테스트용 청구서입니다.",
            "invoice_footer": "이 고지서는 더미 데이터이며 실제 납부 대상이 아닙니다.",
        }})
        for number, label in ((-1, "지하 1층"), (1, "1층"), (2, "2층")):
            _post(client, csrf, "/floors/add", data={"floor_number": str(number), "name": label,
                                                     "electric_contract_number": "DEMO-{}".format(number)})
        floors = {floor.floor_number: floor for floor in Floor.query.all()}
        for number, name, residents, tv, welfare, voucher, water, vacant in UNIT_SPECS:
            _post(client, csrf, "/units/add", data={
                "floor_id": str(floors[number].id), "unit_name": name,
                "residents_count": str(residents), "memo": "[테스트] 더미 세대",
                "has_tv": str(tv).lower(), "electric_welfare": str(welfare).lower(),
                "electric_voucher": str(voucher).lower(), "water_welfare": str(water).lower(),
                "is_vacant": str(vacant).lower(),
            })
        units = {unit.unit_name: unit for unit in Unit.query.all()}
        readings = {unit.id: Decimal(500 + unit.id * 100) for unit in units.values()}
        for index in range(months):
            current = month_after(start, index)
            month = current.strftime("%Y-%m")
            if index == 2:
                _set_vacancy(client, csrf, units["202호"], True)
            if index == 3:
                _set_vacancy(client, csrf, units["203호"], False)
            items = _electric(client, csrf, month, index, floors, readings)
            _post(client, csrf, "/calculate/water", data={
                "billing_month": month, "total_amount": str(165000 + index * 4500),
                "welfare_discount_total": "1500",
                "excluded_units": json.dumps([units["B102호"].id] if index % 2 else []),
            })
            water = WaterBill.query.filter_by(billing_month=current).one()
            items.append({"type": "WATER", "id": water.id, "month": month, "description": month + " 수도"})
            for label, amount, method in (("인터넷", 55000, "BY_RESIDENTS"),
                                          ("관리비", 48000, "BY_UNITS"),
                                          ("공용 소모품", 12000 + index * 1000, "BY_UNITS")):
                _post(client, csrf, "/calculate/common", data={
                    "billing_month": month, "description": label, "total_amount": str(amount),
                    "distribution_method": method,
                })
                common = CommonBill.query.filter_by(billing_month=current, description=label).one()
                items.append({"type": "COMMON", "id": common.id, "month": month, "description": label})
            _post(client, csrf, "/invoice/create", payload={
                "name": "[테스트] {} 월 정산".format(month), "memo": "월별 흐름 검증용 더미 데이터",
                "items": items, "unit_additional_data": _additional(units, index),
            })
            combination = InvoiceCombination.query.order_by(InvoiceCombination.id.desc()).first()
            payment_date = month_after(current, 1).replace(day=5).isoformat()
            for invoice in FinalInvoice.query.filter_by(combination_id=combination.id).all():
                name = db.session.get(Unit, invoice.unit_id).unit_name
                if name == "202호" or (name == "B102호" and index % 3 == 0):
                    continue
                if name == "201호":
                    amount, memo = max(0, invoice.total_amount) + 150000, "초과납부"
                elif name in ("102호", "B102호"):
                    amount, memo = max(0, invoice.total_amount // 2), "부분납부"
                else:
                    amount, memo = max(0, invoice.total_amount), "완납"
                if amount:
                    _post(client, csrf, "/payments/add", payload={
                        "combination_id": combination.id, "unit_id": invoice.unit_id,
                        "payment_date": payment_date, "payment_amount": amount,
                        "payment_method": "계좌이체", "memo": "[테스트] " + memo,
                    })
    metadata = {"version": DEMO_VERSION, "start_month": start.strftime("%Y-%m"), "months": months}
    db.session.add(Setting(setting_key=METADATA_KEY, setting_value=json.dumps(metadata)))
    db.session.commit()
    return metadata
