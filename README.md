# agents-status

여러 AI 코딩 에이전트의 설치 상태, 현재 버전, LLM 프로바이더를 한 화면에서 확인하고 개별 업데이트하는 Textual TUI입니다.

## 요구 사항

- Python 3.13.x
- [uv](https://docs.astral.sh/uv/)
- Linux, WSL2 또는 macOS

Python 버전은 `.python-version`에 `3.13`으로 지정되어 있으며, 의존성은 `uv.lock`으로 고정됩니다.

## 실행

```bash
./agents_status.sh
```

또는 직접 실행할 수 있습니다.

```bash
uv run agents-status
```

첫 실행 시 uv가 Python 환경을 만들고 Textual을 설치합니다.

## 조작

| 키/버튼 | 기능 |
|---|---|
| `↑` / `↓` | 에이전트 선택 |
| `U` / 설치·업데이트 버튼 | `미설치`·`설정만`이면 설치, `설치됨`이면 업데이트 |
| 삭제 버튼 | 프로그램 삭제 및 설정 삭제 여부 선택 |
| `R` | 설치 상태와 버전 새로고침 |
| `Q` | 종료 |

설치 버튼을 누르면 즉시 설치를 시작한다. 업데이트 전에는 실제 실행할 명령을 확인하는 창을 표시한다. 실행 명령·결과·오류는 하단 로그에 표시한다.

삭제 버튼은 설치·업데이트 버튼 옆에 배치한다. 확인 창에서 `Y`는 프로그램과 설정·인증·사용 기록을 함께 삭제하고, `N`은 설정을 보존하고 프로그램만 삭제한다. `Esc` 또는 취소 버튼으로 취소한다. `설정만` 상태에서는 `Y`로 남은 설정을 정리할 수 있으며, `미설치` 상태에서는 삭제 버튼을 비활성화한다. 작업 중에는 새로고침·설치·업데이트·삭제 버튼을 비활성화하며 완료 후 상태를 다시 조회한다.

설치는 npm 전역 패키지를 사용하며 Hermes는 공식 설치 스크립트의 비대화형 모드를 사용한다. Antigravity 항목의 신규 설치 대상은 기존 감지 대상 중 Gemini CLI이다.

삭제는 실제 설치 경로에서 npm/Bun/Homebrew를 판별하여 해당 패키지 관리자를 사용한다. 기본 경로의 Claude·OpenCode·Hermes 네이티브 설치와 `~/.local/bin`의 독립 실행 바이너리도 지원한다. 판별되지 않는 설치는 사유를 표시한다. 프로그램 삭제가 실패하면 설정 삭제를 진행하지 않는다. 설정 삭제 대상 경로는 확인 창에 표시하며, 설정 디렉터리가 심볼릭 링크이면 링크만 삭제한다.

## 표시 정보

| 열 | 설명 |
|---|---|
| 상태 | `설치됨`, `설정만`, `미설치`로 구분 |
| 에이전트 | 도구 이름 |
| 버전 | 설치된 실행 파일의 `--version` 결과 |
| 프로바이더 | 설정에서 확인한 현재 LLM 프로바이더 |
| 실행 파일 | PATH에서 발견한 실제 실행 파일 경로 |

버전 조회는 각 에이전트에 대해 병렬로 실행되며, 응답하지 않는 명령은 5초 후 `조회 실패`로 표시됩니다.

## 지원 에이전트

| 도구 | 프로바이더 조사 소스 | 개별 업데이트 |
|---|---|---|
| Claude | `~/.claude/settings.json`, 환경 변수 | `claude update` |
| Codex CLI | `~/.codex/config.toml`, `auth.json` | npm/Homebrew 설치를 판별한 경우 |
| Command Code | `~/.commandcode/config.json` | `<실행 파일> update` |
| Copilot CLI | GitHub Copilot 고정 | `copilot update` |
| OpenCode | 설정 모델 또는 최근 사용 모델 | `opencode upgrade` |
| Kilo | 설정 모델 또는 최근 사용 모델 | `kilo upgrade` |
| OMP | `~/.omp/agent/config.yml` | `omp update` |
| Pi | 설정 또는 인증 프로바이더 | `pi update --self` |
| Antigravity | Google 고정 | `agy update`, 그 외 npm/Bun/Homebrew 설치 판별 |
| Crush | `~/.local/share/crush/crush.json` | npm/Bun/Homebrew 설치 판별 |
| Hermes | `~/.hermes/config.yaml` | `hermes update --yes` |
| OpenClaw | `~/.openclaw/openclaw.json` | `openclaw update --yes` |

자체 업데이트 명령이 없는 Codex는 실행 파일의 실제 경로에서 npm 또는 Homebrew 설치임이 확인된 경우에만 업데이트한다. Crush와 Gemini CLI(`gemini`로 감지된 경우)는 심볼릭 링크의 실제 경로와 패키지 정보를 이용해 npm/Bun/Homebrew 설치를 판별한다. npm은 원래 설치된 prefix의 npm을 사용하고 동일한 prefix에 업데이트한다. Homebrew는 실제 Cellar/Caskroom 경로의 패키지명을 사용한다. 설치 방식을 판별할 수 없거나 해당 설치의 패키지 관리자가 없으면 경로와 사유를 안내한다.

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
| `agents_status.ps1` | 기존 Windows PowerShell 조회 전용 스크립트 |
| `test_agents_status.py` | 회귀 테스트 |

PowerShell 스크립트는 기존 조회 전용 구현으로 유지됩니다. 새 TUI와 개별 업데이트 기능은 현재 `agents_status.sh` 또는 `uv run agents-status` 실행 경로에서 제공됩니다.
