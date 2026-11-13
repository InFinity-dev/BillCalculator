# Electron 데스크톱 앱 빌드·배포

이 문서는 BillCalculator를 Windows와 macOS용 독립 실행 앱으로 만들고 GitHub Releases에
자동 배포하는 현재 절차를 설명한다. 데스크톱 앱도 브라우저 버전과 같은 Flask·SQLite 도메인
코드를 사용하며, 사용자 데이터는 설치 파일이나 앱 번들 안이 아닌 운영체제별 사용자 데이터
폴더에 저장한다.

## 지원 대상

| 운영체제 | 아키텍처 | 배포 파일 | 최소 환경 |
|---|---|---|---|
| Windows | x64 | NSIS 설치 프로그램 `.exe` | Windows 10/11 64비트 |
| macOS | arm64 | Apple Silicon용 `.dmg` | macOS 13 이상 |
| macOS | x64 | Intel Mac용 `.dmg` | macOS 13 이상 |

코드 서명과 Apple 공증은 현재 제품 정책에 따라 적용하지 않는다. 따라서 macOS Gatekeeper와
Windows SmartScreen이 최초 실행을 경고할 수 있다. macOS는 Finder에서 앱을 Control-클릭해
`열기`를 선택하거나 **시스템 설정 → 개인정보 보호 및 보안 → 확인 없이 열기**를 사용하고,
Windows는 SmartScreen의 **추가 정보 → 실행**을 선택해야 할 수 있다.

## 패키지 구조

```text
Electron main process
├── 보안이 적용된 BrowserWindow
├── 운영체제 사용자 데이터 폴더
└── PyInstaller로 묶은 billcalculator-backend
    ├── Flask + Waitress (127.0.0.1 임의 포트)
    ├── Alembic migrations
    ├── templates / static
    └── 튜토리얼 DB 생성·전용 서버 재실행
```

- Electron 렌더러는 Node.js 통합을 사용하지 않고 context isolation·sandbox를 활성화한다.
- 앱 안의 이동은 loopback 주소로 제한하며 외부 요금 조회 링크는 기본 브라우저로 연다.
- Python 백엔드는 외부 네트워크 인터페이스에 바인딩하지 않는다.
- 일회성 포트에서 실행되는 튜토리얼 화면도 CSS·JavaScript는 수명이 긴 메인 백엔드에서
  불러온다. 모든 정적 자원 주소에는 앱 버전을 붙여 업데이트 뒤의 이전 캐시 혼용을 막는다.
- PyInstaller onefile 백엔드가 데모 DB 생성기와 튜토리얼 서버를 다시 실행할 때는 각각 별도의
  압축 해제 환경을 사용한다. Windows에서 부모 프로세스의 임시 번들 재사용으로 하위 작업이
  종료되는 것을 막는다. 데스크톱 백엔드 진입 시 표준 출력·오류 스트림 자체를 UTF-8로
  재설정해 Windows 기본 코드페이지가 한국어 로그 때문에 시작을 중단하지 않게 한다.
- Windows의 onefile 튜토리얼 종료 시 PyInstaller 부트로더와 실제 서버 프로세스 트리를 함께
  정리해 연습 DB 파일 잠금과 앱 종료 지연을 남기지 않는다.
- 튜토리얼 진행도는 실행 ID별 URL·호스트 쿠키·브라우저 저장소에 함께 기록한다. 화면 이동이나
  저장소 접근 제한이 생겨도 나머지 경로로 현재 단계를 복구하며 다른 연습 실행과 섞이지 않는다.
- 앱 시작 시 현재 Alembic head까지 자동 업그레이드하고 설정 기본값 시딩 및 20개 DB 정합성
  검사를 수행한다.
- 기존 DB에 마이그레이션이 필요하면 먼저 `backups/before_desktop_migration_*.db`를 만든다.
- 앱 종료 시 백엔드와 튜토리얼 하위 프로세스를 순서대로 중단하고, 생성기 소유가 확인된 현재
  연습 DB만 삭제한다.
- 설치 제거 시 사용자 데이터는 삭제하지 않는다. 운영 데이터를 지우려면 앱의 백업을 먼저
  확보한 뒤 사용자가 데이터 폴더를 명시적으로 정리해야 한다.

## 사용자 데이터 위치

| 운영체제 | 기본 위치 |
|---|---|
| Windows | `%APPDATA%\BillCalculator\data\bill_calculator.db` |
| macOS | `~/Library/Application Support/BillCalculator/data/bill_calculator.db` |

같은 `data` 폴더에 `app_state.json`, `tutorial_sessions/`, `backups/`가 위치한다. 기존 로컬 웹앱
데이터를 옮길 때 DB 파일을 실행 중에 직접 복사하지 말고, 기존 앱의 **설정 → 가져오기/내보내기**에서
JSON 전체 백업을 만든 뒤 데스크톱 앱에서 복원한다.

## 로컬 개발·빌드

필수 도구는 Node.js 22.12 이상과 Python 3.9 이상이다.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-desktop.txt
npm ci

# 소스 상태에서 Electron 실행
npm run desktop:dev

# 현재 운영체제용 설치 파일 생성
npm run desktop:dist
```

세부 검증 명령:

```bash
python scripts/generate_desktop_icons.py
python scripts/build_desktop_backend.py
python scripts/smoke_test_desktop_backend.py
```

빌드 결과는 `dist/`에 생성되고 Git에는 포함하지 않는다. Python 백엔드는 현재 운영체제와 CPU
아키텍처에서 만들어야 하므로 Windows·macOS 산출물을 한 운영체제에서 교차 빌드하지 않는다.

## GitHub Releases 자동 배포

`.github/workflows/desktop-release.yml`은 다음 순서로 실행된다.

1. 전체 Python 테스트 실행
2. Windows x64, macOS Apple Silicon, macOS Intel 러너에서 병렬 빌드
3. 각 PyInstaller 백엔드의 새 DB·HTTP, 튜토리얼 하위 프로세스·정적 자원, 안전 종료 스모크 테스트
4. NSIS 설치 파일과 두 DMG 생성
5. SHA-256 체크섬 생성
6. 태그와 같은 이름의 GitHub Release 생성 후 산출물 첨부

릴리즈 태그는 `vMAJOR.MINOR.PATCH` 형식을 사용한다.

```bash
git tag v1.0.0
git push origin v1.0.0
```

태그 버전은 CI에서 `package.json`과 `package-lock.json`의 빌드용 버전에 자동 반영된다. 태그 없이
Actions 화면에서 `Desktop build and release`를 수동 실행하면 세 운영체제 산출물을 14일 보관하는
검증용 Actions artifact만 만들고 GitHub Release는 생성하지 않는다.

릴리즈 자산 이름:

- `BillCalculator-Setup-<version>-Windows-x64.exe`
- `BillCalculator-<version>-macOS-arm64.dmg`
- `BillCalculator-<version>-macOS-x64.dmg`
- `SHA256SUMS.txt`

별도 인증 토큰이나 서명 인증서는 필요 없다. 릴리즈 작업만 GitHub가 제공하는 단기
`GITHUB_TOKEN`에 `contents: write` 권한을 받고, 테스트·빌드 작업은 읽기 권한만 사용한다.
