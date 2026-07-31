"""T-S. 스키마와 SQLite PRAGMA."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from extensions import db, read_pragma

REPO_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_TABLES = {
    "alembic_version",
    "settings",
    "floors",
    "units",
    "electric_bills",
    "electric_bill_months",
    "electric_readings",
    "electric_bill_details",
    "water_bills",
    "water_bill_details",
    "common_bills",
    "common_bill_details",
    "invoice_combinations",
    "invoice_combination_items",
    "final_invoices",
    "final_invoice_charges",
    "payments",
}


def _table_names():
    rows = db.session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table'")
    ).fetchall()
    return {r[0] for r in rows}


def test_migration_creates_all_tables(app):
    assert EXPECTED_TABLES.issubset(_table_names())


def test_foreign_keys_pragma_is_on(app):
    """SQLite 는 기본이 OFF 다. 훅이 커넥션마다 켜 주어야 한다."""
    assert read_pragma("foreign_keys") == 1


def test_foreign_keys_pragma_on_fresh_connection(app):
    """풀에서 커넥션을 새로 얻어도 FK 가 켜져 있어야 한다."""
    engine = db.engine
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_journal_mode_is_wal(app):
    assert read_pragma("journal_mode").lower() == "wal"


def test_busy_timeout_configured(app):
    assert read_pragma("busy_timeout") >= 5000


def test_synchronous_is_full(app):
    # 2 == FULL
    assert read_pragma("synchronous") == 2


def test_integrity_check_ok(app):
    assert read_pragma("integrity_check") == "ok"


def test_foreign_key_check_is_empty(app):
    rows = db.session.execute(text("PRAGMA foreign_key_check")).fetchall()
    assert rows == []


@pytest.mark.parametrize(
    "table,column",
    [
        ("electric_bills", "tv_units_count"),
        ("electric_bills", "monthly_details"),
        ("final_invoices", "additional_charges"),
        ("final_invoices", "memo"),
    ],
)
def test_dropped_columns_are_absent(app, table, column):
    columns = {
        r[1] for r in db.session.execute(text("PRAGMA table_info({})".format(table)))
    }
    assert column not in columns


@pytest.mark.parametrize("table", ["electric_bill_months", "final_invoice_charges"])
def test_new_tables_exist(app, table):
    assert table in _table_names()


def test_is_carryover_column_exists(app):
    columns = {
        r[1] for r in db.session.execute(text("PRAGMA table_info(final_invoice_charges)"))
    }
    assert "is_carryover" in columns


def test_no_schema_drift_between_models_and_migration(app, tmp_path):
    """마이그레이션 결과 스키마가 모델 정의와 일치해야 한다 (autogenerate diff 없음)."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    connection = db.session.connection()
    context = MigrationContext.configure(
        connection, opts={"compare_type": True, "render_as_batch": True}
    )
    diff = compare_metadata(context, db.metadata)
    # alembic_version 테이블은 메타데이터에 없으므로 제외한다.
    diff = [d for d in diff if "alembic_version" not in str(d)]
    assert diff == [], "모델과 마이그레이션 스키마가 다릅니다: {}".format(diff)


def test_migration_upgrade_downgrade_upgrade(tmp_path):
    """fresh DB 에서 upgrade → downgrade base → upgrade 가 모두 성공해야 한다."""
    db_path = tmp_path / "cycle.db"
    env = dict(os.environ)
    env["BILLCALC_DB_PATH"] = str(db_path)
    env["FLASK_APP"] = "app.py"

    def run(*args):
        return subprocess.run(
            [sys.executable, "-m", "flask", *args],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )

    assert run("db", "upgrade").returncode == 0
    assert db_path.exists()
    assert run("db", "downgrade", "base").returncode == 0
    result = run("db", "upgrade")
    assert result.returncode == 0, result.stderr


def test_db_path_follows_config(tmp_path, monkeypatch):
    """DB 경로가 하드코딩이 아니라 환경변수를 따르는지."""
    import importlib

    import config

    monkeypatch.setenv("BILLCALC_DB_PATH", str(tmp_path / "custom.db"))
    monkeypatch.delenv("BILLCALC_DATABASE_URI", raising=False)
    importlib.reload(config)
    try:
        assert config.resolve_db_path() == (tmp_path / "custom.db").resolve()
        assert config.resolve_database_uri().endswith("custom.db")
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_database_uri_env_takes_precedence(tmp_path, monkeypatch):
    import importlib

    import config

    monkeypatch.setenv("BILLCALC_DATABASE_URI", "sqlite:///override.db")
    importlib.reload(config)
    try:
        assert config.resolve_database_uri() == "sqlite:///override.db"
        assert config.resolve_db_path() is None
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_no_mysql_dependency_in_runtime_requirements():
    content = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "mysql" not in content


def test_no_hardcoded_credentials_in_source():
    """DB 자격증명이 소스에 남아 있으면 안 된다."""
    for name in ("app.py", "config.py", "models.py", "extensions.py", "db_types.py"):
        content = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "mslee0702" not in content
        assert "mysql+mysqlconnector://" not in content
