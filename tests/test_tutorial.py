"""격리된 온보딩 데이터, 자동 표시, 완료·재실행 정책을 검증한다."""

from contextlib import contextmanager
from datetime import date
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from demo_data import build_demo
from extensions import db
from itsdangerous import URLSafeTimedSerializer
from models import (
    CommonBill,
    ElectricBill,
    FinalInvoice,
    Floor,
    InvoiceCombinationItem,
    Unit,
    WaterBill,
)
from onboarding_state import onboarding_completed
from scripts import demo
from tests.test_frontend import AssetParser
from tutorial_service import (
    cleanup_owned_tutorial_databases,
    create_tutorial_database,
    delete_tutorial_database,
    tutorial_database_path,
)
import app as app_module


@contextmanager
def onboarding_config(application, tmp_path):
    keys = ("ONBOARDING_ENABLED", "ONBOARDING_STATE_PATH", "TUTORIAL_DATA_DIR")
    previous = {key: application.config.get(key) for key in keys}
    application.config.update(
        ONBOARDING_ENABLED=True,
        ONBOARDING_STATE_PATH=tmp_path / "app_state.json",
        TUTORIAL_DATA_DIR=tmp_path / "tutorial_sessions",
    )
    try:
        yield
    finally:
        application.config.update(previous)


@contextmanager
def fake_tutorial_runtime(monkeypatch):
    launched = []
    stopped = []

    def launch(session_id, database_path, main_url, control_token, **options):
        launched.append({
            "session_id": session_id,
            "database_path": Path(database_path),
            "main_url": main_url,
            "control_token": control_token,
            **options,
        })
        return SimpleNamespace(port=54321)

    monkeypatch.setattr(app_module, "launch_tutorial_process", launch)
    monkeypatch.setattr(app_module, "stop_tutorial_process", lambda session_id: stopped.append(session_id))
    yield launched, stopped


def test_empty_database_offers_tutorial_but_existing_data_does_not(app, client, tmp_path):
    with onboarding_config(app, tmp_path):
        response = client.get("/")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/tutorial")

        floor = Floor(floor_number=1, name="1층")
        db.session.add(floor)
        db.session.commit()
        response = client.get("/")
        assert response.status_code == 200


def test_first_run_completion_opens_requested_real_data_start(app, client, csrf, tmp_path, monkeypatch):
    with onboarding_config(app, tmp_path), fake_tutorial_runtime(monkeypatch) as (launched, stopped):
        response = client.get("/tutorial")
        assert response.status_code == 302
        assert response.headers["Location"] == "http://127.0.0.1:54321/"
        assert launched[0]["had_user_data"] is False
        assert Floor.query.count() == 0
        assert Unit.query.count() == 0

        with client.session_transaction() as session:
            tutorial_id = session["tutorial_id"]
        tutorial_db = tutorial_database_path(tmp_path / "tutorial_sessions", tutorial_id)
        completed = client.post(
            "/tutorial/complete",
            data={"_csrf_token": csrf, "next_action": "import"},
        )
        assert completed.status_code == 302
        assert completed.headers["Location"].endswith("/settings?tab=import-export-tab")
        assert not tutorial_db.exists()
        assert stopped == [tutorial_id]
        assert client.get(completed.headers["Location"]).status_code == 200
        assert Floor.query.count() == 0
        assert Unit.query.count() == 0


def test_tutorial_isolated_completion_and_manual_replay(app, client, csrf, tmp_path, monkeypatch):
    floor = Floor(floor_number=1, name="실제 1층")
    db.session.add(floor)
    db.session.flush()
    db.session.add(Unit(floor_id=floor.id, unit_name="실제 101호", residents_count=2))
    db.session.commit()

    with onboarding_config(app, tmp_path), fake_tutorial_runtime(monkeypatch) as (launched, _stopped):
        response = client.get("/tutorial")
        assert response.status_code == 302
        assert launched[0]["had_user_data"] is True

        with client.session_transaction() as session:
            tutorial_id = session["tutorial_id"]
        tutorial_db = tutorial_database_path(tmp_path / "tutorial_sessions", tutorial_id)
        assert tutorial_db.exists()
        assert Floor.query.count() == 1
        assert Unit.query.one().unit_name == "실제 101호"

        completed = client.post("/tutorial/complete", data={"_csrf_token": csrf})
        assert completed.status_code == 302
        assert completed.headers["Location"].endswith("/")
        assert not tutorial_db.exists()
        assert onboarding_completed(tmp_path / "app_state.json")
        assert Unit.query.one().unit_name == "실제 101호"

        # 완료 뒤 자동 표시는 사라지지만 메뉴를 통한 직접 재실행은 계속 가능하다.
        index_body = client.get("/").get_data(as_text=True)
        assert "사용 안내" in index_body
        assert "다시 보기" in index_body
        replay = client.get("/tutorial")
        assert replay.status_code == 302
        with client.session_transaction() as session:
            replay_id = session["tutorial_id"]
        assert replay_id != tutorial_id
        assert tutorial_database_path(tmp_path / "tutorial_sessions", replay_id).exists()
        client.post("/tutorial/complete", data={"_csrf_token": csrf})
        assert Unit.query.one().unit_name == "실제 101호"


def test_tutorial_reentry_discards_changes_and_creates_fresh_run(app, client, tmp_path, monkeypatch):
    with onboarding_config(app, tmp_path), fake_tutorial_runtime(monkeypatch) as (launched, stopped):
        first = client.get("/tutorial")
        assert first.status_code == 302
        with client.session_transaction() as session:
            first_id = session["tutorial_id"]
        first_db = tutorial_database_path(tmp_path / "tutorial_sessions", first_id)
        with sqlite3.connect(str(first_db)) as connection:
            connection.execute(
                "UPDATE floors SET name = ? WHERE id = (SELECT MIN(id) FROM floors)",
                ("사용자가 변경한 연습 층",),
            )

        second = client.get("/tutorial")
        assert second.status_code == 302
        with client.session_transaction() as session:
            second_id = session["tutorial_id"]
        second_db = tutorial_database_path(tmp_path / "tutorial_sessions", second_id)

        assert second_id != first_id
        assert not first_db.exists()
        assert second_db.exists()
        assert stopped == [first_id]
        assert [item["session_id"] for item in launched] == [first_id, second_id]
        with sqlite3.connect(str(second_db)) as connection:
            changed = connection.execute(
                "SELECT COUNT(*) FROM floors WHERE name = ?",
                ("사용자가 변경한 연습 층",),
            ).fetchone()[0]
        assert changed == 0


def test_live_tutorial_wraps_actual_pages_with_bright_guide(app, client, csrf):
    floor = Floor(floor_number=1, name="연습 1층")
    db.session.add(floor)
    db.session.flush()
    db.session.add(Unit(floor_id=floor.id, unit_name="연습 101호", residents_count=2))
    db.session.add(ElectricBill(
        billing_month=date(2026, 9, 1), floor_id=floor.id, total_amount=120000,
        billing_months_count=1,
    ))
    db.session.commit()
    calculated = client.post("/calculate/common", data={
        "_csrf_token": csrf, "billing_month": "2026-10", "description": "연습 공동요금",
        "total_amount": "10000", "distribution_method": "BY_UNITS",
    })
    assert calculated.get_json()["success"] is True
    common_id = CommonBill.query.one().id
    invoice_payload = {
        "_csrf_token": csrf, "name": "연습 정산서", "memo": "",
        "items": [{"type": "COMMON", "id": common_id, "month": "2026-10", "description": "연습 공동요금"}],
        "unit_additional_data": {},
    }
    preview = client.post("/invoice/preview", json=invoice_payload).get_json()
    invoice_payload["preview_token"] = preview["preview_token"]
    created = client.post("/invoice/create", json=invoice_payload).get_json()
    assert created["success"] is True
    combination_id = created["id"]
    previous = {
        key: app.config.get(key)
        for key in ("TUTORIAL_MODE", "TUTORIAL_MAIN_URL", "TUTORIAL_CONTROL_TOKEN", "TUTORIAL_HAD_USER_DATA")
    }
    app.config.update(
        TUTORIAL_MODE=True,
        TUTORIAL_MAIN_URL="http://127.0.0.1:5000",
        TUTORIAL_CONTROL_TOKEN="signed-token",
        TUTORIAL_HAD_USER_DATA=True,
    )
    try:
        body = client.get("/settings").get_data(as_text=True)
        calculator_body = client.get("/calculator").get_data(as_text=True)
        invoice_body = client.get("/invoice").get_data(as_text=True)
        invoice_view_body = client.get(f"/invoice/view/{combination_id}").get_data(as_text=True)
        print_body = client.get(f"/invoice/print/{combination_id}").get_data(as_text=True)
        payments_body = client.get("/payments").get_data(as_text=True)
    finally:
        app.config.update(previous)

    assert "연습 데이터 사용 중 · 실제 데이터와 완전히 분리되어 있습니다" in body
    assert "실제 화면 따라하기" in body
    assert 'data-tutorial-target="unit-management"' in body
    assert 'data-tutorial-nav="settings"' in body
    assert 'data-tutorial-nav="tutorial"' in body
    assert 'class="tutorial-focus-ring"' in body
    assert 'id="tutorialCue"' in body
    assert "tutorial-mask" not in body
    assert "signed-token" in body
    parsed = AssetParser()
    parsed.feed(body)
    assert "/static/css/tutorial_overlay.css" in parsed.assets
    assert "/static/js/tutorial_overlay.js" in parsed.assets
    assert all(client.get(asset).status_code == 200 for asset in parsed.assets)
    assert 'data-tutorial-target="invoice-name"' in invoice_body
    assert 'data-tutorial-target="invoice-next-items"' in invoice_body
    assert 'data-tutorial-target="invoice-next-review"' in invoice_body
    assert 'data-tutorial-target="invoice-create"' in invoice_body
    assert 'id="invoiceSelectionSummary"' in invoice_body
    assert 'data-electric-month-select="2026-09"' in invoice_body
    assert '0 / 1개 층 선택' in invoice_body
    assert "tutorial:calculation-preview-ready" in calculator_body
    assert "tutorial:calculation-saved" in calculator_body
    assert "tutorial:invoice-items-confirmed" in invoice_body
    assert "tutorial:invoice-preview-ready" in invoice_body
    assert "tutorial:invoice-created" in invoice_body
    assert "tutorial:payment-history-ready" in payments_body
    assert 'data-tutorial-target="invoice-print"' in invoice_view_body
    assert 'target="_blank"' not in invoice_view_body
    assert 'data-tutorial-target="print-preview"' in print_body
    assert 'data-tutorial-target="print-action"' in print_body
    assert 'data-tutorial-target="print-return"' in print_body
    assert "tutorial:print-opened" in print_body
    assert 'id="tutorialGuide"' in print_body
    assert 'id="tutorialPracticeDock"' in print_body
    assert 'id="tutorialPracticeStart"' in print_body
    assert 'id="tutorialPracticeEntryZone"' in print_body
    assert "자유 연습으로 전환" in print_body
    assert 'id="tutorialExit"' in print_body
    assert "연습 종료" in print_body
    assert '"endpoint": "print_invoice"' in print_body
    assert "백업·복원은 실제 데이터에서 사용하세요" in body
    assert "자유 연습 중에는 내보낼 수 없음" in body
    assert "자유 연습 중에는 복원할 수 없음" in body
    assert "const tutorialDataTransferRestricted = true" in body


def test_tutorial_mode_blocks_backup_and_restore_routes(app, client, csrf):
    previous = app.config.get("TUTORIAL_MODE")
    app.config["TUTORIAL_MODE"] = True
    try:
        responses = [
            client.get("/settings/export"),
            client.get("/settings/automatic-backups"),
            client.post(
                "/settings/automatic-backups/preview",
                json={"_csrf_token": csrf},
            ),
            client.post(
                "/settings/automatic-backups/restore",
                json={"_csrf_token": csrf},
            ),
            client.post("/settings/import/preview", json={"_csrf_token": csrf}),
            client.post("/settings/import", json={"_csrf_token": csrf}),
        ]
        assert app_module._create_safety_backup("before_floor_delete") is None
    finally:
        app.config["TUTORIAL_MODE"] = previous

    assert all(response.status_code == 403 for response in responses)
    assert all(response.get_json()["success"] is False for response in responses)
    assert all("자유 연습 중에는" in response.get_json()["message"] for response in responses)


def test_live_tutorial_overlay_tracks_layout_changes_and_covers_full_invoice_flow():
    source = (Path(app_module.__file__).parent / "static/js/tutorial_overlay.js").read_text()
    styles = (Path(app_module.__file__).parent / "static/css/tutorial_overlay.css").read_text()

    assert source.count("endpoint: '") == 24
    assert source.count("cue: '") == 24
    assert "billcalc-live-tutorial-step-v5:${runId}" in source
    assert "billcalc-live-tutorial-mode-v2:${runId}" in source
    assert "legacyStorageKeys" in source
    assert "document.addEventListener('scroll', schedulePosition" in source
    assert "MutationObserver" in source
    assert "window.visualViewport" in source
    assert "dialog.querySelector('.modal-content')" in source
    assert '[aria-hidden="false"]' in source
    assert "열린 창에서 작업을 마친 뒤" in source
    assert "completionEvent: 'tutorial:invoice-created'" in source
    assert "completionEvent: 'tutorial:calculation-preview-ready'" in source
    assert "completionEvent: 'tutorial:calculation-saved'" in source
    assert "completionEvent: 'tutorial:invoice-items-confirmed'" in source
    assert "completionEvent: 'tutorial:invoice-preview-ready'" in source
    assert "completionEvent: 'tutorial:payment-history-ready'" in source
    assert "completionEvent: 'tutorial:electric-month-selected'" in source
    assert "document.addEventListener('click', guardClick, true)" in source
    assert "document.addEventListener('change', guardChange, true)" in source
    assert "document.addEventListener('keydown', guardKeydown, true)" in source
    assert "document.addEventListener('submit', guardSubmit, true)" in source
    assert source.count("if (!event.isTrusted) return;") == 4
    assert "const navigationStepParameter = '_tutorial_step'" in source
    assert "destination.searchParams.set(navigationStepParameter" in source
    assert '[data-electric-month-select="2026-09"]' in source
    assert '[data-electric-month-select="2026-10"]' in source
    assert '.water-item[data-month^="2026-10"]' in source
    assert "2026년 9~10월 사용분 수도요금 (10월 고지)" in source
    assert "view_invoice: config.latestInvoiceUrl" in source
    assert ".tutorial-practice-entry-zone" in styles
    assert "margin-top: 28px" in styles
    assert "print_invoice: config.latestInvoicePrintUrl" in source
    assert "endpoint: 'print_invoice'" in source
    assert "completionEvent: 'tutorial:print-opened'" in source
    assert "skipActionLabel: '인쇄하지 않고 계속'" in source
    assert "event.key === 'Escape' && config.endpoint !== 'print_invoice'" in source
    assert "addEventListener('click', enterPracticeMode)" in source
    assert "tutorialPracticeRestart" in source
    assert "다시 안내받으려면 처음부터 시작해야 합니다" in source
    assert "controlUrl('exit')" in source
    assert "tutorial-practice-mode" in source
    assert "if (isPracticeMode()) return true" in source
    assert "if (step.final && !targetIsVisible(target, step))" in source
    assert ".tutorial-focus-ring" in styles
    assert ".tutorial-cue" in styles
    assert ".tutorial-practice-dock" in styles
    print_styles = (Path(app_module.__file__).parent / "static/css/pages/invoice_print.css").read_text()
    assert ".tutorial-print-mode.tutorial-practice-mode .tutorial-practice-dock" in print_styles
    assert "bottom: 20px" in print_styles
    assert ".tutorial-mask" not in styles
    assert "pointer-events: none;" in styles
    assert "배경은 어두워지지 않으며" not in source


def test_signed_tutorial_control_exit_deletes_current_session(app, client, tmp_path, monkeypatch):
    with onboarding_config(app, tmp_path), fake_tutorial_runtime(monkeypatch) as (_launched, stopped):
        tutorial_id = "b" * 32
        tutorial_db = create_tutorial_database(tmp_path / "tutorial_sessions", tutorial_id)
        with client.session_transaction() as session:
            session["tutorial_id"] = tutorial_id
        token = URLSafeTimedSerializer(
            app.secret_key, salt="live-tutorial-control-v1"
        ).dumps({"tutorial_id": tutorial_id})
        response = client.get("/tutorial/control/exit", query_string={"token": token})
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        body = response.get_data(as_text=True)
        assert "연습을 종료했습니다" in body
        assert "window.location.replace" in body
        assert 'href="/"' in body
        assert stopped == [tutorial_id]
        assert not tutorial_db.exists()
        with client.session_transaction() as session:
            assert "tutorial_id" not in session
            assert session["onboarding_deferred"] is True


def test_tutorial_start_failure_returns_to_app_without_redirect_loop(app, client, tmp_path, monkeypatch):
    with onboarding_config(app, tmp_path):
        monkeypatch.setattr(
            app_module,
            "launch_tutorial_process",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("로컬 포트 오류")),
        )
        response = client.get("/tutorial")
        assert response.status_code == 303
        assert response.headers["Location"].endswith("/")
        index = client.get("/")
        assert index.status_code == 200
        assert "사용 안내를 시작하지 못했습니다: 로컬 포트 오류" in index.get_data(as_text=True)


def test_unrecognized_tutorial_file_is_never_deleted(app, client, csrf, tmp_path):
    with onboarding_config(app, tmp_path):
        tutorial_id = "a" * 32
        target = tutorial_database_path(tmp_path / "tutorial_sessions", tutorial_id)
        target.write_bytes(b"real user data")
        with client.session_transaction() as session:
            session["tutorial_id"] = tutorial_id
            session["tutorial_had_user_data"] = False
        response = client.post("/tutorial/complete", data={"_csrf_token": csrf})
        assert response.status_code == 302
        assert target.read_bytes() == b"real user data"
        assert not (tmp_path / "app_state.json").exists()


def test_active_tutorial_database_is_never_deleted(tmp_path):
    directory = tmp_path / "tutorial_sessions"
    tutorial_id = "c" * 32
    target = create_tutorial_database(directory, tutorial_id)
    connection = sqlite3.connect(str(target))
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="아직 사용 중"):
            delete_tutorial_database(directory, tutorial_id)
        assert target.exists()
    finally:
        connection.rollback()
        connection.close()
    assert delete_tutorial_database(directory, tutorial_id) is True
    assert not target.exists()


def test_desktop_cleanup_removes_only_owned_inactive_tutorial_databases(tmp_path):
    directory = tmp_path / "tutorial_sessions"
    owned = create_tutorial_database(directory, "e" * 32)
    unknown = tutorial_database_path(directory, "f" * 32)
    unknown.write_bytes(b"unrecognized user file")

    assert cleanup_owned_tutorial_databases(directory) == 1
    assert not owned.exists()
    assert unknown.read_bytes() == b"unrecognized user file"


def test_demo_cli_forwards_custom_database_path(monkeypatch, tmp_path):
    captured = {}

    def fake_seed(start, months, reset=False, db_path=None, tutorial_ready=False):
        captured.update(
            start=start, months=months, reset=reset, db_path=db_path,
            tutorial_ready=tutorial_ready,
        )

    monkeypatch.setattr(demo, "seed", fake_seed)
    target = tmp_path / "custom.db"
    assert demo.main(["seed", "--start-month", "2026-11", "--months", "2", "--reset", "--db-path", str(target)]) == 0
    assert captured == {
        "start": date(2026, 11, 1), "months": 2, "reset": True, "db_path": target,
        "tutorial_ready": False,
    }


def test_tutorial_database_prepares_all_floor_electric_and_bimonthly_water(tmp_path):
    target = create_tutorial_database(tmp_path / "tutorial_sessions", "d" * 32)
    connection = sqlite3.connect(str(target))
    try:
        metadata = json.loads(connection.execute(
            "SELECT setting_value FROM settings WHERE setting_key = ?",
            ("__demo_dataset__",),
        ).fetchone()[0])
        available_electric = connection.execute(
            """
            SELECT bill.billing_month, floor.name
            FROM electric_bills AS bill
            JOIN floors AS floor ON floor.id = bill.floor_id
            LEFT JOIN invoice_combination_items AS item ON item.electric_bill_id = bill.id
            WHERE item.id IS NULL
            ORDER BY bill.billing_month, floor.floor_number
            """
        ).fetchall()
        available_water = connection.execute(
            """
            SELECT bill.billing_month, bill.total_amount, COUNT(detail.id)
            FROM water_bills AS bill
            LEFT JOIN invoice_combination_items AS item ON item.water_bill_id = bill.id
            LEFT JOIN water_bill_details AS detail ON detail.water_bill_id = bill.id
            WHERE item.id IS NULL
            GROUP BY bill.id
            """
        ).fetchall()
        available_common = connection.execute(
            """
            SELECT COUNT(*) FROM common_bills AS bill
            LEFT JOIN invoice_combination_items AS item ON item.common_bill_id = bill.id
            WHERE item.id IS NULL
            """
        ).fetchone()[0]
    finally:
        connection.close()

    assert metadata["tutorial_ready"] is True
    assert metadata["tutorial_scenario_version"] == 3
    assert available_electric == [
        ("2026-09-01", "지하 1층"),
        ("2026-09-01", "1층"),
        ("2026-09-01", "2층"),
        ("2026-10-01", "1층"),
        ("2026-10-01", "2층"),
    ]
    assert available_water == [("2026-10-01", 348000, 6)]
    assert available_common == 0


def test_tutorial_invoice_combines_all_floor_electric_and_bimonthly_water(app, client, csrf):
    build_demo(app, date(2026, 4, 1), 6, tutorial_ready=True)
    basement = Floor.query.filter_by(floor_number=-1).one()
    previous = client.get(
        "/get_previous_readings/{}/2026-10".format(basement.id)
    ).get_json()["readings"]
    data = {
        "_csrf_token": csrf,
        "billing_month": "2026-10",
        "floor_id": str(basement.id),
        "tv_distribution_mode": "EQUAL",
        "bill_month_0": "2026-10",
        "bill_amount_0": "248000",
        "bill_welfare_0": "16000",
        "bill_voucher_0": "8000",
        "bill_tv_fee_0": "7500",
    }
    for index, unit in enumerate(u for u in basement.units if not u.is_vacant):
        value = previous[str(unit.id)]
        data["prev_{}".format(unit.id)] = str(value)
        data["curr_{}".format(unit.id)] = str(value + 75 + index * 18)
    calculated = client.post("/calculate/electric", data=data).get_json()
    assert calculated["success"] is True

    electric_bills = (
        ElectricBill.query.filter(~ElectricBill.invoice_items.any())
        .order_by(ElectricBill.billing_month, ElectricBill.floor_id)
        .all()
    )
    water_bill = WaterBill.query.filter(~WaterBill.invoice_items.any()).one()
    assert len(electric_bills) == 6
    assert {(bill.billing_month, bill.floor_ref.floor_number) for bill in electric_bills} == {
        (date(2026, 9, 1), -1), (date(2026, 9, 1), 1), (date(2026, 9, 1), 2),
        (date(2026, 10, 1), -1), (date(2026, 10, 1), 1), (date(2026, 10, 1), 2),
    }
    payload = {
        "_csrf_token": csrf,
        "name": "2026년 10월 정산 (연습)",
        "memo": "모든 층의 두 달치 전기와 격월 수도를 함께 청구",
        "items": [
            {
                "type": "ELECTRIC",
                "id": bill.id,
                "month": bill.billing_month.strftime("%Y-%m"),
                "description": "{} {} 전기요금".format(
                    bill.billing_month.strftime("%Y년 %m월"), bill.floor_ref.name
                ),
            }
            for bill in electric_bills
        ] + [{
            "type": "WATER",
            "id": water_bill.id,
            "month": "2026-10",
            "description": "2026년 9~10월 사용분 수도요금 (10월 고지)",
        }],
        "unit_additional_data": {},
    }
    preview = client.post("/invoice/preview", json=payload).get_json()
    assert preview["success"] is True
    payload["preview_token"] = preview["preview_token"]
    created = client.post("/invoice/create", json=payload).get_json()
    assert created["success"] is True

    items = InvoiceCombinationItem.query.filter_by(combination_id=created["id"]).all()
    invoices = FinalInvoice.query.filter_by(combination_id=created["id"]).all()
    assert sum(item.item_type == "ELECTRIC" for item in items) == 6
    assert sum(item.item_type == "WATER" for item in items) == 1
    assert len(invoices) == 6
    assert {invoice.floor_number_snapshot for invoice in invoices} == {-1, 1, 2}
    assert all(invoice.electric_amount > 0 and invoice.water_amount > 0 for invoice in invoices)
