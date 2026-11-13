"""설정 레지스트리와 전체 백업 복원 정책."""

from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

import app as app_module
from extensions import db
from models import (
    CommonBill,
    CommonBillDetail,
    ElectricBill,
    Floor,
    InvoiceCombination,
    Payment,
    Setting,
    Unit,
)
from settings_registry import SETTING_KEYS, get_def


def test_all_registered_settings_are_seeded(app):
    stored = {row.setting_key for row in Setting.query.all()}
    assert stored == set(SETTING_KEYS)
    assert len(SETTING_KEYS) == 11


def test_seed_settings_is_idempotent(app):
    Setting.query.filter_by(setting_key="tv_fee").one().setting_value = "9999"
    db.session.commit()
    assert app_module.seed_settings() == []
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


def test_calculation_policy_settings_reject_unknown_values(app):
    with pytest.raises(ValueError):
        app_module.set_setting("discount_tv_policy", "UNKNOWN")
    with pytest.raises(ValueError):
        app_module.set_setting("tv_distribution_default", "UNKNOWN")


def test_long_text_setting_survives(app):
    long_memo = "가" * 1000
    app_module.set_setting("invoice_default_memo", long_memo)
    db.session.commit()
    db.session.expire_all()
    assert app_module.get_setting("invoice_default_memo") == long_memo


def test_save_settings_route_only_touches_submitted_keys(app, client, csrf):
    app_module.set_setting("water_customer_number", "12345-67890")
    db.session.commit()
    client.post("/settings/save", data={"_csrf_token": csrf, "tv_fee": "4000"})
    db.session.expire_all()
    assert app_module.get_setting("tv_fee") == "4000"
    assert app_module.get_setting("water_customer_number") == "12345-67890"


def test_tv_distribution_default_controls_calculator_initial_selection(
    app, client, csrf
):
    app_module.set_setting("tv_distribution_default", "INDIVIDUAL")
    db.session.commit()
    html = client.get("/calculator").get_data(as_text=True)
    assert 'value="INDIVIDUAL" selected' in html
    assert 'value="EQUAL" selected' not in html


def test_shared_modal_controls_are_available_on_every_page(app):
    source = Path(app_module.app.static_folder, "js", "app.js").read_text()
    assert "[data-open-modal]" in source
    assert "[data-close-modal]" in source


def _export(client):
    response = client.get("/settings/export")
    assert response.status_code == 200
    return response.get_json()


def _preview(client, csrf, payload, scope="full"):
    request_payload = deepcopy(payload)
    request_payload["_csrf_token"] = csrf
    request_payload["restore_scope"] = scope
    return client.post("/settings/import/preview", json=request_payload).get_json()


def _restore(client, csrf, payload, scope="full"):
    preview = _preview(client, csrf, payload, scope)
    assert preview["success"] is True, preview
    request_payload = deepcopy(payload)
    request_payload["_csrf_token"] = csrf
    request_payload["restore_scope"] = scope
    request_payload["preview_token"] = preview["preview_token"]
    request_payload["confirm_selected_replace"] = True
    return client.post("/settings/import", json=request_payload).get_json()


def _create_common_bill(client, csrf):
    response = client.post(
        "/calculate/common",
        data={
            "_csrf_token": csrf,
            "billing_month": "2025-03",
            "description": "공동 청소비",
            "total_amount": "30000",
            "distribution_method": "BY_RESIDENTS",
        },
    )
    assert response.status_code in (200, 302)
    assert CommonBill.query.count() == 1


def test_export_is_complete_versioned_backup(app, client, floor, units):
    payload = _export(client)
    assert payload["format"] == "billcalculator-full-backup"
    assert payload["version"] == 1
    assert set(payload["tables"]["settings"][0]) >= {"id", "setting_key", "setting_value"}
    assert len(payload["tables"]["floors"]) == 1
    assert len(payload["tables"]["units"]) == 4
    assert set(row["setting_key"] for row in payload["tables"]["settings"]) == set(SETTING_KEYS)


def test_legacy_v1_backup_gets_new_policy_defaults(
    app, client, csrf, floor, units, electric_bill
):
    payload = _export(client)
    payload["tables"]["settings"] = [
        row for row in payload["tables"]["settings"]
        if row["setting_key"] not in {"discount_tv_policy", "tv_distribution_default"}
    ]
    for row in payload["tables"]["electric_bills"]:
        row.pop("discount_tv_policy", None)

    result = _restore(client, csrf, payload)
    assert result["success"] is True, result
    db.session.expire_all()
    assert app_module.get_setting("discount_tv_policy") == "COMBINED_FLOOR_ZERO"
    assert app_module.get_setting("tv_distribution_default") == "EQUAL"
    assert ElectricBill.query.one().discount_tv_policy == "COMBINED_FLOOR_ZERO"


def test_legacy_v1_backup_supports_settings_only_restore(app, client, csrf):
    app_module.set_setting("discount_tv_policy", "TV_PROTECTED")
    app_module.set_setting("tv_distribution_default", "INDIVIDUAL")
    db.session.commit()
    payload = _export(client)
    payload["tables"]["settings"] = [
        row for row in payload["tables"]["settings"]
        if row["setting_key"] not in {"discount_tv_policy", "tv_distribution_default"}
    ]

    result = _restore(client, csrf, payload, "settings")
    assert result["success"] is True, result
    db.session.expire_all()
    assert app_module.get_setting("discount_tv_policy") == "COMBINED_FLOOR_ZERO"
    assert app_module.get_setting("tv_distribution_default") == "EQUAL"


def test_backup_rejects_unknown_calculation_policy(app, client, csrf):
    payload = _export(client)
    next(
        row for row in payload["tables"]["settings"]
        if row["setting_key"] == "discount_tv_policy"
    )["setting_value"] = "UNKNOWN"

    result = _preview(client, csrf, payload)
    assert result["success"] is False
    assert "올바르지 않습니다" in result["message"]


def test_preview_validates_without_changing_current_data(app, client, csrf, floor, units):
    payload = _export(client)
    payload["tables"]["floors"][0]["name"] = "백업 속 이름"
    result = _preview(client, csrf, payload)
    assert result["success"] is True
    assert result["current_counts"]["units"] == 4
    assert result["backup_counts"]["units"] == 4
    db.session.expire_all()
    assert db.session.get(Floor, floor.id).name != "백업 속 이름"


def test_restore_replaces_all_data_instead_of_merging(app, client, csrf, floor, units):
    payload = _export(client)
    kept_unit = units[0]
    payload["tables"]["units"] = [row for row in payload["tables"]["units"] if row["id"] == kept_unit.id]
    payload["tables"]["floors"][0]["name"] = "복원된 1층"

    result = _restore(client, csrf, payload)
    assert result["success"] is True, result
    db.session.expire_all()
    assert Unit.query.count() == 1
    assert Unit.query.one().unit_name == kept_unit.unit_name
    assert Floor.query.one().name == "복원된 1층"
    assert Path(result["automatic_backup"]).exists()


def test_recent_automatic_backup_can_be_previewed_and_fully_restored(
    app, client, csrf, floor, units
):
    original_name = units[0].unit_name
    backup_path = app_module._create_safety_backup("before_unit_delete")

    units[0].unit_name = "백업 후 변경"
    db.session.commit()

    listed = client.get("/settings/automatic-backups").get_json()
    assert listed["success"] is True
    assert backup_path.name in {item["filename"] for item in listed["backups"]}

    preview = client.post(
        "/settings/automatic-backups/preview",
        json={"_csrf_token": csrf, "filename": backup_path.name},
    ).get_json()
    assert preview["success"] is True
    assert preview["backup_counts"]["units"] == 4
    assert Unit.query.filter_by(unit_name="백업 후 변경").count() == 1

    restored = client.post(
        "/settings/automatic-backups/restore",
        json={
            "_csrf_token": csrf,
            "filename": backup_path.name,
            "restore_scope": "full",
            "preview_token": preview["preview_token"],
            "confirm_selected_replace": True,
        },
    ).get_json()
    assert restored["success"] is True, restored
    db.session.expire_all()
    assert Unit.query.filter_by(unit_name=original_name).count() == 1
    assert Unit.query.filter_by(unit_name="백업 후 변경").count() == 0


def test_automatic_backup_restore_rejects_path_traversal(app, client, csrf):
    result = client.post(
        "/settings/automatic-backups/preview",
        json={"_csrf_token": csrf, "filename": "../test.db"},
    ).get_json()
    assert result["success"] is False


def test_restore_requires_explicit_full_replace_confirmation(app, client, csrf, floor, units):
    payload = _export(client)
    payload["_csrf_token"] = csrf
    result = client.post("/settings/import", json=payload).get_json()
    assert result["success"] is False
    assert "선택 범위 교체" in result["message"]
    assert Unit.query.count() == 4


def test_partial_or_wrong_format_backup_is_rejected(app, client, csrf, floor, units):
    result = _preview(client, csrf, {"settings": {}, "floors": []})
    assert result["success"] is False
    assert Unit.query.count() == 4


def test_backup_reference_error_is_rejected_before_restore(app, client, csrf, floor, units):
    payload = _export(client)
    payload["tables"]["units"][0]["floor_id"] = 999999
    result = _preview(client, csrf, payload)
    assert result["success"] is False
    assert Unit.query.count() == 4


def test_backup_boolean_type_is_strict(app, client, csrf, floor, units):
    payload = _export(client)
    payload["tables"]["units"][0]["is_vacant"] = "false"
    result = _preview(client, csrf, payload)
    assert result["success"] is False
    assert units[0].is_vacant is False


def test_export_restore_round_trip(app, client, csrf, floor, units):
    payload = _export(client)
    result = _restore(client, csrf, payload)
    assert result["success"] is True, result
    db.session.expire_all()
    assert Unit.query.count() == 4
    assert Unit.query.filter_by(unit_name="103호").one().residents_count == 3


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


def test_master_restore_replaces_only_floor_and_unit_profiles(
    app, client, csrf, floor, units
):
    _create_common_bill(client, csrf)
    payload = _export(client)
    target = next(row for row in payload["tables"]["units"] if row["id"] == units[0].id)
    target["residents_count"] = 17
    target["memo"] = "백업 프로필"

    result = _restore(client, csrf, payload, "master")
    assert result["success"] is True, result
    db.session.expire_all()
    assert db.session.get(Unit, units[0].id).residents_count == 17
    assert db.session.get(Unit, units[0].id).memo == "백업 프로필"
    assert CommonBill.query.count() == 1


def test_master_restore_blocks_structural_change_for_calculation_reference(
    app, client, csrf, floor, units
):
    _create_common_bill(client, csrf)
    payload = _export(client)
    target = next(row for row in payload["tables"]["units"] if row["id"] == units[0].id)
    target["unit_name"] = "다른 세대"

    result = _preview(client, csrf, payload, "master")
    assert result["success"] is False
    assert "참조" in result["message"]
    assert db.session.get(Unit, units[0].id).unit_name == "101호"


def test_master_restore_blocks_structural_change_for_unlinked_payment_reference(
    app, client, csrf, floor, units
):
    combination = InvoiceCombination(invoice_name="기존 정산")
    db.session.add(combination)
    db.session.flush()
    db.session.add(Payment(
        combination_id=combination.id,
        unit_id=units[0].id,
        payment_date=date(2025, 3, 1),
        payment_amount=10000,
        payment_method="계좌이체",
    ))
    db.session.commit()
    payload = _export(client)
    next(row for row in payload["tables"]["units"] if row["id"] == units[0].id)[
        "unit_name"
    ] = "다른 수납자"

    result = _preview(client, csrf, payload, "master")
    assert result["success"] is False
    assert "참조" in result["message"]


def test_master_restore_may_remove_only_unreferenced_unit(app, client, csrf, floor, units):
    _create_common_bill(client, csrf)
    payload = _export(client)
    unreferenced = next(unit for unit in units if unit.is_vacant)
    unreferenced_id = unreferenced.id
    payload["tables"]["units"] = [
        row for row in payload["tables"]["units"] if row["id"] != unreferenced_id
    ]

    result = _restore(client, csrf, payload, "master")
    assert result["success"] is True, result
    assert db.session.get(Unit, unreferenced_id) is None
    assert CommonBill.query.count() == 1


def test_accounting_restore_replaces_history_and_keeps_master(
    app, client, csrf, floor, units
):
    empty_history = _export(client)
    _create_common_bill(client, csrf)

    preview = _preview(client, csrf, empty_history, "accounting")
    assert preview["success"] is True
    assert preview["warnings"]
    result = _restore(client, csrf, empty_history, "accounting")
    assert result["success"] is True, result
    assert CommonBill.query.count() == 0
    assert Unit.query.count() == 4


def test_accounting_restore_blocks_mismatched_master_fingerprint(
    app, client, csrf, floor, units
):
    _create_common_bill(client, csrf)
    payload = _export(client)
    next(row for row in payload["tables"]["units"] if row["id"] == units[0].id)[
        "unit_name"
    ] = "백업의 다른 세대"

    result = _preview(client, csrf, payload, "accounting")
    assert result["success"] is False
    assert "안전하게 연결" in result["message"]


def test_settings_restore_changes_registered_values_only(app, client, csrf, floor, units):
    internal = Setting(setting_key="__demo_dataset__", setting_value="current-marker")
    db.session.add(internal)
    db.session.commit()
    payload = _export(client)
    next(
        row for row in payload["tables"]["settings"] if row["setting_key"] == "tv_fee"
    )["setting_value"] = "9876"
    next(
        row
        for row in payload["tables"]["settings"]
        if row["setting_key"] == "__demo_dataset__"
    )["setting_value"] = "backup-marker"

    result = _restore(client, csrf, payload, "settings")
    assert result["success"] is True, result
    db.session.expire_all()
    assert app_module.get_setting("tv_fee") == "9876"
    assert Setting.query.filter_by(setting_key="__demo_dataset__").one().setting_value == "current-marker"
    assert Unit.query.count() == 4


def test_property_history_restore_preserves_current_settings(
    app, client, csrf, floor, units
):
    payload = _export(client)
    next(
        row for row in payload["tables"]["settings"] if row["setting_key"] == "tv_fee"
    )["setting_value"] = "9999"
    payload["tables"]["units"][0]["residents_count"] = 23
    app_module.set_setting("tv_fee", "4321")
    db.session.commit()

    result = _restore(client, csrf, payload, "property_history")
    assert result["success"] is True, result
    db.session.expire_all()
    assert app_module.get_setting("tv_fee") == "4321"
    assert db.session.get(Unit, payload["tables"]["units"][0]["id"]).residents_count == 23


def test_restore_rejects_stale_preview_after_current_data_changes(
    app, client, csrf, floor, units
):
    payload = _export(client)
    preview = _preview(client, csrf, payload, "full")
    app_module.set_setting("tv_fee", "7777")
    db.session.commit()
    request_payload = deepcopy(payload)
    request_payload.update({
        "_csrf_token": csrf,
        "restore_scope": "full",
        "preview_token": preview["preview_token"],
        "confirm_selected_replace": True,
    })

    result = client.post("/settings/import", json=request_payload).get_json()
    assert result["success"] is False
    assert "현재 데이터가 변경" in result["message"]
    assert app_module.get_setting("tv_fee") == "7777"


def test_restore_rejects_changed_backup_after_preview(app, client, csrf, floor, units):
    payload = _export(client)
    preview = _preview(client, csrf, payload, "settings")
    next(
        row for row in payload["tables"]["settings"] if row["setting_key"] == "tv_fee"
    )["setting_value"] = "8888"
    payload.update({
        "_csrf_token": csrf,
        "restore_scope": "settings",
        "preview_token": preview["preview_token"],
        "confirm_selected_replace": True,
    })

    result = client.post("/settings/import", json=payload).get_json()
    assert result["success"] is False
    assert "백업 파일" in result["message"]


def test_restore_rejects_unknown_scope(app, client, csrf, floor, units):
    result = _preview(client, csrf, _export(client), "payments_only")
    assert result["success"] is False
    assert "지원하지 않는" in result["message"]


def test_v1_backup_with_legacy_calculation_snapshot_is_enriched(
    app, client, csrf, floor, units
):
    _create_common_bill(client, csrf)
    payload = _export(client)
    for row in payload["tables"]["common_bill_details"]:
        for key in ("unit_id", "floor_id", "floor_number", "floor_name"):
            row["unit_snapshot"].pop(key)

    result = _restore(client, csrf, payload, "full")
    assert result["success"] is True, result
    snapshot = CommonBillDetail.query.first().unit_snapshot
    assert snapshot["unit_id"] in {unit.id for unit in units}
    assert snapshot["floor_id"] == floor.id
    assert snapshot["floor_number"] == floor.floor_number
    assert snapshot["floor_name"] == floor.name


def test_automatic_sqlite_backup_with_json_snapshots_can_be_previewed(
    app, client, csrf, floor, units
):
    _create_common_bill(client, csrf)
    backup_path = app_module._create_safety_backup("before_common_delete")

    preview = client.post(
        "/settings/automatic-backups/preview",
        json={
            "_csrf_token": csrf,
            "filename": backup_path.name,
            "restore_scope": "accounting",
        },
    ).get_json()
    assert preview["success"] is True, preview
    assert preview["target_counts"]["common_bill_details"] > 0


def test_automatic_backup_supports_settings_only_restore(
    app, client, csrf, floor, units
):
    app_module.set_setting("tv_fee", "3000")
    db.session.commit()
    backup_path = app_module._create_safety_backup("before_partial_restore")
    app_module.set_setting("tv_fee", "7000")
    units[0].residents_count = 41
    db.session.commit()

    preview = client.post(
        "/settings/automatic-backups/preview",
        json={
            "_csrf_token": csrf,
            "filename": backup_path.name,
            "restore_scope": "settings",
        },
    ).get_json()
    assert preview["success"] is True, preview
    result = client.post(
        "/settings/automatic-backups/restore",
        json={
            "_csrf_token": csrf,
            "filename": backup_path.name,
            "restore_scope": "settings",
            "preview_token": preview["preview_token"],
            "confirm_selected_replace": True,
        },
    ).get_json()
    assert result["success"] is True, result
    db.session.expire_all()
    assert app_module.get_setting("tv_fee") == "3000"
    assert db.session.get(Unit, units[0].id).residents_count == 41
