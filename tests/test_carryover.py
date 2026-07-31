"""T-K. 이월 불변식.

핵심 불변식 (변경 금지)::

    이월 금액은 FinalInvoice.total_amount 에는 포함된다   (청구서에 찍힌다)
    이월 금액은 balance 계산의 청구액에는 포함되지 않는다  (이중계상 방지)

이번 리팩토링은 **판정 수단**만 description 문자열 → is_carryover 컬럼으로 바꿨다.
결과 숫자는 동일해야 한다.
"""

import re
from datetime import date
from pathlib import Path

import pytest

import balance as balance_service
from extensions import db
from models import FinalInvoice, FinalInvoiceCharge, Payment

REPO_ROOT = Path(__file__).resolve().parent.parent
LEGACY_KEYWORDS = ("미납", "초과납부", "환급", "이월")


def _add_charge(invoice, description, amount, is_carryover):
    charge = FinalInvoiceCharge(
        final_invoice_id=invoice.id,
        description=description,
        amount=amount,
        is_carryover=is_carryover,
    )
    db.session.add(charge)
    db.session.commit()
    db.session.expire(invoice)
    return charge


def test_carryover_included_in_invoice_total(app, client, csrf, units, floor, electric_bill):
    """이월 항목은 청구서 총액에 포함된다."""
    unit = next(u for u in units if not u.is_vacant)
    response = client.post(
        "/invoice/create",
        json={
            "_csrf_token": csrf,
            "name": "이월 포함",
            "memo": "",
            "items": [
                {"type": "ELECTRIC", "id": electric_bill.id, "month": "2025-03-01",
                 "description": "3월 전기"}
            ],
            "unit_additional_data": {
                str(unit.id): {
                    "charges": [
                        {"description": "[이월] 전월 미납금", "amount": 8000,
                         "type": "charge", "is_carryover": True}
                    ],
                    "memo": "",
                }
            },
        },
    )
    assert response.get_json()["success"] is True

    invoice = FinalInvoice.query.filter_by(unit_id=unit.id).one()
    assert invoice.total_amount == 33340 + 8000
    assert invoice.charges[0].is_carryover is True


def test_carryover_excluded_from_balance(app, units, combination):
    """이월 항목은 잔액 계산의 청구액에서 제외된다."""
    unit = next(u for u in units if not u.is_vacant)
    invoice = FinalInvoice.query.filter_by(unit_id=unit.id).one()
    _add_charge(invoice, "[이월] 전월 미납금", 8000, True)

    result = balance_service.unit_balance(unit.id)
    # electric 10000 + water 5000 + common 2000 = 17000 (이월 8000 제외)
    assert result["total_billed"] == 17000
    assert result["balance"] == 17000


def test_non_carryover_included_in_balance(app, units, combination):
    unit = next(u for u in units if not u.is_vacant)
    invoice = FinalInvoice.query.filter_by(unit_id=unit.id).one()
    _add_charge(invoice, "수리비", 8000, False)

    result = balance_service.unit_balance(unit.id)
    assert result["total_billed"] == 17000 + 8000


def test_keyword_in_description_does_not_affect_balance(app, units, combination):
    """★ 문자열 판정 제거 증명.

    description 에 '미납' 이 들어 있어도 is_carryover=False 면 잔액에 포함된다.
    레거시 구현이었다면 조용히 제외되었을 케이스다.
    """
    unit = next(u for u in units if not u.is_vacant)
    invoice = FinalInvoice.query.filter_by(unit_id=unit.id).one()
    _add_charge(invoice, "관리비 미납분 청구", 12000, False)

    result = balance_service.unit_balance(unit.id)
    assert result["total_billed"] == 17000 + 12000


def test_plain_description_with_carryover_flag_is_excluded(app, units, combination):
    """★ 역방향: 평범한 이름이어도 is_carryover=True 면 제외된다."""
    unit = next(u for u in units if not u.is_vacant)
    invoice = FinalInvoice.query.filter_by(unit_id=unit.id).one()
    _add_charge(invoice, "전월 잔액 정산", 9000, True)

    result = balance_service.unit_balance(unit.id)
    assert result["total_billed"] == 17000


def test_refund_carryover(app, units, combination):
    """초과납부 환급 이월도 잔액에서 제외된다."""
    unit = next(u for u in units if not u.is_vacant)
    invoice = FinalInvoice.query.filter_by(unit_id=unit.id).one()
    _add_charge(invoice, "[이월] 전월 초과납부 환급", -3000, True)

    db.session.expire_all()
    invoice = FinalInvoice.query.filter_by(unit_id=unit.id).one()
    assert invoice.carryover_charges_total == -3000
    assert balance_service.unit_balance(unit.id)["total_billed"] == 17000


def test_carryover_flag_survives_round_trip(app, client, csrf, units, floor, electric_bill):
    """프론트엔드가 보낸 is_carryover 가 DB 까지 보존되어야 한다."""
    unit = next(u for u in units if not u.is_vacant)
    client.post(
        "/invoice/create",
        json={
            "_csrf_token": csrf,
            "name": "플래그 보존",
            "memo": "",
            "items": [
                {"type": "ELECTRIC", "id": electric_bill.id, "month": "2025-03-01",
                 "description": "3월 전기"}
            ],
            "unit_additional_data": {
                str(unit.id): {
                    "charges": [
                        {"description": "A", "amount": 1000, "is_carryover": True},
                        {"description": "B", "amount": 2000, "is_carryover": False},
                    ],
                    "memo": "",
                }
            },
        },
    )
    invoice = FinalInvoice.query.filter_by(unit_id=unit.id).one()
    by_desc = {c.description: c.is_carryover for c in invoice.charges}
    assert by_desc == {"A": True, "B": False}


def test_balance_matches_legacy_algorithm(app, units, combination):
    """★ 레거시 문자열 알고리즘과 신규 컬럼 알고리즘의 결과가 같아야 한다.

    이월 항목의 이름을 레거시 규칙대로 지었을 때, 두 방식이 같은 잔액을 낸다.
    """
    unit = next(u for u in units if not u.is_vacant)
    invoice = FinalInvoice.query.filter_by(unit_id=unit.id).one()
    _add_charge(invoice, "[이월] 전월 미납금", 8000, True)
    _add_charge(invoice, "수리비", 5000, False)
    db.session.add(
        Payment(
            combination_id=combination.id,
            unit_id=unit.id,
            payment_date=date(2025, 4, 1),
            payment_amount=10000,
        )
    )
    db.session.commit()

    def legacy_balance():
        total = 0
        for inv in FinalInvoice.query.filter_by(unit_id=unit.id).all():
            total += inv.electric_amount + inv.water_amount + inv.common_amount
            for charge in inv.charges:
                if not any(k in charge.description.lower() for k in LEGACY_KEYWORDS):
                    total += charge.amount
        paid = sum(
            p.payment_amount for p in Payment.query.filter_by(unit_id=unit.id).all()
        )
        return total - paid

    assert balance_service.unit_balance(unit.id)["balance"] == legacy_balance()


def test_application_code_has_no_carryover_string_matching():
    """★ 애플리케이션 코드에 이월 키워드 판정이 남아 있으면 안 된다.

    허용되는 곳:
      - scripts/migrate_mysql_to_sqlite.py (레거시 데이터 1회 변환)
      - db_validate.py (사후 검토용 정보성 리포트)
    """
    checked = ["app.py", "balance.py", "models.py", "settings_registry.py"]
    for name in checked:
        source = (REPO_ROOT / name).read_text(encoding="utf-8")
        for keyword in LEGACY_KEYWORDS:
            pattern = r"['\"][^'\"]*{}[^'\"]*['\"]\s*(?:,|\)|\])".format(re.escape(keyword))
            hits = [
                line
                for line in source.splitlines()
                if re.search(pattern, line) and "in desc" in line
            ]
            assert not hits, "{} 에 이월 문자열 판정이 남아 있습니다: {}".format(name, hits)


def test_no_legacy_keyword_list_in_app(app):
    """이월 키워드 튜플이 app.py / balance.py 에 존재하지 않아야 한다."""
    for name in ("app.py", "balance.py"):
        source = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "'초과납부'" not in source
        assert '"초과납부"' not in source
