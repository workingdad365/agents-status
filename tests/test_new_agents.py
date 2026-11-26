import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from agents_status.core import (
    AGENTS, AgentStatus, StatusKind, UpdateCommand, config_paths, install_command,
    run_uninstall, run_update, scan_agent, uninstall_plan, update_command,
)
from agents_status.instructions import sync_instructions


SPECS = {spec.key: spec for spec in AGENTS}


class NewAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.process_environment = os.environ.copy()
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
                        if key == "qwen" and platform == "win32":
                            self.assertEqual(command.argv[0], "powershell")
                            self.assertIn(f"& '{status.executable}' update", command.argv[-1])
                        else:
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
                self.assertEqual(SPECS[key].provider_probe(self.home, env), "unknown")

    def test_qwen_provider_follows_selected_model_and_config_override(self):
        self.file(".qwen/settings.json", b'{"security":{"auth":{"selectedType":"qwen-oauth"}}}')
        root = self.home / "custom-qwen"
        env = {"QWEN_HOME": str(root), "PATH": ""}
        for provider in ("openai", "openai-responses"):
            config = {
                "security": {"auth": {"selectedType": provider}},
                "model": {"name": "moonshotai/kimi-k3.1-pro"},
                "modelProviders": {
                    "anthropic": [{"id": "moonshotai/kimi-k3.1-pro", "name": "[Wrong] other protocol"}],
                    provider: [
                        {"id": "other", "name": "[Wrong] inactive model"},
                        {"id": "moonshotai/kimi-k3.1-pro", "name": "[OpenRouter] moonshotai/kimi-k3.1-pro",
                         "baseUrl": "https://openrouter.ai/api/v1", "envKey": "OPENROUTER_API_KEY"},
                    ],
                },
            }
            with self.subTest(provider=provider):
                self.file("custom-qwen/settings.json", json.dumps(config).encode())
                status = scan_agent(SPECS["qwen"], home=self.home, env=env)
                self.assertEqual(status.status, StatusKind.CONFIG_ONLY)
                self.assertEqual(status.provider, f"{provider} (OpenRouter)")

    def test_qwen_provider_uses_only_leading_service_label(self):
        for name, expected in (
            ("[OpenRouter] model", "openai (OpenRouter)"),
            ("[ Custom Service ] model [Other]", "openai (Custom Service)"),
            ("model [OpenRouter]", "openai"),
            ("model", "openai"),
            ("[] model", "openai"),
            ("[   ] model", "openai"),
            ("[OpenRouter model", "openai"),
            (None, "openai"),
            (123, "openai"),
        ):
            config = {
                "security": {"auth": {"selectedType": "openai"}},
                "model": {"name": "chosen"},
                "modelProviders": {"openai": [{"id": "chosen", "name": name}]},
            }
            with self.subTest(name=name):
                self.file(".qwen/settings.json", json.dumps(config).encode())
                self.assertEqual(SPECS["qwen"].provider_probe(self.home, {}), expected)

    def test_qwen_provider_preserves_auth_type_without_matching_model(self):
        for overrides in (
            {"model": None},
            {"model": []},
            {"model": "chosen"},
            {"model": {}},
            {"model": {"name": 123}},
            {"model": {"name": ""}},
            {"model": {"name": "missing"}},
            {"modelProviders": None},
            {"modelProviders": []},
            {"modelProviders": {}},
            {"modelProviders": {"openai": None}},
            {"modelProviders": {"openai": {"id": "chosen", "name": "[Wrong] model"}}},
            {"modelProviders": {"openai": [None, "chosen", {}, {"id": "chosen"}]}},
            {"model": {}, "modelProviders": {"openai": [{"name": "[Wrong] model"}]}},
        ):
            config = {
                "security": {"auth": {"selectedType": "openai"}},
                "model": {"name": "chosen"},
                "modelProviders": {"openai": [{"id": "chosen", "name": "[OpenRouter] model"}]},
                **overrides,
            }
            with self.subTest(overrides=overrides):
                self.file(".qwen/settings.json", json.dumps(config).encode())
                self.assertEqual(SPECS["qwen"].provider_probe(self.home, {}), "openai")

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell 자식 프로세스의 압축 해제 검증")
    async def test_qwen_install_and_update_isolate_inherited_powershell_modules(self):
        shell = shutil.which("powershell.exe", path=self.process_environment.get("PATH", ""))
        if not shell:
            self.skipTest("Windows PowerShell이 없음")
        modules = self.home / "incompatible modules"
        self.file("incompatible modules/Microsoft.PowerShell.Archive/Microsoft.PowerShell.Archive.psd1",
                  b"@{ RootModule = 'Microsoft.PowerShell.Archive.psm1'; ModuleVersion = '99.0.0' }")
        self.file("incompatible modules/Microsoft.PowerShell.Archive/Microsoft.PowerShell.Archive.psm1",
                  b"function Expand-Archive { throw 'incompatible archive module' }")
        archive = self.home / "qwen.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("qwen-code/bin/qwen.cmd", "@echo off")
        launcher = self.file("Qwen user's tools/qwen.cmd", (
            '@echo off\r\n'
            'echo ARG:%~1\r\n'
            'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command '
            '"$ErrorActionPreference = \'Stop\'; '
            'Expand-Archive -LiteralPath $env:QWEN_TEST_ARCHIVE -DestinationPath $env:QWEN_TEST_EXTRACT -Force; '
            'exit ([int]$env:QWEN_TEST_EXIT_CODE)"\r\n'
            'exit /b %ERRORLEVEL%\r\n'
        ).encode())
        root = self.home / "Qwen user's install/custom lib/qwen-code"
        inner = self.file("Qwen user's install/custom lib/qwen-code/bin/qwen.cmd", b"@exit /b 91")
        self.file("Qwen user's install/custom lib/qwen-code/node/node.exe", b"MZ")
        self.file("Qwen user's install/custom lib/qwen-code/manifest.json",
                  b'{"name":"@qwen-code/qwen-code","runtime":"node","version":"1.2.3"}')
        wrapper = self.file("Qwen user's install/custom bin/qwen.cmd",
                            f'@echo off\ncall "{inner}" %*\n'.encode())
        environment = {
            **self.process_environment,
            "PSModulePath": str(modules),
            "QWEN_TEST_ARCHIVE": str(archive),
            "QWEN_TEST_VERSION": "8.7.6",
            "QWEN_INSTALL_ROOT": "original root",
        }
        body = (
            "Write-Output ('ARGS:' + ($args -join ',')); "
            "Write-Output ('ROOT:' + $env:QWEN_INSTALL_ROOT); "
            "Write-Output ('LIB:' + $env:QWEN_INSTALL_LIB_DIR); "
            "Write-Output ('BIN:' + $env:QWEN_INSTALL_BIN_DIR); "
            "& '" + str(launcher).replace("'", "''") + "'"
        )
        stub = (
            "function Invoke-RestMethod { param($Uri, $TimeoutSec); "
            "if ($Uri -like 'https://registry.npmjs.org/*') { return @{version=$env:QWEN_TEST_VERSION} }; "
            "'" + body.replace("'", "''") + "' }; "
        )
        for action in ("install", "update", "standalone-wrapper", "standalone-direct"):
            for code in (0, 7):
                with self.subTest(action=action, code=code):
                    destination = self.home / f"{action}-{code}"
                    env = {**environment, "QWEN_TEST_EXTRACT": str(destination), "QWEN_TEST_EXIT_CODE": str(code)}
                    with patch.dict(os.environ, env, clear=True), patch("agents_status.core._which", return_value=shell):
                        executable = {"standalone-wrapper": wrapper, "standalone-direct": inner}.get(action, launcher)
                        status = self.status("qwen", executable)
                        command, reason = (install_command if action == "install" else update_command)(status)
                        self.assertEqual(reason, "")
                        if action != "update":
                            command = UpdateCommand((*command.argv[:-1], stub + command.argv[-1]), command.display)
                        result = await run_update(command, timeout=30)
                        self.assertEqual(os.environ["PSModulePath"], str(modules))
                        self.assertEqual(os.environ["QWEN_INSTALL_ROOT"], "original root")
                    self.assertEqual(result.returncode, code, result.output)
                    self.assertEqual((destination / "qwen-code/bin/qwen.cmd").read_text(), "@echo off")
                    if action == "update":
                        self.assertIn("ARG:update", result.output)
                    elif action.startswith("standalone"):
                        bin_dir = wrapper.parent if action == "standalone-wrapper" else root.parent / "bin"
                        self.assertIn("ARGS:--method,standalone,--version,8.7.6,--no-modify-path", result.output)
                        for prefix, expected in (("ROOT:", root.parent), ("LIB:", root), ("BIN:", bin_dir)):
                            value = next(line.removeprefix(prefix) for line in result.output.splitlines() if line.startswith(prefix))
                            self.assertEqual(Path(value).resolve(), expected.resolve())
        for version in ("", "latest", "1.2.3; Write-Output invalid", "1.2.3", "1.2.2"):
            with self.subTest(no_install_version=version):
                env = {**environment, "QWEN_TEST_VERSION": version}
                with patch.dict(os.environ, env, clear=True), patch("agents_status.core._which", return_value=shell):
                    command, _ = update_command(self.status("qwen", wrapper))
                    command = UpdateCommand((*command.argv[:-1], stub + command.argv[-1]), command.display)
                    result = await run_update(command, timeout=30)
                if version in ("1.2.3", "1.2.2"):
                    self.assertEqual(result.returncode, 0, result.output)
                    self.assertIn("is up to date", result.output)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Invalid Qwen Code release version", result.output)
                self.assertNotIn("ARGS:", result.output)

    def test_qoder_provider_follows_selected_model_and_config_override(self):
        self.file(".qoder/settings.json", b'{"model":{"name":"openai/other"}}')
        root = self.home / "custom-qoder"
        env = {"QODER_CONFIG_DIR": str(root), "PATH": ""}
        cases = (
            ({"model": {"name": "openrouter/pg/deepseek/deepseek-v4.1-flash"},
              "security": {"auth": {"selectedType": "qoder-browser"}}}, "openrouter"),
            ({"model": {"name": "anthropic/claude-sonnet"}}, "anthropic"),
            ({"model": {"name": "auto", "preferences": {"openrouter/old": {}}}}, "qoder"),
            ({"model": {"name": "efficient"}}, "qoder"),
            ({}, "qoder"),
            ({"model": {"name": "/model"}}, "unknown"),
            ({"model": {"name": "openrouter/"}}, "unknown"),
            ({"model": {"name": 123}}, "unknown"),
            ({"model": None}, "unknown"),
            ([], "unknown"),
        )
        for config, expected in cases:
            with self.subTest(config=config):
                self.file("custom-qoder/settings.json", json.dumps(config).encode())
                status = scan_agent(SPECS["qoder"], home=self.home, env=env)
                self.assertEqual(status.provider, expected)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell 설치 후 PATH 검증")
    def test_qoder_install_repairs_long_path_without_duplicates_or_failed_install_changes(self):
        path = self.process_environment.get("PATH", "")
        shell = shutil.which("pwsh.exe", path=path) or shutil.which("powershell.exe", path=path)
        if not shell:
            candidate = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
            shell = str(candidate) if candidate.is_file() else None
        if not shell:
            self.skipTest("PowerShell이 없음")
        self.file(".qoder/bin/qodercli/qodercli.exe", b"MZ")
        self.file(".qoder/entry/qoder.cmd", b"@echo off")
        with patch("agents_status.core._which", return_value=shell):
            command, reason = install_command(self.status("qoder", None))
        self.assertEqual(reason, "")
        # 사용자 레지스트리 대신 자식 프로세스 PATH를 사용하고 공식 설치 응답을 대체함.
        script = command.argv[-1].replace("'User'", "'Process'")
        script = script.removesuffix("exit $LASTEXITCODE") + "Write-Output ('RESULT:' + $env:PATH); exit $LASTEXITCODE"
        original = ";".join(str(self.home / ("existing" + str(i))) for i in range(100))
        self.assertGreater(len(original), 2047)
        expected = original + ";" + str(self.home / ".qoder/bin/qodercli") + ";" + str(self.home / ".qoder/entry")
        for initial, code, result in ((original, 0, expected), (expected, 0, expected), (original, 7, None)):
            with self.subTest(code=code, already_registered=initial == expected):
                # PowerShell 초기화에는 실제 사용자 프로필을 유지하고 테스트 경로는 시작 후 주입함.
                stub = (
                    "Import-Module Microsoft.PowerShell.Utility; "
                    "Import-Module Microsoft.PowerShell.Management; "
                    f"$env:USERPROFILE = '{str(self.home).replace(chr(39), chr(39) * 2)}'; "
                    f"$env:PATH = '{initial.replace(chr(39), chr(39) * 2)}'; "
                    f"function Invoke-RestMethod {{ '$global:LASTEXITCODE = {code}' }}; "
                )
                process = subprocess.run(
                    (command.argv[0], "-NonInteractive", *command.argv[1:-1], stub + script),
                    capture_output=True, text=True, errors="replace", timeout=30,
                    env=self.process_environment,
                )
                self.assertEqual(process.returncode, code, process.stderr)
                if result is None:
                    self.assertNotIn("RESULT:", process.stdout)
                else:
                    self.assertIn("RESULT:" + result, process.stdout)

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
