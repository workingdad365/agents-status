import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from textual.widgets import Button, DataTable

from agents_status.app import AgentsStatusApp, ConfirmDelete, ConfirmUpdate
from agents_status.core import (
    AGENTS, AgentStatus, CommandResult, StatusKind, UninstallPlan, UpdateCommand,
    config_paths, install_command, run_uninstall, run_update, scan_agent, uninstall_plan, update_command,
)


SPECS = {spec.key: spec for spec in AGENTS}


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

    def test_omp_and_agy_self_update(self):
        for key, binary in (("omp", "omp"), ("antigravity", "agy")):
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
                    executable.symlink_to(target)
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
            config.symlink_to(target)
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
                executable.symlink_to(target)
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
            executable.symlink_to(binary)
            config = home / ".claude"
            config.mkdir()
            status = AgentStatus(SPECS["claude"], StatusKind.INSTALLED, executable, "1", "-")
            plan, _ = uninstall_plan(status, home=home)
            result = await run_uninstall(plan)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(root.exists())
            self.assertFalse(executable.is_symlink())
            self.assertTrue(config.exists())

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


if __name__ == "__main__":
    unittest.main()
