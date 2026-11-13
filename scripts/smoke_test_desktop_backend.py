#!/usr/bin/env python3
"""빌드된 백엔드를 실제 실행해 초기 DB·HTTP·안전 종료를 확인한다."""

from contextlib import closing
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent.parent


def available_port():
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def backend_path():
    return ROOT / "build" / "backend" / (
        "billcalculator-backend.exe" if sys.platform == "win32" else "billcalculator-backend"
    )


def wait_for_health(url, process, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output, _ = process.communicate()
            raise RuntimeError("백엔드가 조기에 종료되었습니다:\n{}".format(output[-4000:]))
        try:
            with urlopen(url + "/__desktop__/health", timeout=0.5) as response:
                if json.load(response).get("success"):
                    return
        except (OSError, URLError, ValueError):
            time.sleep(0.1)
    raise RuntimeError("백엔드가 제한 시간 안에 준비되지 않았습니다.")


def main():
    executable = backend_path()
    if not executable.is_file():
        raise RuntimeError("빌드된 백엔드를 찾을 수 없습니다: {}".format(executable))
    with tempfile.TemporaryDirectory(prefix="billcalc-desktop-smoke-") as directory:
        data = Path(directory)
        database = data / "bill_calculator.db"
        port = available_port()
        token = "desktop-smoke-shutdown-token"
        environment = dict(
            os.environ,
            BILLCALC_DB_PATH=str(database),
            BILLCALC_ONBOARDING_STATE_PATH=str(data / "app_state.json"),
            BILLCALC_TUTORIAL_DATA_DIR=str(data / "tutorial_sessions"),
            BILLCALC_DESKTOP_MODE="1",
            BILLCALC_DESKTOP_SHUTDOWN_TOKEN=token,
            BILLCALC_SECRET_KEY="desktop-smoke-secret",
            BILLCALC_ONBOARDING_ENABLED="0",
        )
        process = subprocess.Popen(
            [str(executable), "serve", "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(ROOT),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        url = "http://127.0.0.1:{}".format(port)
        try:
            wait_for_health(url, process)
            with urlopen(url + "/", timeout=2) as response:
                if response.status != 200:
                    raise RuntimeError("첫 화면 응답이 올바르지 않습니다.")
            with sqlite3.connect(str(database)) as connection:
                revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
                settings = connection.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
                floors = connection.execute("SELECT COUNT(*) FROM floors").fetchone()[0]
            if revision != "0004" or settings != 11 or floors != 0:
                raise RuntimeError(
                    "초기 DB가 올바르지 않습니다: revision={}, settings={}, floors={}".format(
                        revision, settings, floors
                    )
                )
            request = Request(
                url + "/__desktop__/shutdown",
                data=b"",
                headers={"X-BillCalculator-Shutdown": token},
                method="POST",
            )
            with urlopen(request, timeout=2) as response:
                if not json.load(response).get("success"):
                    raise RuntimeError("안전 종료 요청이 거부되었습니다.")
            process.wait(timeout=10)
            if process.returncode != 0:
                output = process.stdout.read() if process.stdout else ""
                raise RuntimeError(
                    "백엔드 종료 코드가 올바르지 않습니다: {}\n{}".format(
                        process.returncode, output[-4000:]
                    )
                )
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        print("desktop backend smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
