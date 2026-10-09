from __future__ import annotations

import asyncio
import codecs
from dataclasses import dataclass
from enum import StrEnum
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Callable, Mapping, Sequence


class StatusKind(StrEnum):
    INSTALLED = "설치됨"
    CONFIG_ONLY = "설정만"
    MISSING = "미설치"


ProviderProbe = Callable[[Path, Mapping[str, str]], str]


@dataclass(frozen=True, slots=True)
class AgentSpec:
    key: str
    name: str
    binaries: tuple[str, ...]
    config_root: str
    provider_probe: ProviderProbe
    version_args: tuple[str, ...] = ("--version",)
    update_args: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class AgentStatus:
    spec: AgentSpec
    status: StatusKind
    executable: Path | None
    version: str
    provider: str


@dataclass(frozen=True, slots=True)
class UpdateCommand:
    argv: tuple[str, ...]
    display: str


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    output: str


@dataclass(frozen=True, slots=True)
class UninstallPlan:
    command: UpdateCommand | None = None
    paths: tuple[Path, ...] = ()
    # 값이 삭제한 경로를 가리키면 함께 제거할 사용자 환경 변수(Windows 전용).
    variables: tuple[str, ...] = ()

    @property
    def display(self) -> str:
        if self.command:
            return self.command.display
        if not self.paths:
            return "설치된 프로그램 없음"
        text = "삭제: " + ", ".join(map(str, self.paths))
        if sys.platform == "win32":
            text += "\n정리: 삭제한 경로를 가리키는 사용자 PATH 항목" + "".join(f", {name}" for name in self.variables)
        return text


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _json_string(path: Path, key: str) -> str:
    text = _read(path)
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]*)"', text)
    return match.group(1) if match else ""


def _provider_of(reference: str) -> str:
    return reference.partition("/")[0]


def _model_provider(reference: str) -> str:
    head, separator, tail = reference.partition("/")
    return head if separator and head and tail else ""


def _claude(home: Path, env: Mapping[str, str]) -> str:
    source = "\n".join(f"{key}={value}" for key, value in env.items())
    source += "\n" + _read(home / ".claude/settings.json")
    variants = (
        ("CLAUDE_CODE_USE_BEDROCK", "anthropic (AWS Bedrock)"),
        ("CLAUDE_CODE_USE_VERTEX", "anthropic (Google Vertex AI)"),
        ("CLAUDE_CODE_USE_FOUNDRY", "anthropic (Azure AI Foundry)"),
    )
    for variable, provider in variants:
        if re.search(rf'{variable}["=: ]+[\"]?(?:1|true)', source, re.IGNORECASE):
            return provider
    match = re.search(r'ANTHROPIC_BASE_URL["=: ]+[\"]?([^" ,\n]+)', source)
    return f"anthropic 호환 ({match.group(1)})" if match else "anthropic"


def _codex(home: Path, env: Mapping[str, str]) -> str:
    del env
    match = re.search(
        r'^model_provider\s*=\s*"([^"]*)"',
        _read(home / ".codex/config.toml"),
        re.MULTILINE,
    )
    if match:
        return match.group(1)
    mode = _json_string(home / ".codex/auth.json", "auth_mode")
    return {
        "chatgpt": "openai (ChatGPT 로그인)",
        "apikey": "openai (API key)",
        "": "openai (미로그인)",
    }.get(mode, f"openai ({mode})")


def _commandcode(home: Path, env: Mapping[str, str]) -> str:
    del env
    config = home / ".commandcode/config.json"
    provider = _json_string(config, "provider") or _json_string(config, "modelProvider")
    return provider or _model_provider(_json_string(config, "model")) or "unknown"


def _fixed(value: str) -> ProviderProbe:
    def probe(home: Path, env: Mapping[str, str]) -> str:
        del home, env
        return value

    return probe


def _opencode_like(
    folder: str,
    filenames: tuple[str, ...],
    *,
    require_qualified_model: bool = False,
) -> ProviderProbe:
    def probe(home: Path, env: Mapping[str, str]) -> str:
        for filename in filenames:
            reference = _json_string(home / folder / filename, "model")
            provider = _model_provider(reference) if require_qualified_model else _provider_of(reference)
            if provider:
                return provider
        state_home = Path(env.get("XDG_STATE_HOME", home / ".local/state"))
        provider = _json_string(state_home / folder.removeprefix(".config/") / "model.json", "providerID")
        return f"{provider} (최근 사용)" if provider else "unknown"

    return probe


def _yaml_child(path: Path, section: str, child: str) -> str:
    in_section = False
    for line in _read(path).splitlines():
        if re.match(rf"^{re.escape(section)}:\s*$", line):
            in_section = True
            continue
        if in_section and line and not line[0].isspace():
            in_section = False
        match = re.match(rf"^\s+{re.escape(child)}:\s*(.*)$", line) if in_section else None
        if match:
            return match.group(1).strip()
    return ""


def _omp(home: Path, env: Mapping[str, str]) -> str:
    del env
    return _provider_of(_yaml_child(home / ".omp/agent/config.yml", "modelRoles", "default")) or "unknown"


def _pi(home: Path, env: Mapping[str, str]) -> str:
    del env
    provider = _json_string(home / ".pi/agent/settings.json", "defaultProvider")
    if provider:
        return provider
    try:
        auth = json.loads(_read(home / ".pi/agent/auth.json"))
        keys = list(auth) if isinstance(auth, dict) else []
    except json.JSONDecodeError:
        keys = []
    if len(keys) == 1:
        return f"{keys[0]} (인증 기반 추정)"
    if keys:
        return f"자동 선택 (인증 후보: {','.join(keys)})"
    return "unknown"


def _crush(home: Path, env: Mapping[str, str]) -> str:
    data_home = Path(env.get("XDG_DATA_HOME", home / ".local/share"))
    text = _read(data_home / "crush/crush.json").replace("\n", "")
    large = re.search(r'"large"\s*:\s*\{[^}]*\}', text)
    provider = re.search(r'"provider"\s*:\s*"([^"]*)"', large.group(0)) if large else None
    return provider.group(1) if provider else "unknown"


def _hermes_home(home: Path, env: Mapping[str, str] | None = None) -> Path:
    """Hermes 데이터 디렉터리.

    HERMES_HOME이 우선하며, 없으면 Windows는 %LOCALAPPDATA%\\hermes, 그 외는 ~/.hermes임.
    공식 설치 스크립트는 이 디렉터리 아래 hermes-agent에 소스와 venv를 설치함.
    """
    environment = os.environ if env is None else env
    custom = environment.get("HERMES_HOME", "").strip()
    if custom:
        return Path(custom)
    if sys.platform == "win32":
        return Path(environment.get("LOCALAPPDATA", str(home / "AppData/Local"))) / "hermes"
    return home / ".hermes"


def _hermes(home: Path, env: Mapping[str, str]) -> str:
    return _yaml_child(_hermes_home(home, env) / "config.yaml", "model", "provider") or "unknown"


def _openclaw(home: Path, env: Mapping[str, str]) -> str:
    del env
    config = home / ".openclaw/openclaw.json"
    reference = _json_string(config, "primary") or _json_string(config, "model")
    return _provider_of(reference) or "unknown"


AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec("claude", "Claude", ("claude",), ".claude", _claude, update_args=("update",)),
    AgentSpec("codex", "Codex CLI", ("codex",), ".codex", _codex),
    AgentSpec(
        "commandcode", "Command Code", ("command-code", "commandcode", "cmdc"),
        ".commandcode", _commandcode, update_args=("update",),
    ),
    AgentSpec("copilot", "Copilot CLI", ("copilot",), ".copilot", _fixed("github-copilot"), update_args=("update",)),
    AgentSpec("opencode", "OpenCode", ("opencode",), ".config/opencode", _opencode_like(".config/opencode", ("opencode.jsonc", "opencode.json")), update_args=("upgrade",)),
    AgentSpec("kilo", "Kilo", ("kilo",), ".config/kilo", _opencode_like(".config/kilo", ("kilo.jsonc", "kilo.json"), require_qualified_model=True), update_args=("upgrade",)),
    AgentSpec("omp", "OMP", ("omp",), ".omp", _omp, update_args=("update",)),
    AgentSpec("pi", "Pi", ("pi",), ".pi", _pi, update_args=("update", "--self")),
    AgentSpec("antigravity", "Antigravity", ("agy", "antigravity", "gemini"), ".gemini", _fixed("google")),
    AgentSpec("crush", "Crush", ("crush",), ".config/crush", _crush),
    AgentSpec("hermes", "Hermes", ("hermes",), ".hermes", _hermes, update_args=("update", "--yes")),
    AgentSpec("openclaw", "OpenClaw", ("openclaw",), ".openclaw", _openclaw, update_args=("update", "--yes")),
)


def _scratch_directory() -> tempfile.TemporaryDirectory[str]:
    """외부 명령의 작업 디렉터리로 쓸 임시 디렉터리.

    일부 설치 스크립트(예: @charmland/crush의 npm 래퍼)는 현재 작업 디렉터리에
    임시 파일을 남기므로 프로젝트 디렉터리가 오염되지 않도록 격리하고 종료 후 정리함.
    """
    return tempfile.TemporaryDirectory(prefix="agents-status-", ignore_cleanup_errors=True)


def _config_root(spec: AgentSpec, home: Path, env: Mapping[str, str] | None = None) -> Path:
    if spec.key == "hermes":
        return _hermes_home(home, env)
    return home / spec.config_root


_TERMINAL_ESCAPE = re.compile(
    # OSC(창 제목·하이퍼링크 등), CSI(색상·커서 등), 그 외 ESC 시퀀스.
    r"(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|\x1b[ -/]*[@-~]"
)


def _version(executable: Path, args: Sequence[str], cwd: Path | str | None = None) -> str:
    try:
        result = subprocess.run(
            [str(executable), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=60, check=False, cwd=cwd,
            env={**os.environ, "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return "조회 실패"
    if result.returncode:
        return "조회 실패"
    # NO_COLOR를 무시하는 CLI도 있으므로 표시 전에 터미널 제어문자를 제거함.
    output = _TERMINAL_ESCAPE.sub("", result.stdout + "\n" + result.stderr)
    output = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", output)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[0][:80] if lines else "조회 실패"


def scan_agent(
    spec: AgentSpec,
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    path: str | None = None,
    cwd: Path | str | None = None,
) -> AgentStatus:
    environment = os.environ if env is None else env
    base = Path(environment.get("HOME", Path.home())) if home is None else home
    search_path = environment.get("PATH") if path is None else path
    found = None
    for binary in spec.binaries:
        found = shutil.which(binary, path=search_path)
        if found:
            break
    executable = Path(found) if found else None
    config_root = _config_root(spec, base, environment)
    provider = spec.provider_probe(base, environment) if executable or config_root.is_dir() else "-"
    if executable:
        # 버전 조회도 홈 디렉터리(기본) 또는 지정한 디렉터리에서 실행하여 프로젝트 디렉터리 오염을 막음.
        version = _version(executable, spec.version_args, base if cwd is None else cwd)
        return AgentStatus(spec, StatusKind.INSTALLED, executable, version, provider)
    if config_root.is_dir():
        return AgentStatus(spec, StatusKind.CONFIG_ONLY, None, "-", provider)
    return AgentStatus(spec, StatusKind.MISSING, None, "-", "-")


async def scan_agents(specs: Sequence[AgentSpec] = AGENTS) -> list[AgentStatus]:
    with _scratch_directory() as scratch:
        return list(await asyncio.gather(*(asyncio.to_thread(scan_agent, spec, cwd=scratch) for spec in specs)))


def _package_update(executable: Path, packages: tuple[str, ...], *, uninstall: bool = False) -> UpdateCommand | None:
    resolved = executable.resolve()
    for root in resolved.parents:
        if root.parent.name not in ("node_modules", *{p.split("/")[0] for p in packages if p.startswith("@")}):
            continue
        package = _json_string(root / "package.json", "name")
        if package not in packages:
            continue
        modules = root.parent.parent if package.startswith("@") else root.parent
        if modules.name != "node_modules":
            continue
        # npm 전역 prefix를 고정하여 다른 Node 설치에 업데이트되는 문제 방지.
        if modules.parent.name == "lib":
            prefix = modules.parent.parent
            npm = prefix / "bin/npm"
            if npm.is_file():
                argv = (str(npm), "uninstall" if uninstall else "install", "--global", "--prefix", str(prefix), package if uninstall else f"{package}@latest")
                return UpdateCommand(argv, " ".join(argv))
        if modules.parent.name == "global" and modules.parent.parent.name == "install":
            bun = modules.parent.parent.parent / "bin/bun"
            if bun.is_file():
                argv = (str(bun), "remove" if uninstall else "add", "--global", package if uninstall else f"{package}@latest")
                return UpdateCommand(argv, " ".join(argv))
    for directory, flag in (("Cellar", "--formula"), ("Caskroom", "--cask")):
        if directory in resolved.parts:
            index = resolved.parts.index(directory)
            prefix = Path(*resolved.parts[:index])
            brew = prefix / "bin/brew"
            if brew.is_file() and len(resolved.parts) > index + 1:
                package = resolved.parts[index + 1]
                argv = (str(brew), "uninstall" if uninstall else "upgrade", flag, package)
                return UpdateCommand(argv, " ".join(argv))
    return None


PACKAGES = {
    "claude": "@anthropic-ai/claude-code",
    "codex": "@openai/codex",
    "commandcode": "command-code",
    "copilot": "@github/copilot",
    "opencode": "opencode-ai",
    "kilo": "@kilocode/cli",
    "omp": "@oh-my-pi/pi-coding-agent",
    "pi": "@mariozechner/pi-coding-agent",
    "antigravity": "@google/gemini-cli",
    "crush": "@charmland/crush",
    "openclaw": "openclaw",
}


def _codex_packages(home: Path | None = None) -> Path:
    """Codex 공식 설치 스크립트가 standalone·app-server-daemon 패키지를 두는 디렉터리."""
    base = Path.home() if home is None else home
    return Path(os.environ.get("CODEX_HOME", str(base / ".codex"))) / "packages"


def install_command(status: AgentStatus) -> tuple[UpdateCommand | None, str]:
    if status.spec.key == "hermes":
        if sys.platform == "win32":
            # Windows 공식 설치는 install.ps1이며 %LOCALAPPDATA%\hermes에 설치하고 사용자 PATH·HERMES_HOME을 등록함.
            shell = shutil.which("pwsh") or shutil.which("powershell")
            if not shell:
                return None, "Hermes 설치에는 PowerShell이 필요합니다."
            script = "& ([scriptblock]::Create((irm https://hermes-agent.nousresearch.com/install.ps1))) -NonInteractive"
            return UpdateCommand((shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script), script), ""
        if not shutil.which("curl") or not (bash := shutil.which("bash")):
            return None, "Hermes 설치에는 curl과 bash가 필요합니다."
        script = "curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash -s -- --non-interactive"
        return UpdateCommand((bash, "-o", "pipefail", "-c", script), script), ""
    package = PACKAGES.get(status.spec.key)
    if package and (npm := shutil.which("npm")):
        # Windows의 npm PowerShell 래퍼는 bun.cmd가 아닌 bun.exe를 직접 호출함.
        if status.spec.key == "omp" and not shutil.which("bun.exe" if sys.platform == "win32" else "bun"):
            return None, "OMP 설치에는 Bun이 필요합니다. https://bun.com/docs/installation 에 따라 설치하고 Bun 실행 파일 폴더를 PATH에 추가한 뒤 다시 실행하세요."
        argv = (npm, "install", "--global", f"{package}@latest")
        return UpdateCommand(argv, " ".join(argv)), ""
    if package:
        return None, "설치하려면 npm이 필요합니다."
    return None, f"{status.spec.name} 자동 설치를 지원하지 않습니다."


def config_paths(spec: AgentSpec, *, home: Path | None = None) -> tuple[Path, ...]:
    base = Path.home() if home is None else home
    paths = [_config_root(spec, base)]
    if spec.key == "claude":
        paths.append(base / ".claude.json")
    if spec.key in ("opencode", "kilo", "crush"):
        for variable, default in (("XDG_DATA_HOME", ".local/share"), ("XDG_STATE_HOME", ".local/state"), ("XDG_CACHE_HOME", ".cache")):
            paths.append(Path(os.environ.get(variable, str(base / default))) / spec.key)
    return tuple(paths)


_UNIX_MAGIC = (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")
_PE_MAGIC = (b"MZ",)


def _is_native_binary(path: Path, magics: tuple[bytes, ...]) -> bool:
    try:
        with path.open("rb") as stream:
            head = stream.read(4)
    except OSError:
        return False
    return any(head.startswith(magic) for magic in magics)


def uninstall_plan(status: AgentStatus, *, home: Path | None = None) -> tuple[UninstallPlan | None, str]:
    if not status.executable:
        return UninstallPlan(), ""
    executable = status.executable
    package = PACKAGES.get(status.spec.key)
    command = _package_update(executable, (package,) if package else (), uninstall=True)
    if command:
        return UninstallPlan(command), ""
    base = Path.home() if home is None else home
    resolved = executable.resolve()
    if status.spec.key == "claude":
        root = base / ".local/share/claude"
        # Windows 네이티브 설치는 Unix의 심볼릭 링크 대신 bin에 PE 실행 파일을 둠.
        windows_native = (
            executable.parent == base / ".local/bin"
            and executable.name.lower() == "claude.exe"
            and not executable.is_symlink()
            and _is_native_binary(executable, _PE_MAGIC)
        )
        if resolved.is_relative_to(root) or windows_native:
            return UninstallPlan(paths=(executable, root)), ""
    if status.spec.key == "codex" and resolved.is_relative_to(_codex_packages(base) / "standalone"):
        # 공식 설치 스크립트는 ~/.local/bin/codex 링크(macOS는 codex-code-mode-host 링크 추가)와
        # ~/.codex/packages 아래 standalone·app-server-daemon 패키지를 설치함.
        packages = _codex_packages(base)
        paths = [executable]
        host = executable.parent / "codex-code-mode-host"
        if host.is_symlink() and host.resolve().is_relative_to(packages / "standalone"):
            paths.append(host)
        paths.extend(root for root in (packages / "standalone", packages / "app-server-daemon") if root.is_dir())
        return UninstallPlan(paths=tuple(paths)), ""
    if status.spec.key == "opencode" and executable == base / ".opencode/bin/opencode":
        return UninstallPlan(paths=(executable,)), ""
    if status.spec.key == "hermes":
        # 공식 설치 스크립트는 <설치 루트>/hermes-agent에 소스와 venv를 둠(기본 루트는 $HERMES_HOME,
        # root FHS 설치는 /usr/local/lib). Unix는 ~/.local/bin/hermes에 venv 진입점을 exec하는
        # 셸 shim(구버전은 심볼릭 링크)을 두고, Windows는 venv\Scripts\hermes.exe를 PATH에 등록함.
        root = _hermes_checkout(executable, resolved)
        if root:
            paths = (root,) if executable.is_relative_to(root) else (executable, root)
            variables = ("HERMES_HOME",) if sys.platform == "win32" else ()
            return UninstallPlan(paths=paths, variables=variables), ""
    if status.spec.key == "antigravity" and executable.stem.lower() == "agy" and not executable.is_symlink():
        # 공식 설치 스크립트는 Windows에 %LOCALAPPDATA%\agy\bin\agy.exe, 그 외에는 ~/.local/bin/agy
        # (또는 --dir 지정 경로)로 단일 바이너리를 설치하며 agy update는 같은 폴더에 .old 백업을 남김.
        install_root = Path(os.environ.get("LOCALAPPDATA", str(base / "AppData/Local"))) / "agy"
        if "node_modules" not in resolved.parts and _is_native_binary(resolved, _PE_MAGIC + _UNIX_MAGIC):
            if resolved.parent == install_root / "bin":
                return UninstallPlan(paths=(install_root,)), ""
            return UninstallPlan(paths=(executable,)), ""
    if executable.parent == base / ".local/bin" and not executable.is_symlink() and "node_modules" not in resolved.parts:
        # 독립 실행 바이너리만 제거하며 스크립트 기반 설치는 별도 판별이 필요함.
        if _is_native_binary(executable, _UNIX_MAGIC):
            return UninstallPlan(paths=(executable,)), ""
    return None, f"{status.spec.name} 설치 경로({resolved})에서 지원하는 삭제 방식(npm/Bun/Homebrew/독립 바이너리)을 판별하지 못했습니다."


def _hermes_checkout(executable: Path, resolved: Path) -> Path | None:
    """hermes 실행 파일이 가리키는 공식 설치 체크아웃(hermes-agent) 디렉터리."""
    target = resolved
    if executable == resolved and not _is_native_binary(resolved, _PE_MAGIC + _UNIX_MAGIC):
        match = re.search(r'^exec\s+"([^"]+)"', _read(executable), re.MULTILINE)
        if match:
            target = Path(match.group(1)).resolve()
    if target.parent.parent.name != "venv":
        return None
    root = target.parent.parent.parent
    return root if (root / "hermes_cli").is_dir() else None


def _remove_readonly(function: Callable[..., object], path: str, error: BaseException) -> None:
    """Windows에서 git 객체 등 읽기 전용 파일은 삭제가 거부되므로 쓰기 속성을 준 뒤 재시도함."""
    if not isinstance(error, PermissionError):
        raise error
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _remove_paths(paths: Sequence[Path]) -> None:
    for path in paths:
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path, onexc=_remove_readonly)
        else:
            path.unlink(missing_ok=True)


_ENVIRONMENT_KEY = "Environment"


def _within(path: str, roots: Sequence[Path]) -> bool:
    try:
        candidate = Path(os.path.expandvars(path.strip().strip('"')))
    except (OSError, ValueError):
        return False
    return bool(candidate.parts) and any(candidate == root or candidate.is_relative_to(root) for root in roots)


def _broadcast_environment_change() -> None:
    """새로 시작하는 프로그램이 변경된 사용자 환경 변수를 읽도록 알림(Windows 전용)."""
    try:
        import ctypes

        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None)
    except (AttributeError, OSError):
        pass


def _clean_user_environment(removed: Sequence[Path], variables: Sequence[str]) -> list[str]:
    """삭제한 디렉터리를 가리키는 사용자 PATH 항목과 환경 변수를 레지스트리에서 제거함(Windows 전용).

    Hermes·Antigravity 공식 설치 스크립트는 설치 디렉터리를 사용자 PATH에 등록하므로 삭제 후 정리함.
    """
    if sys.platform != "win32" or not removed:
        return []
    import winreg

    roots = [Path(root) for root in removed]
    notes: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _ENVIRONMENT_KEY, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                value, kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                value, kind = "", winreg.REG_EXPAND_SZ
            entries = [entry for entry in str(value).split(";") if entry]
            kept = [entry for entry in entries if not _within(entry, roots)]
            if len(kept) != len(entries):
                winreg.SetValueEx(key, "Path", 0, kind, ";".join(kept))
                notes.extend(f"사용자 PATH에서 제거: {entry}" for entry in entries if entry not in kept)
            for name in variables:
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                if _within(str(value), roots):
                    winreg.DeleteValue(key, name)
                    notes.append(f"사용자 환경 변수 제거: {name}={value}")
    except OSError as error:
        notes.append(f"사용자 환경 변수 정리 실패: {error}")
        return notes
    if notes:
        _broadcast_environment_change()
    return notes


async def run_uninstall(
    plan: UninstallPlan,
    settings: Sequence[Path] = (),
    on_output: Callable[[str], None] | None = None,
) -> CommandResult:
    if plan.command:
        result = await run_update(plan.command, on_output=on_output)
        if result.returncode:
            return result
    removed = (*plan.paths, *settings)
    try:
        await asyncio.to_thread(_remove_paths, removed)
    except OSError as error:
        return CommandResult(1, f"삭제 실패: {error}")
    notes = await asyncio.to_thread(_clean_user_environment, removed, plan.variables)
    if on_output is not None:
        for note in notes:
            on_output(note)
    return CommandResult(0, "삭제를 완료했습니다.")


def update_command(status: AgentStatus, *, home: Path | None = None) -> tuple[UpdateCommand | None, str]:
    if not status.executable:
        return None, "실행 파일이 없어 업데이트할 수 없습니다."
    spec = status.spec
    if spec.key == "codex" and status.executable.resolve().is_relative_to(_codex_packages(home) / "standalone"):
        # 공식 설치 스크립트 설치는 codex update가 같은 스크립트를 비대화형으로 다시 실행하여 갱신함.
        argv = (str(status.executable), "update")
        return UpdateCommand(argv, "codex update"), ""
    if spec.update_args:
        argv = (str(status.executable), *spec.update_args)
        return UpdateCommand(argv, " ".join((status.executable.name, *spec.update_args))), ""
    if spec.key == "antigravity" and status.executable.stem.lower() == "agy":
        # Windows에서는 agy.EXE처럼 확장자가 붙으므로 stem으로 비교함.
        argv = (str(status.executable), "update")
        return UpdateCommand(argv, "agy update"), ""
    packages = {
        "crush": ("@charmland/crush",),
        "antigravity": ("@google/gemini-cli",),
    }
    if spec.key in packages:
        command = _package_update(status.executable, packages[spec.key])
        if command:
            return command, ""
        return None, f"{spec.name} 설치 경로({status.executable.resolve()})에서 지원하는 업데이트 방식(npm/Bun/Homebrew)을 판별하지 못했습니다."
    if spec.key == "codex":
        resolved = status.executable.resolve()
        if "node_modules" in resolved.parts and (npm := shutil.which("npm")):
            argv = (npm, "install", "--global", "@openai/codex@latest")
            return UpdateCommand(argv, "npm install --global @openai/codex@latest"), ""
        if (brew := shutil.which("brew")) and "Caskroom" in resolved.parts:
            argv = (brew, "upgrade", "--cask", "codex")
            return UpdateCommand(argv, "brew upgrade --cask codex"), ""
        if brew and "Cellar" in resolved.parts:
            argv = (brew, "upgrade", "codex")
            return UpdateCommand(argv, "brew upgrade codex"), ""
        return None, "Codex 설치 방식(npm/Homebrew)을 안전하게 판별할 수 없습니다."
    return None, "이 도구는 설치 방식별 업데이트가 달라 자동 업데이트를 지원하지 않습니다."


async def run_update(
    command: UpdateCommand,
    timeout: float = 600,
    on_output: Callable[[str], None] | None = None,
) -> CommandResult:
    process: asyncio.subprocess.Process | None = None
    scratch = _scratch_directory()
    try:
        process = await asyncio.create_subprocess_exec(
            *command.argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=scratch.name,
            env={**os.environ, "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8"},
        )

        async def consume_output() -> str:
            assert process is not None and process.stdout is not None
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            output_parts: list[str] = []
            pending = ""
            while chunk := await process.stdout.read(1024):
                text = decoder.decode(chunk)
                output_parts.append(text)
                pending += text.replace("\r", "\n")
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if on_output is not None and line:
                        on_output(line)
            final = decoder.decode(b"", final=True)
            output_parts.append(final)
            pending += final
            if on_output is not None and pending:
                on_output(pending)
            await process.wait()
            return "".join(output_parts).strip()

        output = await asyncio.wait_for(consume_output(), timeout=timeout)
    except asyncio.TimeoutError:
        if process is not None:
            process.kill()
            await process.wait()
        message = f"명령 실행 제한 시간({timeout:g}초)을 초과했습니다."
        if on_output is not None:
            on_output(message)
        return CommandResult(124, message)
    except OSError as error:
        message = f"명령을 실행하지 못했습니다: {error}"
        if on_output is not None:
            on_output(message)
        return CommandResult(127, message)
    finally:
        scratch.cleanup()
    return CommandResult(process.returncode or 0, output)
