from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents_status.instructions import (
    _atomic_write, default_instructions_path, restore_bundled_instructions,
)


class InstructionSourceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        home = Path(directory.name).resolve()
        self.bundled = home / "bundled.md"
        self.bundled.write_bytes(b"release A\n")
        self.custom = home / "config/GLOBAL_INSTRUCTIONS.md"
        self.baseline = self.custom.with_suffix(".base.sha256")
        self.enterContext(patch("agents_status.instructions.bundled_instructions_path", return_value=self.bundled))
        self.enterContext(patch("agents_status.instructions.user_instructions_path", return_value=self.custom))

    def test_untouched_copy_follows_multiple_new_bundles(self):
        self.assertEqual(default_instructions_path(), self.bundled)
        restore_bundled_instructions()
        for data in (b"release B\n", b"release C\n"):
            self.bundled.write_bytes(data)
            self.assertEqual(default_instructions_path(), self.bundled)
            self.assertEqual(self.custom.read_bytes(), b"release A\n")

    def test_same_content_with_bom_and_newlines_follows_new_bundle(self):
        restore_bundled_instructions()
        self.bundled.write_bytes(b"release B\n")
        for data in (b"release A\r\n", b"\xef\xbb\xbfrelease A\r\n", b"release A\r"):
            with self.subTest(data=data):
                self.custom.write_bytes(data)
                self.assertEqual(default_instructions_path(), self.bundled)

    def test_real_edits_survive_updates_and_reverting_resumes_bundle(self):
        restore_bundled_instructions()
        self.custom.write_bytes(b"user changes\n")
        self.bundled.write_bytes(b"release B\n")
        self.assertEqual(default_instructions_path(), self.custom)
        self.assertEqual(self.custom.read_bytes(), b"user changes\n")
        self.custom.write_bytes(b"release A\n")
        self.assertEqual(default_instructions_path(), self.bundled)

    def test_explicit_restore_resumes_following_future_bundles(self):
        restore_bundled_instructions()
        self.custom.write_bytes(b"user changes\n")
        self.bundled.write_bytes(b"release B\n")
        restore_bundled_instructions()
        self.assertEqual(self.custom.read_bytes(), b"release B\n")
        self.bundled.write_bytes(b"release C\n")
        self.assertEqual(default_instructions_path(), self.bundled)

    def test_legacy_copy_is_recognized_but_same_header_with_edits_is_preserved(self):
        legacy = (Path(__file__).parent / "fixtures/global_instructions_20261023.md").read_bytes()
        self.custom.parent.mkdir()
        self.custom.write_bytes(legacy)
        self.assertFalse(self.baseline.exists())
        self.assertEqual(default_instructions_path(), self.bundled)
        self.custom.write_bytes(legacy + "\n- 사용자 추가 지침이다.\n".encode("utf-8"))
        self.assertEqual(default_instructions_path(), self.custom)

    def test_unknown_copy_and_unreadable_baseline_preserve_user_source(self):
        self.custom.parent.mkdir()
        self.custom.write_bytes(b"unknown user content\n")
        self.assertEqual(default_instructions_path(), self.custom)
        for data in (b"invalid", b"\xff"):
            self.baseline.write_bytes(data)
            self.assertEqual(default_instructions_path(), self.custom)
        self.baseline.unlink()
        self.baseline.mkdir()
        self.assertEqual(default_instructions_path(), self.custom)

    def test_missing_bundle_preserves_user_source(self):
        restore_bundled_instructions()
        self.bundled.unlink()
        self.assertEqual(default_instructions_path(), self.custom)

    def test_failed_copy_after_baseline_write_preserves_user_content(self):
        restore_bundled_instructions()
        self.custom.write_bytes(b"user changes\n")
        self.bundled.write_bytes(b"release B\n")

        def write(destination, data):
            if destination == self.custom:
                raise PermissionError("접근 거부")
            _atomic_write(destination, data)

        with patch("agents_status.instructions._atomic_write", side_effect=write):
            with self.assertRaises(PermissionError):
                restore_bundled_instructions()
        self.assertEqual(self.custom.read_bytes(), b"user changes\n")
        self.assertEqual(default_instructions_path(), self.custom)
