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

from agents_status.app import AgentsStatusApp, ConfirmDelete, ConfirmUpdate, platform_name
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
                    self.assertEqual(run.call_args.args[0].argv, ("/bin/npm", "install", "--global", f"{package}@latest"))

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
            (release / "bin/codex").touch()
            (release / "bin/codex-code-mode-host").touch()
            current = packages / "standalone/current"
            symlink_or_skip(current, release)
            (packages / "app-server-daemon/releases").mkdir(parents=True)
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
            self.assertEqual(plan.paths, (executable, host, packages / "standalone", packages / "app-server-daemon"))
            result = await run_uninstall(plan)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(packages.joinpath("standalone").exists())
            self.assertFalse(packages.joinpath("app-server-daemon").exists())
            self.assertFalse(executable.is_symlink())
            self.assertFalse(host.is_symlink())
            self.assertTrue(unrelated.exists())
            self.assertTrue(config.exists())

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
                with patch("shutil.which", side_effect=lambda name: winget if name == "winget" else None):
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

    async def test_omp_install_requires_bun_runtime(self):
        status = AgentStatus(SPECS["omp"], StatusKind.MISSING, None, "-", "-")
        for platform, runtime in (("win32", "bun.exe"), ("linux", "bun")):
            for available in (False, True):
                with self.subTest(platform=platform, available=available):
                    # Windows에서 bun.cmd만 발견되는 경우도 런타임 누락으로 처리함.
                    tools = {"npm": "/bin/npm", "bun": "/bin/bun.cmd"} if platform == "win32" else {"npm": "/bin/npm"}
                    if available:
                        tools[runtime] = f"/bin/{runtime}"
                    with patch("agents_status.core.sys.platform", platform), patch("shutil.which", side_effect=tools.get):
                        command, reason = install_command(status)
                    if available:
                        self.assertEqual(command.argv, (
                            "/bin/npm", "install", "--global", "@oh-my-pi/pi-coding-agent@latest",
                        ))
                        self.assertEqual(reason, "")
                    else:
                        self.assertIsNone(command)
                        self.assertIn("Bun", reason)
                        self.assertIn("PATH", reason)


if __name__ == "__main__":
    unittest.main()
