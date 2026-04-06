"""Rich TUI dashboard for Antfarm colony monitoring.

Provides a live-updating terminal dashboard showing pipeline stages:
Building, Backlog, Awaiting Review, Under Review, Merge Ready,
Merge Blocked, Kicked Back, Recently Merged, and Workers.

Usage:
    tui = AntfarmTUI(colony_url="http://localhost:7433", token=None)
    tui.run()
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


@dataclass
class PipelineSnapshot:
    building: list[dict] = field(default_factory=list)
    backlog: list[dict] = field(default_factory=list)
    awaiting_review: list[dict] = field(default_factory=list)
    under_review: list[dict] = field(default_factory=list)
    merge_ready: list[dict] = field(default_factory=list)
    merge_blocked: list[dict] = field(default_factory=list)
    kicked_back: list[dict] = field(default_factory=list)
    recently_merged: list[dict] = field(default_factory=list)
    review_tasks: dict = field(default_factory=dict)


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
        """Fetch status + tasks + workers and build the display."""
        try:
            full = self._fetch("/status/full")
            status = full.get("status", {})
            tasks = full.get("tasks", [])
            workers = full.get("workers", [])
            soldier_status = full.get("soldier", "unknown")
        except Exception as exc:
            layout = Layout()
            layout.update(Panel(f"[red]Connection error: {exc}[/red]", title="Antfarm TUI"))
            return layout

        snap = self._classify_tasks(tasks)

        layout = Layout()
        layout.split_column(
            Layout(name="summary", size=9),
            Layout(name="building", size=8),
            Layout(name="row_backlog_awaiting", size=8),
            Layout(name="row_review_merge", size=8),
            Layout(name="row_kicked_merged", size=8),
            Layout(name="workers"),
        )

        layout["summary"].update(
            Panel(
                self._render_summary(status, tasks, workers, snap, soldier_status),
                title="[bold blue]Antfarm Colony[/bold blue]",
            )
        )

        layout["building"].update(
            Panel(
                self._render_building(snap.building),
                title="[bold yellow]Building[/bold yellow]",
            )
        )

        layout["row_backlog_awaiting"].split_row(
            Layout(name="backlog"),
            Layout(name="awaiting_review"),
        )
        layout["row_backlog_awaiting"]["backlog"].update(
            Panel(
                self._render_backlog(snap.backlog),
                title="[bold blue]Backlog[/bold blue]",
            )
        )
        layout["row_backlog_awaiting"]["awaiting_review"].update(
            Panel(
                self._render_awaiting_review(snap.awaiting_review),
                title="[bold magenta]Awaiting Review[/bold magenta]",
            )
        )

        layout["row_review_merge"].split_row(
            Layout(name="under_review"),
            Layout(name="merge_cols"),
        )
        layout["row_review_merge"]["under_review"].update(
            Panel(
                self._render_under_review(snap.under_review),
                title="[bold cyan]Under Review[/bold cyan]",
            )
        )
        merge_layout = Layout()
        merge_layout.split_column(
            Layout(name="merge_ready", size=4),
            Layout(name="merge_blocked"),
        )
        merge_layout["merge_ready"].update(
            self._render_merge_ready(snap.merge_ready)
        )
        merge_layout["merge_blocked"].update(
            self._render_merge_blocked(snap.merge_blocked)
        )
        layout["row_review_merge"]["merge_cols"].update(
            Panel(merge_layout, title="[bold green]Merge Queue[/bold green]")
        )

        layout["row_kicked_merged"].split_row(
            Layout(name="kicked_back"),
            Layout(name="recently_merged"),
        )
        layout["row_kicked_merged"]["kicked_back"].update(
            Panel(
                self._render_kicked_back(snap.kicked_back),
                title="[bold red]Kicked Back[/bold red]",
            )
        )
        layout["row_kicked_merged"]["recently_merged"].update(
            Panel(
                self._render_recently_merged(snap.recently_merged),
                title="[bold green]Recently Merged[/bold green]",
            )
        )

        layout["workers"].update(
            Panel(
                self._render_workers(workers),
                title="[bold cyan]Workers[/bold cyan]",
            )
        )

        return layout

    def _fetch(self, path: str) -> dict | list:
        """Fetch JSON from the colony API."""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        r = httpx.get(f"{self.colony_url}{path}", headers=headers, timeout=5)
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def _classify_tasks(self, tasks: list[dict]) -> PipelineSnapshot:
        """Classify all tasks into pipeline stages."""
        snap = PipelineSnapshot()

        for task in tasks:
            task_id = task.get("id", "")
            status = task.get("status", "")
            is_review = task_id.startswith("review-")

            if status == "active":
                if is_review:
                    snap.under_review.append(task)
                    # Map review task to its target
                    snap.review_tasks[task_id] = task
                else:
                    snap.building.append(task)

            elif status == "ready":
                if self._is_kicked_back(task):
                    snap.kicked_back.append(task)
                elif is_review:
                    snap.review_tasks[task_id] = task
                else:
                    snap.backlog.append(task)

            elif status == "done":
                if self._has_merged_attempt(task):
                    snap.recently_merged.append(task)
                else:
                    verdict = self._get_verdict(task)
                    block_reason = self._get_merge_block_reason(task)
                    if block_reason:
                        snap.merge_blocked.append(task)
                    elif verdict and verdict.get("result") == "pass":
                        snap.merge_ready.append(task)
                    else:
                        # Done but no review verdict yet — awaiting review
                        snap.awaiting_review.append(task)

        return snap

    def _is_kicked_back(self, task: dict) -> bool:
        """Check if task has a superseded attempt (was kicked back)."""
        return any(
            attempt.get("status") == "superseded" for attempt in task.get("attempts", [])
        )

    def _get_verdict(self, task: dict) -> dict | None:
        """Get review verdict from current attempt."""
        current_id = task.get("current_attempt")
        if not current_id:
            return None
        for attempt in task.get("attempts", []):
            if attempt.get("attempt_id") == current_id:
                return attempt.get("review_verdict")
        return None

    def _has_merged_attempt(self, task: dict) -> bool:
        """Check if any attempt has status=merged."""
        return any(
            attempt.get("status") == "merged" for attempt in task.get("attempts", [])
        )

    def _get_merge_block_reason(self, task: dict) -> str | None:
        """Get Soldier-produced block reason from current attempt."""
        current_id = task.get("current_attempt")
        if not current_id:
            return None
        for attempt in task.get("attempts", []):
            if attempt.get("attempt_id") == current_id:
                return attempt.get("merge_block_reason")
        return None

    def _get_worker_for_task(self, task: dict) -> str:
        """Get worker_id from current attempt."""
        current_id = task.get("current_attempt")
        if not current_id:
            return ""
        for attempt in task.get("attempts", []):
            if attempt.get("attempt_id") == current_id:
                return attempt.get("worker_id", "") or ""
        return ""

    def _get_elapsed(self, task: dict) -> str:
        """Get elapsed time since attempt started (Nm or NhNm)."""
        current_id = task.get("current_attempt")
        if not current_id:
            return ""
        started_at = None
        for attempt in task.get("attempts", []):
            if attempt.get("attempt_id") == current_id:
                started_at = attempt.get("started_at")
                break
        if not started_at:
            return ""
        try:
            start = datetime.fromisoformat(started_at)
            now = datetime.now(UTC)
            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
            delta = now - start
            total_minutes = int(delta.total_seconds() / 60)
            if total_minutes < 60:
                return f"{total_minutes}m"
            hours = total_minutes // 60
            mins = total_minutes % 60
            return f"{hours}h{mins}m"
        except (ValueError, TypeError):
            return ""

    # ------------------------------------------------------------------
    # Render methods
    # ------------------------------------------------------------------

    def _render_summary(
        self,
        status: dict,
        tasks: list[dict],
        workers: list[dict],
        snap: PipelineSnapshot,
        soldier_status: str,
    ) -> Table:
        """Render colony summary with pipeline overview."""
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Metric", style="bold")
        table.add_column("Value")

        # Nodes
        node_names = sorted({w.get("node_id", "?") for w in workers})
        node_str = ", ".join(node_names) if node_names else "none"
        table.add_row("Nodes", Text(f"{status.get('nodes', 0)} ({node_str})"))

        # Workers by type
        type_counts: dict[str, int] = {}
        for w in workers:
            wtype = self._get_worker_type(w)
            type_counts[wtype] = type_counts.get(wtype, 0) + 1
        type_parts = [f"{count} {wtype}" for wtype, count in sorted(type_counts.items())]
        table.add_row("Workers", Text(", ".join(type_parts) if type_parts else "0"))

        # Soldier status
        if soldier_status == "running":
            soldier_text = Text("running", style="green")
        elif soldier_status == "idle":
            soldier_text = Text("idle", style="dim")
        else:
            soldier_text = Text(soldier_status, style="yellow")
        table.add_row("Soldier", soldier_text)

        # Review queue pressure
        awaiting = len(snap.awaiting_review)
        under = len(snap.under_review)
        review_text = f"{awaiting} awaiting, {under} active"
        if awaiting > 3:
            table.add_row("Reviews", Text(review_text, style="red"))
        elif awaiting > 0:
            table.add_row("Reviews", Text(review_text, style="yellow"))
        else:
            table.add_row("Reviews", Text(review_text, style="dim"))

        # Progress bar
        total = len(tasks)
        merged = len(snap.recently_merged)
        if total > 0:
            pct = int(merged / total * 100)
            bar_width = 30
            filled = int(bar_width * merged / total)
            bar = "█" * filled + "░" * (bar_width - filled)
            table.add_row("Progress", Text(f"{bar} {pct}% ({merged}/{total})"))
        else:
            table.add_row("Progress", Text("no tasks"))

        # Pipeline distribution bar
        counts = {
            "building": len(snap.building),
            "backlog": len(snap.backlog),
            "awaiting_review": len(snap.awaiting_review),
            "under_review": len(snap.under_review),
            "merge_ready": len(snap.merge_ready),
            "merge_blocked": len(snap.merge_blocked),
            "kicked_back": len(snap.kicked_back),
            "merged": len(snap.recently_merged),
        }
        table.add_row("Pipeline", self._render_pipeline_bar(counts))

        return table

    def _render_pipeline_bar(self, counts: dict, width: int = 50) -> Text:
        """Render colored block chars representing pipeline distribution."""
        total = sum(counts.values())
        if total == 0:
            return Text("no tasks", style="dim")

        color_map = {
            "building": "yellow",
            "backlog": "blue",
            "awaiting_review": "magenta",
            "under_review": "cyan",
            "merge_ready": "green",
            "merge_blocked": "red",
            "kicked_back": "bright_red",
            "merged": "bright_green",
        }

        text = Text()
        for stage, count in counts.items():
            if count == 0:
                continue
            chars = max(1, int(width * count / total))
            text.append("█" * chars, style=color_map.get(stage, "white"))

        # Legend
        text.append("  ")
        legend_parts = []
        for stage, count in counts.items():
            if count > 0:
                abbrev = stage[:3].upper()
                legend_parts.append(f"{abbrev}:{count}")
        text.append(" ".join(legend_parts), style="dim")

        return text

    def _render_building(self, tasks: list[dict]) -> Table:
        """Render building (active non-review) tasks."""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("ID", max_width=20, no_wrap=True)
        table.add_column("Title", max_width=30, no_wrap=True)
        table.add_column("Worker", max_width=20, no_wrap=True)
        table.add_column("Trail", max_width=35, no_wrap=True)
        table.add_column("Time", max_width=8, no_wrap=True)

        if not tasks:
            table.add_row("[dim]--[/dim]", "[dim]no active builds[/dim]", "", "", "")
            return table

        for task in tasks:
            worker = self._get_worker_for_task(task)
            trail = task.get("trail", [])
            last_trail = trail[-1].get("message", "") if trail else ""
            if len(last_trail) > 33:
                last_trail = last_trail[:30] + "..."
            elapsed = self._get_elapsed(task)
            table.add_row(
                Text(task.get("id", "")[:18], style="yellow"),
                Text(task.get("title", "")[:28], style="yellow"),
                Text(worker[:18] if worker else "—", style="dim"),
                Text(last_trail, style="dim"),
                Text(elapsed, style="dim"),
            )

        return table

    def _render_backlog(self, tasks: list[dict]) -> Table:
        """Render backlog (ready, non-kicked-back) tasks."""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("ID", max_width=20, no_wrap=True)
        table.add_column("Complexity", max_width=5, no_wrap=True)
        table.add_column("Touches", max_width=25, no_wrap=True)

        if not tasks:
            table.add_row("[dim]--[/dim]", "", "[dim]empty[/dim]")
            return table

        for task in tasks:
            touches = ", ".join(task.get("touches", []))
            table.add_row(
                Text(task.get("id", "")[:18], style="blue"),
                Text(task.get("complexity", "M"), style="dim"),
                Text(touches[:23] if touches else "—", style="dim"),
            )

        return table

    def _render_awaiting_review(self, tasks: list[dict]) -> Table:
        """Render tasks awaiting review."""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("ID", max_width=20, no_wrap=True)
        table.add_column("Status", max_width=25, no_wrap=True)

        if not tasks:
            table.add_row("[dim]--[/dim]", "[dim]none[/dim]")
            return table

        for task in tasks:
            table.add_row(
                Text(task.get("id", "")[:18], style="magenta"),
                Text("\u23f3 awaiting review", style="magenta"),
            )

        return table

    def _render_under_review(self, tasks: list[dict]) -> Table:
        """Render tasks actively under review."""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("Review Task", max_width=20, no_wrap=True)
        table.add_column("Reviewer", max_width=20, no_wrap=True)
        table.add_column("Time", max_width=8, no_wrap=True)

        if not tasks:
            table.add_row("[dim]--[/dim]", "[dim]none[/dim]", "")
            return table

        for task in tasks:
            reviewer = self._get_worker_for_task(task)
            elapsed = self._get_elapsed(task)
            table.add_row(
                Text(task.get("id", "")[:18], style="cyan"),
                Text(reviewer[:18] if reviewer else "—", style="dim"),
                Text(elapsed, style="dim"),
            )

        return table

    def _render_merge_ready(self, tasks: list[dict]) -> Table:
        """Render tasks ready to merge (verdict=pass)."""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("ID", max_width=20, no_wrap=True)
        table.add_column("Verdict", max_width=25, no_wrap=True)

        if not tasks:
            table.add_row("[dim]--[/dim]", "[dim]none[/dim]")
            return table

        for task in tasks:
            verdict = self._get_verdict(task) or {}
            freshness = verdict.get("freshness", "fresh")
            table.add_row(
                Text(task.get("id", "")[:18], style="green"),
                Text(f"\u2705 pass {freshness}", style="green"),
            )

        return table

    def _render_merge_blocked(self, tasks: list[dict]) -> Table:
        """Render tasks blocked from merging."""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("ID", max_width=20, no_wrap=True)
        table.add_column("Reason", max_width=35, no_wrap=True)

        if not tasks:
            table.add_row("[dim]--[/dim]", "[dim]none[/dim]")
            return table

        for task in tasks:
            reason = self._get_merge_block_reason(task) or "unknown"
            table.add_row(
                Text(task.get("id", "")[:18], style="red"),
                Text(f"\u26a0 {reason}"[:33], style="red"),
            )

        return table

    def _render_kicked_back(self, tasks: list[dict]) -> Table:
        """Render kicked-back tasks with reason from trail."""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("ID", max_width=20, no_wrap=True)
        table.add_column("Reason", max_width=40, no_wrap=True)

        if not tasks:
            table.add_row("[dim]--[/dim]", "[dim]none[/dim]")
            return table

        for task in tasks:
            trail = task.get("trail", [])
            reason = trail[-1].get("message", "unknown") if trail else "unknown"
            if len(reason) > 38:
                reason = reason[:35] + "..."
            table.add_row(
                Text(task.get("id", "")[:18], style="red"),
                Text(reason, style="red"),
            )

        return table

    def _render_recently_merged(self, tasks: list[dict], max_shown: int = 5) -> Table:
        """Render recently merged tasks."""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("ID", max_width=20, no_wrap=True)
        table.add_column("Merged", max_width=25, no_wrap=True)

        if not tasks:
            table.add_row("[dim]--[/dim]", "[dim]none[/dim]")
            return table

        shown = tasks[:max_shown]
        for task in shown:
            # Find merged attempt time
            merged_ago = ""
            for attempt in task.get("attempts", []):
                if attempt.get("status") == "merged":
                    completed = attempt.get("completed_at", "")
                    if completed:
                        try:
                            ct = datetime.fromisoformat(completed)
                            now = datetime.now(UTC)
                            if ct.tzinfo is None:
                                ct = ct.replace(tzinfo=UTC)
                            mins = int((now - ct).total_seconds() / 60)
                            if mins < 60:
                                merged_ago = f"{mins}m ago"
                            else:
                                merged_ago = f"{mins // 60}h{mins % 60}m ago"
                        except (ValueError, TypeError):
                            pass
                    break
            table.add_row(
                Text(task.get("id", "")[:18], style="green"),
                Text(f"\u2705 merged {merged_ago}", style="green"),
            )

        if len(tasks) > max_shown:
            table.add_row(
                Text(f"... +{len(tasks) - max_shown} more", style="dim"),
                Text("", style="dim"),
            )

        return table

    def _get_worker_type(self, worker: dict) -> str:
        """Determine worker type (builder/reviewer) from agent_type or touches."""
        agent_type = worker.get("agent_type", "")
        if "review" in agent_type.lower():
            return "reviewer"
        capabilities = worker.get("capabilities", [])
        if "review" in capabilities:
            return "reviewer"
        return "builder"

    def _render_workers(self, workers: list) -> Table:
        """Render worker list with type column."""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("Worker", max_width=22, no_wrap=True)
        table.add_column("Node", max_width=15, no_wrap=True)
        table.add_column("Status", max_width=10, no_wrap=True)
        table.add_column("Type", max_width=10, no_wrap=True)
        table.add_column("Rate Limit", max_width=20, no_wrap=True)

        if not workers:
            table.add_row("[dim]—[/dim]", "[dim]no workers[/dim]", "", "", "")
            return table

        for worker in workers:
            worker_id = worker.get("worker_id", "")
            w_status = worker.get("status", "unknown")
            node_id = worker.get("node_id", "")
            wtype = self._get_worker_type(worker)

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

            # Type style
            if wtype == "reviewer":
                type_text = Text("reviewer", style="cyan")
            else:
                type_text = Text("builder", style="yellow")

            table.add_row(
                Text(worker_id[:20], style="cyan"),
                Text(node_id[:13], style="dim"),
                status_text,
                type_text,
                rate_text,
            )

        return table
