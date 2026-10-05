"""월별 더미 데이터와 실제 DB 격리·초기화 보호를 검증한다."""

from contextlib import closing
from datetime import date
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import balance
from db_validate import run_all_checks
from demo_data import build_demo
from models import ElectricBill, ElectricBillDetail, ElectricReading, FinalInvoiceCharge, InvoiceCombination, Payment, Unit, WaterBill
from scripts import demo
from tests.conftest import TEST_DB_PATH

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def monthly_demo(app):
    build_demo(app, date(2026, 10, 1), 6)
    return app


def test_monthly_data_crosses_year_and_passes_validation(monthly_demo):
    assert [bill.billing_month for bill in WaterBill.query.order_by(WaterBill.billing_month).all()] == [
        date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1),
        date(2027, 1, 1), date(2027, 2, 1), date(2027, 3, 1),
    ]
    assert ElectricBill.query.count() == 18
    assert InvoiceCombination.query.count() == 6
    assert Payment.query.count() > 20
    assert all(check.ok for check in run_all_checks())


def test_payment_scenarios_and_vacant_arrears(monthly_demo):
    units = {unit.unit_name: unit for unit in Unit.query.all()}
    for name in ("B101호", "101호", "203호"):
        assert balance.unit_balance(units[name].id)["balance"] == 0
    assert balance.unit_balance(units["201호"].id)["balance"] < 0
    assert balance.unit_balance(units["102호"].id)["balance"] > 0
    assert units["202호"].is_vacant
    assert balance.all_unit_balances()[units["202호"].id]["balance"] > 0
    historical = ElectricBillDetail.query.filter_by(unit_id=units["202호"].id).all()
    assert len(historical) == 2
    assert all(not detail.unit_snapshot["is_vacant"] for detail in historical)
    assert FinalInvoiceCharge.query.filter_by(is_carryover=True).count() > 0
    assert FinalInvoiceCharge.query.filter(FinalInvoiceCharge.amount < 0).count() > 0
    assert FinalInvoiceCharge.query.filter_by(description="수리비 환급", is_carryover=False).one().amount == -15000


def test_monthly_meter_readings_are_continuous(monthly_demo):
    for unit in Unit.query.all():
        rows = ElectricReading.query.filter_by(unit_id=unit.id).order_by(ElectricReading.electric_bill_id).all()
        for previous, current in zip(rows, rows[1:]):
            assert current.previous_reading == previous.current_reading
        assert all(row.current_reading > row.previous_reading for row in rows)


def test_demo_banner_is_only_enabled_for_demo(app, client, monkeypatch):
    text = "테스트 데이터 사용 중"
    assert text not in client.get("/").get_data(as_text=True)
    monkeypatch.setitem(app.config, "DEMO_MODE", True)
    assert text in client.get("/").get_data(as_text=True)


def test_automatic_test_database_is_isolated(app):
    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///" + str(TEST_DB_PATH.resolve())
    assert not os.environ.get("BILLCALC_DATABASE_URI")


@pytest.mark.parametrize("option,value", [("--months", "0"), ("--months", "37"),
                                         ("--start-month", "2026-13"), ("--start-month", "2026-1")])
def test_invalid_cli_options_are_rejected(option, value):
    with pytest.raises(SystemExit) as error:
        demo.main(["seed", option, value])
    assert error.value.code == 2


def test_unrecognized_database_is_preserved(tmp_path, monkeypatch):
    target = tmp_path / "demo.db"
    with closing(sqlite3.connect(str(target))) as connection:
        connection.execute("CREATE TABLE actual_data (value TEXT)")
    previous = target.read_bytes()
    monkeypatch.setattr(demo, "DEMO_DB", target)
    with pytest.raises(RuntimeError, match="기존 파일을 보존"):
        demo.seed(date(2026, 4, 1), 6, reset=True)
    assert target.read_bytes() == previous


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_linked_database_is_rejected(tmp_path, monkeypatch, link):
    original = tmp_path / "actual.db"
    original.write_bytes(b"actual data")
    target = tmp_path / "demo.db"
    if link == "symlink":
        target.symlink_to(original)
    else:
        os.link(str(original), str(target))
    monkeypatch.setattr(demo, "DEMO_DB", target)
    with pytest.raises(RuntimeError, match="연결된 DB"):
        demo.existing_metadata()
    assert original.read_bytes() == b"actual data"


def invoke_demo(target, args, environment=None, fail=False):
    code = "from pathlib import Path; from scripts import demo; demo.DEMO_DB=Path(__import__('sys').argv[1]); "
    if fail:
        code += "import demo_data; demo_data.build_demo=lambda *a: (_ for _ in ()).throw(RuntimeError('injected failure')); "
    code += "raise SystemExit(demo.main(__import__('sys').argv[2:]))"
    return subprocess.run([sys.executable, "-c", code, str(target)] + args,
                          cwd=str(ROOT), env=environment, capture_output=True, text=True, timeout=45)


def financial_snapshot(target):
    with closing(sqlite3.connect(str(target))) as connection:
        return {table: connection.execute("SELECT {} FROM {} ORDER BY id".format(columns, table)).fetchall()
                for table, columns in (
                    ("electric_bills", "id,billing_month,total_amount"),
                    ("final_invoices", "id,combination_id,unit_id,total_amount"),
                    ("payments", "id,combination_id,unit_id,payment_date,payment_amount"),
                )}


def test_seed_isolated_repeatable_and_failure_preserves_existing_db(tmp_path):
    target = tmp_path / "demo.db"
    actual = tmp_path / "actual.db"
    with closing(sqlite3.connect(str(actual))) as connection:
        connection.execute("CREATE TABLE actual_data (value TEXT)")
    untouched = actual.read_bytes()
    environment = dict(os.environ, BILLCALC_DATABASE_URI="sqlite:///" + str(actual),
                       BILLCALC_DB_PATH=str(actual), BILLCALC_PORT="invalid")
    first = invoke_demo(target, ["seed"], environment)
    assert first.returncode == 0, first.stdout + first.stderr
    assert actual.read_bytes() == untouched
    before = financial_snapshot(target)
    repeated = invoke_demo(target, ["seed"], environment)
    assert repeated.returncode == 0
    assert "기존 더미 데이터 유지" in repeated.stdout
    assert financial_snapshot(target) == before
    reset = invoke_demo(target, ["seed", "--reset"], environment)
    assert reset.returncode == 0, reset.stdout + reset.stderr
    assert financial_snapshot(target) == before
    failed = invoke_demo(target, ["seed", "--reset"], environment, fail=True)
    assert failed.returncode == 1
    assert financial_snapshot(target) == before
    assert actual.read_bytes() == untouched
    assert not list(tmp_path.glob(".demo-seed-*"))
    # 실제 DB URI를 상속받아도 pytest 자체도 임시 DB만 사용해야 한다.
    isolated = subprocess.run([sys.executable, "-m", "pytest", "-q",
                               "tests/test_demo.py::test_automatic_test_database_is_isolated"],
                              cwd=str(ROOT), env=dict(environment, BILLCALC_PORT="5000"),
                              capture_output=True, text=True, timeout=45)
    assert isolated.returncode == 0, isolated.stdout + isolated.stderr
    assert actual.read_bytes() == untouched
    # 실제 SQLite 연결이 살아 있으면 초기화를 중단한다.
    with closing(sqlite3.connect(str(target))) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("SELECT * FROM settings").fetchall()
        refused = invoke_demo(target, ["seed", "--reset"], environment)
        assert refused.returncode == 1
        assert "테스트 서버를 종료" in refused.stderr
    assert financial_snapshot(target) == before
    # 종료 뒤 남은 빈 WAL/SHM은 정상 정리하고 다시 생성할 수 있다.
    after_close = invoke_demo(target, ["seed", "--reset"], environment)
    assert after_close.returncode == 0, after_close.stdout + after_close.stderr
    assert financial_snapshot(target) == before
