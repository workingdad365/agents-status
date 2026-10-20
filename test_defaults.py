import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from textual.widgets import Button

from agents_status.app import AgentsStatusApp
from agents_status.core import AGENTS, AgentStatus, StatusKind
from agents_status.defaults import apply_defaults


def statuses(kind=StatusKind.INSTALLED):
    return [AgentStatus(spec, kind, Path(spec.binaries[0]) if kind == StatusKind.INSTALLED else None, "-", "-")
            for spec in AGENTS]


class DefaultsTests(unittest.TestCase):
    def test_creation_and_idempotence(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            results = apply_defaults(statuses(), home=home, env={})
            self.assertEqual([r.outcome for r in results], ["write"] * 3)
            contents = {r.destination: r.destination.read_bytes() for r in results}
            self.assertEqual(json.loads((home / ".commandcode/config.json").read_text()), {"tasteLearning": False})
            self.assertEqual(json.loads((home / ".config/crush/crush.json").read_text())["options"]["data_directory"], str(home / ".local/share/crush/project-data"))
            self.assertEqual(json.loads((home / ".config/opencode/cli.json").read_text()), {
                "$schema": "https://opencode.ai/v2/cli.json", "terminal": {"copy": "select"},
            })
            self.assertTrue(all(r.outcome == "skip" for r in apply_defaults(statuses(), home=home, env={})))
            self.assertEqual(contents, {p: p.read_bytes() for p in contents})

    def test_missing_agents_do_not_create_files(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.assertTrue(all(r.outcome == "skip" for r in apply_defaults(statuses(StatusKind.MISSING), home=home, env={})))
            self.assertEqual(list(home.iterdir()), [])

    def test_preserves_existing_values_and_merges_nested_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = {
                home / ".commandcode/config.json": {"tasteLearning": True, "model": "custom"},
                home / ".config/crush/crush.json": {"options": {"debug": True}, "providers": {"custom": {}}},
                home / ".config/opencode/cli.json": {"terminal": {"copy": "manual", "title": False}},
            }
            for path, content in paths.items():
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(content))
            results = apply_defaults(statuses(), home=home, env={})
            self.assertEqual(results[0].outcome, "skip")
            self.assertEqual(json.loads((home / ".commandcode/config.json").read_text()), paths[home / ".commandcode/config.json"])
            crush = json.loads((home / ".config/crush/crush.json").read_text())
            self.assertTrue(crush["options"]["debug"])
            self.assertEqual(crush["providers"], {"custom": {}})
            self.assertEqual(json.loads((home / ".config/opencode/cli.json").read_text())["terminal"], {"copy": "manual", "title": False})

    def test_invalid_file_is_preserved_and_other_agents_continue(self):
        for content in ['{invalid', '[]', 'null']:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                path = home / ".commandcode/config.json"
                path.parent.mkdir()
                path.write_text(content)
                results = apply_defaults(statuses(), home=home, env={})
                self.assertEqual(results[0].outcome, "error")
                self.assertEqual(path.read_text(), content)
                self.assertEqual([r.outcome for r in results[1:]], ["write", "write"])

    def test_environment_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            env = {"XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
                   "CRUSH_GLOBAL_CONFIG": str(home / "crush-config")}
            apply_defaults(statuses(), home=home, env=env)
            self.assertTrue((home / "config/opencode/cli.json").is_file())
            self.assertEqual(json.loads((home / "crush-config/crush.json").read_text())["options"]["data_directory"], str(home / "data/crush/project-data"))


class DefaultsButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_button_applies_all_statuses_and_respects_busy(self):
        items = statuses()
        with patch("agents_status.app.scan_agents", AsyncMock(return_value=items)), patch("agents_status.app.apply_defaults", return_value=[]) as apply:
            app = AgentsStatusApp()
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.pause()
                await pilot.click("#apply-defaults")
                await app.workers.wait_for_complete()
                apply.assert_called_once_with(tuple(items))
                self.assertFalse(app.busy)
                self.assertFalse(app.query_one("#apply-defaults", Button).disabled)
                app.busy = True
                app._set_buttons(True)
                self.assertTrue(app.query_one("#apply-defaults", Button).disabled)
                app.defaults_pressed()
                apply.assert_called_once()
                app.busy = False
