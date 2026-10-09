# agents-status

여러 AI 코딩 에이전트의 설치 상태, 현재 버전, LLM 프로바이더를 한 화면에서 확인하고 개별 업데이트하는 Textual TUI입니다.

## 요구 사항

- Python 3.13.x
- [uv](https://docs.astral.sh/uv/)
- Windows PowerShell 5.1 이상, Linux, WSL2 또는 macOS

Python 버전은 `.python-version`에 `3.13`으로 지정되어 있으며, 의존성은 `uv.lock`으로 고정됩니다.

## 실행

Linux / WSL2 / macOS:

```bash
./agents_status.sh
```

Windows PowerShell:

```powershell
.\agents_status.ps1
```

스크립트 실행 정책으로 차단되는 경우 다음 명령으로 실행함.

```powershell
powershell -ExecutionPolicy Bypass -File .\agents_status.ps1
```

또는 직접 실행할 수 있습니다.

```bash
uv run agents-status
```

첫 실행 시 uv가 Python 환경을 만들고 Textual을 설치합니다.

### 전역 설치

어느 디렉터리에서든 `agents-status` 명령으로 실행하려면 uv 도구로 설치한다. `--editable` 옵션을 주면 프로젝트 소스를 그대로 참조하므로 `git pull` 등으로 소스가 바뀌어도 재설치 없이 반영된다.

```bash
uv tool install --editable .
agents-status
```

실행 파일은 `~/.local/bin`(Windows는 `%USERPROFILE%\.local\bin`)에 설치되며, PATH에 없으면 `uv tool update-shell`로 추가한다. 의존성이 바뀐 경우에는 `uv tool install --editable . --reinstall`로 다시 설치하고, 제거는 `uv tool uninstall agents-status`로 한다.

## 조작

| 키/버튼 | 기능 |
|---|---|
| `↑` / `↓` | 에이전트 선택 |
| `U` / 설치·업데이트 버튼 | `미설치`·`설정만`이면 설치, `설치됨`이면 업데이트 |
| 삭제 버튼 | 프로그램 삭제 및 설정 삭제 여부 선택 |
| `R` | 설치 상태와 버전 새로고침 |
| 마우스 드래그 | 하단 로그의 텍스트를 선택하고 버튼을 놓으면 클립보드에 복사 |
| `Ctrl+C` | 선택한 텍스트를 다시 클립보드에 복사 (선택이 없으면 종료 안내) |
| `Q` | 종료 |

설치 버튼을 누르면 즉시 설치를 시작한다. 업데이트 전에는 실제 실행할 명령을 확인하는 창을 표시한다. 실행 명령·결과·오류는 하단 로그에 표시한다. 설치·업데이트·삭제 명령과 버전 조회는 `agents-status-` 접두사가 붙은 임시 디렉터리를 작업 디렉터리로 하여 실행하고 종료 후 그 디렉터리를 삭제한다. 일부 설치 스크립트(예: `@charmland/crush`의 npm 래퍼)가 작업 디렉터리에 남기는 임시 파일이 TUI를 실행한 디렉터리에 쌓이지 않도록 하기 위함이다.

하단 로그는 마우스로 드래그하여 텍스트를 선택할 수 있으며, 버튼을 놓는 순간 선택 영역을 OSC 52 이스케이프 시퀀스로 터미널 클립보드에 복사한다. Windows Terminal, iTerm2, kitty, WezTerm 등 OSC 52를 지원하는 터미널에서 동작하며, macOS 기본 Terminal.app처럼 지원하지 않는 터미널에서는 `Shift`를 누른 채 드래그하여 터미널 자체 선택 기능을 사용한다. tmux 안에서는 `set -g set-clipboard on` 설정이 필요하다. 다른 곳을 클릭하거나 `Esc`를 누르면 선택이 해제된다.

삭제 버튼은 설치·업데이트 버튼 옆에 배치한다. 확인 창에서 `Y`는 프로그램과 설정·인증·사용 기록을 함께 삭제하고, `N`은 설정을 보존하고 프로그램만 삭제한다. `Esc` 또는 취소 버튼으로 취소한다. `설정만` 상태에서는 `Y`로 남은 설정을 정리할 수 있으며, `미설치` 상태에서는 삭제 버튼을 비활성화한다. 작업 중에는 새로고침·설치·업데이트·삭제 버튼을 비활성화하며 완료 후 상태를 다시 조회한다.

설치는 npm 전역 패키지를 사용하며 Hermes는 공식 설치 스크립트의 비대화형 모드를 사용한다(Windows는 PowerShell로 `install.ps1`, 그 외는 bash로 `install.sh`). Antigravity 항목의 신규 설치 대상은 기존 감지 대상 중 Gemini CLI이다.

OMP의 npm 패키지는 실행에 Bun이 필요하므로, 신규 설치 전에 PATH에서 Bun을 확인하고 없으면 설치 방법을 안내한다. Windows에서는 PowerShell용 OMP 래퍼가 `bun.exe`를 직접 호출하므로 `bun.cmd`만으로는 충분하지 않다. [Bun 공식 설치 방법](https://bun.com/docs/installation)으로 설치한 뒤 실제 `bun.exe`가 있는 폴더를 PATH에 등록해야 한다. npm으로 Bun을 설치한 경우 해당 폴더는 `<npm prefix --global 결과>\node_modules\bun\bin`이다. 새 터미널에서 `bun.exe --version`과 `omp --version`으로 확인한다.

삭제는 실제 설치 경로에서 npm/Bun/Homebrew를 판별하여 해당 패키지 관리자를 사용한다. 기본 경로의 Claude·OpenCode·Hermes 네이티브 설치와 `~/.local/bin`의 독립 실행 바이너리도 지원한다. Hermes 공식 설치 스크립트 설치는 실행 파일(Unix는 `~/.local/bin/hermes` 셸 shim 또는 구버전 심볼릭 링크, Windows는 `venv\Scripts\hermes.exe`)이 가리키는 `hermes-agent` 체크아웃 디렉터리(기본 위치는 `$HERMES_HOME` 아래, Windows 기본값 `%LOCALAPPDATA%\hermes`, root FHS 설치는 `/usr/local/lib`)와 shim을 삭제한다. Windows에서는 삭제한 디렉터리를 가리키는 사용자 PATH 항목을 레지스트리에서 제거하고, 설정까지 삭제하면 `HERMES_HOME` 환경 변수도 제거한다. Codex 공식 설치 스크립트 설치는 `~/.local/bin/codex` 링크(macOS는 `codex-code-mode-host` 링크 포함)와 `~/.codex/packages`의 `standalone`·`app-server-daemon` 패키지를 삭제한다. 이때 실행 중인 app-server 데몬 프로세스는 종료하지 않으므로, Windows에서 데몬이 실행 중이면 파일 잠금으로 삭제가 실패할 수 있다. Antigravity CLI(`agy`)는 공식 설치 스크립트가 설치한 단일 바이너리를 삭제하며, Windows에서는 `agy update`의 백업 파일이 남는 `%LOCALAPPDATA%\agy` 디렉터리를 함께 삭제한다. 판별되지 않는 설치는 사유를 표시한다. 프로그램 삭제가 실패하면 설정 삭제를 진행하지 않는다. 설정 삭제 대상 경로는 확인 창에 표시하며, 설정 디렉터리가 심볼릭 링크이면 링크만 삭제한다.

Windows의 Claude Code 네이티브 설치는 [공식 제거 경로](https://code.claude.com/docs/en/setup#native-installation)에 따라 `%USERPROFILE%\.local\bin\claude.exe`와 `%USERPROFILE%\.local\share\claude`의 버전 파일을 삭제합니다. 실행 파일의 PE 형식을 확인하며, `N`을 선택하면 `.claude`와 `.claude.json` 설정을 보존합니다. 다른 도구가 함께 사용하는 `.local\bin` 디렉터리와 해당 PATH 항목도 보존합니다.

## 표시 정보

| 열 | 설명 |
|---|---|
| 상태 | `설치됨`, `설정만`, `미설치`로 구분 |
| 에이전트 | 도구 이름 |
| 버전 | 설치된 실행 파일의 `--version` 결과 |
| 프로바이더 | 설정에서 확인한 현재 LLM 프로바이더 |
| 실행 파일 | PATH에서 발견한 실제 실행 파일 경로 |

버전 조회는 각 에이전트에 대해 병렬로 실행됩니다. 출력의 ANSI 색상·커서·창 제목 등 터미널 제어문자를 제거하고 첫 번째 비어 있지 않은 줄을 표시합니다. 실행 오류(예: OMP 실행에 필요한 Bun이 PATH에 없음), 빈 출력 또는 60초 시간 초과는 `조회 실패`로 표시합니다.

## 지원 에이전트

| 도구 | 프로바이더 조사 소스 | 개별 업데이트 |
|---|---|---|
| Claude | `~/.claude/settings.json`, 환경 변수 | `claude update` |
| Codex CLI | `~/.codex/config.toml`, `auth.json` | 공식 설치 스크립트 설치는 `codex update`, 그 외 npm/Homebrew 설치를 판별한 경우 |
| Command Code | `~/.commandcode/config.json` | `<실행 파일> update` |
| Copilot CLI | GitHub Copilot 고정 | `copilot update` |
| OpenCode | 설정 모델 또는 최근 사용 모델 | `opencode upgrade` |
| Kilo | 설정 모델 또는 최근 사용 모델 | `kilo upgrade` |
| OMP | `~/.omp/agent/config.yml` | `omp update` |
| Pi | 설정 또는 인증 프로바이더 | `pi update --self` |
| Antigravity | Google 고정 | `agy update`(Windows `agy.exe` 포함), 그 외 npm/Bun/Homebrew 설치 판별 |
| Crush | `~/.local/share/crush/crush.json` | npm/Bun/Homebrew 설치 판별 |
| Hermes | `$HERMES_HOME/config.yaml`(기본값은 Windows `%LOCALAPPDATA%\hermes`, 그 외 `~/.hermes`) | `hermes update --yes` |
| OpenClaw | `~/.openclaw/openclaw.json` | `openclaw update --yes` |

Codex는 공식 설치 스크립트(`curl https://chatgpt.com/codex/install.sh | sh`)가 `~/.codex/packages/standalone`에 설치한 경우 `codex update`로 자체 업데이트한다. 이 명령은 같은 설치 스크립트를 비대화형으로 다시 실행한다. 그 외에는 실행 파일의 실제 경로에서 npm 또는 Homebrew 설치임이 확인된 경우에만 업데이트한다. Crush와 Gemini CLI(`gemini`로 감지된 경우)는 심볼릭 링크의 실제 경로와 패키지 정보를 이용해 npm/Bun/Homebrew 설치를 판별한다. npm은 원래 설치된 prefix의 npm을 사용하고 동일한 prefix에 업데이트한다. Homebrew는 실제 Cellar/Caskroom 경로의 패키지명을 사용한다. 설치 방식을 판별할 수 없거나 해당 설치의 패키지 관리자가 없으면 경로와 사유를 안내한다.

## 개발 및 테스트

```bash
uv sync
uv run python -m unittest -v
```

테스트는 임시 홈 디렉터리에서 프로바이더 판별 규칙과 업데이트 명령 선택을 검증합니다.

## 프로젝트 구성

| 경로 | 설명 |
|---|---|
| `agents_status/app.py` | Textual 화면과 사용자 동작 |
| `agents_status/core.py` | 상태·버전 조사와 업데이트 실행 |
| `agents_status.sh` | uv 기반 실행 진입점 |
| `agents_status.ps1` | Windows PowerShell용 uv 기반 실행 진입점 |
| `test_agents_status.py` | 회귀 테스트 |

두 스크립트 모두 스크립트 위치의 프로젝트를 기준으로 동일한 Python TUI를 실행하며, 전달받은 인자와 종료 코드를 유지함.
