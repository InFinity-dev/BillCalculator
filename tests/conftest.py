"""테스트 공통 픽스처.

DB 는 **파일 기반** 임시 SQLite 를 쓴다. ``:memory:`` 를 쓰지 않는 이유는
WAL 모드와 FK PRAGMA 등 실제 런타임 설정을 그대로 검증해야 하기 때문이다.

스키마는 반드시 Alembic 마이그레이션으로 만든다.
``db.create_all()`` 로 만들면 마이그레이션이 올바른지 검증되지 않는다.
"""

import os
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# app 을 import 하기 전에 DB 경로를 확정해야 한다.
# config.Config 가 클래스 정의 시점에 URI 를 계산하기 때문이다.
_TMP_DIR = tempfile.mkdtemp(prefix="billcalc-test-")
os.environ.pop("BILLCALC_DATABASE_URI", None)
os.environ["BILLCALC_DB_PATH"] = str(Path(_TMP_DIR) / "test.db")
os.environ["BILLCALC_SECRET_KEY"] = "test-secret-key"

from flask_migrate import upgrade  # noqa: E402

import app as app_module  # noqa: E402
from extensions import db  # noqa: E402
from models import (  # noqa: E402
    CommonBill,
    CommonBillDetail,
    ElectricBill,
    ElectricBillDetail,
    ElectricBillMonth,
    ElectricReading,
    FinalInvoice,
    FinalInvoiceCharge,
    Floor,
    InvoiceCombination,
    InvoiceCombinationItem,
    Payment,
    Setting,
    Unit,
    WaterBill,
    WaterBillDetail,
)

TEST_DB_PATH = Path(os.environ["BILLCALC_DB_PATH"])
CSRF_TOKEN = "test-csrf-token"


def pytest_addoption(parser):
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="골든 마스터 기준선을 현재 결과로 갱신한다",
    )


@pytest.fixture(scope="session")
def flask_app():
    application = app_module.app
    application.config["TESTING"] = True
    with application.app_context():
        upgrade()  # ← 마이그레이션으로 스키마 생성
        yield application


@pytest.fixture()
def app(flask_app):
    """각 테스트마다 데이터를 비우고 설정을 재시딩한다."""
    with flask_app.app_context():
        _truncate_all()
        app_module.seed_settings()
        yield flask_app
        db.session.rollback()


def _truncate_all():
    """자식 → 부모 순으로 전체 삭제한다 (FK 위반 없이)."""
    from sqlalchemy import text

    for table in reversed(db.metadata.sorted_tables):
        if table.name == "alembic_version":
            continue
        db.session.execute(table.delete())
    # sqlite_sequence 는 AUTOINCREMENT 컬럼이 있을 때만 존재한다.
    exists = db.session.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'")
    ).first()
    if exists:
        db.session.execute(text("DELETE FROM sqlite_sequence"))
    db.session.commit()


@pytest.fixture()
def client(app):
    with app.test_client() as test_client:
        with test_client.session_transaction() as sess:
            sess["_csrf_token"] = CSRF_TOKEN
        yield test_client


@pytest.fixture()
def csrf():
    return CSRF_TOKEN


# ---------------------------------------------------------------------------
# 도메인 픽스처
# ---------------------------------------------------------------------------
@pytest.fixture()
def floor(app):
    f = Floor(floor_number=1, name="1층")
    db.session.add(f)
    db.session.commit()
    return f


@pytest.fixture()
def units(app, floor):
    """재실 3세대 + 공실 1세대.

    - 101호: 2명, TV 보유, 복지 대상
    - 102호: 1명, TV 없음, 바우처 대상
    - 103호: 3명, TV 보유, 수도복지 대상
    - 104호: 공실
    """
    made = [
        Unit(floor_id=floor.id, unit_name="101호", residents_count=2, has_tv=True,
             electric_welfare=True),
        Unit(floor_id=floor.id, unit_name="102호", residents_count=1, has_tv=False,
             electric_voucher=True),
        Unit(floor_id=floor.id, unit_name="103호", residents_count=3, has_tv=True,
             water_welfare=True),
        Unit(floor_id=floor.id, unit_name="104호", residents_count=1, is_vacant=True),
    ]
    db.session.add_all(made)
    db.session.commit()
    return made


@pytest.fixture()
def electric_bill(app, floor, units):
    """세대별 detail 이 채워진 전기 고지 1건."""
    bill = ElectricBill(
        billing_month=date(2025, 3, 1),
        floor_id=floor.id,
        total_amount=100000,
        tv_distribution_mode="INDIVIDUAL",
        billing_months_count=1,
    )
    db.session.add(bill)
    db.session.flush()
    db.session.add(
        ElectricBillMonth(
            electric_bill_id=bill.id, billing_month=date(2025, 3, 1), amount=100000
        )
    )
    for index, unit in enumerate(u for u in units if not u.is_vacant):
        db.session.add(
            ElectricReading(
                electric_bill_id=bill.id,
                unit_id=unit.id,
                previous_reading=Decimal("100.00"),
                current_reading=Decimal("200.00"),
            )
        )
        db.session.add(
            ElectricBillDetail(
                electric_bill_id=bill.id,
                unit_id=unit.id,
                usage_amount=Decimal("100.00"),
                base_amount=Decimal("33333.33"),
                final_amount=Decimal("33333.33"),
                charged_amount=33340,
                unit_snapshot=app_module.create_unit_snapshot(unit),
            )
        )
    db.session.commit()
    return bill


@pytest.fixture()
def combination(app, units):
    """세대별 청구서가 있는 정산서 1건."""
    combo = InvoiceCombination(invoice_name="2025년 3월 정산", memo="테스트")
    db.session.add(combo)
    db.session.flush()
    for unit in units:
        if unit.is_vacant:
            continue
        db.session.add(
            FinalInvoice(
                combination_id=combo.id,
                unit_id=unit.id,
                electric_amount=10000,
                water_amount=5000,
                common_amount=2000,
                total_amount=17000,
            )
        )
    db.session.commit()
    return combo
