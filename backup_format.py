"""BillCalculator JSON·SQLite 백업 형식과 검증/복원.

전체 또는 안전하게 고정된 부분 묶음을 복원한다. 부분 복원도 행 단위로
병합하지 않고, 선택 묶음 전체를 백업의 같은 묶음으로 교체한다.
"""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from copy import deepcopy
import hashlib
import json
import sqlite3

from sqlalchemy import Boolean, Date, DateTime, Integer, JSON, create_engine, event, text

from db_types import ExactDecimal, Money
from extensions import db
from models import TABLE_ORDER
from settings_registry import SETTING_KEYS, get_def


FORMAT_NAME = "billcalculator-full-backup"
FORMAT_VERSION = 1
CALCULATION_SNAPSHOT_KEYS = (
    "unit_id", "floor_id", "floor_number", "floor_name", "unit_name",
    "electric_welfare", "electric_voucher", "has_tv", "water_welfare",
    "residents_count", "is_vacant",
)

RESTORE_SCOPES = {
    "full": {
        "label": "전체 데이터",
        "description": "설정부터 정산·입금까지 모두 교체",
        "tables": TABLE_ORDER,
    },
    "property_history": {
        "label": "주택 구성 + 전체 이력",
        "description": "앱 설정만 유지하고 나머지를 모두 교체",
        "tables": tuple(name for name in TABLE_ORDER if name != "settings"),
    },
    "master": {
        "label": "층·세대 구성",
        "description": "층과 세대 구성만 교체",
        "tables": ("floors", "units"),
    },
    "accounting": {
        "label": "계산·정산·입금 이력",
        "description": "설정과 층·세대는 유지하고 이력만 교체",
        "tables": tuple(
            name for name in TABLE_ORDER if name not in ("settings", "floors", "units")
        ),
    },
    "settings": {
        "label": "앱 설정",
        "description": "등록된 앱 설정 값만 교체",
        "tables": ("settings",),
    },
}


class BackupFormatError(ValueError):
    pass


def _json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def export_payload():
    tables = {}
    for table_name in TABLE_ORDER:
        table = db.metadata.tables[table_name]
        rows = db.session.execute(table.select().order_by(*table.primary_key.columns)).mappings()
        tables[table_name] = [
            {key: _json_value(value) for key, value in row.items()}
            for row in rows
        ]
    return {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "tables": tables,
    }


def _decode_value(column, value):
    if value is None:
        return None
    column_type = column.type
    if isinstance(column_type, DateTime):
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))
    if isinstance(column_type, Date):
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))
    if isinstance(column_type, Boolean):
        if type(value) is not bool:
            raise BackupFormatError("{}.{} 값은 true/false여야 합니다.".format(column.table.name, column.name))
        return value
    if isinstance(column_type, Money):
        return int(value)
    if isinstance(column_type, ExactDecimal):
        return Decimal(str(value))
    if isinstance(column_type, Integer):
        if isinstance(value, bool):
            raise BackupFormatError("{}.{} 값은 정수여야 합니다.".format(column.table.name, column.name))
        return int(value)
    if isinstance(column_type, JSON) and isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            raise BackupFormatError("{}.{} JSON 값이 올바르지 않습니다.".format(column.table.name, column.name))
    return value


def decode_payload(payload):
    errors = []
    if not isinstance(payload, dict):
        raise BackupFormatError("백업 파일의 최상위 구조가 객체가 아닙니다.")
    if payload.get("format") != FORMAT_NAME:
        errors.append("BillCalculator 전체 백업 파일이 아닙니다.")
    if payload.get("version") != FORMAT_VERSION:
        errors.append("지원하지 않는 백업 형식 버전입니다.")
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        errors.append("백업에 tables 객체가 없습니다.")
        tables = {}

    missing_tables = [name for name in TABLE_ORDER if name not in tables]
    unknown_tables = sorted(set(tables) - set(TABLE_ORDER))
    if missing_tables:
        errors.append("필수 테이블 누락: {}".format(", ".join(missing_tables)))
    if unknown_tables:
        errors.append("알 수 없는 테이블: {}".format(", ".join(unknown_tables)))
    if errors:
        raise BackupFormatError(" ".join(errors))

    decoded = {}
    for table_name in TABLE_ORDER:
        table = db.metadata.tables[table_name]
        raw_rows = tables[table_name]
        if not isinstance(raw_rows, list):
            raise BackupFormatError("{} 데이터가 배열이 아닙니다.".format(table_name))
        allowed = {column.name for column in table.columns}
        required = {
            column.name for column in table.columns
            if not column.nullable and column.default is None and column.server_default is None
        }
        seen_ids = set()
        decoded_rows = []
        for index, raw in enumerate(raw_rows):
            if not isinstance(raw, dict):
                raise BackupFormatError("{}[{}] 행이 객체가 아닙니다.".format(table_name, index))
            unknown = set(raw) - allowed
            missing = required - set(raw)
            if unknown:
                raise BackupFormatError("{}[{}] 알 수 없는 컬럼: {}".format(table_name, index, ", ".join(sorted(unknown))))
            if missing:
                raise BackupFormatError("{}[{}] 필수 컬럼 누락: {}".format(table_name, index, ", ".join(sorted(missing))))
            try:
                row = {
                    key: _decode_value(table.columns[key], value)
                    for key, value in raw.items()
                }
            except (TypeError, ValueError) as exc:
                raise BackupFormatError("{}[{}] 값 오류: {}".format(table_name, index, exc))
            identifier = row.get("id")
            if identifier is not None:
                if identifier in seen_ids:
                    raise BackupFormatError("{}의 id {}가 중복되었습니다.".format(table_name, identifier))
                seen_ids.add(identifier)
            decoded_rows.append(row)
        decoded[table_name] = decoded_rows
    return decoded


def _validate_in_temporary_database(decoded):
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    db.metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            for table_name in TABLE_ORDER:
                rows = decoded[table_name]
                if rows:
                    connection.execute(db.metadata.tables[table_name].insert(), rows)
            broken = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise BackupFormatError("백업 내부 참조 관계가 올바르지 않습니다.")
            integrity = connection.exec_driver_sql("PRAGMA integrity_check").scalar()
            if integrity != "ok":
                raise BackupFormatError("백업 데이터 무결성 검사 실패: {}".format(integrity))
    except BackupFormatError:
        raise
    except Exception as exc:
        raise BackupFormatError("백업 데이터를 현재 스키마에 적용할 수 없습니다: {}".format(exc))
    finally:
        engine.dispose()


def validate_payload(payload):
    payload = _enrich_legacy_calculation_snapshots(payload)
    payload = _enrich_legacy_registered_settings(payload)
    decoded = decode_payload(payload)
    registered = {
        row["setting_key"]: row.get("setting_value")
        for row in decoded["settings"]
        if row["setting_key"] in SETTING_KEYS
    }
    missing_settings = [key for key in SETTING_KEYS if key not in registered]
    if missing_settings:
        raise BackupFormatError(
            "백업에 필수 앱 설정이 누락되었습니다: {}".format(", ".join(missing_settings))
        )
    for key, value in registered.items():
        definition = get_def(key)
        if not isinstance(value, str):
            raise BackupFormatError("설정 {} 값은 문자열이어야 합니다.".format(key))
        if definition.kind == "money":
            try:
                amount = Decimal(value)
            except (ValueError, ArithmeticError):
                raise BackupFormatError("설정 {} 값이 올바른 금액이 아닙니다.".format(key))
            if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
                raise BackupFormatError("설정 {} 값은 0 이상의 정수 금액이어야 합니다.".format(key))
        elif definition.choices:
            try:
                definition.normalize(value)
            except ValueError as exc:
                raise BackupFormatError(str(exc))
    for table_name in (
        "electric_bill_details",
        "water_bill_details",
        "common_bill_details",
    ):
        for row in decoded[table_name]:
            snapshot = row.get("unit_snapshot")
            missing = [
                key for key in CALCULATION_SNAPSHOT_KEYS
                if not isinstance(snapshot, dict) or key not in snapshot
            ]
            if missing:
                raise BackupFormatError(
                    "{}.id={} 계산 스냅샷에 필수 값이 누락되었습니다: {}".format(
                        table_name, row.get("id"), ", ".join(missing)
                    )
                )
    _validate_in_temporary_database(decoded)
    return {
        "payload": payload,
        "decoded": decoded,
        "counts": {name: len(decoded[name]) for name in TABLE_ORDER},
        "created_at": payload.get("created_at"),
    }


def validate_sqlite_backup(path):
    """현재 스키마로 만든 SQLite 자동 백업을 전체 복원 데이터로 검증한다."""
    from db_backup import verify_backup

    path = Path(path)
    verify_backup(path)
    payload = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "created_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        "tables": {},
    }
    try:
        uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            for table_name in TABLE_ORDER:
                table = db.metadata.tables[table_name]
                existing_columns = {
                    row["name"]
                    for row in connection.execute('PRAGMA table_info("{}")'.format(table_name))
                }
                expected_columns = {column.name for column in table.columns}
                if existing_columns != expected_columns:
                    raise BackupFormatError(
                        "{} 테이블 구조가 현재 버전과 다릅니다.".format(table_name)
                    )
                primary_keys = [column.name for column in table.primary_key.columns]
                order_sql = " ORDER BY " + ", ".join('"{}"'.format(name) for name in primary_keys) if primary_keys else ""
                rows = connection.execute(
                    'SELECT * FROM "{}"{}'.format(table_name, order_sql)
                ).fetchall()
                serialized = []
                for row in rows:
                    values = {}
                    for column in table.columns:
                        value = row[column.name]
                        if value is not None and isinstance(column.type, Boolean):
                            value = bool(value)
                        values[column.name] = value
                    serialized.append(values)
                payload["tables"][table_name] = serialized
    except BackupFormatError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise BackupFormatError("자동 백업을 읽을 수 없습니다: {}".format(exc))
    return validate_payload(payload)


def payload_digest(payload):
    """백업 데이터 본문만의 안정적인 해시를 반환한다."""
    canonical = {
        "format": payload.get("format") if isinstance(payload, dict) else None,
        "version": payload.get("version") if isinstance(payload, dict) else None,
        "tables": payload.get("tables") if isinstance(payload, dict) else None,
    }
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _scope(scope):
    if scope not in RESTORE_SCOPES:
        raise BackupFormatError("지원하지 않는 복원 범위입니다.")
    return RESTORE_SCOPES[scope]


def _rows_by_id(payload, table_name):
    return {row["id"]: row for row in payload["tables"][table_name]}


def _referenced_master_ids(payload):
    """이력이 참조하는 세대·층 ID를 모은다."""
    tables = payload["tables"]
    unit_ids = set()
    for table_name in (
        "electric_readings",
        "electric_bill_details",
        "water_bill_details",
        "common_bill_details",
        "final_invoices",
        "payments",
    ):
        unit_ids.update(row["unit_id"] for row in tables[table_name])
    floor_ids = {row["floor_id"] for row in tables["electric_bills"]}
    units = _rows_by_id(payload, "units")
    floor_ids.update(units[unit_id]["floor_id"] for unit_id in unit_ids if unit_id in units)
    return unit_ids, floor_ids


def _structural_identity(row, table_name):
    if table_name == "units":
        return (row["id"], row["floor_id"], row["unit_name"])
    return (row["id"], row["floor_number"], row.get("name"))


def _assert_structural_matches(reference_payload, candidate_payload, unit_ids, floor_ids, message):
    for table_name, identifiers in (("units", unit_ids), ("floors", floor_ids)):
        reference = _rows_by_id(reference_payload, table_name)
        candidate = _rows_by_id(candidate_payload, table_name)
        for identifier in sorted(identifiers):
            if identifier not in reference or identifier not in candidate:
                raise BackupFormatError(
                    message.format(table=table_name, id=identifier, reason="누락")
                )
            if _structural_identity(reference[identifier], table_name) != _structural_identity(
                candidate[identifier], table_name
            ):
                raise BackupFormatError(
                    message.format(table=table_name, id=identifier, reason="식별 정보 불일치")
                )


def _replace_registered_settings(current_rows, backup_rows):
    backup = {row["setting_key"]: row for row in backup_rows if row["setting_key"] in SETTING_KEYS}
    missing = [key for key in SETTING_KEYS if key not in backup]
    if missing:
        raise BackupFormatError(
            "설정 부분 복원에 필요한 키가 누락되었습니다: {}".format(", ".join(missing))
        )
    result = deepcopy(current_rows)
    current = {row["setting_key"]: row for row in result}
    if any(key not in current for key in SETTING_KEYS):
        raise BackupFormatError("현재 DB의 등록 설정이 완전하지 않아 부분 복원할 수 없습니다.")
    for key in SETTING_KEYS:
        current[key]["setting_value"] = backup[key]["setting_value"]
    return result


def _enrich_legacy_registered_settings(payload):
    """새 설정 도입 전에 내려받은 v1 백업에 안전한 기본값을 보강한다.

    ``restore_default_if_missing`` 으로 명시한 신규 설정에만 적용한다. 기존부터
    필수였던 설정이 사라진 손상 백업은 ``validate_payload`` 가 계속 거절한다.
    """
    enriched = deepcopy(payload)
    tables = enriched.get("tables", {}) if isinstance(enriched, dict) else {}
    rows = tables.get("settings") if isinstance(tables, dict) else None
    if not isinstance(rows, list):
        return enriched

    existing = {
        row.get("setting_key")
        for row in rows
        if isinstance(row, dict)
    }
    identifiers = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            identifiers.append(int(row.get("id")))
        except (TypeError, ValueError):
            continue
    next_id = max(identifiers or [0]) + 1
    now = datetime.now().isoformat(timespec="seconds")
    for key in SETTING_KEYS:
        definition = get_def(key)
        if key in existing or not definition.restore_default_if_missing:
            continue
        rows.append({
            "id": next_id,
            "setting_key": key,
            "setting_value": definition.default,
            "created_at": now,
            "updated_at": now,
        })
        next_id += 1
    return enriched


def _enrich_legacy_calculation_snapshots(payload):
    """
    0003 이전에 내려받은 v1 백업에 세대·층 식별 스냅샷을 보강한다.

    기존 계산 속성은 손대지 않고, 레거시 화면이 원래 보여주던 백업 내의
    마스터 식별자만 이관 시점 값으로 고정한다.
    """
    enriched = deepcopy(payload)
    tables = enriched.get("tables", {}) if isinstance(enriched, dict) else {}
    if not isinstance(tables, dict):
        return enriched
    raw_units = tables.get("units", [])
    raw_floors = tables.get("floors", [])
    raw_units = raw_units if isinstance(raw_units, list) else []
    raw_floors = raw_floors if isinstance(raw_floors, list) else []
    units = {row.get("id"): row for row in raw_units if isinstance(row, dict)}
    floors = {row.get("id"): row for row in raw_floors if isinstance(row, dict)}
    for table_name in (
        "electric_bill_details",
        "water_bill_details",
        "common_bill_details",
    ):
        rows = tables.get(table_name, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            snapshot = row.get("unit_snapshot")
            if isinstance(snapshot, str):
                try:
                    snapshot = json.loads(snapshot)
                except ValueError:
                    continue
            if not isinstance(snapshot, dict):
                continue
            unit = units.get(row.get("unit_id"))
            floor = floors.get(unit.get("floor_id")) if unit else None
            if not unit or not floor:
                continue
            snapshot = dict(snapshot)
            snapshot.setdefault("unit_id", unit["id"])
            snapshot.setdefault("floor_id", floor["id"])
            snapshot.setdefault("floor_number", floor["floor_number"])
            snapshot.setdefault("floor_name", floor.get("name") or "")
            row["unit_snapshot"] = snapshot
    return enriched


def compose_restore_payload(backup_payload, scope, current_payload=None):
    """
    선택 묶음은 백업으로 완전 교체하고 나머지는 현재 값을 보존한
    하나의 완전한 목표 DB 페이로드를 만든다. 행 단위 병합은 하지 않는다.
    """
    definition = _scope(scope)
    backup_payload = validate_payload(backup_payload)["payload"]
    current_payload = validate_payload(current_payload or export_payload())["payload"]

    if scope == "master":
        unit_ids, floor_ids = _referenced_master_ids(current_payload)
        _assert_structural_matches(
            current_payload,
            backup_payload,
            unit_ids,
            floor_ids,
            "현재 계산·정산·입금 이력이 참조하는 {table} id={id}이(가) "
            "백업에서 {reason}입니다. 이력과 수신자를 다른 세대로 연결할 수 없어 복원을 중단했습니다.",
        )
    elif scope == "accounting":
        unit_ids, floor_ids = _referenced_master_ids(backup_payload)
        _assert_structural_matches(
            backup_payload,
            current_payload,
            unit_ids,
            floor_ids,
            "백업 이력이 참조하는 {table} id={id}의 {reason}으로 현재 주택 구성에 "
            "안전하게 연결할 수 없어 복원을 중단했습니다.",
        )

    composed = deepcopy(current_payload)
    composed["created_at"] = backup_payload.get("created_at")
    if scope == "settings":
        composed["tables"]["settings"] = _replace_registered_settings(
            current_payload["tables"]["settings"], backup_payload["tables"]["settings"]
        )
    else:
        for table_name in definition["tables"]:
            composed["tables"][table_name] = deepcopy(backup_payload["tables"][table_name])
    return composed


def prepare_restore(backup_payload, scope="full"):
    """복원 미리보기와 실제 적용이 공유하는 단일 준비 경로."""
    definition = _scope(scope)
    current_payload = export_payload()
    composed = compose_restore_payload(backup_payload, scope, current_payload)
    validated = validate_payload(composed)
    current_table_counts = {
        name: len(current_payload["tables"][name]) for name in TABLE_ORDER
    }
    target_counts = validated["counts"]
    warnings = []
    selected = definition["tables"]
    if (
        scope != "settings"
        and sum(target_counts[name] for name in selected) == 0
        and sum(current_table_counts[name] for name in selected) > 0
    ):
        warnings.append(
            "선택한 복원 묶음의 백업 데이터가 비어 있어, 현재 데이터가 모두 삭제됩니다."
        )
    return {
        "scope": scope,
        "scope_label": definition["label"],
        "affected_tables": list(selected),
        "warnings": warnings,
        "current_counts": current_table_counts,
        "target_counts": target_counts,
        "backup_created_at": backup_payload.get("created_at"),
        "backup_digest": payload_digest(backup_payload),
        "current_digest": payload_digest(current_payload),
        "validated": validated,
    }


def replace_all(decoded):
    """현재 데이터를 전체 삭제한 뒤 백업 데이터로 교체한다.

    호출자는 commit/rollback과 적용 직전 네이티브 DB 백업을 담당한다.
    """
    for table_name in reversed(TABLE_ORDER):
        db.session.execute(db.metadata.tables[table_name].delete())
    for table_name in TABLE_ORDER:
        rows = decoded[table_name]
        if rows:
            db.session.execute(db.metadata.tables[table_name].insert(), rows)
    db.session.flush()
    broken = db.session.execute(text("PRAGMA foreign_key_check")).fetchall()
    if broken:
        raise BackupFormatError("복원 후 외래키 검사에서 {}건의 오류가 발견되었습니다.".format(len(broken)))
