# 00. 현재 Database 감사 (Current DB Audit)

> 감사 기준: `master` @ `273f01d`
> 감사 일자: 2026-07-31
> 선행 문서: [../../codebase-analysis/](../../codebase-analysis/) 8종

---

## 1. 실제 MySQL DB 접근 시도 결과 — **확인 불가**

**[확인된 사실]**

| 항목 | 결과 |
|---|---|
| MySQL 서버 (127.0.0.1:3306) | **가동 중** (포트 OPEN) |
| `power_user` / TCP 접속 | `ERROR 1045 (28000): Access denied` |
| `power_user` / Unix socket 접속 | `ERROR 1045 (28000): Access denied` |
| `root` (무암호) | `ERROR 1045 (28000): Access denied` |

**결론**: `app.py:107`에 하드코딩된 자격증명이 **현재 로컬 환경에서 유효하지 않다.**
따라서 다음 항목은 **이번 감사에서 실측하지 못했다**:

- 실제 존재하는 테이블 목록
- 실제 컬럼 정의 (타입, nullable, default)
- **실제 FK의 `ON DELETE` 정책 (CASCADE인지 RESTRICT인지)** ← 가장 중요
- 실제 인덱스
- 실제 데이터 볼륨 및 값 분포 (소수점 존재 여부, `monthly_details.month` 타입 분포)

### 이 제약에 대한 대응 설계

1. 목표 스키마는 **ORM Model + 애플리케이션 코드가 실제로 요구하는 데이터 관계**를 기준으로 설계한다.
2. 레거시 마이그레이션 스크립트는 **스키마를 가정하지 않고 런타임에 introspect** 하도록 만든다
   (`information_schema` 조회 → 실제 컬럼 목록에 맞춰 SELECT 생성).
3. 값 수준의 불확실성(소수점, 타입 혼재, 제약 위반)은 마이그레이션이 **`requires-review`로 탐지·격리**하고, 임의 변환하지 않는다.

---

## 2. 세 소스 비교: ORM Model vs SQLSchema.txt vs 애플리케이션 요구

### 2.1 테이블 존재 여부

| 테이블 | ORM Model | SQLSchema.txt | 앱에서 사용 |
|---|:---:|:---:|:---:|
| `floors` | ✅ `app.py:129` | ✅ `:42` | ✅ |
| `units` | ✅ `:140` | ✅ `:51` | ✅ |
| `settings` | ✅ `:156` | ✅ `:70` | ✅ |
| `electric_bills` | ✅ `:165` | ✅ `:82` | ✅ |
| `electric_readings` | ✅ `:185` | ✅ `:101` | ✅ |
| `electric_bill_details` | ✅ `:197` | ✅ `:117` | ✅ |
| `water_bills` | ✅ `:214` | ✅ `:141` | ✅ |
| `water_bill_details` | ✅ `:225` | ✅ `:151` | ✅ |
| `common_bills` | ✅ `:240` | ✅ `:172` | ✅ |
| `common_bill_details` | ✅ `:252` | ✅ `:184` | ✅ |
| `invoice_combinations` | ✅ `:264` | ✅ `:203` | ✅ |
| `invoice_combination_items` | ✅ `:275` | ⚠️ `:212` **구조 상이** | ✅ |
| `final_invoices` | ✅ `:295` | ⚠️ `:227` **컬럼 부족** | ✅ |
| `payments` | ✅ `:313` | ✅ `:346` | ✅ |

### 2.2 컬럼 수준 불일치 (확정)

| # | 위치 | ORM Model | SQLSchema.txt | 판정 |
|---|---|---|---|---|
| D-1 | `floors.electric_contract_number` | `String(50)` (`:134`) | **없음** | Model이 정답 (설정/계산기 UI가 사용) |
| D-2 | `electric_bills.tv_units_count` | `Integer` (`:175`) | 하단 마이그레이션에서 추가 (`:301`) | Model에 존재하나 **항상 0, 읽는 곳 없음 → 삭제 대상** |
| D-3 | `electric_bills.monthly_details` | `JSON` (`:177`) | 하단 마이그레이션에서 추가 (`:328`) | Model이 정답 |
| D-4 | `water_bill_details.is_excluded` | `Boolean` (`:235`) | 하단 `ALTER` (`:340`) | Model이 정답 |
| D-5 | `invoice_combination_items` 키 구조 | `electric_bill_id`/`water_bill_id`/`common_bill_id` 3개 nullable FK (`:285-287`) | `item_id INT NOT NULL` 단일 컬럼 (`:216`) | **Model이 정답** — 앱이 3개 FK를 사용 (`app.py:1207-1212`, `joinedload` `:1284-1286`) |
| D-6 | `invoice_combination_items.item_type` | `String(20)` (`:280`) | `ENUM('ELECTRIC','WATER','COMMON')` (`:215`) | 값 집합은 SQLSchema가 정확, 타입은 Model이 현행 |
| D-7 | `final_invoices.additional_charges` | `JSON` (`:304`) | **없음** | Model이 정답 |
| D-8 | `final_invoices.unit_memo` | `Text` (`:307`) | **없음** | Model이 정답 |
| D-9 | `final_invoices.common_details` | `JSON` (`:303`) | ✅ 있음 (`:234`) | 일치 |
| D-10 | **FK `ON DELETE` 정책** | 미지정 → SQLAlchemy 기본 = **RESTRICT** (DDL에 `ON DELETE` 절 없음) | **명시적 CASCADE** (`:64,94,109,130,161,192,220,238,356`) | ⚠️ **정반대. 실측 불가** |
| D-11 | `electric_bills` UNIQUE | **없음** | `UNIQUE KEY (floor_id, billing_month)` (`:98`) | **SQLSchema가 도메인상 정답** — 앱이 `SELECT→검사→INSERT`로만 방어 (`app.py:756`) |
| D-12 | 인덱스 | **하나도 없음** (`index=True` 미사용) | 다수 정의 | SQLSchema 방향이 맞음 |

**결론**: `SQLSchema.txt`는 **현재 스키마의 SoT가 아니다.** 다만 `UNIQUE(floor_id, billing_month)`, FK 정책 명시, 인덱스, `item_type` 값 집합 등 **의도가 담긴 부분은 목표 스키마 설계에 반영할 가치가 있다.**

---

## 3. MySQL 특화 코드 전수

| # | 위치 | 코드 | 처리 방침 |
|---|---|---|---|
| M-1 | `app.py:8-9` | `import mysql.connector` / `from mysql.connector import Error` | **제거** |
| M-2 | `app.py:107` | `'mysql+mysqlconnector://power_user:mslee0702@localhost/bill_calculator'` | **제거** → `config.py` + 환경변수 |
| M-3 | `app.py:113-123` | `init_database()` — `CREATE DATABASE IF NOT EXISTS ... CHARACTER SET utf8mb4` | **제거** (SQLite는 파일 생성으로 대체) |
| M-4 | `app.py:109` | `pool_pre_ping`, `pool_recycle=3600` | SQLite에 무의미 → SQLite용 옵션으로 교체 |
| M-5 | `app.py:246` | `db.Enum('BY_RESIDENTS','BY_UNITS')` | SQLite는 ENUM 미지원 → `String` + `CHECK` |
| M-6 | `app.py` 25개 `db.Numeric` | MySQL `DECIMAL` 고정소수점 | SQLite는 DECIMAL 없음 → §5 참조 |
| M-7 | `app.py` 6개 `db.JSON` | MySQL native JSON | SQLite는 TEXT + JSON1 → SQLAlchemy `JSON`이 자동 처리 |
| M-8 | `requirements.txt:3` | `mysql-connector-python==9.0.0` | **런타임 필수 의존성에서 제거** → 레거시 마이그레이션 전용 optional |
| M-9 | `SQLSchema.txt` | MySQL DDL 전체 (`ENGINE=InnoDB`, `utf8mb4`, `PREPARE/EXECUTE`) | **SoT 폐기**, historical reference로 표기 |
| M-10 | `app.py:1642-1660` | `__main__` 내 부트스트랩 (`db.create_all()` + 시딩) | **제거** → Alembic + CLI 명령 |

---

## 4. Numeric 컬럼 전수 및 의미 분류

**[확인된 사실]** `db.Numeric` 25개. 각각의 도메인 의미를 코드로 추적한 결과:

### 4.1 진짜 통화 금액 (정수 원) — INTEGER 전환 대상

| 컬럼 | 근거 |
|---|---|
| `electric_bills.total_amount` | `Σ bill_amount_N`. 입력 `<input type="number" min="0">` (step 기본 1) → 정수 (`calculator.html:322`) |
| `electric_bills.welfare_discount` | 사용자 입력 합계 **또는** `설정값(정수) × month_count × 세대수` → 정수 (`app.py:804,807`) |
| `electric_bills.voucher_discount` | 동일 (`app.py:814,817`) |
| `electric_bills.tv_fee_total` | `Σ bill_tv_fee_N` 입력 → 정수 (`calculator.html:337`) |
| `electric_bill_details.charged_amount` | `ceil(x/10)*10` → **항상 10의 배수** (`app.py:842`) |
| `water_bills.total_amount` | 입력 (`calculator.html:164`) |
| `water_bills.welfare_discount_total` | 입력 또는 설정값×세대수 (`app.py:907,910`) |
| `water_bill_details.charged_amount` | `ceil(x/10)*10` (`app.py:944`) |
| `common_bills.total_amount` | 입력 |
| `common_bill_details.charged_amount` | `ceil(x/10)*10` (`app.py:989,996`) |
| `final_invoices.electric_amount` | `Σ charged_amount` → 정수 (`app.py:1228`) |
| `final_invoices.water_amount` | `Σ charged_amount` (`app.py:1231`) |
| `final_invoices.common_amount` | `Σ charged_amount` (`app.py:1235`) |
| `final_invoices.total_amount` | 위 3개 + additional(입력 정수) (`app.py:1255`) |
| `payments.payment_amount` | 입력 `<input type="number" min="0">` (`payments.html:172`) |

### 4.2 분수 의미를 갖는 배분 중간값 — 정확 Decimal 유지

| 컬럼 | 근거 |
|---|---|
| `electric_bill_details.base_amount` | `(usage/total_usage) × original` — **본질적으로 분수** (`app.py:827`) |
| `electric_bill_details.welfare_discount` | `welfare_input / len(welfare_units)` — 분수 가능 (`app.py:803`) |
| `electric_bill_details.voucher_discount` | 동일 (`app.py:813`) |
| `electric_bill_details.tv_fee` | EQUAL 모드에서 `tv_fee_total / len(units)` — 분수 가능 (`app.py:795`) |
| `electric_bill_details.final_amount` | `base − 할인 + tv` — 분수 (`app.py:838`) |
| `water_bill_details.base_amount` | `(residents/total_residents) × original` — 분수 (`app.py:934`) |
| `water_bill_details.welfare_discount` | `welfare_input / len(welfare_units)` — 분수 (`app.py:906`) |
| `water_bill_details.final_amount` | 분수 (`app.py:941`) |
| `common_bill_details.amount` | `(residents/total) × total` 또는 `total/len` — 분수 (`app.py:987,994`) |

### 4.3 계량 데이터 — 정확 Decimal 유지

| 컬럼 | 근거 |
|---|---|
| `electric_readings.previous_reading` | 입력 `step="0.01"` → **소수 실제 사용** (`calculator.html:588`) |
| `electric_readings.current_reading` | 동일 (`calculator.html:620`) |
| `electric_bill_details.usage_amount` | `curr − prev` → 소수. **음수 가능** (도메인상 허용, `app.py:825`) |

**⚠️ 중요**: 4.2와 4.3을 INTEGER로 바꾸면 **표시 금액과 사용량이 달라진다.** 반드시 정확 Decimal을 유지해야 한다.

---

## 5. SQLite의 Decimal 문제 — 실측 필요 사항

**[확인된 사실]** SQLAlchemy는 SQLite에서 `Numeric`을 사용할 때 다음 경고를 발생시킨다:

> `SAWarning: Dialect sqlite+pysqlite does *not* support Decimal objects natively, and SQLAlchemy must convert from floating point - rounding errors and other issues may occur.`

즉 `Numeric(10,2)`를 그대로 SQLite에 옮기면 **REAL(부동소수점)로 저장되어 금액 정합성이 깨질 수 있다.**
→ 목표 스키마에서는 `Numeric`을 **직접 사용하지 않는다** ([01-target-sqlite-schema.md §3](01-target-sqlite-schema.md) 참조).

---

## 6. JSON 컬럼 6개 — 개별 판정

판정 기준: **비즈니스 로직에서 집계·판정에 쓰이는가?** (쓰인다면 관계형 분리, 표시 전용 스냅샷이면 JSON 유지)

| 컬럼 | 구조 고정? | 비즈니스 집계 대상? | 문제점 | **판정** |
|---|---|---|---|---|
| `electric_bill_details.unit_snapshot` | ✅ 7키 고정 | ❌ 표시 전용 | 없음 | **JSON 유지** |
| `water_bill_details.unit_snapshot` | ✅ | ❌ (`view_water_detail.html:158`에서 인원 합산 표시) | 없음 | **JSON 유지** |
| `common_bill_details.unit_snapshot` | ✅ | ❌ (`view_common_detail.html:50,96` 표시) | 없음 | **JSON 유지** |
| `final_invoices.common_details` | ✅ 2키 | ❌ 인쇄 표시 전용 (`invoice_print.html:376`) | 없음 | **JSON 유지** |
| `electric_bills.monthly_details` | ⚠️ 5키이나 `month` **타입 불안정** | ✅ 차트 집계 (`view.html:203-213`), 5개 템플릿이 파싱 | `{% if months[0] is string %}` 분기 존재(`invoice_view.html:157`) → 과거 데이터에 `date`/`str` 혼재 시사. `view.html:205`의 `.substring(0,7)`은 문자열만 가정 → **혼재 시 차트 파손** | **관계형 테이블 분리** |
| `final_invoices.additional_charges` | ⚠️ `type`/`is_carryover` 유실 | ✅ **잔액 계산의 핵심** (4곳에서 순회 + 문자열 판정) | C-6 (문자열 키워드 매칭) | **관계형 테이블 분리 + `is_carryover` 명시 컬럼** |

**근거 보강 — `unit_snapshot`을 JSON으로 유지하는 이유**:
스냅샷은 "계산 당시 `units` 테이블의 상태"를 박제한 것이다. 관계형으로 분리하면 `units`의 스키마 진화에 따라 과거 스냅샷 테이블도 함께 변경해야 하며, 이는 **불변성을 깨뜨린다.** JSON은 이 경우 오히려 올바른 선택이다. (사용자 지침 §12와 일치)

---

## 7. Enum 컬럼

| 컬럼 | 현재 | 값 집합 | 목표 |
|---|---|---|---|
| `common_bills.distribution_method` | `db.Enum('BY_RESIDENTS','BY_UNITS')` (`:246`) | 2개 | `String(20)` + `CHECK IN (...)` |
| `electric_bills.tv_distribution_mode` | `String(20)` default `'INDIVIDUAL'` (`:174`) | `INDIVIDUAL`/`EQUAL` (`calculator.html:50-51`) | `String(20)` + `CHECK IN (...)` |
| `invoice_combination_items.item_type` | `String(20)` (`:280`) | `ELECTRIC`/`WATER`/`COMMON` (`app.py:1207-1212`) | `String(20)` + `CHECK IN (...)` |
| `payments.payment_method` | `String(50)` default `'계좌이체'` (`:320`) | 현금/계좌이체/카드/기타 (`payments.html:180-183`) | **자유 문자열 유지** (사용자가 '기타'로 임의 입력할 여지, CHECK 미적용) |

---

## 8. 발견된 정합성 문제 (DB 계층 한정)

| # | 문제 | 위치 | 현재 방어 | 목표 |
|---|---|---|---|---|
| I-1 | **FK ON DELETE 정책 불일치** (CASCADE vs RESTRICT) | D-10 | 없음 | 도메인 기준으로 **명시** |
| I-2 | **`invoice_combination_items` 다형 참조 무제약** — 전부 NULL / 2개 이상 non-NULL / `item_type` 불일치 가능 | `app.py:275-292` | 애플리케이션 분기만 | **CHECK 제약** |
| I-3 | **이월 판정이 문자열 매칭** | `app.py:1405,1457,1561,1611` | 없음 | **`is_carryover` 컬럼** |
| I-4 | **`electric_bills` 중복 방지가 앱 레벨 SELECT→INSERT** | `app.py:756` | TOCTOU 창 존재 | **UNIQUE(floor_id, billing_month)** |
| I-5 | **`billing_month` day=1 정규화가 3곳에 분산** | `app.py:720,867,972` | 앱 코드만 | **CHECK + 모델 validator** |
| I-6 | **detail 테이블에 (bill, unit) 중복 방지 없음** | 4개 detail 테이블 | 없음 | **UNIQUE(bill_id, unit_id)** |
| I-7 | **`final_invoices`에 (combination, unit) 중복 방지 없음** | `app.py:295` | 없음 | **UNIQUE(combination_id, unit_id)** |
| I-8 | **`units`에 (floor, unit_name) 중복 방지 없음** | `app.py:140` | 없음 | **UNIQUE(floor_id, unit_name)** — Import upsert 키로도 필요 |
| I-9 | **`residents_count` 음수 허용** | `app.py:150` | UI `min="0"`만 | **CHECK >= 0** |
| I-10 | **금액 음수 허용** (charged_amount 등) | 다수 | 앱 clamp만 | **CHECK >= 0** (환급 가능한 컬럼은 제외) |
| I-11 | **설정 Import가 Floor 전체 삭제** | `app.py:490-492` | 없음 | **merge/upsert + 파괴적 삭제 금지** |
| I-12 | **`common_bills` 중복 생성 무제한** | `app.py:977` | 없음 | 도메인상 같은 월에 여러 항목이 정상 → **제약 없음 유지**, 단 `description` NOT NULL |
| I-13 | **인덱스 전무** | 모델 전체 | FK 자동 인덱스만 | 쿼리 패턴 기반 인덱스 |
| I-14 | **`monthly_details.month` 타입 혼재** | `invoice_view.html:157` | 템플릿 분기 | **DATE 컬럼으로 정규화** |
| I-15 | **`InvoiceCombination` 삭제 시 `payments` 처리 불명** | `app.py:1328` | 없음 | **RESTRICT** (입금 기록은 회계 데이터) |

---

## 9. 트랜잭션 경계 감사

**[확인된 사실]** 다중 row를 함께 쓰는 작업 3개:

| 작업 | 쓰기 대상 | 현재 | 위험 |
|---|---|---|---|
| `calculate_electric` (`app.py:716`) | `electric_bills` 1 + `electric_readings` N + `electric_bill_details` N | 단일 `commit()` (856행), `except`에서 `rollback()` | 실질 원자적. 단 `flush()` 후 예외 시 부분 상태가 세션에 남음 |
| `calculate_water` / `calculate_common` | bill 1 + details N | 동일 | 동일 |
| `create_invoice` (`app.py:1175`) | `invoice_combinations` 1 + `items` N + `final_invoices` N | 단일 `commit()` (1274행) | 실질 원자적 |
| `import_settings` (`app.py:485`) | **삭제 + 삽입** | 단일 `commit()` (528행) | ⚠️ 삭제가 flush되므로 중간 실패 시 rollback에 의존 |

**판정**: 현재도 대체로 원자적이나, **명시적 트랜잭션 블록이 없어 코드를 읽고서야 알 수 있다.** 목표: 명시적 `with db.session.begin_nested()` 또는 서비스 헬퍼로 경계를 드러낸다.

---

## 10. Query Pattern (인덱스 설계 근거)

**[확인된 사실]** 라우트에서 실제 사용되는 조회 패턴:

| 패턴 | 위치 | 빈도 |
|---|---|---|
| `ElectricBill.filter_by(billing_month, floor_id)` | `app.py:756` | 계산 시 |
| `ElectricBill.filter(floor_id, billing_month < X).order_by(billing_month desc)` | `app.py:1345-1348` | 이전 검침 |
| `ElectricBill.order_by(billing_month desc)` | `app.py:1018,1145` | 목록 |
| `WaterBill.filter_by(billing_month)` | `app.py:878` | 계산 시 |
| `*BillDetail.filter_by(bill_id, unit_id)` | `app.py:1227,1230,1233` | **정산서 생성 — 세대×항목 N+1** |
| `*BillDetail.filter_by(bill_id)` | `app.py:1097,1109,1116` | 상세 조회 |
| `ElectricReading.filter_by(electric_bill_id)` | `app.py:1098` | 상세 조회 |
| `FinalInvoice.filter_by(unit_id)` | `app.py:1445,1549,1594` | **잔액 계산 — 세대별 전량 순회** |
| `FinalInvoice.filter_by(combination_id).order_by(unit_id)` | `app.py:1290-1294` | 정산서 조회 |
| `Payment.filter_by(combination_id, unit_id)` | `app.py:1389` | 이력 |
| `sum(Payment.payment_amount).filter_by(unit_id)` | `app.py:1463,1567,1617` | 잔액 |
| `Unit.filter_by(is_vacant=False)` | 8곳 | 전 계산 |
| `Setting.filter_by(setting_key)` | `app.py:361,366` | **호출마다 SELECT** |

---

## 11. Settings (EAV) 감사

**[확인된 사실]** 키 9개가 **코드 5곳에 중복 정의**되어 있다:

| 위치 | 역할 | 키 개수 |
|---|---|---|
| `app.py:410-420` | 읽기 (settings 페이지) | 9 |
| `app.py:429-438` | 쓰기 (저장) | 9 |
| `app.py:453-462` | Export | 9 |
| `app.py:495-504` | Import | 9 |
| `app.py:1648-1653` | 부트스트랩 시딩 | **6 (3개 누락)** |

누락 키: `electric_bill_url`, `water_bill_url`, `water_customer_number`

**타입**: 전부 `String(255)`. 소비 시점에 `dec()` / `str` 변환 (`app.py:792,806,816,909`).

**판정**: 키 목록이 **고정적인 소수(9개)** 이고, 향후 추가 가능성이 있다.
→ **테이블 구조(key-value)는 유지**하되, **Python 측 단일 정의 레지스트리**(키·타입·기본값·검증)를 도입해 5중 중복을 제거한다.
테이블을 typed 컬럼으로 바꾸면 설정 추가마다 마이그레이션이 필요해져 migration 난이도가 올라가므로 채택하지 않는다.

---

## 12. 감사 요약 — 목표 스키마가 해결해야 할 항목

| 분류 | 항목 수 | 내용 |
|---|---|---|
| MySQL 제거 | 10 | M-1 ~ M-10 |
| 스키마 불일치 | 12 | D-1 ~ D-12 |
| 정합성 결함 | 15 | I-1 ~ I-15 |
| 금액 표현 | 3그룹 | 통화(15) / 배분 중간값(9) / 계량(3) |
| JSON | 6 | 4개 유지, 2개 관계형 분리 |
| Enum | 4 | 3개 CHECK, 1개 자유 |

다음: [01-target-sqlite-schema.md](01-target-sqlite-schema.md)
