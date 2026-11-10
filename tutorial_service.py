"""실제 업무 DB와 분리된 튜토리얼 데이터 수명주기."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

from scripts.demo import existing_metadata


REPO_ROOT = Path(__file__).resolve().parent
DEMO_SCRIPT = REPO_ROOT / "scripts" / "demo.py"
SESSION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
STALE_AFTER = timedelta(hours=24)


def _root_path(directory, *, create=False):
    root = Path(directory).expanduser()
    if root.is_symlink():
        raise RuntimeError("튜토리얼 데이터 폴더가 연결된 폴더여서 사용할 수 없습니다.")
    if create:
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:
            pass
    return root.resolve()


def tutorial_database_path(directory, session_id):
    if not SESSION_ID_PATTERN.fullmatch(str(session_id or "")):
        raise ValueError("튜토리얼 세션 식별자가 올바르지 않습니다.")
    root = _root_path(directory, create=True)
    target = (root / ("tutorial-{}.db".format(session_id))).resolve()
    if target.parent != root:
        raise ValueError("튜토리얼 데이터 경로가 올바르지 않습니다.")
    return target


def create_tutorial_database(directory, session_id):
    """기존 더미 생성기를 별도 프로세스로 실행해 전용 DB를 만든다."""
    target = tutorial_database_path(directory, session_id)
    if target.exists():
        if existing_metadata(target) is None:
            raise RuntimeError("기존 튜토리얼 데이터를 확인할 수 없습니다.")
        return target
    environment = dict(os.environ)
    environment.pop("BILLCALC_DATABASE_URI", None)
    environment["BILLCALC_ONBOARDING_ENABLED"] = "0"
    try:
        subprocess.run(
            [sys.executable, str(DEMO_SCRIPT), "seed", "--db-path", str(target)],
            cwd=str(REPO_ROOT), env=environment, check=True,
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        detail = getattr(error, "stderr", "") or getattr(error, "stdout", "") or str(error)
        raise RuntimeError("튜토리얼 데이터를 준비하지 못했습니다: {}".format(detail.strip()[-500:])) from error
    if existing_metadata(target) is None:
        raise RuntimeError("생성된 튜토리얼 데이터를 확인할 수 없습니다.")
    return target


def delete_tutorial_database(directory, session_id):
    """이 생성기의 식별 정보가 있는 현재 세션 파일만 삭제한다."""
    target = tutorial_database_path(directory, session_id)
    if not target.exists():
        return False
    existing_metadata(target)
    sidecars = [Path(str(target) + suffix) for suffix in ("-wal", "-shm", "-journal")]
    if any(path.exists() for path in sidecars):
        try:
            with sqlite3.connect(str(target), timeout=0) as connection:
                mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                if str(mode).lower() != "delete":
                    raise sqlite3.OperationalError("DB in use")
        except sqlite3.Error as error:
            raise RuntimeError(
                "연습 데이터가 아직 사용 중입니다. 연습 화면을 닫은 뒤 다시 시도하세요."
            ) from error
    for sidecar in sidecars:
        sidecar.unlink(missing_ok=True)
    target.unlink()
    return True


def cleanup_stale_tutorial_databases(directory, now=None):
    root = _root_path(directory, create=True)
    cutoff = (now or datetime.now(timezone.utc)) - STALE_AFTER
    removed = 0
    for target in root.glob("tutorial-*.db"):
        match = re.fullmatch(r"tutorial-([0-9a-f]{32})\.db", target.name)
        if not match or target.is_symlink():
            continue
        modified = datetime.fromtimestamp(target.stat().st_mtime, timezone.utc)
        if modified >= cutoff:
            continue
        try:
            removed += int(delete_tutorial_database(root, match.group(1)))
        except (OSError, RuntimeError, ValueError):
            # 식별되지 않은 파일은 자동으로 지우지 않는다.
            continue
    return removed


