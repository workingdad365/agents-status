from __future__ import annotations

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Label, LoadingIndicator, RichLog, Static

from .core import AgentStatus, StatusKind, UpdateCommand, scan_agents, run_update, update_command


class ConfirmUpdate(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmUpdate { align: center middle; background: $background 70%; }
    ConfirmUpdate > Vertical { width: 64; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    ConfirmUpdate Horizontal { height: auto; align-horizontal: right; margin-top: 1; }
    ConfirmUpdate Button { margin-left: 1; }
    """

    def __init__(self, name: str, command: str) -> None:
        super().__init__()
        self.agent_name = name
        self.command = command

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]{self.agent_name}[/b]을(를) 업데이트하시겠습니까?")
            yield Static(f"[dim]$ {self.command}[/dim]")
            with Horizontal():
                yield Button("취소", id="cancel")
                yield Button("업데이트", id="confirm", variant="primary")

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm")
    def confirm(self) -> None:
        self.dismiss(True)


class AgentsStatusApp(App[None]):
    TITLE = "Agents Status"
    SUB_TITLE = "AI 코딩 에이전트 상태 및 업데이트"
    CSS = """
    Screen { layout: vertical; }
    #summary { height: 3; padding: 1 2; background: $boost; }
    DataTable { height: 1fr; }
    #actions { height: 3; padding: 0 1; align-vertical: middle; }
    #actions Button { margin-right: 1; }
    #progress { display: none; width: 8; height: 1; }
    #activity { width: 1fr; height: 1; color: $text-muted; }
    RichLog { height: 9; border-top: solid $accent; padding: 0 1; }
    """
    BINDINGS = [
        ("r", "refresh", "새로고침"),
        Binding("R,ㄱ", "refresh", "새로고침", show=False),
        ("u", "update", "선택 업데이트"),
        Binding("U,ㅕ", "update", "선택 업데이트", show=False),
        ("q", "quit", "종료"),
        Binding("Q,ㅂ", "quit", "종료", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.statuses: list[AgentStatus] = []
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("에이전트 정보를 조회하는 중입니다…", id="summary")
        yield DataTable(id="agents", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="actions"):
            yield Button("새로고침", id="refresh", variant="default")
            yield Button("선택 업데이트", id="update", variant="primary")
            yield LoadingIndicator(id="progress")
            yield Label("", id="activity")
        yield RichLog(id="log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("상태", "에이전트", "버전", "프로바이더", "실행 파일")
        self.load_statuses()

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
        counts = {kind: sum(item.status == kind for item in self.statuses) for kind in StatusKind}
        self.query_one("#summary", Static).update(
            f"[b]합계 {len(self.statuses)}개[/b]  "
            f"[green]설치 {counts[StatusKind.INSTALLED]}[/green]  "
            f"[yellow]설정만 {counts[StatusKind.CONFIG_ONLY]}[/yellow]  "
            f"[dim]미설치 {counts[StatusKind.MISSING]}[/dim]"
        )

    def _selected(self) -> AgentStatus | None:
        table = self.query_one(DataTable)
        if not self.statuses or table.cursor_row < 0:
            return None
        key = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        return next((status for status in self.statuses if status.spec.key == key), None)

    def _set_buttons(self, disabled: bool) -> None:
        self.query_one("#refresh", Button).disabled = disabled
        self.query_one("#update", Button).disabled = disabled

    @on(Button.Pressed, "#refresh")
    def refresh_pressed(self) -> None:
        self.action_refresh()

    @on(Button.Pressed, "#update")
    def update_pressed(self) -> None:
        self.action_update()

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
        command, reason = update_command(status)
        if command is None:
            self.notify(reason, title=status.spec.name, severity="warning", timeout=5)
            return
        self.push_screen(
            ConfirmUpdate(status.spec.name, command.display),
            lambda confirmed: self.update_agent(status, command) if confirmed else None,
        )

    @work(exclusive=True, group="update")
    async def update_agent(self, status: AgentStatus, command: UpdateCommand) -> None:
        self.busy = True
        self._set_buttons(True)
        log = self.query_one("#log", RichLog)
        log.write(f"[bold]{status.spec.name} 업데이트[/bold]: $ {command.display}")
        log.write("[yellow]업데이트를 시작했습니다. 완료될 때까지 기다려 주세요.[/yellow]")
        self.query_one("#progress", LoadingIndicator).styles.display = "block"
        self.query_one("#activity", Label).update(f"{status.spec.name} 업데이트 진행 중…")
        try:
            result = await run_update(
                command,
                on_output=lambda line: log.write(Text.from_ansi(line)),
            )
            if result.returncode == 0:
                self.notify("업데이트를 완료했습니다.", title=status.spec.name)
                log.write("[green]업데이트를 완료했습니다.[/green]")
            else:
                self.notify(f"업데이트 실패(종료 코드 {result.returncode})", title=status.spec.name, severity="error")
                log.write(f"[red]업데이트 실패(종료 코드 {result.returncode})[/red]")
        finally:
            self.query_one("#progress", LoadingIndicator).styles.display = "none"
            self.query_one("#activity", Label).update("")
            self.busy = False
            self._set_buttons(False)
            self.load_statuses()


def main() -> None:
    AgentsStatusApp().run()


if __name__ == "__main__":
    main()
