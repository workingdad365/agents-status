import asyncio
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from textual.widgets import Button, RichLog

from agents_status.app import AgentsStatusApp
from agents_status.core import AGENTS, AgentStatus, CommandResult, StatusKind, UpdateCommand


class UpdateAllTests(unittest.IsolatedAsyncioTestCase):
    async def test_sequential_updates_continue_after_failures_and_skip_missing(self):
        items = [AgentStatus(spec, StatusKind.INSTALLED, Path(spec.binaries[0]), "1", "-") for spec in AGENTS[:6]]
        items[-1] = AgentStatus(items[-1].spec, StatusKind.MISSING, None, "-", "-")
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        def command(status):
            if status.spec.key == "opencode":
                return None, "업데이트 미지원"
            return UpdateCommand((status.spec.key,), status.spec.key), ""

        async def run(cmd, **kwargs):
            calls.append(cmd.argv[0])
            if len(calls) == 1:
                started.set()
                await release.wait()
                return CommandResult(1, "failed")
            if len(calls) == 2:
                raise OSError("실행 실패")
            return CommandResult(0, "ok")

        with patch("agents_status.app.scan_agents", AsyncMock(return_value=items)) as scan, patch("agents_status.app.update_command", side_effect=command), patch("agents_status.app.run_update", side_effect=run):
            app = AgentsStatusApp()
            async with app.run_test(size=(160, 40)) as pilot:
                await pilot.pause()
                buttons = list(app.query_one("#actions").query(Button))
                self.assertEqual([b.id for b in buttons][1:4], ["update", "update-all", "delete"])
                self.assertTrue(all(b.region.height == 3 for b in buttons))
                self.assertTrue(all(a.region.right < b.region.x for a, b in zip(buttons, buttons[1:])))
                await pilot.click("#update-all")
                await asyncio.wait_for(started.wait(), 2)
                self.assertEqual(len(calls), 1)
                self.assertTrue(app.busy)
                self.assertTrue(all(b.disabled for b in buttons))
                app.update_all_pressed()
                release.set()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(calls, ["claude", "codex", "commandcode", "copilot"])
                self.assertEqual(scan.await_count, 2)
                self.assertFalse(app.busy)
                text = "\n".join(line.text for line in app.query_one(RichLog).lines)
                self.assertIn("2개 성공, 2개 실패, 2개 건너뜀", text)

    async def test_cancel_stops_queue_and_restores_controls(self):
        items = [AgentStatus(spec, StatusKind.INSTALLED, Path(spec.binaries[0]), "1", "-") for spec in AGENTS[:2]]
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def run(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        with patch("agents_status.app.scan_agents", AsyncMock(return_value=items)), patch("agents_status.app.update_command", return_value=(UpdateCommand(("test",), "test"), "")), patch("agents_status.app.run_update", side_effect=run) as execute:
            app = AgentsStatusApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.click("#update-all")
                await asyncio.wait_for(started.wait(), 2)
                workers = [worker for worker in app.workers if worker.group == "update"]
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*(worker.wait() for worker in workers), return_exceptions=True)
                await pilot.pause()
                self.assertTrue(stopped.is_set())
                self.assertFalse(app.busy)
                execute.assert_awaited_once()

    async def test_no_installed_agents_disables_button(self):
        with patch("agents_status.app.scan_agents", AsyncMock(return_value=[])), patch("agents_status.app.run_update") as execute:
            app = AgentsStatusApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertTrue(app.query_one("#update-all", Button).disabled)
                app.update_all_pressed()
                execute.assert_not_called()
