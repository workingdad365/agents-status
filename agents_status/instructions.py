from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Mapping, Sequence

from .core import AGENTS, AgentSpec, _config_root, _which


MARK_BEGIN = "<!-- GLOBAL_INSTRUCTIONS:BEGIN -->"
MARK_END = "<!-- GLOBAL_INSTRUCTIONS:END -->"
LEGACY_HEADER = "# Global Instructions (ver."
# 기준 해시 기록 도입 전 배포된 지침 원문의 해시 목록임.
LEGACY_BUNDLED_DIGESTS = frozenset({
    "a87e754a051dba9b0317076beae3c8ec51a54ee9cdd4f4bd1d5dd196d92c69e1",
    "0058c7b76bf259ef5467c82bf7de67b4a3ed04b1a0d34b691f5ca9e1b0e3e46d",
    "97c956396bc0368fbabb020b3289b12db025d4ac397193403acb4bff3126c4f9",
    "278970247422b84c75327cee619a0bbe1bff33591ad101206b74210c8a4a08ca",
    "6658a6129ea0f9e18c31490c670174cb03b2ed105bbabe609c067d3d852c23c7",
    "5be02201eb9d09cd0e3548213987aadf10426958f0284c21354758420d3ff887",
})
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
    "kimi": "AGENTS.md",
    "qwen": "QWEN.md",
    "qoder": "AGENTS.md",
}


@dataclass(frozen=True, slots=True)
class SyncResult:
    name: str
    destination: Path
    outcome: str
    detail: str = ""


def user_instructions_path() -> Path:
    """앱 업데이트와 독립적인 사용자 편집본 위치를 반환함."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "agents-status" / "GLOBAL_INSTRUCTIONS.md"


def default_instructions_path() -> Path:
    """실제로 수정한 편집본만 우선하고 미수정 사본은 현재 배포 원본을 사용함."""
    custom = user_instructions_path()
    bundled = bundled_instructions_path()
    if not custom.is_file():
        return bundled
    try:
        digest = instructions_fingerprint(custom.read_bytes())
        if digest == instructions_fingerprint(bundled.read_bytes()) or digest in LEGACY_BUNDLED_DIGESTS:
            return bundled
        baseline = custom.with_suffix(".base.sha256").read_text(encoding="ascii").strip()
        if digest == baseline:
            return bundled
    except (OSError, UnicodeError):
        # 비교 정보가 없거나 읽을 수 없으면 기존 편집본을 우선함.
        pass
    return custom


def instructions_fingerprint(data: bytes) -> str:
    """BOM과 줄바꿈 차이를 제외한 내용 해시를 반환함."""
    normalized = data.removeprefix(b"\xef\xbb\xbf").replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(normalized).hexdigest()


def bundled_instructions_path() -> Path:
    """사용자 편집본과 독립적으로 배포 원본 위치를 반환함."""
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


def _read_instructions(source: Path) -> tuple[bytes, str]:
    """지침 원본을 읽고 쓰기 전에 내용을 검증함."""
    data = source.read_bytes()
    content = data.decode("utf-8-sig")
    if not content.strip():
        raise ValueError("지침 원본이 비어 있음")
    if MARK_BEGIN in content or MARK_END in content:
        raise ValueError("지침 원본에 동기화 마커를 포함할 수 없음")
    return data, content


def restore_bundled_instructions() -> Path:
    """배포 지침의 기준 해시를 기록하고 사용자 편집본을 덮어씀."""
    data, _ = _read_instructions(bundled_instructions_path())
    custom = user_instructions_path()
    destination = custom.resolve()
    _atomic_write(custom.with_suffix(".base.sha256"), (instructions_fingerprint(data) + "\n").encode("ascii"))
    _atomic_write(destination, data)
    return destination


def sync_instructions(
    source: Path,
    specs: Sequence[AgentSpec] = AGENTS,
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> list[SyncResult]:
    """원본을 한 번 읽고 대상별 동기화 결과를 반환함. 원본 오류는 쓰기 전에 발생함."""
    data, content = _read_instructions(source)
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
