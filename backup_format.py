"""BillCalculator 전체 JSON 백업 형식과 검증/복원.

백업 가져오기는 병합하지 않는다. 현재 애플리케이션 데이터 테이블 전체를
백업의 내용으로 교체하며, 사전 검증과 실제 적용이 동일한 스키마를 사용한다.
"""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import sqlite3

from sqlalchemy import Boolean, Date, DateTime, Integer, create_engine, event, func, select, text

from db_types import ExactDecimal, Money
from extensions import db
from models import TABLE_ORDER


FORMAT_NAME = "billcalculator-full-backup"
FORMAT_VERSION = 1


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
    decoded = decode_payload(payload)
    _validate_in_temporary_database(decoded)
    return {
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


def current_counts():
    return {
        table_name: db.session.execute(
            select(func.count()).select_from(db.metadata.tables[table_name])
        ).scalar()
        for table_name in TABLE_ORDER
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
