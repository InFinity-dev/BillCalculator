# 05. 외부 시스템 / 의존성

---

## 1. 요약

**[확인된 사실]** 이 애플리케이션의 외부 연동은 매우 좁다. 하드웨어 제어, 시리얼/USB/HID, SSH/ADB, subprocess, 메시지 큐, 클라우드 SDK는 **하나도 없다**.

```mermaid
graph LR
    App["BillCalculator<br/>(Flask, localhost:5000)"]
    App -->|"필수 · TCP 3306"| MySQL[("MySQL<br/>bill_calculator")]
    Browser["사용자 브라우저"] -->|"HTTP"| App
    Browser -.->|"선택 · &lt;script src&gt;"| CDN["cdnjs.cloudflare.com<br/>Chart.js 3.9.1"]
    Browser -.->|"선택 · window.open"| KEPCO["한전 요금조회<br/>(사용자 설정 URL)"]
    Browser -.->|"선택 · window.open"| WATER["상수도 요금조회<br/>(사용자 설정 URL)"]
    Browser -.->|"로컬 파일"| FS["settings_YYYY-MM-DD.json<br/>다운로드/업로드"]

    style MySQL fill:#fee2e2
    style CDN fill:#fef3c7
```

---

## 2. 의존성 상세

### 2.1 MySQL — **치명적 의존 (Critical)**

| 항목 | 내용 |
|---|---|
| **목적** | 유일한 영속 저장소. 마스터·계산결과·정산·납부 전부 |
| **연결 방식** | ① SQLAlchemy `mysql+mysqlconnector://power_user:mslee0702@localhost/bill_calculator` (`app.py:107`)<br/>② `init_database()`에서 mysql-connector 직접 접속 (`app.py:116`) |
| **호출 위치** | 전 라우트 34개 |
| **풀 설정** | `pool_pre_ping=True`, `pool_recycle=3600` (`app.py:109`) — 끊긴 커넥션 자동 감지만 |
| **timeout** | **미설정** (드라이버 기본값) |
| **retry** | **없음** |
| **실패 시 처리** | 부트스트랩: `print` 후 계속 진행 (`app.py:122-123`)<br/>런타임: 라우트 `except Exception` → `str(e)`를 사용자 alert에 노출 |
| **중요도** | ★★★★★ — DB 없이는 어떤 페이지도 렌더되지 않는다 (`/` 조차 `Floor.query.count()` 호출) |

**⚠️ [확인된 사실] 자격증명이 소스에 평문 하드코딩되어 있고 Git에 커밋되어 있다.**
- `app.py:107` — `power_user:mslee0702`
- `app.py:116` — 동일 값이 한 번 더
- 환경변수·`.env`·설정 파일 지원 없음
- Git 히스토리에도 남아 있어 파일만 고쳐서는 제거되지 않는다

**[확인된 사실]** 본 분석 중 `mysql -upower_user -p...` 로 접속을 시도했으나 `Access denied` 로 실패했다. 즉 **현재 로컬 환경에서는 이 자격증명이 유효하지 않다.** 실제 DB 상태(테이블 존재 여부, FK 정책, 데이터 양)는 **검증하지 못했다** — 이 문서의 DB 관련 서술 중 실물 검증이 필요한 항목은 [불명확]으로 표기했다.

**[불명확]** 실제 DB가 `SQLSchema.txt`로 만들어졌는지 `db.create_all()`로 만들어졌는지 확인 불가. 이 차이가 FK `ON DELETE` 정책(CASCADE vs RESTRICT)을 좌우하며, **설정 Import·층 삭제·세대 삭제의 동작이 정반대로 갈린다** ([02-feature-inventory.md F-06](02-feature-inventory.md) 참조). 리팩토링 착수 전 **최우선으로 실측해야 할 항목**이다.

```sql
-- 실측 방법 (읽기 전용)
SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, DELETE_RULE
FROM information_schema.REFERENTIAL_CONSTRAINTS rc
JOIN information_schema.KEY_COLUMN_USAGE k USING (CONSTRAINT_NAME)
WHERE rc.CONSTRAINT_SCHEMA = 'bill_calculator';
```

### 2.2 Chart.js (CDN) — 선택적 의존

| 항목 | 내용 |
|---|---|
| **목적** | 조회 페이지의 전기(층별 고지월 추이) / 수도(월별 추이) 라인 차트 |
| **연결 방식** | `<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/3.9.1/chart.min.js">` (`view.html:157`) |
| **호출 위치** | `view.html:301 updateElectricChart`, `367 updateWaterChart` |
| **SRI (무결성 검증)** | **없음** — `integrity` 속성 미지정. CDN 침해 시 임의 스크립트 실행 |
| **실패 시 처리** | **없음**. 로드 실패 시 `Chart is not defined` ReferenceError |
| **중요도** | ★★☆☆☆ 기능적으로는 부가 요소 |

**[확인된 사실] 실패 영향이 차트에 국한되지 않는다.** `view.html:583-588`의 초기화 순서:
```javascript
renderElectric();       // 목록 렌더 (성공)
renderWater();
renderCommon();
updateElectricChart();  // ← Chart 미정의 시 여기서 throw
updateWaterChart();     // ← 실행 안 됨
```
목록 3개는 먼저 렌더되므로 표는 보이지만, 이후 스크립트가 중단되어 **차트 영역이 빈 캔버스로 남는다.** 오프라인 환경(이 앱의 주 사용 시나리오일 가능성이 높다)에서 항상 발생한다.

**[확인된 사실] 이 앱의 유일한 외부 런타임 의존성**이다. 이것만 로컬 번들로 바꾸면 앱이 완전히 오프라인 동작 가능해진다.

### 2.3 외부 요금 조회 사이트 — 사용자 지정, 느슨한 연동

| 항목 | 내용 |
|---|---|
| **목적** | 한전/상수도 요금 조회 페이지 바로가기 |
| **연결 방식** | `window.open(url, '_blank')` — **데이터 교환 없음**. 단순 링크 |
| **설정** | `settings.electric_bill_url`, `settings.water_bill_url` (`app.py:418-419`) |
| **호출 위치** | `calculator.html:21`(전기 탭), `137`(수도 탭) — URL이 빈 값이면 버튼 미렌더 |
| **실패 시 처리** | 없음 (브라우저 위임) |
| **중요도** | ★☆☆☆☆ |

**[확인된 사실] URL 검증이 전혀 없다.** `javascript:alert(1)` 같은 스킴도 저장되고, `onclick` JS 문자열 컨텍스트에 그대로 삽입된다(`calculator.html:21`). 단일 사용자 앱이라 자기 자신을 공격하는 셈이지만, 구조적 결함이다.

**[확인된 사실] 스크래핑·API 연동은 없다.** 요금 데이터는 사용자가 눈으로 보고 손으로 입력한다. 이 프로젝트에서 **자동화 여지가 가장 큰 지점**이다.

### 2.4 로컬 파일 (Export/Import)

| 항목 | 내용 |
|---|---|
| **목적** | 층/세대/요금 설정 백업·복원 |
| **연결 방식** | 서버 파일시스템 접근 **없음**. 브라우저 `Blob` + `URL.createObjectURL` 로 다운로드(`settings.html:648-656`), `File.text()` 로 업로드(`settings.html:668`) |
| **형식** | JSON |
| **실패 시 처리** | `JSON.parse` 실패 → `alert('파일 형식이 올바르지 않습니다.')`. 서버 측은 스키마 검증 없이 `data.get(...)` 으로 진행(`app.py:494-526`) |
| **중요도** | ★★☆☆☆ (Import는 파괴적이므로 실질 위험도는 높음) |

**[확인된 사실] 서버는 파일시스템을 전혀 사용하지 않는다.** 업로드 디렉터리, 로그 파일, 임시 파일, 캐시 파일이 없다. 상태는 100% DB에 있다. 이는 **컨테이너화·이식성 측면에서 유리한 특성**이다.

### 2.5 Python 패키지

**[확인된 사실]** `requirements.txt` 4줄. 실제 설치 시 전이 의존까지 15개.

| 패키지 | 버전 | 직접 사용 | 사용처 |
|---|---|---|---|
| flask | 3.0.3 | ✅ | 전체 |
| flask_sqlalchemy | 3.1.1 | ✅ | 모델·쿼리 |
| mysql-connector-python | 9.0.0 | ✅ | DB 드라이버 + `init_database()` |
| python-dateutil | 2.9.0.post0 | ❌ | **import조차 없음 — 제거 대상** |
| (전이) sqlalchemy | 2.0.51 | ✅ | `joinedload`, `func`, `relationship` 직접 import (`app.py:11-12`) |
| (전이) jinja2, werkzeug, click, blinker, itsdangerous, markupsafe, typing-extensions, six, zipp, importlib-metadata | - | 간접 | - |

**[확인된 사실] 버전 고정 방식이 `==` 핀이지만 lock 파일은 없다.** 전이 의존성 버전은 설치 시점에 따라 달라진다.

**[확인된 사실] Python 버전 혼재**: `venv/`는 Python 3.9.13, 커밋된 `__pycache__/app.cpython-311.pyc`는 Python 3.11. 개발 환경이 통일되어 있지 않다.

---

## 3. 존재하지 않는 의존성 (확인 완료)

**[확인된 사실]** 다음은 전부 코드베이스에 **존재하지 않는다**. 리팩토링 범위 산정에 중요하다.

| 범주 | 확인 결과 |
|---|---|
| REST API 클라이언트 | 없음 (`requests`, `httpx`, `urllib` import 없음) |
| WebSocket / SSE | 없음 |
| Raw Socket / Serial / USB / HID | 없음 (`socket`, `pyserial` 없음) |
| SSH / ADB / subprocess / shell | 없음 (`subprocess`, `os.system` 없음) |
| 하드웨어 / 디바이스 드라이버 | 없음 — **계량기는 사람이 눈으로 읽어 입력한다** |
| 메시지 큐 / 브로커 | 없음 |
| 캐시 (Redis/Memcached) | 없음 |
| 이메일 / SMS / 푸시 | 없음 — 청구서는 **종이 인쇄 배부** |
| 결제 게이트웨이 | 없음 — 입금은 **수동 기록** |
| 클라우드 SDK (AWS/GCP/Azure) | 없음 |
| 인증 제공자 (OAuth/LDAP) | 없음 |
| 파일 스토리지 (S3 등) | 없음 |
| PDF 생성 라이브러리 | 없음 — 브라우저 인쇄 기능 사용 |
| Excel/CSV 라이브러리 | 없음 (`openpyxl`, `pandas` 없음) |
| 스케줄러 (cron/APScheduler/Celery) | 없음 |
| 모니터링 / APM / Sentry | 없음 |
| 로깅 프레임워크 | 없음 (`print` 2회가 전부) |

---

## 4. 배포·운영 의존성

**[확인된 사실]** 배포 인프라 관련 파일이 **하나도 없다**.

| 항목 | 상태 |
|---|---|
| Dockerfile / docker-compose.yml | 없음 |
| CI/CD (`.github/workflows`, `.gitlab-ci.yml`) | 없음 |
| WSGI 설정 (gunicorn/uwsgi conf) | 없음 |
| 리버스 프록시 설정 (nginx 등) | 없음 |
| systemd 유닛 / 프로세스 매니저 | 없음 |
| 환경변수 파일 (`.env`, `.env.example`) | 없음 |
| `.gitignore` | **없음** → `__pycache__/app.cpython-311.pyc` 와 `.idea/` 가 Git에 커밋되어 있다 |
| README / 설치 문서 | 없음 |
| 마이그레이션 도구 (Alembic) | 없음 — `SQLSchema.txt`에 수동 SQL 누적 |

**실행 방법 [강한 추론]**: `python app.py` → `http://localhost:5000`. `TODO.md:11`의 "Electron + sqlite 방식도 고려중 (독립형 앱으로)" 메모는 **배포 방식이 미해결 과제로 남아 있음**을 보여준다.

---

## 5. 의존성별 리스크 평가

| 의존성 | 가용성 리스크 | 보안 리스크 | 교체 난이도 | 비고 |
|---|---|---|---|---|
| **MySQL** | 높음 — 단일 장애점 | **높음** — 평문 자격증명 커밋 | **중** — SQLAlchemy 추상화 덕에 SQLite/PostgreSQL 전환은 가능하나, `mysql.connector` 직접 사용부(`app.py:113-123`)와 MySQL `JSON` 타입 의존, `Enum` 컬럼(`app.py:246`)이 걸림돌 | `TODO.md`의 SQLite 전환 검토와 직결 |
| **Chart.js CDN** | 중 — 오프라인 시 항상 실패 | 중 — SRI 없음 | **하** — 파일 1개 로컬 번들 | 즉시 개선 가능 |
| **외부 조회 URL** | 낮음 | 낮음 (URL 검증 부재) | 하 | 데이터 교환 없음 |
| **python-dateutil** | - | - | 하 | **미사용, 제거** |
| **로컬 파일 I/O** | 없음 (브라우저 위임) | 중 (Import 파괴적) | - | 서버는 파일시스템 미사용 |

---

## 6. 리팩토링 관점 시사점

**[확인된 사실 기반 판단]**

1. **외부 의존이 좁다는 것은 큰 이점이다.** 하드웨어 추상화 계층, API 클라이언트 mock, 네트워크 재시도 정책 같은 무거운 작업이 필요 없다. 리팩토링의 초점은 **내부 구조(계층 분리·테스트)** 에 100% 집중할 수 있다.

2. **테스트 격리 장벽은 사실상 MySQL 하나뿐이다.** 도메인 계산 로직을 순수 함수로 추출하면 DB 없이 단위 테스트가 가능해진다. 통합 테스트는 SQLite in-memory 또는 testcontainers로 대응 가능하다.

3. **오프라인 완전 동작이 눈앞이다.** Chart.js를 로컬로 내리면 외부 네트워크 의존이 0이 된다. `TODO.md`의 Electron 전환 구상과 정합한다.

4. **DB 이식성 검토 항목** (SQLite 전환 시):
   - `db.JSON` → SQLite 3.38+ JSON1 확장으로 대응 가능하나 쿼리 방식이 다름 (현재는 파이썬 측에서만 다루므로 영향 적음)
   - `db.Enum('BY_RESIDENTS','BY_UNITS')` (`app.py:246`) → SQLite는 CHECK 제약으로 에뮬레이션
   - `db.Numeric(12,2)` → SQLite는 실제 DECIMAL 타입이 없어 **부동소수점 오차 위험**. 정수(원 단위) 저장으로 바꾸는 것이 안전
   - `mysql.connector` 직접 사용부(`init_database`) 제거 필요
   - `ON UPDATE CURRENT_TIMESTAMP` → SQLAlchemy `onupdate`로 이미 대응됨 (`app.py:136` 등)

5. **자격증명 외부화는 리팩토링 1일차 작업**이다. 코드 변경 범위가 2줄로 작고, 이후 모든 환경 분리(개발/운영/테스트 DB)의 전제 조건이다.
