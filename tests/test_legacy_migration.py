"""T-M. 레거시 MySQL → SQLite 이전 로직.

실제 MySQL 에 접근할 수 없으므로(감사 문서 00절 참조), **레거시 스키마 형태를 재현한
SQLite DB** 를 소스로 사용해 변환 로직을 검증한다.
``LegacySource`` 는 SQLAlchemy 로 introspect 하므로 소스 엔진 종류에 의존하지 않는다.
"""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from migrate_mysql_to_sqlite import (  # noqa: E402
    LegacySource,
    Migrator,
    Report,
    legacy_is_carryover,
    normalize_month,
    to_money,
)

LEGACY_DDL = """
CREATE TABLE settings (id INTEGER PRIMARY KEY, setting_key TEXT, setting_value TEXT,
                       created_at TEXT, updated_at TEXT);
CREATE TABLE floors (id INTEGER PRIMARY KEY, floor_number INTEGER, name TEXT,
                     electric_contract_number TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE units (id INTEGER PRIMARY KEY, floor_id INTEGER, unit_name TEXT, memo TEXT,
                    electric_welfare INTEGER, electric_voucher INTEGER, has_tv INTEGER,
                    water_welfare INTEGER, residents_count INTEGER, is_vacant INTEGER,
                    created_at TEXT, updated_at TEXT);
CREATE TABLE electric_bills (id INTEGER PRIMARY KEY, billing_month TEXT, floor_id INTEGER,
                             total_amount TEXT, welfare_discount TEXT, voucher_discount TEXT,
                             tv_fee_total TEXT, tv_distribution_mode TEXT,
                             tv_units_count INTEGER, billing_months_count INTEGER,
                             monthly_details TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE electric_readings (id INTEGER PRIMARY KEY, electric_bill_id INTEGER,
                                unit_id INTEGER, previous_reading TEXT, current_reading TEXT,
                                created_at TEXT, updated_at TEXT);
CREATE TABLE electric_bill_details (id INTEGER PRIMARY KEY, electric_bill_id INTEGER,
                                    unit_id INTEGER, usage_amount TEXT, base_amount TEXT,
                                    welfare_discount TEXT, voucher_discount TEXT, tv_fee TEXT,
                                    final_amount TEXT, charged_amount TEXT,
                                    unit_snapshot TEXT, created_at TEXT);
CREATE TABLE water_bills (id INTEGER PRIMARY KEY, billing_month TEXT, total_amount TEXT,
                          welfare_discount_total TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE water_bill_details (id INTEGER PRIMARY KEY, water_bill_id INTEGER,
                                 unit_id INTEGER, base_amount TEXT, welfare_discount TEXT,
                                 final_amount TEXT, charged_amount TEXT, unit_snapshot TEXT,
                                 is_excluded INTEGER, created_at TEXT);
CREATE TABLE common_bills (id INTEGER PRIMARY KEY, billing_month TEXT, description TEXT,
                           total_amount TEXT, distribution_method TEXT,
                           created_at TEXT, updated_at TEXT);
CREATE TABLE common_bill_details (id INTEGER PRIMARY KEY, common_bill_id INTEGER,
                                  unit_id INTEGER, amount TEXT, charged_amount TEXT,
                                  unit_snapshot TEXT, created_at TEXT);
CREATE TABLE invoice_combinations (id INTEGER PRIMARY KEY, invoice_name TEXT, memo TEXT,
                                   created_at TEXT, updated_at TEXT);
CREATE TABLE invoice_combination_items (id INTEGER PRIMARY KEY, combination_id INTEGER,
                                        item_type TEXT, billing_month TEXT,
                                        item_description TEXT, electric_bill_id INTEGER,
                                        water_bill_id INTEGER, common_bill_id INTEGER,
                                        created_at TEXT);
CREATE TABLE final_invoices (id INTEGER PRIMARY KEY, combination_id INTEGER, unit_id INTEGER,
                             electric_amount TEXT, water_amount TEXT, common_amount TEXT,
                             common_details TEXT, additional_charges TEXT,
                             total_amount TEXT, memo TEXT, unit_memo TEXT, created_at TEXT);
CREATE TABLE payments (id INTEGER PRIMARY KEY, combination_id INTEGER, unit_id INTEGER,
                       payment_date TEXT, payment_amount TEXT, payment_method TEXT,
                       memo TEXT, created_at TEXT, updated_at TEXT);
"""

SNAPSHOT = json.dumps(
    {
        "unit_name": "101호",
        "electric_welfare": False,
        "electric_voucher": False,
        "has_tv": True,
        "water_welfare": False,
        "residents_count": 2,
        "is_vacant": False,
    }
)
NOW = "2025-03-01 00:00:00"


def _build_legacy_db(path, overrides=None):
    """레거시 형태의 SQLite DB 를 만든다."""
    overrides = overrides or {}
    conn = sqlite3.connect(str(path))
    conn.executescript(LEGACY_DDL)

    conn.execute(
        "INSERT INTO settings VALUES (1,'tv_fee','2500',?,?)", (NOW, NOW)
    )
    conn.execute(
        "INSERT INTO floors VALUES (1,1,'1층','C-1',?,?)", (NOW, NOW)
    )
    conn.execute(
        "INSERT INTO units VALUES (1,1,'101호','',0,0,1,0,2,0,?,?)", (NOW, NOW)
    )
    conn.execute(
        "INSERT INTO units VALUES (2,1,'102호','',0,0,NULL,0,1,0,?,?)", (NOW, NOW)
    )

    conn.execute(
        "INSERT INTO electric_bills VALUES "
        "(1,'2025-03-01',1,'60000.00','0.00','0.00','0.00','INDIVIDUAL',?,1,?,?,?)",
        (
            overrides.get("tv_units_count", 0),
            overrides.get("monthly_details", json.dumps([{"month": "2025-03",
                                                         "amount": 60000, "welfare": 0,
                                                         "voucher": 0, "tv_fee": 0}])),
            NOW,
            NOW,
        ),
    )
    conn.execute(
        "INSERT INTO electric_readings VALUES (1,1,1,'0.00','100.00',?,?)", (NOW, NOW)
    )
    conn.execute(
        "INSERT INTO electric_bill_details VALUES "
        "(1,1,1,'100.00','30000.00','0.00','0.00','0.00','30000.00','30000.00',?,?)",
        (SNAPSHOT, NOW),
    )
    conn.execute(
        "INSERT INTO water_bills VALUES (1,'2025-03-01','30000.00','0.00',?,?)", (NOW, NOW)
    )
    conn.execute(
        "INSERT INTO water_bill_details VALUES "
        "(1,1,1,'20000.00','0.00','20000.00','20000.00',?,0,?)",
        (SNAPSHOT, NOW),
    )
    conn.execute(
        "INSERT INTO common_bills VALUES (1,'2025-03-01','인터넷','20000.00','BY_UNITS',?,?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO common_bill_details VALUES (1,1,1,'10000.00','10000.00',?,?)",
        (SNAPSHOT, NOW),
    )
    conn.execute(
        "INSERT INTO invoice_combinations VALUES (1,'2025년 3월','공통 메모',?,?)", (NOW, NOW)
    )
    conn.execute(
        "INSERT INTO invoice_combination_items VALUES "
        "(1,1,'ELECTRIC','2025-03-01','3월 전기',1,NULL,NULL,?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO final_invoices VALUES "
        "(1,1,1,'30000.00','20000.00','10000.00',?,?,'68000.00','공통 메모','',?)",
        (
            json.dumps([{"description": "인터넷", "amount": 10000}]),
            overrides.get(
                "additional_charges",
                json.dumps(
                    [
                        {"description": "[이월] 전월 미납금", "amount": 5000},
                        {"description": "수리비", "amount": 3000},
                    ]
                ),
            ),
            NOW,
        ),
    )
    conn.execute(
        "INSERT INTO payments VALUES (1,1,1,'2025-04-01','50000.00','계좌이체','',?,?)",
        (NOW, NOW),
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def legacy_db(tmp_path):
    return _build_legacy_db(tmp_path / "legacy.db")


def _extract(path):
    report = Report()
    source = LegacySource("sqlite:///{}".format(path)).open()
    try:
        migrator = Migrator(source, report)
        migrator.extract()
    finally:
        source.close()
    return migrator, report


# ---------------------------------------------------------------------------
# 값 변환
# ---------------------------------------------------------------------------
def test_money_conversion_from_decimal_text():
    report = Report()
    assert to_money("12500.00", report, "t", 1, "c") == 12500
    assert report.blocking == []


def test_money_with_fraction_is_blocked():
    report = Report()
    to_money("12345.67", report, "electric_bills", 1, "total_amount")
    assert report.blocking
    assert report.blocking[0]["issue"] == "money_has_fraction"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2025-03", "2025-03-01"),
        ("2025-03-17", "2025-03-01"),
        ("2025-03-01", "2025-03-01"),
    ],
)
def test_normalize_month(value, expected):
    assert normalize_month(value).isoformat() == expected


def test_normalize_month_handles_date_object():
    from datetime import date

    assert normalize_month(date(2025, 3, 17)) == date(2025, 3, 1)


def test_normalize_month_rejects_empty():
    assert normalize_month("") is None
    assert normalize_month(None) is None


@pytest.mark.parametrize(
    "description,expected",
    [
        ("[이월] 전월 미납금", True),
        ("[이월] 전월 초과납부 환급", True),
        ("수리비", False),
        ("보증금 환급", True),      # 애매 케이스 — 레거시가 잔액에서 제외해 왔다
        ("관리비 미납분", True),
        ("청소비", False),
    ],
)
def test_legacy_carryover_rule(description, expected):
    assert legacy_is_carryover(description) is expected


# ---------------------------------------------------------------------------
# Extract / Transform
# ---------------------------------------------------------------------------
def test_extract_basic_counts(legacy_db):
    migrator, report = _extract(legacy_db)
    assert report.blocking == []
    assert len(migrator.extracted["floors"]) == 1
    assert len(migrator.extracted["units"]) == 2
    assert len(migrator.extracted["electric_bills"]) == 1
    assert len(migrator.extracted["electric_bill_months"]) == 1
    assert len(migrator.extracted["final_invoices"]) == 1
    assert len(migrator.extracted["final_invoice_charges"]) == 2
    assert len(migrator.extracted["payments"]) == 1


def test_primary_keys_are_preserved(legacy_db):
    migrator, _ = _extract(legacy_db)
    assert migrator.extracted["floors"][0]["id"] == 1
    assert [u["id"] for u in migrator.extracted["units"]] == [1, 2]
    assert migrator.extracted["final_invoices"][0]["id"] == 1


def test_money_columns_become_int(legacy_db):
    migrator, _ = _extract(legacy_db)
    bill = migrator.extracted["electric_bills"][0]
    assert bill["total_amount"] == 60000
    assert isinstance(bill["total_amount"], int)
    assert migrator.extracted["payments"][0]["payment_amount"] == 50000


def test_monthly_details_become_rows(legacy_db):
    migrator, _ = _extract(legacy_db)
    month = migrator.extracted["electric_bill_months"][0]
    assert month["electric_bill_id"] == 1
    assert month["billing_month"].isoformat() == "2025-03-01"
    assert month["amount"] == 60000


def test_monthly_details_month_variants(tmp_path):
    for raw, expected in (("2025-03", "2025-03-01"), ("2025-03-17", "2025-03-01")):
        path = tmp_path / "m-{}.db".format(raw.replace("-", ""))
        _build_legacy_db(
            path,
            overrides={
                "monthly_details": json.dumps([{"month": raw, "amount": 60000}])
            },
        )
        migrator, report = _extract(path)
        assert report.blocking == []
        assert migrator.extracted["electric_bill_months"][0][
            "billing_month"
        ].isoformat() == expected


def test_monthly_details_missing_month_is_blocked(tmp_path):
    """★ 고지월을 임의로 추정하지 않는다."""
    path = _build_legacy_db(
        tmp_path / "nomonth.db",
        overrides={"monthly_details": json.dumps([{"month": None, "amount": 60000}])},
    )
    migrator, report = _extract(path)
    assert any(b["issue"] == "monthly_detail_missing_month" for b in report.blocking)
    assert migrator.extracted["electric_bill_months"] == []


def test_monthly_details_duplicate_month_is_blocked(tmp_path):
    path = _build_legacy_db(
        tmp_path / "dupmonth.db",
        overrides={
            "monthly_details": json.dumps(
                [{"month": "2025-03", "amount": 1}, {"month": "2025-03", "amount": 2}]
            )
        },
    )
    _, report = _extract(path)
    assert any(
        b["issue"] == "monthly_detail_duplicate_month" for b in report.blocking
    )


def test_tv_units_count_nonzero_is_warned(tmp_path):
    path = _build_legacy_db(tmp_path / "tv.db", overrides={"tv_units_count": 3})
    _, report = _extract(path)
    assert any(w["issue"] == "tv_units_count_dropped" for w in report.warnings)


def test_additional_charges_become_rows_with_carryover_flag(legacy_db):
    migrator, _ = _extract(legacy_db)
    charges = sorted(
        migrator.extracted["final_invoice_charges"], key=lambda c: c["sort_order"]
    )
    assert charges[0]["description"] == "[이월] 전월 미납금"
    assert charges[0]["is_carryover"] is True
    assert charges[0]["amount"] == 5000
    assert charges[1]["description"] == "수리비"
    assert charges[1]["is_carryover"] is False


def test_ambiguous_carryover_is_reported(tmp_path):
    """'[이월]' 접두사 없이 키워드만 포함하는 항목은 검토 대상으로 수집한다."""
    path = _build_legacy_db(
        tmp_path / "ambiguous.db",
        overrides={
            "additional_charges": json.dumps(
                [{"description": "보증금 환급", "amount": -50000}]
            )
        },
    )
    migrator, report = _extract(path)
    assert len(report.ambiguous_carryover) == 1
    entry = report.ambiguous_carryover[0]
    assert entry["description"] == "보증금 환급"
    # 레거시가 잔액에서 제외해 왔으므로 동일하게 True 로 이전한다 (balance 보존).
    assert entry["assigned_is_carryover"] is True
    assert migrator.extracted["final_invoice_charges"][0]["is_carryover"] is True


def test_boolean_null_uses_domain_default(legacy_db):
    migrator, _ = _extract(legacy_db)
    units = {u["id"]: u for u in migrator.extracted["units"]}
    assert units[1]["has_tv"] is True
    assert units[2]["has_tv"] is True    # NULL → has_tv 기본값 True
    assert units[2]["is_vacant"] is False


def test_final_invoice_memo_column_is_dropped(legacy_db):
    migrator, _ = _extract(legacy_db)
    assert "memo" not in migrator.extracted["final_invoices"][0]


def test_snapshot_is_copied_verbatim(legacy_db):
    migrator, _ = _extract(legacy_db)
    snapshot = migrator.extracted["electric_bill_details"][0]["unit_snapshot"]
    assert snapshot["unit_name"] == "101호"
    assert snapshot["residents_count"] == 2


def test_legacy_balance_is_computed(legacy_db):
    migrator, _ = _extract(legacy_db)
    # billed = 30000 + 20000 + 10000 + 3000(수리비, 이월 아님) = 63000
    # paid   = 50000  →  balance = 13000
    assert migrator.legacy_balances[1] == 13000


# ---------------------------------------------------------------------------
# 위반 탐지
# ---------------------------------------------------------------------------
def _mutate(path, sql, params=()):
    conn = sqlite3.connect(str(path))
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def test_duplicate_unit_name_is_blocked(legacy_db):
    _mutate(
        legacy_db,
        "INSERT INTO units VALUES (3,1,'101호','',0,0,1,0,1,0,?,?)",
        (NOW, NOW),
    )
    _, report = _extract(legacy_db)
    assert any(b["issue"] == "duplicate_unit_name" for b in report.blocking)


def test_duplicate_detail_is_blocked(legacy_db):
    _mutate(
        legacy_db,
        "INSERT INTO electric_bill_details VALUES "
        "(2,1,1,'1.00','1.00','0.00','0.00','0.00','1.00','10.00',?,?)",
        (SNAPSHOT, NOW),
    )
    _, report = _extract(legacy_db)
    assert any(b["issue"] == "duplicate_detail" for b in report.blocking)


def test_polymorphic_mismatch_is_blocked(legacy_db):
    _mutate(
        legacy_db,
        "INSERT INTO invoice_combination_items VALUES "
        "(2,1,'ELECTRIC','2025-03-01','x',NULL,1,NULL,?)",
        (NOW,),
    )
    _, report = _extract(legacy_db)
    assert any(b["issue"] == "polymorphic_mismatch" for b in report.blocking)


def test_invalid_item_type_is_blocked(legacy_db):
    _mutate(
        legacy_db,
        "INSERT INTO invoice_combination_items VALUES "
        "(3,1,'GAS','2025-03-01','x',1,NULL,NULL,?)",
        (NOW,),
    )
    _, report = _extract(legacy_db)
    assert any(b["issue"] == "invalid_item_type" for b in report.blocking)


def test_negative_reading_is_blocked(legacy_db):
    _mutate(
        legacy_db,
        "INSERT INTO electric_readings VALUES (2,1,2,'-5.00','10.00',?,?)",
        (NOW, NOW),
    )
    _, report = _extract(legacy_db)
    assert any(b["issue"] == "negative_reading" for b in report.blocking)


def test_charged_amount_not_multiple_of_10_is_warned(legacy_db):
    _mutate(
        legacy_db,
        "INSERT INTO electric_bill_details VALUES "
        "(2,1,2,'1.00','1.00','0.00','0.00','0.00','1.00','13.00',?,?)",
        (SNAPSHOT, NOW),
    )
    _, report = _extract(legacy_db)
    assert any(w["issue"] == "charged_not_multiple_of_10" for w in report.warnings)


def test_legacy_single_item_id_column_is_supported(tmp_path):
    """SQLSchema.txt 기준(item_id 단일 컬럼)으로 만들어진 DB 도 처리한다."""
    path = tmp_path / "legacy-itemid.db"
    _build_legacy_db(path)
    conn = sqlite3.connect(str(path))
    conn.execute("DROP TABLE invoice_combination_items")
    conn.execute(
        "CREATE TABLE invoice_combination_items (id INTEGER PRIMARY KEY, "
        "combination_id INTEGER, item_type TEXT, item_id INTEGER, billing_month TEXT, "
        "item_description TEXT, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO invoice_combination_items VALUES (1,1,'ELECTRIC',1,'2025-03-01','x',?)",
        (NOW,),
    )
    conn.commit()
    conn.close()

    migrator, report = _extract(path)
    assert report.blocking == []
    item = migrator.extracted["invoice_combination_items"][0]
    assert item["electric_bill_id"] == 1
    assert item["water_bill_id"] is None


# ---------------------------------------------------------------------------
# 전체 실행 (CLI)
# ---------------------------------------------------------------------------
def _run_script(*args, cwd=REPO_ROOT):
    env = dict(os.environ)
    env.pop("BILLCALC_DATABASE_URI", None)
    return subprocess.run(
        [sys.executable, "scripts/migrate_mysql_to_sqlite.py", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )


def _file_digest(path):
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.mark.slow
def test_full_migration_run(legacy_db, tmp_path):
    target = tmp_path / "migrated.db"
    report_path = tmp_path / "report.json"
    before = _file_digest(legacy_db)

    result = _run_script(
        "--mysql-uri", "sqlite:///{}".format(legacy_db),
        "--sqlite-path", str(target),
        "--report", str(report_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert target.exists()

    # ★ 소스가 변경되지 않아야 한다
    assert _file_digest(legacy_db) == before

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["blocking"] == []
    assert all(c["ok"] for c in report["counts"].values())
    assert all(a["ok"] for a in report["aggregates"].values())
    assert report["balance_check"]["ok"] is True

    conn = sqlite3.connect(str(target))
    try:
        assert conn.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM electric_bill_months"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM final_invoice_charges"
        ).fetchone()[0] == 2
        # is_carryover 가 저장되었는지
        rows = dict(
            conn.execute("SELECT description, is_carryover FROM final_invoice_charges")
        )
        assert rows["[이월] 전월 미납금"] == 1
        assert rows["수리비"] == 0
        # 금액이 정수로 저장되었는지
        assert conn.execute(
            "SELECT typeof(total_amount) FROM electric_bills"
        ).fetchone()[0] == "integer"
        # 참조 무결성
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


@pytest.mark.slow
def test_report_only_does_not_create_target(legacy_db, tmp_path):
    target = tmp_path / "not-created.db"
    report_path = tmp_path / "report.json"
    result = _run_script(
        "--mysql-uri", "sqlite:///{}".format(legacy_db),
        "--sqlite-path", str(target),
        "--report", str(report_path),
        "--report-only",
    )
    assert result.returncode == 0
    assert not target.exists()
    assert report_path.exists()


@pytest.mark.slow
def test_blocking_issue_aborts_migration(legacy_db, tmp_path):
    _mutate(
        legacy_db,
        "INSERT INTO units VALUES (3,1,'101호','',0,0,1,0,1,0,?,?)",
        (NOW, NOW),
    )
    target = tmp_path / "aborted.db"
    result = _run_script(
        "--mysql-uri", "sqlite:///{}".format(legacy_db),
        "--sqlite-path", str(target),
        "--report", str(tmp_path / "report.json"),
    )
    assert result.returncode == 1
    assert not target.exists()


@pytest.mark.slow
def test_existing_target_is_refused(legacy_db, tmp_path):
    target = tmp_path / "existing.db"
    target.write_bytes(b"not empty")
    result = _run_script(
        "--mysql-uri", "sqlite:///{}".format(legacy_db),
        "--sqlite-path", str(target),
        "--report", str(tmp_path / "report.json"),
    )
    assert result.returncode == 2
    assert target.read_bytes() == b"not empty"
