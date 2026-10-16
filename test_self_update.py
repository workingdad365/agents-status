import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
import zipfile

from agents_status import self_update as update


class UpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / "agents-status"
        self.root.mkdir()
        self.version = "1.0.13"
        self.old = "1.0.12"
        self.start_patch(patch.object(update, "__version__", self.old))
        self.start_patch(patch.object(update.platform, "system", return_value="Linux"))
        self.start_patch(patch.object(update.platform, "machine", return_value="x86_64"))
        self.start_patch(patch.object(update, "_cache_path", return_value=self.home / "cache" / "release.json"))
        old_directory = self.root / "versions" / self.old
        old_directory.mkdir(parents=True)
        self.old_executable = old_directory / update._executable_name()
        self.old_executable.write_bytes(b"old executable")
        (old_directory / "_internal").mkdir()
        self.select(self.old)
        self.release = self.make_release(self.version)

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def select(self, version):
        (self.root / "current.json").write_text(json.dumps({"version": version}), encoding="utf-8")

    def selected(self):
        return json.loads((self.root / "current.json").read_text(encoding="utf-8"))["version"]

    def make_release(self, version):
        base = f"{update.RELEASE_BASE}/download/v{version}/"
        return update.Release(version, f"{update.RELEASE_BASE}/tag/v{version}", base + update._platform_asset(version), base + "SHA256SUMS")

    def metadata(self, version=None):
        release = self.make_release(version or self.version)
        return {"tag_name": "v" + release.version, "draft": False, "prerelease": False,
                "html_url": release.notes_url, "assets": [
                    {"name": update._platform_asset(release.version), "browser_download_url": release.asset_url},
                    {"name": "SHA256SUMS", "browser_download_url": release.sha256_url}]}

    def archive_bytes(self, extras=()):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("agents-status/" + update._executable_name(), b"new executable")
            archive.writestr("agents-status/_internal/data", b"bundled data")
            for name, content in extras:
                archive.writestr(name, content)
        return stream.getvalue()

    def prepare(self, archive=None, bad_hash=False, smoke_error=None):
        content = self.archive_bytes() if archive is None else archive
        digest = "0" * 64 if bad_hash else hashlib.sha256(content).hexdigest()

        def download(url, destination, limit):
            if destination is None:
                return f"{digest}  {update._platform_asset(self.version)}\n".encode()
            destination.write_bytes(content)
            return b""

        with patch.object(update, "installation_root", return_value=self.root), \
                patch.object(update, "_download", side_effect=download), \
                patch.object(update, "_smoke", side_effect=smoke_error):
            return update.prepare_update(self.release)

    def assert_preserved(self):
        self.assertEqual(self.selected(), self.old)
        self.assertEqual(self.old_executable.read_bytes(), b"old executable")
        self.assertFalse((self.root / ".update-lock").exists())

    def test_versions_are_exact_stable_numeric_tuples(self):
        self.assertGreater(update._version_tuple("1.10.0"), update._version_tuple("1.9.99"))
        for version in ("1.2", "v1.2.3", "1.2.3rc1", "1.2.3+build", "01.2.3", "1.2.3\n", "１.2.3", None):
            with self.subTest(version=version), self.assertRaises(update.UpdateError):
                update._version_tuple(version)

    def test_platform_asset_names(self):
        for system, machine, expected in (("Linux", "aarch64", "linux-aarch64"), ("Windows", "AMD64", "windows-x86_64"), ("Darwin", "arm64", "macos-aarch64")):
            with self.subTest(system=system), patch.object(update.platform, "system", return_value=system), patch.object(update.platform, "machine", return_value=machine):
                self.assertEqual(update._platform_asset(self.version), f"agents-status-{self.version}-{expected}.zip")

    def test_windows_arm_host_uses_frozen_x64_runtime_asset(self):
        with patch.object(update.platform, "system", return_value="Windows"), patch.object(update.platform, "machine", return_value="ARM64"), patch.object(update.sysconfig, "get_platform", return_value="win-amd64"):
            self.assertEqual(update._platform_asset(self.version), f"agents-status-{self.version}-windows-x86_64.zip")

    def test_update_check_cache_force_and_no_downgrade(self):
        with patch.object(update, "_download", return_value=json.dumps(self.metadata()).encode()) as download:
            self.assertEqual(update.check_for_update(), self.release)
            self.assertEqual(update.check_for_update(), self.release)
            self.assertEqual(download.call_count, 1)
            self.assertEqual(update.check_for_update(force=True), self.release)
            self.assertEqual(download.call_count, 2)
        for version in (self.old, "1.0.9"):
            with patch.object(update, "_download", return_value=json.dumps(self.metadata(version)).encode()):
                self.assertIsNone(update.check_for_update(force=True))

    def test_invalid_expired_and_future_cache_are_refetched(self):
        cache = update._cache_path()
        cache.parent.mkdir()
        values = ["not json", json.dumps({"checked_at": time.time() - update.CACHE_SECONDS - 1, "release": self.metadata()}),
                  json.dumps({"checked_at": time.time() + 100, "release": self.metadata()}),
                  json.dumps({"checked_at": time.time(), "release": {"draft": True}})]
        for value in values:
            cache.write_text(value, encoding="utf-8")
            with patch.object(update, "_download", return_value=json.dumps(self.metadata()).encode()) as download:
                self.assertEqual(update.check_for_update(), self.release)
                download.assert_called_once()

    def test_malformed_and_untrusted_metadata_rejected(self):
        malformed = [None, [], {"draft": True}, self.metadata()]
        malformed[-1]["prerelease"] = True
        for key, value in (("tag_name", "v1.2.3-rc1"), ("html_url", "https://evil.example/release"), ("assets", None)):
            record = self.metadata()
            record[key] = value
            malformed.append(record)
        record = self.metadata()
        record["assets"][0]["browser_download_url"] = "https://evil.example/payload.zip"
        malformed.append(record)
        record = self.metadata()
        record["assets"].append(copy.deepcopy(record["assets"][0]))
        malformed.append(record)
        for value in malformed:
            with self.subTest(metadata=value), self.assertRaises(update.UpdateError):
                update._parse_release(value)

    def test_offline_errors_are_typed_and_do_not_touch_install(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError("offline")
        with patch.object(update.urllib.request, "build_opener", return_value=opener), self.assertRaises(update.NetworkError):
            update.check_for_update(force=True)
        self.assert_preserved()

    def test_download_limits_and_redirect_allowlist(self):
        for url in ("http://github.com/x", "https://github.com/other/repo/releases/download/v1/a", "https://github.com@evil.example/a", "file:///tmp/payload", "https://github.com:bad/a"):
            with self.subTest(url=url), self.assertRaises(update.UpdateError):
                update._trusted_url(url, redirected=True)
        update._trusted_url("https://release-assets.githubusercontent.com/github-production-release-asset/a?sig=x", redirected=True)
        with self.assertRaises(update.UpdateError):
            update._trusted_url("https://release-assets.githubusercontent.com/a")
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = self.release.asset_url
        response.headers = {"Content-Length": "100"}
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(update.urllib.request, "build_opener", return_value=opener), self.assertRaises(update.UpdateError):
            update._download(self.release.asset_url, None, 4)
        response.headers = {}
        response.read.side_effect = [b"12345", b""]
        with patch.object(update.urllib.request, "build_opener", return_value=opener), self.assertRaises(update.UpdateError):
            update._download(self.release.asset_url, None, 4)
        handler = update._TrustedRedirect()
        with self.assertRaises(update.UpdateError):
            handler.redirect_request(None, None, 302, "redirect", {}, "https://evil.example/a")

    def test_source_install_is_never_modified(self):
        with patch.object(update.sys, "frozen", False, create=True):
            self.assertIsNone(update.installation_root())
            with self.assertRaisesRegex(update.UnsupportedInstallError, "migrate"):
                update.prepare_update(self.release)
        self.assert_preserved()

    def test_managed_install_requires_frozen_fixed_executable_path(self):
        with patch.object(update, "_default_root", return_value=self.root), patch.object(update.sys, "frozen", True, create=True), patch.object(update.sys, "executable", str(self.old_executable)):
            self.assertEqual(update.installation_root(), self.root)
            with patch.object(update.sys, "executable", str(self.home / "arbitrary")):
                self.assertIsNone(update.installation_root())
            (self.root / "current.json").write_text('{"version":"../../escape"}', encoding="utf-8")
            self.assertIsNone(update.installation_root())

    def test_checksum_requires_exact_unique_filename(self):
        name = update._platform_asset(self.version)
        digest = "ab" * 32
        self.assertEqual(update._checksum(f"{digest} *{name}\n".encode(), name), digest)
        for content in (f"{digest}  other.zip\n", f"{digest}  {name}\n{digest}  {name}\n", "not a checksum"):
            with self.assertRaises(update.UpdateError):
                update._checksum(content.encode(), name)

    def test_hash_failure_preserves_current_and_releases_lock(self):
        with self.assertRaisesRegex(update.UpdateError, "SHA256"):
            self.prepare(bad_hash=True)
        self.assert_preserved()
        self.assertEqual(list((self.root / ".staging").iterdir()), [])

    def test_archive_traversal_symlink_reserved_and_duplicates_rejected(self):
        names = ["../escape", "agents-status/../../escape", "/absolute", "other/file", "agents-status/a\\b", "agents-status/C:bad", "agents-status/CON", "agents-status/dot.", "agents-status/./file", "agents-status//file", "agents-status/_internal/DATA"]
        symlink = zipfile.ZipInfo("agents-status/link")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        for name in [*names, symlink]:
            with self.subTest(name=name), self.assertRaises(update.UpdateError):
                self.prepare(self.archive_bytes([(name, "../../escape")]))
            self.assert_preserved()
            self.assertFalse((self.home / "escape").exists())

    def test_expansion_limits_preserve_current(self):
        with patch.object(update, "MAX_EXPANDED_BYTES", 1), self.assertRaises(update.UpdateError):
            self.prepare()
        self.assert_preserved()
        with patch.object(update, "MAX_MEMBERS", 1), self.assertRaises(update.UpdateError):
            self.prepare()
        self.assert_preserved()

    def test_missing_internal_directory_rejected(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("agents-status/" + update._executable_name(), b"binary")
        with self.assertRaises(update.UpdateError):
            self.prepare(stream.getvalue())
        self.assert_preserved()

    def test_smoke_rejects_missing_version_output_and_timeout(self):
        with patch.object(update.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), self.assertRaises(update.UpdateError):
            update._smoke(self.old_executable, self.version)
        with patch.object(update.subprocess, "run", side_effect=subprocess.TimeoutExpired("binary", 30)), self.assertRaises(update.UpdateError):
            update._smoke(self.old_executable, self.version)

    def test_preparation_holds_lock_until_idempotent_discard(self):
        prepared = self.prepare()
        self.assertEqual(self.selected(), self.old)
        self.assertTrue((self.root / ".update-lock").is_dir())
        self.assertFalse((self.root / "versions" / self.version).exists())
        with self.assertRaises(update.UpdateBusyError):
            self.prepare()
        self.assertTrue((self.root / ".update-lock").is_dir())
        update.discard_prepared_update(prepared)
        update.discard_prepared_update(prepared)
        self.assertFalse(prepared.staging.exists())
        self.assert_preserved()

    def test_existing_version_and_changed_selector_are_not_overwritten(self):
        destination = self.root / "versions" / self.version
        destination.mkdir()
        (destination / "preserve").write_text("keep", encoding="utf-8")
        with self.assertRaises(update.UpdateError):
            self.prepare()
        self.assertEqual((destination / "preserve").read_text(), "keep")
        self.assert_preserved()
        self.select("1.0.11")
        with self.assertRaises(update.UpdateError):
            self.prepare()
        self.assertEqual(self.selected(), "1.0.11")
        self.assertFalse((self.root / ".update-lock").exists())

    def test_preparation_does_not_downgrade_or_trust_external_release(self):
        with patch.object(update, "installation_root", return_value=self.root):
            for release in (self.make_release(self.old), self.make_release("1.0.1"), update.Release(self.version, self.release.notes_url, "https://evil.example/a.zip", self.release.sha256_url)):
                with self.assertRaises(update.UpdateError):
                    update.prepare_update(release)
        self.assert_preserved()

    def test_smoke_and_download_failures_clean_staging(self):
        with self.assertRaises(update.UpdateError):
            self.prepare(smoke_error=update.UpdateError("smoke failed"))
        self.assert_preserved()
        self.assertEqual(list((self.root / ".staging").iterdir()), [])
        with patch.object(update, "installation_root", return_value=self.root), patch.object(update, "_download", side_effect=update.NetworkError("offline")), self.assertRaises(update.NetworkError):
            update.prepare_update(self.release)
        self.assert_preserved()
        self.assertEqual(list((self.root / ".staging").iterdir()), [])

    def test_selector_write_failure_keeps_previous_version(self):
        prepared = self.prepare()
        with patch.object(update, "installation_root", return_value=self.root), patch.object(update, "_atomic_json", side_effect=OSError("read-only")), self.assertRaises(update.UpdateError):
            update.apply_update(prepared, [])
        self.assert_preserved()
        self.assertFalse((self.root / "versions" / self.version).exists())

    def test_sigterm_during_readiness_restores_selector(self):
        prepared = self.prepare()
        child = MagicMock()
        child.poll.return_value = None
        previous_handler = update.signal.getsignal(update.signal.SIGTERM)
        with patch.object(update, "installation_root", return_value=self.root), patch.object(update.subprocess, "Popen", return_value=child), patch.object(update, "_wait_ready", side_effect=update._Interrupted(update.signal.SIGTERM)):
            self.assertEqual(update.apply_update(prepared, []), 128 + update.signal.SIGTERM)
        self.assertEqual(update.signal.getsignal(update.signal.SIGTERM), previous_handler)
        self.assert_preserved()

    def test_activation_preserves_argv_cwd_and_previous_version(self):
        prepared = self.prepare()
        child = MagicMock()
        child.wait.return_value = 7
        arguments = ["--agent", "name with spaces", "$(not-shell)"]
        with patch.object(update, "installation_root", return_value=self.root), patch.object(update.subprocess, "Popen", return_value=child) as launch, patch.object(update, "_wait_ready", return_value=True):
            self.assertEqual(update.apply_update(prepared, arguments), 7)
        self.assertEqual(self.selected(), self.version)
        self.assertTrue(self.old_executable.exists())
        self.assertEqual(launch.call_args.args[0], [str(self.root / "versions" / self.version / update._executable_name()), *arguments])
        self.assertEqual(launch.call_args.kwargs["cwd"], os.getcwd())
        self.assertEqual(launch.call_args.kwargs["env"][update._READY_TOKEN], prepared.token)
        self.assertNotIn("shell", launch.call_args.kwargs)
        self.assertFalse((self.root / ".update-lock").exists())
        self.assertFalse(prepared.staging.exists())
        update.discard_prepared_update(prepared)

    def test_readiness_failure_rolls_back_and_restarts_previous(self):
        prepared = self.prepare()
        child = MagicMock()
        child.poll.return_value = None
        with patch.object(update, "installation_root", return_value=self.root), patch.object(update.subprocess, "Popen", return_value=child), patch.object(update, "_wait_ready", return_value=False), patch.object(update.subprocess, "call", return_value=0) as restart:
            self.assertEqual(update.apply_update(prepared, ["--verbose"]), 0)
        child.terminate.assert_called_once()
        self.assertEqual(restart.call_args.args[0], [str(self.old_executable), "--verbose"])
        self.assertNotIn(update._READY_PATH, restart.call_args.kwargs["env"])
        self.assert_preserved()
        self.assertFalse((self.root / "versions" / self.version).exists())

    def test_launch_failure_rolls_back_and_restarts_previous(self):
        prepared = self.prepare()
        with patch.object(update, "installation_root", return_value=self.root), patch.object(update.subprocess, "Popen", side_effect=OSError("cannot launch")), patch.object(update.subprocess, "call", return_value=0):
            self.assertEqual(update.apply_update(prepared, []), 0)
        self.assert_preserved()

    def test_interrupt_before_readiness_restores_selector(self):
        prepared = self.prepare()
        child = MagicMock()
        child.poll.return_value = None
        with patch.object(update, "installation_root", return_value=self.root), patch.object(update.subprocess, "Popen", return_value=child), patch.object(update, "_wait_ready", side_effect=KeyboardInterrupt):
            self.assertEqual(update.apply_update(prepared, []), 130)
        child.terminate.assert_called_once()
        self.assert_preserved()

    def test_interrupt_after_readiness_does_not_rollback(self):
        prepared = self.prepare()
        child = MagicMock()
        child.poll.return_value = None
        child.wait.side_effect = [KeyboardInterrupt(), 0]
        with patch.object(update, "installation_root", return_value=self.root), patch.object(update.subprocess, "Popen", return_value=child), patch.object(update, "_wait_ready", return_value=True):
            self.assertEqual(update.apply_update(prepared, []), 130)
        self.assertEqual(self.selected(), self.version)
        child.terminate.assert_called_once()
        self.assertFalse((self.root / ".update-lock").exists())

    def test_startup_marker_requires_nonce_and_matching_version(self):
        prepared = self.prepare()
        self.addCleanup(update.discard_prepared_update, prepared)
        marker = prepared.staging / "ready.json"
        child = MagicMock()
        child.poll.return_value = None
        with patch.object(update, "installation_root", return_value=self.root), patch.object(update, "__version__", self.version), patch.dict(os.environ, {update._READY_PATH: str(marker), update._READY_TOKEN: prepared.token}):
            update.acknowledge_startup()
            self.assertNotIn(update._READY_PATH, os.environ)
        self.assertTrue(update._wait_ready(child, prepared))
        marker.write_text(json.dumps({"token": "wrong", "version": self.version}), encoding="utf-8")
        with patch.object(update.time, "monotonic", side_effect=[0, 0, 100]), patch.object(update.time, "sleep"):
            self.assertFalse(update._wait_ready(child, prepared))
        child.poll.return_value = 1
        self.assertFalse(update._wait_ready(child, prepared))

    def test_startup_marker_cannot_write_arbitrary_path(self):
        target = self.home / "do-not-write.json"
        with patch.object(update, "installation_root", return_value=self.root), patch.dict(os.environ, {update._READY_PATH: str(target), update._READY_TOKEN: "a" * 64}), self.assertRaises(update.UpdateError):
            update.acknowledge_startup()
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
