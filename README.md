# agents-status

여러 AI 코딩 도구의 설치 여부와 현재 설정된 LLM 프로바이더를 한 번에 조사하여 출력함. 설정을 변경하지 않으며 조사 결과만 보여줌.

대상 도구 목록은 [sync-instructions](../sync-instructions) 와 동일함.

## 구성

| 파일 | 설명 |
|---|---|
| `agents_status.sh` | Linux / WSL2 / macOS 용 조사 스크립트 (bash) |
| `agents_status.ps1` | Windows PowerShell 5.1 / PowerShell 7 용 조사 스크립트 |

두 스크립트는 동일한 대상과 동일한 규칙으로 동작함.

## 사용법

**Linux / WSL2 / macOS**

```bash
./agents_status.sh
```

**Windows PowerShell**

```powershell
powershell -ExecutionPolicy Bypass -File .\agents_status.ps1
```

`-ExecutionPolicy Bypass` 는 이번 프로세스에서만 스크립트 실행 정책 검사를 건너뜀. 시스템 설정은 바뀌지 않음.

실행 결과 예시:

```
[ok]    claude: /home/user/.local/bin/claude | provider: anthropic
[ok]    codex cli: /home/user/.nvm/versions/node/v26.10.0/bin/codex | provider: openai (ChatGPT 로그인)
[cfg]   commandcode: 실행파일 없음, 설정 디렉토리만 남음 (/home/user/.commandcode) | provider: openrouter
[none]  omp: 미설치
...

합계: 설치 8개, 설정만 남음 1개, 미설치 2개
```

## 상태 분류

| 표시 | 조건 |
|---|---|
| `[ok]` | 실행파일이 PATH 에 있음. 경로와 프로바이더를 출력함 |
| `[cfg]` | 실행파일은 없고 설정 디렉토리만 남아 있음. 설정 파일에서 읽을 수 있으면 프로바이더도 출력함 |
| `[none]` | 실행파일도 설정 디렉토리도 없음 |

## 대상 도구와 프로바이더 조사 방식

모델 이름은 출력하지 않고 프로바이더만 출력함. 설정 파일은 정규식 단순 검색으로 읽음.

| 도구 | 실행파일 | 최상위 디렉토리 | 프로바이더 조사 소스 |
|---|---|---|---|
| claude | `claude` | `~/.claude` | 환경변수 또는 `settings.json` 의 `CLAUDE_CODE_USE_BEDROCK` / `VERTEX` / `FOUNDRY`, `ANTHROPIC_BASE_URL`. 없으면 `anthropic` |
| codex cli | `codex` | `~/.codex` | `config.toml` 의 `model_provider`. 없으면 `auth.json` 의 `auth_mode` (ChatGPT 로그인 / API key) |
| commandcode | `commandcode` | `~/.commandcode` | `config.json` 의 `modelProvider`. 없으면 `model` 의 접두어 |
| copilot CLI | `copilot` | `~/.copilot` | 고정 `github-copilot` |
| opencode | `opencode` | `~/.config/opencode` | `opencode.jsonc` 의 `model` 접두어. 없으면 `~/.local/state/opencode/model.json` 의 최근 사용 프로바이더 |
| omp | `omp` | `~/.omp` | `agent/config.yml` 의 `modelRoles.default` 접두어 |
| pi | `pi` | `~/.pi` | `agent/settings.json` 의 `defaultProvider`. 없으면 `agent/auth.json` 의 인증된 프로바이더 목록 |
| antigravity | `agy`, `antigravity`, `gemini` | `~/.gemini` | 고정 `google` |
| crush | `crush` | `~/.config/crush` | `~/.local/share/crush/crush.json` (Windows 는 `%LOCALAPPDATA%\crush\crush.json`) 의 `models.large.provider` |
| hermes | `hermes` | `~/.hermes` | `config.yaml` 의 `model.provider` |
| openclaw | `openclaw` | `~/.openclaw` | `openclaw.json` 의 `model.primary` 접두어 |

Windows 에서는 `~` 가 `C:\Users\<사용자>` 로 풀림.

### 대상 추가

- `agents_status.sh`: `TARGETS` 배열에 `"도구이름|실행파일후보(공백 구분)|최상위디렉토리|조사함수명"` 형식으로 추가하고 `probe_<이름>` 함수를 작성함.
- `agents_status.ps1`: `$Targets` 배열에 `@{ Name; Bins; Root; Probe }` 해시테이블로 추가하고 `Probe-<이름>` 함수를 작성함.

## 주의 사항

- JSON / YAML 을 정규식으로 단순 검색하므로 도구의 설정 파일 구조가 크게 바뀌면 `unknown` 이 출력될 수 있음.
- 설정 파일에 인증 토큰이 포함된 경우가 있으나 스크립트는 프로바이더 이름만 출력하고 토큰은 출력하지 않음.
- `agents_status.ps1` 은 UTF-8 BOM 으로 저장되어 있음. PowerShell 5.1 이 한글 스크립트를 올바르게 읽기 위한 것이므로 BOM 을 제거하지 않아야 함.
