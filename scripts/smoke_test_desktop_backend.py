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
from http.cookiejar import CookieJar
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen


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
            BILLCALC_APP_VERSION="smoke-test",
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
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        try:
            wait_for_health(url, process)
            with opener.open(url + "/", timeout=2) as response:
                if response.status != 200:
                    raise RuntimeError("첫 화면 응답이 올바르지 않습니다.")
            # 패키징된 실행 파일이 다시 자기 자신을 실행해 만드는 튜토리얼
            # 서버까지 검증한다. Windows에서 이 경로의 화면 이동과 정적 자원
            # 로딩이 실제 릴리즈 전에 깨졌는지 확인할 수 있다.
            with opener.open(url + "/tutorial", timeout=150) as response:
                tutorial_url = response.geturl()
                tutorial_body = response.read().decode("utf-8")
            if urlsplit(tutorial_url).netloc == urlsplit(url).netloc:
                marker = tutorial_body.find("사용 안내를 시작하지 못했습니다")
                if marker >= 0:
                    summary = " ".join(tutorial_body[marker:marker + 2200].split())
                else:
                    summary = " ".join(tutorial_body.split())[-1000:]
                raise RuntimeError(
                    "튜토리얼 전용 서버로 이동하지 않았습니다: {}\n{}".format(
                        tutorial_url, summary
                    )
                )
            required_assets = (
                url + "/static/css/app.css?v=smoke-test",
                url + "/static/css/tutorial_overlay.css?v=smoke-test",
                url + "/static/js/tutorial_overlay.js?v=smoke-test",
            )
            if not all(asset in tutorial_body for asset in required_assets):
                raise RuntimeError("튜토리얼 화면이 메인 앱의 정적 자원을 사용하지 않습니다.")
            for asset in required_assets:
                with opener.open(asset, timeout=5) as response:
                    content = response.read()
                    if response.status != 200 or not content:
                        raise RuntimeError("튜토리얼 정적 자원을 불러오지 못했습니다: {}".format(asset))
            # sqlite3.Connection 컨텍스트 매니저는 트랜잭션만 끝내고 파일
            # 핸들은 닫지 않는다. Windows에서도 임시 DB를 즉시 정리할 수
            # 있도록 연결 수명을 명시적으로 종료한다.
            with closing(sqlite3.connect(str(database))) as connection:
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
            with opener.open(request, timeout=2) as response:
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
                try:
                    cleanup_request = Request(
                        url + "/__desktop__/shutdown",
                        data=b"",
                        headers={"X-BillCalculator-Shutdown": token},
                        method="POST",
                    )
                    with opener.open(cleanup_request, timeout=2):
                        pass
                    process.wait(timeout=10)
                except (OSError, URLError, subprocess.TimeoutExpired):
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
        print("desktop backend smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
