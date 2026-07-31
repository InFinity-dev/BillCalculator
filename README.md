# 다세대 주택 공과금 정산 시스템

층·건물 단위로 부과된 공과금(전기/수도/공동)을 세대별로 재분배 계산하고,
세대별 청구서를 생성·인쇄하며, 입금과 미납 잔액을 추적하는 로컬 웹 애플리케이션.

**별도의 DB 서버 설치가 필요 없습니다.** 데이터는 로컬 SQLite 파일 하나에 저장됩니다.

---

## 빠른 시작

```bash
git clone <repo>
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

---

## 문서

| 경로 | 내용 |
|---|---|
| [docs/codebase-analysis/](docs/codebase-analysis/) | 리팩토링 전 코드베이스 전체 분석 (8종) |
| [docs/refactoring/database/](docs/refactoring/database/) | DB 계층 리팩토링 감사·설계·이전·테스트 계획 |
| [TODO.md](TODO.md) | 남은 기능 과제 |

---

## 알려진 제한

- **인증이 없습니다.** 로컬 단독 사용을 전제로 합니다. 기본 바인딩이 `127.0.0.1` 인 이유입니다.
- 계산기의 **공동 공과금 탭은 프론트엔드/백엔드 필드 계약이 어긋나 있어** 총액이 0으로 저장됩니다
  (분석 문서 C-1). 백엔드 배분 로직 자체는 정상이며, 수정은 별도 단계에서 진행합니다.
