"""격리된 온보딩 데이터, 자동 표시, 완료·재실행 정책을 검증한다."""

from contextlib import contextmanager
from datetime import date
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from extensions import db
from itsdangerous import URLSafeTimedSerializer
from models import Floor, Unit
from onboarding_state import onboarding_completed
from scripts import demo
from tests.test_frontend import AssetParser
from tutorial_service import (
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


def test_live_tutorial_wraps_actual_pages_with_bright_guide(app, client):
    floor = Floor(floor_number=1, name="연습 1층")
    db.session.add(floor)
    db.session.flush()
    db.session.add(Unit(floor_id=floor.id, unit_name="연습 101호", residents_count=2))
    db.session.commit()
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
    finally:
        app.config.update(previous)

    assert "연습 데이터 사용 중 · 실제 데이터와 완전히 분리되어 있습니다" in body
    assert "실제 화면 따라하기" in body
    assert 'data-tutorial-target="unit-management"' in body
    assert 'data-tutorial-nav="settings"' in body
    assert 'data-tutorial-nav="tutorial"' in body
    assert "signed-token" in body
    parsed = AssetParser()
    parsed.feed(body)
    assert "/static/css/tutorial_overlay.css" in parsed.assets
    assert "/static/js/tutorial_overlay.js" in parsed.assets
    assert all(client.get(asset).status_code == 200 for asset in parsed.assets)


def test_signed_tutorial_control_defer_stops_only_current_session(app, client, tmp_path, monkeypatch):
    with onboarding_config(app, tmp_path), fake_tutorial_runtime(monkeypatch) as (_launched, stopped):
        tutorial_id = "b" * 32
        with client.session_transaction() as session:
            session["tutorial_id"] = tutorial_id
        token = URLSafeTimedSerializer(
            app.secret_key, salt="live-tutorial-control-v1"
        ).dumps({"tutorial_id": tutorial_id})
        response = client.get("/tutorial/control/defer", query_string={"token": token})
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/")
        assert stopped == [tutorial_id]
        with client.session_transaction() as session:
            assert session["tutorial_id"] == tutorial_id
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


def test_demo_cli_forwards_custom_database_path(monkeypatch, tmp_path):
    captured = {}

    def fake_seed(start, months, reset=False, db_path=None):
        captured.update(start=start, months=months, reset=reset, db_path=db_path)

    monkeypatch.setattr(demo, "seed", fake_seed)
    target = tmp_path / "custom.db"
    assert demo.main(["seed", "--start-month", "2026-11", "--months", "2", "--reset", "--db-path", str(target)]) == 0
    assert captured == {
        "start": date(2026, 11, 1), "months": 2, "reset": True, "db_path": target,
    }
