"""설정 레지스트리와 전체 백업 복원 정책."""

from copy import deepcopy
from pathlib import Path

import pytest

import app as app_module
from extensions import db
from models import Floor, Setting, Unit
from settings_registry import SETTING_KEYS, get_def


def test_all_registered_settings_are_seeded(app):
    stored = {row.setting_key for row in Setting.query.all()}
    assert stored == set(SETTING_KEYS)
    assert len(SETTING_KEYS) == 9


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


def _export(client):
    response = client.get("/settings/export")
    assert response.status_code == 200
    return response.get_json()


def _preview(client, csrf, payload):
    request_payload = deepcopy(payload)
    request_payload["_csrf_token"] = csrf
    return client.post("/settings/import/preview", json=request_payload).get_json()


def _restore(client, csrf, payload):
    request_payload = deepcopy(payload)
    request_payload["_csrf_token"] = csrf
    request_payload["confirm_full_replace"] = True
    return client.post("/settings/import", json=request_payload).get_json()


def test_export_is_complete_versioned_backup(app, client, floor, units):
    payload = _export(client)
    assert payload["format"] == "billcalculator-full-backup"
    assert payload["version"] == 1
    assert set(payload["tables"]["settings"][0]) >= {"id", "setting_key", "setting_value"}
    assert len(payload["tables"]["floors"]) == 1
    assert len(payload["tables"]["units"]) == 4
    assert set(row["setting_key"] for row in payload["tables"]["settings"]) == set(SETTING_KEYS)


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
            "confirm_full_replace": True,
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
    assert "전체 교체" in result["message"]
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
