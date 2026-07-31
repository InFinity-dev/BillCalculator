# 02. Migration 계획

> 선행: [01-target-sqlite-schema.md](01-target-sqlite-schema.md)

**두 종류의 마이그레이션을 엄격히 분리한다.**

| | A. Schema Migration | B. Legacy Data Migration |
|---|---|---|
| 목적 | SQLite 스키마 버전 관리 | 기존 MySQL 데이터 이전 |
| 도구 | **Alembic (Flask-Migrate)** | `scripts/migrate_mysql_to_sqlite.py` |
| 실행 빈도 | 스키마 변경마다 | **일회성** |
| 대상 | 빈 DB 또는 기존 SQLite DB | MySQL → 신규 SQLite |
| 커밋 | `migrations/versions/` | `scripts/` |

---

## A. Schema Migration (Alembic)

### A-1. 구성

```
migrations/
├── env.py              ← target_metadata = db.metadata, render_as_batch=True
├── script.py.mako
└── versions/
    └── 0001_initial_sqlite_schema.py   ← baseline
```

**`render_as_batch=True` 필수**: SQLite는 `ALTER TABLE ... DROP COLUMN` / 제약 변경을 지원하지 않는다.
Alembic의 batch 모드가 "새 테이블 생성 → 복사 → 교체" 를 자동 수행한다. 향후 마이그레이션의 전제 조건이다.

### A-2. Baseline 정책

기존 MySQL DB와의 연속성을 갖는 마이그레이션을 만들지 **않는다.**
SQLite 스키마는 **새 스키마**이므로 `0001_initial_sqlite_schema` 하나로 전체를 생성한다.
기존 MySQL 데이터는 B(레거시 마이그레이션)로 별도 이전한다.

### A-3. Fresh Install 흐름

```
git clone
→ pip install -r requirements.txt
→ flask db upgrade          # data/bill_calculator.db 생성 + 전체 스키마
→ flask seed-settings       # 설정 기본값 시딩 (레지스트리 기준, 멱등)
→ python app.py
```

`db.create_all()`은 **애플리케이션 코드에서 완전히 제거**한다.
`SQLSchema.txt`는 **스키마 생성 수단이 아님**을 파일 상단에 명시하고 `docs/legacy/` 로 이동한다.

---

## B. Legacy Data Migration (MySQL → SQLite)

### B-1. 안전 원칙

| # | 원칙 | 구현 |
|---|---|---|
| 1 | **MySQL 원본을 절대 변경하지 않는다** | 커넥션을 읽기 전용으로 열고, 스크립트에 `INSERT/UPDATE/DELETE/ALTER/DROP` SQL을 한 줄도 포함하지 않는다. 실행 시작 시 `SET SESSION TRANSACTION READ ONLY` |
| 2 | **대상 SQLite를 덮어쓰지 않는다** | 대상 파일이 이미 존재하고 비어있지 않으면 **거부**. `--force` 명시 필요 |
| 3 | **스키마를 가정하지 않는다** | `information_schema.COLUMNS`로 실제 컬럼을 introspect 후 존재하는 컬럼만 SELECT |
| 4 | **애매한 데이터를 임의 변환하지 않는다** | `requires-review`로 리포트하고 해당 행을 **건너뛰거나 중단** (모드 선택) |
| 5 | **전 과정이 단일 트랜잭션** | 검증 실패 시 SQLite 측 전체 롤백 |

### B-2. 실행 순서

```
Extract  → MySQL에서 읽기 전용 SELECT (FK 위상 순서)
Validate → 값 수준 검증, 위반 수집
Transform→ 타입/구조 변환
Insert   → SQLite에 삽입 (PK 보존)
Verify   → row count + 금액 aggregate + balance 비교
```

**PK 보존**: 원본 `id`를 그대로 유지한다. FK 재매핑이 필요 없어지고, 이전 전후 비교가 단순해진다.
삽입 후 SQLite의 `sqlite_sequence`를 최대 id로 맞춘다.

### B-3. 테이블 이전 순서 (FK 위상 정렬)

```
1. settings
2. floors
3. units
4. electric_bills
5. electric_bill_months      ← electric_bills.monthly_details JSON 에서 생성
6. electric_readings
7. electric_bill_details
8. water_bills
9. water_bill_details
10. common_bills
11. common_bill_details
12. invoice_combinations
13. invoice_combination_items
14. final_invoices
15. final_invoice_charges    ← final_invoices.additional_charges JSON 에서 생성
16. payments
```

---

## C. 컬럼별 변환 명세

### C-1. Money (`DECIMAL(x,2)` → `INTEGER`)

**대상**: `electric_bills.{total_amount, welfare_discount, voucher_discount, tv_fee_total}`,
`*_bill_details.charged_amount`, `water_bills.{total_amount, welfare_discount_total}`,
`common_bills.total_amount`, `final_invoices.{electric,water,common,total}_amount`,
`payments.payment_amount`

```
변환:  Decimal('12500.00') → 12500
검증:  v != v.to_integral_value()  →  ⚠️ requires-review
        (소수부가 0이 아닌 통화 값은 정보 손실이 발생하므로 임의 반올림하지 않는다)
NULL:  → 0 (해당 컬럼은 목표 스키마에서 NOT NULL DEFAULT 0)
```

`charged_amount`는 추가 검증: `v % 10 != 0` → ⚠️ 경고 (10원 올림 정책 위반 데이터)

### C-2. ExactDecimal (`DECIMAL(x,2)` → `TEXT`)

**대상**: `electric_readings.{previous,current}_reading`,
`electric_bill_details.{usage_amount, base_amount, welfare_discount, voucher_discount, tv_fee, final_amount}`,
`water_bill_details.{base_amount, welfare_discount, final_amount}`,
`common_bill_details.amount`

```
변환:  Decimal('12345.67') → '12345.67'   (quantize 0.01, ROUND_HALF_UP)
검증:  reading < 0                   → ⚠️ requires-review (CHECK 위반 예정)
        final_amount < 0             → ⚠️ requires-review (CHECK 위반 예정)
        usage_amount / base_amount   → 음수 허용, 검증 안 함
NULL:  → '0.00'
```

### C-3. Date (`DATE` → `DATE`, day=1 정규화)

**대상**: `*.billing_month`

```
변환:  date(2025, 3, 17) → date(2025, 3, 1)
검증:  d.day != 1  →  ⚠️ 경고 후 정규화 (day=1이 도메인 정의이며 앱도 항상 그렇게 저장)
UNIQUE 충돌:
   electric_bills(floor_id, billing_month) 중복  →  ❌ requires-review, 중단
   water_bills(billing_month) 중복                →  ❌ requires-review, 중단
```

`payments.payment_date`는 실제 일자이므로 **정규화하지 않는다.**

### C-4. Enum (`ENUM` → `VARCHAR + CHECK`)

```
common_bills.distribution_method:
   'BY_RESIDENTS' | 'BY_UNITS'  → 그대로
   그 외 / NULL                  → ⚠️ 'BY_RESIDENTS' 로 보정 + 경고 (앱 기본값과 동일)

electric_bills.tv_distribution_mode:
   'INDIVIDUAL' | 'EQUAL'       → 그대로
   그 외 / NULL                  → ⚠️ 'INDIVIDUAL' 로 보정 + 경고 (앱 기본값과 동일)

invoice_combination_items.item_type:
   'ELECTRIC' | 'WATER' | 'COMMON' → 그대로
   그 외                            → ❌ requires-review, 중단
```

### C-5. `monthly_details` JSON → `electric_bill_months` 행

```
입력:  [{month, amount, welfare, voucher, tv_fee}, ...]

month 필드 타입 분기 (I-14의 원인):
   'YYYY-MM'      → date(YYYY, MM, 1)
   'YYYY-MM-DD'   → date(YYYY, MM, 1)
   date/datetime  → d.replace(day=1)
   '' / None      → ❌ requires-review  (고지월 없는 명세는 의미 불명 → 임의 추정 금지)
   그 외          → ❌ requires-review

amount/welfare/voucher/tv_fee → Money 변환 (C-1)
   None → 0

중복 month  → ❌ requires-review (UNIQUE(electric_bill_id, billing_month) 위반)
JSON이 NULL/빈 배열 → 행을 만들지 않음 (정상. billing_months_count 는 원본 값 유지)
```

> `month`가 비어 있는 항목을 헤더의 `billing_month`로 대체하지 **않는다.**
> 정산월과 고지월은 독립된 개념이므로(분석 문서 B-7) 임의 대입은 데이터 왜곡이다.

### C-6. `additional_charges` JSON → `final_invoice_charges` 행 (**핵심**)

```
입력:  [{description, amount}, ...]      ← type/is_carryover 는 원본에 없음

description → 그대로 (NULL/'' → '(설명 없음)' + ⚠️ 경고)
amount      → Money 변환 (C-1). 음수 허용
sort_order  → 배열 인덱스
is_carryover→ ★ 레거시 문자열 규칙으로 1회 판정:
                 desc.lower() 에 '미납'|'초과납부'|'환급'|'이월' 중 하나라도 포함 → True
                 그 외 → False
```

**이 문자열 판정은 마이그레이션에서 단 한 번만 사용되며, 이후 애플리케이션 코드에서는 절대 사용하지 않는다.**
(`app.py:1405,1457,1561,1611`의 4중 중복은 전부 `is_carryover` 컬럼 조회로 대체)

**판정 근거**: 레거시 데이터에는 이월 여부를 알 수 있는 다른 정보가 존재하지 않는다.
기존 시스템의 잔액 계산이 이 규칙으로 동작해 왔으므로, **동일한 규칙을 적용해야 이전 후 balance가 보존된다.**

**애매 케이스 처리**: `'[이월]'` 접두사가 없으면서 키워드만 포함하는 항목
(예: `"보증금 환급"`, `"관리비 미납분"`)은 기존 시스템에서 **잔액 계산에서 제외**되고 있었다.
→ 동일하게 `is_carryover=True`로 이전해야 balance가 보존된다.
→ 단, **`ambiguous_carryover` 리포트에 별도 수집**하여 사용자가 사후 검토할 수 있게 한다.
   (임의로 `False`로 바꾸면 balance가 달라진다 = 데이터 왜곡)

### C-7. nullable FK (`invoice_combination_items`)

```
검증 (신규 CHECK 제약 대응):
   item_type='ELECTRIC' 인데 electric_bill_id IS NULL       → ❌ requires-review
   item_type 과 다른 FK가 함께 채워져 있음                    → ❌ requires-review
   3개 모두 NULL                                             → ❌ requires-review

참조 무결성:
   electric_bill_id 가 존재하지 않는 electric_bills.id 참조   → ❌ requires-review
```

> SQLSchema.txt 기준 DB(`item_id` 단일 컬럼)로 만들어진 데이터가 존재할 가능성에 대비해,
> introspect 결과에 `item_id`만 있고 3개 FK가 없으면 `item_type` + `item_id` 로 매핑한다.

### C-8. `unit_snapshot` JSON

```
변환 없음. 그대로 복사.
검증:  필수 7키 존재 여부 확인 (unit_name, electric_welfare, electric_voucher,
                                has_tv, water_welfare, residents_count, is_vacant)
       누락 시 ⚠️ 경고 (broken snapshot 리포트). 값을 채워 넣지 않는다.
NULL:  → NULL 유지 (목표 스키마에서도 nullable)
```

### C-9. 삭제되는 컬럼

| 컬럼 | 처리 |
|---|---|
| `electric_bills.tv_units_count` | 이전하지 않음. **값이 0이 아니면 ⚠️ 리포트** (읽는 코드가 없어 손실 없음을 확인) |
| `final_invoices.memo` | 이전하지 않음. `invoice_combinations.memo`와 동일한지 검증 후, **다르면 ⚠️ 리포트** |
| `electric_bills.monthly_details` | C-5로 분해 후 원본 컬럼 미생성 |
| `final_invoices.additional_charges` | C-6으로 분해 후 원본 컬럼 미생성 |

### C-10. Boolean

```
MySQL TINYINT(1) → SQLite INTEGER 0/1
NULL → 컬럼별 도메인 기본값 (has_tv → 1, 그 외 → 0)
      ※ 목표 스키마에서 NOT NULL 이 되므로 NULL 을 남길 수 없다
```

### C-11. `units` UNIQUE(floor_id, unit_name)

```
검증: 같은 층에 동일 unit_name 이 2개 이상  → ❌ requires-review, 중단
      (자동 rename 하지 않는다. 어느 쪽이 진짜인지 코드로 판단할 수 없다)
```

### C-12. detail 테이블 UNIQUE(bill_id, unit_id)

```
검증: 중복 존재 → ❌ requires-review, 중단
      (중복 detail 은 금액 이중계상을 의미하므로 자동 병합/삭제 금지)
```

---

## D. `requires-review` 처리 정책

```
scripts/migrate_mysql_to_sqlite.py --report-only     # 검증만 수행, 리포트 출력
scripts/migrate_mysql_to_sqlite.py                   # 위반 1건이라도 있으면 중단
scripts/migrate_mysql_to_sqlite.py --skip-invalid    # 위반 행을 건너뛰고 진행 (리포트 필수 확인)
```

리포트 출력 형식 (`data/migration_report.json`):

```json
{
  "generated_at": "...",
  "source": "mysql://.../bill_calculator",
  "target": "data/bill_calculator.db",
  "blocking": [
    {"table":"units","id":12,"issue":"duplicate_unit_name","detail":"floor_id=3, unit_name='101호'"}
  ],
  "warnings": [
    {"table":"electric_bills","id":7,"issue":"tv_units_count_nonzero","detail":"value=3"}
  ],
  "ambiguous_carryover": [
    {"final_invoice_id":88,"description":"보증금 환급","amount":-50000,
     "assigned_is_carryover":true,
     "note":"레거시 규칙상 잔액에서 제외되어 왔음. 실제 이월 항목인지 확인 필요"}
  ]
}
```

---

## E. 이전 후 검증 (Verify)

### E-1. Row count 비교 (13개 테이블)

`floors, units, electric_bills, electric_readings, electric_bill_details, water_bills, water_bill_details, common_bills, common_bill_details, invoice_combinations, invoice_combination_items, final_invoices, payments`

추가 (파생 테이블):
- `electric_bill_months` == `Σ len(monthly_details)` (유효 항목 기준)
- `final_invoice_charges` == `Σ len(additional_charges)`

### E-2. 금액 aggregate 비교

| 항목 | MySQL 측 쿼리 | SQLite 측 |
|---|---|---|
| `Σ electric_bill_details.charged_amount` | `SELECT SUM(charged_amount)` | 동일 |
| `Σ water_bill_details.charged_amount` | 〃 | 동일 |
| `Σ common_bill_details.charged_amount` | 〃 | 동일 |
| `Σ final_invoices.total_amount` | 〃 | 동일 |
| `Σ payments.payment_amount` | 〃 | 동일 |
| `Σ electric_bills.total_amount` | 〃 | 동일 |
| `Σ water_bills.total_amount` | 〃 | 동일 |
| `Σ common_bills.total_amount` | 〃 | 동일 |

**허용 오차 0.** 1원이라도 다르면 실패로 처리하고 원인을 리포트한다.

### E-3. 세대별 balance 비교 ★가장 중요

MySQL 원본에 대해 **기존 알고리즘(문자열 판정)** 으로 계산한 세대별 잔액과,
SQLite 이전 후 **신규 알고리즘(`is_carryover` 컬럼)** 으로 계산한 잔액이 **완전히 일치**해야 한다.

```
for each unit:
    legacy_balance = Σ(electric+water+common + Σ(문자열판정 비이월 charge)) − Σ(payments)
    new_balance    = Σ(electric+water+common + Σ(is_carryover=0 charge))   − Σ(payments)
    assert legacy_balance == new_balance
```

이것이 **C-6 변환이 도메인 결과를 바꾸지 않았다는 증명**이다.

### E-4. 참조 무결성

```sql
PRAGMA foreign_key_check;   -- 결과가 비어 있어야 함
PRAGMA integrity_check;     -- 'ok' 여야 함
```

---

## F. MySQL 접근 불가 시 대응

**[확인된 사실]** 현재 환경에서 `power_user` 자격증명이 유효하지 않아 실제 MySQL DB에 접근할 수 없다.

따라서:

1. 마이그레이션 스크립트는 **접속 정보를 CLI 인자/환경변수로 받는다** (하드코딩 금지)
   ```
   python scripts/migrate_mysql_to_sqlite.py \
       --mysql-uri "mysql+mysqlconnector://user:pw@host/bill_calculator" \
       --sqlite-path data/bill_calculator.db
   ```
2. 스크립트는 **실제 스키마를 introspect** 하여 동작하므로, MySQL 측이 `SQLSchema.txt` 기준이든
   `db.create_all()` 기준이든 모두 처리한다.
3. **스크립트 자체의 변환 로직은 SQLite→SQLite 시뮬레이션 테스트로 검증한다.**
   레거시 형태(JSON 컬럼 + DECIMAL)를 가진 임시 SQLite DB를 만들어 변환 함수를 통과시키고,
   E-1~E-4 검증이 통과하는지 확인한다. → [03-test-plan.md](03-test-plan.md) T-M 계열

---

## G. 롤백 전략

| 상황 | 대응 |
|---|---|
| 마이그레이션 중 실패 | SQLite 트랜잭션 전체 롤백 + 생성 중이던 파일 삭제. **MySQL 원본 무영향** |
| 이전 후 검증 실패 | SQLite 파일을 `.failed` 로 리네임하고 리포트 보존. MySQL 원본 그대로 사용 가능 |
| 운영 중 문제 발견 | MySQL 원본이 그대로 남아 있으므로 이전 버전 코드로 복귀 가능 (`git revert`) |

**MySQL 원본은 마이그레이션 성공 후에도 사용자가 직접 삭제하기 전까지 보존된다.**
스크립트는 원본 삭제를 제안하거나 수행하지 않는다.

---

다음: [03-test-plan.md](03-test-plan.md)
