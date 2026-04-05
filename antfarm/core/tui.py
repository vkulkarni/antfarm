"""Rich TUI dashboard for Antfarm colony monitoring.

Provides a live-updating terminal dashboard showing colony summary,
active tasks, ready queue, merge queue, and worker status.

Usage:
    tui = AntfarmTUI(colony_url="http://localhost:7433", token=None)
    tui.run()
"""

from __future__ import annotations

import time

import httpx
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


class AntfarmTUI:
    """Live TUI dashboard for an Antfarm colony.

    Args:
        colony_url: Base URL of the colony server.
        token: Optional bearer token for authentication.
        refresh_interval: Seconds between display refreshes.
    """

    def __init__(
        self,
        colony_url: str,
        token: str | None = None,
        refresh_interval: float = 2.0,
    ):
        self.colony_url = colony_url.rstrip("/")
        self.token = token
        self.refresh_interval = refresh_interval

    def run(self) -> None:
        """Main loop using rich.live.Live."""
        with Live(self._build_display(), refresh_per_second=1) as live:
            try:
                while True:
                    live.update(self._build_display())
                    time.sleep(self.refresh_interval)
            except KeyboardInterrupt:
                pass

    def _build_display(self) -> Layout:
        """Fetch status + tasks + workers and build the display.

        Returns:
            A rich Layout containing all dashboard panels.
        """
        try:
            full = self._fetch("/status/full")
            status = full.get("status", {})
            tasks = full.get("tasks", [])
            workers = full.get("workers", [])
        except Exception as exc:
            layout = Layout()
            layout.update(Panel(f"[red]Connection error: {exc}[/red]", title="Antfarm TUI"))
            return layout

        active_tasks = [t for t in tasks if t.get("status") == "active"]
        ready_tasks = [t for t in tasks if t.get("status") == "ready"]
        done_tasks = [t for t in tasks if t.get("status") == "done"]

        layout = Layout()
        layout.split_column(
            Layout(name="top", size=7),
            Layout(name="middle", size=12),
            Layout(name="bottom"),
        )

        layout["top"].update(
            Panel(self._render_summary(status), title="[bold blue]Antfarm Colony[/bold blue]")
        )

        layout["middle"].update(
            Panel(
                self._render_tasks(active_tasks, "Active Tasks", ["active"]),
                title="[bold yellow]Active[/bold yellow]",
            )
        )

        layout["bottom"].split_row(
            Layout(name="bottom_left"),
            Layout(name="bottom_right"),
        )

        layout["bottom"]["bottom_left"].split_column(
            Layout(name="ready"),
            Layout(name="merge"),
        )

        layout["bottom"]["bottom_left"]["ready"].update(
            Panel(
                self._render_tasks(ready_tasks, "Ready Queue", ["ready"]),
                title="[bold blue]Ready Queue[/bold blue]",
            )
        )
        layout["bottom"]["bottom_left"]["merge"].update(
            Panel(
                self._render_tasks(done_tasks, "Merge Queue", ["done"]),
                title="[bold green]Merge Queue (Done)[/bold green]",
            )
        )

        layout["bottom"]["bottom_right"].update(
            Panel(
                self._render_workers(workers),
                title="[bold cyan]Workers[/bold cyan]",
            )
        )

        return layout

    def _fetch(self, path: str) -> dict | list:
        """Fetch JSON from the colony API.

        Args:
            path: API path to fetch (e.g. "/status/full").

        Returns:
            Parsed JSON response as dict or list.

        Raises:
            httpx.HTTPError: On non-2xx responses.
        """
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        r = httpx.get(f"{self.colony_url}{path}", headers=headers, timeout=5)
        r.raise_for_status()
        return r.json()

    def _render_summary(self, status: dict) -> Table:
        """Render the colony summary as a rich Table.

        Args:
            status: Status dict from /status or /status/full.

        Returns:
            A rich Table with key colony metrics.
        """
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Metric", style="bold")
        table.add_column("Value")

        field_labels = [
            ("nodes", "Nodes"),
            ("workers", "Workers"),
            ("tasks_ready", "Ready"),
            ("tasks_active", "Active"),
            ("tasks_done", "Done"),
            ("tasks_paused", "Paused"),
            ("tasks_blocked", "Blocked"),
        ]

        for field, label in field_labels:
            val = status.get(field, 0)
            if field == "tasks_active" and isinstance(val, int) and val > 0:
                value_text = Text(str(val), style="bold yellow")
            elif field == "tasks_done" and isinstance(val, int) and val > 0:
                value_text = Text(str(val), style="bold green")
            elif field == "tasks_blocked" and isinstance(val, int) and val > 0:
                value_text = Text(str(val), style="bold red")
            elif field == "tasks_ready" and isinstance(val, int) and val > 0:
                value_text = Text(str(val), style="bold blue")
            else:
                value_text = Text(str(val))
            table.add_row(label, value_text)

        return table

    def _render_tasks(self, tasks: list, title: str, statuses: list) -> Table:
        """Render a list of tasks as a rich Table.

        Args:
            tasks: List of task dicts to display.
            title: Display title for the table (unused — panels handle titles).
            statuses: List of statuses being shown (for color context).

        Returns:
            A rich Table showing task ID, title, worker, and last trail message.
        """
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("ID", max_width=20, no_wrap=True)
        table.add_column("Title", max_width=30, no_wrap=True)
        table.add_column("Worker", max_width=20, no_wrap=True)
        table.add_column("Last Trail", max_width=40, no_wrap=True)

        if not tasks:
            table.add_row("[dim]—[/dim]", "[dim]empty[/dim]", "", "")
            return table

        for task in tasks:
            task_id = task.get("id", "")
            task_title = task.get("title", "")
            status = task.get("status", "")

            # Get worker from current attempt (current_attempt is a string ID)
            current_attempt_id = task.get("current_attempt")
            worker_id = ""
            if current_attempt_id:
                for attempt in task.get("attempts", []):
                    if attempt.get("attempt_id") == current_attempt_id:
                        worker_id = attempt.get("worker_id", "")
                        break

            # Get last trail message
            trail = task.get("trail", [])
            last_trail = trail[-1].get("message", "") if trail else ""
            if len(last_trail) > 38:
                last_trail = last_trail[:35] + "..."

            # Color by status
            if status == "done":
                id_style = "green"
                title_style = "green"
            elif status == "active":
                id_style = "yellow"
                title_style = "yellow"
            elif status == "blocked":
                id_style = "red"
                title_style = "red"
            elif status == "ready":
                id_style = "blue"
                title_style = "blue"
            elif status == "paused":
                id_style = "dim"
                title_style = "dim"
            else:
                id_style = ""
                title_style = ""

            table.add_row(
                Text(task_id[:18], style=id_style),
                Text(task_title[:28], style=title_style),
                Text(worker_id[:18] if worker_id else "—", style="dim"),
                Text(last_trail, style="dim"),
            )

        return table

    def _render_workers(self, workers: list) -> Table:
        """Render worker list as a rich Table.

        Args:
            workers: List of worker dicts, each optionally including rate limit fields.

        Returns:
            A rich Table showing worker ID, status, node, and rate limit state.
        """
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("Worker", max_width=22, no_wrap=True)
        table.add_column("Status", max_width=10, no_wrap=True)
        table.add_column("Node", max_width=15, no_wrap=True)
        table.add_column("Rate Limit", max_width=20, no_wrap=True)

        if not workers:
            table.add_row("[dim]—[/dim]", "[dim]no workers[/dim]", "", "")
            return table

        for worker in workers:
            worker_id = worker.get("worker_id", "")
            w_status = worker.get("status", "unknown")
            node_id = worker.get("node_id", "")

            # Rate limit info
            rate_limited = worker.get("rate_limited", False)
            rate_limit_until = worker.get("rate_limit_until", None)
            if rate_limited and rate_limit_until:
                rate_text = Text(f"limited until {rate_limit_until[:16]}", style="red")
            elif rate_limited:
                rate_text = Text("rate limited", style="red")
            else:
                rate_text = Text("ok", style="green")

            # Status color
            if w_status == "idle":
                status_text = Text("idle", style="dim")
            elif w_status == "busy":
                status_text = Text("busy", style="yellow")
            else:
                status_text = Text(w_status, style="dim")

            table.add_row(
                Text(worker_id[:20], style="cyan"),
                status_text,
                Text(node_id[:13], style="dim"),
                rate_text,
            )

        return table
