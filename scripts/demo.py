#!/usr/bin/env python
"""더미 DB 생성과 실행. 일반 app.py 실행에는 영향을 주지 않는다."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_DB = REPO_ROOT / "data" / "demo.db"
DEMO_APPLICATION_ID = 0x4243444D
DEFAULT_START = "2026-04"
DEFAULT_MONTHS = 6


def parse_month(value):
    try:
        if not re.fullmatch(r"\d{4}-\d{2}", value):
            raise ValueError()
        return date.fromisoformat(value + "-01")
    except ValueError:
        raise argparse.ArgumentTypeError("시작월은 YYYY-MM 형식이어야 합니다.")


def parse_months(value):
    try:
        number = int(value)
        if not 1 <= number <= 36:
            raise ValueError()
        return number
    except ValueError:
        raise argparse.ArgumentTypeError("개월 수는 1~36이어야 합니다.")


def existing_metadata():
    """생성기가 만든 DB만 사용·초기화할 수 있다. 다른 파일은 읽기만 한다."""
    if DEMO_DB.is_symlink() or (DEMO_DB.exists() and DEMO_DB.stat().st_nlink > 1):
        raise RuntimeError("연결된 DB 파일은 더미 DB로 사용할 수 없습니다.")
    if not DEMO_DB.exists():
        return None
    try:
        # 식별 정보는 생성 완료 시 본 파일에 확정된다. 확인 자체가 WAL 파일을 만들지 않게 한다.
        with closing(sqlite3.connect(DEMO_DB.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            if connection.execute("PRAGMA application_id").fetchone()[0] != DEMO_APPLICATION_ID:
                raise RuntimeError("demo.db가 이 생성기로 만든 테스트 DB가 아닙니다. 기존 파일을 보존합니다.")
            row = connection.execute("SELECT setting_value FROM settings WHERE setting_key='__demo_dataset__'").fetchone()
            metadata = json.loads(row[0]) if row else None
            if not isinstance(metadata, dict) or metadata.get("version") != 1:
                raise RuntimeError("더미 DB 식별 정보가 올바르지 않습니다. 기존 파일을 보존합니다.")
            return metadata
    except (sqlite3.Error, ValueError, TypeError) as error:
        raise RuntimeError("demo.db를 테스트 DB로 확인할 수 없습니다. 기존 파일을 보존합니다.") from error


def _check_sidecars():
    sidecars = [Path(str(DEMO_DB) + suffix) for suffix in ("-wal", "-shm", "-journal")]
    if not any(path.exists() for path in sidecars):
        return
    # 일부 SQLite 빌드는 정상 종료 뒤에도 빈 WAL/SHM을 남긴다.
    # 생성기 식별과 SQLite의 배타적 모드 전환이 성공한 경우에만 정리한다.
    if existing_metadata() is None:
        raise RuntimeError("테스트 DB의 복구 파일이 남아 있습니다. 기존 파일을 보존합니다.")
    try:
        with closing(sqlite3.connect(str(DEMO_DB), timeout=0)) as connection:
            if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise sqlite3.OperationalError("DB in use")
    except sqlite3.Error as error:
        raise RuntimeError("테스트 DB가 사용 중입니다. 테스트 서버를 종료한 뒤 다시 실행하세요.") from error
    for path in sidecars:
        path.unlink(missing_ok=True)


def _target_identity():
    if not DEMO_DB.exists():
        return None
    info = DEMO_DB.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _configure_database(path):
    # URI가 DB_PATH보다 우선하므로 상속받은 실제 DB 설정을 먼저 해제한다.
    os.environ.pop("BILLCALC_DATABASE_URI", None)
    os.environ["BILLCALC_DB_PATH"] = str(path)
    os.environ["BILLCALC_HOST"] = "127.0.0.1"
    os.environ["BILLCALC_PORT"] = "5001"
    os.environ["BILLCALC_DEBUG"] = "0"
    sys.path.insert(0, str(REPO_ROOT))


def seed(start, months, reset=False):
    if reset:
        _check_sidecars()
    existing = existing_metadata()
    if existing and not reset:
        print("기존 더미 데이터 유지: {}부터 {}개월. 다시 만들려면 seed --reset을 사용하세요.".format(
            existing["start_month"], existing["months"]), flush=True)
        return
    _check_sidecars()
    original_identity = _target_identity()
    # 연도 범위를 벗어나는 요청도 DB를 생성하기 전에 거부한다.
    final_number = start.year * 12 + start.month - 1 + months
    year, month = divmod(final_number, 12)
    date(year, month + 1, 5)
    DEMO_DB.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".demo-seed-", dir=str(DEMO_DB.parent)) as directory:
        staged = Path(directory) / "demo.db"
        _configure_database(staged)
        import app as app_module
        from flask_migrate import upgrade
        from demo_data import build_demo
        from db_validate import run_all_checks
        from extensions import db

        with app_module.app.app_context():
            try:
                upgrade(directory=str(REPO_ROOT / "migrations"))
                app_module.seed_settings()
                build_demo(app_module.app, start, months)
                checks = run_all_checks()
                failed = [check.name for check in checks if not check.ok]
                if failed:
                    raise RuntimeError("더미 DB 정합성 검사 실패: " + ", ".join(failed))
            finally:
                db.session.remove()
                db.engine.dispose()
        with closing(sqlite3.connect(str(staged))) as connection:
            connection.execute("PRAGMA application_id = {}".format(DEMO_APPLICATION_ID))
            if connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                raise RuntimeError("생성한 테스트 DB 저장을 완료할 수 없습니다.")
            connection.execute("PRAGMA journal_mode=DELETE")
        # 생성 중 다른 프로세스가 대상 DB를 사용하거나 바꿨으면 교체하지 않는다.
        _check_sidecars()
        if existing_metadata() != existing or _target_identity() != original_identity:
            raise RuntimeError("생성 중 대상 DB가 바뀌었습니다. 기존 파일을 보존합니다.")
        os.replace(str(staged), str(DEMO_DB))
    print("더미 DB 생성 완료: {} / {}부터 {}개월 / 정합성 검사 {}개 통과".format(
        DEMO_DB, start.strftime("%Y-%m"), months, len(checks)), flush=True)


def run(port):
    if existing_metadata() is None:
        # 생성 과정의 임시 DB 설정이 실행 서버에 남지 않도록 별도 프로세스에서 생성한다.
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "seed"], check=True)
    _configure_database(DEMO_DB)
    os.environ["BILLCALC_DEBUG"] = "0"
    import app as app_module
    app_module.app.config["DEMO_MODE"] = True
    print("테스트 데이터 사용 중: {} / http://127.0.0.1:{}".format(DEMO_DB, port), flush=True)
    app_module.app.run(host="127.0.0.1", port=port, debug=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description="더미 DB만 사용하는 데이터 생성·테스트 실행")
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("seed", help="6개월 더미 데이터 생성 (기존 데이터 유지)")
    generate.add_argument("--start-month", type=parse_month, default=DEFAULT_START)
    generate.add_argument("--months", type=parse_months, default=DEFAULT_MONTHS)
    generate.add_argument("--reset", action="store_true", help="생성기가 만든 더미 DB를 초기 상태로 다시 생성")
    server = commands.add_parser("run", help="테스트 서버 실행 (DB가 없으면 기본 데이터 생성)")
    server.add_argument("--port", type=int, choices=range(1, 65536), default=5001, metavar="PORT")
    args = parser.parse_args(argv)
    try:
        if args.command == "seed":
            seed(args.start_month, args.months, args.reset)
        else:
            run(args.port)
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as error:
        print("오류: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
