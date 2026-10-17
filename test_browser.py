import base64
import subprocess
import unittest
from unittest.mock import AsyncMock, patch

from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Static

from agents_status.app import AgentTable, AgentsStatusApp, SelectableRichLog
from agents_status.browser import RELEASE_PAGES, open_release_page
from agents_status.core import AGENTS, AgentStatus, StatusKind


class BrowserTests(unittest.TestCase):
    def test_every_agent_has_https_release_page(self):
        self.assertEqual(set(RELEASE_PAGES), {spec.key for spec in AGENTS})
        self.assertTrue(all(url.startswith("https://") for url in RELEASE_PAGES.values()))

    def test_wsl_uses_windows_powershell_without_shell_interpolation(self):
        with patch("agents_status.browser.sys.platform", "linux"), patch("agents_status.browser.platform.release", return_value="microsoft-standard-WSL2"), patch("agents_status.browser.shutil.which", return_value="/windows/powershell.exe"), patch("agents_status.browser.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run, patch("agents_status.browser.webbrowser.open") as browser:
            open_release_page("qwen")
            argv = run.call_args.args[0]
            self.assertEqual(argv[:4], ("/windows/powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand"))
            script = base64.b64decode(argv[4]).decode("utf-16-le")
            self.assertIn("Start-Process '" + RELEASE_PAGES["qwen"] + "'", script)
            browser.assert_not_called()

    def test_native_browser_failure_is_reported(self):
        with patch("agents_status.browser.sys.platform", "darwin"), patch("agents_status.browser.webbrowser.open", return_value=False):
            with self.assertRaises(OSError):
                open_release_page("claude")

    def test_wsl_missing_launcher_and_failed_launcher(self):
        with patch("agents_status.browser.sys.platform", "linux"), patch("agents_status.browser.platform.release", return_value="microsoft"), patch("agents_status.browser.Path.is_file", return_value=False), patch("agents_status.browser.shutil.which", return_value=None):
            with self.assertRaises(OSError):
                open_release_page("kimi")
        with patch("agents_status.browser.sys.platform", "linux"), patch("agents_status.browser.platform.release", return_value="microsoft"), patch("agents_status.browser.shutil.which", side_effect=lambda name: "wslview" if name == "wslview" else None), patch("agents_status.browser.Path.is_file", return_value=False), patch("agents_status.browser.subprocess.run", return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaises(OSError):
                open_release_page("kimi")


class TableInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_sorted_rows_double_click_and_refresh_selection(self):
        statuses = [AgentStatus(spec, StatusKind.MISSING, None, "-", "-") for spec in reversed(AGENTS)]
        with patch("agents_status.app.scan_agents", AsyncMock(return_value=statuses)), patch("agents_status.app.open_release_page") as browser:
            app = AgentsStatusApp()
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                table = app.query_one(AgentTable)
                expected = sorted(AGENTS, key=lambda spec: spec.name.casefold())
                self.assertEqual([row.key.value for row in table.ordered_rows], [spec.key for spec in expected])
                await pilot.click("#agents", offset=(5, 2))
                await pilot.pause()
                browser.assert_not_called()
                await pilot.click("#agents", offset=(5, 2), times=2)
                await pilot.pause()
                browser.assert_called_once_with(expected[1].key)
                self.assertEqual(app._selected().spec.key, expected[1].key)
                app._render_table()
                self.assertEqual(app._selected().spec.key, expected[1].key)
                browser.reset_mock()
                await pilot.click("#agents", offset=(5, 0), times=2)
                await pilot.pause()
                browser.assert_not_called()

    async def test_only_log_can_select_and_copy(self):
        with patch("agents_status.app.scan_agents", AsyncMock(return_value=[])):
            app = AgentsStatusApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertFalse(app.query_one(AgentTable).allow_select)
                self.assertFalse(app.query_one("#summary", Static).allow_select)
                log = app.query_one(SelectableRichLog)
                self.assertTrue(log.allow_select)
                log.clear()
                log.write("log only")
                await pilot.pause()
                with patch.object(app, "copy_to_clipboard") as copy:
                    app.copy_selection()
                    copy.assert_not_called()
                    app.screen.selections = {log: Selection(Offset(0, 0), Offset(8, 0))}
                    app.copy_selection()
                    copy.assert_called_once_with("log only")
