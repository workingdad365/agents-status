import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from agents_status.app import AgentsStatusApp, ConfirmUpdate
from agents_status.core import AGENTS, AgentStatus, StatusKind, UpdateCommand, run_update, scan_agent, update_command


SPECS = {spec.key: spec for spec in AGENTS}


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
        self.assertEqual(command.argv, ("/opt/bin/opencode", "upgrade"))
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


if __name__ == "__main__":
    unittest.main()
