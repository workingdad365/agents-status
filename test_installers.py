"""네트워크 없이 POSIX 설치/검증/롤백 계약을 점검함."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parent


@unittest.skipUnless(os.name == "posix" and all(shutil.which(name) for name in ("sh", "unzip", "zipinfo", "curl")), "POSIX 설치 도구 필요")
class PosixInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="agents status installer ")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.assets = self.home / "assets"
        self.assets.mkdir()
        self.tools = self.home / "tools"
        self.tools.mkdir()
        for name in ("unzip", "zipinfo"):
            (self.tools / name).symlink_to(shutil.which(name))
        self.root = self.home / ".local/share/agents-status"
        self.env = dict(os.environ, HOME=str(self.home), PATH=f"{self.tools}{os.pathsep}/usr/bin{os.pathsep}/bin", TEST_ASSETS=str(self.assets))
        self.executable("uname", "#!/bin/sh\ncase \"$1\" in -s) echo Linux;; -m) echo x86_64;; esac\n")
        self.executable("curl", f"#!{sys.executable}\n" + "import os, pathlib, shutil, sys\nargs = sys.argv[1:]\nurl = next(a for a in args if a.startswith('https://'))\nif url.endswith('/latest'):\n print('https://github.com/workingdad365/agents-status-releases/releases/tag/v1.0.12', end='')\nelse:\n shutil.copyfile(pathlib.Path(os.environ['TEST_ASSETS']) / url.rsplit('/', 1)[1], args[args.index('-o') + 1])\n")

    def executable(self, name: str, content: str):
        destination = self.tools / name
        destination.write_text(content, encoding="utf-8")
        destination.chmod(0o755)

    def archive(self, *, unsafe: bool = False, fail_after_activation: bool = False):
        name = "agents-status-1.0.12-linux-x86_64.zip"
        executable = "#!/bin/sh\ncase \"$1\" in --version) echo 'agents-status 1.0.12';; --self-check) "
        if fail_after_activation:
            executable += "case \"$0\" in */versions/*) exit 9;; esac; "
        executable += "exit 0;; *) exit 2;; esac\n"
        with zipfile.ZipFile(self.assets / name, "w") as archive:
            for path, data in [
                ("agents-status/agents-status", executable),
                ("agents-status/_internal/resource", "bundled resource"),
            ] + ([("agents-status/../../escaped", "bad")] if unsafe else []):
                info = zipfile.ZipInfo(path)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o755) << 16
                archive.writestr(info, data)
        checksum = hashlib.sha256((self.assets / name).read_bytes()).hexdigest()
        (self.assets / "SHA256SUMS").write_text(f"{checksum}  {name}\n", encoding="ascii")

    def install(self, *arguments):
        return subprocess.run(["sh", str(ROOT / "install.sh"), "--no-path", *arguments], env=self.env, capture_output=True, text=True, timeout=30)

    def test_latest_installs_with_space_in_home(self):
        self.archive()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "current.json").read_text().strip(), '{"version":"1.0.12"}')
        launcher = self.home / ".local/bin/agents-status"
        result = subprocess.run([str(launcher), "--version"], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "agents-status 1.0.12")
        self.assertFalse((self.root / ".update-lock").exists())

    def test_bad_checksum_never_activates(self):
        self.archive()
        with (self.assets / "agents-status-1.0.12-linux-x86_64.zip").open("ab") as stream:
            stream.write(b"corrupt")
        result = self.install("--version", "1.0.12")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "current.json").exists())
        self.assertFalse((self.root / ".update-lock").exists())

    def test_traversal_rejected_even_with_matching_checksum(self):
        self.archive(unsafe=True)
        result = self.install("--version", "1.0.12")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "current.json").exists())
        self.assertFalse((self.home / "escaped").exists())

    def test_failed_launcher_smoke_restores_selector_and_launcher(self):
        self.archive(fail_after_activation=True)
        self.root.mkdir(parents=True)
        previous = '{"version":"1.0.11"}\n'
        (self.root / "current.json").write_text(previous)
        launcher = self.home / ".local/bin/agents-status"
        launcher.parent.mkdir(parents=True)
        previous_launcher = "#!/bin/sh\n# agents-status managed launcher\nexit 0\n"
        launcher.write_text(previous_launcher)
        launcher.chmod(0o755)
        result = self.install("--version", "1.0.12")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "current.json").read_text(), previous)
        self.assertEqual(launcher.read_text(), previous_launcher)
        self.assertFalse((self.root / "versions/1.0.12").exists())
        self.assertFalse((self.root / ".update-lock").exists())

    def test_migration_requires_opt_in_and_preserves_old_tool(self):
        self.archive()
        source = self.home / "old-source"
        source.mkdir()
        old_tool = source / "agents-status"
        old_tool.write_text("#!/bin/sh\nexit 0\n")
        old_tool.chmod(0o755)
        launcher = self.home / ".local/bin/agents-status"
        launcher.parent.mkdir(parents=True)
        launcher.symlink_to(old_tool)
        result = self.install("--version", "1.0.12")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(launcher.is_symlink())
        result = self.install("--version", "1.0.12", "--migrate")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(old_tool.read_text(), "#!/bin/sh\nexit 0\n")
        self.assertFalse(launcher.is_symlink())
        self.assertEqual(len(list(self.root.glob("legacy-launcher-*"))), 1)


if __name__ == "__main__":
    unittest.main()
