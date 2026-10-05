"""T-G. 설정 레지스트리 및 비파괴적 Import."""

from datetime import date
from decimal import Decimal

import pytest

import app as app_module
from extensions import db
from models import (
    ElectricBill,
    ElectricBillDetail,
    FinalInvoice,
    Floor,
    Payment,
    Setting,
    Unit,
)
from settings_registry import SETTING_KEYS, get_def


# ---------------------------------------------------------------------------
# 레지스트리
# ---------------------------------------------------------------------------
def test_all_registered_settings_are_seeded(app):
    """★ 기존에는 시딩 목록에서 3개 키가 누락되어 있었다."""
    stored = {s.setting_key for s in Setting.query.all()}
    assert stored == set(SETTING_KEYS)
    assert len(SETTING_KEYS) == 9


def test_seed_settings_is_idempotent(app):
    Setting.query.filter_by(setting_key="tv_fee").one().setting_value = "9999"
    db.session.commit()

    created = app_module.seed_settings()
    assert created == []
    assert app_module.get_setting("tv_fee") == "9999"


def test_unknown_setting_key_raises(app):
    with pytest.raises(KeyError):
        app_module.get_setting("does_not_exist")
    with pytest.raises(KeyError):
        app_module.set_setting("does_not_exist", "1")


def test_money_setting_is_normalized(app):
    app_module.set_setting("tv_fee", "3,000")
    db.session.commit()
    assert app_module.get_setting("tv_fee") == "3000"
    assert get_def("tv_fee").parse("3000") == 3000


def test_long_text_setting_survives(app):
    """VARCHAR(255) → TEXT 전환 검증 (기존에는 잠재적 절단 버그)."""
    long_memo = "가" * 1000
    app_module.set_setting("invoice_default_memo", long_memo)
    db.session.commit()
    db.session.expire_all()
    assert app_module.get_setting("invoice_default_memo") == long_memo


def test_save_settings_route_only_touches_submitted_keys(app, client, csrf):
    app_module.set_setting("water_customer_number", "12345-67890")
    db.session.commit()

    client.post(
        "/settings/save",
        data={"_csrf_token": csrf, "tv_fee": "4000"},
    )
    db.session.expire_all()
    assert app_module.get_setting("tv_fee") == "4000"
    assert app_module.get_setting("water_customer_number") == "12345-67890"


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------
def test_export_contains_all_settings_and_floors(app, client, floor, units):
    payload = client.get("/settings/export").get_json()
    assert set(payload["settings"]) == set(SETTING_KEYS)
    assert len(payload["floors"]) == 1
    assert len(payload["floors"][0]["units"]) == 4


@pytest.fixture()
def accounting_state(app, client, csrf, floor, units, electric_bill, combination):
    """계산 + 정산 + 납부 데이터가 존재하는 상태."""
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
    return {
        "electric_bills": ElectricBill.query.count(),
        "electric_details": ElectricBillDetail.query.count(),
        "final_invoices": FinalInvoice.query.count(),
        "payments": Payment.query.count(),
        "unit_ids": sorted(u.id for u in Unit.query.all()),
    }


def test_import_does_not_delete_accounting_data(app, client, csrf, accounting_state):
    """★ 가장 중요한 회귀 방지: Import 가 과거 데이터를 삭제하면 안 된다."""
    payload = {
        "_csrf_token": csrf,
        "settings": {"tv_fee": "2500"},
        "floors": [
            {
                "floor_number": 1,
                "name": "1층 (갱신)",
                "units": [{"unit_name": "101호", "residents_count": 5}],
            }
        ],
    }
    response = client.post("/settings/import", json=payload)
    assert response.get_json()["success"] is True

    assert ElectricBill.query.count() == accounting_state["electric_bills"]
    assert ElectricBillDetail.query.count() == accounting_state["electric_details"]
    assert FinalInvoice.query.count() == accounting_state["final_invoices"]
    assert Payment.query.count() == accounting_state["payments"]


def test_import_preserves_unit_ids(app, client, csrf, accounting_state):
    """세대 id 가 유지되어야 과거 계산의 FK 참조가 유효하다."""
    client.post(
        "/settings/import",
        json={
            "_csrf_token": csrf,
            "settings": {},
            "floors": [
                {
                    "floor_number": 1,
                    "name": "1층",
                    "units": [{"unit_name": "101호", "residents_count": 7}],
                }
            ],
        },
    )
    assert sorted(u.id for u in Unit.query.all()) == accounting_state["unit_ids"]


def test_import_updates_existing_unit(app, client, csrf, floor, units):
    client.post(
        "/settings/import",
        json={
            "_csrf_token": csrf,
            "settings": {},
            "floors": [
                {
                    "floor_number": 1,
                    "name": "1층",
                    "units": [
                        {"unit_name": "101호", "residents_count": 9, "has_tv": False}
                    ],
                }
            ],
        },
    )
    db.session.expire_all()
    unit = Unit.query.filter_by(unit_name="101호").one()
    assert unit.residents_count == 9
    assert unit.has_tv is False


def test_partial_import_preserves_unspecified_unit_and_floor_fields(
    app, client, csrf, floor, units
):
    floor.electric_contract_number = "C-1"
    unit = units[0]
    unit.electric_voucher = True
    unit.has_tv = False
    unit.is_vacant = True
    unit.memo = "기존 메모"
    db.session.commit()

    response = client.post(
        "/settings/import",
        json={
            "_csrf_token": csrf,
            "floors": [{
                "floor_number": floor.floor_number,
                "units": [{"unit_name": unit.unit_name, "residents_count": 9}],
            }],
        },
    )
    assert response.get_json()["success"] is True
    db.session.expire_all()
    assert unit.residents_count == 9
    assert unit.electric_welfare is True
    assert unit.electric_voucher is True
    assert unit.has_tv is False
    assert unit.is_vacant is True
    assert unit.memo == "기존 메모"
    assert floor.electric_contract_number == "C-1"


def test_import_rejects_string_boolean(app, client, csrf, floor, units):
    response = client.post(
        "/settings/import",
        json={
            "_csrf_token": csrf,
            "floors": [{
                "floor_number": floor.floor_number,
                "units": [{"unit_name": units[0].unit_name, "is_vacant": "false"}],
            }],
        },
    )
    assert response.get_json()["success"] is False
    assert units[0].is_vacant is False


def test_fractional_resident_count_is_rejected(app, client, csrf, floor, units):
    original_count = units[0].residents_count
    response = client.post(
        "/units/{}/update".format(units[0].id),
        data={
            "_csrf_token": csrf,
            "unit_name": units[0].unit_name,
            "residents_count": "2.5",
        },
    )
    assert response.get_json()["success"] is False
    db.session.expire_all()
    assert units[0].residents_count == original_count

    response = client.post(
        "/settings/import",
        json={
            "_csrf_token": csrf,
            "floors": [{
                "floor_number": floor.floor_number,
                "units": [{"unit_name": units[0].unit_name, "residents_count": 2.5}],
            }],
        },
    )
    assert response.get_json()["success"] is False
    assert units[0].residents_count == original_count


def test_import_adds_new_floor_and_unit(app, client, csrf, floor, units):
    client.post(
        "/settings/import",
        json={
            "_csrf_token": csrf,
            "settings": {},
            "floors": [
                {
                    "floor_number": -1,
                    "name": "B1층",
                    "units": [{"unit_name": "B101호", "residents_count": 1}],
                }
            ],
        },
    )
    assert Floor.query.filter_by(floor_number=-1).count() == 1
    assert Unit.query.filter_by(unit_name="B101호").count() == 1


def test_import_keeps_units_absent_from_payload(app, client, csrf, floor, units):
    """payload 에 없는 기존 세대는 삭제되지 않는다."""
    before = Unit.query.count()
    client.post(
        "/settings/import",
        json={
            "_csrf_token": csrf,
            "settings": {},
            "floors": [
                {
                    "floor_number": 1,
                    "name": "1층",
                    "units": [{"unit_name": "101호", "residents_count": 2}],
                }
            ],
        },
    )
    assert Unit.query.count() == before


def test_import_rejects_invalid_payload_without_touching_db(app, client, csrf, floor, units):
    before_floors = Floor.query.count()
    before_units = Unit.query.count()

    response = client.post(
        "/settings/import",
        json={
            "_csrf_token": csrf,
            "settings": {"unknown_key": "x"},
            "floors": [{"floor_number": "not-a-number", "units": []}],
        },
    )
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["errors"]
    assert Floor.query.count() == before_floors
    assert Unit.query.count() == before_units


def test_import_rejects_duplicate_unit_names(app, client, csrf):
    response = client.post(
        "/settings/import",
        json={
            "_csrf_token": csrf,
            "settings": {},
            "floors": [
                {
                    "floor_number": 3,
                    "units": [{"unit_name": "301호"}, {"unit_name": "301호"}],
                }
            ],
        },
    )
    payload = response.get_json()
    assert payload["success"] is False
    assert any("중복" in e for e in payload["errors"])


def test_import_rejects_negative_residents(app, client, csrf):
    response = client.post(
        "/settings/import",
        json={
            "_csrf_token": csrf,
            "settings": {},
            "floors": [
                {"floor_number": 4, "units": [{"unit_name": "401호", "residents_count": -1}]}
            ],
        },
    )
    assert response.get_json()["success"] is False
    assert Floor.query.filter_by(floor_number=4).count() == 0


def test_export_import_round_trip(app, client, csrf, floor, units):
    exported = client.get("/settings/export").get_json()
    exported["_csrf_token"] = csrf

    response = client.post("/settings/import", json=exported)
    assert response.get_json()["success"] is True

    db.session.expire_all()
    assert Unit.query.count() == 4
    assert Unit.query.filter_by(unit_name="103호").one().residents_count == 3
