import asyncio
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from textual.widgets import Button, DataTable, LoadingIndicator, RichLog

from agents_status.app import AgentsStatusApp
from agents_status.core import AGENTS, AgentStatus, StatusKind
from agents_status.instructions import (
    MARK_BEGIN, MARK_END, SyncResult, _atomic_write,
    default_instructions_path, sync_instructions,
)


SPECS = {spec.key: spec for spec in AGENTS}
SOURCE = "# Global Instructions (ver. 20261009)\n\n- 새 지침이다.\n"


class InstructionsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.source = self.home / "GLOBAL_INSTRUCTIONS.md"
        self.source.write_text(SOURCE, encoding="utf-8")
        self.env = {"HERMES_HOME": str(self.home / ".hermes"), "PATH": ""}

    def write(self, relative, content):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        return path

    def sync(self, *keys):
        specs = tuple(SPECS[key] for key in keys) if keys else AGENTS
        return sync_instructions(self.source, specs, home=self.home, env=self.env)

    def test_copy_destinations_and_creation_of_nested_directories(self):
        destinations = {
            "claude": ".claude/CLAUDE.md", "codex": ".codex/AGENTS.md",
            "commandcode": ".commandcode/AGENTS.md", "copilot": ".copilot/copilot-instructions.md",
            "opencode": ".config/opencode/AGENTS.md", "kilo": ".config/kilo/AGENTS.md",
            "omp": ".omp/agent/AGENTS.md", "pi": ".pi/agent/AGENTS.md",
            "pi-bolt": ".pi/agent/AGENTS.md",
            "antigravity": ".gemini/GEMINI.md", "crush": ".config/crush/CRUSH.md",
        }
        for key in destinations:
            (self.home / SPECS[key].config_root).mkdir(parents=True, exist_ok=True)
        old = self.write(destinations["claude"], "기존 지침")
        results = self.sync(*destinations)
        self.assertEqual([result.outcome for result in results], ["copy"] * len(destinations))
        for relative in destinations.values():
            self.assertEqual((self.home / relative).read_bytes(), self.source.read_bytes())
        self.assertNotIn("기존 지침", old.read_text(encoding="utf-8"))

    def test_selected_agent_does_not_modify_other_agents(self):
        target = self.write(".claude/CLAUDE.md", "Claude 기존")
        other = self.write(".codex/AGENTS.md", "Codex 기존")
        self.assertEqual(len(self.sync("claude")), 1)
        self.assertEqual(target.read_text(encoding="utf-8"), SOURCE)
        self.assertEqual(other.read_text(encoding="utf-8"), "Codex 기존")

    def test_all_agents_are_skipped_without_config_directories(self):
        results = self.sync()
        self.assertEqual(len(results), len(AGENTS))
        self.assertTrue(all(result.outcome == "skip" for result in results))
        self.assertEqual(list(self.home.iterdir()), [self.source])

    def test_merge_creates_file_and_is_idempotent(self):
        for key in ("hermes", "openclaw"):
            (self.home / SPECS[key].config_root).mkdir()
        results = self.sync("hermes", "openclaw")
        expected = f"{MARK_BEGIN}\n{SOURCE}{MARK_END}\n"
        for result in results:
            self.assertEqual(result.outcome, "merge")
            self.assertEqual(result.destination.read_text(encoding="utf-8"), expected)
        self.sync("hermes", "openclaw")
        for result in results:
            self.assertEqual(result.destination.read_text(encoding="utf-8"), expected)

    def test_merge_replaces_only_block_and_preserves_crlf(self):
        path = self.write(".openclaw/workspace/AGENTS.md", f"앞 내용\r\n\r\n{MARK_BEGIN}\r\n이전 지침\r\n{MARK_END}\r\n뒤 내용")
        self.assertEqual(self.sync("openclaw")[0].outcome, "merge")
        expected = f"앞 내용\n\n{MARK_BEGIN}\n{SOURCE}{MARK_END}\n뒤 내용".replace("\n", "\r\n")
        self.assertEqual(path.read_bytes(), expected.encode("utf-8"))

    def test_merge_appends_to_existing_content_without_final_newline(self):
        path = self.write(".hermes/SOUL.md", "기본 성격")
        self.source.write_bytes(SOURCE.rstrip().encode("utf-8"))
        self.sync("hermes")
        self.assertEqual(path.read_text(encoding="utf-8"), f"기본 성격\n\n{MARK_BEGIN}\n{SOURCE}{MARK_END}\n")

    def test_legacy_hermes_restores_default_and_openclaw_discards_old_copy(self):
        hermes = self.write(".hermes/SOUL.md", "# Global Instructions (ver. 20260101)\n기존 지침")
        self.write(".hermes/hermes-agent/SOUL.md", "기본 성격")
        openclaw = self.write(".openclaw/workspace/AGENTS.md", "# Global Instructions (ver. 20260101)\n기존 지침")
        self.sync("hermes", "openclaw")
        block = f"{MARK_BEGIN}\n{SOURCE}{MARK_END}\n"
        self.assertEqual(hermes.read_text(encoding="utf-8"), "기본 성격\n\n" + block)
        self.assertEqual(openclaw.read_text(encoding="utf-8"), block)

    def test_legacy_hermes_without_default_becomes_block(self):
        path = self.write(".hermes/SOUL.md", "# Global Instructions (ver. old)\n기존")
        self.sync("hermes")
        self.assertEqual(path.read_text(encoding="utf-8"), f"{MARK_BEGIN}\n{SOURCE}{MARK_END}\n")

    def test_invalid_markers_preserve_file(self):
        cases = [MARK_BEGIN, MARK_END, f"{MARK_END}\n{MARK_BEGIN}\n",
                 f"{MARK_BEGIN}\n{MARK_BEGIN}\n{MARK_END}\n"]
        for existing in cases:
            with self.subTest(existing=existing):
                path = self.write(".hermes/SOUL.md", existing)
                result = self.sync("hermes")[0]
                self.assertEqual(result.outcome, "error")
                self.assertEqual(path.read_text(encoding="utf-8"), existing)

    def test_invalid_source_never_modifies_targets(self):
        target = self.write(".claude/CLAUDE.md", "기존 지침")
        for data in (b"", b"  \n", b"\xff", MARK_BEGIN.encode()):
            with self.subTest(data=data):
                self.source.write_bytes(data)
                with self.assertRaises((ValueError, UnicodeError)):
                    self.sync("claude")
                self.assertEqual(target.read_text(encoding="utf-8"), "기존 지침")
        self.source.unlink()
        with self.assertRaises(FileNotFoundError):
            self.sync("claude")
        self.assertEqual(target.read_text(encoding="utf-8"), "기존 지침")

    def test_failed_target_does_not_stop_later_targets(self):
        self.write(".claude/CLAUDE.md", "기존")
        codex = self.write(".codex/AGENTS.md", "기존")

        def write(destination, content):
            if destination.name == "CLAUDE.md":
                raise PermissionError("접근 거부")
            _atomic_write(destination, content)

        with patch("agents_status.instructions._atomic_write", side_effect=write):
            results = self.sync("claude", "codex")
        self.assertEqual([result.outcome for result in results], ["error", "copy"])
        self.assertEqual(codex.read_text(encoding="utf-8"), SOURCE)

    def test_failed_replace_preserves_file_and_cleans_temporary_file(self):
        target = self.write(".claude/CLAUDE.md", "기존")
        with patch("pathlib.Path.replace", side_effect=PermissionError("접근 거부")):
            self.assertEqual(self.sync("claude")[0].outcome, "error")
        self.assertEqual(target.read_text(encoding="utf-8"), "기존")
        self.assertEqual(list(target.parent.iterdir()), [target])

    @unittest.skipIf(sys.platform == "win32", "POSIX 파일 권한 및 링크 검증")
    def test_sync_preserves_symlink_and_file_permissions(self):
        actual = self.write("shared.md", "기존")
        actual.chmod(0o640)
        link = self.home / ".claude/CLAUDE.md"
        link.parent.mkdir()
        link.symlink_to(actual)
        self.sync("claude")
        self.assertTrue(link.is_symlink())
        self.assertEqual(actual.read_text(encoding="utf-8"), SOURCE)
        self.assertEqual(actual.stat().st_mode & 0o777, 0o640)

    def test_crush_custom_paths_and_data_only_installation(self):
        config = self.home / "crush-config"
        data = self.home / "crush-data"
        data.mkdir()
        self.env.update(CRUSH_GLOBAL_CONFIG=str(config), CRUSH_GLOBAL_DATA=str(data))
        result = self.sync("crush")[0]
        self.assertEqual(result.outcome, "copy")
        self.assertEqual(result.destination, config / "CRUSH.md")

    def test_crush_xdg_paths_and_binary_only_installation(self):
        config = self.home / "config"
        self.env["XDG_CONFIG_HOME"] = str(config)
        with patch("agents_status.instructions._which", return_value="/bin/crush"):
            result = self.sync("crush")[0]
        self.assertEqual(result.outcome, "copy")
        self.assertEqual(result.destination, config / "crush/CRUSH.md")

    def test_crush_xdg_data_directory(self):
        data = self.home / "data"
        (data / "crush").mkdir(parents=True)
        self.env["XDG_DATA_HOME"] = str(data)
        self.assertEqual(self.sync("crush")[0].outcome, "copy")

    def test_hermes_custom_home_and_windows_default(self):
        custom = self.home / "custom-hermes"
        custom.mkdir()
        self.env["HERMES_HOME"] = str(custom)
        self.assertEqual(self.sync("hermes")[0].destination, custom / "SOUL.md")
        del self.env["HERMES_HOME"]
        local = self.home / "local"
        (local / "hermes").mkdir(parents=True)
        self.env["LOCALAPPDATA"] = str(local)
        with patch("agents_status.core.sys.platform", "win32"):
            result = self.sync("hermes")[0]
        self.assertEqual(result.outcome, "merge")
        self.assertEqual(result.destination, local / "hermes/SOUL.md")

    def test_default_source_is_independent_of_working_directory(self):
        with patch("pathlib.Path.cwd", return_value=self.home):
            source = default_instructions_path()
        self.assertTrue(source.is_file())
        self.assertEqual(source.name, "GLOBAL_INSTRUCTIONS.md")


class SyncAppTests(unittest.IsolatedAsyncioTestCase):
    def statuses(self):
        return [AgentStatus(SPECS[key], StatusKind.CONFIG_ONLY, None, "-", "-") for key in ("claude", "codex")]

    async def test_selected_and_all_buttons_use_correct_targets(self):
        source = Path("/tmp/custom-instructions.md")
        with patch("agents_status.app.scan_agents", AsyncMock(return_value=self.statuses())), patch("agents_status.app.sync_instructions", return_value=[]) as sync:
            app = AgentsStatusApp(source)
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.pause()
                app.query_one(DataTable).move_cursor(row=1)
                await pilot.pause()
                await pilot.click("#sync")
                await app.workers.wait_for_complete()
                sync.assert_called_once_with(source.resolve(), (SPECS["codex"],))
                await pilot.click("#sync-all")
                await app.workers.wait_for_complete()
                self.assertEqual(sync.call_count, 2)
                self.assertEqual(sync.call_args.args, (source.resolve(), AGENTS))

    async def test_sync_buttons_write_only_to_temporary_home(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / "source.md"
            source.write_text(SOURCE, encoding="utf-8")
            for key in ("claude", "codex"):
                (home / SPECS[key].config_root).mkdir()

            def sync(source, specs):
                return sync_instructions(source, specs, home=home, env={"PATH": ""})

            with patch("agents_status.app.scan_agents", AsyncMock(return_value=self.statuses())), patch("agents_status.app.sync_instructions", side_effect=sync):
                app = AgentsStatusApp(source)
                async with app.run_test(size=(80, 30)) as pilot:
                    await pilot.pause()
                    await pilot.click("#sync")
                    await app.workers.wait_for_complete()
                    self.assertEqual((home / ".claude/CLAUDE.md").read_text(encoding="utf-8"), SOURCE)
                    self.assertFalse((home / ".codex/AGENTS.md").exists())
                    await pilot.click("#sync-all")
                    await app.workers.wait_for_complete()
                    self.assertEqual((home / ".codex/AGENTS.md").read_text(encoding="utf-8"), SOURCE)
                    log = "\n".join(line.text for line in app.query_one(RichLog).lines)
                self.assertIn(f"2개 작성, {len(AGENTS) - 2}개 건너뜀, 0개 실패", log)

    async def test_sync_disables_conflicting_actions_until_finished(self):
        started = threading.Event()
        release = threading.Event()

        def sync(*args):
            started.set()
            release.wait(timeout=10)
            return []

        with patch("agents_status.app.scan_agents", AsyncMock(return_value=self.statuses())) as scan, patch("agents_status.app.sync_instructions", side_effect=sync) as run:
            app = AgentsStatusApp()
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.pause()
                await pilot.click("#sync")
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 2))
                    for button in ("refresh", "update", "delete", "sync", "sync-all"):
                        self.assertTrue(app.query_one(f"#{button}", Button).disabled)
                    app.sync_pressed()
                    app.sync_all_pressed()
                    app.action_refresh()
                    app.action_update()
                    app.delete_pressed()
                    self.assertEqual(run.call_count, 1)
                    self.assertEqual(scan.await_count, 1)
                finally:
                    release.set()
                await app.workers.wait_for_complete()
                self.assertFalse(app.busy)
                self.assertFalse(app.query_one("#sync", Button).disabled)

    async def test_source_error_is_logged_and_controls_recover(self):
        with patch("agents_status.app.scan_agents", AsyncMock(return_value=self.statuses())), patch("agents_status.app.sync_instructions", side_effect=FileNotFoundError("원본 없음")):
            app = AgentsStatusApp()
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.pause()
                await pilot.click("#sync-all")
                await app.workers.wait_for_complete()
                self.assertFalse(app.busy)
                self.assertFalse(app.query_one("#sync-all", Button).disabled)
                self.assertEqual(app.query_one(LoadingIndicator).styles.display, "none")
                self.assertIn("원본 없음", "\n".join(line.text for line in app.query_one(RichLog).lines))

    async def test_partial_failure_is_reported_with_counts(self):
        results = [SyncResult("Claude", Path("CLAUDE.md"), "copy"),
                   SyncResult("Codex CLI", Path("AGENTS.md"), "error", "접근 거부")]
        with patch("agents_status.app.scan_agents", AsyncMock(return_value=self.statuses())), patch("agents_status.app.sync_instructions", return_value=results):
            app = AgentsStatusApp()
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.pause()
                await pilot.click("#sync-all")
                await app.workers.wait_for_complete()
                log = "\n".join(line.text for line in app.query_one(RichLog).lines)
                self.assertIn("1개 작성, 0개 건너뜀, 1개 실패", log)
                self.assertIn("접근 거부", log)


if __name__ == "__main__":
    unittest.main()
