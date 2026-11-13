"""격리된 실제 화면 튜토리얼 서버의 프로세스 수명주기."""

from dataclasses import dataclass
import atexit
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parent
APP_SCRIPT = REPO_ROOT / "app.py"
_LOCK = threading.RLock()
_PROCESSES = {}


def server_command():
    """소스 실행과 PyInstaller 실행에서 각각 올바른 서버 명령을 만든다."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "serve"]
    return [sys.executable, str(APP_SCRIPT)]


@dataclass
class TutorialProcess:
    session_id: str
    port: int
    database_path: Path
    process: subprocess.Popen
    output: object = None


def _output_tail(output, limit=2000):
    if output is None:
        return ""
    try:
        output.flush()
        output.seek(0)
        return output.read().decode("utf-8", errors="replace")[-limit:].strip()
    except (OSError, ValueError):
        return ""


def _terminate_process(process, timeout):
    """PyInstaller 부트로더를 포함한 튜토리얼 프로세스를 종료한다."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )
        process.wait(timeout=timeout)
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def _port_is_available(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", int(port)))
        return True
    except (OSError, TypeError, ValueError):
        return False


def _available_port(preferred_port=None):
    if preferred_port and _port_is_available(preferred_port):
        return int(preferred_port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_ready(port, process, timeout=12):
    deadline = time.monotonic() + timeout
    health_url = "http://127.0.0.1:{}/tutorial/health".format(port)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("연습 화면 서버가 시작 중 종료되었습니다.")
        try:
            with urlopen(health_url, timeout=0.4) as response:
                if response.status == 200:
                    return
        except (OSError, URLError):
            time.sleep(0.08)
    raise RuntimeError("연습 화면 서버가 제한 시간 안에 시작되지 않았습니다.")


def launch_tutorial_process(
    session_id, database_path, main_url, control_token, preferred_port=None,
    had_user_data=False,
):
    """현재 세션 전용 Flask 프로세스를 시작하고 브라우저 주소를 반환한다."""
    database_path = Path(database_path).resolve()
    with _LOCK:
        existing = _PROCESSES.get(session_id)
        if existing and existing.process.poll() is None:
            return existing
        if existing:
            _PROCESSES.pop(session_id, None)

        port = _available_port(preferred_port)
        environment = dict(os.environ)
        environment.pop("BILLCALC_DATABASE_URI", None)
        environment.update({
            "BILLCALC_DB_PATH": str(database_path),
            "BILLCALC_HOST": "127.0.0.1",
            "BILLCALC_PORT": str(port),
            "BILLCALC_DEBUG": "0",
            "BILLCALC_ONBOARDING_ENABLED": "0",
            "BILLCALC_TUTORIAL_MODE": "1",
            "BILLCALC_TUTORIAL_MAIN_URL": main_url.rstrip("/"),
            "BILLCALC_TUTORIAL_CONTROL_TOKEN": control_token,
            "BILLCALC_TUTORIAL_HAD_USER_DATA": "1" if had_user_data else "0",
            "BILLCALC_TUTORIAL_RUN_ID": session_id,
            "BILLCALC_SECRET_KEY": secrets.token_hex(32),
            "PYTHONIOENCODING": "utf-8",
        })
        if getattr(sys, "frozen", False):
            # PyInstaller 6.9+ onefile 실행 파일이 자기 자신을 장시간 실행되는
            # 별도 앱으로 띄울 때는 새 압축 해제 환경을 명시해야 한다.
            # 그렇지 않으면 특히 Windows에서 부모의 임시 번들을 재사용하다
            # 튜토리얼 서버가 시작 직후 종료될 수 있다.
            environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        output = tempfile.TemporaryFile(mode="w+b")
        try:
            process = subprocess.Popen(
                server_command(),
                cwd=str(REPO_ROOT),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception:
            output.close()
            raise
        running = TutorialProcess(session_id, port, database_path, process, output)
        _PROCESSES[session_id] = running
        try:
            _wait_until_ready(port, process)
        except Exception as error:
            detail = _output_tail(output)
            stop_tutorial_process(session_id)
            if detail:
                raise RuntimeError("{}: {}".format(error, detail)) from error
            raise
        return running


def stop_tutorial_process(session_id, timeout=4):
    """직접 시작해 보관 중인 해당 세션 프로세스만 종료한다."""
    with _LOCK:
        running = _PROCESSES.pop(session_id, None)
    if not running:
        return False
    try:
        if running.process.poll() is None:
            _terminate_process(running.process, timeout)
    finally:
        if running.output is not None:
            running.output.close()
    return True


def stop_all_tutorial_processes():
    with _LOCK:
        session_ids = list(_PROCESSES)
    for session_id in session_ids:
        try:
            stop_tutorial_process(session_id)
        except OSError:
            pass


atexit.register(stop_all_tutorial_processes)
