# agents-status

여러 AI 코딩 에이전트의 설치 상태, 현재 버전, LLM 프로바이더를 한 화면에서 확인하고 개별 업데이트 및 글로벌 지침 동기화를 수행하는 Textual TUI임.

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
| 지침 동기화 버튼 | 선택한 에이전트에 글로벌 지침 반영 |
| 전체 동기화 버튼 | 지원하는 모든 에이전트에 글로벌 지침 반영 |
| `R` | 설치 상태와 버전 새로고침 |
| 마우스 드래그 | 하단 로그의 텍스트를 선택하고 버튼을 놓으면 클립보드에 복사 |
| `Ctrl+C` | 선택한 텍스트를 다시 클립보드에 복사 (선택이 없으면 종료 안내) |
| `Q` | 종료 |

설치 버튼을 누르면 즉시 설치를 시작한다. 업데이트 전에는 실제 실행할 명령을 확인하는 창을 표시한다. 실행 명령·결과·오류는 하단 로그에 표시한다. 설치·업데이트·삭제 명령과 버전 조회는 `agents-status-` 접두사가 붙은 임시 디렉터리를 작업 디렉터리로 하여 실행하고 종료 후 그 디렉터리를 삭제한다. 일부 설치 스크립트(예: `@charmland/crush`의 npm 래퍼)가 작업 디렉터리에 남기는 임시 파일이 TUI를 실행한 디렉터리에 쌓이지 않도록 하기 위함이다.

하단 로그는 마우스로 드래그하여 텍스트를 선택할 수 있으며, 버튼을 놓는 순간 선택 영역을 OSC 52 이스케이프 시퀀스로 터미널 클립보드에 복사한다. Windows Terminal, iTerm2, kitty, WezTerm 등 OSC 52를 지원하는 터미널에서 동작하며, macOS 기본 Terminal.app처럼 지원하지 않는 터미널에서는 `Shift`를 누른 채 드래그하여 터미널 자체 선택 기능을 사용한다. tmux 안에서는 `set -g set-clipboard on` 설정이 필요하다. 다른 곳을 클릭하거나 `Esc`를 누르면 선택이 해제된다.

삭제 버튼은 설치·업데이트 버튼 옆에 배치한다. 확인 창에서 `Y`는 프로그램과 설정·인증·사용 기록을 함께 삭제하고, `N`은 설정을 보존하고 프로그램만 삭제한다. `Esc` 또는 취소 버튼으로 취소한다. `설정만` 상태에서는 `Y`로 남은 설정을 정리할 수 있으며, `미설치` 상태에서는 삭제 버튼을 비활성화한다. 작업 중에는 새로고침·설치·업데이트·삭제 버튼을 비활성화하며 완료 후 상태를 다시 조회한다.

설치는 npm 전역 패키지를 사용하며 Hermes는 공식 설치 스크립트의 비대화형 모드를 사용한다(Windows는 PowerShell로 `install.ps1`, 그 외는 bash로 `install.sh`). OMP는 npm 패키지가 실행에 Bun을 요구하므로 공식 설치 스크립트의 `--binary` 모드로 Bun이 필요 없는 단일 바이너리를 설치한다(Windows는 PowerShell로 `https://omp.sh/install.ps1`이 `$PI_INSTALL_DIR`, 기본값 `%LOCALAPPDATA%\omp`에 설치하고 사용자 PATH에 등록하며, 그 외는 `https://omp.sh/install`이 `~/.local/bin`에 설치한다). Antigravity 항목의 신규 설치 대상은 기존 감지 대상 중 Gemini CLI이다.

Windows Hermes는 PATH에서 찾지 못하면 `$HERMES_HOME`(기본값 `%LOCALAPPDATA%\hermes`) 아래의 `bin\hermes.exe`, `bin\hermes.cmd`, 구버전 `hermes-agent\venv\Scripts\hermes.exe`를 차례로 확인한다. 설치 후 부모 터미널의 PATH가 갱신되지 않아도 설치 상태를 감지하며, PATH에 이미 등록된 실행 파일을 우선한다. Windows OMP도 같은 이유로 PATH에서 찾지 못하면 `$PI_INSTALL_DIR`(기본값 `%LOCALAPPDATA%\omp`)의 `omp.exe`를 확인한다. Windows 설치 스크립트가 반환한 실패 코드도 앱에 전달하여 설치 실패를 성공으로 표시하지 않도록 한다.

Windows Hermes의 `bin\hermes.exe`·`bin\hermes.cmd` 설치는 같은 설치 루트의 `hermes-agent` 체크아웃과 venv 진입점을 확인하여 삭제한다. `hermes`·`hermes-acp`의 `.exe`·`.cmd` 런처와 체크아웃만 제거하며, `bin`에 함께 설치된 `uv` 등 공용 도구와 해당 PATH는 보존한다. `N`을 선택하면 설정과 `HERMES_HOME`도 보존한다.

OMP의 npm 패키지(`@oh-my-pi/pi-coding-agent`)는 실행 파일이 `#!/usr/bin/env bun` 스크립트라 Bun 없이는 동작하지 않는다. 신규 설치는 이 패키지 대신 공식 설치 스크립트의 단일 바이너리를 사용하므로 Bun 설치가 필요 없다. 이미 npm이나 Bun으로 설치한 OMP는 기존대로 패키지 관리자 설치로 판별하여 업데이트·삭제한다. Windows에서는 PowerShell용 OMP 래퍼가 `bun.exe`를 직접 호출하므로 `bun.cmd`만으로는 실행되지 않으며, 이 경우 버전이 `조회 실패`로 표시된다.

삭제는 실제 설치 경로에서 npm/Bun/Homebrew를 판별하여 해당 패키지 관리자를 사용한다. Windows에서는 심볼릭 링크 대신 shim을 두므로 Bun은 `%USERPROFILE%\.bun\bin\<이름>.bunx`에 기록된 대상 경로를, npm은 shim과 같은 디렉터리의 `node_modules\<패키지>`를 보고 판별한다. `bun remove --global`은 Windows에서 패키지만 지우고 `.bun\bin`의 `.exe`·`.bunx` shim을 남기므로, 같은 패키지를 가리키는 shim을 함께 삭제한다. 패키지는 이미 지워지고 shim만 남은 경우(`설치됨`이지만 버전이 `조회 실패`)에는 shim만 삭제한다. OpenCode는 npm 패키지 이름이 `opencode-ai`에서 `@opencode/cli`로, Pi는 `@mariozechner/pi-coding-agent`에서 `@earendil-works/pi-coding-agent`로 바뀌었으므로 두 이름 모두 판별한다(신규 설치는 새 이름을 사용한다). WinGet 이식 가능(portable) 패키지(예: `winget install GitHub.Copilot`)는 `%LOCALAPPDATA%\Microsoft\WinGet\Packages\<패키지 ID>_<원본>` 경로에서 패키지 ID를 읽어 `winget uninstall --id <ID> --exact --purge --force`로 삭제한다. `--force`는 `copilot update` 등 자체 업데이트로 파일 해시가 바뀐 경우를 위한 것이고, `--purge`는 백업 파일(`.old`)까지 지우기 위한 것이다. 기본 경로의 Claude·OpenCode·Hermes 네이티브 설치와 `~/.local/bin`의 독립 실행 바이너리도 지원한다. Hermes 공식 설치 스크립트 설치는 실행 파일(Unix는 `~/.local/bin/hermes` 셸 shim 또는 구버전 심볼릭 링크, Windows는 `venv\Scripts\hermes.exe`)이 가리키는 `hermes-agent` 체크아웃 디렉터리(기본 위치는 `$HERMES_HOME` 아래, Windows 기본값 `%LOCALAPPDATA%\hermes`, root FHS 설치는 `/usr/local/lib`)와 shim을 삭제한다. shim이 가리키는 진입점은 구버전 `venv/bin/hermes`와 최신 `.hermes/bin/hermes` 모두 인식하며, Unix에서는 같은 체크아웃을 가리키는 `hermes-agent`·`hermes-acp` shim도 함께 삭제한다. Kilo 공식 설치 스크립트 설치(`~/.kilo/bin/kilo`)는 tree-sitter 문법을 포함한 `~/.kilo/bin` 디렉터리를 삭제한다. OMP 공식 설치 스크립트의 단일 바이너리는 실행 파일을 삭제하며, Windows 기본 위치 `%LOCALAPPDATA%\omp`는 디렉터리째 삭제한다. Windows에서는 삭제한 디렉터리를 가리키는 사용자 PATH 항목을 레지스트리에서 제거하고, 설정까지 삭제하면 `HERMES_HOME` 환경 변수도 제거한다. Codex 공식 설치 스크립트 설치는 `~/.local/bin/codex` 링크(macOS는 `codex-code-mode-host` 링크 포함)와 `~/.codex/packages`의 `standalone`·`app-server-daemon` 패키지를 삭제한다. 이때 실행 중인 app-server 데몬 프로세스는 종료하지 않으므로, Windows에서 데몬이 실행 중이면 파일 잠금으로 삭제가 실패할 수 있다. Antigravity CLI(`agy`)는 공식 설치 스크립트가 설치한 단일 바이너리를 삭제하며, Windows에서는 `agy update`의 백업 파일이 남는 `%LOCALAPPDATA%\agy` 디렉터리를 함께 삭제한다. Pi 공식 설치 스크립트(`https://pi.dev/install.sh`, `install.ps1`)의 관리형 설치는 `~/.pi/agent/install`의 `managed-install.json`으로 판별하며, 설치 스크립트의 Uninstall Pi와 같이 진입점 링크(Unix의 `~/.local/bin/pi` 등 심볼릭 링크)·`~/.pi/agent/bin`의 런처(Unix는 `pi`, Windows는 `pi`·`pi.cmd`·`pi.ps1`·`pi-launcher.js`)·`install` 디렉터리를 삭제한다. `bin`에 Pi가 내려받은 `fd` 등 도구와 Windows 사용자 PATH의 `bin` 항목은 보존한다. 판별되지 않는 설치는 사유를 표시한다. 프로그램 삭제가 실패하면 설정 삭제를 진행하지 않는다. 설정 삭제 대상 경로는 확인 창에 표시하며, 설정 디렉터리가 심볼릭 링크이면 링크만 삭제한다.

Windows의 Claude Code 네이티브 설치는 [공식 제거 경로](https://code.claude.com/docs/en/setup#native-installation)에 따라 `%USERPROFILE%\.local\bin\claude.exe`와 `%USERPROFILE%\.local\share\claude`의 버전 파일을 삭제합니다. 실행 파일의 PE 형식을 확인하며, `N`을 선택하면 `.claude`와 `.claude.json` 설정을 보존합니다. 다른 도구가 함께 사용하는 `.local\bin` 디렉터리와 해당 PATH 항목도 보존합니다.

## 글로벌 지침 동기화

`sync-instructions`의 동기화 기능을 Python으로 통합함. 기본 원본은 이 프로젝트의 `GLOBAL_INSTRUCTIONS.md`이며, 상위의 `sync-instructions` 디렉터리 없이 독립적으로 동작함. 일반 패키지 설치 시에는 패키지에 포함된 원본을 사용함. 사용 중인 원본 경로는 동기화 버튼 옆과 실행 로그에 표시함.

다른 원본을 사용하려면 실행 시 `--instructions`로 지정함. 상대 경로는 실행한 작업 디렉터리를 기준으로 해석함.

```bash
uv run agents-status --instructions /path/to/GLOBAL_INSTRUCTIONS.md
./agents_status.sh --instructions ../sync-instructions/GLOBAL_INSTRUCTIONS.md
```

```powershell
.\agents_status.ps1 --instructions C:\path\to\GLOBAL_INSTRUCTIONS.md
```

버튼을 누르면 즉시 동기화하며, copy 대상 파일은 원본으로 덮어씀. 도구의 설정 디렉터리가 없으면 건너뜀. Crush는 데이터 디렉터리나 실행 파일만 있어도 설정 디렉터리를 생성하여 반영함. 원본을 읽을 수 없거나 내용이 비어 있으면 파일을 수정하지 않고 오류를 표시함. 개별 대상의 실패는 로그에 남기고 나머지 대상을 계속 처리함. 실행 중에는 설치·업데이트·삭제·동기화 버튼을 비활성화함.

| 에이전트 | 대상 파일 | 방식 |
|---|---|---|
| Claude | `~/.claude/CLAUDE.md` | copy |
| Codex CLI | `~/.codex/AGENTS.md` | copy |
| Command Code | `~/.commandcode/AGENTS.md` | copy |
| Copilot CLI | `~/.copilot/copilot-instructions.md` | copy |
| OpenCode | `~/.config/opencode/AGENTS.md` | copy |
| Kilo | `~/.config/kilo/AGENTS.md` | copy |
| OMP | `~/.omp/agent/AGENTS.md` | copy |
| Pi | `~/.pi/agent/AGENTS.md` | copy |
| Pi-Bolt | `~/.pi/agent/AGENTS.md` (Pi와 공유) | copy |
| Antigravity | `~/.gemini/GEMINI.md` | copy |
| Crush | `~/.config/crush/CRUSH.md` | copy |
| Hermes | `~/.hermes/SOUL.md` | merge |
| OpenClaw | `~/.openclaw/workspace/AGENTS.md` | merge |

Crush는 `CRUSH_GLOBAL_CONFIG`·`CRUSH_GLOBAL_DATA`를 우선하고, 미지정 시 `XDG_CONFIG_HOME`·`XDG_DATA_HOME` 아래의 `crush` 디렉터리를 사용함. Hermes는 기존 상태 조회와 동일하게 `HERMES_HOME`을 우선하며, Windows 기본 경로는 `%LOCALAPPDATA%\hermes`임.

merge 방식은 `<!-- GLOBAL_INSTRUCTIONS:BEGIN -->`과 `<!-- GLOBAL_INSTRUCTIONS:END -->` 사이만 교체하고 나머지 내용과 기존 개행 형식(LF/CRLF)을 보존함. 마커가 없으면 끝에 블록을 추가하고, 파일이 없으면 블록으로 새로 작성함. 첫 줄이 `# Global Instructions (ver.`로 시작하는 예전 copy 결과물은 블록 방식으로 전환함. 이때 Hermes는 `hermes-agent/SOUL.md`가 있으면 기본 내용으로 복원한 뒤 블록을 추가함. 불완전하거나 중복된 마커는 기존 파일을 보존하고 오류로 처리함.

## 표시 정보

| 열 | 설명 |
|---|---|
| 상태 | `설치됨`, `설정만`, `미설치`로 구분 |
| 에이전트 | 도구 이름 |
| 버전 | 설치된 실행 파일의 `--version` 결과 |
| 프로바이더 | 설정에서 확인한 현재 LLM 프로바이더 |
| 실행 파일 | PATH 또는 Windows Hermes 설치 위치에서 발견한 실행 파일 경로 |

버전 조회는 각 에이전트에 대해 병렬로 실행됩니다. 출력의 ANSI 색상·커서·창 제목 등 터미널 제어문자를 제거하고 첫 번째 비어 있지 않은 줄을 표시합니다. 실행 오류(예: npm으로 설치한 OMP 실행에 필요한 Bun이 PATH에 없음), 빈 출력 또는 60초 시간 초과는 `조회 실패`로 표시합니다.

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
| Pi-Bolt | `~/.pi/agent/settings.json`, `auth.json` (Pi와 공유) | `pi-bolt update` |
| Antigravity | Google 고정 | `agy update`(Windows `agy.exe` 포함), 그 외 npm/Bun/Homebrew 설치 판별 |
| Crush | `~/.local/share/crush/crush.json` | npm/Bun/Homebrew 설치 판별 |
| Hermes | `$HERMES_HOME/config.yaml`(기본값은 Windows `%LOCALAPPDATA%\hermes`, 그 외 `~/.hermes`) | `hermes update --yes` |
| OpenClaw | `~/.openclaw/openclaw.json` | `openclaw update --yes` |

Codex는 공식 설치 스크립트(`curl https://chatgpt.com/codex/install.sh | sh`)가 `~/.codex/packages/standalone`에 설치한 경우 `codex update`로 자체 업데이트한다. 이 명령은 같은 설치 스크립트를 비대화형으로 다시 실행한다. 그 외에는 실행 파일의 실제 경로에서 npm 또는 Homebrew 설치임이 확인된 경우에만 업데이트한다. Crush와 Gemini CLI(`gemini`로 감지된 경우)는 심볼릭 링크의 실제 경로와 패키지 정보를 이용해 npm/Bun/Homebrew 설치를 판별한다(Windows는 삭제와 같은 방식으로 Bun `.bunx` shim과 npm prefix 레이아웃을 판별). npm은 원래 설치된 prefix의 npm을 사용하고 동일한 prefix에 업데이트한다. Homebrew는 실제 Cellar/Caskroom 경로의 패키지명을 사용한다. 설치 방식을 판별할 수 없거나 해당 설치의 패키지 관리자가 없으면 경로와 사유를 안내한다.

Pi-Bolt는 [공식 저장소](https://github.com/opensec-git/Pi-Bolt)의 npm 패키지 `pi-bolt`로 설치하며 필수 설치 스크립트를 허용한다. Pi와 실행 파일은 별개지만 설정·인증·세션과 글로벌 지침은 공유하므로, 어느 쪽에서든 설정 삭제를 선택하면 양쪽에 반영된다. 공식 스크립트 설치(`~/.pi-bolt` 또는 `PIBOLT_INSTALL`)는 현재 실행 파일의 플랫폼별 설치 디렉터리와 진입점 링크를 삭제한다. Windows에서는 PATH에 없을 때 해당 설치 루트의 `pi-bolt-win32-<variant>/pi-bolt.exe`도 감지한다.

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
| `agents_status/instructions.py` | 에이전트별 글로벌 지침 복사 및 블록 병합 |
| `GLOBAL_INSTRUCTIONS.md` | 동기화할 글로벌 지침 원본 |
| `agents_status.sh` | uv 기반 실행 진입점 |
| `agents_status.ps1` | Windows PowerShell용 uv 기반 실행 진입점 |
| `test_agents_status.py` | 회귀 테스트 |
| `test_instructions.py` | 지침 동기화 및 버튼 동작 테스트 |

두 스크립트 모두 스크립트 위치의 프로젝트를 기준으로 동일한 Python TUI를 실행하며, 전달받은 인자와 종료 코드를 유지함.
