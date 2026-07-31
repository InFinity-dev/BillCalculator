# 00. 프로젝트 개요

> 분석 기준: `master` @ `6efd646` (계산 로직 FE/BE rowId 불일치 수정)
> 분석 일자: 2026-07-31
> 분석 범위: Repository 전체 (`app.py` 1,662줄 + `templates/` 12개 파일 6,467줄 + `SQLSchema.txt` + `TODO.md`)

---

## 1. 이 프로그램은 무엇인가

**[확인된 사실]**

이 애플리케이션은 **다세대 주택(원룸/다가구 임대건물)의 소유주 또는 관리인이, 건물에 층 단위·주택 단위로 부과된 공과금 고지서(전기/수도/공동비용)를 각 세대별 사용량·거주인원 기준으로 재분배 계산하고, 세대별 청구서(정산서)를 생성·인쇄·배부하며, 세대별 입금 내역과 누적 미납/초과 잔액을 추적하기 위한 로컬 웹 애플리케이션**이다.

주체 → 대상 → 작업 → 결과로 풀어쓰면:

| 구분 | 내용 |
|---|---|
| **주체** | 건물주 / 관리인 (단일 사용자, 인증 없음) |
| **대상** | 건물의 층(`floors`) · 세대(`units`), 외부에서 받은 종이/웹 고지서 금액 |
| **작업** | ① 층·세대 마스터 등록 → ② 고지 금액 + 세대별 전기 계량기 검침값 입력 → ③ 비례 배분 계산 → ④ 항목 조합해 세대별 정산서 생성 → ⑤ 인쇄 배부 → ⑥ 입금 등록 → ⑦ 미납 잔액 다음 달 이월 |
| **결과** | `final_invoices` 레코드 + A4 인쇄용 세대별 청구서 HTML + 세대별 누적 잔액 |

핵심 도메인 문제는 **"한전은 층 단위로, 상수도는 건물 단위로 고지하는데, 실제 부담은 세대 단위여야 한다"** 는 배분(allocation) 문제이다.

### 배분 규칙 요약 (핵심 비즈니스 로직)

**[확인된 사실]** — `app.py:716-1005`

| 요금 | 배분 기준 | 조정 항목 |
|---|---|---|
| **전기** (`/calculate/electric`) | 층 내 세대의 **계량기 사용량 비율** (`(당월-전월) / 층 총사용량`) | 복지할인 / 바우처할인 차감, TV수신료 가산 |
| **수도** (`/calculate/water`) | 건물 전체 세대의 **거주 인원수 비율** | 수도복지할인 차감, 특정 세대 정산 제외 |
| **공동** (`/calculate/common`) | **인원수 비례** 또는 **세대 균등** 선택 | 없음 |

세 요금 모두 마지막에 **10원 단위 올림**(`round_up_to_10`, `app.py:356`) 처리 후 `charged_amount`로 확정한다.

전기/수도의 할인 처리에는 **grossing-up 로직**이 있다 (`app.py:822`, `app.py:915`):

```
original_amount = 입력한 고지 총액 + 복지할인 총액 + 바우처할인 총액
세대 배분액 = (세대 사용량 / 총 사용량) × original_amount
세대 최종액 = 세대 배분액 - 세대별 복지할인 - 세대별 바우처할인 + TV수신료
```

즉 **"고지서에 찍힌 금액은 이미 할인이 반영된 순액이므로, 할인 전 금액으로 되돌린 뒤 비례 배분하고, 할인 대상 세대에게만 할인을 돌려준다"** 는 규칙이다. 이것이 이 시스템의 가장 중요한 도메인 규칙이며, 리팩토링 시 반드시 보존해야 한다.

---

## 2. 주요 사용자와 사용 시나리오

**[강한 추론]** — 인증/권한/멀티테넌시 코드가 전무하고, `app.run(debug=True, host='0.0.0.0', port=5000)`으로 단일 프로세스 로컬 실행되는 점, DB 접속정보가 소스에 하드코딩된 점, `TODO.md`에 "Electron + sqlite 방식도 고려중 (독립형 앱으로)"라고 적힌 점으로 보아 **1인 사용자가 자기 PC에서 실행하는 개인 도구**이다.

### 월간 운영 시나리오 (재구성)

```
[월초]
 1. 한전/상수도 고지서 수령 (또는 계산기 페이지의 "요금 조회" 버튼으로 외부 사이트 확인)
 2. 각 층 세대 계량기를 직접 검침
 3. 계산기 > 전기요금 탭
      - 층 선택 → 세대 정보 카드 + 검침 입력표 자동 렌더
      - "이전 정산의 현월 검침 불러오기"로 전월 검침 자동 채움
      - 월별 고지 내역 행 추가 (N개월 밀린 고지도 한 번에 처리 가능)
      - 저장 → 세대별 charged_amount 확정
 4. 계산기 > 수도요금 탭 (정산 제외 세대 체크 해제 후 저장)
 5. 계산기 > 공동 공과금 탭
 6. 정산 > 새 정산 생성 (3-step 위저드)
      Step1: 이번 달에 반영할 전기/수도/공동 항목 체크
      Step2: 세대별 미납금 이월 버튼 클릭 + 기타 항목/메모 입력
      Step3: 최종 확인 → 생성
 7. 인쇄용 보기 → 세대별 1페이지 A4 출력 → 문 앞에 배부
[월중]
 8. 납부 내역 > 세대 선택 > 입금 등록
 9. 잔액 정합성 검증으로 전체 미납 현황 확인
```

---

## 3. 핵심 기능 (요약)

상세는 [02-feature-inventory.md](02-feature-inventory.md) 참조.

1. 층/세대 마스터 관리 (CRUD, 속성: 복지·바우처·TV보유·수도복지·거주인원·공실)
2. 요금 정책 설정 (TV 단가, 할인 단가, 정산서 고정 메모/푸터, 외부 조회 URL)
3. 설정 JSON Export / Import (백업·복원)
4. 전기요금 계산 — 사용량 비례 배분 + N개월 묶음 정산 + TV수신료 2가지 배분 모드
5. 수도요금 계산 — 인원수 비례 배분 + 세대별 정산 제외
6. 공동 공과금 계산 — 인원 비례 / 세대 균등
7. 이전 검침값 자동 불러오기
8. 정산 내역 조회 + Chart.js 추이 그래프 + 항목별 상세 페이지
9. 정산서(Invoice) 조합 생성 — 3-step 위저드, 세대별 기타 항목/메모
10. 정산서 조회 / A4 인쇄용 뷰 (세대별 페이지 분리, 해당 층 전기요금만 표기)
11. 납부 내역 관리 — 입금 등록/수정/삭제, 세대별 이력
12. 누적 잔액 계산 + 미납금 자동 이월 (이월 항목 중복 계산 방지)
13. 잔액 정합성 검증 (`/admin/validate_balances`)

---

## 4. 기술 스택

**[확인된 사실]** — `requirements.txt`, `app.py` import 및 실제 사용처 확인

| 계층 | 기술 | 버전 | 실제 사용 확인 위치 |
|---|---|---|---|
| Web Framework | Flask | 3.0.3 | `app.py:1,77` 전 라우트 |
| ORM | Flask-SQLAlchemy | 3.1.1 | `app.py:2,110` 모델 12개 |
| DB Driver | mysql-connector-python | 9.0.0 | `app.py:8,116` (`init_database`) + SQLAlchemy URI `mysql+mysqlconnector` |
| 날짜 유틸 | python-dateutil | 2.9.0.post0 | **미사용** (import조차 없음) |
| Template | Jinja2 (Flask 내장) | 3.1.6 | `templates/` 12개 |
| Frontend | **Vanilla JS + 인라인 `<style>`** | - | 빌드 도구·번들러·프레임워크 없음 |
| Chart | Chart.js 3.9.1 (CDN) | - | `view.html:157` |
| DB | MySQL 5.7+/8.0 (InnoDB, utf8mb4) | - | `SQLSchema.txt`, `.idea/dataSources.xml` |
| Python | 3.9 (venv) / 3.11 (`__pycache__` 흔적) | - | 두 버전 혼재 흔적 |

**없는 것 (모두 [확인된 사실])**: README, `.gitignore`, 테스트, CI/CD, Dockerfile, `.env`/설정 파일, 로깅 설정, 마이그레이션 도구(Alembic), 정적 파일 디렉터리(`static/`), 린터/포매터 설정, 타입 힌트.

---

## 5. 전체 구조

```
BillCalculator/
├── app.py                    ← 단일 파일 백엔드 (1,662줄). 모델·라우트·계산로직·유틸 전부
├── requirements.txt          ← 4개 패키지 (1개는 미사용)
├── SQLSchema.txt             ← DDL 스크립트. **현재 모델과 불일치 (Stale)**
├── TODO.md                   ← 9개 항목 중 6개 완료. Electron+SQLite 전환 검토 메모
├── templates/                ← Jinja2 템플릿 12개, CSS/JS 전부 인라인
│   ├── base.html             ← 디자인 시스템(CSS 변수) + GNB + 모달/CSRF/fetch 헬퍼
│   ├── index.html            ← 홈 대시보드
│   ├── settings.html         ← 설정 (5탭)
│   ├── calculator.html       ← 계산기 (3탭) — JS로 폼 동적 생성
│   ├── view.html             ← 조회 (3탭) + Chart.js
│   ├── view_electric_detail.html / view_water_detail.html / view_common_detail.html
│   ├── invoice.html          ← 정산 조합 생성 (3-step 위저드)
│   ├── invoice_view.html     ← 정산서 조회
│   ├── invoice_print.html    ← A4 인쇄 (base.html 미상속, 독립 HTML)
│   └── payments.html         ← 납부 내역 관리
├── .idea/                    ← PyCharm 설정 (Git에 커밋됨)
├── __pycache__/              ← **Git에 커밋됨** (.gitignore 부재)
└── venv/                     ← 로컬 가상환경 (untracked)
```

**아키텍처 한 문장 요약**: 계층 분리가 전혀 없는 **단일 파일 Flask 모놀리스**로, HTTP 라우트 함수 하나가 파라미터 파싱·검증·비즈니스 계산·ORM 영속화·JSON 응답을 모두 수행하며, 프론트엔드는 서버 렌더 Jinja와 클라이언트 `innerHTML` 문자열 조립이 혼재된 Vanilla JS다.

---

## 6. 현재 시스템의 특징

### 잘 되어 있는 점 [확인된 사실]

- **스냅샷 패턴**: 모든 상세 테이블(`*_bill_details`)이 계산 시점의 세대 속성을 `unit_snapshot` JSON으로 저장한다 (`create_unit_snapshot`, `app.py:374`). 세대 정보가 나중에 바뀌어도 과거 정산 내역이 왜곡되지 않는다. **도메인 이해도가 높은 설계**이며 반드시 유지해야 한다.
- **Decimal 일관성**: `dec()` 헬퍼(`app.py:26`)와 커스텀 JSON Provider(`app.py:83`)로 금액을 float 오차 없이 다룬다.
- **디자인 시스템**: `base.html`의 CSS 변수 체계(`--space-*`, `--radius-*`, `--gray-*`)가 잘 잡혀 있다. 다만 실제 사용률은 낮다(인라인 style 남용).
- **CSRF 방어**: `csrf_protect` 데코레이터(`app.py:340`)가 모든 POST 라우트에 적용되어 있다.
- **묶음 정산**: 여러 달 밀린 고지서를 한 번에 처리하는 `monthly_details` JSON 구조 — 실제 운영 필요에서 나온 기능.

### 구조적 취약점 [확인된 사실] — 상세는 [06-technical-debt.md](06-technical-debt.md)

- **1,662줄 단일 파일** — 계층 없음, 계산 로직 재사용 불가, 단위 테스트 불가
- **테스트 0개** — 금액 계산 시스템인데 회귀 방어 장치가 전혀 없음
- **FE/BE 계약 불일치 3건 (동작 파손)** — 공동 공과금 총액이 항상 0원으로 저장됨, 전기요금 덮어쓰기 불가 등
- **DB 자격증명 하드코딩 + `debug=True` + `host=0.0.0.0`** — 원격 RCE 노출
- **`SQLSchema.txt`가 실제 모델과 불일치** — DB 재구축 불가
- **미납/이월 판정이 문자열 키워드 매칭** — `'미납','초과납부','환급','이월'` (`app.py:1405,1457,1561,1611`, 4곳 중복)

---

## 7. README와 코드의 차이

**[확인된 사실]** README가 존재하지 않는다. 대신 `index.html`이 기능 소개 역할을 한다.

`index.html`의 기능 설명 중 **현재 코드와 불일치하는 항목**:

| `index.html` 설명 | 실제 코드 |
|---|---|
| "🏘️ 공동 공과금 — 유연한 분배 방식: 항목별 특성에 맞게 선택" (`index.html:60`) | **동작하지 않음**. FE가 `distribution_mode`(RESTRICT값 `RESIDENTS`/`UNIT`)를 보내는데 BE는 `distribution_method`(`BY_RESIDENTS`/`BY_UNITS`)를 읽어 항상 기본값 `BY_RESIDENTS`가 적용됨 |
| "N개월 묶음 정산: 2~12개월치" (`index.html:35`) | 상한 12개월 제한 코드 없음. 무제한 행 추가 가능 |
| "미납금/초과납부 자동 이월: 전월 잔액 자동 반영" (`index.html:72`) | "자동"이 아니라 Step2에서 **사용자가 버튼을 눌러야** 반영됨 (`invoice.html:1074 applyBalance`) |

---

## 다음 문서

- [01-architecture.md](01-architecture.md) — 계층 구조와 의존 관계
- [02-feature-inventory.md](02-feature-inventory.md) — 기능 인벤토리 (전수)
- [03-runtime-flow.md](03-runtime-flow.md) — 실행/런타임 흐름
- [04-data-flow.md](04-data-flow.md) — 데이터 구조와 흐름
- [05-external-dependencies.md](05-external-dependencies.md) — 외부 의존성
- [06-technical-debt.md](06-technical-debt.md) — 기술 부채 (Critical~Low)
- [07-refactoring-foundation.md](07-refactoring-foundation.md) — 리팩토링 기반 분석
