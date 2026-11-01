import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from agents_status.core import (
    AGENTS, AgentStatus, CommandResult, StatusKind, run_uninstall, uninstall_plan, update_command,
)


SPECS = {spec.key: spec for spec in AGENTS}


class NativePathAliasTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.alias = self.root / "home-alias"
        self.symlink(self.alias, self.home)
        environment = patch.dict(os.environ, {
            "LOCALAPPDATA": str(self.alias / "AppData/Local"),
            "ProgramFiles": str(self.alias / "Program Files"),
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        for target, value in (("_which", None), ("_clean_user_environment", [])):
            mock = patch(f"agents_status.core.{target}", return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def symlink(self, link, target):
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows 심볼릭 링크 생성 권한이 없음")
            raise

    def file(self, path, content=b"\x7fELF\x02"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def status(self, key, executable):
        return AgentStatus(SPECS[key], StatusKind.INSTALLED, executable, "1.0", "-")

    async def test_codex_alias_update_and_uninstall_preserve_unrelated_host_and_settings(self):
        for custom_home in (False, True):
            with self.subTest(custom_home=custom_home):
                codex_home = self.alias / ("custom-codex" if custom_home else ".codex")
                environment = {"CODEX_HOME": str(codex_home)} if custom_home else {}
                with patch.dict(os.environ, environment):
                    packages = codex_home / "packages"
                    target = self.file(packages / "standalone/1.0/codex")
                    settings = self.file(codex_home / "config.toml", b'model = "keep"')
                    link = self.alias / ".local/bin/codex"
                    self.symlink(link, target)
                    unrelated = self.file(self.home / "unrelated/host")
                    host = link.with_name("codex-code-mode-host")
                    self.symlink(host, unrelated)
                    status = self.status("codex", link)
                    command, reason = update_command(status, home=self.alias)
                    self.assertEqual(reason, "")
                    self.assertEqual(command.argv, (str(link), "update"))
                    plan, reason = uninstall_plan(status, home=self.alias)
                    self.assertEqual(reason, "")
                    self.assertEqual(plan.paths[0], link)
                    self.assertNotIn(host, plan.paths)
                    result = await run_uninstall(plan)
                    self.assertEqual(result.returncode, 0)
                    self.assertFalse(link.is_symlink())
                    self.assertFalse((packages / "standalone").exists())
                    self.assertTrue(host.is_symlink())
                    self.assertEqual(unrelated.read_bytes(), b"\x7fELF\x02")
                    self.assertEqual(settings.read_bytes(), b'model = "keep"')
                    host.unlink()

    async def test_claude_alias_removes_install_but_not_home_or_settings(self):
        target = self.file(self.home / ".local/share/claude/versions/1.0")
        settings = self.file(self.home / ".claude/settings.json", b"{}")
        shared = self.file(self.home / ".local/bin/other")
        executable = self.alias / ".local/bin/claude"
        self.symlink(executable, target)
        plan, reason = uninstall_plan(self.status("claude", executable), home=self.alias)
        self.assertEqual(reason, "")
        self.assertEqual(plan.paths[0], executable)
        result = await run_uninstall(plan)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(executable.is_symlink())
        self.assertFalse(target.exists())
        self.assertTrue(self.alias.is_symlink())
        self.assertEqual(settings.read_bytes(), b"{}")
        self.assertTrue(shared.is_file())

    async def test_opencode_alias_unlinks_entrypoint_without_deleting_target(self):
        target = self.file(self.home / "shared/opencode")
        executable = self.alias / ".opencode/bin/opencode"
        self.symlink(executable, target)
        plan, reason = uninstall_plan(self.status("opencode", executable), home=self.home)
        self.assertEqual(reason, "")
        self.assertEqual(plan.paths, (executable,))
        result = await run_uninstall(plan)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(executable.is_symlink())
        self.assertTrue(target.is_file())

    def test_alias_does_not_accept_unrelated_installations(self):
        for key in ("claude", "codex", "opencode", "kilo", "hermes"):
            with self.subTest(key=key):
                executable = self.file(self.alias / "unrelated" / key)
                status = self.status(key, executable)
                plan, reason = uninstall_plan(status, home=self.alias)
                self.assertIsNone(plan)
                self.assertTrue(reason)
                self.assertTrue(executable.is_file())
                if key == "codex":
                    command, reason = update_command(status, home=self.alias)
                    self.assertIsNone(command)
                    self.assertTrue(reason)

    async def test_hermes_aliased_shim_preserves_other_checkout_companion(self):
        checkout = self.home / ".hermes/hermes-agent"
        (checkout / "hermes_cli").mkdir(parents=True)
        target = self.file(checkout / "venv/bin/hermes", b"#!/usr/bin/python\n")
        shim = self.file(self.alias / ".local/bin/hermes", f'exec "{target.as_posix()}" "$@"\n'.encode())
        companion = self.alias / ".local/bin/hermes-agent"
        self.symlink(companion, target)
        unrelated = self.home / "other/hermes-agent"
        (unrelated / "hermes_cli").mkdir(parents=True)
        other = self.file(unrelated / "venv/bin/hermes-acp", b"#!/usr/bin/python\n")
        acp = self.alias / ".local/bin/hermes-acp"
        self.symlink(acp, other)
        settings = self.file(self.home / ".hermes/config.yaml", b"keep: true")
        with patch("agents_status.core.sys.platform", "linux"):
            plan, reason = uninstall_plan(self.status("hermes", shim), home=self.alias)
        self.assertEqual(reason, "")
        self.assertEqual(plan.paths, (shim, companion, checkout))
        result = await run_uninstall(plan)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(shim.exists())
        self.assertFalse(companion.is_symlink())
        self.assertFalse(checkout.exists())
        self.assertTrue(acp.is_symlink())
        self.assertTrue(other.is_file())
        self.assertEqual(settings.read_bytes(), b"keep: true")

    def test_hermes_direct_entrypoint_alias_does_not_duplicate_checkout_removal(self):
        checkout = self.home / ".hermes/hermes-agent"
        (checkout / "hermes_cli").mkdir(parents=True)
        self.file(checkout / "venv/Scripts/hermes.exe", b"MZ\x00\x00")
        executable = self.alias / ".hermes/hermes-agent/venv/Scripts/hermes.exe"
        with patch("agents_status.core.sys.platform", "win32"):
            plan, reason = uninstall_plan(self.status("hermes", executable), home=self.alias)
        self.assertEqual(reason, "")
        self.assertEqual(plan.paths, (checkout,))

    async def test_hermes_hardlinked_companion_is_removed_but_shared_tool_is_kept(self):
        data = self.home / "hermes"
        checkout = data / "hermes-agent"
        (checkout / "hermes_cli").mkdir(parents=True)
        self.file(checkout / "venv/Scripts/hermes.exe", b"MZ")
        executable = self.file(data / "bin/hermes.exe", b"MZ")
        companion = executable.with_name("hermes-acp.exe")
        os.link(executable, companion)
        shared = self.file(executable.with_name("uv.exe"), b"MZ")
        with patch("agents_status.core.sys.platform", "win32"):
            plan, reason = uninstall_plan(self.status("hermes", executable), home=self.home)
        self.assertEqual(reason, "")
        result = await run_uninstall(plan)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(executable.exists())
        self.assertFalse(companion.exists())
        self.assertFalse(checkout.exists())
        self.assertTrue(shared.is_file())

    async def test_winget_alias_detects_package_and_cleans_only_self_updated_link(self):
        for environment, prefix in (("LOCALAPPDATA", "AppData/Local/Microsoft"), ("ProgramFiles", "Program Files")):
            with self.subTest(environment=environment):
                root = self.home / prefix / "WinGet"
                package = self.file(root / "Packages/GitHub.Copilot_Source/copilot.EXE", b"MZ\x00\x00")
                executable = self.alias / prefix / "WinGet/Links/copilot.EXE"
                self.symlink(executable, package)
                status = self.status("copilot", executable)
                with patch("agents_status.core._which", return_value="winget"):
                    plan, reason = uninstall_plan(status, home=self.alias)
                self.assertEqual(reason, "")
                self.assertEqual(plan.command.argv[1:4], ("uninstall", "--id", "GitHub.Copilot"))
                self.assertEqual(plan.paths, ())
                executable.unlink()
                executable.write_bytes(b"MZ\x00\x00")
                backup = self.file(executable.with_name("copilot.exe.old-1"))
                shared = self.file(executable.with_name("other.exe"))
                with patch("agents_status.core._which", return_value="winget"):
                    plan, reason = uninstall_plan(status, home=self.alias)
                self.assertEqual(reason, "")
                with patch("agents_status.core.run_update", new_callable=AsyncMock, return_value=CommandResult(0, "")) as command:
                    result = await run_uninstall(plan)
                command.assert_awaited_once_with(plan.command, on_output=None)
                self.assertEqual(result.returncode, 0)
                self.assertFalse(executable.exists())
                self.assertFalse(backup.exists())
                self.assertTrue(shared.is_file())
                self.assertTrue(package.is_file())

    async def test_native_install_roots_accept_aliases_and_keep_sibling_settings(self):
        cases = (
            ("claude", ".local/bin/claude.exe", ".local/bin/claude.exe", ".claude/settings.json"),
            ("kilo", ".kilo/bin/kilo.exe", ".kilo/bin", ".kilo/config.json"),
            ("omp", "AppData/Local/omp/omp.exe", "AppData/Local/omp", ".omp/settings.json"),
            ("antigravity", "AppData/Local/agy/bin/agy.exe", "AppData/Local/agy", ".antigravity/settings.json"),
        )
        for key, binary, removed, config in cases:
            with self.subTest(key=key):
                executable = self.file(self.alias / binary, b"MZ\x00\x00")
                settings = self.file(self.home / config, b"keep")
                shared = self.file(self.home / "AppData/Local/shared/tool.exe")
                with patch("agents_status.core.sys.platform", "win32"):
                    plan, reason = uninstall_plan(self.status(key, executable), home=self.home)
                self.assertEqual(reason, "")
                result = await run_uninstall(plan)
                self.assertEqual(result.returncode, 0)
                self.assertFalse((self.home / removed).exists())
                self.assertEqual(settings.read_bytes(), b"keep")
                self.assertTrue(shared.is_file())
