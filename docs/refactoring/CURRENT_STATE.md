# CURRENT_STATE — 작업 인계 문서

> 다른 머신에서 이 작업을 그대로 이어받기 위한 **현재 상태 기록**이다.
> 계획서가 아니라 **지금 코드·git·테스트 실행 결과의 스냅샷**이다.
> 계획은 [database/](database/) 하위 4개 문서에 있다.

작성 시각: 2026-07-31
작성 기준: 실제 `git status` / `git diff --stat` / `pytest tests -q` 실행 결과

---

## 1. 현재 branch / 기준 commit

| 항목 | 값 |
|---|---|
| branch | `master` |
| HEAD | `273f01d` — Pre-refactoring Analysis |
| origin/master | `273f01d` (동기화됨, ahead/behind 없음) |
| 직전 커밋 | `6efd646` 계산 로직 FE/BE rowId 불일치 수정 |

**이번 DB 리팩토링 작업은 아직 커밋되지 않았다.** 전부 워킹 트리에만 존재한다.
`273f01d` 는 이전 세션의 코드베이스 분석 문서(`docs/codebase-analysis/` 8종)만 담고 있으며 코드 변경은 없다.

재개 시 첫 작업은 커밋 여부 결정이다. 아직 실패 테스트 3건이 남아 있으므로 아래 §9 를 먼저 처리하고 커밋하는 것을 권한다.

---

## 2. 이번 세션에서 완료한 작업

MySQL → SQLite **데이터 계층 전면 리팩토링**. 드라이버 교체가 아니라 스키마 정규화를 동반했다.

1. **감사·설계 문서 4종 작성** (`docs/refactoring/database/`)
2. **SQLite 설정 계층 신설** — `config.py`(환경변수 기반), `extensions.py`(PRAGMA 훅), `db_types.py`(금액 타입)
3. **`models.py` 분리 + 스키마 정규화** — `app.py` 안에 있던 모델 14개를 꺼내고 테이블 2개 신설
4. **Alembic baseline migration 도입** — `db.create_all()` 완전 제거
5. **`app.py` 라우트 적응** — 계산 알고리즘은 손대지 않고 영속화 계층만 교체
6. **정합성 검증 / 백업 도구** — `db_validate.py`(13종 검사), `db_backup.py`
7. **레거시 이전 스크립트** — `scripts/migrate_mysql_to_sqlite.py` (읽기 전용 접근)
8. **테스트 207개 작성** — 10개 파일 + 골든 마스터 데이터셋

**하지 못한 것: 실제 MySQL DB 감사.** `power_user` 자격증명이 현재 환경에서 무효(`Access denied`, TCP·소켓 모두)라 운영 스키마·FK 정책·데이터 분포를 실측하지 못했다. 이 제약은 이후 여러 항목의 근본 원인이므로 §8, §12 에서 다시 언급한다.

---

## 3. 현재 변경된 파일

`git diff --stat` 기준 **수정 5개 / 신규 다수(전부 untracked)**.

### 수정된 파일

```
 .gitignore                   |    9 +
 SQLSchema.txt                |   29 +-
 app.py                       | 2290 ++++++++++++++++-------------------
 requirements.txt             |    3 +-
 templates/invoice_print.html |    5 +-
 5 files changed, 1247 insertions(+), 1089 deletions(-)
```

- `app.py` (1,784줄) — 모델 정의 제거, `create_app()` 팩토리화, 라우트 적응
- `SQLSchema.txt` — DEPRECATED 배너 + 불일치 목록 추가. **더 이상 SoT 아님**
- `templates/invoice_print.html` — **이번 작업에서 수정한 유일한 템플릿**. 이월 판정을 문자열 → `charge.is_carryover`
- `requirements.txt` — `mysql-connector-python`, `python-dateutil` 제거 / `Flask-Migrate` 추가
- `.gitignore` — `data/*`, `*.db-wal`, `*.db-shm`, `backups/`

### 신규 파일 (untracked)

| 파일 | 줄 | 역할 |
|---|---|---|
| `models.py` | 807 | 16개 모델. **스키마 SoT** |
| `db_validate.py` | 414 | 정합성 검사 13종 |
| `balance.py` | 173 | 잔액 계산 단일 구현 |
| `db_types.py` | 134 | `Money` / `ExactDecimal` |
| `db_backup.py` | 115 | SQLite 네이티브 백업 |
| `config.py` | 103 | 환경변수 설정 |
| `settings_registry.py` | 84 | 설정 9종 단일 정의 |
| `extensions.py` | 68 | `db`/`migrate` + PRAGMA 훅 |
| `migrations/versions/0001_initial_sqlite_schema.py` | 388 | baseline |
| `scripts/migrate_mysql_to_sqlite.py` | 1,129 | 레거시 이전 |
| `scripts/export_golden.py` | 55 | 골든 추출(읽기 전용) |
| `tests/` | 3,186 | 10개 파일 + `conftest.py` + `golden/` |
| `docs/refactoring/database/` | 1,590 | 감사·설계·이전·테스트 계획 |
| `README.md` | 122 | 신규 |
| `pytest.ini`, `requirements-dev.txt`, `requirements-legacy.txt` | — | — |

`data/` 도 untracked 로 잡히지만 `.gitignore` 대상이며 로컬 DB 파일이다. **커밋하면 안 된다.**

---

## 4. 현재 architecture 결정사항

### 4.1 스키마 Single Source of Truth 교체

```
[변경 전] app.py 모델 ─┐
          SQLSchema.txt ├─ 3중 불일치 (D-1…D-12)
          실제 MySQL DB ─┘

[변경 후] models.py + migrations/  ← 유일한 SoT
          SQLSchema.txt            ← 역사적 참고자료. 실행 금지
```

`db.create_all()` 은 코드에서 제거했다. 테스트조차 `flask db upgrade` 로 스키마를 만든다 — 마이그레이션이 실제로 올바른지 검증되지 않으면 의미가 없기 때문이다(`tests/conftest.py:69`).

### 4.2 SQLite PRAGMA — 이 앱의 특성에 근거한 선택

| PRAGMA | 값 | 근거 |
|---|---|---|
| `foreign_keys` | `ON` | SQLite 기본값이 **OFF**. 커넥션마다 훅으로 강제해야 함 |
| `journal_mode` | `WAL` | 납부 페이지 등 읽기가 잦음. 읽기가 쓰기를 막지 않음 |
| `synchronous` | `FULL` | 금액 데이터. 최신 트랜잭션 유실 불허 (NORMAL 은 WAL 에서 유실 가능) |
| `busy_timeout` | `5000` | 단일 사용자라 경합은 드물지만 백업 중 충돌 대비 |

일반적 best practice여서가 아니라 **단일 사용자 / 로컬 Flask / 작은 DB / 동시 쓰기 거의 없음** 이라는 이 앱의 조건에서 고른 값이다.

### 4.3 금액 표현 — 의미별 3분류

25개 `Numeric(10,2)` 컬럼을 일괄 INTEGER 로 바꾸지 않고 **도메인 의미로 나눴다.**

| 분류 | 타입 | 대상 |
|---|---|---|
| 통화 | `Money` (INTEGER, 원) | 입력 총액, `charged_amount`, 정산서 금액, 납부액 |
| 배분 중간값 | `ExactDecimal` (TEXT) | `base_amount`, 세대별 할인/TV, `final_amount` |
| 계량값 | `ExactDecimal` (TEXT) | 검침 지침, 사용량 |

배분 중간값과 검침값은 본질적으로 분수다. INTEGER 로 바꾸면 표시 금액과 사용량이 실제로 달라진다. `ExactDecimal` 은 REAL 이 아니라 TEXT 에 정확한 십진 문자열을 넣어 부동소수점 오차를 원천 차단한다.

### 4.4 FK 정책 — 구조 vs 회계

| 정책 | 관계 | 근거 |
|---|---|---|
| `CASCADE` | 고지서 → 자식 detail/reading/month, 정산서 → charge/item | 부모 없이는 의미 없는 **구조적** 자식 |
| `RESTRICT` | 세대 → 검침·detail·정산서·납부, **정산서 → 납부** | **회계 기록**. 마스터를 지웠다고 과거 기록이 사라지면 안 됨 |

`invoice_combinations → payments` 를 RESTRICT 로 바꾼 것이 실질적 변화다. 기존에는 정산서 삭제가 입금 기록을 조용히 지울 수 있었다.

`Floor → Unit` 은 CASCADE 를 **유지**했다(기존 동작 보존). Unit 하위가 이미 RESTRICT 로 막고 있어 회계 데이터는 안전하다.

ORM 쪽은 `passive_deletes=True`(CASCADE) / `passive_deletes='all'`(RESTRICT) 로 맞춰, SQLAlchemy 가 FK 를 NULL 로 밀어버리고 DB 제약을 우회하는 일을 막았다.

### 4.5 JSON 컬럼 — 개별 판단

기준: **비즈니스 로직이 값을 집계·판정에 사용하면 관계형, 표시 전용 스냅샷이면 JSON.**

| 컬럼 | 판정 |
|---|---|
| `electric_bills.monthly_details` | → **`electric_bill_months` 테이블** (월별 금액을 집계에 사용) |
| `final_invoices.additional_charges` | → **`final_invoice_charges` 테이블** (이월 판정·잔액 계산에 사용) |
| `*.unit_snapshot` | **JSON 유지** — 과거 시점 표시용 불변 스냅샷 |
| `water_bills.excluded_units` | **JSON 유지** |

### 4.6 이월 판정 — 문자열 → 컬럼

`final_invoice_charges.is_carryover` 를 명시 저장한다. 신규 코드에서 `description` 문자열 매칭은 **`scripts/migrate_mysql_to_sqlite.py` 안에서만** 쓴다(레거시 데이터 해석 목적).

불변식은 그대로: **이월 금액은 `total_amount` 에 포함되고, 잔액 계산에서는 제외된다.**

### 4.7 설정 — key-value 유지

EAV 를 관계형으로 재설계하지 않았다. 설정이 9개뿐이고, 향후 추가가 잦으며, migration 난이도가 낮고, 읽기 성능이 문제되지 않는다. 대신 **`settings_registry.py` 로 정의를 단일화**해 5곳에 흩어져 있던 키 목록 중복(M-4)과 시딩 누락 3건을 해소했다.

"EAV 는 나쁘다"는 이유만으로 과도하게 재설계하지 않았다.

### 4.8 `billing_month` 3단 방어

```
① @validates 로 day=1 강제  (BillingMonthMixin)
② CHECK substr(billing_month, 9, 2) = '01'
③ UNIQUE (floor_id, billing_month)
```

SELECT → 검사 → INSERT 만으로는 경합에 취약하므로 DB 제약을 최종 방어선으로 뒀다.

---

## 5. 의도적으로 유지한 기존 behavior

**계산 로직은 한 줄도 바꾸지 않았다.** 아래는 전부 원본 그대로다.

- `round_up_to_10` — `math.ceil(float(amount)/10)*10`
- grossing-up — `original = total + welfare_total + voucher_total`
- TV 요금 배분 2모드 (`EQUAL` / `INDIVIDUAL`)
- 복지·바우처 할인의 **입력값 우선 / 설정값 fallback** 분기
- `total_usage == 0` 일 때 균등 분배 fallback
- 음수 clamp
- 수도 제외 세대 처리
- 공동 공과금 배분 2방식 (`BY_RESIDENTS` / `BY_UNITS`)
- 정산월 vs 고지월의 **독립 개념**
- 스냅샷 불변성 — 과거 계산이 현재 Unit 마스터 변경에 영향받지 않음

**템플릿 호환을 위해 남긴 읽기 전용 property** (덕분에 템플릿 5개를 안 고쳤다):

```python
ElectricBill.monthly_details   # → [{'month': 'YYYY-MM', ...}]  레거시와 동일한 문자열 형식
FinalInvoice.additional_charges # → self.charges
FinalInvoice.billable_charges_total / carryover_charges_total
```

**의도적으로 제약을 걸지 않은 것** (도메인이 허용하므로):
음수 사용량 / 음수 배분액 / 환급(음수 charge) / 음수 `total_amount`. 이를 테스트로 고정해 두었다.

**`excluded_units` 침묵 실패** — 파싱 실패 시 제외가 무시되는 현재 동작을 **고치지 않고 테스트로 고정**만 했다. 이번 범위가 아니다.

---

## 6. 해결한 문제

분석 문서(`docs/codebase-analysis/06-technical-debt.md`)의 부채 번호와 연결한다.

| ID | 내용 | 해결 |
|---|---|---|
| **C-2** | DB 자격증명 소스 하드코딩 (`power_user:mslee0702`) | `config.py` + 환경변수. 소스에 없음 |
| **C-3** | `debug=True` + `0.0.0.0` 바인딩 | 기본 `debug=False`, `127.0.0.1` |
| **C-4** | 설정 Import 가 `Floor` 전체 삭제 | **merge/upsert 재설계.** 삭제 없음, unit id 보존, 검증 실패 시 DB 무변경 |
| **C-5** | `SQLSchema.txt` ↔ 모델 불일치 | Alembic 도입 + DEPRECATED 배너 |
| **C-6** | 이월 판정 문자열 매칭 | `is_carryover` 컬럼 |
| **C-8** | 전기 고지 덮어쓰기 응답 키 불일치 | `exists` / `already_exists` 양쪽 반환 |
| **H-3** | 잔액 계산 4중 중복 | `balance.py` 단일 구현 |
| **H-9** | 잔액 조회 N+1 | `all_unit_balances()` 일괄 집계 |
| **H-10** | 스키마 관리 부재 | Alembic + `render_as_batch=True` |
| **M-4** | 설정 키 목록 5곳 중복 | `settings_registry.py` (시딩 누락 3건 동시 해소) |
| **I-1…I-15** | 무결성 결함 15종 | FK 명시, 다형 참조 CHECK, `billing_month` 3단 방어, UNIQUE 8종 추가 |
| **D-1…D-12** | 스키마 3중 불일치 | SoT 단일화로 소멸 |

**실행으로 검증된 것:**

```
flask db upgrade      → 17개 테이블 생성
flask db migrate      → "No changes in schema detected"  (모델↔마이그레이션 drift 없음)
flask seed-settings   → 9개 전부 시딩
flask check-db        → 검사 13건 중 0건 실패
PRAGMA journal_mode   → wal
```

`flask check-db` 최신 실행 결과(전 항목 OK): `foreign_keys_enabled` / `integrity_check` / `foreign_key_check` / `no_orphan_rows` / `billing_month_normalized` / `invoice_item_polymorphic_reference` / `unit_snapshot_complete` / `charged_amount_multiple_of_10` / `final_invoice_total_consistent` / `electric_billing_months_count` / `money_columns_integer` / `decimal_columns_parseable` / `carryover_flag_review`

---

## 7. 현재 테스트 결과

```
$ venv/bin/python -m pytest tests -q
3 failed, 204 passed, 2 warnings in 4.33s
```

| 파일 | 개수 | 상태 |
|---|---:|---|
| `test_constraints.py` | 32 | ✅ 전부 통과 |
| `test_money.py` | 32 | ✅ |
| `test_legacy_migration.py` | 38 | ❌ 1건 실패 |
| `test_persistence.py` | 31 | ❌ 1건 실패 |
| `test_schema.py` | 21 | ✅ |
| `test_delete_policy.py` | 16 | ✅ |
| `test_settings.py` | 16 | ✅ |
| `test_carryover.py` | 10 | ✅ |
| `test_snapshot.py` | 7 | ✅ |
| `test_golden.py` | 4 | ❌ 1건 실패 |
| **합계** | **207** | **204 pass / 3 fail** |

골든 마스터 기준선 `tests/golden/expected.json` (8.7KB) 은 생성되어 있고, **비교 테스트(`test_golden_matches`)는 통과한다.**

경고 2건은 `migrations/env.py:21` 의 `get_engine()` Deprecation (Flask-Migrate 템플릿 기본 생성물). 동작에는 영향 없다.

---

## 8. 실패 중인 테스트와 원인

### ❌ 1. `test_golden.py::test_dataset_covers_all_branches` — **테스트 데이터 결함**

```
assert any(d["voucher_discount"] != "0.00" for d in details)
E   assert False
```

**원인:** 바우처 분기가 실제로 한 번도 실행되지 않는다.
`tests/golden/dataset.py:167` 에서 `voucher: 3000` 을 **2층** 3개월 묶음에 넣었는데, 바우처 대상 세대는 `UNIT_SPECS` 상 **B102호(B1층)** 뿐이다(`dataset.py:44`). 2층 세대(201·202·203호)에는 `electric_voucher` 가 없다.

`app.py:934` 의 `voucher_units = [u for u in units if u.electric_voucher]` 가 빈 리스트가 되어 `voucher_per_unit = 0` 으로 떨어진다. 게다가 **B1층은 전기 계산 자체가 없다** — `_calc_electric_single` 은 1층, `_calc_electric_bundle` 은 2층만 쓴다.

**애플리케이션 버그가 아니다.** 데이터셋이 "모든 분기를 덮는다"고 주장하면서 실제로는 바우처 경로를 비워 둔 것이다. 이 테스트는 정확히 그 누락을 잡아냈으므로 **테스트가 제 역할을 한 것**이다.

**주의:** 데이터셋을 고치면 골든 기준선이 바뀐다. `--update-golden` 재생성이 필요하다.

### ❌ 2. `test_persistence.py::test_electric_overwrite_replaces_bill` — **테스트 기대값이 MySQL 전제**

```
assert bill.id != original_id
E   assert 1 != 1
```

**원인:** SQLite 의 rowid 재사용.
`app.py:876` 은 덮어쓰기 시 기존 고지서를 `delete` 하고 새로 만든다. MySQL 의 `AUTO_INCREMENT` 는 카운터가 되감기지 않아 새 id 가 나오지만, SQLite 는 `AUTOINCREMENT` 키워드가 없으면 `max(rowid)+1` 을 쓴다. 유일한 행을 지우면 max 가 0 이 되어 **다시 id=1** 이 나온다.

**덮어쓰기 자체는 정상 동작한다** — 같은 테스트의 `bill.total_amount == 90000` 은 통과했다.

**애플리케이션 버그가 아니다.** 테스트가 검증하려던 것("이전 고지서가 남지 않고 교체된다")은 id 로 확인할 대상이 아니다. 삭제+재생성은 한 트랜잭션 안에서 일어나고, 기존 고지서가 정산서에 쓰이고 있으면 `_electric_bill_in_use()` 가 RESTRICT 로 먼저 막는다.

### ❌ 3. `test_legacy_migration.py::test_full_migration_run` — **스크립트의 실제 결함**

```
TypeError: SQLite DateTime type only accepts Python datetime and date objects as input.
[SQL: INSERT INTO floors (...) VALUES (...)]
[parameters: {'created_at': '2025-03-01 00:00:00', ...}]
```

**원인:** `scripts/migrate_mysql_to_sqlite.py` 가 `created_at`/`updated_at` 을 **아무 변환 없이 통과**시킨다.

```python
"created_at": row.get("created_at") or datetime.utcnow(),
```

이 패턴이 스크립트 전체에 14곳 있다. `to_month()`(146행)·`to_date()`(167행) 같은 날짜 변환 헬퍼는 있지만 **`created_at`/`updated_at` 용 datetime 정규화 헬퍼가 없다.**

테스트는 레거시 원본을 SQLite 로 흉내 내므로 `TEXT` 문자열이 그대로 돌아오고, SQLAlchemy `DateTime` 이 이를 거부한다.

**실제 MySQL 에서도 터지는가 — 확인하지 못했다.** `mysql-connector` 는 DATETIME 을 `datetime` 객체로 반환하므로 실환경에서는 통과할 가능성이 높다. 그러나 **MySQL 접근이 불가능해 검증할 수 없었다.** 확인 전까지는 실제 결함으로 취급해야 한다.

---

## 9. 아직 해결하지 않은 문제

### 이번 작업으로 생긴 것

1. **위 실패 3건** (§8)
2. **골든 마스터 기준선의 태생적 한계** — 기준선을 MySQL 버전이 아니라 **SQLite 전환 직후 구현에서 뽑았다**(MySQL 접근 불가). 따라서 이것은 *전환이 금액을 바꾸지 않았다는 증명이 아니라*, **이후 리팩토링에 대한 회귀 방어선**이다. 이 한계는 `tests/test_golden.py` 상단에도 적어 두었다.
3. **레거시 이전 스크립트가 실데이터로 검증되지 않았다** — introspection 으로 스키마 가정을 피했지만, 실제 운영 DB 로 돌려본 적이 없다. 반드시 `--report-only` 부터 실행할 것.
4. **`restore_database()` 는 CLI/UI 미노출** — 함수만 제공(설계 §26 대로).

### 이전부터 있었고 이번 범위가 아니었던 것

5. **C-1 공동 공과금 FE/BE 계약 파손** — 프론트엔드 필드명과 백엔드 파싱이 어긋나 **총액이 0 으로 저장된다.** 백엔드 배분 로직 자체는 정상이며 테스트도 통과한다. 실제로 금액이 누락되는 **유일하게 남은 Critical**이다.
6. **`excluded_units` 침묵 실패** (§5)
7. **인증 없음** — 로컬 단독 사용 전제. 기본 바인딩이 `127.0.0.1` 인 이유.

---

## 10. 다음으로 해야 할 작업 순서

**A. 실패 3건 정리 (커밋 전 필수)**

1. `test_electric_overwrite_replaces_bill` — id 비교를 걷어내고 **의도**를 검증하도록 수정. `ElectricBill.query.count() == 1` + `total_amount == 90000` + 이전 detail/month 가 남지 않았는지. → 가장 쉽고 위험 없음
2. `migrate_mysql_to_sqlite.py` — `to_datetime()` 헬퍼 추가 후 14곳 `created_at`/`updated_at` 에 적용. 문자열/`datetime`/`None` 모두 수용 → 테스트 통과
3. `dataset.py` — 바우처 분기를 실제로 태운다. B1층 전기 계산을 추가하거나, 2층 세대 하나에 `electric_voucher` 를 준다. **수정 후 `--update-golden` 으로 기준선 재생성 필수**

**B. 전체 재검증**

```bash
venv/bin/python -m pytest tests -q          # 207 전부 통과 확인
FLASK_APP=app.py venv/bin/flask check-db    # 13건 0실패 확인
```

**C. 커밋** — `data/` 가 들어가지 않았는지 반드시 확인 (이전 세션에서 `venv/` 2,504개 파일이 커밋에 섞여 push 가 HTTP 400 으로 실패한 전례가 있다)

```bash
git status --short          # data/ 없어야 함
git add -A && git status    # 스테이징 후 재확인
```

**D. 이후 리팩토링 (우선순위 순)**

1. **C-1 공동 공과금 계약 복구 + FE/BE 스키마 도입(H-4)** — 실제로 금액이 누락되는 유일한 Critical. DB·테스트 기반이 갖춰진 지금이 안전하다
2. **Domain Layer 추출(H-1/H-2)** — `calculate_electric` 의 배분 로직 60줄을 `domain/electric.py` 순수 함수로. 현재는 HTTP 를 거쳐야만 검증 가능하다
3. **Blueprint 분할 + 오류/로깅 인프라(H-8)**

---

## 11. 작업 재개 시 먼저 읽어야 할 파일

**순서대로 읽으면 맥락이 복원된다.**

| 순서 | 파일 | 왜 |
|---|---|---|
| 1 | 이 문서 | 현재 위치 |
| 2 | `docs/refactoring/database/01-target-sqlite-schema.md` | **스키마 설계 근거 전체.** 왜 이 타입/제약인지 |
| 3 | `models.py` | 스키마 SoT. 실제 정의 |
| 4 | `db_types.py` | `Money`/`ExactDecimal` — 금액 동작의 핵심 |
| 5 | `tests/conftest.py` | 테스트가 왜 파일 기반 DB 와 `upgrade()` 를 쓰는지 |
| 6 | `docs/refactoring/database/00-current-db-audit.md` | 부채 ID(D/M/I) 정의. MySQL 접근 불가 기록 |

**필요할 때만:**
- `docs/refactoring/database/02-migration-plan.md` — 레거시 이전을 만질 때
- `docs/refactoring/database/03-test-plan.md` — 테스트를 추가할 때
- `docs/codebase-analysis/06-technical-debt.md` — 부채 번호(C/H/M/L)를 찾을 때
- `README.md` — 실행 방법

---

## 12. 주의해야 할 설계 결정

재개하는 사람이 **모르고 깨뜨리기 쉬운** 것들이다.

### ⚠️ `db.create_all()` 을 다시 쓰지 말 것
스키마는 `flask db upgrade` 로만 만든다. 테스트도 마찬가지다. `create_all()` 은 CHECK 제약과 마이그레이션 정확성을 검증하지 않고 우회한다.

### ⚠️ SQLite 의 `foreign_keys` 는 기본 OFF
커넥션마다 켜야 한다. `extensions.py:register_sqlite_pragmas()` 훅이 담당한다. **새 엔진/커넥션 경로를 만들면 FK 가 조용히 꺼진다.** `test_schema.py` 가 이를 실제로 검증한다 — "스키마에 FK 가 있으니 되겠지"로 넘기면 안 된다.

### ⚠️ TEXT 컬럼의 CHECK 는 `CAST` 없이는 무용지물
SQLite 타입 친화도(affinity) 때문에 `base_amount >= 0` 은 문자열 비교가 된다. 반드시 `CAST(base_amount AS REAL) >= 0` 형태로 쓸 것.

### ⚠️ Alembic 은 반드시 `render_as_batch=True`
SQLite 는 제약을 `ALTER` 로 바꿀 수 없다. batch 모드가 테이블 재생성으로 우회한다. 끄면 마이그레이션이 실패한다.

### ⚠️ 새 마이그레이션이 커스텀 타입을 쓰면 `import db_types` 를 직접 추가
Alembic autogenerate 는 `db_types.Money()` 를 렌더링하지만 **import 문을 넣어주지 않는다.** 이미 한 번 겪은 문제다.

### ⚠️ SQLite 는 id 를 재사용한다
`AUTOINCREMENT` 를 쓰지 않으므로 `max(rowid)+1` 이다. **id 의 단조 증가에 의존하는 코드나 테스트를 쓰지 말 것.** §8-2 가 정확히 이 함정이다.

### ⚠️ 계산 결과는 1원도 달라지면 안 된다
DB 엔진 교체를 이유로 금액이 바뀌면 "SQLite 특성"으로 넘기지 말고 원인을 조사할 것. 골든 마스터는 **0원 허용오차**로 비교한다.

### ⚠️ `is_carryover` 문자열 판별은 마이그레이션 스크립트 안에서만
신규 코드에서 `description.startswith('[이월]')` 같은 판별을 되살리지 말 것. 잔액 계산은 `balance.py` 단일 구현만 쓴다.

### ⚠️ 레거시 MySQL 원본에 절대 쓰기 금지
`scripts/migrate_mysql_to_sqlite.py` 는 `SET SESSION TRANSACTION READ ONLY` 로 연다. DELETE/UPDATE 를 추가하지 말 것.

### ⚠️ 설정 Import 는 절대 삭제하지 않는다
merge/upsert 만 한다(C-4). "간단하니까 지우고 다시 넣자"로 되돌리면 과거 정산 데이터가 날아간다.

### ⚠️ 템플릿 호환 property 를 지우지 말 것
`ElectricBill.monthly_details` 와 `FinalInvoice.additional_charges` 는 죽은 코드처럼 보이지만 **템플릿이 쓰고 있다.** 특히 `monthly_details` 는 `month` 를 레거시와 동일한 `'YYYY-MM'` 문자열로 돌려준다.

### ⚠️ 고지월은 이제 비워 둘 수 없다
`DATE NOT NULL` 이라 빈 값이면 거부한다(`app.py:844`). 정산월을 임의 대입하면 **정산월 ≠ 고지월** 이라는 도메인 개념이 훼손되므로 추정하지 않는다. UI 는 이미 `required` 로 막고 있다.

---

## 부록: 자주 쓰는 명령

```bash
source venv/bin/activate

flask db upgrade                     # 스키마 생성/갱신
flask db migrate -m "..."            # 모델 변경 후 마이그레이션 생성
flask seed-settings                  # 설정 시딩 (멱등)
flask check-db                       # 정합성 13종 검사
flask backup-db                      # SQLite 네이티브 백업

pytest tests -q                      # 전체
pytest -m "not slow"                 # 서브프로세스 테스트 제외
pytest tests/test_golden.py --update-golden   # 기준선 재생성 (의도적일 때만)

python scripts/migrate_mysql_to_sqlite.py --mysql-uri "..." --report-only
```
