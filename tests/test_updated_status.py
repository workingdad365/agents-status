from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from textual.widgets import DataTable

from agents_status.app import AgentsStatusApp
from agents_status.core import AGENTS, AgentStatus, CommandResult, StatusKind, UpdateCommand


class UpdatedStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_update_persists_until_restart(self):
        before = AgentStatus(AGENTS[0], StatusKind.INSTALLED, Path("claude"), "1.0", "-")
        after = replace(before, version="2.0")
        command = UpdateCommand(("test",), "test")
        with patch("agents_status.app.scan_agents", AsyncMock(return_value=[before])) as scan, patch("agents_status.app.run_update", AsyncMock(return_value=CommandResult(0, "ok"))):
            app = AgentsStatusApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                table = app.query_one(DataTable)
                self.assertEqual(table.get_row("claude")[0], "[green]설치됨[/green]")
                scan.return_value = [after]
                app.update_agent(before, command)
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(table.get_row("claude")[0], "[cyan]갱신됨[/cyan]")
                app.action_refresh()
                await app.workers.wait_for_complete()
                self.assertEqual(table.get_row("claude")[0], "[cyan]갱신됨[/cyan]")
                app.update_agent(after, command)
                await app.workers.wait_for_complete()
                self.assertEqual(table.get_row("claude")[0], "[cyan]갱신됨[/cyan]")
                self.assertEqual(app.statuses[0].status, StatusKind.INSTALLED)
            restarted = AgentsStatusApp()
            async with restarted.run_test() as pilot:
                await pilot.pause()
                self.assertEqual(restarted.query_one(DataTable).get_row("claude")[0], "[green]설치됨[/green]")

    async def test_bulk_marks_only_observed_version_changes(self):
        versions = ["1.0", "1.0", "조회 실패", "1.0", "1.0", "1.0"]
        items = [AgentStatus(spec, StatusKind.INSTALLED, Path(spec.binaries[0]), version, "-")
                 for spec, version in zip(AGENTS, versions)]
        after = [replace(item, version=version) for item, version in zip(items, ["2.0", "1.0", "2.0", "조회 실패", "2.0", "2.0"])]

        def command(status):
            if status.spec.key == items[-1].spec.key:
                return None, "미지원"
            return UpdateCommand(("test",), "test"), ""

        with patch("agents_status.app.scan_agents", AsyncMock(return_value=items)) as scan, patch("agents_status.app.update_command", side_effect=command), patch("agents_status.app.run_update", AsyncMock(side_effect=[CommandResult(0, "ok")] * 4 + [CommandResult(1, "failed")])):
            app = AgentsStatusApp()
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.pause()
                scan.return_value = after
                await pilot.click("#update-all")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(app._updated_agents, {items[0].spec.key, items[4].spec.key})
                table = app.query_one(DataTable)
                for item in items:
                    self.assertIn("갱신됨" if item.spec.key in app._updated_agents else "설치됨", table.get_row(item.spec.key)[0])

    async def test_install_and_external_changes_are_not_updates(self):
        missing = AgentStatus(AGENTS[0], StatusKind.MISSING, None, "-", "-")
        installed = replace(missing, status=StatusKind.INSTALLED, executable=Path("claude"), version="1.0")
        with patch("agents_status.app.scan_agents", AsyncMock(return_value=[missing])) as scan, patch("agents_status.app.run_update", AsyncMock(return_value=CommandResult(0, "ok"))):
            app = AgentsStatusApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                scan.return_value = [installed]
                app.update_agent(missing, UpdateCommand(("test",), "test"))
                await app.workers.wait_for_complete()
                scan.return_value = [replace(installed, version="2.0")]
                app.action_refresh()
                await app.workers.wait_for_complete()
                self.assertEqual(app.query_one(DataTable).get_row("claude")[0], "[green]설치됨[/green]")
