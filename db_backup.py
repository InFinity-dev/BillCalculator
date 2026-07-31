"""SQLite 백업/복원.

실행 중인 DB 파일을 단순 복사하는 방식은 WAL 모드에서 안전하지 않다
(``-wal`` 사이드카에 아직 체크포인트되지 않은 트랜잭션이 남아 있을 수 있다).
따라서 SQLite 공식 온라인 백업 API(``sqlite3.Connection.backup``)를 사용한다.
이 API 는 백업 도중의 쓰기까지 일관되게 처리한다.

향후 UI 기능으로 확장할 수 있도록 경계만 함수로 노출한다.
"""

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from config import DATA_DIR, resolve_db_path

BACKUP_DIR = DATA_DIR / "backups"


class BackupError(RuntimeError):
    pass


def _require_sqlite_path():
    path = resolve_db_path()
    if path is None:
        raise BackupError(
            "BILLCALC_DATABASE_URI 로 URI 를 직접 지정한 경우에는 파일 백업을 지원하지 않습니다."
        )
    return Path(path)


def backup_database(output=None, timestamp=None):
    """현재 DB 를 안전하게 백업하고 백업 파일 경로를 반환한다.

    Parameters
    ----------
    output : str | Path | None
        저장 경로. None 이면 ``data/backups/bill_calculator_<YYYYmmdd_HHMMSS>.db``.
    timestamp : datetime | None
        파일명에 쓸 시각. 테스트에서 주입할 수 있게 열어 둔다.
    """
    src_path = _require_sqlite_path()
    if not src_path.exists():
        raise BackupError("DB 파일이 존재하지 않습니다: {}".format(src_path))

    if output is None:
        stamp = (timestamp or datetime.now()).strftime("%Y%m%d_%H%M%S")
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        out_path = BACKUP_DIR / "bill_calculator_{}.db".format(stamp)
    else:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)

    source = sqlite3.connect(str(src_path))
    target = sqlite3.connect(str(out_path))
    try:
        with target:
            source.backup(target)
    finally:
        target.close()
        source.close()

    verify_backup(out_path)
    return out_path


def verify_backup(path):
    """백업 파일이 열리고 무결성 검사를 통과하는지 확인한다."""
    conn = sqlite3.connect(str(path))
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise BackupError("백업 파일 무결성 검사 실패: {}".format(result))
    finally:
        conn.close()
    return True


def restore_database(backup_path, keep_previous=True):
    """백업 파일로 현재 DB 를 복원한다.

    복원 전에 백업 파일의 무결성을 먼저 확인하고,
    기존 DB 는 기본적으로 ``.pre-restore`` 사본으로 남긴다.

    주의: 애플리케이션이 실행 중이 아닐 때 호출해야 한다.
    """
    backup_path = Path(backup_path)
    if not backup_path.exists():
        raise BackupError("백업 파일이 없습니다: {}".format(backup_path))
    verify_backup(backup_path)

    dest_path = _require_sqlite_path()
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    if keep_previous and dest_path.exists():
        shutil.copy2(str(dest_path), str(dest_path) + ".pre-restore")

    # WAL 사이드카가 남아 있으면 복원 결과와 섞일 수 있으므로 함께 정리한다.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(dest_path) + suffix)
        if sidecar.exists():
            sidecar.unlink()

    source = sqlite3.connect(str(backup_path))
    target = sqlite3.connect(str(dest_path))
    try:
        with target:
            source.backup(target)
    finally:
        target.close()
        source.close()

    return dest_path
