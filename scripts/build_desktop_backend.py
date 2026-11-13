#!/usr/bin/env python3
"""현재 운영체제용 PyInstaller 백엔드 실행 파일을 재현 가능하게 빌드한다."""

from pathlib import Path
import os
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "desktop" / "backend.spec"
OUTPUT = ROOT / "build" / "backend"
WORK = ROOT / "build" / "pyinstaller"


def main():
    for directory in (OUTPUT, WORK):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(OUTPUT),
        "--workpath",
        str(WORK),
        str(SPEC),
    ]
    environment = dict(os.environ)
    environment["PYINSTALLER_CONFIG_DIR"] = str(ROOT / "build" / "pyinstaller-config")
    subprocess.run(command, cwd=str(ROOT), env=environment, check=True)
    executable = OUTPUT / (
        "billcalculator-backend.exe" if sys.platform == "win32" else "billcalculator-backend"
    )
    if not executable.is_file():
        raise RuntimeError("백엔드 실행 파일이 생성되지 않았습니다: {}".format(executable))
    print(executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
