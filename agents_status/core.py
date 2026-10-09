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
import subprocess
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

    @property
    def display(self) -> str:
        if self.command:
            return self.command.display
        return "삭제: " + ", ".join(map(str, self.paths)) if self.paths else "설치된 프로그램 없음"


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


def _hermes(home: Path, env: Mapping[str, str]) -> str:
    del env
    return _yaml_child(home / ".hermes/config.yaml", "model", "provider") or "unknown"


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


def _version(executable: Path, args: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            [str(executable), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=60, check=False,
            env={**os.environ, "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return "조회 실패"
    lines = (result.stdout + "\n" + result.stderr).strip().splitlines()
    return lines[0][:80] if lines else "조회 실패"


def scan_agent(
    spec: AgentSpec,
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    path: str | None = None,
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
    provider = spec.provider_probe(base, environment) if executable or (base / spec.config_root).is_dir() else "-"
    if executable:
        return AgentStatus(spec, StatusKind.INSTALLED, executable, _version(executable, spec.version_args), provider)
    if (base / spec.config_root).is_dir():
        return AgentStatus(spec, StatusKind.CONFIG_ONLY, None, "-", provider)
    return AgentStatus(spec, StatusKind.MISSING, None, "-", "-")


async def scan_agents(specs: Sequence[AgentSpec] = AGENTS) -> list[AgentStatus]:
    return list(await asyncio.gather(*(asyncio.to_thread(scan_agent, spec) for spec in specs)))


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


def install_command(status: AgentStatus) -> tuple[UpdateCommand | None, str]:
    if status.spec.key == "hermes":
        if not shutil.which("curl") or not (bash := shutil.which("bash")):
            return None, "Hermes 설치에는 curl과 bash가 필요합니다."
        script = "curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash -s -- --non-interactive"
        return UpdateCommand((bash, "-o", "pipefail", "-c", script), script), ""
    package = PACKAGES.get(status.spec.key)
    if package and (npm := shutil.which("npm")):
        argv = (npm, "install", "--global", f"{package}@latest")
        return UpdateCommand(argv, " ".join(argv)), ""
    if package:
        return None, "설치하려면 npm이 필요합니다."
    return None, f"{status.spec.name} 자동 설치를 지원하지 않습니다."


def config_paths(spec: AgentSpec, *, home: Path | None = None) -> tuple[Path, ...]:
    base = Path.home() if home is None else home
    paths = [base / spec.config_root]
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
    if status.spec.key == "claude" and resolved.is_relative_to(base / ".local/share/claude"):
        return UninstallPlan(paths=(executable, base / ".local/share/claude")), ""
    if status.spec.key == "opencode" and executable == base / ".opencode/bin/opencode":
        return UninstallPlan(paths=(executable,)), ""
    if status.spec.key == "hermes":
        root = base / ".hermes/hermes-agent"
        if resolved.is_relative_to(root) and (root / "hermes_cli").is_dir():
            return UninstallPlan(paths=(executable, root)), ""
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


def _remove_paths(paths: Sequence[Path]) -> None:
    for path in paths:
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


async def run_uninstall(
    plan: UninstallPlan,
    settings: Sequence[Path] = (),
    on_output: Callable[[str], None] | None = None,
) -> CommandResult:
    if plan.command:
        result = await run_update(plan.command, on_output=on_output)
        if result.returncode:
            return result
    try:
        await asyncio.to_thread(_remove_paths, (*plan.paths, *settings))
    except OSError as error:
        return CommandResult(1, f"삭제 실패: {error}")
    return CommandResult(0, "삭제를 완료했습니다.")


def update_command(status: AgentStatus) -> tuple[UpdateCommand | None, str]:
    if not status.executable:
        return None, "실행 파일이 없어 업데이트할 수 없습니다."
    spec = status.spec
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
    try:
        process = await asyncio.create_subprocess_exec(
            *command.argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
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
    return CommandResult(process.returncode or 0, output)
