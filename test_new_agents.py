import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents_status.core import (
    AGENTS, AgentStatus, StatusKind, config_paths, install_command,
    run_uninstall, scan_agent, uninstall_plan, update_command,
)
from agents_status.instructions import sync_instructions


SPECS = {spec.key: spec for spec in AGENTS}


class NewAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def file(self, relative, data=b"\x7fELF\x02"):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o755)
        return path

    def status(self, key, executable):
        return AgentStatus(SPECS[key], StatusKind.INSTALLED, executable, "1.0", "-")

    def test_official_install_and_noninteractive_update(self):
        for key, host, args in (
            ("kimi", "code.kimi.com", ("upgrade", "--yes")),
            ("qwen", "qwen-code-assets.oss-cn-hangzhou.aliyuncs.com", ("update",)),
            ("qoder", "qoder.com", ("update",)),
        ):
            with self.subTest(key=key), patch("agents_status.core._which", side_effect=lambda name: name):
                status = self.status(key, self.home / SPECS[key].binaries[0])
                for platform in ("linux", "darwin", "win32"):
                    with patch("agents_status.core.sys.platform", platform):
                        command, reason = install_command(status)
                        self.assertEqual(reason, "")
                        self.assertIn(host, command.display)
                        self.assertIn("LASTEXITCODE" if platform == "win32" else "pipefail", " ".join(command.argv))
                command, reason = update_command(status)
                self.assertEqual(reason, "")
                self.assertEqual(command.argv[1:], args)
            with patch("agents_status.core._which", return_value=None):
                self.assertIsNone(install_command(status)[0])

    def test_config_provider_and_instruction_overrides(self):
        source = self.file("global.md", b"# Instructions\n")
        for key, variable, filename, config, expected in (
            ("kimi", "KIMI_CODE_HOME", "config.toml",
             b'default_model = "chosen"\n[models.other]\nprovider="wrong"\n[models.chosen]\nprovider="anthropic"', "anthropic"),
            ("qwen", "QWEN_HOME", "settings.json",
             b'{"security":{"auth":{"selectedType":"openai-responses"}}}', "openai-responses"),
            ("qoder", "QODER_CONFIG_DIR", "settings.json", b'{}', "qoder"),
        ):
            root = self.home / key
            self.file(f"{key}/{filename}", config)
            env = {variable: str(root), "PATH": ""}
            with self.subTest(key=key), patch.dict(os.environ, env):
                status = scan_agent(SPECS[key], home=self.home, env=env)
                self.assertEqual(status.status, StatusKind.CONFIG_ONLY)
                self.assertEqual(status.provider, expected)
                self.assertEqual(config_paths(SPECS[key], home=self.home), (root,))
                result = sync_instructions(source, (SPECS[key],), home=self.home, env=env)[0]
                self.assertEqual(result.outcome, "copy")
                self.assertEqual(result.destination.read_bytes(), source.read_bytes())
                self.file(f"{key}/{filename}", b"invalid")
                if key != "qoder":
                    self.assertEqual(SPECS[key].provider_probe(self.home, env), "unknown")

    def test_install_location_scan_without_path(self):
        for platform in ("linux", "win32"):
            paths = {
                "kimi": ".kimi-code/bin/kimi" + (".exe" if platform == "win32" else ""),
                "qwen": "AppData/Local/qwen-code/bin/qwen.cmd" if platform == "win32" else ".local/bin/qwen",
                "qoder": ".local/bin/qodercli" + (".exe" if platform == "win32" else ""),
            }
            for key, relative in paths.items():
                executable = self.file(relative)
                with self.subTest(platform=platform, key=key), patch("agents_status.core.sys.platform", platform), patch("agents_status.core._which", return_value=None), patch("agents_status.core._version", return_value="1.2.3"):
                    status = scan_agent(SPECS[key], home=self.home, env={})
                    self.assertEqual(status.executable, executable)
                    self.assertEqual(status.version, "1.2.3")

    async def test_kimi_uninstall_preserves_settings_and_other_tools(self):
        binary = self.file(".kimi-code/bin/kimi")
        backup = self.file(".kimi-code/bin/kimi.bak")
        tool = self.file(".kimi-code/bin/fd")
        settings = self.file(".kimi-code/config.toml", b"keep")
        plan, reason = uninstall_plan(self.status("kimi", binary), home=self.home)
        self.assertEqual(reason, "")
        self.assertEqual((await run_uninstall(plan)).returncode, 0)
        self.assertFalse(binary.exists())
        self.assertFalse(backup.exists())
        self.assertTrue(tool.exists())
        self.assertEqual(settings.read_bytes(), b"keep")

    async def test_qwen_standalone_uninstall_preserves_settings_and_shared_bin(self):
        root = self.home / ".local/lib/qwen-code"
        self.file(".local/lib/qwen-code/bin/qwen", b"#!/bin/sh\n")
        self.file(".local/lib/qwen-code/node/bin/node")
        self.file(".local/lib/qwen-code/manifest.json", json.dumps({"name": "@qwen-code/qwen-code", "runtime": "node"}).encode())
        launcher = self.file(".local/bin/qwen", f'#!/usr/bin/env sh\nexec \'{root}/bin/qwen\' "$@"\n'.encode())
        other = self.file(".local/bin/other")
        settings = self.file(".qwen/settings.json", b"{}")
        plan, reason = uninstall_plan(self.status("qwen", launcher), home=self.home)
        self.assertEqual(reason, "")
        self.assertEqual((await run_uninstall(plan)).returncode, 0)
        self.assertFalse(launcher.exists())
        self.assertFalse(root.exists())
        self.assertTrue(other.exists())
        self.assertTrue(settings.exists())

    def test_unrecognized_qwen_wrapper_is_not_deleted(self):
        launcher = self.file(".local/bin/qwen", b'#!/bin/sh\nexec /usr/bin/node "$@"\n')
        self.assertIsNone(uninstall_plan(self.status("qwen", launcher), home=self.home)[0])

    def test_windows_native_qoder_uninstall(self):
        binary = self.file(".local/bin/qodercli.exe", b"MZ\x00\x00")
        with patch("agents_status.core.sys.platform", "win32"):
            plan, reason = uninstall_plan(self.status("qoder", binary), home=self.home)
        self.assertEqual(reason, "")
        self.assertEqual(plan.paths, (binary,))

    async def test_windows_qoder_without_path_detects_install_and_removes_versions(self):
        binary = self.file(".qoder/bin/qodercli/qodercli.exe", b"MZ\x00\x00")
        version = self.file(".qoder/bin/qodercli/qodercli-1.1.73.exe", b"MZ\x00\x00")
        settings = self.file(".qoder/settings.json", b"{}")
        other = self.file(".qoder/bin/other.exe", b"MZ\x00\x00")
        with patch("agents_status.core.sys.platform", "win32"), patch("agents_status.core._which", return_value=None), patch("agents_status.core._version", return_value="1.1.73"):
            status = scan_agent(SPECS["qoder"], home=self.home, env={"PATH": ""})
            self.assertEqual(status.status, StatusKind.INSTALLED)
            self.assertEqual(status.executable, binary)
            self.assertEqual(status.version, "1.1.73")
            command, reason = update_command(status)
            self.assertEqual(reason, "")
            self.assertEqual(command.argv, (str(binary), "update"))
            plan, reason = uninstall_plan(status, home=self.home)
            self.assertEqual(reason, "")
            self.assertEqual(plan.paths, (binary.parent,))
            with patch("agents_status.core._clean_user_environment", return_value=[]):
                self.assertEqual((await run_uninstall(plan)).returncode, 0)
            self.assertEqual(scan_agent(SPECS["qoder"], home=self.home, env={"PATH": ""}).status, StatusKind.CONFIG_ONLY)
        self.assertFalse(binary.exists())
        self.assertFalse(version.exists())
        self.assertEqual(settings.read_bytes(), b"{}")
        self.assertTrue(other.exists())

    def test_npm_install_uses_original_prefix_for_update_and_uninstall(self):
        for key, package in (("kimi", "@moonshot-ai/kimi-code"), ("qwen", "@qwen-code/qwen-code"), ("qoder", "@qoder-ai/qodercli")):
            prefix = self.home / key
            binary = self.file(f"{key}/{SPECS[key].binaries[0]}.cmd", b"@echo off")
            npm = self.file(f"{key}/npm.cmd", b"@echo off")
            self.file(f"{key}/node_modules/{package}/package.json", json.dumps({"name": package}).encode())
            with self.subTest(key=key):
                status = self.status(key, binary)
                command, reason = update_command(status)
                self.assertEqual(reason, "")
                self.assertEqual(command.argv, (str(npm), "install", "--global", "--prefix", str(prefix), package + "@latest"))
                plan, reason = uninstall_plan(status, home=self.home)
                self.assertEqual(reason, "")
                self.assertEqual(plan.command.argv, (str(npm), "uninstall", "--global", "--prefix", str(prefix), package))

    @unittest.skipUnless(os.name == "nt", "Windows cmd 경로 검증")
    async def test_windows_qwen_standalone_uninstall(self):
        root = self.home / "qwen-code/lib/qwen-code"
        self.file("qwen-code/lib/qwen-code/bin/qwen.cmd", b"@echo off")
        self.file("qwen-code/lib/qwen-code/node/node.exe", b"MZ\x00\x00")
        self.file("qwen-code/lib/qwen-code/manifest.json", b'{"name":"@qwen-code/qwen-code","runtime":"node"}')
        launcher = self.file("qwen-code/bin/qwen.cmd", f'@echo off\ncall "{root}\\bin\\qwen.cmd" %*\n'.encode())
        plan, reason = uninstall_plan(self.status("qwen", launcher), home=self.home)
        self.assertEqual(reason, "")
        self.assertEqual((await run_uninstall(plan)).returncode, 0)
        self.assertFalse(launcher.exists())
        self.assertFalse(root.exists())

    async def test_qoder_uninstall_removes_versions_but_preserves_settings(self):
        target = self.file(".qoder/bin/qodercli/qodercli-1.1.73")
        old = self.file(".qoder/bin/qodercli/qodercli-1.1.72")
        settings = self.file(".qoder/settings.json", b"{}")
        link = self.home / ".local/bin/qodercli"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(target)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows 심볼릭 링크 생성 권한이 없음")
            raise
        plan, reason = uninstall_plan(self.status("qoder", link), home=self.home)
        self.assertEqual(reason, "")
        self.assertEqual((await run_uninstall(plan)).returncode, 0)
        self.assertFalse(link.is_symlink())
        self.assertFalse(old.exists())
        self.assertTrue(settings.exists())
