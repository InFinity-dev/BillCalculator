# 다세대 주택 공과금 정산 시스템

층·건물 단위로 부과된 공과금(전기/수도/공동)을 세대별로 재분배 계산하고,
세대별 청구서를 생성·인쇄하며, 입금과 미납 잔액을 추적하는 로컬 웹 애플리케이션.

**별도의 DB 서버 설치가 필요 없습니다.** 데이터는 로컬 SQLite 파일 하나에 저장됩니다.

---

## 빠른 시작

```bash
git clone https://github.com/InFinity-dev/BillCalculator.git
cd BillCalculator

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

flask db upgrade        # data/bill_calculator.db 생성 + 스키마 적용
flask seed-settings     # 설정 기본값 시딩 (멱등)

python app.py           # http://127.0.0.1:5000
```

`FLASK_APP` 이 필요하면 `export FLASK_APP=app.py` 를 먼저 실행하세요.

---

## PyCharm에서 실행

1. 복제한 `BillCalculator` 폴더를 프로젝트로 엽니다.
2. 프로젝트 인터프리터를 해당 PC에서 생성한 `venv`로 지정합니다. macOS/Linux는 `venv/bin/python`, Windows는 `venv\Scripts\python.exe`입니다.
3. 해당 인터프리터에 `requirements.txt`의 패키지를 설치하고, 빠른 시작의 DB 초기화 명령을 실행합니다.
4. 실행 대상은 `app.py`, 작업 디렉터리는 프로젝트 루트로 지정합니다. 필요한 환경변수는 실행 설정에 입력합니다.

새 PC에서는 가상환경을 새로 만들고 `requirements.txt`로 패키지를 설치합니다. 기존 데이터를 이어서 사용하려면 `flask backup-db`로 만든 백업을 새 PC로 옮겨 `BILLCALC_DB_PATH`에 지정합니다.

---

## 더미 데이터로 테스트

실제 사용과 화면 테스트는 서로 다른 SQLite DB를 사용합니다.

| 실행 방식 | DB | 용도 |
|---|---|---|
| `python app.py` | 기본 `data/bill_calculator.db` 또는 지정한 환경변수 | 실제 사용 |
| `python scripts/demo.py run` | `data/demo.db` 고정 | 화면·기능 테스트 |
| `pytest` | 임시 DB | 자동 테스트 |

### PyCharm

프로젝트에 공유 실행 설정 두 개를 추가했습니다. 프로젝트를 다시 열거나 파일을 동기화한 뒤 실행 목록에서 선택합니다.

- **BillCalculator Demo**: 테스트 서버 실행. 더미 DB가 없으면 처음 한 번 기본 데이터를 생성합니다.
- **BillCalculator Demo Seed**: 기본 더미 데이터 생성. 이미 생성한 데이터는 유지합니다.

두 설정 모두 프로젝트 인터프리터를 사용합니다. 기존 실제 사용 실행 설정과 전환해서 사용할 수 있습니다. 실행 설정이 보이지 않으면 **Run → Edit Configurations → Python**에서 스크립트를 `scripts/demo.py`, 작업 디렉터리를 프로젝트 루트, Parameters를 `run`으로 지정합니다. [PyCharm 실행 설정 안내](https://www.jetbrains.com/help/pycharm/run-debug-configuration-python.html)

### 터미널

가상환경을 활성화한 뒤 프로젝트 루트에서 실행합니다.

```bash
python scripts/demo.py run
```

[http://127.0.0.1:5001](http://127.0.0.1:5001)에 접속합니다. 기본 데이터는 **2026년 4~9월의 6개월치**, 3개 층·8세대이며 전기·수도·공동 요금, 월별 정산서, 완납·부분납·미납·초과납부, 이월·환급, 입주·퇴거 사례를 포함합니다. 서버는 `Ctrl+C` 또는 PyCharm의 중지 버튼으로 종료합니다.

재실행하면 테스트 중 변경한 데이터가 유지됩니다. 화면에는 **테스트 데이터 사용 중** 표시가 나타나고, 인쇄 고지서에도 테스트용 문구가 포함됩니다.

### 생성·초기화 및 기간 변경

```bash
# 서버를 띄우지 않고 기본 데이터 생성. 기존 데이터는 유지
python scripts/demo.py seed

# 테스트 서버를 종료한 뒤, 기본 6개월치로 초기화
python scripts/demo.py seed --reset

# 2026년 1월부터 12개월치로 다시 생성
python scripts/demo.py seed --reset --start-month 2026-01 --months 12

# 다른 포트로 실행
python scripts/demo.py run --port 5002
```

`--reset`은 테스트 중 추가·수정한 내용도 초기화합니다. 시작월은 `YYYY-MM`, 개월 수는 1~36입니다. 새 데이터의 계산·DB 정합성 검증이 완료된 뒤 기존 더미 DB를 교체하며, 생성에 실패하면 기존 데이터가 유지됩니다. 생성기가 만든 것으로 식별되지 않는 `demo.db`와 연결된 DB 파일은 교체하지 않습니다.

테스트 실행 파일은 상속받은 `BILLCALC_DATABASE_URI`와 `BILLCALC_DB_PATH`를 대신해 전용 더미 DB를 선택합니다. 일반 `app.py` 실행에는 더미 데이터가 자동 주입되지 않습니다. 자동 테스트도 상속받은 DB URI를 해제하고 임시 DB를 사용합니다. 생성된 DB는 Git에서 제외되며, 생성 코드와 PyCharm 실행 설정은 공유할 수 있습니다.

---

## 반영한 개선 — 2026-10-05

### 화면 리뉴얼

- 흰색·중립색·파란색 중심의 B2B SaaS 스타일로 통일하고 그라데이션과 장식용 이모지를 제거했습니다.
- 공통 사이드 메뉴와 대시보드를 정리하고, 모바일에서는 메뉴·폼·입력 표를 작은 화면에 맞게 전환합니다.
- 전기 검침은 세대별 전월·현월·사용량을 한 행에서 확인하며, 잘못된 검침값은 입력 오류로 표시합니다.
- 청구서 최종 확인의 청구·환급·이월 표시, 입금 내역, 인쇄 고지서와 모바일 미리보기를 개선했습니다.
- 모달과 탭의 키보드 조작 및 포커스 처리를 정리했습니다.

검침 입력의 기본 Tab 순서는 **전월 → 현월 → 다음 세대**입니다. 이전 화면의 열별 Tab 이동 방식은 현재 적용되지 않습니다.

### 오류 및 예외 처리

- 잘못된 금액·정수·불리언 입력, 음수 검침 및 수도 분배 대상이 없는 상황을 검증합니다.
- 전기 정산 개월 수를 실제 월별 내역에서 계산하고, 인터넷·관리비·기타 공동 요금을 각각 저장합니다.
- 청구서 정산 원본의 중복 사용·재사용·고지월 불일치와 입금의 청구서·세대 연결을 검증합니다.
- 과거 정산 세대와 공실 세대가 청구·입금·잔액 조회에서 누락되는 경우를 보완했습니다.
- 입금 없이 잔액이 0인 경우는 ‘청구 없음’으로 표시하고, 실제 납부가 있는 완납과 구분합니다.
- 정산서별 전체 세대의 납부 내역과 청구서 연결이 없는 과거 입금도 조회·삭제할 수 있습니다.
- 설정 일부를 가져올 때 생략한 기존 값을 보존하고, 세대명·메모 등 동적 텍스트를 HTML 이스케이프 처리합니다.
- DB 정합성 검사와 MySQL 이전 검증을 강화하고, 이전 실패 시 기존 SQLite DB를 보존합니다.

완료 항목과 남은 기능 과제는 [TODO.md](TODO.md)에 정리되어 있습니다.

---

## 정산서 및 납부 내역 삭제

1. **정산서 → 정산 내역**에서 해당 정산서의 **납부 내역**을 선택합니다. **납부 관리 → 정산서별 납부 내역**에서 직접 정산서를 선택해도 됩니다.
2. 해당 정산서에 등록된 **전체 세대의 입금**을 확인하고, 삭제할 입금의 **입금 삭제** 버튼을 누릅니다. 0원으로 등록된 입금이나 세대별 청구서 연결이 없는 과거 입금도 표시됩니다.
3. 입금이 모두 삭제되면 **정산서 삭제** 버튼이 활성화됩니다. 삭제하면 해당 정산서와 세대별 청구 내역이 제거됩니다.

정산서 삭제는 완납 표시 여부가 아닌 **실제 등록된 입금의 존재**로 제한됩니다. 다른 세대의 입금이 남아 있으면 삭제할 수 없으며, 정산서 삭제를 시도하면 해당 입금 목록으로 이동합니다. 납부 내역과 정산서 삭제는 복구 기능이 없으므로 필요한 기록은 삭제 전에 백업하세요.

---

## 화면 개발

Flask가 HTML과 정적 파일을 직접 제공합니다. 별도의 프론트엔드 설치나 빌드 과정은 필요하지 않습니다.

- `templates/base.html`: 공통 메뉴와 화면 틀
- `static/css/app.css`: 색상·간격 기준, 버튼·폼·표·모달 등 공통 구성 요소
- `static/css/utilities.css`: 여러 화면에서 사용하는 간격·정렬·글자 스타일
- `static/css/pages/`: 화면별 스타일과 인쇄 스타일
- `static/js/app.js`: 모바일 메뉴, 탭, 모달, 숫자 표시, 반응형 표 처리

템플릿에는 인라인 CSS를 추가하지 않고 공통 클래스 또는 화면별 CSS를 사용합니다. 768px 이하에서 메뉴와 폼이 모바일 형태로 전환되며, 입력 표는 카드로, 상세 내역 표는 가로 스크롤로 표시됩니다. PyCharm에서 기본 실행 설정으로 작업할 때는 변경 후 서버를 재시작하고 새로고침하세요.

---

## 설정 (환경변수)

모두 선택 사항이며, 지정하지 않으면 안전한 기본값이 적용됩니다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `BILLCALC_DB_PATH` | `data/bill_calculator.db` | SQLite 파일 경로 |
| `BILLCALC_DATABASE_URI` | (미설정) | SQLAlchemy URI 를 통째로 지정. 최우선 |
| `BILLCALC_SECRET_KEY` | 프로세스마다 랜덤 | Flask 세션 키. **지정하지 않으면 재시작 시 세션이 무효화됩니다** |
| `BILLCALC_DEBUG` | `0` | `1` 일 때만 디버그 모드 |
| `BILLCALC_HOST` | `127.0.0.1` | 바인딩 주소. 이 앱에는 인증이 없으므로 외부 노출을 권장하지 않습니다 |
| `BILLCALC_PORT` | `5000` | 포트 |

DB 접속 정보는 소스에 하드코딩되어 있지 않습니다.

---

## 명령어

| 명령 | 설명 |
|---|---|
| `flask db upgrade` | 스키마 생성/갱신 |
| `flask db downgrade base` | 스키마 전체 되돌리기 |
| `flask seed-settings` | 설정 기본값 시딩 (기존 값은 덮어쓰지 않음) |
| `flask check-db` | DB 정합성 검증 (FK, 무결성, 도메인 규칙) |
| `flask backup-db [--output PATH]` | SQLite 네이티브 backup API 로 안전하게 백업 |

---

## 데이터베이스

- 엔진: **SQLite** (WAL 모드, `foreign_keys=ON`, `synchronous=FULL`)
- 스키마 정의: `models.py` + `migrations/` (Alembic) ← **Single Source of Truth**
- 데이터 위치: `data/` (Git 에 커밋되지 않습니다)
- `SQLSchema.txt` 는 MySQL 시절의 역사적 참고 자료이며 **실행하면 안 됩니다**

설계 문서: [docs/refactoring/database/](docs/refactoring/database/)

### 기존 MySQL 데이터 이전

```bash
pip install -r requirements-legacy.txt

# 1) 검증만 — 대상 DB 를 만들지 않습니다
python scripts/migrate_mysql_to_sqlite.py \
    --mysql-uri "mysql+mysqlconnector://user:pw@localhost/bill_calculator" \
    --report-only

# 2) 실제 이전
python scripts/migrate_mysql_to_sqlite.py \
    --mysql-uri "mysql+mysqlconnector://user:pw@localhost/bill_calculator" \
    --sqlite-path data/bill_calculator.db
```

- MySQL 원본은 **읽기 전용으로만** 접근하며 변경되지 않습니다.
- 이전 후 row count · 금액 합계 · **세대별 잔액**을 원본과 대조합니다.
- 검토가 필요한 데이터는 `data/migration_report.json` 에 기록되고 이전이 중단됩니다.
- 대상 DB가 있어 `--force`를 쓰더라도 임시 DB에서 적재·검증을 마친 뒤 교체합니다. 앱이 실행 중이거나 대상 WAL 파일이 남아 있으면 교체하지 마세요.

---

## 테스트

```bash
pip install -r requirements-dev.txt
pytest -q                        # 전체
pytest -m golden                 # 골든 마스터만
pytest -m "not slow"             # 서브프로세스 테스트 제외
```

골든 마스터 기준선 갱신 (의도적으로 결과가 바뀔 때만):

```bash
pytest tests/test_golden.py --update-golden
```

### 최근 검증 결과 — 2026-10-05

- 전체 자동 테스트 **254개 통과**. `tests/test_regressions.py`는 오류 수정 회귀 검증, `tests/test_frontend.py`는 주요 화면 렌더링·로컬 정적 파일·HTML 속성 검증, `tests/test_demo.py`는 월별 더미 데이터·DB 격리·초기화 검증, `tests/test_payment_management.py`는 청구 없음 표시·숨겨진 입금 조회·입금 삭제 후 정산서 삭제 검증을 포함합니다.
- 임시 DB로 11개 화면과 10개 로컬 정적 파일의 정상 응답 및 JavaScript 구문을 확인했습니다.
- 브라우저 너비 **320·768·1440px**에서 11개 화면을 확인했고, 문서 전체의 가로 넘침이 없었습니다. 상세 내역 표는 표 영역 안에서 가로 스크롤됩니다.
- 임시 DB에서 **공동 요금 정산 → 청구서 생성 → 입금 등록 → 잔액 확인** 흐름과 모바일 메뉴·모달·검침 오류 표시를 확인했습니다.
- 실제 로컬 DB의 정합성 검사 17개가 통과했습니다. 실제 DB에 자동 데이터 보정을 수행하지 않았습니다.
- 6개월 더미 DB 생성 시 정합성 검사 17개가 통과했고, 실행 서버의 8개 화면에서 정상 응답과 테스트용 표시·문구를 확인했습니다. 생성 전후 실제 DB 파일이 동일함을 확인했습니다.
- 별도 임시 DB의 브라우저 화면에서 청구 없음·완납·청구서 연결 확인 구분과 정산서별 전체 입금 표시를 확인했습니다. 삭제 확인 창에서 브라우저 자동 제어가 멈춰 최종 삭제 흐름은 자동 테스트의 실제 요청으로 검증했습니다.

이 결과는 해당 날짜의 소스 기준입니다. 모바일 검증은 브라우저 화면 크기 기준이며 실제 기기 검증은 포함하지 않습니다.

---

## 문서

| 경로 | 내용 |
|---|---|
| [docs/codebase-analysis/](docs/codebase-analysis/) | 리팩토링 전 코드베이스 전체 분석 (8종) |
| [docs/refactoring/database/](docs/refactoring/database/) | DB 계층 리팩토링 감사·설계·이전·테스트 계획 |
| [TODO.md](TODO.md) | 완료 작업, 남은 기능 과제 및 검증 기록 |

---

## 알려진 제한

- **인증이 없습니다.** 로컬 단독 사용을 전제로 합니다. 기본 바인딩이 `127.0.0.1` 인 이유입니다.
