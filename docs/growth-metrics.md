# GitHub 성장 지표 자동 수집

BillCalculator는 `.github/workflows/collect-growth-metrics.yml`을 통해 GitHub Release 다운로드 수를 매일 보관한다. 별도 서버는 사용하지 않으며 GitHub Actions가 한국 시간 오전 9시 17분에 실행된다.

## 수집 범위

- Release별·파일별 누적 다운로드 수
- `.exe`, `.dmg` 등 설치 파일의 누적 합계와 전일 대비 변화
- 체크섬 등 보조 파일의 누적 다운로드 수
- 권한이 허용된 경우 최근 14일 저장소 조회·순 방문자·클론·유입 경로

GitHub의 `download_count`는 고유 사용자 수가 아니라 파일 요청 횟수다. 앱 설치와 실행 성공 여부도 뜻하지 않는다.

## 실행 방식

- 자동 실행: 매일 `09:17 Asia/Seoul`
- 수동 실행: GitHub의 **Actions → Collect growth metrics → Run workflow**
- 실행 코드: 기본 브랜치 `master`
- 데이터 저장: `analytics-data` 브랜치

첫 실행 때 `analytics-data` 브랜치가 없으면 원본 소스와 분리된 데이터 전용 브랜치를 자동 생성한다. 정각의 Actions 혼잡을 피하기 위해 17분에 실행한다. GitHub 상황에 따라 시작이 지연될 수 있다. 공개 저장소에 60일 동안 활동이 없으면 예약 워크플로가 비활성화될 수 있으므로 Actions 화면에서 상태를 확인한다.

## 저장 파일

| 파일 | 내용 |
|---|---|
| `README.md` | 사람이 바로 확인할 수 있는 최신 요약 |
| `latest.json` | 가장 최근 전체 API 결과 |
| `snapshots/YYYY-MM-DD.json` | 한국 기준 날짜별 원본 |
| `downloads.csv` | 파일별 누적 다운로드와 일간 변화 |
| `daily-summary.csv` | 날짜별 설치 파일 합계와 저장소 트래픽 요약 |

같은 날 수동으로 다시 실행하면 그 날짜의 스냅샷을 최신 값으로 교체하고, 일간 변화는 직전 날짜의 스냅샷과 비교한다. Release 자산을 삭제하고 다시 올려 누적 수가 초기화되면 음수 변화가 기록될 수 있으므로 해당 날짜를 별도로 확인한다.

## 저장소 트래픽 권한

Release 다운로드는 기본 `GITHUB_TOKEN`으로 수집한다. 저장소 조회·클론·유입 경로 API까지 수집하려면 다음 조건의 fine-grained personal access token을 Repository secret `METRICS_TRAFFIC_TOKEN`으로 등록한다.

- 대상 저장소: `InFinity-dev/BillCalculator`
- Repository permission: **Administration — Read-only**
- Secret 이름: `METRICS_TRAFFIC_TOKEN`

Secret이 없거나 권한이 부족해도 작업은 실패하지 않는다. Release 다운로드는 계속 보관하고 트래픽 항목만 미수집 사유를 기록한다. 토큰 값 자체는 로그나 데이터 파일에 저장하지 않는다.

## 개인정보와 해석

이 자동화는 GitHub가 제공하는 저장소 집계값만 사용하며 세대명, 주소, 검침값, 청구액 또는 앱 내부 데이터를 수집하지 않는다. 다운로드 수를 실제 사용자 수로 해석하지 않고, 향후 앱 내부 익명 통계를 도입할 경우에도 별도 동의와 데이터 최소화 정책을 적용한다.
