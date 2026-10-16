# agents-status

AI 코딩 에이전트의 설치 상태·버전·LLM 프로바이더를 확인하고, 설치·업데이트·삭제와 글로벌 지침 동기화를 수행하는 터미널 앱.

## 설치

Git·Python·uv 없이 설치 가능. 아래 명령은 버전 지정 없이 **최신 안정 버전**을 설치함.

### Linux / WSL2 / macOS

```bash
curl --fail --location --proto '=https' --proto-redir '=https' \
  https://github.com/workingdad365/agents-status-releases/releases/latest/download/install.sh \
  -o install-agents-status.sh &&
sh install-agents-status.sh &&
"$HOME/.local/bin/agents-status"
```

### Windows PowerShell

```powershell
Invoke-WebRequest -UseBasicParsing `
  https://github.com/workingdad365/agents-status-releases/releases/latest/download/install.ps1 `
  -OutFile install-agents-status.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-agents-status.ps1
& "$env:LOCALAPPDATA\agents-status\bin\agents-status.cmd"
```

설치 후 새 터미널에서는 `agents-status`로 실행 가능. PATH 자동 등록을 원하지 않으면 `--no-path` / `-NoPath` 사용. fish 등 bash·zsh 이외의 셸에서는 `~/.local/bin`을 PATH에 직접 추가함.

Linux·WSL2·macOS에서는 기존 uv 설치를 확인하면 새 패키지 검증 후 자동으로 `uv tool uninstall agents-status`를 실행하고 릴리스 설치를 계속함. 전환 실패 시 기존 uv 환경과 실행기를 복구함. uv 소유로 확인되지 않은 실행기는 자동 삭제하지 않음.

| 환경 | 요구 사항 |
|---|---|
| Linux / WSL2 | x86_64: glibc 2.35 이상, ARM64: glibc 2.39 이상 |
| macOS | Intel 또는 Apple Silicon |
| Windows | x64, PowerShell 5.1 이상. ARM64는 x64 에뮬레이션 사용 |
| Linux / macOS 설치 도구 | curl, unzip/zipinfo, sha256sum 또는 shasum |

실행 패키지는 [공개 릴리스](https://github.com/workingdad365/agents-status-releases/releases)에서 다운로드 가능. 소스 저장소는 비공개이며, GitHub의 자동 생성 `Source code` 압축 파일에는 앱 소스가 포함되지 않음. OS 코드 서명·공증은 제공하지 않으므로 보안 정책에 따라 실행 허용이 필요할 수 있음.

## 조작

| 키 / 버튼 | 기능 |
|---|---|
| `↑` / `↓` | 에이전트 선택 |
| `U` / 설치·업데이트 | 선택한 에이전트 설치 또는 업데이트 |
| 삭제 | 프로그램 삭제 및 설정 보존 여부 선택 |
| 지침 동기화 | 선택한 에이전트에 글로벌 지침 반영 |
| 전체 동기화 | 지원하는 모든 에이전트에 글로벌 지침 반영 |
| 앱 업데이트 확인 / 앱 업데이트 | agents-status 자체 업데이트 확인 및 설치 |
| `R` | 설치 상태와 버전 새로고침 |
| 마우스 드래그 / `Ctrl+C` | 하단 로그 선택 및 복사 |
| `Q` | 종료. 작업 중에는 중단 여부 확인 |

- **설치·업데이트:** 설치는 즉시 시작하며, 업데이트는 실행할 명령을 확인한 뒤 진행함. 진행 상황과 오류는 하단 로그에 표시함.
- **삭제:** 확인 창에서 `Y`는 프로그램과 설정·인증·사용 기록을 함께 삭제하고, `N`은 설정을 보존함. `Esc`로 취소 가능.
- **로그 복사:** 드래그 후 버튼을 놓으면 선택한 내용을 복사함. 클립보드 연동을 지원하지 않는 터미널에서는 `Shift`를 누른 채 드래그하여 터미널 자체 선택 기능 사용. tmux에서는 `set -g set-clipboard on` 설정 필요.
- **작업 중단:** 종료 확인 창에서 중단 여부 선택 가능. 중단하더라도 이미 적용된 설치·업데이트·삭제 내용은 되돌리지 않음.

## 앱 업데이트

새 버전이 있으면 알림을 표시하며, **앱 업데이트**에서 승인하면 다운로드·검증 후 재시작함. 새 버전 시작에 실패하면 이전 버전으로 복구함.

앱 시작 시 새 버전을 확인하며 최근 6시간 이내의 조회 결과는 재사용함. 버튼으로 즉시 재확인 가능. 에이전트 작업 중에는 앱 업데이트를 시작하지 않으며, 네트워크 연결 실패 시에도 에이전트 관리 기능은 계속 사용 가능.

## 표시 정보

| 항목 | 설명 |
|---|---|
| 상태 | `설치됨`, `설정만`, `미설치` |
| 에이전트 | 도구 이름 |
| 버전 | 설치된 실행 파일에서 조회한 버전 |
| 프로바이더 | 설정에서 확인한 현재 LLM 프로바이더 |
| 실행 파일 | 감지한 실행 파일 경로 |

버전 명령 실행에 실패하거나 응답이 없으면 `조회 실패`로 표시함.

## 지원 에이전트

Claude, Codex CLI, Command Code, Copilot CLI, OpenCode, Kilo, OMP, Pi, Pi-Bolt, Antigravity, Crush, Hermes, OpenClaw 지원.

에이전트별 공식 설치·업데이트 명령 또는 기존 패키지 관리자를 사용함. 설치 방식을 판별할 수 없으면 자동 변경 대신 사유를 표시함. 에이전트에 따라 npm 등 별도의 실행·설치 도구가 필요할 수 있음.

**Pi와 Pi-Bolt는 설정·인증·세션·글로벌 지침을 공유함. 어느 쪽에서든 설정 삭제를 선택하면 양쪽에 영향을 줌.**

## 글로벌 지침 동기화

기본 원본은 앱에 포함된 `GLOBAL_INSTRUCTIONS.md`이며, 사용 중인 원본 경로는 화면과 로그에 표시함. 별도 파일을 사용하려면 실행 시 지정함.

```bash
agents-status --instructions /path/to/GLOBAL_INSTRUCTIONS.md
```

```powershell
agents-status --instructions C:\path\to\GLOBAL_INSTRUCTIONS.md
```

동기화 버튼을 누르면 즉시 반영함. **덮어쓰기 대상 파일에 별도 내용을 작성했다면 먼저 백업 필요.**

| 에이전트 | 기본 대상 파일 | 방식 |
|---|---|---|
| Claude | `~/.claude/CLAUDE.md` | 덮어쓰기 |
| Codex CLI | `~/.codex/AGENTS.md` | 덮어쓰기 |
| Command Code | `~/.commandcode/AGENTS.md` | 덮어쓰기 |
| Copilot CLI | `~/.copilot/copilot-instructions.md` | 덮어쓰기 |
| OpenCode | `~/.config/opencode/AGENTS.md` | 덮어쓰기 |
| Kilo | `~/.config/kilo/AGENTS.md` | 덮어쓰기 |
| OMP | `~/.omp/agent/AGENTS.md` | 덮어쓰기 |
| Pi / Pi-Bolt | `~/.pi/agent/AGENTS.md` | 덮어쓰기 |
| Antigravity | `~/.gemini/GEMINI.md` | 덮어쓰기 |
| Crush | `~/.config/crush/CRUSH.md` | 덮어쓰기 |
| Hermes | `~/.hermes/SOUL.md` | 지침 블록만 교체 |
| OpenClaw | `~/.openclaw/workspace/AGENTS.md` | 지침 블록만 교체 |

Hermes는 `HERMES_HOME`을 우선하며 Windows 기본 경로는 `%LOCALAPPDATA%\hermes`. Crush는 `CRUSH_GLOBAL_CONFIG`·`CRUSH_GLOBAL_DATA`와 XDG 경로 설정을 반영함.

블록 교체는 `GLOBAL_INSTRUCTIONS:BEGIN`과 `GLOBAL_INSTRUCTIONS:END` 마커 사이만 갱신하며 나머지 내용을 보존함. 원본 파일이 비어 있거나 읽을 수 없으면 동기화하지 않음.

## 제거

앱 종료 후 아래 설치 경로와 실행기를 삭제하고 PATH 등록을 제거함. 에이전트 자체 설치와 설정은 별도로 유지됨.

| 환경 | 설치 경로 | 실행기 |
|---|---|---|
| Linux / WSL2 | `~/.local/share/agents-status` | `~/.local/bin/agents-status` |
| macOS | `~/Library/Application Support/agents-status` | `~/.local/bin/agents-status` |
| Windows | `%LOCALAPPDATA%\agents-status` | 설치 경로의 `bin\agents-status.cmd` |

Linux·macOS는 셸 설정의 `# agents-status PATH`부터 `# agents-status PATH end`까지 제거함. Windows는 사용자 PATH에서 해당 `bin` 항목을 제거함. 공용 `~/.local/bin` 디렉터리 자체는 삭제하지 않음.

## 개발 및 테스트

Python 3.13.x와 [uv](https://docs.astral.sh/uv/) 필요.

```bash
uv sync
uv run agents-status
uv run python -m unittest -v
```

실행 스크립트 `./agents_status.sh` 또는 `.\agents_status.ps1`도 사용 가능. 설치 테스트에는 unzip/zipinfo 필요.

## 릴리스 게시

### 최초 1회 설정

Bash·Git·uv·GitHub CLI가 필요하며, `gh auth login`으로 로그인함. 공개 배포 저장소 `workingdad365/agents-status-releases`에만 **Contents: Read and write** 권한을 가진 fine-grained 토큰을 발급하고 비공개 소스 저장소에 등록함.

```bash
gh secret set RELEASE_TOKEN --repo workingdad365/agents-status
```

매 배포 시 재등록할 필요 없음. 토큰 만료·폐기·교체 시에만 다시 등록함.

### 매 릴리스

`main`에서 배포할 기능·문서 변경을 먼저 커밋한 뒤 실행함. 미커밋 변경이 있으면 배포하지 않음.

```bash
./release.sh
```

스크립트가 다음 절차를 자동 수행함.

1. 로그인·배포 토큰 등록·원격 브랜치 상태 확인.
2. 패치 버전 증가: `1.0.14` → `1.0.15`. 버전·태그 번호를 직접 입력할 필요 없음.
3. 잠금 파일 갱신, 전체 테스트, CLI 버전 및 실행 점검.
4. 버전 변경 커밋 및 같은 버전의 태그 생성.
5. `main`과 태그를 원자적으로 푸시.
6. 5개 플랫폼의 CI 테스트·빌드·자동 게시가 끝날 때까지 대기.
7. 공개 릴리스와 ZIP 5개·설치 스크립트 2개·체크섬 파일을 확인한 뒤 완료 URL 출력.

**태그 푸시만으로 배포 완료를 표시하지 않음.** 공개 게시와 배포 파일 확인까지 성공해야 스크립트가 성공 종료함. 기존 태그는 덮어쓰지 않음.

### 실패 시

- **커밋 전 검증 실패:** 버전·잠금 파일을 실행 전 상태로 복구함. 원인을 수정하고 커밋한 뒤 다시 실행함.
- **커밋 후 푸시 실패:** 릴리스 커밋과 생성된 태그를 보존함. 오류 안내의 태그를 확인하고 `git push --atomic origin main <해당 태그>`로 푸시를 재개함. 태그 생성 자체가 실패했다면 먼저 해당 커밋에 태그를 생성함.
- **CI 또는 게시 실패:** [Release Actions](https://github.com/workingdad365/agents-status/actions/workflows/release.yml)에서 해당 실행의 오류 확인. 권한 문제만 수정했다면 실패 작업을 재실행할 수 있음. 코드 수정이 필요하면 수정·커밋 후 `./release.sh`로 다음 버전을 배포함.

커밋 후 실패했을 때 무작정 스크립트를 다시 실행하면 버전이 한 번 더 증가하므로 기존 배포 상태를 먼저 확인함. 수동 태그 생성이나 버전 파일 수정은 일반 배포 절차에 필요 없음.

