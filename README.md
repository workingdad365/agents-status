# agents-status

AI 코딩 에이전트의 설치 상태·버전·LLM 프로바이더를 확인하고, 설치·업데이트·삭제와 글로벌 지침 동기화를 수행하는 터미널 앱.

## 설치

Git·Python·uv 없이 설치 가능. 아래 명령은 버전 지정 없이 **최신 안정 버전**을 설치함.

### Linux / WSL2 / macOS

```bash
curl --fail --location --proto '=https' --proto-redir '=https' \
  https://github.com/workingdad365/agents-status-releases/releases/latest/download/install.sh \
  -o install-agents-status.sh
sh install-agents-status.sh
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

### 최초 1회: 배포 권한 설정

공개 배포 저장소 `workingdad365/agents-status-releases`에만 **Contents: Read and write** 권한을 가진 fine-grained 토큰을 발급하고, 비공개 소스 저장소에 등록함.

```bash
gh secret set RELEASE_TOKEN --repo workingdad365/agents-status
```

매 배포 시 재등록할 필요 없음. 토큰 만료·폐기·교체 시에만 다시 등록함. 토큰 값은 코드나 명령 인자에 기록하지 않음.

### 매 릴리스: 버전 변경 → 검증 → 커밋 → 태그 게시

아래 명령은 저장소 루트의 Bash 기준. `main`에서 배포할 변경 사항을 준비한 뒤 진행함.

**1. 코드 버전 변경**

`agents_status/__init__.py`의 `__version__`을 아직 사용하지 않은 새 버전으로 직접 변경함. `1.0.14` 이후 배포라면 `1.0.15` 등 다음 번호 사용.

**태그만 만들어서는 앱 버전이 바뀌지 않음. 코드 버전과 태그가 다르면 CI가 배포를 중단함.**

**2. 의존성 잠금 갱신 및 검증**

```bash
uv lock
uv sync --locked
VERSION=$(uv run --no-sync python -c 'from agents_status import __version__; print(__version__)')
uv run --no-sync python packaging/build_release.py --check-tag "v$VERSION"
uv run --no-sync python -m unittest discover -v
uv run --no-sync agents-status --version
uv run --no-sync agents-status --self-check
```

모든 명령이 성공하고 출력 버전이 의도한 배포 번호와 일치하는지 확인함. 실패하면 이후 단계를 진행하지 않음.

**3. 배포할 변경 사항 커밋·푸시**

```bash
git add agents_status/__init__.py uv.lock
git add -p
git diff --cached
git commit -m "release: v$VERSION"
git push origin main
```

`git add -p`에서 이번 배포에 포함할 코드·문서 변경을 선택함. 새 파일이 있다면 파일 경로를 지정해 별도로 `git add`함. 커밋하지 않은 변경은 릴리스에 포함되지 않음.

**4. 커밋된 코드 버전으로 태그 생성·푸시**

```bash
git diff --exit-code HEAD -- agents_status/__init__.py &&
VERSION=$(uv run --no-sync python -c 'from agents_status import __version__; print(__version__)') &&
git tag -a "v$VERSION" -m "agents-status $VERSION" &&
git push origin "v$VERSION"
```

첫 명령으로 버전 파일에 미커밋 변경이 없는지 확인함. 명령을 `&&`로 연결하므로 확인·태그 생성이 실패하면 푸시하지 않음. 기존 태그를 강제로 덮어쓰지 않음. 새 터미널에서도 코드에서 버전을 읽으므로 태그 번호를 따로 입력할 필요 없음.

**5. 배포 완료 확인**

[소스 저장소의 Release Actions](https://github.com/workingdad365/agents-status/actions/workflows/release.yml)에서 해당 태그 실행의 `build` 5개와 `publish`가 모두 성공했는지 확인함. 태그 푸시 성공만으로 배포 완료를 판단하지 않음.

```bash
gh release view "v$VERSION" --repo workingdad365/agents-status-releases
```

공개 릴리스에 ZIP 5개, `install.sh`, `install.ps1`, `SHA256SUMS`가 있으며 초안이 아닌지 확인함. 마지막으로 이 문서의 버전 미지정 설치 명령으로 설치하고 `agents-status --version`이 새 버전인지 확인함. 비공개 소스나 커밋 이력은 공개 배포 저장소에 복사하지 않음.

### 선택 사항: 태그 없이 CI 빌드 검증

```bash
gh workflow run release.yml --repo workingdad365/agents-status --ref main
```

수동 실행은 5개 플랫폼의 테스트·빌드만 수행하며 릴리스를 게시하지 않음. 태그 배포가 실패하면 Actions의 실패 원인을 먼저 수정함. 새 커밋을 올려도 기존 태그는 이동하지 않으므로, 코드 수정이 필요하면 새 버전으로 위 절차를 다시 진행함.
