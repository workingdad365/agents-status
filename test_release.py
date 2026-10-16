"""실제 Git 저장소에서 자동 배포의 변경·중단 경계를 검증함."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.name != "nt" and shutil.which("bash") and shutil.which("git"), "POSIX Bash/Git 필요")
class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "source"
        self.repo.mkdir()
        self.remote = self.root / "remote.git"
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.env = {**os.environ, "PATH": f"{self.tools}{os.pathsep}{os.environ['PATH']}",
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "PYTHONDONTWRITEBYTECODE": "1"}
        self.git("init", "--bare", str(self.remote))
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Release Test")
        self.git("config", "user.email", "release@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "tag.gpgsign", "false")
        self.git("remote", "add", "origin", str(self.remote))
        (self.repo / "agents_status").mkdir()
        self.version = self.repo / "agents_status/__init__.py"
        self.version.write_text('__version__ = "1.0.14"\n')
        (self.repo / "uv.lock").write_text("initial lock\n")
        (self.repo / ".gitignore").write_text("__pycache__/\n")
        shutil.copy2(Path(__file__).parent / "release.sh", self.repo / "release.sh")
        self.git("add", ".")
        self.git("commit", "-m", "initial")
        self.git("push", "origin", "main")
        self.initial = self.git("rev-parse", "HEAD").stdout.strip()
        self.executable("uv", '''import os, pathlib, subprocess, sys
args = sys.argv[1:]
if args[0] in ("sync", "lock"):
    if args[0] == "lock":
        pathlib.Path("uv.lock").write_text("updated lock\\n")
    sys.exit(0)
args = args[2:]
if args[:3] == ["python", "-m", "unittest"]:
    sys.exit(int(os.environ.get("FAIL_TESTS", "0")))
if args[:2] == ["python", "packaging/build_release.py"]:
    sys.exit(0)
if args[0] == "agents-status":
    sys.exit(0)
if args[0] == "python":
    os.execv(sys.executable, [sys.executable, *args[1:]])
sys.exit(91)
''')
        self.executable("gh", '''import json, os, sys
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    sys.exit(0)
if args[:2] == ["repo", "view"]:
    print("workingdad365/agents-status")
elif args[:2] == ["secret", "list"]:
    print("true")
elif args[:2] == ["run", "list"]:
    print("123")
elif args[:2] == ["run", "watch"]:
    sys.exit(0)
elif args[:2] == ["release", "view"]:
    tag = args[2]
    names = [f"agents-status-{tag[1:]}-{target}.zip" for target in
             ("linux-x86_64", "linux-aarch64", "macos-x86_64", "macos-aarch64", "windows-x86_64")]
    names += ["install.sh", "install.ps1", "SHA256SUMS"]
    if os.environ.get("INCOMPLETE_RELEASE"):
        names.pop()
    print(json.dumps({"tagName": tag, "isDraft": False, "isPrerelease": False,
                      "assets": [{"name": name} for name in names],
                      "url": "https://example.invalid/releases/" + tag}))
else:
    sys.exit(92)
''')

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, env=self.env,
                              text=True, capture_output=True, check=True)

    def executable(self, name, body):
        path = self.tools / name
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o755)

    def run_release(self, **environment):
        return subprocess.run(["bash", str(self.repo / "release.sh")], cwd=self.root,
                              env={**self.env, **environment}, text=True,
                              capture_output=True, timeout=30)

    def remote_head(self):
        return self.git("--git-dir", str(self.remote), "rev-parse", "refs/heads/main").stdout.strip()

    def test_patch_increment_publishes_same_commit_and_annotated_tag(self):
        result = self.run_release()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.version.read_text(), '__version__ = "1.0.15"\n')
        self.assertNotEqual(self.remote_head(), self.initial)
        self.assertEqual(self.remote_head(), self.git("rev-parse", "v1.0.15^{}").stdout.strip())
        self.assertEqual(self.git("--git-dir", str(self.remote), "cat-file", "-t", "v1.0.15").stdout.strip(), "tag")
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_failed_validation_restores_files_without_commit_or_remote_change(self):
        result = self.run_release(FAIL_TESTS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.version.read_text(), '__version__ = "1.0.14"\n')
        self.assertEqual((self.repo / "uv.lock").read_text(), "initial lock\n")
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.initial)
        self.assertEqual(self.remote_head(), self.initial)
        self.assertEqual(self.git("tag", "--list").stdout, "")

    def test_dirty_tree_preserves_user_changes(self):
        self.version.write_text('__version__ = "2.0.0"\n')
        result = self.run_release()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.version.read_text(), '__version__ = "2.0.0"\n')
        self.assertEqual(self.remote_head(), self.initial)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.initial)

    def test_existing_remote_tag_is_not_replaced(self):
        self.git("tag", "v1.0.15")
        self.git("push", "origin", "v1.0.15")
        self.git("tag", "-d", "v1.0.15")
        result = self.run_release()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.version.read_text(), '__version__ = "1.0.14"\n')
        self.assertEqual(self.remote_head(), self.initial)
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", "v1.0.15").stdout.strip(), self.initial)

    def test_rejected_push_preserves_local_release_and_remote_state(self):
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        result = self.run_release()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.version.read_text(), '__version__ = "1.0.15"\n')
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.git("rev-parse", "v1.0.15^{}").stdout.strip())
        self.assertEqual(self.remote_head(), self.initial)
        self.assertEqual(self.git("--git-dir", str(self.remote), "tag", "--list").stdout, "")

    def test_missing_public_asset_does_not_report_success(self):
        result = self.run_release(INCOMPLETE_RELEASE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("배포 완료:", result.stdout)
        self.assertEqual(self.remote_head(), self.git("rev-parse", "HEAD").stdout.strip())


if __name__ == "__main__":
    unittest.main()
