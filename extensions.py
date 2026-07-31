"""Flask 확장 인스턴스와 SQLite 커넥션 설정.

``db`` 를 app 과 분리해 두어야 Alembic(migrations/env.py)이 애플리케이션을
기동하지 않고도 메타데이터를 참조할 수 있다.
"""

import sqlite3

from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import MetaData, event, text
from sqlalchemy.engine import Engine

#: 제약 이름을 결정적으로 만든다.
#: SQLite 는 ALTER TABLE 로 제약을 바꿀 수 없어 Alembic 이 batch 모드(테이블 재생성)를
#: 사용하는데, 이때 이름이 없는 제약은 재생성 과정에서 유실된다.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

db = SQLAlchemy(metadata=MetaData(naming_convention=NAMING_CONVENTION))
migrate = Migrate()


def _apply_sqlite_pragmas(dbapi_connection, pragmas):
    """커넥션 하나에 PRAGMA 를 적용한다.

    SQLite 의 ``foreign_keys`` 는 **커넥션 단위** 설정이며 기본값이 OFF 다.
    풀에서 커넥션을 새로 열 때마다 반드시 다시 켜야 한다.
    """
    cursor = dbapi_connection.cursor()
    try:
        for name, value in pragmas.items():
            cursor.execute("PRAGMA {} = {}".format(name, value))
    finally:
        cursor.close()


_pragma_hook_registered = False


def register_sqlite_pragmas(pragmas):
    """SQLite 커넥션이 열릴 때마다 PRAGMA 를 적용하는 훅을 등록한다.

    엔진 단위가 아니라 ``Engine`` 클래스 전역에 한 번만 등록한다.
    테스트가 앱을 여러 번 만들어도 훅이 중복 등록되지 않도록 가드한다.
    """
    global _pragma_hook_registered
    if _pragma_hook_registered:
        return
    _pragma_hook_registered = True

    @event.listens_for(Engine, "connect")
    def _on_connect(dbapi_connection, connection_record):
        # sqlite3 커넥션에만 적용한다.
        # (레거시 마이그레이션 스크립트는 같은 프로세스에서 MySQL 커넥션도 연다)
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
        _apply_sqlite_pragmas(dbapi_connection, pragmas)


def read_pragma(name):
    """현재 세션 커넥션에서 PRAGMA 값을 읽는다. 테스트/검증용."""
    return db.session.execute(text("PRAGMA {}".format(name))).scalar()
