# 03. 테스트 계획

> 선행: [02-migration-plan.md](02-migration-plan.md)
> 원칙: **스키마를 바꾸기 전에 테스트를 먼저 작성한다.** 테스트가 현재 동작을 박제한 뒤 구조를 바꾼다.

---

## 1. 테스트 인프라

```
tests/
├── conftest.py              ← app/db fixture, 임시 SQLite, 도메인 fixture
├── test_money.py            ← T-N: 타입 변환
├── test_schema.py           ← T-S: 스키마/PRAGMA
├── test_constraints.py      ← T-C: UNIQUE/CHECK/FK
├── test_delete_policy.py    ← T-D: 삭제 정책
├── test_persistence.py      ← T-P: 계산 결과 영속화 회귀
├── test_carryover.py        ← T-K: 이월 불변식
├── test_snapshot.py         ← T-Y: 스냅샷 불변성
├── test_settings.py         ← T-G: 설정 레지스트리 + Import
├── test_legacy_migration.py ← T-M: 레거시 변환 로직
└── golden/
    ├── dataset.py           ← 대표 데이터셋 정의
    └── expected.json        ← 골든 마스터 (자동 생성)
```

**DB fixture 전략**: 파일 기반 임시 SQLite (`tmp_path`)를 사용한다.
`:memory:`를 쓰지 않는 이유 — WAL 모드와 FK PRAGMA 등 **실제 런타임 설정을 그대로 검증**해야 하기 때문이다.

**스키마 생성 경로**: 테스트도 `flask db upgrade`(Alembic)로 스키마를 만든다.
`db.create_all()`로 만들면 **마이그레이션이 실제로 올바른지 검증되지 않는다.**

---

## 2. T-N. Money / ExactDecimal 타입 (`test_money.py`)

| # | 케이스 | 기대 |
|---|---|---|
| T-N1 | `round_up_to_10(0)` | `0` |
| T-N2 | `round_up_to_10(1)` | `10` |
| T-N3 | `round_up_to_10(10)` | `10` |
| T-N4 | `round_up_to_10(11)` | `20` |
| T-N5 | `round_up_to_10(Decimal('4999.01'))` | `5000` |
| T-N6 | `round_up_to_10(Decimal('12345.67'))` | `12350` |
| T-N7 | **리팩토링 전후 `round_up_to_10` 구현이 동일** | 소스 비교 또는 대량 랜덤값 대조 |
| T-N8 | `Money` bind: `Decimal('12500.00')` → `12500` | 저장 후 read = `12500` (int) |
| T-N9 | `Money` bind: `12500` → `12500` | |
| T-N10 | `Money` bind: `None` → `None` | |
| T-N11 | `Money` round-trip 타입이 `int` | `isinstance(v, int)` |
| T-N12 | `ExactDecimal` round-trip: `Decimal('12345.67')` | `Decimal('12345.67')` — **float 경유 없음** |
| T-N13 | `ExactDecimal` 반올림: `Decimal('0.005')` → `Decimal('0.01')` (ROUND_HALF_UP) | MySQL DECIMAL과 동일 |
| T-N14 | `ExactDecimal` 음수: `Decimal('-12.34')` | 보존 |
| T-N15 | `ExactDecimal` round-trip 타입이 `Decimal` | `isinstance(v, Decimal)` |
| T-N16 | **`Numeric` 컬럼이 모델에 하나도 없다** | 모델 메타데이터 스캔 (SQLite float 경고 방지 보증) |

---

## 3. T-S. Schema / PRAGMA (`test_schema.py`)

| # | 케이스 | 기대 |
|---|---|---|
| T-S1 | Fresh DB에 `flask db upgrade` 실행 | 성공 |
| T-S2 | 기대 테이블 15개 존재 | `alembic_version` 포함 |
| T-S3 | **`PRAGMA foreign_keys` = 1** | ★ 커넥션마다 ON |
| T-S4 | 새 커넥션(풀에서 재획득)에서도 FK ON | 훅이 커넥션 단위로 동작하는지 |
| T-S5 | `PRAGMA journal_mode` = `wal` | |
| T-S6 | `PRAGMA busy_timeout` > 0 | |
| T-S7 | `PRAGMA integrity_check` = `ok` | |
| T-S8 | `PRAGMA foreign_key_check` 결과 비어 있음 | |
| T-S9 | 삭제된 컬럼 부재: `electric_bills.tv_units_count`, `electric_bills.monthly_details`, `final_invoices.additional_charges`, `final_invoices.memo` | |
| T-S10 | 신규 테이블 존재: `electric_bill_months`, `final_invoice_charges` | |
| T-S11 | `flask db upgrade` → `flask db downgrade base` → `upgrade` 재실행 | 멱등성 |
| T-S12 | 모델 메타데이터와 마이그레이션 결과 스키마 일치 | `alembic check` 상당 (autogenerate diff 없음) |
| T-S13 | DB 파일 경로가 `config`를 따름 | 환경변수 override 동작 |

---

## 4. T-C. Constraints (`test_constraints.py`)

### UNIQUE

| # | 케이스 | 기대 |
|---|---|---|
| T-C1 | 동일 `floor_number` 2회 | `IntegrityError` |
| T-C2 | 동일 층에 동일 `unit_name` 2회 | `IntegrityError` ★신규 |
| T-C3 | 동일 `(floor_id, billing_month)` ElectricBill 2회 | `IntegrityError` ★신규 (I-4) |
| T-C4 | 동일 `billing_month` WaterBill 2회 | `IntegrityError` |
| T-C5 | 동일 `(electric_bill_id, unit_id)` detail 2회 | `IntegrityError` ★신규 |
| T-C6 | 동일 `(combination_id, unit_id)` FinalInvoice 2회 | `IntegrityError` ★신규 |
| T-C7 | 동일 `(electric_bill_id, billing_month)` month 행 2회 | `IntegrityError` ★신규 |
| T-C8 | 동일 `setting_key` 2회 | `IntegrityError` |
| T-C9 | **다른 층에는 같은 `unit_name` 허용** | 성공 (과도 제약 아님을 확인) |
| T-C10 | **같은 달 CommonBill 여러 건 허용** | 성공 (I-12) |

### CHECK

| # | 케이스 | 기대 |
|---|---|---|
| T-C11 | `billing_month = 2025-03-17` (day≠1) | `IntegrityError` — 단, 모델 validator가 먼저 정규화하므로 **raw SQL로 검증** |
| T-C12 | 모델 경유 저장 시 day가 자동으로 1로 정규화 | `date(2025,3,1)` |
| T-C13 | `residents_count = -1` | `IntegrityError` |
| T-C14 | `residents_count = 0` | **성공** (0명 세대는 허용) |
| T-C15 | `tv_distribution_mode = 'WRONG'` | `IntegrityError` |
| T-C16 | `distribution_method = 'WRONG'` | `IntegrityError` |
| T-C17 | `item_type = 'WRONG'` | `IntegrityError` |
| T-C18 | `charged_amount = -1` | `IntegrityError` |
| T-C19 | `payment_amount = -1` | `IntegrityError` |
| T-C20 | **`usage_amount = -5.5` (음수 사용량)** | **성공** ★ 도메인상 허용 — 과도 제약이 아님을 보증 |
| T-C21 | **`base_amount` 음수** | **성공** ★ 음수 사용량의 귀결 |
| T-C22 | **`final_invoice_charges.amount = -50000` (환급)** | **성공** ★ |
| T-C23 | **`final_invoices.total_amount` 음수** | **성공** ★ 환급 초과 시 |
| T-C24 | `previous_reading = -1` | `IntegrityError` (CAST 비교가 실제로 동작하는지 검증) |

### 다형 참조 CHECK (I-2) — ★ 핵심

| # | 케이스 | 기대 |
|---|---|---|
| T-C25 | `item_type='ELECTRIC'` + `electric_bill_id` 설정 | 성공 |
| T-C26 | `item_type='ELECTRIC'` + 3개 FK 모두 NULL | `IntegrityError` |
| T-C27 | `item_type='ELECTRIC'` + `water_bill_id` 설정 (type 불일치) | `IntegrityError` |
| T-C28 | `item_type='ELECTRIC'` + electric·water 둘 다 설정 | `IntegrityError` |
| T-C29 | `item_type='WATER'` + `water_bill_id` 설정 | 성공 |
| T-C30 | `item_type='COMMON'` + `common_bill_id` 설정 | 성공 |

### FK

| # | 케이스 | 기대 |
|---|---|---|
| T-C31 | 존재하지 않는 `floor_id`로 Unit 생성 | `IntegrityError` ★FK가 실제 동작하는지 |
| T-C32 | 존재하지 않는 `unit_id`로 Payment 생성 | `IntegrityError` |
| T-C33 | 존재하지 않는 `electric_bill_id`로 item 생성 | `IntegrityError` |

---

## 5. T-D. 삭제 정책 (`test_delete_policy.py`)

| # | 케이스 | 기대 | 근거 |
|---|---|---|---|
| T-D1 | 세대 없는 Floor 삭제 | 성공 | |
| T-D2 | 세대만 있는 Floor 삭제 | 성공 + **units CASCADE 삭제** | 현재 동작 보존 |
| T-D3 | **정산 내역이 있는 Unit 삭제** | `IntegrityError` (RESTRICT) | ★P-2 |
| T-D4 | **납부 내역이 있는 Unit 삭제** | `IntegrityError` | ★P-2 |
| T-D5 | 전기 detail이 있는 Unit 삭제 | `IntegrityError` | ★P-2 |
| T-D6 | **회계 기록 있는 세대를 가진 Floor 삭제** | `IntegrityError` + **아무것도 삭제되지 않음(원자성)** | ★가장 중요 |
| T-D7 | ElectricBill 삭제 → months/readings/details **CASCADE** | 성공, 자식 0건 | 재계산 시 통째 교체 |
| T-D8 | **정산서에 포함된 ElectricBill 삭제** | `IntegrityError` (RESTRICT) | 발행된 청구서 보호 |
| T-D9 | InvoiceCombination 삭제 → items/final_invoices/charges **CASCADE** | 성공 | 현재 동작 보존 |
| T-D10 | **납부 내역이 있는 InvoiceCombination 삭제** | `IntegrityError` (RESTRICT) | ★I-15 신규 |
| T-D11 | 납부 내역 삭제 후 InvoiceCombination 삭제 | 성공 | |
| T-D12 | WaterBill 삭제 → details CASCADE | 성공 | |
| T-D13 | 라우트 레벨: T-D3~T-D6, T-D8, T-D10이 **원시 SQL 에러가 아닌 한국어 메시지**를 반환 | `success=false` + 이해 가능한 message | UX |

---

## 6. T-P. 계산 결과 영속화 회귀 (`test_persistence.py`) ★최우선

**목적**: DB 엔진 교체로 **금액이 1원도 달라지지 않음**을 증명한다.

각 테스트는 `POST /calculate/*` → DB 재조회 → 기대값 대조 형태다.

### 전기

| # | 시나리오 | 검증 |
|---|---|---|
| T-P1 | 3세대 정상 배분 (단일 고지월) | `usage/base/final/charged` 전부 명시적 기대값 |
| T-P2 | **grossing-up**: 복지+바우처 동시 적용 | `original = total + welfare_total + voucher_total` 기준 배분 |
| T-P3 | 할인 **입력값** 우선 경로 (`welfare_input > 0`) | `per_unit = input / len(welfare_units)` |
| T-P4 | 할인 **설정값** fallback 경로 | `per_unit = setting × month_count` |
| T-P5 | 할인 대상 세대 0명 | `per_unit = 0`, `total = 0` |
| T-P6 | **`total_usage == 0` → 균등 분할 fallback** | `base = original / len(units)` ★조용한 분기 보존 |
| T-P7 | **`final_amount` 음수 clamp** | `final = 0`, `charged = 0` |
| T-P8 | TV `EQUAL` 모드 | `tv_per_unit = tv_fee_total / len(units)`, 전 재실 세대 |
| T-P9 | TV `INDIVIDUAL` 모드 + `has_tv` 혼재 | `has_tv=False` 세대는 `tv_fee = 0` |
| T-P10 | **N개월 묶음 (3개월)** | `electric_bill_months` 3행, `billing_months_count=3`, TV/할인에 `month_count` 반영 |
| T-P11 | 공실 세대 제외 | detail이 생성되지 않음 |
| T-P12 | 세대 0개인 층 | 예외 없이 처리 |
| T-P13 | **음수 사용량이 DB까지 저장됨** | BE 검증이 없다는 현재 동작 보존 |
| T-P14 | **덮어쓰기(overwrite=true)** | 기존 bill 교체, months/readings/details 정리 ★C-8 회귀 방지 |
| T-P15 | 덮어쓰기 없이 중복 요청 | `success=false`, 기존 데이터 무변경 |

### 수도

| # | 시나리오 | 검증 |
|---|---|---|
| T-P16 | 인원 비례 배분 | 명시적 기대값 |
| T-P17 | **제외 세대** | `is_excluded=True`, 전 금액 0, **detail 행은 존재** |
| T-P18 | 전원 제외 | `included=[]`, 예외 없음 |
| T-P19 | 총 인원 0 → 균등 분할 fallback | |
| T-P20 | 복지 할인 (설정값 경로에 **month_count 곱하지 않음**) | ★전기와의 비대칭 보존 |
| T-P21 | `excluded_units` JSON 파싱 실패 | **현재 동작(전 세대 포함) 보존** — 침묵 실패는 별도 단계에서 다룸 |

### 공동

| # | 시나리오 | 검증 |
|---|---|---|
| T-P22 | `BY_RESIDENTS` 배분 | 명시적 기대값 |
| T-P23 | `BY_UNITS` 균등 배분 | |
| T-P24 | 세대 0개 | 예외 없음 |

> ⚠️ 공동 공과금은 FE/BE 계약이 깨져 있어(C-1) 실제로는 총액 0이 저장된다.
> **이번 DB 단계에서는 C-1을 고치지 않는다.** 테스트는 `/calculate/common` 을 **올바른 필드명으로 직접 호출**하여 BE 로직만 검증한다.
> (FE 계약 복구는 별도 단계 — 본 작업의 Scope 밖)

### 정산서

| # | 시나리오 | 검증 |
|---|---|---|
| T-P25 | 전기+수도+공동 조합 | `final_invoices` 금액 = `Σ charged_amount` |
| T-P26 | 기타 항목 포함 | `final_invoice_charges` 행 생성, `total_amount`에 반영 |
| T-P27 | 환급(음수) 항목 | 부호 보존 |
| T-P28 | `common_details` JSON 유지 | 인쇄 템플릿 입력 형태 보존 |
| T-P29 | 재실 세대 전부에 대해 FinalInvoice 생성 (0원 포함) | 현재 동작 보존 |

### 납부

| # | 시나리오 | 검증 |
|---|---|---|
| T-P30 | 입금 등록/수정/삭제 | 금액 보존 |
| T-P31 | 4개 잔액 엔드포인트가 **동일 세대에 동일 값** | ★H-3 회귀 방지 |

---

## 7. T-K. 이월 불변식 (`test_carryover.py`) ★C-6 핵심

| # | 케이스 | 기대 |
|---|---|---|
| T-K1 | `is_carryover=True` 항목이 `final_invoices.total_amount`에 **포함** | 청구서 금액에 반영 |
| T-K2 | `is_carryover=True` 항목이 **balance 계산에서 제외** | 이중계상 방지 |
| T-K3 | `is_carryover=False` 항목은 balance에 **포함** | |
| T-K4 | **`description`에 '미납'이 들어가도 `is_carryover=False`면 balance에 포함** | ★문자열 판정 제거 증명 |
| T-K5 | **`description`이 평범해도 `is_carryover=True`면 balance에서 제외** | ★ |
| T-K6 | FE가 보낸 `is_carryover`가 DB까지 보존 | `/invoice/create` → DB 확인 |
| T-K7 | 이월 항목 포함 시나리오의 balance가 **레거시 문자열 알고리즘 결과와 동일** | 골든 마스터 대조 |
| T-K8 | 애플리케이션 코드에 이월 키워드 문자열이 남아있지 않음 | 소스 grep (`'미납'`, `'초과납부'`, `'환급'`, `'이월'` 판정 로직) — 마이그레이션 스크립트는 예외 |

---

## 8. T-Y. 스냅샷 불변성 (`test_snapshot.py`)

| # | 케이스 | 기대 |
|---|---|---|
| T-Y1 | 계산 후 `unit.unit_name` 변경 → 과거 `unit_snapshot.unit_name` 불변 | ★핵심 자산 |
| T-Y2 | 계산 후 `unit.residents_count` 변경 → 과거 스냅샷 불변 | |
| T-Y3 | 계산 후 `unit.is_vacant` 변경 → 과거 스냅샷 불변 | |
| T-Y4 | 스냅샷 7키 전부 존재 | |
| T-Y5 | 전기/수도/공동 3개 detail 모두 스냅샷 보유 | |
| T-Y6 | 세대 속성 변경이 과거 `charged_amount`에 영향 없음 | |

---

## 9. T-G. 설정 (`test_settings.py`)

| # | 케이스 | 기대 |
|---|---|---|
| T-G1 | 레지스트리의 모든 키가 시딩됨 (9개) | ★현재 3개 누락 문제 해결 |
| T-G2 | `seed-settings` 멱등 (2회 실행) | 기존 값 덮어쓰지 않음 |
| T-G3 | 타입 변환: `tv_fee` → `int`/`Decimal` | |
| T-G4 | 미등록 키 접근 | 명시적 에러 |
| T-G5 | 255자 초과 메모 저장/복원 | ★TEXT 전환 검증 |

### Settings Import (C-4 / I-11) — ★핵심

| # | 케이스 | 기대 |
|---|---|---|
| T-G6 | **회계 기록이 있는 상태에서 Import** | 기존 계산/정산/납부 데이터가 **하나도 삭제되지 않음** |
| T-G7 | Import merge: 기존 층/세대 **갱신** | `(floor_number)` / `(floor_id, unit_name)` 자연키 upsert |
| T-G8 | Import merge: 신규 층/세대 **추가** | |
| T-G9 | Import에 없는 기존 세대 | **삭제되지 않음** (보존) |
| T-G10 | 잘못된 payload | validation 에러, **DB 무변경** |
| T-G11 | Import 중간 실패 | **전체 롤백** (부분 적용 없음) |
| T-G12 | Import 후 기존 세대의 `unit_id`가 보존됨 | 과거 계산의 FK 참조 유효 |

---

## 10. T-M. 레거시 마이그레이션 (`test_legacy_migration.py`)

MySQL에 접근할 수 없으므로 **레거시 형태를 재현한 SQLite DB**를 소스로 사용해 변환 로직을 검증한다.

| # | 케이스 | 기대 |
|---|---|---|
| T-M1 | `DECIMAL('12500.00')` → Money `12500` | |
| T-M2 | `DECIMAL('12345.67')` (통화 컬럼) | `requires-review` 리포트 |
| T-M3 | `monthly_details` `month='2025-03'` | `date(2025,3,1)` |
| T-M4 | `monthly_details` `month='2025-03-17'` | `date(2025,3,1)` |
| T-M5 | `monthly_details` `month=date(...)` | 정규화 |
| T-M6 | `monthly_details` `month=None` | `requires-review`, **임의 대입 안 함** |
| T-M7 | `monthly_details` 월 중복 | `requires-review` |
| T-M8 | `additional_charges` `'[이월] 전월 미납금'` | `is_carryover=True` |
| T-M9 | `additional_charges` `'수리비'` | `is_carryover=False` |
| T-M10 | `additional_charges` `'보증금 환급'` | `is_carryover=True` + **`ambiguous_carryover` 리포트** |
| T-M11 | `item_type`/FK 불일치 | `requires-review` |
| T-M12 | `units` 동일 층 이름 중복 | `requires-review`, 자동 rename 안 함 |
| T-M13 | detail `(bill,unit)` 중복 | `requires-review`, 자동 병합 안 함 |
| T-M14 | Boolean NULL → 도메인 기본값 | `has_tv` NULL → 1, 나머지 → 0 |
| T-M15 | **PK 보존** | 원본 `id`와 대상 `id` 동일 |
| T-M16 | **row count 검증** 13테이블 + 파생 2테이블 | |
| T-M17 | **금액 aggregate 검증** 8종 | 오차 0 |
| T-M18 | **★ balance 동등성**: 레거시 문자열 알고리즘 vs 신규 `is_carryover` 알고리즘 | **전 세대 완전 일치** |
| T-M19 | `--report-only` 모드가 대상 DB를 만들지 않음 | |
| T-M20 | blocking 위반 존재 시 기본 모드는 중단 | 대상 DB 미생성 |
| T-M21 | **소스 DB가 변경되지 않음** | 실행 전후 소스 해시 동일 |
| T-M22 | 대상 파일이 이미 존재 & 비어있지 않으면 거부 | `--force` 필요 |

---

## 11. Golden Master

### 11.1 방식

MySQL에 접근할 수 없으므로 **대표 데이터셋을 코드로 정의**하고, 이를 계산 API에 통과시킨 결과를 JSON으로 고정한다.

```
tests/golden/dataset.py     대표 데이터셋 (3층 / 8세대 / 3개월 / 전기·수도·공동 · 정산 2건 · 납부 5건)
tests/golden/expected.json  기대 결과 (최초 1회 생성 후 커밋)
scripts/export_golden.py    현재 DB에서 골든 마스터 추출 (MySQL/SQLite 공용)
```

### 11.2 대표 데이터셋 요구사항

계산 분기를 **전부 통과**하도록 설계한다:

- 3개 층 (지하 1층 포함 → 음수 `floor_number`)
- 재실 6 / 공실 2
- 복지 대상 2, 바우처 대상 1, TV 미보유 1, 수도복지 1
- 전기: 1개월 정산 1건 + **3개월 묶음** 정산 1건, TV `INDIVIDUAL`/`EQUAL` 각 1건
- 수도: 제외 세대 1개 포함
- 공동: `BY_RESIDENTS` 1건 + `BY_UNITS` 1건
- 정산서 2건: 기타 항목(부과/환급), **이월 항목 포함**
- 납부: 완납 1, 미납 1, 초과납부 1

### 11.3 비교 대상

```json
{
  "electric_bill_details": [{"bill_id","unit_id","usage_amount","base_amount",
                             "welfare_discount","voucher_discount","tv_fee",
                             "final_amount","charged_amount"}],
  "water_bill_details":    [...],
  "common_bill_details":   [...],
  "final_invoices":        [{"combination_id","unit_id","electric_amount","water_amount",
                             "common_amount","total_amount"}],
  "final_invoice_charges": [{"final_invoice_id","description","amount","is_carryover"}],
  "balances":              {"unit_id": balance},
  "aggregates":            {"sum_electric_charged", "sum_water_charged", ...}
}
```

**허용 오차 0.** 1원이라도 다르면 실패. "SQLite 특성"으로 넘어가지 않는다.

### 11.4 사용 시점

| 시점 | 용도 |
|---|---|
| 스키마 변경 전 | 현재 코드로 `expected.json` 생성 후 **커밋** |
| 스키마 변경 후 | 동일 데이터셋 → 동일 결과 검증 |
| 향후 모든 리팩토링 | 회귀 방어선 |

---

## 12. 테스트 실행

```
pytest -q                      # 전체
pytest tests/test_persistence.py -q
pytest -m golden               # 골든 마스터만
```

**의존성 추가** (`requirements-dev.txt`):
```
pytest
pytest-cov
```

---

## 13. 완료 판정 기준

| 기준 | 검증 방법 |
|---|---|
| FK가 실제 활성화 | T-S3, T-S4, T-C31~33 |
| 회계 데이터가 연쇄 삭제되지 않음 | T-D3~T-D6, T-D10 |
| 다형 참조 정합성 보장 | T-C25~T-C30 |
| 이월이 문자열에 의존하지 않음 | T-K4, T-K5, T-K8 |
| 설정 Import가 파괴적이지 않음 | T-G6, T-G9, T-G11 |
| **계산 결과 불변** | T-P 전체 + Golden Master |
| 스냅샷 불변성 | T-Y 전체 |
| 마이그레이션 안전성 | T-M18, T-M21, T-M22 |
| 금액 정합성 | T-N12, T-N16, T-M17 |
