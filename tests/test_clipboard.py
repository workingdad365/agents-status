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

    async def test_drag_scrolls_horizontally_and_copies_original_text(self):
        with patch("agents_status.app.scan_agents", AsyncMock(return_value=[])), patch("agents_status.app.sys.platform", "linux"):
            app = AgentsStatusApp()
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.pause()
                log = app.query_one(SelectableRichLog)
                for word, cell_width in (("0123456789", 1), ("가나다라마바사아자차", 2)):
                    for direction in (1, -1):
                        with self.subTest(word=word, direction=direction):
                            text = word * 30
                            log.clear()
                            log.write("above")
                            log.write(text)
                            log.write("below")
                            await pilot.pause()
                            log.scroll_to(x=40, y=0, animate=False, immediate=True)
                            await pilot.pause()
                            region = log.scrollable_content_region
                            origin = region.offset - log.region.offset
                            edge = region.width - 1 if direction == 1 else 0
                            edge_offset = (origin.x + edge, origin.y + 1)
                            await pilot.hover(log, offset=edge_offset)
                            await pilot.pause(0.15)
                            self.assertEqual(log.scroll_x, 40)
                            await pilot.mouse_down(log, offset=(origin.x + 8, origin.y + 1))
                            await pilot.hover(log, offset=edge_offset)
                            await pilot.pause(0.3)
                            await pilot.mouse_up(log, offset=edge_offset)
                            await pilot.pause()
                            end = int(log.scroll_x)
                            self.assertGreater((end - 40) * direction, 0)
                            self.assertEqual(log.scroll_y, 0)
                            start_index = 48 // cell_width
                            end_index = (end + edge + cell_width - 1) // cell_width
                            start_index, end_index = sorted((start_index, end_index))
                            self.assertEqual(app._clipboard, text[start_index:end_index])
                            stopped = log.scroll_offset
                            await pilot.pause(0.15)
                            self.assertEqual(log.scroll_offset, stopped)
                            self.assertIsNone(app.mouse_captured)

    async def test_drag_scrolls_both_axes_at_corners_and_stops_at_limits(self):
        with patch("agents_status.app.scan_agents", AsyncMock(return_value=[])), patch("agents_status.app.sys.platform", "linux"):
            app = AgentsStatusApp()
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.pause()
                log = app.query_one(SelectableRichLog)
                log.clear()
                for index in range(50):
                    log.write(f"line {index:03}: " + "0123456789" * 30)
                await pilot.pause()
                region = log.scrollable_content_region
                origin = region.offset - log.region.offset
                for direction in (1, -1):
                    with self.subTest(direction=direction):
                        limit = Offset(log.max_scroll_x, log.max_scroll_y) if direction == 1 else Offset(0, 0)
                        log.scroll_to(x=limit.x - direction * 2, y=limit.y - direction * 2, animate=False, immediate=True)
                        await pilot.pause()
                        corner = region.size.region.bottom_right_inclusive if direction == 1 else Offset(0, 0)
                        edge_offset = (origin.x + corner.x, origin.y + corner.y)
                        await pilot.mouse_down(log, offset=(origin.x + 8, origin.y + 2))
                        await pilot.hover(log, offset=edge_offset)
                        await pilot.pause(0.3)
                        self.assertEqual(log.scroll_offset, limit)
                        selection = log.text_selection
                        self.assertIsNotNone(selection)
                        self.assertEqual(selection.end.y if direction == 1 else selection.start.y, 49 if direction == 1 else 0)
                        outside = corner + Offset(direction, direction)
                        edge_offset = (origin.x + outside.x, origin.y + outside.y)
                        await pilot.hover(log, offset=edge_offset)
                        await pilot.pause(0.15)
                        selection = log.text_selection
                        self.assertEqual(selection.end if direction == 1 else selection.start,
                                         Offset(310, 49) if direction == 1 else Offset(0, 0))
                        await pilot.pause(0.15)
                        self.assertEqual(log.scroll_offset, limit)
                        self.assertEqual(log.text_selection, selection)
                        await pilot.mouse_up(log, offset=edge_offset)
                        await pilot.pause()
                        self.assertIsNone(app.mouse_captured)
