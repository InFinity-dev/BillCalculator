"""업무 DB 복원과 분리된 첫 실행 안내 완료 상태."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile


STATE_VERSION = 1
TUTORIAL_VERSION = 1


def _state_path(path):
    target = Path(path).expanduser()
    if target.is_symlink():
        raise RuntimeError("앱 상태 파일이 연결된 파일이어서 사용할 수 없습니다.")
    return target


def read_app_state(path):
    """알 수 없는 키를 보존할 수 있도록 전체 상태 사전을 반환한다."""
    target = _state_path(path)
    if not target.exists():
        return {"version": STATE_VERSION}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("앱 상태 파일을 읽을 수 없습니다.") from error
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        raise RuntimeError("앱 상태 파일 형식이 올바르지 않습니다.")
    return payload


def onboarding_completed(path):
    try:
        state = read_app_state(path)
    except RuntimeError:
        return False
    onboarding = state.get("onboarding")
    return (
        isinstance(onboarding, dict)
        and onboarding.get("status") == "completed"
    )


def mark_onboarding_completed(path):
    """동일 디렉터리의 임시 파일을 원자적으로 교체한다."""
    target = _state_path(path)
    state = read_app_state(target)
    state["onboarding"] = {
        "status": "completed",
        "tutorial_version": TUTORIAL_VERSION,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".app-state-", suffix=".json",
            dir=str(target.parent), delete=False,
        ) as temporary:
            json.dump(state, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, target)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
    return state
