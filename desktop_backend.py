"""Electron에 포함되는 BillCalculator 로컬 백엔드 실행기.

일반 웹 실행(`python app.py`)과 달리 데스크톱 앱은 이 진입점을 사용한다.
사용자 데이터 폴더의 SQLite를 마이그레이션·검증한 뒤 loopback 주소에서만
Waitress를 실행하며, 같은 실행 파일로 튜토리얼 서버와 데모 DB 생성도 처리한다.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import errno
import hmac
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading


def _configure_standard_streams(streams=None):
    """Windows 패키지에서도 한국어 로그를 UTF-8로 안전하게 기록한다."""
    for stream in streams or (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            continue


def resource_root() -> Path:
    """소스와 PyInstaller 번들에서 공통으로 리소스 루트를 반환한다."""
    return Path(__file__).resolve().parent


def migration_directory() -> Path:
    return resource_root() / "migrations"


def _current_revision(database_path: Path):
    if not database_path.exists() or database_path.stat().st_size == 0:
        return None
    try:
        with sqlite3.connect(str(database_path)) as connection:
            row = connection.execute(
                "SELECT version_num FROM alembic_version LIMIT 1"
            ).fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


def _has_user_tables(database_path: Path) -> bool:
    if not database_path.exists() or database_path.stat().st_size == 0:
        return False
    try:
        with sqlite3.connect(str(database_path)) as connection:
            return connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                  AND name != 'alembic_version'
                LIMIT 1
                """
            ).fetchone() is not None
    except sqlite3.Error:
        return True


def _migration_head() -> str:
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    directory = migration_directory()
    configuration = AlembicConfig(str(directory / "alembic.ini"))
    configuration.set_main_option("script_location", str(directory))
    return ScriptDirectory.from_config(configuration).get_current_head()


def _backup_before_migration(database_path: Path, current, head):
    if current == head or not _has_user_tables(database_path):
        return None
    from db_backup import backup_database

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    destination = database_path.parent / "backups" / (
        "before_desktop_migration_{}.db".format(stamp)
    )
    return backup_database(output=destination)


def bootstrap_database(application):
    """사용자 DB를 백업·업그레이드·시딩한 뒤 전체 정합성을 확인한다."""
    from config import resolve_db_path
    from db_validate import run_all_checks
    from flask_migrate import upgrade
    import app as app_module

    database_path = resolve_db_path()
    if database_path is None:
        raise RuntimeError("데스크톱 앱은 파일 기반 SQLite 경로가 필요합니다.")
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    head = _migration_head()
    current = _current_revision(database_path)
    backup_path = _backup_before_migration(database_path, current, head)
    if backup_path:
        print("BILLCALC_MIGRATION_BACKUP={}".format(backup_path), flush=True)

    with application.app_context():
        upgrade(directory=str(migration_directory()))
        app_module.seed_settings()
        failed = [check for check in run_all_checks() if not check.ok]
        if failed:
            raise RuntimeError(
                "DB 정합성 검사 실패: {}".format(
                    ", ".join("{} ({})".format(check.name, check.detail) for check in failed)
                )
            )


def _install_desktop_routes(application, shutdown_token, shutdown_callback):
    from flask import abort, jsonify, request

    @application.get("/__desktop__/health")
    def desktop_health():
        if not application.config.get("DESKTOP_MODE"):
            abort(404)
        return jsonify(
            {
                "success": True,
                "version": application.config.get("APP_VERSION", ""),
                "tutorial": bool(application.config.get("TUTORIAL_MODE")),
            }
        )

    @application.post("/__desktop__/shutdown")
    def desktop_shutdown():
        if not application.config.get("DESKTOP_MODE") or not shutdown_token:
            abort(404)
        supplied = request.headers.get("X-BillCalculator-Shutdown", "")
        if not hmac.compare_digest(str(supplied), str(shutdown_token)):
            abort(403)
        threading.Thread(target=shutdown_callback, daemon=True).start()
        return jsonify({"success": True})


def run_server(host: str, port: int) -> int:
    os.environ["BILLCALC_HOST"] = host
    os.environ["BILLCALC_PORT"] = str(port)
    os.environ["BILLCALC_DEBUG"] = "0"

    import app as app_module
    from extensions import db
    from tutorial_service import cleanup_owned_tutorial_databases
    from tutorial_runtime import stop_all_tutorial_processes
    from waitress import create_server

    application = app_module.app
    bootstrap_database(application)

    server_holder = {}
    shutdown_started = threading.Event()

    def shutdown():
        if shutdown_started.is_set():
            return
        shutdown_started.set()
        stop_all_tutorial_processes()
        if not application.config.get("TUTORIAL_MODE"):
            cleanup_owned_tutorial_databases(application.config["TUTORIAL_DATA_DIR"])
        server = server_holder.get("server")
        if server is not None:
            server.close()

    shutdown_token = os.environ.get("BILLCALC_DESKTOP_SHUTDOWN_TOKEN", "")
    _install_desktop_routes(application, shutdown_token, shutdown)
    server = create_server(
        application,
        host=host,
        port=port,
        threads=8,
        ident="BillCalculator",
        clear_untrusted_proxy_headers=True,
    )
    server_holder["server"] = server

    def handle_signal(_signum, _frame):
        threading.Thread(target=shutdown, daemon=True).start()

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), handle_signal)

    print("BILLCALC_READY=http://{}:{}".format(host, port), flush=True)
    try:
        try:
            server.run()
        except OSError as error:
            # Waitress의 asyncore loop가 다른 스레드에서 닫힌 listen socket을 한 번
            # 더 poll할 수 있다. 명시적인 종료 중 EBADF만 정상 종료로 처리한다.
            if not shutdown_started.is_set() or error.errno != errno.EBADF:
                raise
    finally:
        stop_all_tutorial_processes()
        with application.app_context():
            db.session.remove()
            db.engine.dispose()
    return 0


def seed_demo(database_path: Path, tutorial_ready=False) -> int:
    from datetime import date
    from scripts.demo import seed

    seed(
        date(2026, 4, 1),
        6,
        reset=False,
        db_path=database_path,
        tutorial_ready=tutorial_ready,
    )
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="BillCalculator 데스크톱 백엔드")
    commands = parser.add_subparsers(dest="command")

    serve = commands.add_parser("serve", help="로컬 웹 백엔드 실행")
    serve.add_argument("--host", default=os.environ.get("BILLCALC_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("BILLCALC_PORT", "5000")))

    demo = commands.add_parser("seed-demo", help="튜토리얼용 데모 DB 생성")
    demo.add_argument("--db-path", type=Path, required=True)
    demo.add_argument("--tutorial-ready", action="store_true")
    return parser


def main(argv=None):
    _configure_standard_streams()
    parser = build_parser()
    arguments = parser.parse_args(argv)
    command = arguments.command or "serve"
    if command == "serve":
        host = getattr(arguments, "host", os.environ.get("BILLCALC_HOST", "127.0.0.1"))
        port = getattr(arguments, "port", int(os.environ.get("BILLCALC_PORT", "5000")))
        if host not in {"127.0.0.1", "localhost"}:
            parser.error("데스크톱 백엔드는 loopback 주소에서만 실행할 수 있습니다.")
        if not 1 <= port <= 65535:
            parser.error("포트는 1~65535 범위여야 합니다.")
        return run_server(host, port)
    if command == "seed-demo":
        return seed_demo(arguments.db_path, tutorial_ready=arguments.tutorial_ready)
    parser.error("지원하지 않는 명령입니다.")


if __name__ == "__main__":
    raise SystemExit(main())
