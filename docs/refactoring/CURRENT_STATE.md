# CURRENT_STATE — 작업 인계 문서

> 이 문서는 현재 코드·마이그레이션·테스트 결과를 요약한다.
> Ground Truth 우선순위는 `현재 코드 + migrations/ + 자동 테스트 > 이 문서 > 2026-07 분석 문서`다.

- 최종 갱신: 2026-11-05
- branch: `master`
- 이 갱신 직전 기준 commit: `f4e3e55`
- 스키마 최신 revision: `0003`
- 자동 테스트: **278 passed**
- DB 정합성 검사: **20개 중 실패 0개**

---

## 1. 현재 시스템

BillCalculator는 단일 관리자가 다세대 주택의 전기·수도·공동 공과금을 세대별로
배분하고, 정산서 발행·입금·잔액·이월을 관리하는 Flask + SQLite 로컬 웹 애플리케이션이다.

핵심 흐름:

1. 층·세대 마스터와 할인·공실·거주 인원을 등록한다.
2. 전기·수도·공동 요금을 입력하고 저장 전 세대별 미리보기를 확인한다.
3. 서로 다른 정산월의 계산 결과도 하나의 정산서로 묶을 수 있다.
4. 정산서를 인쇄·배부하고 세대별 입금과 누적 잔액을 관리한다.
5. JSON 전체 백업을 내보내고, 파일 또는 자동 복원 지점에서 고정 묶음을 선택해 복원한다.

---

## 2. 구조와 Single Source of Truth

| 영역 | 현재 구조 |
|---|---|
| 실행/라우팅 | `app.py` |
| 요금 배분 | `calculation_service.py` |
| 잔액/이월 | `balance.py` |
| ORM 스키마 SoT | `models.py` |
| DB 스키마 이력 SoT | `migrations/versions/` |
| 백업/부분 복원 | `backup_format.py`, `db_backup.py` |
| 정합성 검증 | `db_validate.py` |
| MySQL → SQLite | `scripts/migrate_mysql_to_sqlite.py` |
| 설정 레지스트리 | `settings_registry.py` |
| 현재 도메인 요약 | `BillCalculator_Knowledge_Base.md` |

`SQLSchema.txt`는 역사적 참고자료이며 스키마 생성에 사용하지 않는다. 빈 DB와 기존 DB 모두
`flask db upgrade`로만 생성·갱신한다. `db.create_all()`로 마이그레이션을 우회하지 않는다.

SQLite 실행 정책:

- `foreign_keys=ON`
- `journal_mode=WAL`
- `synchronous=FULL`
- `busy_timeout=5000`
- 통화는 `Money`(INTEGER 원), 배분 중간값·계량값은 `ExactDecimal`(TEXT)

---

## 3. 보존할 핵심 도메인 규칙

- 최종 세대 청구액은 10원 단위로 올림한다.
- 전기·수도 할인은 고지 순액을 할인 전 금액으로 되돌려 배분한 후 적용한다.
- 할인 대상 세대가 없으면 할인을 버리지 않고 실제 정산 대상 전체에 균등 배분한다.
- 사용량 또는 총 인원이 0이면 정산 대상 세대에 균등 배분하고 저장 전 경고한다.
- 수도 제외 세대도 0원 상세 행을 보존한다.
- 서로 다른 정산월을 하나의 정산서에 포함하는 것은 의도된 정상 흐름이다.
- 정산 원본은 정산서 하나에서만 사용한다.
- 입금이 있는 정산서는 삭제하지 않는다.
- 이월 항목은 정산서 표시 총액에는 포함하지만 누적 잔액에서는 제외해 이중계상을 막는다.

---

## 4. 불변 스냅샷

계산 상세의 `unit_snapshot`은 아래 11개 키를 보존한다.

```
unit_id, floor_id, floor_number, floor_name, unit_name,
electric_welfare, electric_voucher, has_tv, water_welfare,
residents_count, is_vacant
```

계산 상세 화면은 현재 `Unit`/`Floor`가 아닌 이 스냅샷의 세대명·층명을 표시한다.
`0003_calculation_identity_snapshots`가 기존 SQLite 상세를 보강하고, v1 JSON 백업과 MySQL 이전
스크립트도 동일한 식별 스냅샷을 생성한다.

발행된 `FinalInvoice`는 세대명·층번호·층명·세대 비고를 별도 스냅샷으로 보존한다.

---

## 5. 백업과 복원 정책

내보내기 형식은 `billcalculator-full-backup` v1이며 16개 업무 테이블을 모두 포함한다.
복원은 아래 5개 고정 묶음만 제공한다.

| 범위 | 교체 | 유지 |
|---|---|---|
| `full` | 모든 업무 데이터 | 없음 |
| `property_history` | 주택 구성 + 계산·정산·입금 | 앱 설정 |
| `master` | 층·세대 | 계산·정산·입금, 설정 |
| `accounting` | 계산·정산·입금 | 층·세대, 설정 |
| `settings` | 등록된 설정 9개의 값 | 내부 설정, 나머지 모든 데이터 |

중요 정책:

- 행 단위 merge/upsert는 하지 않는다. **선택 묶음 전체를 교체**한다.
- 선택하지 않은 묶음은 현재 데이터를 목표 DB에 합성해 보존한다.
- `master`는 현재 이력이 참조하는 세대·층의 구조 식별자가 백업과 다르면 차단한다.
- `accounting`은 백업 이력이 참조하는 세대·층의 구조 식별자가 현재 마스터와 다르면 차단한다.
- 세대 구조 식별자는 `(id, floor_id, unit_name)`, 층은 `(id, floor_number, name)`이다.
- 현재 데이터가 있는데 선택 백업 묶음이 비어 있으면 고위험 경고를 표시한다.
- 미리보기 토큰은 10분 동안 범위, 백업 본문, 현재 DB 지문을 묶는다.
- 적용 직전 SQLite 자동 백업을 만들고, 완전한 목표 DB를 하나의 트랜잭션으로 적용한다.
- 적용 중 20개 정합성 검사 중 하나라도 실패하면 전체 롤백한다.

`data/migration_20261105/BillCalculator_full_backup_20261105.json`은 수정 없이 최신 목표 DB에 적재해
검증했다. 16개 테이블 333행을 적재했고 정합성 검사 20개가 모두 통과했다.
해당 `data/` 산출물은 운영 데이터이므로 Git에 커밋하지 않는다.

---

## 6. 마이그레이션 이력

| revision | 역할 |
|---|---|
| `0001` | SQLite 초기 16개 업무 테이블 스키마 |
| `0002` | 정산서 수신자 스냅샷, 정산 원본 1회 사용, 공동요금 요청 토큰 |
| `0003` | 기존 계산 스냅샷에 세대·층 식별자 보강 |

MySQL 이전 스크립트는 원본을 읽기 전용으로 열고, 임시 SQLite에 적재·검증한 뒤 대상을 교체한다.
소수 통화, 중복 키, 고아 참조, 알 수 없는 설정은 임의로 보정하지 않고 보고서로 차단한다.

---

## 7. 검증 결과

2026-11-05 실행 결과:

```
pytest -q
278 passed
```

범위:

- 계산·배분·반올림·할인 대상 없음·사용량/0인원 예외
- 정산월 혼합, 정산 원본 재사용 방지, 정산서 스냅샷
- 완납·부분납·미납·과납·이월·환급·미연결 레거시 입금
- 스키마 제약, FK 삭제 정책, 마이그레이션 upgrade/downgrade/upgrade
- 계산 11키 스냅샷과 과거 상세 화면 불변성
- 5개 복원 묶음, 구조 지문, 빈 묶음 경고, 토큰 변경 감지
- JSON/SQLite 자동 백업과 v1 레거시 스냅샷 호환
- 11개 화면·10개 로컬 정적 파일, 모바일 레이아웃, JavaScript 구문

운영 이전용 JSON을 최신 임시 DB에 실제 적재한 후 `flask check-db` 동일 20개 검사를 실행했고
실패는 0개였다. 원본 JSON과 현재 로컬 운영 DB는 변경하지 않았다.

---

## 8. 운영 명령

```bash
source .venv/bin/activate

flask db upgrade
flask seed-settings
flask check-db
flask backup-db
pytest -q

python scripts/migrate_mysql_to_sqlite.py --mysql-uri "..." --report-only
```

실행 전 `BILLCALC_DB_PATH`가 의도한 DB를 가리키는지 확인한다. 업그레이드 전에는 `flask backup-db`로
별도 백업을 생성한다. `data/`, `*.db`, `*.db-wal`, `*.db-shm`, 이전 보고서는 커밋하지 않는다.

---

## 9. 주의사항

- 복원 정책을 merge/upsert로 바꾸지 않는다. 선택 묶음 완전 교체가 현재 제품 정책이다.
- 서로 다른 정산월 혼합을 오류로 처리하지 않는다.
- 계산 상세에서 `detail.unit.unit_name` / `bill.floor_ref.name`으로 되돌아가지 않는다.
- 기존 토큰 미리보기를 우회한 복원 엔드포인트를 추가하지 않는다.
- 정합성 검증 전에 운영 데이터를 임의로 보정하지 않는다.
- 스키마를 바꿀 때는 모델·Alembic·MySQL 이전·JSON 백업 호환·테스트를 함께 갱신한다.

---

## 10. 남은 제품 과제

- 정산 시 월 일괄 선택
- 공동 공과금 분배 방식 안내 통합
- 독립형 데스크톱 패키지링
- 첫 실행 온보딩, 월간 업무 체크리스트, 긴 목록 검색·필터·페이지 구분

완료 상태와 남은 제품 과제는 [TODO.md](../../TODO.md)를 최신 기준으로 본다.

---

## 11. 다음 세션 읽기 순서

1. [BillCalculator_Knowledge_Base.md](../../BillCalculator_Knowledge_Base.md)
2. 이 문서
3. [README.md](../../README.md)
4. [TODO.md](../../TODO.md)
5. `models.py` + `migrations/versions/`
6. `backup_format.py` + `tests/test_settings.py`

`docs/codebase-analysis/`와 `docs/refactoring/database/00-current-db-audit.md`는 2026-07 리팩토링 전 상태를
기록한 역사 문서다. 현재 동작을 판단할 때 사실 기준으로 사용하지 않는다.
