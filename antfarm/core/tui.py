"""Rich TUI dashboard for Antfarm colony monitoring.

Provides a live-updating terminal dashboard showing pipeline stages:
Waiting (New + Rework), Building, Awaiting Review, Under Review,
Merge Ready, Merge Blocked, Recently Merged, and Workers.

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
    waiting_new: list[dict] = field(default_factory=list)
    waiting_rework: list[dict] = field(default_factory=list)
    awaiting_review: list[dict] = field(default_factory=list)
    under_review: list[dict] = field(default_factory=list)
    merge_ready: list[dict] = field(default_factory=list)
    merge_blocked: list[dict] = field(default_factory=list)
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
            Layout(name="summary", size=8),
            Layout(name="workers", size=max(5, len(workers) + 4)),
            Layout(name="waiting", size=7),
            Layout(name="building", size=6),
            Layout(name="review", size=12),
            Layout(name="merge", size=6),
            Layout(name="merged", size=4),
        )

        layout["summary"].update(
            Panel(
                self._render_summary(status, tasks, workers, snap, soldier_status),
                title="[bold blue]Antfarm Colony[/bold blue]",
            )
        )

        layout["workers"].update(
            Panel(
                self._render_workers(workers),
                title=f"[bold cyan]Workers ({len(workers)})[/bold cyan]",
            )
        )

        layout["waiting"].split_row(
            Layout(name="waiting_new"),
            Layout(name="waiting_rework"),
        )
        layout["waiting"]["waiting_new"].update(
            Panel(
                self._render_waiting_new(snap.waiting_new),
                title=f"[bold blue]Waiting: New ({len(snap.waiting_new)})[/bold blue]",
            )
        )
        layout["waiting"]["waiting_rework"].update(
            Panel(
                self._render_waiting_rework(snap.waiting_rework),
                title=f"[bold red]Waiting: Rework ({len(snap.waiting_rework)})[/bold red]",
            )
        )

        layout["building"].update(
            Panel(
                self._render_building(snap.building),
                title=f"[bold yellow]Building ({len(snap.building)})[/bold yellow]",
            )
        )

        layout["review"].split_row(
            Layout(name="awaiting_review"),
            Layout(name="under_review"),
        )
        layout["review"]["awaiting_review"].update(
            Panel(
                self._render_awaiting_review(snap.awaiting_review),
                title=(
                    f"[bold magenta]Awaiting Review"
                    f" ({len(snap.awaiting_review)})[/bold magenta]"
                ),
            )
        )
        layout["review"]["under_review"].update(
            Panel(
                self._render_under_review(snap.under_review),
                title=f"[bold cyan]Under Review ({len(snap.under_review)})[/bold cyan]",
            )
        )

        layout["merge"].split_row(
            Layout(name="merge_ready"),
            Layout(name="merge_blocked"),
        )
        layout["merge"]["merge_ready"].update(
            Panel(
                self._render_merge_ready(snap.merge_ready),
                title=f"[bold green]Merge Ready ({len(snap.merge_ready)})[/bold green]",
            )
        )
        layout["merge"]["merge_blocked"].update(
            Panel(
                self._render_merge_blocked(snap.merge_blocked),
                title=f"[bold red]Merge Blocked ({len(snap.merge_blocked)})[/bold red]",
            )
        )

        layout["merged"].update(
            Panel(
                self._render_recently_merged(snap.recently_merged),
                title=(
                    f"[bold green]Recently Merged"
                    f" ({len(snap.recently_merged)})[/bold green]"
                ),
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
                    snap.review_tasks[task_id] = task
                else:
                    snap.building.append(task)

            elif status == "ready":
                if self._is_kicked_back(task):
                    snap.waiting_rework.append(task)
                elif is_review:
                    snap.review_tasks[task_id] = task
                else:
                    snap.waiting_new.append(task)

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
        return self._format_elapsed_since(started_at)

    def _format_elapsed_since(self, iso_timestamp: str) -> str:
        """Format elapsed time since an ISO timestamp."""
        try:
            start = datetime.fromisoformat(iso_timestamp)
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

    def _get_time_since_created(self, task: dict) -> str:
        """Time since task was created."""
        created_at = task.get("created_at", "")
        if not created_at:
            return ""
        return self._format_elapsed_since(created_at)

    def _get_time_since_kickback(self, task: dict) -> str:
        """Time since last kickback (latest superseded attempt's completed_at)."""
        latest_superseded_at = None
        for attempt in task.get("attempts", []):
            if attempt.get("status") == "superseded":
                completed = attempt.get("completed_at")
                if completed and (latest_superseded_at is None or completed > latest_superseded_at):
                    latest_superseded_at = completed
        if not latest_superseded_at:
            return ""
        return self._format_elapsed_since(latest_superseded_at)

    def _get_time_since_harvested(self, task: dict) -> str:
        """Time since attempt was harvested (completed_at on current attempt)."""
        current_id = task.get("current_attempt")
        if not current_id:
            return ""
        for attempt in task.get("attempts", []):
            if attempt.get("attempt_id") == current_id:
                completed = attempt.get("completed_at", "")
                if completed:
                    return self._format_elapsed_since(completed)
        return ""

    # ------------------------------------------------------------------
    # Overflow helper
    # ------------------------------------------------------------------

    def _add_overflow_hint(self, table: Table, total: int, max_shown: int) -> None:
        """Add '+N more -- run: antfarm inbox' hint if overflow."""
        if total > max_shown:
            remaining = total - max_shown
            cols = table.columns
            hint_row = [Text(f"+{remaining} more \u2014 run: antfarm inbox", style="dim")]
            hint_row.extend(Text("", style="dim") for _ in range(len(cols) - 1))
            table.add_row(*hint_row)

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

        # Nodes — show names from workers, fall back to count
        node_count = status.get("nodes", 0)
        node_ids = sorted(set(w.get("node_id", "") for w in workers if w.get("node_id")))
        if node_ids:
            table.add_row(
                f"Nodes ({len(node_ids)})",
                Text(", ".join(node_ids)),
            )
        else:
            table.add_row("Nodes", Text(str(node_count)))

        # Soldier status
        if soldier_status == "running":
            soldier_text = Text("running", style="green")
        elif soldier_status == "idle":
            soldier_text = Text("idle", style="dim")
        elif soldier_status == "unknown":
            soldier_text = Text("not started", style="yellow")
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
            bar = "\u2588" * filled + "\u2591" * (bar_width - filled)
            table.add_row("Progress", Text(f"{bar} {pct}% ({merged}/{total})"))
        else:
            table.add_row("Progress", Text("no tasks"))

        # Pipeline distribution bar
        counts = {
            "building": len(snap.building),
            "waiting": len(snap.waiting_new) + len(snap.waiting_rework),
            "awaiting_review": len(snap.awaiting_review),
            "under_review": len(snap.under_review),
            "merge_ready": len(snap.merge_ready),
            "merge_blocked": len(snap.merge_blocked),
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
            "waiting": "blue",
            "awaiting_review": "magenta",
            "under_review": "cyan",
            "merge_ready": "green",
            "merge_blocked": "red",
            "merged": "bright_green",
        }

        abbrev_map = {
            "building": "bld",
            "waiting": "wt",
            "awaiting_review": "rev",
            "under_review": "urev",
            "merge_ready": "mrdy",
            "merge_blocked": "mblk",
            "merged": "mrg",
        }

        text = Text()
        for stage, count in counts.items():
            if count == 0:
                continue
            chars = max(1, int(width * count / total))
            text.append("\u2588" * chars, style=color_map.get(stage, "white"))

        # Legend
        text.append("  ")
        legend_parts = []
        for stage, count in counts.items():
            if count > 0:
                abbrev = abbrev_map.get(stage, stage[:3])
                legend_parts.append(f"{abbrev}:{count}")
        text.append(" ".join(legend_parts), style="dim")

        return text

    def _render_building(self, tasks: list[dict], max_shown: int = 5) -> Table:
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

        shown = tasks[:max_shown]
        for task in shown:
            worker = self._get_worker_for_task(task)
            trail = task.get("trail", [])
            last_trail = trail[-1].get("message", "") if trail else ""
            if len(last_trail) > 33:
                last_trail = last_trail[:30] + "..."
            elapsed = self._get_elapsed(task)
            table.add_row(
                Text(task.get("id", "")[:18], style="yellow"),
                Text(task.get("title", "")[:28], style="yellow"),
                Text(worker[:18] if worker else "\u2014", style="dim"),
                Text(last_trail, style="dim"),
                Text(elapsed, style="dim"),
            )

        self._add_overflow_hint(table, len(tasks), max_shown)
        return table

    def _render_waiting_new(self, tasks: list[dict], max_shown: int = 5) -> Table:
        """Render fresh backlog tasks (no superseded attempts)."""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("ID", max_width=20, no_wrap=True)
        table.add_column("Complexity", max_width=5, no_wrap=True)
        table.add_column("Touches", max_width=20, no_wrap=True)
        table.add_column("Time", max_width=8, no_wrap=True)

        if not tasks:
            table.add_row("[dim]--[/dim]", "", "[dim]empty[/dim]", "")
            return table

        shown = tasks[:max_shown]
        for task in shown:
            touches = ", ".join(task.get("touches", []))
            elapsed = self._get_time_since_created(task)
            table.add_row(
                Text(task.get("id", "")[:18], style="blue"),
                Text(task.get("complexity", "M"), style="dim"),
                Text(touches[:18] if touches else "\u2014", style="dim"),
                Text(elapsed, style="dim"),
            )

        self._add_overflow_hint(table, len(tasks), max_shown)
        return table

    def _render_waiting_rework(self, tasks: list[dict], max_shown: int = 5) -> Table:
        """Render kicked-back tasks waiting for rework."""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("ID", max_width=20, no_wrap=True)
        table.add_column("Reason", max_width=35, no_wrap=True)
        table.add_column("Time", max_width=8, no_wrap=True)

        if not tasks:
            table.add_row("[dim]--[/dim]", "[dim]none[/dim]", "")
            return table

        shown = tasks[:max_shown]
        for task in shown:
            trail = task.get("trail", [])
            reason = trail[-1].get("message", "unknown") if trail else "unknown"
            if len(reason) > 33:
                reason = reason[:30] + "..."
            elapsed = self._get_time_since_kickback(task)
            table.add_row(
                Text(task.get("id", "")[:18], style="red"),
                Text(f"\u274c {reason}", style="red"),
                Text(elapsed, style="dim"),
            )

        self._add_overflow_hint(table, len(tasks), max_shown)
        return table

    def _render_awaiting_review(
        self, tasks: list[dict], max_shown: int = 8
    ) -> Table:
        """Render tasks awaiting review."""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("ID", max_width=20, no_wrap=True)
        table.add_column("Status", max_width=25, no_wrap=True)
        table.add_column("Time", max_width=8, no_wrap=True)

        if not tasks:
            table.add_row("[dim]--[/dim]", "[dim]none[/dim]", "")
            return table

        shown = tasks[:max_shown]
        for task in shown:
            elapsed = self._get_time_since_harvested(task)
            table.add_row(
                Text(task.get("id", "")[:18], style="magenta"),
                Text("\u23f3 awaiting review", style="magenta"),
                Text(elapsed, style="dim"),
            )

        self._add_overflow_hint(table, len(tasks), max_shown)
        return table

    def _render_under_review(self, tasks: list[dict], max_shown: int = 5) -> Table:
        """Render tasks actively under review."""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("Review Task", max_width=20, no_wrap=True)
        table.add_column("Reviewer", max_width=20, no_wrap=True)
        table.add_column("Time", max_width=8, no_wrap=True)

        if not tasks:
            table.add_row("[dim]--[/dim]", "[dim]none[/dim]", "")
            return table

        shown = tasks[:max_shown]
        for task in shown:
            reviewer = self._get_worker_for_task(task)
            elapsed = self._get_elapsed(task)
            table.add_row(
                Text(task.get("id", "")[:18], style="cyan"),
                Text(reviewer[:18] if reviewer else "\u2014", style="dim"),
                Text(elapsed, style="dim"),
            )

        self._add_overflow_hint(table, len(tasks), max_shown)
        return table

    def _render_merge_ready(self, tasks: list[dict], max_shown: int = 5) -> Table:
        """Render tasks ready to merge (verdict=pass)."""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("ID", max_width=20, no_wrap=True)
        table.add_column("Verdict", max_width=25, no_wrap=True)
        table.add_column("Time", max_width=8, no_wrap=True)

        if not tasks:
            table.add_row("[dim]--[/dim]", "[dim]none[/dim]", "")
            return table

        shown = tasks[:max_shown]
        for task in shown:
            verdict = self._get_verdict(task) or {}
            freshness = verdict.get("freshness", "fresh")
            elapsed = self._get_time_since_harvested(task)
            table.add_row(
                Text(task.get("id", "")[:18], style="green"),
                Text(f"\u2705 pass {freshness}", style="green"),
                Text(elapsed, style="dim"),
            )

        self._add_overflow_hint(table, len(tasks), max_shown)
        return table

    def _render_merge_blocked(self, tasks: list[dict], max_shown: int = 5) -> Table:
        """Render tasks blocked from merging."""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("ID", max_width=20, no_wrap=True)
        table.add_column("Reason", max_width=35, no_wrap=True)
        table.add_column("Time", max_width=8, no_wrap=True)

        if not tasks:
            table.add_row("[dim]--[/dim]", "[dim]none[/dim]", "")
            return table

        shown = tasks[:max_shown]
        for task in shown:
            reason = self._get_merge_block_reason(task) or "unknown"
            elapsed = self._get_time_since_harvested(task)
            table.add_row(
                Text(task.get("id", "")[:18], style="red"),
                Text(f"\u26a0 {reason}"[:33], style="red"),
                Text(elapsed, style="dim"),
            )

        self._add_overflow_hint(table, len(tasks), max_shown)
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
                        merged_ago = self._format_elapsed_since(completed)
                        if merged_ago:
                            merged_ago = f"{merged_ago} ago"
                    break
            table.add_row(
                Text(task.get("id", "")[:18], style="green"),
                Text(f"\u2705 merged {merged_ago}", style="green"),
            )

        self._add_overflow_hint(table, len(tasks), max_shown)

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
            table.add_row("[dim]\u2014[/dim]", "[dim]no workers[/dim]", "", "", "")
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
