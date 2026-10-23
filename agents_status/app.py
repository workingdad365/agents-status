from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import platform
import subprocess
import sys
import webbrowser

from rich.cells import get_character_cell_size
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.geometry import Offset
from textual.message import Message
from textual.screen import ModalScreen
from textual.selection import Selection
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button, DataTable, Footer, Header, Label, LoadingIndicator, RichLog, Static

from . import __version__
from .browser import RELEASE_PAGES, open_release_page
from .self_update import (
    PreparedUpdate, Release, UpdateError, acknowledge_startup, apply_update,
    check_for_update, discard_prepared_update, installation_root, prepare_update,
)
from .core import (
    AGENTS, AgentSpec, AgentStatus, StatusKind, UpdateCommand, UninstallPlan, config_paths,
    install_command, scan_agents, run_uninstall, run_update, uninstall_plan, update_command,
)
from .instructions import default_instructions_path, sync_instructions
from .defaults import apply_defaults


def _cell_to_index(text: str, cell: int) -> int:
    """셀 위치에 해당하는 문자 인덱스를 구함. 한글 등 2칸 문자를 고려함."""
    width = 0
    for index, character in enumerate(text):
        if width >= cell:
            return index
        width += get_character_cell_size(character)
    return len(text)


class AgentTable(DataTable):
    ALLOW_SELECT = False

    class OpenRelease(Message):
        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key

    async def _on_click(self, event: events.Click) -> None:
        await super()._on_click(event)
        row = event.style.meta.get("row", -1)
        column = event.style.meta.get("column", -1)
        if event.button == 1 and event.chain == 2 and 0 <= row < self.row_count and column >= 0:
            self.post_message(self.OpenRelease(str(self.ordered_rows[row].key.value)))


class SelectableRichLog(RichLog):
    """마우스 드래그로 텍스트를 선택하고 복사할 수 있는 RichLog.

    RichLog는 선택 영역의 텍스트 추출과 강조 표시를 구현하지 않으므로
    Log 위젯과 같은 방식으로 문자 오프셋 메타와 선택 스타일을 적용함.
    """

    def on_mount(self) -> None:
        self._drag_position: Offset | None = None
        self._dragging = False
        self.set_interval(0.05, self._scroll_selection)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 1 and self.scrollable_content_region.contains(*event.screen_offset):
            self._drag_position = event.screen_offset
            self._dragging = False
            self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._drag_position is not None:
            self._dragging |= self._drag_position != event.screen_offset
            self._drag_position = event.screen_offset

    def on_mouse_up(self, event: events.MouseUp) -> None:
        self._drag_position = None
        self.release_mouse()

    def _scroll_selection(self) -> None:
        position = self._drag_position
        screen = self.screen
        if position is None or not self._dragging or not screen._selecting or not self.lines:
            return
        region = self.scrollable_content_region
        direction = -1 if position.y <= region.y else 1 if position.y >= region.bottom - 1 else 0
        if not direction:
            return
        previous_y = self.scroll_y
        self.scroll_relative(y=direction, animate=False, immediate=True)
        if self.scroll_y == previous_y:
            return
        row = min(len(self.lines) - 1, int(self.scroll_y) + (0 if direction < 0 else region.height - 1))
        column = max(0, min(position.x - region.x, region.width - 1)) + int(self.scroll_x)
        # Textual의 선택 시작점은 유지하고 스크롤된 문서 좌표로 끝점만 갱신함.
        screen._select_end = (self, position, Offset(_cell_to_index(self.lines[row].text, column), row))

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        text = "\n".join(strip.text for strip in self.lines)
        selected = selection.extract(text)
        return "\n".join(line.rstrip() for line in selected.split("\n")), "\n"

    def selection_updated(self, selection: Selection | None) -> None:
        self._line_cache.clear()
        self.refresh()

    def _render_line(self, y: int, scroll_x: int, width: int) -> Strip:
        if y >= len(self.lines):
            return Strip.blank(width, self.rich_style)
        selection = self.text_selection
        key = (y + self._start_line, scroll_x, width, self._widest_line_width, selection)
        if key in self._line_cache:
            return self._line_cache[key]
        source = self.lines[y]
        line = source
        span = selection.get_span(y) if selection is not None else None
        if span is not None:
            start, end = span
            text = Text(no_wrap=True, end="")
            for segment in source:
                text.append(segment.text, segment.style)
            text.stylize(self.screen.get_component_rich_style("screen--selection"), start, None if end == -1 else end)
            line = Strip(text.render(self.app.console), source.cell_length)
        line = line.crop_extend(scroll_x, scroll_x + width, self.rich_style)
        line = line.apply_offsets(_cell_to_index(source.text, scroll_x), y)
        self._line_cache[key] = line
        return line


class ConfirmUpdate(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmUpdate { align: center middle; background: $background 70%; }
    ConfirmUpdate > Vertical { width: 64; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    ConfirmUpdate Horizontal { height: auto; align-horizontal: right; margin-top: 1; }
    ConfirmUpdate Button { margin-left: 1; }
    """

    def __init__(self, name: str, command: str, operation: str = "업데이트") -> None:
        super().__init__()
        self.agent_name = name
        self.command = command
        self.operation = operation

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]{self.agent_name}[/b]을(를) {self.operation}하시겠습니까?")
            yield Static(f"[dim]$ {self.command}[/dim]")
            with Horizontal():
                yield Button("취소", id="cancel")
                yield Button(self.operation, id="confirm", variant="primary")

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm")
    def confirm(self) -> None:
        self.dismiss(True)


class ConfirmAppUpdate(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmAppUpdate { align: center middle; background: $background 70%; }
    ConfirmAppUpdate > Vertical { width: 72; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    ConfirmAppUpdate Horizontal { height: auto; align-horizontal: right; margin-top: 1; }
    ConfirmAppUpdate Button { margin-left: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "취소", show=False)]

    def __init__(self, release: Release) -> None:
        super().__init__()
        self.release = release

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"agents-status {__version__} → {self.release.version}")
            yield Static(Text(f"릴리스 노트: {self.release.notes_url}"))
            yield Static("다운로드와 검증을 마치면 앱을 종료하고 새 버전으로 다시 시작합니다.")
            with Horizontal():
                yield Button("나중에", id="later")
                yield Button("업데이트 후 재시작", id="apply", variant="primary")

    @on(Button.Pressed, "#later")
    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#apply")
    def accept(self) -> None:
        self.dismiss(True)


class ConfirmQuit(ConfirmUpdate):
    BINDINGS = [Binding("escape", "keep_running", "계속 실행", show=False)]

    def __init__(self) -> None:
        super().__init__("진행 중인 작업", "", "중단 후 종료")

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("작업이 진행 중입니다. 중단하고 종료하시겠습니까?")
            yield Static("이미 적용된 변경은 롤백되지 않습니다.\n업데이트 중단 시 설치 상태를 별도로 확인해야 합니다.")
            with Horizontal():
                yield Button("계속 실행", id="cancel", variant="primary")
                yield Button("중단 후 종료", id="confirm", variant="error")

    def action_keep_running(self) -> None:
        self.dismiss(False)


class ConfirmDelete(ModalScreen[bool | None]):
    DEFAULT_CSS = """
    ConfirmDelete { align: center middle; background: $background 70%; }
    ConfirmDelete > Vertical { width: 76; height: auto; max-height: 95%; padding: 1 2; border: round $error; background: $surface; overflow-y: auto; }
    ConfirmDelete Horizontal { height: auto; align-horizontal: right; margin-top: 1; }
    ConfirmDelete Button { margin-left: 1; }
    """
    BINDINGS = [
        Binding("y,Y", "answer(True)", "설정 삭제", show=False),
        Binding("n,N", "answer(False)", "설정 보존", show=False),
        Binding("escape", "cancel", "취소", show=False),
    ]

    def __init__(self, status: AgentStatus, plan: UninstallPlan) -> None:
        super().__init__()
        self.status = status
        self.plan = plan

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]{self.status.spec.name}[/b] 삭제 시 설정도 삭제하시겠습니까? (Y/N)")
            yield Static(self.plan.display, markup=False)
            yield Static("Y: 아래 설정·인증·사용 기록까지 삭제\n" + "\n".join(map(str, config_paths(self.status.spec))), markup=False)
            if self.status.spec.key in ("pi", "pi-bolt"):
                yield Static("Pi와 Pi-Bolt는 위 설정을 공유합니다.")
            yield Static("N: 설정을 보존하고 프로그램만 삭제")
            with Horizontal():
                yield Button("취소", id="cancel-delete")
                yield Button("N · 설정 보존", id="keep-config", variant="primary")
                yield Button("Y · 설정 삭제", id="delete-config", variant="error")

    def action_answer(self, delete_config: bool) -> None:
        self.dismiss(delete_config)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed)
    def answer_pressed(self, event: Button.Pressed) -> None:
        self.dismiss({"delete-config": True, "keep-config": False}.get(event.button.id))


def platform_name() -> str:
    """헤더에 표시할 실행 환경 이름(Windows, Linux, Linux (WSL), macOS)."""
    system = platform.system()
    if system == "Linux":
        try:
            if "microsoft" in Path("/proc/version").read_text(encoding="utf-8", errors="replace").lower():
                return "Linux (WSL)"
        except OSError:
            pass
        return "Linux"
    return {"Windows": "Windows", "Darwin": "macOS"}.get(system, system or "unknown")


class AgentsStatusApp(App[None]):
    TITLE = "AI 코딩 에이전트 상태 조회 및 설치 관리"
    SUB_TITLE = platform_name()
    CSS = """
    Screen { layout: vertical; }
    Header, Header.-tall { height: 1; }
    Header HeaderClockSpace { display: none; }
    #app-version { dock: right; width: auto; padding: 0 1; content-align: right middle; height: 100%; }
    #self-update-bar { height: 1; padding: 0 1; }
    #self-update-status { width: 1fr; color: $text-muted; }
    #self-update { width: auto; min-width: 0; height: 1; border: none; padding: 0 1; }
    #summary { height: 3; padding: 1 2; background: $boost; }
    DataTable { height: 1fr; }
    #actions, #defaults-actions { height: 1; padding: 0 1; }
    #actions Button, #defaults-actions Button { width: auto; min-width: 0; height: 1; border: none; padding: 0 1; margin-right: 1; }
    #action-status { height: 1; padding: 0 1; }
    #instructions-source { width: 100%; height: auto; padding: 0 1; content-align: left top; color: $text-muted; }
    #progress { display: none; width: 8; height: 1; }
    #activity { width: 1fr; height: 1; color: $text-muted; }
    RichLog { height: 9; border-top: solid $accent; padding: 0 1; }
    """
    BINDINGS = [
        ("r", "refresh", "새로고침"),
        Binding("R,ㄱ", "refresh", "새로고침", show=False),
        ("u", "update", "업데이트"),
        Binding("U,ㅕ", "update", "업데이트", show=False),
        ("q", "quit", "종료"),
        Binding("Q,ㅂ", "quit", "종료", show=False),
    ]

    def __init__(self, instructions_source: Path | None = None) -> None:
        super().__init__()
        self.statuses: list[AgentStatus] = []
        self.busy = False
        self._quitting = False
        self.available_release: Release | None = None
        self.prepared_update: PreparedUpdate | None = None
        self._checking_release = False
        self._preparing_update = False
        self.instructions_source = (instructions_source if instructions_source is not None else default_instructions_path()).expanduser().resolve()

    def compose(self) -> ComposeResult:
        with Header():
            yield Label(f"v{__version__}", id="app-version")
        yield Static("에이전트 정보를 조회하는 중입니다…", id="summary")
        with Horizontal(id="self-update-bar"):
            yield Label("", id="self-update-status")
            yield Button("앱 업데이트 확인", id="self-update")
        yield AgentTable(id="agents", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="actions"):
            yield Button("새로고침", id="refresh", variant="default")
            yield Button("업데이트", id="update", variant="primary")
            yield Button("삭제", id="delete", variant="error")
            yield Button("지침 동기화", id="sync")
            yield Button("지침 전체 동기화", id="sync-all")
        with Horizontal(id="defaults-actions"):
            yield Button("기본 설정 적용", id="apply-defaults")
        yield Static(Text(f"공통 지침 위치: {self.instructions_source}", overflow="fold", no_wrap=False), id="instructions-source")
        with Horizontal(id="action-status"):
            yield LoadingIndicator(id="progress")
            yield Label("", id="activity")
        yield SelectableRichLog(id="log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        for widget in self.screen.walk_children(Widget):
            widget.ALLOW_SELECT = isinstance(widget, SelectableRichLog)
        table = self.query_one(DataTable)
        table.add_columns("상태", "에이전트", "버전", "프로바이더", "실행 파일")
        acknowledge_startup()
        self.load_statuses()
        if installation_root() is not None:
            self.check_app_release()

    @on(events.TextSelected)
    def copy_selection(self) -> None:
        # macOS는 터미널의 OSC 52 지원 여부와 무관하게 시스템 클립보드에 복사함.
        log = self.query_one("#log", SelectableRichLog)
        selection = log.text_selection
        text = log.get_selection(selection)[0] if selection is not None else ""
        if text:
            if sys.platform == "darwin":
                self.copy_macos_selection(text)
                return
            self.copy_to_clipboard(text)
            self.notify("선택한 텍스트를 클립보드에 복사했습니다.", timeout=2)

    @work(group="clipboard", exclusive=True)
    async def copy_macos_selection(self, text: str) -> None:
        try:
            await asyncio.to_thread(
                subprocess.run, ["/usr/bin/pbcopy"], input=text.encode("utf-8"),
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as error:
            self.notify(f"클립보드 복사에 실패했습니다: {error}", severity="error")
        else:
            self.notify("선택한 텍스트를 클립보드에 복사했습니다.", timeout=2)

    @on(AgentTable.OpenRelease)
    def release_requested(self, event: AgentTable.OpenRelease) -> None:
        self.open_agent_release(event.key)

    @work(group="browser", exclusive=True)
    async def open_agent_release(self, key: str) -> None:
        try:
            await asyncio.to_thread(open_release_page, key)
        except (OSError, subprocess.TimeoutExpired, webbrowser.Error) as error:
            self.notify(f"{error}\n{RELEASE_PAGES[key]}", title="브라우저 실행 실패", severity="error", timeout=8)

    @work(exclusive=True, group="scan")
    async def load_statuses(self) -> None:
        self.busy = True
        self._set_buttons(True)
        try:
            self.statuses = await scan_agents()
            self._render_table()
            self.query_one("#log", RichLog).write("[green]상태 조회를 완료했습니다.[/green]")
        finally:
            self.busy = False
            self._set_buttons(False)

    def _render_table(self) -> None:
        table = self.query_one(DataTable)
        selected_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key if table.row_count else None
        table.clear()
        colors = {
            StatusKind.INSTALLED: "green",
            StatusKind.CONFIG_ONLY: "yellow",
            StatusKind.MISSING: "dim",
        }
        for status in sorted(self.statuses, key=lambda item: item.spec.name.casefold()):
            path = str(status.executable) if status.executable else "-"
            table.add_row(
                f"[{colors[status.status]}]{status.status}[/{colors[status.status]}]",
                status.spec.name, status.version, status.provider, path,
                key=status.spec.key,
            )
        if selected_key in table.rows:
            table.move_cursor(row=table.get_row_index(selected_key))
        counts = {kind: sum(item.status == kind for item in self.statuses) for kind in StatusKind}
        self.query_one("#summary", Static).update(
            f"[b]합계 {len(self.statuses)}개[/b]  "
            f"[green]설치 {counts[StatusKind.INSTALLED]}[/green]  "
            f"[yellow]설정만 {counts[StatusKind.CONFIG_ONLY]}[/yellow]  "
            f"[dim]미설치 {counts[StatusKind.MISSING]}[/dim]"
        )

    def _selected(self) -> AgentStatus | None:
        table = self.query_one(DataTable)
        if not self.statuses or not table.row_count or table.cursor_row < 0:
            return None
        key = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        return next((status for status in self.statuses if status.spec.key == key), None)

    def _set_buttons(self, disabled: bool) -> None:
        self.query_one("#refresh", Button).disabled = disabled
        status = self._selected()
        operation = "설치" if status and status.status != StatusKind.INSTALLED else "업데이트"
        button = self.query_one("#update", Button)
        button.label = operation
        button.disabled = disabled or status is None
        self.query_one("#delete", Button).disabled = disabled or status is None or status.status == StatusKind.MISSING
        self.query_one("#sync", Button).disabled = disabled or status is None
        self.query_one("#sync-all", Button).disabled = disabled
        self.query_one("#apply-defaults", Button).disabled = disabled
        self.query_one("#self-update", Button).disabled = disabled or self._checking_release

    @work(exclusive=True, group="release-check")
    async def check_app_release(self) -> None:
        self._checking_release = True
        self.query_one("#self-update", Button).disabled = True
        status = self.query_one("#self-update-status", Label)
        status.update("앱 새 버전 확인 중…")
        try:
            self.available_release = await asyncio.to_thread(check_for_update)
            if self.available_release is not None:
                status.update(f"새 버전 {self.available_release.version} 사용 가능")
                self.query_one("#self-update", Button).label = "앱 업데이트"
                self.notify(f"agents-status {self.available_release.version} 업데이트가 있습니다.", timeout=8)
            else:
                status.update(f"현재 버전 {__version__}")
                self.query_one("#self-update", Button).label = "앱 업데이트 확인"
        except (UpdateError, OSError, ValueError) as error:
            status.update("앱 업데이트 확인 불가 · 현재 기능은 계속 사용할 수 있습니다.")
            self.query_one("#log", RichLog).write(Text(f"앱 업데이트 확인 실패: {error}"))
        finally:
            self._checking_release = False
            self.query_one("#self-update", Button).disabled = self.busy

    @on(Button.Pressed, "#self-update")
    def app_update_pressed(self) -> None:
        if self.busy or self._checking_release:
            return
        if self.available_release is None:
            self.check_app_release()
            return
        if installation_root() is None:
            self.notify(
                "소스·uv 설치는 자체 업데이트하지 않습니다. README의 릴리스 설치 전환 절차를 사용해 주세요.",
                title="릴리스 설치 필요", severity="warning", timeout=10,
            )
            return
        release = self.available_release
        self.push_screen(ConfirmAppUpdate(release), lambda accepted: self._accept_app_update(release) if accepted else None)

    def _accept_app_update(self, release: Release) -> None:
        if self.busy or self._preparing_update:
            return
        self.busy = True
        self._preparing_update = True
        self._set_buttons(True)
        self.prepare_app_update(release)

    @work(exclusive=True, group="self-update")
    async def prepare_app_update(self, release: Release) -> None:
        self.query_one("#progress", LoadingIndicator).styles.display = "block"
        self.query_one("#activity", Label).update("앱 업데이트 다운로드 및 검증 중…")
        try:
            self.prepared_update = await asyncio.to_thread(prepare_update, release)
        except (UpdateError, OSError, ValueError) as error:
            self.query_one("#log", RichLog).write(Text(f"앱 업데이트 실패: {error}"))
            self.notify("앱 업데이트에 실패했습니다. 기존 버전을 유지합니다.", severity="error", timeout=8)
        finally:
            self._preparing_update = False
            self.busy = False
            self._set_buttons(False)
            self.query_one("#progress", LoadingIndicator).styles.display = "none"
            self.query_one("#activity", Label).update("")
        if self.prepared_update is not None:
            self._quitting = True
            self.exit()

    @on(Button.Pressed, "#sync")
    def sync_pressed(self) -> None:
        if not self.busy and (status := self._selected()) is not None:
            self._start_sync((status.spec,))

    @on(Button.Pressed, "#sync-all")
    def sync_all_pressed(self) -> None:
        if not self.busy:
            self._start_sync(AGENTS)

    @on(Button.Pressed, "#apply-defaults")
    def defaults_pressed(self) -> None:
        if not self.busy:
            self.busy = True
            self._set_buttons(True)
            self.apply_agent_defaults()

    @work(exclusive=True, group="update")
    async def apply_agent_defaults(self) -> None:
        log = self.query_one("#log", RichLog)
        self.query_one("#progress", LoadingIndicator).styles.display = "block"
        self.query_one("#activity", Label).update("기본 설정 적용 중…")
        try:
            results = await asyncio.to_thread(apply_defaults, tuple(self.statuses))
            for result in results:
                detail = f" ({result.detail})" if result.detail else ""
                log.write(Text(f"[{result.outcome}] {result.name}: {result.destination}{detail}"))
            written = sum(result.outcome == "write" for result in results)
            skipped = sum(result.outcome == "skip" for result in results)
            failed = sum(result.outcome == "error" for result in results)
            summary = f"기본 설정 적용: {written}개 작성, {skipped}개 건너뜀, {failed}개 실패"
            log.write(Text(summary))
            self.notify(summary, severity="error" if failed else "information")
        finally:
            self.query_one("#progress", LoadingIndicator).styles.display = "none"
            self.query_one("#activity", Label).update("")
            self.busy = False
            self._set_buttons(False)

    def _start_sync(self, specs: tuple[AgentSpec, ...]) -> None:
        self.busy = True
        self._set_buttons(True)
        self.sync_agents(specs)

    @work(exclusive=True, group="update")
    async def sync_agents(self, specs: tuple[AgentSpec, ...]) -> None:
        log = self.query_one("#log", RichLog)
        log.write(Text(f"지침 동기화 원본: {self.instructions_source}"))
        self.query_one("#progress", LoadingIndicator).styles.display = "block"
        self.query_one("#activity", Label).update("지침 동기화 진행 중…")
        try:
            results = await asyncio.to_thread(sync_instructions, self.instructions_source, specs)
            for result in results:
                detail = f" ({result.detail})" if result.detail else ""
                log.write(Text(f"[{result.outcome}] {result.name}: {result.destination}{detail}"))
            written = sum(result.outcome in ("copy", "merge") for result in results)
            skipped = sum(result.outcome == "skip" for result in results)
            failed = sum(result.outcome == "error" for result in results)
            summary = f"지침 동기화: {written}개 작성, {skipped}개 건너뜀, {failed}개 실패"
            log.write(Text(summary))
            self.notify(summary, severity="error" if failed else "information")
        except (OSError, UnicodeError, ValueError) as error:
            log.write(Text(f"지침 동기화 실패: {error}"))
            self.notify("지침 동기화에 실패했습니다. 원본 경로와 로그를 확인해 주세요.", severity="error")
        finally:
            self.query_one("#progress", LoadingIndicator).styles.display = "none"
            self.query_one("#activity", Label).update("")
            self.busy = False
            self._set_buttons(False)

    @on(DataTable.RowHighlighted)
    def selection_changed(self) -> None:
        if self.is_running:
            self._set_buttons(self.busy)

    @on(Button.Pressed, "#delete")
    def delete_pressed(self) -> None:
        if self.busy:
            return
        status = self._selected()
        if status is None or status.status == StatusKind.MISSING:
            return
        plan, reason = uninstall_plan(status)
        if plan is None:
            self.notify(reason, title=status.spec.name, severity="warning", timeout=5)
            return
        self.push_screen(
            ConfirmDelete(status, plan),
            lambda answer: self.delete_agent(status, plan, answer) if answer is not None else None,
        )

    @work(exclusive=True, group="update")
    async def delete_agent(self, status: AgentStatus, plan: UninstallPlan, delete_config: bool) -> None:
        self.busy = True
        self._set_buttons(True)
        log = self.query_one("#log", RichLog)
        log.write(Text(f"{status.spec.name} 삭제: {plan.display}"))
        self.query_one("#progress", LoadingIndicator).styles.display = "block"
        self.query_one("#activity", Label).update(f"{status.spec.name} 삭제 진행 중…")
        try:
            result = await run_uninstall(
                plan, config_paths(status.spec) if delete_config else (),
                on_output=lambda line: log.write(Text.from_ansi(line)),
            )
            log.write(Text(result.output))
            self.notify("삭제를 완료했습니다." if result.returncode == 0 else "삭제에 실패했습니다. 로그를 확인해 주세요.",
                        title=status.spec.name, severity="information" if result.returncode == 0 else "error")
        finally:
            self.query_one("#progress", LoadingIndicator).styles.display = "none"
            self.query_one("#activity", Label).update("")
            self.busy = False
            self._set_buttons(False)
            if not self._quitting:
                self.load_statuses()

    @on(Button.Pressed, "#refresh")
    def refresh_pressed(self) -> None:
        self.action_refresh()

    @on(Button.Pressed, "#update")
    def update_pressed(self) -> None:
        self.action_update()

    async def action_quit(self) -> None:
        if self._preparing_update:
            self.notify("앱 업데이트 검증이 끝날 때까지 기다려 주세요.", severity="warning")
            return
        if self._quitting or isinstance(self.screen, ConfirmQuit):
            return
        if self.busy:
            self.push_screen(ConfirmQuit(), self._confirm_quit)
        else:
            self.exit()

    async def _confirm_quit(self, confirmed: bool) -> None:
        if not confirmed:
            return
        self._quitting = True
        workers = list(self.workers)
        self.workers.cancel_all()
        await asyncio.gather(*(worker.wait() for worker in workers), return_exceptions=True)
        self.exit()

    def action_refresh(self) -> None:
        if not self.busy:
            self.query_one("#log", RichLog).write("상태를 다시 조회합니다…")
            self.load_statuses()

    def action_update(self) -> None:
        if self.busy:
            return
        status = self._selected()
        if status is None:
            self.notify("에이전트를 선택해 주세요.", severity="warning")
            return
        operation = "업데이트" if status.status == StatusKind.INSTALLED else "설치"
        command, reason = update_command(status) if operation == "업데이트" else install_command(status)
        if command is None:
            self.notify(reason, title=status.spec.name, severity="warning", timeout=5)
            return
        if operation == "설치":
            self.update_agent(status, command)
            return
        self.push_screen(
            ConfirmUpdate(status.spec.name, command.display, operation),
            lambda confirmed: self.update_agent(status, command) if confirmed else None,
        )

    @work(exclusive=True, group="update")
    async def update_agent(self, status: AgentStatus, command: UpdateCommand) -> None:
        self.busy = True
        self._set_buttons(True)
        log = self.query_one("#log", RichLog)
        operation = "업데이트" if status.status == StatusKind.INSTALLED else "설치"
        log.write(f"[bold]{status.spec.name} {operation}[/bold]: $ {command.display}")
        log.write(f"[yellow]{operation}를 시작했습니다. 완료될 때까지 기다려 주세요.[/yellow]")
        self.query_one("#progress", LoadingIndicator).styles.display = "block"
        self.query_one("#activity", Label).update(f"{status.spec.name} {operation} 진행 중…")
        warned = False

        def receive_output(line: str) -> None:
            nonlocal warned
            log.write(Text.from_ansi(line))
            if status.spec.key == "openclaw" and not warned and "Could not inspect OpenClaw processes" in line:
                warned = True
                log.write(Text(
                    "OpenClaw의 프로세스 검사에 실패했습니다. 다른 격리 환경의 실행 경로를 "
                    "해석하지 못한 경우일 수 있습니다. still running은 진행률이 아니며, "
                    "package-install 성공만으로 업데이트 완료를 의미하지 않습니다. "
                    "임시 런타임을 삭제하지 말고 OpenClaw 상태를 확인해 주세요."
                ))
                self.notify("OpenClaw 내부 프로세스 검사 오류: 로그를 확인해 주세요.", severity="warning", timeout=10)

        try:
            result = await run_update(command, on_output=receive_output)
            if result.returncode == 0:
                self.notify(f"{operation}를 완료했습니다.", title=status.spec.name)
                log.write(f"[green]{operation}를 완료했습니다.[/green]")
            else:
                self.notify(f"{operation} 실패(종료 코드 {result.returncode})", title=status.spec.name, severity="error")
                log.write(f"[red]{operation} 실패(종료 코드 {result.returncode})[/red]")
        finally:
            self.query_one("#progress", LoadingIndicator).styles.display = "none"
            self.query_one("#activity", Label).update("")
            self.busy = False
            self._set_buttons(False)
            if not self._quitting:
                self.load_statuses()


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 코딩 에이전트 상태 및 글로벌 지침 관리")
    parser.add_argument("--version", action="version", version=f"agents-status {__version__}")
    parser.add_argument("--self-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--instructions", type=Path, help="동기화할 글로벌 지침 원본 경로")
    args = parser.parse_args()
    if args.self_check:
        from .self_update import _ssl_context

        _ssl_context()
        source = default_instructions_path()
        if not source.is_file() or not source.read_text(encoding="utf-8").strip():
            parser.exit(1, "번들 글로벌 지침 파일을 읽을 수 없습니다.\n")
        if get_character_cell_size("한") != 2:
            parser.exit(1, "한글 렌더링 자원을 불러오지 못했습니다.\n")
        print(f"agents-status {__version__} ready")
        return
    app = AgentsStatusApp(instructions_source=args.instructions)
    try:
        app.run()
        if app.return_code:
            raise SystemExit(app.return_code)
        if isinstance(app.prepared_update, PreparedUpdate):
            try:
                result = apply_update(app.prepared_update, sys.argv[1:])
            except (UpdateError, OSError, ValueError) as error:
                print(f"앱 업데이트 실패: {error}", file=sys.stderr)
                result = 1
            raise SystemExit(result)
    finally:
        if isinstance(app.prepared_update, PreparedUpdate):
            discard_prepared_update(app.prepared_update)


if __name__ == "__main__":
    main()
