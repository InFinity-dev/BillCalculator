# Windows에서만 먼저 드러난 튜토리얼 문제의 플랫폼 차이 분석

- 기록일: 2026-11-14
- 대상 범위: `v1.0.0` → `v1.0.2`
- 관련 기록: [v1.0.1 검증](v1.0.1-bugfix-verification.md), [v1.0.2 검증](v1.0.2-bugfix-verification.md)

## 결론

macOS에서 확인되지 않고 Windows 클라이언트에서 먼저 확인된 이유는 두 종류로 나뉜다.

1. Windows의 문자 인코딩, 프로세스 종료와 열린 파일 처리 방식 때문에 실제로 Windows에서만 실패한 문제
2. 운영체제와 무관한 결함이지만 macOS 수동·자동 검증 범위가 해당 경로를 실행하지 않아 Windows 사용자 흐름에서 먼저 발견된 문제

따라서 “macOS에서 통과했으므로 Windows 고유 버그다” 또는 “모든 문제가 PyInstaller 때문이다”라고 해석하면 안 된다.

| 문제 | 플랫폼 성격 | macOS에서 발견되지 않은 이유 |
|---|---|---|
| 데모 생성 완료 문구의 `cp1252` 오류 | Windows 환경에서 실제 재현 | macOS의 일반적인 UTF-8 출력 환경에서는 한국어 출력 성공 |
| 종료 지연과 SQLite `WinError 32` | Windows 프로세스·파일 잠금 차이 | POSIX 종료 처리와 열린 파일 삭제 정책이 다름 |
| PyInstaller `onefile` 자기 재실행 | 플랫폼 공통 패키징 결함 | macOS 실행에서는 당시 조건과 타이밍에서 허용됐으나 Windows에서 실패로 표면화 |
| 튜토리얼 진행도 초기화 | 플랫폼 공통 화면 상태 결함 | macOS 검증에서 동일한 페이지 이동 경로를 충분히 반복하지 않음 |
| 요금 계산 화면 스타일 누락 | 플랫폼 공통 자원 전달·검증 결함 | 기존 패키지 테스트가 실제 5단계와 `calculator.css`를 검사하지 않음 |
| 데모 데이터 준비 판정 누락 | 플랫폼 공통 서버 준비 검증 결함 | 기존 상태 확인이 실제 층·세대·계산서 건수를 검사하지 않음 |

## 1. macOS 개발 실행과 Windows 배포 실행의 조건 차이

개발 중 macOS 수동 검증에는 소스 기반 로컬 Flask 실행이 포함됐다. 반면 문제를 보고한 Windows 사용자는 Electron 설치본 안의 PyInstaller `onefile` 백엔드를 실행했다.

```text
macOS 소스 기반 검증
Python → Flask → 브라우저

Windows 배포본
Electron
  └─ PyInstaller onefile 백엔드
       ├─ seed-demo 하위 실행
       └─ 튜토리얼 serve 하위 실행
```

소스 실행에는 PyInstaller 부트로더, 자기 실행파일 재호출, 일회성 압축 해제 폴더가 없다. 따라서 macOS 로컬 웹 테스트만으로 패키징된 Windows 하위 프로세스 문제를 검출할 수 없다.

macOS 패키지 CI도 실행됐지만 v1.0.1 당시 스모크 테스트는 초기 튜토리얼 HTML과 일부 정적 자원만 확인했다. 실제 요금 계산 5단계와 튜토리얼 DB의 기대 데이터 건수는 v1.0.2에서 검증 범위에 추가됐다.

## 2. Windows에서 실제로만 실패한 요소

### 2.1 지역 코드 페이지와 한국어 표준 출력

[GitHub Actions 43453327570](https://github.com/InFinity-dev/BillCalculator/actions/runs/43453327570)에서 macOS Intel 패키지 스모크 테스트는 성공했지만 Windows x64는 데모 DB 생성 완료 문구를 출력하다 다음 오류로 실패했다.

```text
encodings\cp1252.py
UnicodeEncodeError: 'charmap' codec can't encode characters
```

오류 위치는 `scripts/demo.py`의 한국어 완료 메시지 출력이다. macOS의 일반적인 UTF-8 로케일에서는 같은 문자열을 출력할 수 있지만 Windows 패키지 프로세스의 출력 스트림은 당시 `cp1252`로 구성됐다. Python도 Windows에서 UTF-8 모드를 사용하지 않으면 지역 코드 페이지가 기본 텍스트 인코딩에 영향을 줄 수 있음을 [공식 문서](https://docs.python.org/3/using/windows.html#utf-8-mode)에 설명한다.

`PYTHONIOENCODING=utf-8`만 적용한 [실행 43454348932](https://github.com/InFinity-dev/BillCalculator/actions/runs/43454348932)에서도 같은 오류가 재현됐다. PyInstaller frozen 앱의 내장 인터프리터가 Python용 환경 변수를 일반 실행과 동일하게 처리하지 않을 수 있기 때문이다. 효과가 확인된 조치는 백엔드 시작 시 `sys.stdout`과 `sys.stderr`를 `reconfigure(encoding="utf-8", errors="backslashreplace")`로 직접 설정한 것이다.

### 2.2 프로세스 종료 방식

Python의 `Popen.terminate()`는 POSIX에서 `SIGTERM`을 보내지만 Windows에서는 `TerminateProcess()`를 호출한다. Windows의 강제 종료는 해당 프로세스에만 적용되고 자식 프로세스의 정상 종료 처리를 보장하지 않는다. 차이는 [Python subprocess 문서](https://docs.python.org/3/library/subprocess.html#subprocess.Popen.terminate)에 명시돼 있다.

BillCalculator의 패키징 백엔드는 PyInstaller 부트로더, 실제 서버와 튜토리얼 서버 자식으로 이어질 수 있다. 추적한 프로세스 하나만 종료하면 실제 튜토리얼 서버가 남아 SQLite 핸들을 계속 잡을 수 있었다.

[GitHub Actions 43455317105](https://github.com/InFinity-dev/BillCalculator/actions/runs/43455317105)의 Windows 작업은 종료 제한 시간을 넘긴 뒤 튜토리얼 DB를 삭제하지 못하고 `WinError 32`로 실패했다. 같은 실행의 macOS Intel 작업은 통과했다. Windows에서는 이후 `taskkill /PID ... /T /F`로 전체 프로세스 트리를 종료하도록 변경했다.

### 2.3 열린 파일 삭제 정책

Windows는 다른 프로세스가 사용 중인 파일의 삭제를 거부한다. Unix 계열인 macOS는 열린 파일의 디렉터리 항목을 제거할 수 있고 실제 저장 공간은 마지막 핸들이 닫힐 때 해제한다. 이 차이는 [Python `os.remove()` 문서](https://docs.python.org/3/library/os.html#os.remove)에 명시돼 있다.

따라서 같은 잔존 프로세스나 열린 SQLite 핸들이 있더라도 macOS 임시 폴더 정리는 겉으로 성공할 수 있지만 Windows는 즉시 `WinError 32`로 실패한다. 이는 Windows가 문제를 더 분명하게 드러낸 이유이자, macOS 성공만으로 프로세스 수명주기의 완전성을 판단하면 안 되는 이유다.

## 3. Windows 전용으로 단정할 수 없는 요소

### 3.1 PyInstaller 독립 하위 실행

PyInstaller 6.9 이상은 같은 `onefile` 실행파일을 `sys.executable`로 다시 시작할 때 기본적으로 현재 번들의 작업 프로세스로 해석할 수 있다. 독립적으로 실행되거나 부모보다 오래 살아야 하는 프로세스에는 `PYINSTALLER_RESET_ENVIRONMENT=1`이 필요하다. 이는 [PyInstaller 공식 문서](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#using-sys-executable-to-spawn-subprocesses-that-outlive-the-application-process)에 설명된 플랫폼 공통 요구사항이다.

Windows CI에서는 튜토리얼 서버 이동 실패로 먼저 드러났지만 이 설계 결함 자체는 Windows 전용이 아니다. 현재 코드는 frozen 실행이면 운영체제와 관계없이 데모 생성과 튜토리얼 서버 자식 모두에 독립 실행 환경을 적용한다.

### 3.2 진행도 초기화

진행도를 `localStorage`에 주로 의존하고 프로그램이 수행하는 모든 페이지 이동에 현재 단계를 전달하지 않은 것이 확인된 원인이다. 브라우저 문서 이동과 저장소 상태 문제이므로 macOS에서도 같은 경로를 실행하면 발생할 수 있었다.

Windows에서만 보고됐다는 사실은 Windows가 근본 원인이라는 증거가 아니다. macOS 수동 테스트에서 `상세` 버튼을 포함한 동일 이동 경로와 저장소 실패 조건을 충분히 반복하지 않은 테스트 범위 차이가 있었다.

### 3.3 스타일 누락

v1.0.1 패키지 스모크 테스트는 초기 튜토리얼 HTML의 `app.css`, `tutorial_overlay.css`, `tutorial_overlay.js`만 확인했다. 실제 `/calculator` 페이지와 이 화면이 크게 의존하는 `utilities.css`, `css/pages/calculator.css`는 확인하지 않았다.

따라서 macOS 패키지 테스트 성공은 요금 계산 화면의 스타일이 완전했다는 증거가 아니다. 원 사용자 PC의 Electron Network/Console 기록이 없어 Windows에서 어떤 개별 CSS 요청이 실패했는지는 특정할 수 없다. v1.0.2는 튜토리얼 CSS를 HTML에 직접 포함하고 실제 계산 화면을 패키지 테스트해 이 외부 요청 의존성과 검증 공백을 함께 제거했다.

### 3.4 데모 데이터 준비 판정

v1.0.1의 `/tutorial/health`는 튜토리얼 모드 서버가 응답한다는 사실만 확인했다. DB의 층·세대·미정산 전기·수도 계산서가 실제로 존재하는지는 검사하지 않았다. macOS에서 상태 확인이 성공했어도 데이터 준비가 완전했다는 의미는 아니었다.

v1.0.2부터 상태 확인과 패키지 스모크 테스트가 층 3개, 세대 8개, 전기 계산서 5건, 수도 계산서 1건을 검사한다. 이 문제도 Windows 고유 문제가 아니라 양쪽 플랫폼에 존재했던 준비 완료 계약의 누락이다.

## 4. 증거 수준과 표현 원칙

| 판단 | 증거 수준 |
|---|---|
| Windows `cp1252`가 한국어 출력을 중단시킴 | CI 예외와 호출 위치로 확정 |
| Windows 종료 후 튜토리얼 DB가 잠김 | CI의 종료 시간 초과와 `WinError 32`로 확정 |
| 프로세스 트리 종료 후 Windows 검증 성공 | 후속 CI로 확인 |
| 진행도 상태 전달 설계가 불완전했음 | 코드 비교와 회귀 테스트로 확인 |
| v1.0.1 패키지 테스트가 계산 화면과 데이터 실체를 검사하지 않음 | 당시 스모크 테스트 코드로 확인 |
| 사용자 PC에서 스타일 요청이 실패한 정확한 HTTP 오류 | 현장 Network/Console 로그 부재로 확인 불가 |
| 사용자 PC의 불완전 튜토리얼 DB가 만들어진 최초 하위 원인 | 당시 DB·백엔드 로그 부재로 확인 불가 |

향후 기록에서는 CI로 확인된 Windows 고유 오류, 플랫폼 공통 설계 결함과 현장 자료 부재로 추정만 가능한 원인을 구분해 표현한다.

## 5. 현재의 재발 방지 장치

- Windows·macOS 패키지를 각각 해당 운영체제에서 네이티브 빌드한다.
- frozen 하위 실행은 새 PyInstaller 인스턴스로 시작한다.
- 패키징 백엔드 표준 출력·오류 스트림을 UTF-8로 직접 구성한다.
- Windows 종료는 프로세스 트리 단위로 수행한다.
- 튜토리얼 준비 완료는 서버 응답뿐 아니라 실제 데모 데이터 구성을 검사한다.
- 패키지 테스트가 실제 요금 계산 5단계 HTML과 화면별 CSS·필수 JavaScript 포함 여부를 확인한다.
- 진행 단계는 URL, 실행별 쿠키와 `localStorage`에 중복 보존하며 모든 프로그램 이동에 명시적으로 전달한다.
