from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
import tempfile
from typing import Mapping, Sequence

from .core import AGENTS, AgentSpec, _config_root, _which


MARK_BEGIN = "<!-- GLOBAL_INSTRUCTIONS:BEGIN -->"
MARK_END = "<!-- GLOBAL_INSTRUCTIONS:END -->"
LEGACY_HEADER = "# Global Instructions (ver."
DESTINATIONS = {
    "claude": "CLAUDE.md",
    "codex": "AGENTS.md",
    "commandcode": "AGENTS.md",
    "copilot": "copilot-instructions.md",
    "opencode": "AGENTS.md",
    "kilo": "AGENTS.md",
    "omp": "agent/AGENTS.md",
    "pi": "agent/AGENTS.md",
    "pi-bolt": "agent/AGENTS.md",
    "antigravity": "GEMINI.md",
    "crush": "CRUSH.md",
    "hermes": "SOUL.md",
    "openclaw": "workspace/AGENTS.md",
}


@dataclass(frozen=True, slots=True)
class SyncResult:
    name: str
    destination: Path
    outcome: str
    detail: str = ""


def default_instructions_path() -> Path:
    """소스 실행은 프로젝트 원본, 패키지 설치는 포함된 원본을 사용함."""
    package = Path(__file__).resolve().parent
    bundled = package / "GLOBAL_INSTRUCTIONS.md"
    return bundled if bundled.is_file() else package.parent / "GLOBAL_INSTRUCTIONS.md"


def _merge_instructions(existing: str, source: str, default: str = "") -> str:
    newline = "\r\n" if "\r\n" in existing else "\n"
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    block = MARK_BEGIN + "\n" + source.rstrip("\n") + "\n" + MARK_END + "\n"
    block = block.replace("\n", newline)
    lines = existing.splitlines(keepends=True)
    begins = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == MARK_BEGIN]
    ends = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == MARK_END]
    if begins or ends:
        if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
            raise ValueError("글로벌 지침 마커가 불완전하거나 중복됨")
        return "".join(lines[:begins[0]]) + block + "".join(lines[ends[0] + 1:])
    if existing.startswith(LEGACY_HEADER):
        existing = default.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)
    if not existing:
        return block
    return existing + ("" if existing.endswith("\n") else newline) + newline + block


def _atomic_write(destination: Path, content: bytes) -> None:
    """심볼릭 링크와 기존 권한을 보존하고 같은 파일시스템에서 원자적으로 교체함."""
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(destination.stat().st_mode) if destination.exists() else None
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        if mode is not None:
            temporary.chmod(mode)
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def sync_instructions(
    source: Path,
    specs: Sequence[AgentSpec] = AGENTS,
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> list[SyncResult]:
    """원본을 한 번 읽고 대상별 동기화 결과를 반환함. 원본 오류는 쓰기 전에 발생함."""
    data = source.read_bytes()
    content = data.decode("utf-8-sig")
    if not content.strip():
        raise ValueError("지침 원본이 비어 있음")
    if MARK_BEGIN in content or MARK_END in content:
        raise ValueError("지침 원본에 동기화 마커를 포함할 수 없음")
    base = Path.home() if home is None else home
    environment = os.environ if env is None else env
    results: list[SyncResult] = []
    for spec in specs:
        root = _config_root(spec, base, environment)
        if spec.key == "crush":
            root = Path(environment.get("CRUSH_GLOBAL_CONFIG") or str(
                Path(environment.get("XDG_CONFIG_HOME") or str(base / ".config")) / "crush"
            ))
        destination = root / DESTINATIONS[spec.key]
        try:
            available = root.is_dir()
            if not available and spec.key == "crush":
                data_root = Path(environment.get("CRUSH_GLOBAL_DATA") or str(
                    Path(environment.get("XDG_DATA_HOME") or str(base / ".local/share")) / "crush"
                ))
                available = data_root.is_dir() or bool(_which("crush", path=environment.get("PATH", "")))
            if not available:
                results.append(SyncResult(spec.name, destination, "skip", f"설정 디렉터리 없음: {root}"))
                continue
            output = data
            mode = "copy"
            if spec.key in ("hermes", "openclaw"):
                mode = "merge"
                existing = destination.read_bytes().decode("utf-8-sig") if destination.exists() else ""
                default = ""
                if spec.key == "hermes" and existing.startswith(LEGACY_HEADER) and MARK_BEGIN not in existing:
                    default_path = root / "hermes-agent/SOUL.md"
                    if default_path.is_file():
                        default = default_path.read_bytes().decode("utf-8-sig")
                output = _merge_instructions(existing, content, default).encode("utf-8")
            _atomic_write(destination, output)
            results.append(SyncResult(spec.name, destination, mode))
        except (OSError, UnicodeError, ValueError) as error:
            results.append(SyncResult(spec.name, destination, "error", str(error)))
    return results
