from __future__ import annotations

import base64
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import webbrowser


RELEASE_PAGES = {
    "antigravity": "https://antigravity.google/changelog",
    "claude": "https://github.com/anthropics/claude-code/releases",
    "codex": "https://github.com/openai/codex/releases",
    "commandcode": "https://commandcode.ai/changelog",
    "copilot": "https://github.com/github/copilot-cli/releases",
    "crush": "https://github.com/charmbracelet/crush/releases",
    "hermes": "https://github.com/NousResearch/hermes-agent/releases",
    "kilo": "https://github.com/Kilo-Org/kilocode/releases",
    "kimi": "https://github.com/MoonshotAI/kimi-code/releases",
    "omp": "https://github.com/can1357/oh-my-pi/releases",
    "openclaw": "https://github.com/openclaw/openclaw/releases",
    "opencode": "https://github.com/anomalyco/opencode/releases",
    "pi": "https://github.com/earendil-works/pi/releases",
    "pi-bolt": "https://github.com/opensec-git/Pi-Bolt/releases",
    "qoder": "https://docs.qoder.com/release-notes/qoder-cli",
    "qwen": "https://github.com/QwenLM/qwen-code/releases",
}


def open_release_page(key: str) -> None:
    """기본 브라우저로 릴리스 페이지를 엶. WSL에서는 Windows 브라우저를 사용함."""
    url = RELEASE_PAGES[key]
    if sys.platform == "linux" and "microsoft" in platform.release().lower():
        powershell = shutil.which("powershell.exe")
        fallback = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
        if not powershell and fallback.is_file():
            powershell = str(fallback)
        if powershell:
            script = "$ErrorActionPreference = 'Stop'; Start-Process '" + url.replace("'", "''") + "'"
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            argv = (powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded)
        elif viewer := shutil.which("wslview"):
            argv = (viewer, url)
        else:
            raise OSError("Windows 브라우저 연동을 위한 powershell.exe 또는 wslview를 찾을 수 없습니다.")
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=False)
        if result.returncode:
            raise OSError("Windows 기본 브라우저 실행에 실패했습니다. WSL 실행 연동과 기본 브라우저 설정을 확인해 주세요.")
    elif not webbrowser.open(url, new=2):
        raise OSError("기본 브라우저를 실행하지 못했습니다.")
