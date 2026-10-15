from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import platform

from rich.cells import get_character_cell_size
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import Button, DataTable, Footer, Header, Label, LoadingIndicator, RichLog, Static

from .core import (
    AGENTS, AgentSpec, AgentStatus, StatusKind, UpdateCommand, UninstallPlan, config_paths,
    install_command, scan_agents, run_uninstall, run_update, uninstall_plan, update_command,
)
from .instructions import default_instructions_path, sync_instructions


def _cell_to_index(text: str, cell: int) -> int:
    """셀 위치에 해당하는 문자 인덱스를 구함. 한글 등 2칸 문자를 고려함."""
    width = 0
    for index, character in enumerate(text):
        if width >= cell:
            return index
        width += get_character_cell_size(character)
    return len(text)


class SelectableRichLog(RichLog):
    """마우스 드래그로 텍스트를 선택하고 복사할 수 있는 RichLog.

    RichLog는 선택 영역의 텍스트 추출과 강조 표시를 구현하지 않으므로
    Log 위젯과 같은 방식으로 문자 오프셋 메타와 선택 스타일을 적용함.
    """

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
    Header HeaderClockSpace { display: none; }
    #app-version { dock: right; width: auto; padding: 0 1; content-align: right middle; height: 100%; }
    #summary { height: 3; padding: 1 2; background: $boost; }
    DataTable { height: 1fr; }
    #actions { height: 1; padding: 0 1; }
    #actions Button { width: auto; min-width: 0; height: 1; border: none; padding: 0 1; margin-right: 1; }
    #action-status { height: 1; padding: 0 1; }
    #instructions-source { width: 1fr; height: 1; color: $text-muted; }
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
        self.instructions_source = (instructions_source if instructions_source is not None else default_instructions_path()).expanduser().resolve()

    def compose(self) -> ComposeResult:
        with Header():
            yield Label("v1.0.10", id="app-version")
        yield Static("에이전트 정보를 조회하는 중입니다…", id="summary")
        yield DataTable(id="agents", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="actions"):
            yield Button("새로고침", id="refresh", variant="default")
            yield Button("업데이트", id="update", variant="primary")
            yield Button("삭제", id="delete", variant="error")
            yield Button("지침 동기화", id="sync")
            yield Button("전체 동기화", id="sync-all")
        with Horizontal(id="action-status"):
            yield LoadingIndicator(id="progress")
            yield Label("", id="activity")
            source = Label(Text(f"원본: {self.instructions_source}"), id="instructions-source")
            source.tooltip = str(self.instructions_source)
            yield source
        yield SelectableRichLog(id="log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("상태", "에이전트", "버전", "프로바이더", "실행 파일")
        self.load_statuses()

    @on(events.TextSelected)
    def copy_selection(self) -> None:
        # 마우스 버튼을 놓으면 드래그로 선택한 텍스트를 OSC 52로 터미널 클립보드에 복사함.
        text = self.screen.get_selected_text()
        if text:
            self.copy_to_clipboard(text)
            self.notify("선택한 텍스트를 클립보드에 복사했습니다.", timeout=2)

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
        for status in self.statuses:
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

    @on(Button.Pressed, "#sync")
    def sync_pressed(self) -> None:
        if not self.busy and (status := self._selected()) is not None:
            self._start_sync((status.spec,))

    @on(Button.Pressed, "#sync-all")
    def sync_all_pressed(self) -> None:
        if not self.busy:
            self._start_sync(AGENTS)

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
    parser.add_argument("--instructions", type=Path, help="동기화할 글로벌 지침 원본 경로")
    args = parser.parse_args()
    AgentsStatusApp(instructions_source=args.instructions).run()


if __name__ == "__main__":
    main()
