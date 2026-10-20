from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping, Sequence

from .core import AgentStatus, StatusKind
from .instructions import SyncResult, _atomic_write


def _add_missing(target: dict, defaults: dict) -> bool:
    """기존 값을 보존하며 누락된 설정만 추가함."""
    changed = False
    for key, value in defaults.items():
        if key not in target:
            target[key] = value
            changed = True
        elif isinstance(value, dict) and isinstance(target[key], dict):
            changed = _add_missing(target[key], value) or changed
    return changed


def apply_defaults(
    statuses: Sequence[AgentStatus], *, home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> list[SyncResult]:
    """설치된 에이전트에 기본 설정을 추가하고 파일별 결과를 반환함."""
    home = Path.home() if home is None else home
    env = os.environ if env is None else env
    config = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    data = Path(env.get("XDG_DATA_HOME") or home / ".local/share")
    crush_config = Path(env.get("CRUSH_GLOBAL_CONFIG") or config / "crush")
    defaults = {
        "commandcode": (home / ".commandcode/config.json", {"tasteLearning": False}),
        "crush": (crush_config / "crush.json", {
            "options": {"data_directory": str(data / "crush/project-data")},
        }),
        "opencode": (config / "opencode/cli.json", {
            "$schema": "https://opencode.ai/v2/cli.json", "terminal": {"copy": "select"},
        }),
    }
    results = []
    for status in statuses:
        if status.spec.key not in defaults:
            continue
        path, settings = defaults[status.spec.key]
        if status.status != StatusKind.INSTALLED:
            results.append(SyncResult(status.spec.name, path, "skip", "미설치"))
            continue
        try:
            try:
                content = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                content = {}
            if not isinstance(content, dict):
                raise ValueError("설정 파일의 최상위 값이 JSON 객체가 아님")
            if not _add_missing(content, settings):
                results.append(SyncResult(status.spec.name, path, "skip", "기존 설정 보존"))
                continue
            _atomic_write(path, (json.dumps(content, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            results.append(SyncResult(status.spec.name, path, "write"))
        except (OSError, UnicodeError, ValueError) as error:
            results.append(SyncResult(status.spec.name, path, "error", str(error)))
    return results
