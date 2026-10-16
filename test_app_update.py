import asyncio
from pathlib import Path
import threading
import unittest
from unittest.mock import AsyncMock, patch

from textual.widgets import Button

from agents_status.app import AgentsStatusApp, ConfirmAppUpdate
from agents_status.self_update import Release, UpdateError


RELEASE = Release(
    "1.0.13",
    "https://github.com/workingdad365/agents-status-releases/releases/tag/v1.0.13",
    "https://github.com/workingdad365/agents-status-releases/releases/download/v1.0.13/agents-status-1.0.13-linux-x86_64.zip",
    "https://github.com/workingdad365/agents-status-releases/releases/download/v1.0.13/SHA256SUMS",
)


class AppUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_declining_release_keeps_app_running_without_download(self):
        with patch("agents_status.app.scan_agents", AsyncMock(return_value=[])), patch("agents_status.app.installation_root", return_value=None), patch("agents_status.app.prepare_update") as prepare:
            app = AgentsStatusApp()
            async with app.run_test(size=(100, 32)) as pilot:
                await app.workers.wait_for_complete()
                app.available_release = RELEASE
                with patch("agents_status.app.installation_root", return_value=Path("/managed")):
                    await pilot.click("#self-update")
                self.assertIsInstance(app.screen, ConfirmAppUpdate)
                await pilot.press("escape")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, ConfirmAppUpdate)
                self.assertIsNone(app.prepared_update)
                self.assertFalse(app._quitting)
                self.assertFalse(app.busy)
                prepare.assert_not_called()

    async def test_source_install_does_not_offer_destructive_self_replacement(self):
        with patch("agents_status.app.scan_agents", AsyncMock(return_value=[])), patch("agents_status.app.installation_root", return_value=None), patch("agents_status.app.prepare_update") as prepare:
            app = AgentsStatusApp()
            async with app.run_test(size=(100, 32)) as pilot:
                await app.workers.wait_for_complete()
                app.available_release = RELEASE
                await pilot.click("#self-update")
                self.assertNotIsInstance(app.screen, ConfirmAppUpdate)
                self.assertIsNone(app.prepared_update)
                self.assertFalse(app._quitting)
                prepare.assert_not_called()

    async def test_check_failure_does_not_disable_agent_management(self):
        with patch("agents_status.app.scan_agents", AsyncMock(return_value=[])), patch("agents_status.app.installation_root", return_value=None), patch("agents_status.app.check_for_update", side_effect=UpdateError("offline")):
            app = AgentsStatusApp()
            async with app.run_test(size=(100, 32)) as pilot:
                await app.workers.wait_for_complete()
                await pilot.click("#self-update")
                await app.workers.wait_for_complete()
                self.assertFalse(app.busy)
                self.assertFalse(app.query_one("#refresh", Button).disabled)
                self.assertFalse(app.query_one("#self-update", Button).disabled)
                self.assertIsNone(app.available_release)
                await pilot.click("#refresh")
                await app.workers.wait_for_complete()
                self.assertFalse(app.busy)

    async def test_approved_download_blocks_conflicting_actions_and_recovers_on_failure(self):
        started = threading.Event()
        release = threading.Event()

        def prepare(_release):
            started.set()
            release.wait(timeout=10)
            raise UpdateError("checksum mismatch")

        with patch("agents_status.app.scan_agents", AsyncMock(return_value=[])), patch("agents_status.app.installation_root", return_value=None), patch("agents_status.app.prepare_update", side_effect=prepare):
            app = AgentsStatusApp()
            async with app.run_test(size=(100, 32)) as pilot:
                await app.workers.wait_for_complete()
                app.available_release = RELEASE
                with patch("agents_status.app.installation_root", return_value=Path("/managed")):
                    await pilot.click("#self-update")
                await pilot.click("#apply")
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 2))
                    for name in ("refresh", "update", "delete", "sync", "sync-all", "self-update"):
                        self.assertTrue(app.query_one(f"#{name}", Button).disabled)
                    await pilot.press("q")
                    self.assertFalse(app._quitting)
                    self.assertTrue(app._preparing_update)
                finally:
                    release.set()
                await app.workers.wait_for_complete()
                self.assertIsNone(app.prepared_update)
                self.assertFalse(app.busy)
                self.assertFalse(app._quitting)
                self.assertFalse(app.query_one("#self-update", Button).disabled)


if __name__ == "__main__":
    unittest.main()
