"""실제 화면 입력과 회계 이력에서 발생했던 금액 오류 회귀 테스트."""

from datetime import date

from db_validate import (
    check_common_bill_allocations,
    check_duplicate_invoice_sources,
    check_negative_electric_usage,
    check_payment_invoice_pairs,
)
from extensions import db
from models import (
    CommonBill, FinalInvoice, InvoiceCombination, InvoiceCombinationItem, Payment,
)


def test_common_form_saves_each_nonzero_item_with_selected_distribution(
    app, client, csrf, floor, units
):
    response = client.post(
        "/calculate/common",
        data={
            "_csrf_token": csrf,
            "billing_month": "2025-03",
            "distribution_mode": "UNIT",
            "internet_amount": "10000",
            "management_amount": "5000",
            "other_amount": "0",
        },
    )
    assert response.get_json()["success"] is True
    bills = CommonBill.query.order_by(CommonBill.id).all()
    assert [(b.description, b.total_amount, b.distribution_method) for b in bills] == [
        ("인터넷", 10000, "BY_UNITS"),
        ("관리비", 5000, "BY_UNITS"),
    ]
    assert [sorted(d.charged_amount for d in b.details) for b in bills] == [
        [3340, 3340, 3340], [1670, 1670, 1670]
    ]


def test_common_form_rejects_invalid_amount_without_saving(app, client, csrf, floor, units):
    response = client.post(
        "/calculate/common",
        data={
            "_csrf_token": csrf,
            "billing_month": "2025-03",
            "distribution_mode": "UNIT",
            "internet_amount": "invalid",
            "management_amount": "5000",
        },
    )
    assert response.get_json()["success"] is False
    assert CommonBill.query.count() == 0


def test_common_form_rejects_zero_amount(app, client, csrf, floor, units):
    response = client.post(
        "/calculate/common",
        data={
            "_csrf_token": csrf,
            "billing_month": "2025-03",
            "description": "인터넷",
            "total_amount": "0",
            "distribution_method": "BY_RESIDENTS",
        },
    )
    assert response.get_json()["success"] is False
    assert CommonBill.query.count() == 0


def test_invoice_rejects_duplicate_reused_and_wrong_month_items(
    app, client, csrf, floor, units
):
    assert client.post(
        "/calculate/common",
        data={
            "_csrf_token": csrf,
            "billing_month": "2025-03",
            "description": "인터넷",
            "total_amount": "6000",
            "distribution_method": "BY_UNITS",
        },
    ).get_json()["success"] is True
    bill = CommonBill.query.one()
    item = {"type": "COMMON", "id": bill.id, "month": "2025-03", "description": "인터넷"}

    def create(name, items):
        return client.post(
            "/invoice/create",
            json={"_csrf_token": csrf, "name": name, "items": items},
        ).get_json()

    assert create("중복", [item, item])["success"] is False
    assert create("잘못된 월", [{**item, "month": "2025-04"}])["success"] is False
    assert InvoiceCombination.query.count() == 0
    assert create("정상", [item])["success"] is True
    billed_before = sum(i.common_amount for i in FinalInvoice.query.all())
    assert create("재사용", [item])["success"] is False
    assert InvoiceCombination.query.count() == 1
    assert sum(i.common_amount for i in FinalInvoice.query.all()) == billed_before == 6000


def test_vacancy_does_not_drop_prior_bill_or_hide_unpaid_balance(
    app, client, csrf, floor, units, electric_bill
):
    unit = units[0]
    unit.is_vacant = True
    db.session.commit()

    response = client.post(
        "/invoice/create",
        json={
            "_csrf_token": csrf,
            "name": "과거 전기요금",
            "items": [{
                "type": "ELECTRIC", "id": electric_bill.id,
                "month": "2025-03", "description": "3월 전기",
            }],
        },
    )
    assert response.get_json()["success"] is True
    assert FinalInvoice.query.filter_by(unit_id=unit.id).one().electric_amount == 33340
    assert client.get("/payments/balance/{}".format(unit.id)).get_json()["balance"] == 33340
    assert client.get("/payments/all_units_balance").get_json()["balances"][str(unit.id)]["balance"] == 33340
    report = client.get("/admin/validate_balances").get_json()["report"]
    assert any(row["unit_id"] == unit.id and row["balance"] == 33340 for row in report)
    assert unit.unit_name in client.get("/payments").get_data(as_text=True)


def test_payment_requires_invoice_for_that_unit(app, client, csrf, units, combination):
    vacant_unit = next(u for u in units if u.is_vacant)
    response = client.post(
        "/payments/add",
        json={
            "_csrf_token": csrf,
            "combination_id": combination.id,
            "unit_id": vacant_unit.id,
            "payment_date": date(2025, 4, 1).isoformat(),
            "payment_amount": 500,
        },
    )
    assert response.get_json()["success"] is False
    assert Payment.query.count() == 0


def test_payment_rejects_invalid_amount(app, client, csrf, units, combination):
    unit = next(u for u in units if not u.is_vacant)
    response = client.post(
        "/payments/add",
        json={
            "_csrf_token": csrf,
            "combination_id": combination.id,
            "unit_id": unit.id,
            "payment_date": date(2025, 4, 1).isoformat(),
            "payment_amount": "oops",
        },
    )
    assert response.get_json()["success"] is False
    assert Payment.query.count() == 0


def test_integrity_checks_find_preexisting_accounting_errors(
    app, units, combination, electric_bill
):
    db.session.add_all([
        InvoiceCombinationItem(
            combination_id=combination.id, item_type="ELECTRIC",
            billing_month=date(2025, 3, 1), electric_bill_id=electric_bill.id,
        ),
        InvoiceCombinationItem(
            combination_id=combination.id, item_type="ELECTRIC",
            billing_month=date(2025, 3, 1), electric_bill_id=electric_bill.id,
        ),
    ])
    vacant_unit = next(u for u in units if u.is_vacant)
    db.session.add(Payment(
        combination_id=combination.id, unit_id=vacant_unit.id,
        payment_date=date(2025, 4, 1), payment_amount=500,
    ))
    electric_bill.details[0].usage_amount = -1
    db.session.add(CommonBill(
        billing_month=date(2025, 3, 1), description="과거 0원 오류",
        total_amount=0, distribution_method="BY_UNITS",
    ))
    db.session.commit()

    assert check_duplicate_invoice_sources().ok is False
    assert check_payment_invoice_pairs().ok is False
    assert check_negative_electric_usage().ok is False
    assert check_common_bill_allocations().ok is False
