# BillCalculator 개발 가이드

이 문서는 BillCalculator 소스 실행, 데모 데이터, 환경 변수, 데이터베이스, 테스트와 데스크톱 패키징 방법을 설명한다. 일반 사용자를 위한 제품 소개와 다운로드 안내는 루트 [README](../README.md)를 우선한다.

Ground Truth 우선순위는 다음과 같다.

1. 현재 코드, `migrations/`, 자동 테스트
2. [현재 구현 상태](refactoring/CURRENT_STATE.md)
3. [도메인 지식 베이스](../BillCalculator_Knowledge_Base.md)
4. 과거 분석·계획 문서

## 기술 구성

- Backend/UI: Flask, Jinja, JavaScript, CSS
- Database: SQLite, SQLAlchemy, Alembic/Flask-Migrate
- Desktop: Electron, PyInstaller, Waitress
- Test: pytest
- Release: GitHub Actions, electron-builder

Flask가 HTML과 정적 파일을 직접 제공한다. 별도의 프론트엔드 번들러는 사용하지 않는다.

## 소스 웹앱 빠른 시작

Python 3.9 이상을 권장한다.

```bash
git clone https://github.com/InFinity-dev/BillCalculator.git
cd BillCalculator

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export FLASK_APP=app.py
flask db upgrade
flask seed-settings

python app.py
```

기본 주소는 [http://127.0.0.1:5000](http://127.0.0.1:5000)이다. Windows PowerShell에서는 가상환경을 다음처럼 활성화한다.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:FLASK_APP = "app.py"
flask db upgrade
flask seed-settings
python app.py
```

일반 실행은 스키마와 기본 설정만 가진 빈 업무 DB로 시작한다. 데모 데이터는 자동으로 실제 DB에 들어가지 않는다.

## PyCharm 실행

1. 저장소 루트를 프로젝트로 연다.
2. 프로젝트 인터프리터를 `.venv`로 지정한다.
3. `requirements.txt`를 설치하고 DB 마이그레이션과 설정 시딩을 실행한다.
4. 실행 대상은 `app.py`, 작업 디렉터리는 저장소 루트로 설정한다.

macOS/Linux 인터프리터는 `.venv/bin/python`, Windows는 `.venv\Scripts\python.exe`다. 새 PC에서는 가상환경을 복사하지 말고 다시 만든다.

## 데모 데이터로 화면 테스트

실제 업무 DB, 데모 DB와 자동 테스트 DB는 서로 분리된다.

| 실행 방식 | DB | 용도 |
|---|---|---|
| `python app.py` | `data/bill_calculator.db` 또는 환경 변수 지정 경로 | 실제 소스 실행 |
| `python scripts/demo.py run` | `data/demo.db` | 화면·기능 수동 테스트 |
| `pytest` | 테스트별 임시 DB | 자동 테스트 |

가상환경을 활성화한 뒤 다음 명령을 실행한다.

```bash
python scripts/demo.py run
```

기본 주소는 [http://127.0.0.1:5001](http://127.0.0.1:5001)이다. DB가 없으면 2026년 4~9월의 6개월치, 3개 층·8세대 데모 데이터를 만든다. 전기·수도·공동요금, 정산서, 완납·부분납·미납·초과납부, 이월·환급, 입주·퇴거 사례가 포함된다.

```bash
# DB만 생성하고 기존 데모 데이터는 유지
python scripts/demo.py seed

# 테스트 서버를 종료한 뒤 기본 6개월치로 초기화
python scripts/demo.py seed --reset

# 지정 기간으로 다시 생성
python scripts/demo.py seed --reset --start-month 2026-01 --months 12

# 다른 포트로 실행
python scripts/demo.py run --port 5002
```

`--reset`은 생성기가 소유한 것으로 검증된 데모 DB만 교체한다. 새 DB의 스키마·정합성 검사가 완료된 후 기존 파일을 교체하며, 생성 실패 시 기존 DB를 보존한다.

## Electron 데스크톱 개발

Node.js 22.12 이상과 Python 3.9 이상이 필요하다.

```bash
source .venv/bin/activate
pip install -r requirements-desktop.txt
npm ci

# 소스 상태에서 Electron 실행
npm run desktop:dev

# 현재 운영체제용 설치 파일 생성
npm run desktop:dist
```

세부 구조, 운영체제별 데이터 위치, PyInstaller 스모크 테스트와 태그 배포는 [Electron 데스크톱 앱 빌드·배포](desktop-distribution.md)를 참고한다.

릴리즈는 `vMAJOR.MINOR.PATCH` 형식의 태그로 만든다. GitHub Actions가 Windows x64, macOS arm64, macOS x64 환경에서 각각 네이티브 빌드한 뒤 GitHub Release를 생성한다.

## 주요 디렉터리

| 경로 | 역할 |
|---|---|
| `app.py` | Flask 라우트와 애플리케이션 진입점 |
| `models.py` | SQLAlchemy 모델과 현재 스키마 정의 |
| `calculation_service.py` | 전기·수도·공동요금 계산 서비스 |
| `tutorial_service.py`·`tutorial_runtime.py` | 온보딩 데이터와 격리 실행 관리 |
| `backup_format.py`·`db_backup.py`·`db_validate.py` | 백업 형식과 DB 안전성 검사 |
| `templates/` | Jinja 화면과 공통 컴포넌트 |
| `static/` | 공통·화면별 CSS와 JavaScript |
| `migrations/` | Alembic 마이그레이션 |
| `desktop/` | Electron 메인 프로세스와 PyInstaller spec |
| `desktop_backend.py` | 패키징된 로컬 백엔드 진입점 |
| `scripts/` | 데모 생성, 데이터 이전, 빌드·검증 스크립트 |
| `tests/` | 계산·DB·화면·튜토리얼·데스크톱 회귀 테스트 |
| `docs/refactoring/` | 현재 상태와 DB 리팩터링 문서 |

## 화면 개발 규칙

- `templates/base.html`: 공통 사이드 메뉴와 화면 틀
- `templates/components/`: 아이콘과 공통 화면 조각
- `static/css/app.css`: 토큰, 공통 구성 요소, 반응형 셸
- `static/css/utilities.css`: 공통 간격·정렬·텍스트 유틸리티
- `static/css/pages/`: 화면별·인쇄 스타일
- `static/js/app.js`: 모바일 메뉴, 탭, 모달, 숫자 표시, 반응형 표
- `static/js/tutorial_overlay.js`: 실제 화면 기반 온보딩 진행 제어

템플릿에 새 인라인 CSS를 추가하지 않고 공통 클래스 또는 화면별 CSS를 사용한다. 768px 이하에서는 메뉴와 폼이 모바일 형태로 전환되고, 입력 표는 카드 또는 가로 스크롤 형태로 표시된다.

## 환경 변수

모두 선택 사항이며 지정하지 않으면 로컬 실행에 맞는 기본값이 적용된다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `BILLCALC_DB_PATH` | `data/bill_calculator.db` | SQLite 파일 경로 |
| `BILLCALC_DATABASE_URI` | 미설정 | SQLAlchemy URI 직접 지정. DB 경로보다 우선 |
| `BILLCALC_SECRET_KEY` | 프로세스마다 임의 생성 | Flask 세션 키 |
| `BILLCALC_DEBUG` | `0` | `1`일 때만 디버그 모드 |
| `BILLCALC_HOST` | `127.0.0.1` | 바인딩 주소. 인증이 없으므로 외부 공개 금지 |
| `BILLCALC_PORT` | `5000` | 포트 |
| `BILLCALC_ONBOARDING_ENABLED` | `1` | 빈 DB의 첫 실행 사용 안내 활성화 |
| `BILLCALC_ONBOARDING_STATE_PATH` | DB 폴더의 `app_state.json` | 온보딩 완료 상태 파일 |
| `BILLCALC_TUTORIAL_DATA_DIR` | DB 폴더의 `tutorial_sessions/` | 실행별 연습 DB 폴더 |
| `BILLCALC_TUTORIAL_MODE` | `0` | 내부용 연습 서버 구분 |
| `BILLCALC_TUTORIAL_MAIN_URL` | 자동 지정 | 연습 완료·종료 후 본 앱 주소 |
| `BILLCALC_TUTORIAL_CONTROL_TOKEN` | 자동 생성 | 본 앱의 연습 서버 제어 토큰 |
| `BILLCALC_TUTORIAL_HAD_USER_DATA` | 자동 판정 | 연습 시작 전 업무 데이터 존재 여부 |
| `BILLCALC_TUTORIAL_RUN_ID` | 자동 생성 | 현재 연습 실행 식별자 |
| `BILLCALC_DESKTOP_MODE` | `0` | Electron이 관리하는 백엔드 여부 |
| `BILLCALC_DESKTOP_SHUTDOWN_TOKEN` | 자동 생성 | Electron 안전 종료 토큰 |
| `BILLCALC_APP_VERSION` | 패키지 버전 | 데스크톱 앱 버전 |
| `BILLCALC_PYTHON` | 자동 탐색 | Electron 개발 실행에서 사용할 Python 경로 |

DB 접속 정보나 비밀 값은 소스에 하드코딩하지 않는다. `.env`와 실제 DB·백업 파일은 Git에 커밋하지 않는다.

## Flask 관리 명령

| 명령 | 설명 |
|---|---|
| `flask db upgrade` | 최신 스키마 생성·업그레이드 |
| `flask db downgrade base` | 스키마 전체 되돌리기 |
| `flask seed-settings` | 누락된 기본 설정 시딩 |
| `flask check-db` | FK·무결성·도메인 정합성 검사 |
| `flask backup-db [--output PATH]` | SQLite 온라인 backup API로 백업 |

## 데이터베이스

- 엔진: SQLite
- 실행 정책: WAL, `foreign_keys=ON`, `synchronous=FULL`, `busy_timeout=5000`
- 통화: INTEGER 원 단위
- 배분 중간값·계량값: 정확한 Decimal 문자열
- 스키마 Ground Truth: `models.py`와 `migrations/`
- 런타임 데이터: `data/` 아래에 저장하며 Git에서 제외

`SQLSchema.txt`는 과거 MySQL 시절 참고 자료이며 현재 스키마 생성에 사용하지 않는다.

### 기존 MySQL 데이터 이전

```bash
pip install -r requirements-legacy.txt

# 검증만 수행
python scripts/migrate_mysql_to_sqlite.py \
    --mysql-uri "mysql+mysqlconnector://user:pw@localhost/bill_calculator" \
    --report-only

# 임시 SQLite에 적재·검증한 뒤 대상 DB 교체
python scripts/migrate_mysql_to_sqlite.py \
    --mysql-uri "mysql+mysqlconnector://user:pw@localhost/bill_calculator" \
    --sqlite-path data/bill_calculator.db
```

MySQL 원본은 읽기 전용으로 접근한다. 이전 후 row count, 금액 합계와 세대별 잔액을 대조하며, 검토가 필요한 데이터가 있으면 보고서를 남기고 이전을 중단한다. 대상 DB가 존재하더라도 임시 DB 적재와 전체 검증이 끝난 뒤에만 교체한다.

기존 대상 DB를 교체해야 하는 경우에만 `--force`를 추가한다. 실행 중인 DB 또는 WAL 파일이 남아 있는 DB에는 사용하지 않는다.

## 자동 테스트

```bash
pip install -r requirements-dev.txt

pytest -q
pytest -m golden
pytest -m "not slow"
```

의도적으로 골든 결과를 변경할 때만 다음 명령으로 기준선을 갱신한다.

```bash
pytest tests/test_golden.py --update-golden
```

2026-11-14 기준 전체 자동 테스트는 **321개**이며, 계산·할인·TV 정책·스냅샷·복원·납부·온보딩·데스크톱 bootstrap·튜토리얼 실제 데이터 준비를 검증한다. 날짜가 있는 테스트 수치는 기록이며 현재 사실은 실제 테스트 실행 결과를 우선한다.

## 데이터 안전 원칙

- 운영 DB를 테스트나 데모 실행에 사용하지 않는다.
- SQLite WAL 파일이 남아 있는 실행 중 DB를 단순 복사하지 않는다.
- 삭제·복원·덮어쓰기 전에 자동 백업을 만든다.
- 복원은 선택 범위의 전체 교체이며 행 단위 병합으로 해석하지 않는다.
- 계산·정산 스냅샷을 과거 마스터 정보로 다시 계산하지 않는다.
- 서로 다른 정산월을 한 정산서에 넣는 것은 정상 도메인 흐름이다.
- 할인액이 배분 가능한 금액을 넘으면 해당 항목을 0원까지만 낮추고 다른 세대에 재배분하지 않는다.

## 관련 문서

- [현재 구현 상태](refactoring/CURRENT_STATE.md)
- [도메인 지식 베이스](../BillCalculator_Knowledge_Base.md)
- [DB 감사·설계·이전·테스트 계획](refactoring/database/)
- [Electron 빌드·배포](desktop-distribution.md)
- [릴리즈 버그 수정 검증 기록](releases/README.md)
- [GitHub 성장 지표 자동 수집](growth-metrics.md)
- [기능 과제와 검증 기록](../TODO.md)
- [기여 방법](../CONTRIBUTING.md)
