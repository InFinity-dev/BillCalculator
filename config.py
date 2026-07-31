"""애플리케이션 설정.

DB 접속 정보를 소스에 하드코딩하지 않기 위한 단일 정의 지점이다.
(codebase-analysis 06-technical-debt.md C-2 해결)

환경변수
--------
BILLCALC_DATABASE_URI  SQLAlchemy URI 를 통째로 지정한다. 최우선.
BILLCALC_DB_PATH       SQLite 파일 경로만 지정한다.
BILLCALC_SECRET_KEY    Flask SECRET_KEY. 미지정 시 프로세스마다 랜덤 생성된다.
BILLCALC_DEBUG         '1'/'true'/'yes' 일 때만 debug 모드.
BILLCALC_HOST          바인딩 주소. 기본값 127.0.0.1 (외부 노출 금지).
BILLCALC_PORT          포트. 기본값 5000.
"""

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

#: 런타임 데이터(DB 파일, 백업, 마이그레이션 리포트)가 놓이는 디렉터리.
DATA_DIR = BASE_DIR / "data"

#: 기본 SQLite 파일 경로.
DEFAULT_DB_PATH = DATA_DIR / "bill_calculator.db"


def _env_flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def resolve_db_path():
    """설정된 SQLite 파일 경로를 반환한다 (URI 를 직접 지정한 경우 None)."""
    if os.environ.get("BILLCALC_DATABASE_URI"):
        return None
    raw = os.environ.get("BILLCALC_DB_PATH")
    if raw:
        return Path(raw).expanduser().resolve()
    return DEFAULT_DB_PATH


def resolve_database_uri():
    """SQLAlchemy 데이터베이스 URI 를 반환한다.

    우선순위: BILLCALC_DATABASE_URI > BILLCALC_DB_PATH > data/bill_calculator.db
    """
    uri = os.environ.get("BILLCALC_DATABASE_URI")
    if uri:
        return uri
    return "sqlite:///" + str(resolve_db_path())


def ensure_data_dir():
    """DB 파일이 놓일 디렉터리를 생성한다. 이미 있으면 아무 것도 하지 않는다."""
    path = resolve_db_path()
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.parent


class Config:
    """기본(운영) 설정."""

    SECRET_KEY = os.environ.get("BILLCALC_SECRET_KEY") or secrets.token_hex(32)

    SQLALCHEMY_DATABASE_URI = resolve_database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # SQLite 는 커넥션 풀 pre-ping / recycle 이 의미가 없다.
    # check_same_thread=False 는 Flask 개발 서버가 threaded 로 동작하기 때문에 필요하다.
    SQLALCHEMY_ENGINE_OPTIONS = {
        "connect_args": {"check_same_thread": False},
    }

    #: SQLite PRAGMA. extensions.py 의 connect 훅이 적용한다.
    #: 값 선정 근거는 docs/refactoring/database/01-target-sqlite-schema.md 2.1 참조.
    SQLITE_PRAGMAS = {
        "foreign_keys": "ON",      # SQLite 기본값은 OFF 다. 반드시 켜야 FK 가 검증된다.
        "journal_mode": "WAL",     # 동시 읽기가 잦은 접근 패턴 때문에 선택
        "synchronous": "FULL",     # 금액 데이터이므로 최신 트랜잭션 유실을 허용하지 않는다
        "busy_timeout": "5000",
    }

    DEBUG = _env_flag("BILLCALC_DEBUG", False)
    HOST = os.environ.get("BILLCALC_HOST", "127.0.0.1")
    PORT = int(os.environ.get("BILLCALC_PORT", "5000"))


class TestConfig(Config):
    """테스트 설정.

    DB 경로는 테스트가 tmp_path 로 주입한다.
    WAL 은 파일 기반 임시 DB 에서도 그대로 검증한다 (실제 런타임 설정을 그대로 확인하기 위함).
    """

    TESTING = True
    SECRET_KEY = "test-secret-key"
    WTF_CSRF_ENABLED = False
