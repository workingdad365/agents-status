import asyncio
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from rich.text import Text
from textual.widgets import Button, DataTable, RichLog

from agents_status.app import AgentsStatusApp, ConfirmDelete, ConfirmQuit, ConfirmUpdate, platform_name
from agents_status.core import (
    AGENTS, AgentStatus, CommandResult, StatusKind, UninstallPlan, UpdateCommand,
    config_paths, install_command, run_uninstall, run_update, scan_agent, uninstall_plan, update_command,
)
from agents_status.core import _clean_user_environment, _version


SPECS = {spec.key: spec for spec in AGENTS}


def symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as error:
        if sys.platform == "win32" and error.winerror == 1314:
            raise unittest.SkipTest("Windows 심볼릭 링크 생성 권한이 없음") from error
        raise


class LauncherTests(unittest.TestCase):
    def test_powershell_forwards_project_arguments_and_exit_code(self):
        shells = [shell for name in ("powershell", "pwsh") if (shell := shutil.which(name))]
        if not shells:
            self.skipTest("PowerShell이 설치되지 않음")
        with tempfile.TemporaryDirectory(prefix="agents status ") as directory:
            root = Path(directory)
            launcher = root / "agents_status.ps1"
            shutil.copyfile(Path(__file__).with_name("agents_status.ps1"), launcher)
            bootstrap = root / "check.ps1"
            bootstrap.write_text(
                "function uv {\n"
                "    ConvertTo-Json -Compress -InputObject @($args)\n"
                "    $global:LASTEXITCODE = 7\n"
                "}\n"
                "& (Join-Path $PSScriptRoot 'agents_status.ps1') '--example' 'value with spaces'\n"
                "exit $LASTEXITCODE\n",
                encoding="utf-8",
            )
            for shell in shells:
                with self.subTest(shell=shell):
                    result = subprocess.run(
                        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(bootstrap)],
                        cwd=root.parent, capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(result.returncode, 7, result.stderr)
                    self.assertEqual(json.loads(result.stdout), [
                        "run", "--project", str(root), "agents-status", "--example", "value with spaces",
                    ])


class ProviderTests(unittest.TestCase):
    def status(self, key, files, extra_env=None):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for name, data in files.items():
                path = home / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            env = {"HOME": directory, "PATH": "", **(extra_env or {})}
            return scan_agent(SPECS[key], home=home, env=env, path="")

    def test_commandcode_provider_precedence_and_model_boundary(self):
        cases = [
            ({"provider": "codex", "modelProvider": "openrouter", "model": "gpt-6"}, "codex"),
            ({"modelProvider": "openrouter", "model": "gpt-6"}, "openrouter"),
            ({"model": "openrouter/openai/gpt-6"}, "openrouter"),
            ({"model": "gpt-6"}, "unknown"),
            ({"model": "/gpt-6"}, "unknown"),
            ({"model": "openrouter/"}, "unknown"),
        ]
        for config, expected in cases:
            with self.subTest(config=config):
                status = self.status("commandcode", {".commandcode/config.json": config})
                self.assertEqual(status.provider, expected)

    def test_kilo_config_and_saved_provider(self):
        status = self.status("kilo", {
            ".config/kilo/kilo.jsonc": {"model": "github-copilot/gpt-6"},
            ".config/kilo/kilo.json": {"model": "anthropic/claude"},
            ".local/state/kilo/model.json": {"recent": [{"providerID": "openrouter"}]},
        })
        self.assertEqual(status.provider, "github-copilot")
        status = self.status("kilo", {
            ".config/kilo/kilo.jsonc": {},
            ".local/state/kilo/model.json": {"recent": [{"providerID": "openrouter"}]},
        })
        self.assertEqual(status.provider, "openrouter (최근 사용)")

    def test_kilo_model_boundary_and_xdg_state(self):
        cases = [
            ({"model": "openrouter/openai/gpt-6"}, "openrouter"),
            ({"model": "gpt-6"}, "unknown"),
            ({"model": "/gpt-6"}, "unknown"),
            ({"model": "openrouter/"}, "unknown"),
            ({}, "unknown"),
        ]
        for filename in ("kilo.json", "kilo.jsonc"):
            for config, expected in cases:
                with self.subTest(filename=filename, config=config):
                    status = self.status("kilo", {f".config/kilo/{filename}": config})
                    self.assertEqual(status.provider, expected)
        with tempfile.TemporaryDirectory() as state_home:
            status = self.status("kilo", {
                ".config/kilo/kilo.jsonc": {},
                f"{state_home}/kilo/model.json": {"recent": [{"providerID": "github-copilot"}]},
            }, {"XDG_STATE_HOME": state_home})
            self.assertEqual(status.provider, "github-copilot (최근 사용)")

    def test_pi_provider_candidates(self):
        cases = [
            ({"defaultProvider": "anthropic"}, ["openrouter"], "anthropic"),
            ({}, ["openrouter"], "openrouter (인증 기반 추정)"),
            ({}, ["openrouter", "anthropic"], "자동 선택 (인증 후보: openrouter,anthropic)"),
            ({}, [], "unknown"),
        ]
        for settings, providers, expected in cases:
            with self.subTest(settings=settings, providers=providers):
                status = self.status("pi", {
                    ".pi/agent/settings.json": settings,
                    ".pi/agent/auth.json": {provider: {} for provider in providers},
                })
                self.assertEqual(status.provider, expected)

    def test_hermes_provider_follows_hermes_home(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            data = home / "AppData/Local/hermes"
            data.mkdir(parents=True)
            (data / "config.yaml").write_text("model:\n  provider: azure-foundry\n  default: gpt-6\n", encoding="utf-8")
            env = {"HOME": directory, "PATH": "", "HERMES_HOME": str(data)}
            status = scan_agent(SPECS["hermes"], home=home, env=env, path="")
            self.assertEqual(status.status, StatusKind.CONFIG_ONLY)
            self.assertEqual(status.provider, "azure-foundry")
            with patch("agents_status.core.sys.platform", "win32"):
                env = {"HOME": directory, "PATH": "", "LOCALAPPDATA": str(home / "AppData/Local")}
                status = scan_agent(SPECS["hermes"], home=home, env=env, path="")
            self.assertEqual(status.provider, "azure-foundry")
            with patch("agents_status.core.sys.platform", "linux"):
                status = scan_agent(SPECS["hermes"], home=home, env={"HOME": directory, "PATH": ""}, path="")
            self.assertEqual(status.status, StatusKind.MISSING)


class DetectionTests(unittest.TestCase):
    def test_pi_bolt_windows_native_detection_and_removal_preserve_shared_pi(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            executable = home / ".pi-bolt/pi-bolt-win32-x64-jit/pi-bolt.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"MZ\x00\x00")
            settings = home / ".pi/agent/settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text('{"defaultProvider": "openrouter"}', encoding="utf-8")
            env = {"PATH": "", "PIBOLT_VARIANT": "x64-jit"}
            with patch("agents_status.core.sys.platform", "win32"), patch.dict(os.environ, env, clear=True), patch("agents_status.core._version", return_value="0.8.5"):
                status = scan_agent(SPECS["pi-bolt"], home=home, env=env)
                self.assertEqual(status.executable, executable)
                self.assertEqual(status.provider, "openrouter")
                plan, reason = uninstall_plan(status, home=home)
            self.assertEqual(reason, "")
            self.assertEqual(plan.paths, (executable.parent,))
            self.assertEqual(config_paths(status.spec, home=home), (home / ".pi",))
            command, reason = update_command(status)
            self.assertEqual(command.argv, (str(executable), "update"))

    def test_pi_bolt_native_symlink_removal_and_npm_install(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            binary = home / ".pi-bolt/pi-bolt-linux-x64/pi"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"\x7fELF")
            link = home / ".local/bin/pi-bolt"
            link.parent.mkdir(parents=True)
            symlink_or_skip(link, binary)
            status = AgentStatus(SPECS["pi-bolt"], StatusKind.INSTALLED, link, "0.8.5", "unknown")
            with patch.dict(os.environ, {}, clear=True), patch("agents_status.core._which", return_value=None):
                plan, reason = uninstall_plan(status, home=home)
            self.assertEqual(reason, "")
            self.assertEqual(plan.paths, (link, binary.parent))
            with patch("agents_status.core._which", return_value="npm"):
                command, reason = install_command(status)
            self.assertEqual(command.argv, ("npm", "install", "--global", "--allow-scripts=pi-bolt", "pi-bolt@latest"))

    def test_wsl_ignores_windows_agents_and_preserves_config_status(self):
        for spec in AGENTS:
            with self.subTest(agent=spec.key), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                env = {"PATH": "/mnt/c/nvm4w/nodejs:/mnt/d/tools"}
                with patch("agents_status.core.sys.platform", "linux"), patch("agents_status.core.platform.release", return_value="6.6-microsoft-standard-WSL2"), patch("agents_status.core._version") as probe:
                    status = scan_agent(spec, home=home, env=env)
                    self.assertEqual(status.status, StatusKind.MISSING)
                    self.assertIsNone(status.executable)
                    (home / spec.config_root).mkdir(parents=True)
                    status = scan_agent(spec, home=home, env=env)
                    self.assertEqual(status.status, StatusKind.CONFIG_ONLY)
                    probe.assert_not_called()

    def test_wsl_finds_linux_agent_after_windows_path_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            links = home / "links"
            native = home / "bin"
            links.mkdir()
            native.mkdir()
            windows = home / "windows"
            windows.mkdir()
            windows_executable = windows / "claude"
            windows_executable.write_text("#!/bin/sh\nexit 0\n")
            windows_executable.chmod(0o755)
            symlink_or_skip(links / "claude", windows_executable)
            executable = native / "claude"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            env = {"PATH": os.pathsep.join((str(windows), str(links), str(native)))}
            with patch("agents_status.core.sys.platform", "linux"), patch("agents_status.core.platform.release", return_value="microsoft"), patch("agents_status.core._read", return_value=f"C: {windows} drvfs rw 0 0\n"), patch("agents_status.core._version", return_value="1"):
                status = scan_agent(SPECS["claude"], home=home, env=env)
            self.assertEqual(status.status, StatusKind.INSTALLED)
            self.assertEqual(status.executable, executable)

    def test_wsl_install_does_not_use_windows_npm(self):
        status = AgentStatus(SPECS["claude"], StatusKind.MISSING, None, "-", "-")
        with patch("agents_status.core.sys.platform", "linux"), patch("agents_status.core.platform.release", return_value="microsoft"), patch.dict(os.environ, {"PATH": "/mnt/c/nvm4w/nodejs"}):
            command, reason = install_command(status)
        self.assertIsNone(command)
        self.assertIn("npm", reason)

    def test_windows_drive_detection_supports_custom_mounts(self):
        from agents_status.core import _windows_drive_path
        mounts = "C: /windows/drive\\040c 9p rw,aname=drvfs;path=C: 0 0\nD: /drives/d drvfs rw 0 0\n"
        with patch("agents_status.core._read", return_value=mounts):
            self.assertTrue(_windows_drive_path(Path("/windows/drive c/tools/claude")))
            self.assertTrue(_windows_drive_path(Path("/drives/d/tools/claude")))
            self.assertFalse(_windows_drive_path(Path("/drives/data/claude")))
            self.assertFalse(_windows_drive_path(Path("/usr/bin/claude")))

    def test_non_wsl_keeps_normal_command_search(self):
        from agents_status.core import _which
        for system, release in (("win32", "microsoft"), ("linux", "6.6-generic"), ("darwin", "24")):
            with self.subTest(system=system), patch("agents_status.core.sys.platform", system), patch("agents_status.core.platform.release", return_value=release), patch("shutil.which", return_value="/mnt/c/tools/claude") as which:
                self.assertEqual(_which("claude", path="/mnt/c/tools"), "/mnt/c/tools/claude")
                which.assert_called_once_with("claude", path="/mnt/c/tools")

    def test_windows_hermes_is_found_without_inherited_path(self):
        for custom_home in (False, True):
            for relative in ("bin/hermes.exe", "bin/hermes.cmd", "hermes-agent/venv/Scripts/hermes.exe"):
                with self.subTest(custom_home=custom_home, relative=relative), tempfile.TemporaryDirectory() as directory:
                    home = Path(directory)
                    data = home / ("custom hermes" if custom_home else "AppData/Local/hermes")
                    executable = data / relative
                    executable.parent.mkdir(parents=True)
                    executable.touch()
                    (data / "config.yaml").write_text("model:\n  provider: openai\n", encoding="utf-8")
                    env = {"PATH": "", "LOCALAPPDATA": str(home / "AppData/Local")}
                    if custom_home:
                        env["HERMES_HOME"] = str(data)
                    with patch("agents_status.core.sys.platform", "win32"), patch("agents_status.core._version", return_value="Hermes Agent v1.0") as probe:
                        status = scan_agent(SPECS["hermes"], home=home, env=env, path="")
                    self.assertEqual(status.status, StatusKind.INSTALLED)
                    self.assertEqual(status.executable, executable)
                    self.assertEqual(status.version, "Hermes Agent v1.0")
                    self.assertEqual(status.provider, "openai")
                    probe.assert_called_once_with(executable, ("--version",), home)
                    command, reason = update_command(status)
                    self.assertEqual(reason, "")
                    self.assertEqual(command.argv, (str(executable), "update", "--yes"))

    def test_windows_omp_is_found_in_official_install_dir(self):
        for custom_dir in (False, True):
            with self.subTest(custom_dir=custom_dir), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                install_dir = home / ("custom omp" if custom_dir else "AppData/Local/omp")
                install_dir.mkdir(parents=True)
                executable = install_dir / "omp.exe"
                executable.write_bytes(b"MZ\x90\x00")
                (home / ".omp/agent").mkdir(parents=True)
                (home / ".omp/agent/config.yml").write_text("modelRoles:\n  default: openai/gpt-5\n", encoding="utf-8")
                env = {"PATH": "", "LOCALAPPDATA": str(home / "AppData/Local")}
                if custom_dir:
                    env["PI_INSTALL_DIR"] = str(install_dir)
                with patch("agents_status.core.sys.platform", "win32"), patch("agents_status.core._version", return_value="18.8.6") as probe:
                    status = scan_agent(SPECS["omp"], home=home, env=env, path="")
                self.assertEqual(status.status, StatusKind.INSTALLED)
                self.assertEqual(status.executable, executable)
                self.assertEqual(status.version, "18.8.6")
                self.assertEqual(status.provider, "openai")
                probe.assert_called_once_with(executable, ("--version",), home)
                with patch("agents_status.core.sys.platform", "linux"):
                    status = scan_agent(SPECS["omp"], home=home, env=env, path="")
                self.assertEqual(status.status, StatusKind.CONFIG_ONLY)

    def test_windows_hermes_prefers_path_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fallback = home / "AppData/Local/hermes/bin/hermes.exe"
            fallback.parent.mkdir(parents=True)
            fallback.touch()
            selected = home / "other/hermes.exe"
            with patch("agents_status.core.sys.platform", "win32"), patch("shutil.which", return_value=str(selected)), patch("agents_status.core._version", return_value="1"):
                status = scan_agent(SPECS["hermes"], home=home, env={})
            self.assertEqual(status.executable, selected)

    def test_windows_hermes_config_without_launcher_is_not_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            data = home / "AppData/Local/hermes"
            env = {"LOCALAPPDATA": str(home / "AppData/Local"), "PATH": ""}
            with patch("agents_status.core.sys.platform", "win32"), patch("agents_status.core._version") as probe:
                status = scan_agent(SPECS["hermes"], home=home, env=env, path="")
                self.assertEqual(status.status, StatusKind.MISSING)
                # 설치 소스나 이름이 같은 디렉터리만 남아 있어도 실행 파일로 보지 않음.
                (data / "hermes-agent/hermes_cli").mkdir(parents=True)
                (data / "bin/hermes.exe").mkdir(parents=True)
                status = scan_agent(SPECS["hermes"], home=home, env=env, path="")
                self.assertEqual(status.status, StatusKind.CONFIG_ONLY)
                self.assertIsNone(status.executable)
                probe.assert_not_called()

    def test_windows_hermes_fallback_does_not_apply_to_unix(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            executable = home / ".hermes/bin/hermes.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch("agents_status.core.sys.platform", "linux"):
                status = scan_agent(SPECS["hermes"], home=home, env={}, path="")
            self.assertEqual(status.status, StatusKind.CONFIG_ONLY)


class VersionTests(unittest.TestCase):
    def test_version_strips_terminal_sequences_and_skips_empty_lines(self):
        cases = (
            ("\x1b[?25l\r\n\x1b[1;36m12.3.4\x1b[0m\x1b[?25h\n", "12.3.4"),
            ("\x1b]0;OMP\x07\x1b]11;?\x1b\\\n12.3.4\n", "12.3.4"),
            ("\x1b]8;;https://example.com\x1b\\12.3.4\x1b]8;;\x1b\\", "12.3.4"),
            ("\x9d0;OMP\x9c\x9b32m12.3.4\x9b0m", "12.3.4"),
            ("\x1b(B\x00\x0712.3.4\x7f", "12.3.4"),
            ("\n  2.1.295 (Claude Code)\r\n", "2.1.295 (Claude Code)"),
            ("\x1b[32m" + "1" * 100 + "\x1b[0m", "1" * 80),
            ("\x1b[?25h\n\x1b[0m", "조회 실패"),
        )
        for output, expected in cases:
            for stream in ("stdout", "stderr"):
                with self.subTest(output=repr(output), stream=stream):
                    result = subprocess.CompletedProcess([], 0, **{
                        "stdout": output if stream == "stdout" else "",
                        "stderr": output if stream == "stderr" else "",
                    })
                    with patch("agents_status.core.subprocess.run", return_value=result):
                        self.assertEqual(_version(Path("omp.cmd"), ("--version",)), expected)

    def test_failed_version_does_not_display_error_as_version(self):
        result = subprocess.CompletedProcess([], 1, "12.3.4\n", "'bun' is not recognized\ufffd\n")
        with patch("agents_status.core.subprocess.run", return_value=result):
            self.assertEqual(_version(Path("omp.cmd"), ("--version",)), "조회 실패")

    def test_version_cleans_real_subprocess_output(self):
        script = "import sys; sys.stdout.write('\\x1b]0;OMP\\x07\\n\\x1b[32m12.3.4\\x1b[0m\\n')"
        self.assertEqual(_version(Path(sys.executable), ("-c", script)), "12.3.4")


class UpdateTests(unittest.TestCase):
    def test_self_update_uses_discovered_executable(self):
        status = AgentStatus(SPECS["opencode"], StatusKind.INSTALLED, Path("/opt/bin/opencode"), "1.0", "openai")
        command, reason = update_command(status)
        self.assertEqual(command.argv, (str(status.executable), "upgrade"))
        self.assertEqual(reason, "")

    def test_missing_agent_cannot_update(self):
        status = AgentStatus(SPECS["claude"], StatusKind.MISSING, None, "-", "-")
        command, reason = update_command(status)
        self.assertIsNone(command)
        self.assertIn("실행 파일", reason)

    def test_codex_npm_install_is_detected(self):
        status = AgentStatus(
            SPECS["codex"], StatusKind.INSTALLED,
            Path("/home/user/.nvm/bin/node_modules/@openai/codex/bin/codex"), "1.0", "openai",
        )
        with patch("pathlib.Path.resolve", return_value=status.executable), patch("shutil.which", return_value="/usr/bin/npm"):
            command, _ = update_command(status)
        self.assertEqual(command.argv[-2:], ("--global", "@openai/codex@latest"))

    def test_codex_standalone_install_uses_self_update(self):
        # 공식 설치 스크립트는 ~/.local/bin/codex 링크를 ~/.codex/packages/standalone/current/codex로 연결함.
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            release = home / ".codex/packages/standalone/releases/0.162.0-x86_64-unknown-linux-musl"
            (release / "bin").mkdir(parents=True)
            (release / "bin/codex").touch()
            current = home / ".codex/packages/standalone/current"
            symlink_or_skip(current, release)
            executable = home / ".local/bin/codex"
            executable.parent.mkdir(parents=True)
            symlink_or_skip(executable, current / "bin/codex")
            status = AgentStatus(SPECS["codex"], StatusKind.INSTALLED, executable, "codex-cli 0.162.0", "openai")
            command, _ = update_command(status, home=home)
            self.assertEqual(command.argv, (str(executable), "update"))
            self.assertEqual(command.display, "codex update")

    def test_omp_and_agy_self_update(self):
        for key, binary in (("omp", "omp"), ("antigravity", "agy"), ("antigravity", "agy.EXE")):
            with self.subTest(key=key):
                executable = Path("/custom/bin") / binary
                status = AgentStatus(SPECS[key], StatusKind.INSTALLED, executable, "1.0", "unknown")
                command, reason = update_command(status)
                self.assertEqual(command.argv, (str(executable), "update"))
                self.assertEqual(reason, "")

    def test_package_updates_follow_symlink_and_original_manager(self):
        for key, package, binary in (
            ("crush", "@charmland/crush", "crush"),
            ("antigravity", "@google/gemini-cli", "gemini"),
        ):
            for manager in ("npm", "bun", "brew"):
                with self.subTest(key=key, manager=manager), tempfile.TemporaryDirectory() as directory:
                    prefix = Path(directory)
                    (prefix / "bin").mkdir()
                    tool = prefix / "bin" / manager
                    tool.touch()
                    if manager == "brew":
                        root = prefix / "Cellar" / binary / "1.0"
                    else:
                        modules = prefix / ("lib/node_modules" if manager == "npm" else "install/global/node_modules")
                        root = modules / package
                    root.mkdir(parents=True)
                    (root / "package.json").write_text(json.dumps({"name": package}))
                    target = root / binary
                    target.touch()
                    executable = prefix / "bin" / binary
                    symlink_or_skip(executable, target)
                    status = AgentStatus(SPECS[key], StatusKind.INSTALLED, executable, "1.0", "unknown")
                    with patch("shutil.which", return_value="/different/installation/bin/npm"):
                        command, reason = update_command(status)
                    expected = {
                        "npm": (str(tool), "install", "--global", "--prefix", str(prefix), f"{package}@latest"),
                        "bun": (str(tool), "add", "--global", f"{package}@latest"),
                        "brew": (str(tool), "upgrade", "--formula", binary),
                    }
                    self.assertEqual(command.argv, expected[manager])
                    self.assertEqual(reason, "")

    def test_unknown_or_unrelated_package_does_not_select_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory)
            root = prefix / "lib/node_modules/@other/crush"
            root.mkdir(parents=True)
            (root / "package.json").write_text('{"name":"@other/crush"}')
            (prefix / "bin").mkdir()
            (prefix / "bin/npm").touch()
            for key, executable in (
                ("crush", root / "crush"),
                ("crush", prefix / "crush"),
                ("antigravity", prefix / "gemini"),
                ("antigravity", prefix / "antigravity"),
            ):
                with self.subTest(key=key, executable=executable):
                    status = AgentStatus(SPECS[key], StatusKind.INSTALLED, executable, "1.0", "unknown")
                    command, reason = update_command(status)
                    self.assertIsNone(command)
                    self.assertIn("판별하지 못했습니다", reason)

    def test_npm_install_without_matching_manager_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "lib/node_modules/@charmland/crush"
            root.mkdir(parents=True)
            (root / "package.json").write_text('{"name":"@charmland/crush"}')
            status = AgentStatus(SPECS["crush"], StatusKind.INSTALLED, root / "crush", "1.0", "unknown")
            with patch("shutil.which", return_value="/different/bin/npm"):
                command, _ = update_command(status)
            self.assertIsNone(command)


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def test_header_shows_title_and_platform(self):
        self.assertEqual(AgentsStatusApp.TITLE, "AI 코딩 에이전트 상태 조회 및 설치 관리")
        self.assertEqual(AgentsStatusApp.SUB_TITLE, platform_name())
        for system, version, expected in (("Windows", "", "Windows"), ("Darwin", "", "macOS"), ("Linux", "Linux version 6.6", "Linux"), ("Linux", "5.15.0-microsoft-standard-WSL2", "Linux (WSL)")):
            with self.subTest(system=system), patch("platform.system", return_value=system), patch("agents_status.app.Path.read_text", return_value=version):
                self.assertEqual(platform_name(), expected)
        app = AgentsStatusApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertEqual((app.title, app.sub_title), (AgentsStatusApp.TITLE, platform_name()))

    async def test_selection_changes_install_label_and_command(self):
        statuses = [AgentStatus(SPECS[key], kind, executable, "-", "-") for key, kind, executable in (
            ("pi", StatusKind.INSTALLED, Path("/bin/pi")),
            ("codex", StatusKind.CONFIG_ONLY, None),
            ("claude", StatusKind.MISSING, None),
        )]
        with patch("agents_status.app.scan_agents", AsyncMock(return_value=statuses)), patch("shutil.which", return_value="/bin/npm"), patch("agents_status.app.run_update", AsyncMock(return_value=CommandResult(0, "완료"))) as run:
            app = AgentsStatusApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertEqual(str(app.query_one("#update", Button).label), "업데이트")
                for row in (1, 2):
                    app.query_one(DataTable).move_cursor(row=row)
                    await pilot.pause()
                    self.assertEqual(str(app.query_one("#update", Button).label), "설치")
                    self.assertEqual(app.query_one("#delete", Button).disabled, row == 2)
                    await pilot.click("#update")
                    await pilot.pause()
                    self.assertNotIsInstance(app.screen, ConfirmUpdate)
                    self.assertEqual(run.await_count, row)
                    package = "@openai/codex" if row == 1 else "@anthropic-ai/claude-code"
                    scripts = ("--allow-scripts=@anthropic-ai/claude-code",) if row == 2 else ()
                    self.assertEqual(run.call_args.args[0].argv, ("/bin/npm", "install", "--global", *scripts, f"{package}@latest"))

    async def test_delete_answers_keep_remove_or_cancel(self):
        status = AgentStatus(SPECS["codex"], StatusKind.CONFIG_ONLY, None, "-", "-")
        for key, expected in (("y", True), ("n", False), ("escape", None), ("#delete-config", True), ("#keep-config", False), ("#cancel-delete", None)):
            with self.subTest(key=key), patch("agents_status.app.scan_agents", AsyncMock(return_value=[status])), patch("agents_status.app.run_uninstall", AsyncMock(return_value=CommandResult(0, "완료"))) as run:
                app = AgentsStatusApp()
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause()
                    await pilot.click("#delete")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ConfirmDelete)
                    if key.startswith("#"):
                        await pilot.click(key)
                    else:
                        await pilot.press(key)
                    await pilot.pause()
                    if expected is None:
                        run.assert_not_awaited()
                    else:
                        run.assert_awaited_once()
                        self.assertEqual(run.call_args.args[1], config_paths(status.spec) if expected else ())

    async def test_update_button_opens_confirmation_screen(self):
        status = AgentStatus(
            SPECS["pi"], StatusKind.INSTALLED,
            Path("/home/user/.local/bin/pi"), "1.0.0", "openai-codex",
        )
        with patch("agents_status.app.scan_agents", AsyncMock(return_value=[status])):
            app = AgentsStatusApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.click("#update")
                await pilot.pause()
                self.assertIsInstance(app.screen, ConfirmUpdate)
                self.assertEqual(app.screen.agent_name, "Pi")

    async def test_log_drag_selection_copies_text_and_click_clears(self):
        status = AgentStatus(SPECS["pi"], StatusKind.INSTALLED, Path("/bin/pi"), "1.0.0", "openai")
        with patch("agents_status.app.scan_agents", AsyncMock(return_value=[status])):
            app = AgentsStatusApp()
            async with app.run_test(size=(40, 30)) as pilot:
                await pilot.pause()
                log = app.query_one("#log", RichLog)
                log.clear()
                log.write("[bold]OpenClaw 업데이트[/bold]: $ openclaw update --yes")
                log.write(Text.from_ansi("\x1b[31mNode 22 is unsupported\x1b[0m; use 26"))
                await pilot.pause()
                # 로그 위젯은 위쪽 테두리 1줄과 좌우 여백 1칸이 있으므로 내용 좌표는 (1, 1)부터 시작함.
                await pilot.mouse_down("#log", offset=(1, 1))
                await pilot.hover("#log", offset=(23, 2))
                await pilot.mouse_up("#log", offset=(23, 2))
                await pilot.pause()
                self.assertEqual(app._clipboard, "OpenClaw 업데이트: $ openclaw update --yes\nNode 22 is unsupported")
                selection_style = app.screen.get_component_rich_style("screen--selection")

                def highlighted(y):
                    return "".join(segment.text for segment in log.render_line(y) if segment.style and segment.style.bgcolor == selection_style.bgcolor)

                # 첫 줄은 화면 폭(38칸)에 맞게 잘린 보이는 부분 전체가, 둘째 줄은 선택 끝까지만 강조됨.
                self.assertEqual(highlighted(0).rstrip(), log.render_line(0).text.rstrip())
                self.assertTrue(highlighted(0).startswith("OpenClaw 업데이트: $ openclaw update"))
                self.assertEqual(highlighted(1), "Node 22 is unsupported")
                # 한글은 2칸을 차지하므로 셀 9~17을 드래그하면 '업데이트'만 선택됨.
                await pilot.mouse_down("#log", offset=(10, 1))
                await pilot.hover("#log", offset=(18, 1))
                await pilot.mouse_up("#log", offset=(18, 1))
                await pilot.pause()
                self.assertEqual(app._clipboard, "업데이트")
                # 가로 스크롤 상태에서도 화면 셀 위치를 원본 문자 인덱스로 변환함 (가장 긴 줄 42칸, 폭 38칸).
                log.scroll_to(x=6, animate=False)
                await pilot.pause()
                self.assertEqual(log.scroll_offset.x, 6)
                await pilot.mouse_down("#log", offset=(4, 1))
                await pilot.hover("#log", offset=(8, 1))
                await pilot.mouse_up("#log", offset=(8, 1))
                await pilot.pause()
                self.assertEqual(app._clipboard, "업데")
                app._clipboard = ""
                await pilot.click("#log", offset=(3, 2))
                await pilot.pause()
                self.assertEqual(app.screen.selections, {})
                self.assertEqual(app._clipboard, "")

    async def test_update_output_is_streamed_before_completion(self):
        lines = []
        first_line = asyncio.Event()

        def receive(line):
            lines.append(line)
            first_line.set()

        command = UpdateCommand(
            (
                sys.executable,
                "-c",
                "import time; print('시작', flush=True); time.sleep(0.3); print('완료', flush=True)",
            ),
            "stream test",
        )
        task = asyncio.create_task(run_update(command, on_output=receive))
        await asyncio.wait_for(first_line.wait(), timeout=1)
        self.assertEqual(lines, ["시작"])
        self.assertFalse(task.done())
        result = await task
        self.assertEqual(result.returncode, 0)
        self.assertEqual(lines, ["시작", "완료"])


class UpdateInterruptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_quit_requires_confirmation(self):
        with patch("agents_status.app.scan_agents", AsyncMock(return_value=[])):
            app = AgentsStatusApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.busy = True
                await pilot.press("q")
                self.assertIsInstance(app.screen, ConfirmQuit)
                await pilot.press("escape")
                self.assertNotIsInstance(app.screen, ConfirmQuit)
                await pilot.press("q")
                with patch.object(app, "exit") as exit_app:
                    await pilot.click("#confirm")
                    await pilot.pause()
                    exit_app.assert_called_once()
                app.busy = False

    async def test_confirmed_quit_waits_for_command_cleanup(self):
        started = asyncio.Event()
        cleaned = asyncio.Event()
        status = AgentStatus(SPECS["openclaw"], StatusKind.INSTALLED, Path("/bin/openclaw"), "1", "-")

        async def update(*args, **kwargs):
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                await asyncio.sleep(0.05)
                cleaned.set()

        with patch("agents_status.app.scan_agents", AsyncMock(return_value=[status])) as scan, patch("agents_status.app.run_update", update):
            app = AgentsStatusApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.update_agent(status, UpdateCommand(("openclaw", "update"), "openclaw update"))
                await asyncio.wait_for(started.wait(), 5)
                await pilot.press("q")
                with patch.object(app, "exit", side_effect=lambda: self.assertTrue(cleaned.is_set())) as exit_app:
                    await pilot.click("#confirm")
                    await pilot.pause()
                    exit_app.assert_called_once()
                self.assertEqual(scan.await_count, 1)

    async def test_openclaw_inspection_warning_is_shown_once(self):
        status = AgentStatus(SPECS["openclaw"], StatusKind.INSTALLED, Path("/bin/openclaw"), "1", "-")

        async def update(command, *, on_output):
            for _ in range(2):
                on_output("Could not inspect OpenClaw processes: could not resolve script /usr/local/bin/n8n")
            return CommandResult(1, "")

        with patch("agents_status.app.scan_agents", AsyncMock(return_value=[status])), patch("agents_status.app.run_update", update):
            app = AgentsStatusApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                worker = app.update_agent(status, UpdateCommand(("openclaw", "update"), "openclaw update"))
                await worker.wait()
                text = "\n".join(line.text for line in app.query_one("#log", RichLog).lines)
                self.assertEqual(text.count("OpenClaw의 프로세스 검사에 실패했습니다."), 1)

    @unittest.skipUnless(sys.platform == "linux", "Linux 프로세스 상태 검사")
    async def test_cancel_and_timeout_kill_descendants(self):
        for cancel in (True, False):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as directory:
                marker = Path(directory, "child-pid")
                ready = asyncio.Event()
                command = UpdateCommand((sys.executable, "-c",
                    "import os, subprocess, sys, time; "
                    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                    f"open({str(marker)!r}, 'w').write(str(child.pid)); "
                    "print(os.getcwd(), flush=True); time.sleep(60)"), "tree test")
                lines = []

                def receive(line):
                    lines.append(line)
                    ready.set()

                task = asyncio.create_task(run_update(command, timeout=1 if not cancel else 60, on_output=receive))
                try:
                    await asyncio.wait_for(ready.wait(), 5)
                    pid = int(marker.read_text())
                    if cancel:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(task, 5)
                    else:
                        result = await asyncio.wait_for(task, 5)
                        self.assertEqual(result.returncode, 124)
                        self.assertIn("롤백되지", result.output)
                    self.assertFalse(Path(lines[0]).exists())
                    for _ in range(100):
                        proc = Path(f"/proc/{pid}/stat")
                        if not proc.exists() or proc.read_text().split(") ", 1)[1].startswith("Z"):
                            break
                        await asyncio.sleep(0.01)
                    else:
                        self.fail("업데이트 하위 프로세스가 남아 있음")
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)


class WorkingDirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_runs_in_temporary_directory_and_cleans_leftovers(self):
        # 설치 스크립트가 작업 디렉터리에 남긴 파일까지 함께 정리되어야 함.
        command = UpdateCommand(
            (sys.executable, "-c", "import os; print(os.getcwd(), flush=True); os.mkdir('archive-leftover')"),
            "cwd test",
        )
        result = await run_update(command)
        self.assertEqual(result.returncode, 0)
        workdir = Path(result.output.strip())
        self.assertTrue(workdir.name.startswith("agents-status-"), workdir)
        self.assertNotEqual(workdir, Path.cwd())
        self.assertFalse(workdir.exists())

    def test_version_probe_runs_in_home_or_given_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory, "home")
            bin_dir = Path(directory, "bin")
            scratch = Path(directory, "scratch")
            for folder in (home, bin_dir, scratch):
                folder.mkdir()
            if sys.platform == "win32":
                probe = bin_dir / "copilot.cmd"
                probe.write_text("@echo off\ncd\n", encoding="utf-8")
            else:
                probe = bin_dir / "copilot"
                probe.write_text(f"#!{sys.executable}\nimport os\nprint(os.getcwd())\n", encoding="utf-8")
            probe.chmod(0o755)
            scanned = scan_agent(SPECS["copilot"], home=home, env={}, path=str(bin_dir))
            self.assertEqual(scanned.version, str(home))
            scanned = scan_agent(SPECS["copilot"], home=home, env={}, path=str(bin_dir), cwd=scratch)
            self.assertEqual(scanned.version, str(scratch))


class UninstallTests(unittest.IsolatedAsyncioTestCase):
    async def test_config_is_removed_only_when_requested_and_uninstall_succeeds(self):
        for remove, returncode in ((False, 0), (True, 0), (True, 1)):
            with self.subTest(remove=remove, returncode=returncode), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / ".codex"
                config.mkdir()
                (config / "auth.json").write_text("credentials")
                plan = UninstallPlan(UpdateCommand(("npm", "uninstall"), "uninstall"))
                with patch("agents_status.core.run_update", AsyncMock(return_value=CommandResult(returncode, "result"))):
                    result = await run_uninstall(plan, (config,) if remove else ())
                self.assertEqual(result.returncode, returncode)
                self.assertEqual(config.exists(), not (remove and returncode == 0))

    async def test_config_symlink_does_not_delete_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "shared"
            target.mkdir()
            (target / "keep").touch()
            config = root / ".codex"
            symlink_or_skip(config, target)
            result = await run_uninstall(UninstallPlan(), (config,))
            self.assertEqual(result.returncode, 0)
            self.assertFalse(config.is_symlink())
            self.assertTrue((target / "keep").exists())

    async def test_package_uninstall_uses_original_manager(self):
        for manager in ("npm", "bun", "brew"):
            with self.subTest(manager=manager), tempfile.TemporaryDirectory() as directory:
                prefix = Path(directory)
                (prefix / "bin").mkdir()
                tool = prefix / "bin" / manager
                tool.touch()
                root = prefix / {"npm": "lib/node_modules/@openai/codex", "bun": "install/global/node_modules/@openai/codex", "brew": "Caskroom/codex/1.0"}[manager]
                root.mkdir(parents=True)
                (root / "package.json").write_text('{"name":"@openai/codex"}')
                target = root / "codex"
                target.touch()
                executable = prefix / "bin/codex"
                symlink_or_skip(executable, target)
                status = AgentStatus(SPECS["codex"], StatusKind.INSTALLED, executable, "1.0", "openai")
                plan, reason = uninstall_plan(status)
                self.assertEqual(reason, "")
                self.assertEqual(plan.command.argv, {
                    "npm": (str(tool), "uninstall", "--global", "--prefix", str(prefix), "@openai/codex"),
                    "bun": (str(tool), "remove", "--global", "@openai/codex"),
                    "brew": (str(tool), "uninstall", "--cask", "codex"),
                }[manager])

    async def test_unknown_installation_is_not_deleted(self):
        status = AgentStatus(SPECS["pi"], StatusKind.INSTALLED, Path("/unknown/pi"), "1", "-")
        plan, reason = uninstall_plan(status)
        self.assertIsNone(plan)
        self.assertIn("판별하지 못했습니다", reason)

    async def test_native_claude_uninstall_preserves_config(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / ".local/share/claude"
            root.mkdir(parents=True)
            binary = root / "1.0"
            binary.touch()
            executable = home / ".local/bin/claude"
            executable.parent.mkdir()
            symlink_or_skip(executable, binary)
            config = home / ".claude"
            config.mkdir()
            status = AgentStatus(SPECS["claude"], StatusKind.INSTALLED, executable, "1", "-")
            plan, _ = uninstall_plan(status, home=home)
            result = await run_uninstall(plan)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(root.exists())
            self.assertFalse(executable.is_symlink())
            self.assertTrue(config.exists())

    async def test_windows_native_claude_uninstall_preserves_config_and_shared_bin(self):
        for name in ("claude.exe", "claude.EXE"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                root = home / ".local/share/claude"
                (root / "versions").mkdir(parents=True)
                (root / "versions/2.1.295").write_bytes(b"MZ\x90\x00")
                executable = home / ".local/bin" / name
                executable.parent.mkdir(parents=True)
                executable.write_bytes(b"MZ\x90\x00")
                other = executable.with_name("other.exe")
                other.touch()
                config = home / ".claude/settings.json"
                config.parent.mkdir()
                config.write_text("{}")
                state = home / ".claude.json"
                state.write_text("{}")
                status = AgentStatus(SPECS["claude"], StatusKind.INSTALLED, executable, "2.1.295", "anthropic")
                plan, reason = uninstall_plan(status, home=home)
                self.assertEqual(reason, "")
                self.assertIsNone(plan.command)
                self.assertEqual(plan.paths, (executable, root))
                with patch("agents_status.core._clean_user_environment", return_value=[]):
                    result = await run_uninstall(plan)
                self.assertEqual(result.returncode, 0, result.output)
                self.assertFalse(executable.exists())
                self.assertFalse(root.exists())
                self.assertTrue(other.exists())
                self.assertEqual(config.read_text(), "{}")
                self.assertEqual(state.read_text(), "{}")

    async def test_windows_claude_requires_native_binary_in_expected_directory(self):
        for location, contents in (
            (".local/bin/claude.exe", b"@echo off"),
            (".local/bin/claude.cmd", b"@echo off"),
            ("custom/claude.exe", b"MZ\x90\x00"),
        ):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                executable = home / location
                executable.parent.mkdir(parents=True)
                executable.write_bytes(contents)
                status = AgentStatus(SPECS["claude"], StatusKind.INSTALLED, executable, "1", "-")
                plan, reason = uninstall_plan(status, home=home)
                self.assertIsNone(plan)
                self.assertIn("판별하지 못했습니다", reason)
                self.assertTrue(executable.exists())

    async def test_codex_standalone_uninstall_removes_packages_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            packages = home / ".codex/packages"
            release = packages / "standalone/releases/0.162.0-x86_64-unknown-linux-musl"
            (release / "bin").mkdir(parents=True)
            # 삭제 전 데몬 종료 명령의 인자를 기록하는 가짜 실행 파일
            stopped = home / "stopped"
            (release / "bin/codex").write_text(f'#!/bin/sh\necho "$@" > "{stopped}"\n')
            (release / "bin/codex").chmod(0o755)
            (release / "bin/codex-code-mode-host").touch()
            current = packages / "standalone/current"
            symlink_or_skip(current, release)
            (packages / "app-server-daemon/releases").mkdir(parents=True)
            state = home / ".codex/app-server-daemon"
            state.mkdir()
            (state / "daemon-updater.stderr.log").touch()
            executable = home / ".local/bin/codex"
            executable.parent.mkdir(parents=True)
            symlink_or_skip(executable, current / "bin/codex")
            host = home / ".local/bin/codex-code-mode-host"
            symlink_or_skip(host, current / "bin/codex-code-mode-host")
            unrelated = home / ".local/bin/other"
            unrelated.touch()
            config = home / ".codex/config.toml"
            config.touch()
            status = AgentStatus(SPECS["codex"], StatusKind.INSTALLED, executable, "codex-cli 0.162.0", "openai")
            plan, _ = uninstall_plan(status, home=home)
            self.assertIsNone(plan.command)
            self.assertEqual(plan.paths, (executable, host, packages / "standalone", packages / "app-server-daemon", state))
            self.assertEqual(plan.prepare.argv, (str(executable), "app-server", "daemon", "stop"))
            result = await run_uninstall(plan)
            self.assertEqual(result.returncode, 0)
            if sys.platform != "win32":
                self.assertEqual(stopped.read_text().strip(), "app-server daemon stop")
            self.assertFalse(packages.joinpath("standalone").exists())
            self.assertFalse(packages.joinpath("app-server-daemon").exists())
            self.assertFalse(state.exists())
            self.assertFalse(executable.is_symlink())
            self.assertFalse(host.is_symlink())
            self.assertTrue(unrelated.exists())
            self.assertTrue(config.exists())

    async def test_uninstall_continues_when_prepare_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.touch()
            prepare = UpdateCommand((str(Path(directory) / "missing"),), "missing")
            lines: list[str] = []
            result = await run_uninstall(UninstallPlan(paths=(target,), prepare=prepare), on_output=lines.append)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(target.exists())
            self.assertTrue(any("정리 명령 실패" in line for line in lines))

    async def test_pi_managed_install_uninstall_removes_links_launcher_and_install_root(self):
        marker = {"kind": "pi-managed-install", "schemaVersion": 1, "layout": "releases-v1"}
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            agent = home / ".pi/agent"
            install = agent / "install"
            (install / "releases/1.1.0/node_modules/.bin").mkdir(parents=True)
            (install / "releases/1.1.0/node_modules/.bin/pi").touch()
            (install / "current-version").write_text("1.1.0\n")
            (agent / "bin").mkdir()
            launcher = agent / "bin/pi"
            launcher.write_text("#!/bin/sh\nexec \"$pi_release_bin\" \"$@\"\n")
            tool = agent / "bin/fd"
            tool.write_bytes(b"\x7fELF\x02")
            (home / ".local/bin").mkdir(parents=True)
            executable = home / ".local/bin/pi"
            symlink_or_skip(executable, Path("../../.pi/agent/bin/pi"))
            extra = home / "bin/pi"
            extra.parent.mkdir()
            symlink_or_skip(extra, launcher)
            (install / "managed-install.json").write_text(json.dumps({**marker, "entrypoint": {"type": "symlink", "path": str(extra)}}))
            settings = agent / "settings.json"
            settings.write_text('{"defaultProvider": "openai-codex"}')
            status = AgentStatus(SPECS["pi"], StatusKind.INSTALLED, executable, "1.1.0", "openai-codex")
            plan, reason = uninstall_plan(status, home=home)
            self.assertEqual(reason, "")
            self.assertIsNone(plan.command)
            self.assertEqual(plan.paths, (extra, executable, launcher, install))
            with patch("agents_status.core._clean_user_environment", return_value=[]):
                result = await run_uninstall(plan)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(install.exists())
            self.assertFalse(launcher.exists())
            self.assertFalse(executable.is_symlink())
            self.assertFalse(extra.is_symlink())
            self.assertTrue(tool.exists())
            self.assertTrue(settings.exists())

            # 런처 디렉터리 자체가 PATH에 있어 링크 없이 설치된 경우에는 런처와 install 루트만 삭제함.
            install.mkdir()
            (install / "managed-install.json").write_text(json.dumps({**marker, "entrypoint": {"type": "symlink", "path": str(launcher)}}))
            launcher.write_text("#!/bin/sh\n")
            status = AgentStatus(SPECS["pi"], StatusKind.INSTALLED, launcher, "1.1.0", "openai-codex")
            plan, reason = uninstall_plan(status, home=home)
            self.assertEqual(reason, "")
            self.assertEqual(plan.paths, (launcher, install))

            # Windows 설치 스크립트는 bin에 pi·pi.cmd·pi.ps1·pi-launcher.js 런처를 두고 pi.cmd를 진입점으로 기록함.
            for name in ("pi.cmd", "pi.ps1", "pi-launcher.js"):
                (agent / "bin" / name).write_text("")
            (install / "managed-install.json").write_text(json.dumps({**marker, "entrypoint": {"type": "script", "path": str(agent / "bin/pi.cmd")}}))
            status = AgentStatus(SPECS["pi"], StatusKind.INSTALLED, agent / "bin/pi.cmd", "1.1.0", "openai-codex")
            plan, reason = uninstall_plan(status, home=home)
            self.assertEqual(reason, "")
            self.assertEqual(plan.paths, (launcher, agent / "bin/pi.cmd", agent / "bin/pi.ps1", agent / "bin/pi-launcher.js", install))

            # marker가 없거나 형식이 다르면 관리형 설치로 판별하지 않음.
            (install / "managed-install.json").write_text(json.dumps({**marker, "layout": "releases-v2"}))
            status = AgentStatus(SPECS["pi"], StatusKind.INSTALLED, executable, "1.1.0", "openai-codex")
            symlink_or_skip(executable, launcher)
            plan, reason = uninstall_plan(status, home=home)
            self.assertIsNone(plan)
            self.assertIn("판별하지 못했습니다", reason)

    async def test_pi_npm_uninstall_detects_renamed_package(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "nodejs"
            for package in ("@earendil-works/pi-coding-agent", "@mariozechner/pi-coding-agent"):
                with self.subTest(package=package):
                    root = prefix / "node_modules" / package
                    root.mkdir(parents=True)
                    (root / "package.json").write_text(json.dumps({"name": package}))
                    executable = prefix / "pi.cmd"
                    executable.write_text("@echo off")
                    (prefix / "npm.cmd").write_text("")
                    status = AgentStatus(SPECS["pi"], StatusKind.INSTALLED, executable, "1.1.0", "-")
                    with patch("shutil.which", return_value=None):
                        plan, reason = uninstall_plan(status)
                    self.assertEqual(reason, "")
                    self.assertEqual(plan.command.argv, (str(prefix / "npm.cmd"), "uninstall", "--global", "--prefix", str(prefix), package))
                    shutil.rmtree(root.parent.parent if package.startswith("@") else root)

    async def test_agy_standalone_uninstall(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            install_root = home / "AppData/Local/agy"
            (install_root / "bin").mkdir(parents=True)
            executable = install_root / "bin/agy.exe"
            executable.write_bytes(b"MZ\x90\x00")
            (install_root / "bin/agy.exe.123.old").write_bytes(b"MZ\x90\x00")
            config = home / ".gemini/antigravity-cli"
            config.mkdir(parents=True)
            status = AgentStatus(SPECS["antigravity"], StatusKind.INSTALLED, executable, "1.3.2", "google")
            with patch.dict(os.environ, {"LOCALAPPDATA": str(home / "AppData/Local")}):
                plan, reason = uninstall_plan(status, home=home)
            self.assertEqual(reason, "")
            self.assertEqual(plan.paths, (install_root,))
            result = await run_uninstall(plan)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(install_root.exists())
            self.assertTrue(config.exists())

            custom = home / "opt/agy"
            custom.parent.mkdir()
            custom.write_bytes(b"\x7fELF\x02")
            status = AgentStatus(SPECS["antigravity"], StatusKind.INSTALLED, custom, "1.3.2", "google")
            plan, reason = uninstall_plan(status, home=home)
            self.assertEqual(reason, "")
            self.assertEqual(plan.paths, (custom,))

            script = home / "opt/agy.cmd"
            script.write_text("@echo off")
            status = AgentStatus(SPECS["antigravity"], StatusKind.INSTALLED, script, "1.3.2", "google")
            plan, reason = uninstall_plan(status, home=home)
            self.assertIsNone(plan)
            self.assertIn("판별하지 못했습니다", reason)

    async def test_hermes_windows_uninstall_uses_hermes_home_and_removes_readonly_files(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            data = home / "AppData/Local/hermes"
            root = data / "hermes-agent"
            (root / "hermes_cli").mkdir(parents=True)
            (root / "venv/Scripts").mkdir(parents=True)
            executable = root / "venv/Scripts/hermes.exe"
            executable.write_bytes(b"MZ")
            pack = root / ".git/objects/pack/pack.idx"
            pack.parent.mkdir(parents=True)
            pack.write_bytes(b"idx")
            pack.chmod(0o444)
            (data / "config.yaml").write_text("model:\n  provider: openai\n", encoding="utf-8")
            status = AgentStatus(SPECS["hermes"], StatusKind.INSTALLED, executable, "Hermes Agent v0.18.2", "openai")
            with patch.dict(os.environ, {"HERMES_HOME": str(data)}):
                plan, reason = uninstall_plan(status, home=home)
                self.assertEqual(reason, "")
                self.assertEqual(plan.paths, (root,))
                self.assertEqual(plan.variables, ("HERMES_HOME",) if sys.platform == "win32" else ())
                self.assertEqual(config_paths(SPECS["hermes"], home=home), (data,))
            with patch("agents_status.core._clean_user_environment", return_value=[]) as cleanup:
                result = await run_uninstall(plan)
            self.assertEqual(cleanup.call_args.args, ((root,), plan.variables))
            self.assertEqual(result.returncode, 0)
            self.assertFalse(root.exists())
            self.assertTrue((data / "config.yaml").exists())

    async def test_hermes_windows_bin_uninstall_preserves_settings_and_shared_tools(self):
        for location in ("AppData/Local/hermes", "custom hermes"):
            for launcher in ("hermes.EXE", "hermes.cmd"):
                with self.subTest(location=location, launcher=launcher), tempfile.TemporaryDirectory() as directory:
                    home = Path(directory)
                    data = home / location
                    root = data / "hermes-agent"
                    (root / "hermes_cli").mkdir(parents=True)
                    (root / "venv/Scripts").mkdir(parents=True)
                    target = root / "venv/Scripts/hermes.exe"
                    target.write_bytes(b"MZ")
                    bin_dir = data / "bin"
                    bin_dir.mkdir()
                    launchers = tuple(bin_dir / name for name in (
                        launcher if launcher.lower() == "hermes.exe" else "hermes.exe",
                        "hermes.cmd", "hermes-acp.exe", "hermes-acp.cmd",
                    ))
                    for path in launchers:
                        if path.suffix.lower() == ".exe":
                            path.write_bytes(b"MZ")
                        else:
                            path.write_text(f'@echo off\n"{target}" %*\n', encoding="utf-8")
                    shared = (bin_dir / "uv.exe", bin_dir / "browser.exe")
                    for path in shared:
                        path.write_bytes(b"MZ")
                    config = data / "config.yaml"
                    config.write_text("model:\n  provider: openai\n", encoding="utf-8")
                    executable = bin_dir / launcher
                    status = AgentStatus(SPECS["hermes"], StatusKind.INSTALLED, executable, "1", "openai")
                    with patch("agents_status.core.sys.platform", "win32"):
                        plan, reason = uninstall_plan(status, home=home)
                    self.assertEqual(reason, "")
                    self.assertIsNone(plan.command)
                    self.assertEqual(set(plan.paths), {*launchers, root})
                    self.assertEqual(plan.variables, ("HERMES_HOME",))
                    with patch("agents_status.core._clean_user_environment", return_value=[]) as cleanup:
                        result = await run_uninstall(plan)
                    self.assertEqual(result.returncode, 0, result.output)
                    self.assertFalse(root.exists())
                    self.assertFalse(any(path.exists() for path in launchers))
                    self.assertTrue(all(path.exists() for path in shared))
                    self.assertTrue(config.exists())
                    self.assertNotIn(bin_dir, cleanup.call_args.args[0])
                    self.assertNotIn(data, cleanup.call_args.args[0])

    async def test_hermes_windows_bin_requires_matching_checkout(self):
        for missing in ("checkout", "entrypoint", "native_binary"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                data = home / "hermes"
                root = data / "hermes-agent"
                if missing != "checkout":
                    (root / "hermes_cli").mkdir(parents=True)
                if missing != "entrypoint":
                    (root / "venv/Scripts").mkdir(parents=True)
                    (root / "venv/Scripts/hermes.exe").write_bytes(b"MZ")
                executable = data / "bin/hermes.exe"
                executable.parent.mkdir(parents=True)
                executable.write_bytes(b"@echo off" if missing == "native_binary" else b"MZ")
                status = AgentStatus(SPECS["hermes"], StatusKind.INSTALLED, executable, "1", "-")
                with patch("agents_status.core.sys.platform", "win32"):
                    plan, reason = uninstall_plan(status, home=home)
                self.assertIsNone(plan)
                self.assertIn("판별하지 못했습니다", reason)
                self.assertTrue(executable.exists())

    async def test_hermes_unix_uninstall_removes_link_and_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            unix_root = home / ".hermes/hermes-agent"
            (unix_root / "hermes_cli").mkdir(parents=True)
            (unix_root / "venv/bin").mkdir(parents=True)
            target = unix_root / "venv/bin/hermes"
            target.touch()
            wrapper = home / ".local/bin/hermes"
            wrapper.parent.mkdir(parents=True)
            symlink_or_skip(wrapper, target)
            status = AgentStatus(SPECS["hermes"], StatusKind.INSTALLED, wrapper, "Hermes Agent v0.18.2", "openai")
            with patch.dict(os.environ, {"HERMES_HOME": ""}), patch("agents_status.core.sys.platform", "linux"):
                plan, reason = uninstall_plan(status, home=home)
            self.assertEqual(reason, "")
            self.assertEqual(plan.paths, (wrapper, unix_root))

    async def test_hermes_unix_shim_uninstall_removes_shim_and_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for root in (home / ".hermes/hermes-agent", home / "usr/local/lib/hermes-agent"):
                with self.subTest(root=root):
                    (root / "hermes_cli").mkdir(parents=True)
                    (root / "venv/bin").mkdir(parents=True)
                    (root / "venv/bin/hermes").write_text("#!/usr/bin/python\n")
                    shim = home / ".local/bin/hermes"
                    shim.parent.mkdir(parents=True, exist_ok=True)
                    shim.write_text(f'#!/usr/bin/env bash\nunset PYTHONPATH\nexec "{root / "venv/bin/hermes"}" "$@"\n')
                    status = AgentStatus(SPECS["hermes"], StatusKind.INSTALLED, shim, "Hermes Agent v0.18.2", "openai")
                    plan, reason = uninstall_plan(status, home=home)
                    self.assertEqual(reason, "")
                    self.assertEqual(plan.paths, (shim, root))
                    with patch("agents_status.core._clean_user_environment", return_value=[]):
                        result = await run_uninstall(plan)
                    self.assertEqual(result.returncode, 0)
                    self.assertFalse(root.exists())
                    self.assertFalse(shim.exists())

            stray = home / ".local/bin/hermes"
            stray.write_text('#!/bin/sh\nexec "/opt/other/venv/bin/hermes" "$@"\n')
            status = AgentStatus(SPECS["hermes"], StatusKind.INSTALLED, stray, "1", "-")
            plan, reason = uninstall_plan(status, home=home)
            self.assertIsNone(plan)
            self.assertIn("판별하지 못했습니다", reason)

    async def test_hermes_unix_store_launcher_uninstall_removes_companion_shims(self):
        # 최신 설치는 <체크아웃>/.hermes/bin의 진입점을 따옴표 없이 exec하는 shim 세 개를 ~/.local/bin에 둠.
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / ".hermes/hermes-agent"
            (root / "hermes_cli").mkdir(parents=True)
            (root / ".hermes/bin").mkdir(parents=True)
            for name in ("hermes", "hermes-acp"):
                (root / ".hermes/bin" / name).write_text("#!/bin/sh\nexec /usr/bin/python3 -I -c 'pass'\n")
            local_bin = home / ".local/bin"
            local_bin.mkdir(parents=True)
            shim = local_bin / "hermes"
            shim.write_text(f'#!/bin/sh\nexec {root / ".hermes/bin/hermes"} "$@"\n')
            agent = local_bin / "hermes-agent"
            agent.write_text(f'#!/bin/sh\nexec {root / ".hermes/bin/hermes"} --run-module run_agent "$@"\n')
            acp = local_bin / "hermes-acp"
            acp.write_text(f'#!/bin/sh\nexec {root / ".hermes/bin/hermes-acp"} "$@"\n')
            shared = local_bin / "uv"
            shared.write_bytes(b"\x7fELF\x02")
            status = AgentStatus(SPECS["hermes"], StatusKind.INSTALLED, shim, "Hermes Agent v0.21.6", "openai")
            with patch("agents_status.core.sys.platform", "linux"):
                plan, reason = uninstall_plan(status, home=home)
            self.assertEqual(reason, "")
            self.assertEqual(plan.paths, (shim, agent, acp, root))
            self.assertEqual(plan.variables, ())
            with patch("agents_status.core._clean_user_environment", return_value=[]):
                result = await run_uninstall(plan)
            self.assertEqual(result.returncode, 0)
            for path in (shim, agent, acp, root):
                self.assertFalse(path.exists(), path)
            self.assertTrue(shared.exists())
            self.assertTrue((home / ".hermes").is_dir())

            # 다른 체크아웃을 가리키는 보조 shim은 보존함.
            (root / "hermes_cli").mkdir(parents=True)
            (root / ".hermes/bin").mkdir(parents=True)
            (root / ".hermes/bin/hermes").write_text("#!/bin/sh\n")
            shim.write_text(f'#!/bin/sh\nexec "{root / ".hermes/bin/hermes"}" "$@"\n')
            other = home / "opt/hermes-agent"
            (other / "hermes_cli").mkdir(parents=True)
            (other / ".hermes/bin").mkdir(parents=True)
            (other / ".hermes/bin/hermes-acp").write_text("#!/bin/sh\n")
            acp.write_text(f'#!/bin/sh\nexec {other / ".hermes/bin/hermes-acp"} "$@"\n')
            with patch("agents_status.core.sys.platform", "linux"):
                plan, reason = uninstall_plan(status, home=home)
            self.assertEqual(reason, "")
            self.assertEqual(plan.paths, (shim, root))

    async def test_kilo_official_install_uninstall_removes_bin_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            bin_dir = home / ".kilo/bin"
            (bin_dir / "tree-sitter").mkdir(parents=True)
            (bin_dir / "tree-sitter/bash.so").write_bytes(b"\x7fELF\x02")
            executable = bin_dir / "kilo"
            executable.write_bytes(b"\x7fELF\x02")
            config = home / ".config/kilo"
            config.mkdir(parents=True)
            (home / ".kilo/.gitignore").write_text("node_modules\n")
            status = AgentStatus(SPECS["kilo"], StatusKind.INSTALLED, executable, "1.2.3", "openai")
            plan, reason = uninstall_plan(status, home=home)
            self.assertEqual(reason, "")
            self.assertEqual(plan.paths, (bin_dir,))
            self.assertIn(str(bin_dir), plan.display)
            with patch("agents_status.core._clean_user_environment", return_value=[]):
                result = await run_uninstall(plan)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(bin_dir.exists())
            self.assertTrue(config.exists())
            self.assertTrue((home / ".kilo/.gitignore").exists())

            # Windows 공식 설치의 kilo.exe도 같은 방식으로 판별함.
            bin_dir.mkdir(parents=True)
            windows = bin_dir / "kilo.exe"
            windows.write_bytes(b"MZ\x90\x00")
            status = AgentStatus(SPECS["kilo"], StatusKind.INSTALLED, windows, "1.2.3", "openai")
            plan, reason = uninstall_plan(status, home=home)
            self.assertEqual(reason, "")
            self.assertEqual(plan.paths, (bin_dir,))

            # 바이너리가 아닌 스크립트나 다른 위치의 설치는 판별하지 않음.
            script = bin_dir / "kilo"
            script.write_text("#!/bin/sh\nexec node /opt/kilo/index.js\n")
            status = AgentStatus(SPECS["kilo"], StatusKind.INSTALLED, script, "1.2.3", "openai")
            plan, reason = uninstall_plan(status, home=home)
            self.assertIsNone(plan)
            self.assertIn("판별하지 못했습니다", reason)
            elsewhere = home / "opt/kilo"
            elsewhere.parent.mkdir()
            elsewhere.write_bytes(b"\x7fELF\x02")
            status = AgentStatus(SPECS["kilo"], StatusKind.INSTALLED, elsewhere, "1.2.3", "openai")
            plan, reason = uninstall_plan(status, home=home)
            self.assertIsNone(plan)

    async def test_omp_official_binary_uninstall(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            # Windows 기본 설치 위치는 전용 디렉터리이므로 디렉터리째 삭제함.
            install_root = home / "AppData/Local/omp"
            install_root.mkdir(parents=True)
            executable = install_root / "omp.exe"
            executable.write_bytes(b"MZ\x90\x00")
            config = home / ".omp/agent"
            config.mkdir(parents=True)
            status = AgentStatus(SPECS["omp"], StatusKind.INSTALLED, executable, "18.8.6", "openai")
            with patch("agents_status.core.sys.platform", "win32"), patch.dict(os.environ, {"LOCALAPPDATA": str(home / "AppData/Local")}):
                plan, reason = uninstall_plan(status, home=home)
            self.assertEqual(reason, "")
            self.assertEqual(plan.paths, (install_root,))
            with patch("agents_status.core._clean_user_environment", return_value=[]):
                result = await run_uninstall(plan)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(install_root.exists())
            self.assertTrue(config.exists())

            # Unix 기본 위치 ~/.local/bin과 사용자 지정 위치는 공용 디렉터리이므로 실행 파일만 삭제함.
            for location in (home / ".local/bin/omp", home / "opt/tools/omp"):
                location.parent.mkdir(parents=True)
                location.write_bytes(b"\x7fELF\x02")
                status = AgentStatus(SPECS["omp"], StatusKind.INSTALLED, location, "18.8.6", "openai")
                with patch("agents_status.core.sys.platform", "linux"):
                    plan, reason = uninstall_plan(status, home=home)
                self.assertEqual(reason, "")
                self.assertEqual(plan.paths, (location,))

            # npm 전역 설치의 셸 shim은 바이너리가 아니므로 이 방식으로 판별하지 않음.
            shim = home / "opt/npm/omp"
            shim.parent.mkdir()
            shim.write_text("#!/usr/bin/env bun\n")
            status = AgentStatus(SPECS["omp"], StatusKind.INSTALLED, shim, "18.8.6", "openai")
            plan, reason = uninstall_plan(status, home=home)
            self.assertIsNone(plan)
            self.assertIn("판별하지 못했습니다", reason)

    async def test_bun_global_install_rooted_at_ancestor_package_json(self):
        # Bun은 ~/.bun/install/global에 package.json이 없으면 상위에서 package.json을 찾아 그곳(예: 홈)을 전역
        # 프로젝트로 삼고, 링크는 여전히 ~/.bun/bin에 둠.
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            bun_home = home / ".bun"
            (bun_home / "bin").mkdir(parents=True)
            (bun_home / "install/global").mkdir(parents=True)
            bun = bun_home / "bin/bun"
            bun.touch()
            root = home / "node_modules/@oh-my-pi/pi-coding-agent"
            (root / "dist").mkdir(parents=True)
            (root / "package.json").write_text('{"name":"@oh-my-pi/pi-coding-agent"}')
            (root / "dist/cli.js").touch()
            executable = bun_home / "bin/omp"
            symlink_or_skip(executable, root / "dist/cli.js")
            status = AgentStatus(SPECS["omp"], StatusKind.INSTALLED, executable, "18.0.4", "openai")
            # 전역 프로젝트의 package.json에 의존성으로 등록되지 않았다면 Bun 전역 설치로 보지 않음.
            plan, reason = uninstall_plan(status, home=home)
            self.assertIsNone(plan)
            self.assertIn("판별하지 못했습니다", reason)
            (home / "package.json").write_text('{"dependencies":{"@oh-my-pi/pi-coding-agent":"^18.0.4"}}')
            plan, reason = uninstall_plan(status, home=home)
            self.assertEqual(reason, "")
            self.assertEqual(plan.command.argv, (str(bun), "remove", "--global", "@oh-my-pi/pi-coding-agent"))
            self.assertEqual(plan.paths, ())

    async def test_windows_bun_shim_uninstall_follows_bunx_sidecar(self):
        # Bun은 Windows에서 ~/.bun/bin/<이름>.exe shim과 대상 경로를 UTF-16으로 담은 <이름>.bunx를 둠.
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            bun_home = home / ".bun"
            (bun_home / "bin").mkdir(parents=True)
            (bun_home / "bin/bun.exe").write_bytes(b"MZ")
            root = bun_home / "install/global/node_modules/@opencode/cli"
            (root / "bin").mkdir(parents=True)
            (root / "package.json").write_text('{"name":"@opencode/cli"}')
            (root / "bin/opencode.exe").write_bytes(b"MZ")
            executable = bun_home / "bin/opencode.exe"
            executable.write_bytes(b"MZ")
            relative = PureWindowsPath("install/global/node_modules/@opencode/cli/bin/opencode.exe")
            (bun_home / "bin/opencode.bunx").write_bytes(f'{relative}"'.encode("utf-16-le") + b"\x00\x000\xab")
            shutil.copy(executable, bun_home / "bin/opencode2.exe")
            shutil.copy(bun_home / "bin/opencode.bunx", bun_home / "bin/opencode2.bunx")
            (bun_home / "bin/other.exe").write_bytes(b"MZ")
            (bun_home / "bin/other.bunx").write_bytes(f'{PureWindowsPath("install/global/node_modules/other/bin/other.exe")}"'.encode("utf-16-le"))
            shims = tuple(bun_home / "bin" / name for name in ("opencode.exe", "opencode.bunx", "opencode2.exe", "opencode2.bunx"))
            status = AgentStatus(SPECS["opencode"], StatusKind.INSTALLED, executable, "2.0.24", "openai")
            with patch("shutil.which", return_value=None):
                plan, reason = uninstall_plan(status, home=home)
            self.assertEqual(reason, "")
            self.assertEqual(plan.command.argv, (str(bun_home / "bin/bun.exe"), "remove", "--global", "@opencode/cli"))
            # bun remove --global은 Windows에서 shim을 남기므로 같은 패키지의 shim을 함께 삭제함.
            self.assertEqual(plan.paths, shims)
            self.assertTrue(plan.display.startswith(plan.command.display))
            self.assertIn("opencode2.bunx", plan.display)

            # 패키지가 이미 지워져 shim만 남은 경우에는 shim만 삭제함.
            shutil.rmtree(root)
            with patch("shutil.which", return_value=None):
                plan, reason = uninstall_plan(status, home=home)
            self.assertEqual(reason, "")
            self.assertIsNone(plan.command)
            self.assertEqual(plan.paths, shims)
            with patch("agents_status.core._clean_user_environment", return_value=[]):
                result = await run_uninstall(plan)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(any(shim.exists() for shim in shims))
            self.assertTrue((bun_home / "bin/other.exe").exists())
            self.assertTrue((bun_home / "bin/bun.exe").exists())

            executable.write_bytes(b"MZ")
            (bun_home / "bin/opencode.bunx").write_bytes(b"\x00\x00")
            with patch("shutil.which", return_value=None):
                plan, reason = uninstall_plan(status, home=home)
            self.assertIsNone(plan)
            self.assertIn("판별하지 못했습니다", reason)

    async def test_windows_npm_prefix_uninstall_uses_prefix_layout(self):
        # Windows npm 전역 설치는 <prefix>/<이름>.cmd shim과 <prefix>/node_modules/<패키지>를 둠.
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "nodejs"
            root = prefix / "node_modules/@openai/codex"
            root.mkdir(parents=True)
            (root / "package.json").write_text('{"name":"@openai/codex"}')
            executable = prefix / "codex.CMD"
            executable.write_text("@echo off")
            status = AgentStatus(SPECS["codex"], StatusKind.INSTALLED, executable, "1.0", "openai")
            expected = ("uninstall", "--global", "--prefix", str(prefix), "@openai/codex")

            with patch("shutil.which", return_value=str(prefix.parent / "other/npm.cmd")):
                plan, reason = uninstall_plan(status)
            self.assertEqual(reason, "")
            self.assertEqual(plan.command.argv, (str(prefix.parent / "other/npm.cmd"), *expected))

            (prefix / "npm.cmd").write_text("@echo off")
            with patch("shutil.which", return_value=str(prefix.parent / "other/npm.cmd")):
                plan, reason = uninstall_plan(status)
            self.assertEqual(plan.command.argv, (str(prefix / "npm.cmd"), *expected))

            with patch("shutil.which", return_value=None):
                command, reason = update_command(AgentStatus(SPECS["crush"], StatusKind.INSTALLED, prefix / "crush.cmd", "1.0", "-"))
            self.assertIsNone(command)
            self.assertIn("판별하지 못했습니다", reason)

    async def test_windows_npm_prefix_prefers_cmd_over_posix_script(self):
        # nvm-windows는 prefix에 POSIX 셸 스크립트 npm과 npm.cmd를 함께 두며 Windows에서는 npm.cmd만 실행 가능함.
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "nodejs"
            root = prefix / "node_modules/@opencode/cli"
            root.mkdir(parents=True)
            (root / "package.json").write_text('{"name":"@opencode/cli"}')
            executable = prefix / "opencode.CMD"
            executable.write_text("@echo off")
            for name in ("npm", "npm.cmd", "npm.ps1"):
                (prefix / name).write_text("")
            status = AgentStatus(SPECS["opencode"], StatusKind.INSTALLED, executable, "1.0", "-")
            with patch("shutil.which", return_value=None):
                plan, reason = uninstall_plan(status)
            self.assertEqual(reason, "")
            self.assertEqual(plan.command.argv, (str(prefix / "npm.cmd"), "uninstall", "--global", "--prefix", str(prefix), "@opencode/cli"))

    async def test_winget_portable_uninstall_uses_package_identifier(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            packages = home / "AppData/Local/Microsoft/WinGet/Packages"
            install_root = packages / "GitHub.Copilot_Microsoft.Winget.Source_8wekyb3d8bbwe"
            install_root.mkdir(parents=True)
            executable = install_root / "copilot.EXE"
            executable.write_bytes(b"MZ")
            (install_root / "copilot.exe.old").write_bytes(b"MZ")
            status = AgentStatus(SPECS["copilot"], StatusKind.INSTALLED, executable, "1.0.94", "github-copilot")
            winget = str(home / "winget.exe")
            with patch.dict(os.environ, {"LOCALAPPDATA": str(home / "AppData/Local")}):
                with patch("agents_status.core._which", side_effect=lambda name: winget if name == "winget" else None):
                    plan, reason = uninstall_plan(status, home=home)
                self.assertEqual(reason, "")
                self.assertEqual(plan.command.argv[:5], (winget, "uninstall", "--id", "GitHub.Copilot", "--exact"))
                self.assertIn("--purge", plan.command.argv)
                self.assertIn("--force", plan.command.argv)
                self.assertIn("--disable-interactivity", plan.command.argv)

                with patch("shutil.which", return_value=None):
                    plan, reason = uninstall_plan(status, home=home)
                self.assertIsNone(plan)
                self.assertIn("winget", reason)

                stray = packages / "copilot.exe"
                stray.write_bytes(b"MZ")
                with patch("shutil.which", return_value=winget):
                    plan, reason = uninstall_plan(AgentStatus(SPECS["copilot"], StatusKind.INSTALLED, stray, "1", "-"), home=home)
                self.assertIsNone(plan)
                self.assertIn("판별하지 못했습니다", reason)

    async def test_winget_self_updated_link_is_matched_to_package_directory(self):
        # copilot update는 WinGet Links의 심볼릭 링크를 실제 파일로 바꾸고 .old-<pid>-<time> 백업을 남김.
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            winget_root = home / "AppData/Local/Microsoft/WinGet"
            install_root = winget_root / "Packages/GitHub.Copilot_Microsoft.Winget.Source_8wekyb3d8bbwe"
            install_root.mkdir(parents=True)
            (install_root / "copilot.exe").write_bytes(b"MZ")
            (winget_root / "Packages/Starship.Starship_Microsoft.Winget.Source_8wekyb3d8bbwe").mkdir()
            links = winget_root / "Links"
            links.mkdir()
            executable = links / "copilot.EXE"
            executable.write_bytes(b"MZ")
            backup = links / "copilot.exe.old-53332-1791378375580"
            backup.write_bytes(b"MZ")
            (links / "starship.exe").write_bytes(b"MZ")
            status = AgentStatus(SPECS["copilot"], StatusKind.INSTALLED, executable, "1.0.95", "github-copilot")
            winget = str(home / "winget.exe")
            with patch.dict(os.environ, {"LOCALAPPDATA": str(home / "AppData/Local")}):
                with patch("agents_status.core._which", side_effect=lambda name: winget if name == "winget" else None):
                    plan, reason = uninstall_plan(status, home=home)
                self.assertEqual(reason, "")
                self.assertEqual(plan.command.argv[:5], (winget, "uninstall", "--id", "GitHub.Copilot", "--exact"))
                self.assertEqual(plan.paths, (executable.resolve(), backup))

                # 패키지 디렉터리에 같은 이름의 실행 파일이 없으면 WinGet 설치로 보지 않음.
                unknown = links / "other.exe"
                unknown.write_bytes(b"MZ")
                with patch("shutil.which", return_value=winget):
                    plan, reason = uninstall_plan(AgentStatus(SPECS["copilot"], StatusKind.INSTALLED, unknown, "1", "-"), home=home)
                self.assertIsNone(plan)
                self.assertIn("판별하지 못했습니다", reason)

    @unittest.skipUnless(sys.platform == "win32", "Windows 레지스트리 전용")
    async def test_windows_user_environment_cleanup(self):
        import winreg

        key_path = r"Software\agents-status-test\Environment"
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "hermes"
            data.mkdir()
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, f"C:\\keep;{data}\\hermes-agent\\venv\\Scripts;{data}\\bin\\;%SystemRoot%")
                winreg.SetValueEx(key, "HERMES_HOME", 0, winreg.REG_SZ, str(data))
                winreg.SetValueEx(key, "OTHER", 0, winreg.REG_SZ, str(data))
            try:
                with patch("agents_status.core._ENVIRONMENT_KEY", key_path), patch("agents_status.core._broadcast_environment_change") as broadcast:
                    notes = _clean_user_environment((data / "hermes-agent",), ("HERMES_HOME",))
                    self.assertEqual(len(notes), 1)
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                        self.assertEqual(winreg.QueryValueEx(key, "Path"), (f"C:\\keep;{data}\\bin\\;%SystemRoot%", winreg.REG_EXPAND_SZ))
                        self.assertEqual(winreg.QueryValueEx(key, "HERMES_HOME")[0], str(data))
                    notes = _clean_user_environment((data,), ("HERMES_HOME",))
                    self.assertEqual(len(notes), 2)
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                        self.assertEqual(winreg.QueryValueEx(key, "Path")[0], "C:\\keep;%SystemRoot%")
                        self.assertRaises(FileNotFoundError, winreg.QueryValueEx, key, "HERMES_HOME")
                        self.assertEqual(winreg.QueryValueEx(key, "OTHER")[0], str(data))
                    self.assertEqual(broadcast.call_count, 2)
                    self.assertEqual(_clean_user_environment((data,), ("HERMES_HOME",)), [])
            finally:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\agents-status-test")

    async def test_native_wrapper_install_allows_required_postinstall(self):
        for key, package in (("claude", "@anthropic-ai/claude-code"), ("opencode", "@opencode/cli")):
            for system in ("linux", "win32", "darwin"):
                with self.subTest(agent=key, system=system), patch("agents_status.core.sys.platform", system), patch("agents_status.core._which", return_value="/bin/npm"):
                    status = AgentStatus(SPECS[key], StatusKind.MISSING, None, "-", "-")
                    command, reason = install_command(status)
                    self.assertEqual(reason, "")
                    self.assertEqual(command.argv, (
                        "/bin/npm", "install", "--global",
                        f"--allow-scripts={package}", f"{package}@latest",
                    ))
                    self.assertIn(f"--allow-scripts={package}", command.display)

    async def test_install_command_and_missing_dependencies(self):
        status = AgentStatus(SPECS["codex"], StatusKind.CONFIG_ONLY, None, "-", "-")
        with patch("shutil.which", return_value="/bin/npm"):
            command, reason = install_command(status)
        self.assertEqual(command.argv, ("/bin/npm", "install", "--global", "@openai/codex@latest"))
        self.assertEqual(reason, "")
        with patch("shutil.which", return_value=None):
            command, reason = install_command(status)
        self.assertIsNone(command)
        self.assertIn("npm", reason)

    async def test_hermes_install_uses_platform_script(self):
        status = AgentStatus(SPECS["hermes"], StatusKind.MISSING, None, "-", "-")
        with patch("agents_status.core.sys.platform", "win32"), patch("shutil.which", side_effect=lambda name: r"C:\pwsh.exe" if name == "pwsh" else None):
            command, reason = install_command(status)
        self.assertEqual(reason, "")
        self.assertEqual(command.argv[:5], (r"C:\pwsh.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"))
        self.assertIn("install.ps1", command.argv[5])
        self.assertIn("-NonInteractive", command.argv[5])
        with patch("agents_status.core.sys.platform", "win32"), patch("shutil.which", return_value=None):
            command, reason = install_command(status)
        self.assertIsNone(command)
        self.assertIn("PowerShell", reason)
        with patch("agents_status.core.sys.platform", "linux"), patch("shutil.which", return_value="/bin/tool"):
            command, reason = install_command(status)
        self.assertEqual(command.argv[:3], ("/bin/tool", "-o", "pipefail"))
        self.assertIn("install.sh", command.argv[4])

    async def test_hermes_powershell_installer_propagates_failure(self):
        shells = [shell for name in ("powershell", "pwsh") if (shell := shutil.which(name))]
        if not shells:
            self.skipTest("PowerShell이 설치되지 않음")
        status = AgentStatus(SPECS["hermes"], StatusKind.MISSING, None, "-", "-")
        for shell in shells:
            for exitcode in (0, 17):
                with self.subTest(shell=shell, exitcode=exitcode):
                    with patch("agents_status.core.sys.platform", "win32"), patch("shutil.which", return_value=shell):
                        command, _ = install_command(status)
                    # 실제 다운로드/설치 없이 공식 스크립트의 LASTEXITCODE 실패 보고를 재현함.
                    installer = f"param([switch]$NonInteractive); if (-not $NonInteractive) {{ throw 'interactive' }}; $global:LASTEXITCODE = {exitcode}"
                    # irm은 별칭이므로 별칭이 가리키는 명령을 대체해야 실제 다운로드를 막을 수 있음.
                    stub = "function Invoke-RestMethod { '" + installer.replace("'", "''") + "' }; "
                    result = subprocess.run((*command.argv[:-1], stub + command.argv[-1]), capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, exitcode, result.stderr)
            stub = "function Invoke-RestMethod { throw 'download failed' }; "
            result = subprocess.run((*command.argv[:-1], stub + command.argv[-1]), capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)

    async def test_omp_install_uses_official_binary_installer(self):
        # npm 패키지는 실행에 Bun이 필요하므로 Bun 없이 동작하는 공식 설치 스크립트의 --binary 모드를 사용함.
        status = AgentStatus(SPECS["omp"], StatusKind.MISSING, None, "-", "-")
        tools = {"npm": "/bin/npm", "pwsh": r"C:\pwsh.exe", "curl": "/bin/curl", "bash": "/bin/bash"}
        with patch("agents_status.core.sys.platform", "win32"), patch("agents_status.core._which", side_effect=tools.get):
            command, reason = install_command(status)
        self.assertEqual(reason, "")
        self.assertEqual(command.argv[:5], (r"C:\pwsh.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"))
        self.assertIn("https://omp.sh/install.ps1", command.argv[5])
        self.assertIn("-Binary", command.argv[5])
        self.assertNotIn("bun", command.argv[5].lower())
        with patch("agents_status.core.sys.platform", "win32"), patch("agents_status.core._which", side_effect={"npm": "/bin/npm"}.get):
            command, reason = install_command(status)
        self.assertIsNone(command)
        self.assertIn("PowerShell", reason)
        with patch("agents_status.core.sys.platform", "linux"), patch("agents_status.core._which", side_effect=tools.get):
            command, reason = install_command(status)
        self.assertEqual(reason, "")
        self.assertEqual(command.argv[:4], ("/bin/bash", "-o", "pipefail", "-c"))
        self.assertEqual(command.argv[4], "curl -fsSL https://omp.sh/install | sh -s -- --binary")
        self.assertEqual(command.display, command.argv[4])
        with patch("agents_status.core.sys.platform", "linux"), patch("agents_status.core._which", side_effect={"npm": "/bin/npm", "bash": "/bin/bash"}.get):
            command, reason = install_command(status)
        self.assertIsNone(command)
        self.assertIn("curl", reason)


if __name__ == "__main__":
    unittest.main()
