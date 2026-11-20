# 릴리즈 검증 기록

이 디렉터리는 공개 릴리즈의 버그 수정 근거, 회귀 방지 방법, 빌드 결과와 검증 한계를 기록한다. 사용자용 변경 사항은 각 GitHub Release를, 현재 구현의 Ground Truth는 코드와 자동 테스트를 우선한다.

| 버전 | 검증 기록 | 핵심 내용 |
|---|---|---|
| v1.0.1 | [버그 수정 검증](v1.0.1-bugfix-verification.md) | 튜토리얼 진행 상태 보존, 정적 자원 주소 안정화. 이후 계산 화면 스타일 재발로 부분 개선임을 정정 |
| v1.0.2 | [버그 수정 검증](v1.0.2-bugfix-verification.md) | 튜토리얼 데모 데이터 실체 검증·자동 재생성, 계산 화면 필수 자원의 HTML 내장과 패키지 회귀 검사 |
| v1.0.3 | [잔액 정산 및 초과 입금 검증](v1.0.3-bugfix-verification.md) | 정산서 잔액 적용 버튼 수정, 미납액 초과 입금의 예상 잔액·이후 정산 차감 정책 명시와 회귀 검사 |

공통 원인 분석:

- [Windows에서만 먼저 드러난 튜토리얼 문제의 플랫폼 차이](windows-macos-tutorial-difference.md)

- 최신 대상 릴리즈: [v1.0.3](https://github.com/InFinity-dev/BillCalculator/releases/tag/v1.0.3)
- v1.0.3 배포 검증: [GitHub Actions 44737167198](https://github.com/InFinity-dev/BillCalculator/actions/runs/44737167198)

릴리즈 태그 이후 작성된 검증 문서는 해당 실행 파일의 내용을 바꾸지 않는다. 문서가 설명하는 코드 범위는 각 문서에 고정된 커밋과 태그로 추적한다.
