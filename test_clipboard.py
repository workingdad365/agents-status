import subprocess
import unittest
from unittest.mock import AsyncMock, patch

from textual.geometry import Offset
from textual.selection import Selection

from agents_status.app import AgentsStatusApp, SelectableRichLog


class ClipboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_macos_native_copy_reports_success_only_after_pbcopy_succeeds(self):
        with patch("agents_status.app.scan_agents", AsyncMock(return_value=[])):
            app = AgentsStatusApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                log = app.query_one(SelectableRichLog)
                log.clear()
                log.write("한글 로그")
                await pilot.pause()
                app.screen.selections = {log: Selection(Offset(0, 0), Offset(5, 0))}
                for error in (None, OSError("missing"), subprocess.CalledProcessError(1, "pbcopy"), subprocess.TimeoutExpired("pbcopy", 5)):
                    with self.subTest(error=error), patch("agents_status.app.sys.platform", "darwin"), patch("agents_status.app.subprocess.run", side_effect=error) as run, patch.object(app, "notify") as notify:
                        app.copy_selection()
                        await app.workers.wait_for_complete()
                        self.assertEqual(run.call_args.args[0], ["/usr/bin/pbcopy"])
                        self.assertEqual(run.call_args.kwargs["input"], "한글 로그".encode("utf-8"))
                        notify.assert_called_once()
                        self.assertEqual(notify.call_args.kwargs.get("severity"), "error" if error else None)

    async def test_drag_scrolls_beyond_viewport_in_both_directions_and_stops(self):
        with patch("agents_status.app.scan_agents", AsyncMock(return_value=[])), patch("agents_status.app.sys.platform", "linux"):
            app = AgentsStatusApp()
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.pause()
                log = app.query_one(SelectableRichLog)
                log.clear()
                for index in range(100):
                    log.write(f"line {index:03}")
                await pilot.pause()
                log.scroll_to(y=30, animate=False, immediate=True)
                await pilot.pause()
                height = log.scrollable_content_region.height
                for direction in (1, -1):
                    with self.subTest(direction=direction):
                        start = int(log.scroll_y)
                        edge = height + 1 if direction == 1 else 0
                        await pilot.mouse_down(log, offset=(1, 2))
                        await pilot.hover(log, offset=(8, edge))
                        await pilot.pause(0.4)
                        end = int(log.scroll_y)
                        self.assertGreater((end - start) * direction, 0)
                        await pilot.mouse_up(log, offset=(8, edge))
                        await pilot.pause()
                        self.assertIn(f"line {start + 1 if direction == 1 else start:03}", app._clipboard)
                        self.assertIn(f"line {end + height - 2 if direction == 1 else end + 1:03}", app._clipboard)
                        stopped = log.scroll_y
                        await pilot.pause(0.15)
                        self.assertEqual(log.scroll_y, stopped)
                        self.assertIsNone(app.mouse_captured)
