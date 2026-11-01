from contextlib import nullcontext
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from textual.widgets import Button

from agents_status.app import AgentsStatusApp
from agents_status.instructions import default_instructions_path


class EditorTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_copy_is_persistent_and_buttons_share_one_row(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory).resolve() / "bundled.md"
            source.write_text("original", encoding="utf-8")
            destination = Path(directory).resolve() / "config/GLOBAL_INSTRUCTIONS.md"
            with patch("agents_status.app.scan_agents", AsyncMock(return_value=[])), patch("agents_status.app.default_instructions_path", return_value=source), patch("agents_status.app.user_instructions_path", return_value=destination), patch("agents_status.app.shutil.which", return_value="/usr/bin/nano"), patch("agents_status.app.subprocess.run") as run:
                app = AgentsStatusApp()
                async with app.run_test(size=(120, 30)) as pilot:
                    await pilot.pause()
                    buttons = list(app.query_one("#actions").query(Button))
                    self.assertEqual([button.id for button in buttons], ["refresh", "update", "update-all", "delete", "edit-instructions", "sync", "sync-all", "apply-defaults"])
                    self.assertEqual(len({button.region.y for button in buttons}), 1)
                    with patch.object(app, "suspend", side_effect=nullcontext):
                        await pilot.click("#edit-instructions")
                        run.assert_called_once_with(["/usr/bin/nano", str(destination)], check=True)
                        self.assertEqual(destination.read_text(), "original")
                        destination.write_text("edited", encoding="utf-8")
                        app.edit_instructions_pressed()
                    self.assertEqual(source.read_text(), "original")
                    self.assertEqual(destination.read_text(), "edited")
                    self.assertEqual(app.instructions_source, destination)
                    with patch("agents_status.instructions.user_instructions_path", return_value=destination):
                        self.assertEqual(default_instructions_path(), destination)

    async def test_explicit_source_windows_editor_failure_and_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory).resolve() / "custom instructions.md"
            source.write_text("custom", encoding="utf-8")
            with patch("agents_status.app.scan_agents", AsyncMock(return_value=[])):
                app = AgentsStatusApp(instructions_source=source)
                async with app.run_test(size=(120, 30)) as pilot:
                    await pilot.pause()
                    with patch("agents_status.app.sys.platform", "win32"), patch("agents_status.app.shutil.which", return_value="edit.exe") as which, patch("agents_status.app.subprocess.run", side_effect=subprocess.CalledProcessError(1, "edit")) as run, patch.object(app, "suspend", side_effect=nullcontext), patch.object(app, "notify") as notify:
                        app.edit_instructions_pressed()
                        which.assert_called_once_with("edit")
                        run.assert_called_once_with(["edit.exe", str(source)], check=True)
                        self.assertEqual(notify.call_args.kwargs["severity"], "error")
                        self.assertFalse(app.busy)
                        self.assertFalse(app.query_one("#edit-instructions", Button).disabled)
                        app.busy = True
                        app.edit_instructions_pressed()
                        run.assert_called_once()
                        app.busy = False
