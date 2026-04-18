"""Rich TUI dashboard for Antfarm colony monitoring.

Provides a live-updating terminal dashboard showing pipeline stages:
Waiting (New + Rework), Planning, Building, Awaiting Review, Under Review,
Merge Ready, Recently Merged, and Workers.

Usage:
    tui = AntfarmTUI(colony_url="http://localhost:7433", token=None)
    tui.run()
"""

from __future__ import annotations

import collections
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from antfarm.core.missions import is_infra_task

logger = logging.getLogger(__name__)


@dataclass
class PipelineSnapshot:
    planning: list[dict] = field(default_factory=list)
    building: list[dict] = field(default_factory=list)
    waiting_new: list[dict] = field(default_factory=list)
    waiting_rework: list[dict] = field(default_factory=list)
    awaiting_review: list[dict] = field(default_factory=list)
    under_review: list[dict] = field(default_factory=list)
    merge_ready: list[dict] = field(default_factory=list)
    recently_merged: list[dict] = field(default_factory=list)
    review_tasks: dict = field(default_factory=dict)
    warnings: list[dict] = field(default_factory=list)


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
        autostart_activity: bool = True,
    ):
        self.colony_url = colony_url.rstrip("/")
        self.token = token
        self.refresh_interval = refresh_interval

        self._activity_events: collections.deque = collections.deque(maxlen=1000)
        self._activity_cursor: int = 0
        self._activity_epoch: str = ""
        self._activity_status: str = "connected"
        self._activity_lock = threading.Lock()
        self._activity_thread: threading.Thread | None = None
        if autostart_activity:
            self._start_activity_thread()

    def _start_activity_thread(self) -> None:
        """Start the background SSE consumer thread (idempotent)."""
        if self._activity_thread is not None and self._activity_thread.is_alive():
            return
        t = threading.Thread(
            target=self._activity_loop,
            daemon=True,
            name="antfarm-tui-activity",
        )
        self._activity_thread = t
        t.start()

    def _activity_loop(self) -> None:
        """Consume the colony /events SSE stream with exponential backoff.

        Classifies errors and surfaces a human-readable status string to the
        TUI header. Auth errors (401/403) are terminal; transport errors and
        HTTP 5xx bump the backoff up to 30s. Successful polls reset backoff.
        """
        backoff = 1.0
        max_backoff = 30.0
        while True:
            try:
                self._poll_events_once()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (401, 403):
                    with self._activity_lock:
                        self._activity_status = f"auth error ({status})"
                    return
                with self._activity_lock:
                    self._activity_status = f"http {status} — retry in {backoff:.0f}s"
                time.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue
            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.ReadError,
                httpx.RemoteProtocolError,
                httpx.ConnectTimeout,
            ):
                with self._activity_lock:
                    self._activity_status = f"reconnecting in {backoff:.0f}s"
                time.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue
            except Exception as e:
                with self._activity_lock:
                    self._activity_status = f"error: {type(e).__name__}"
                time.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue
            # Healthy return — stream closed at server timeout with no error.
            with self._activity_lock:
                self._activity_status = "connected"
            backoff = 1.0
            time.sleep(0.5)  # rate-limit empty-stream reconnects

    def _poll_events_once(self) -> None:
        """Open one streaming request to /events and ingest each event."""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        with httpx.stream(
            "GET",
            f"{self.colony_url}/events",
            params={
                "after": self._activity_cursor,
                "epoch": self._activity_epoch,
                "timeout": 5,
            },
            headers=headers,
            timeout=30.0,
        ) as response:
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except (json.JSONDecodeError, ValueError):
                    continue
                self._ingest_event(event)

    def _ingest_event(self, event: dict) -> None:
        """Append an event to the activity deque and advance the cursor.

        If the event carries an epoch that differs from the one we've been
        tracking, the colony restarted — zero the cursor so we replay events
        from the new server's id=1 onward (#306).
        """
        with self._activity_lock:
            incoming_epoch = event.get("epoch", "")
            if incoming_epoch and incoming_epoch != self._activity_epoch:
                if self._activity_epoch:
                    logger.info(
                        "colony epoch changed %s -> %s; resetting cursor",
                        self._activity_epoch,
                        incoming_epoch,
                    )
                self._activity_epoch = incoming_epoch
                self._activity_cursor = 0
            self._activity_events.append(event)
            eid = event.get("id", 0)
            if isinstance(eid, int) and eid > self._activity_cursor:
                self._activity_cursor = eid

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
        """Fetch status + tasks + workers + missions and build the display."""
        try:
            full = self._fetch("/status/full")
            status = full.get("status", {})
            tasks = full.get("tasks", [])
            workers = full.get("workers", [])
            soldier_status = full.get("soldier", "unknown")
        except httpx.ConnectError:
            layout = Layout()
            msg = (
                f"[red]Can't reach colony at {self.colony_url} — is it running?[/red]\n\n"
                f"[bold]Start the colony on this host:[/bold]\n"
                f"    antfarm colony\n\n"
                f"[bold]Or point the TUI elsewhere:[/bold]\n"
                f"    antfarm scout --tui --colony-url http://<host>:<port>"
            )
            layout.update(Panel(msg, title="Antfarm TUI"))
            return layout
        except Exception as exc:
            layout = Layout()
            layout.update(Panel(f"[red]Connection error: {exc}[/red]", title="Antfarm TUI"))
            return layout

        try:
            missions = self._fetch("/missions")
        except Exception:
            missions = []

        snap = self._classify_tasks(tasks)
        snap.warnings = full.get("warnings", [])

        layout = Layout()
        missions_size = max(5, min(len(missions) + 4, 10)) if missions else 5
        warnings_size = len(snap.warnings) + 2 if snap.warnings else 0
        column_slices = [
            Layout(name="header", size=10),
            Layout(name="missions", size=missions_size),
            Layout(name="workers", size=max(5, len(workers) + 5)),
            Layout(name="waiting", size=7),
            Layout(name="planning", size=5),
            Layout(name="building", size=6),
            Layout(name="review", size=12),
            Layout(name="merge_ready", size=6),
            Layout(name="merged", size=6),
            Layout(name="activity", size=8),
        ]
        if snap.warnings:
            column_slices.insert(0, Layout(name="warnings", size=warnings_size))
        layout.split_column(*column_slices)

        # Warnings panel — only present when there are warnings
        if snap.warnings:
            layout["warnings"].update(self._render_warnings(snap.warnings))

        # Header: banner (left) + colony summary (right) side by side
        layout["header"].split_row(
            Layout(name="banner", ratio=1),
            Layout(name="summary", ratio=2),
        )

        # Banner — ANTFARM in half-height block letters with ant icons
        line1 = "▄▀█ █▄░█ ▀█▀ █▀▀ █▀█ █▀█ █▀▄▀█"
        line2 = "█▀█ █░▀█ ░█░ █▀░ █▀█ █▀▄ █░▀░█"
        banner = Text()
        banner.append("\n")
        banner.append(" 🐜·· ", style="bold rgb(205,133,63)")
        banner.append(line1, style="bold dark_orange")
        banner.append(" ··🐜\n", style="bold rgb(205,133,63)")
        banner.append("      ", style="dim")
        banner.append(line2, style="bold dark_orange")
        from antfarm.core import __version__

        banner.append(f"\n v{__version__}", style="bold bright_white")

        # SSE consumer status — visible signal that the event stream is healthy
        # or in backoff (#307). Read once under the lock to avoid torn strings.
        with self._activity_lock:
            stream_status = self._activity_status
        if len(stream_status) > 60:
            stream_status = stream_status[:59] + "…"
        banner.append(f"\n stream: {stream_status}", style="dim")
        layout["header"]["banner"].update(Panel(banner))

        layout["header"]["summary"].update(
            Panel(
                self._render_summary(status, tasks, workers, snap, soldier_status),
                title="[bold blue]Antfarm Colony[/bold blue]",
            )
        )

        layout["missions"].update(
            Panel(
                self._render_missions(missions, tasks=tasks),
                title=f"[bold bright_white]Missions ({len(missions)})[/bold bright_white]",
            )
        )

        worker_count = len(workers) + (1 if soldier_status != "disabled" else 0)
        layout["workers"].update(
            Panel(
                self._render_workers(workers, soldier_status),
                title=f"[bold cyan]Workers ({worker_count})[/bold cyan]",
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

        layout["planning"].update(
            Panel(
                self._render_planning(snap.planning),
                title=f"[bold magenta]Planning ({len(snap.planning)})[/bold magenta]",
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
                    f"[bold magenta]Awaiting Review ({len(snap.awaiting_review)})[/bold magenta]"
                ),
            )
        )
        layout["review"]["under_review"].update(
            Panel(
                self._render_under_review(snap.under_review),
                title=f"[bold cyan]Under Review ({len(snap.under_review)})[/bold cyan]",
            )
        )

        layout["merge_ready"].update(
            Panel(
                self._render_merge_ready(snap.merge_ready),
                title=f"[bold green]Merge Ready ({len(snap.merge_ready)})[/bold green]",
            )
        )

        layout["merged"].update(
            Panel(
                self._render_recently_merged(snap.recently_merged),
                title=(f"[bold green]Recently Merged ({len(snap.recently_merged)})[/bold green]"),
            )
        )

        layout["activity"].update(
            Panel(
                self._render_activity(max_rows=6),
                title="[bold white]Activity[/bold white]",
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
                caps_req = set(task.get("capabilities_required", []))
                if is_review:
                    snap.under_review.append(task)
                    snap.review_tasks[task_id] = task
                elif "plan" in caps_req:
                    snap.planning.append(task)
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
                if task.get("cancelled_at"):
                    snap.review_tasks[task_id] = task
                    continue
                if self._has_merged_attempt(task):
                    if not is_infra_task(task):  # only show impl tasks in merged
                        snap.recently_merged.append(task)
                elif is_infra_task(task):
                    # Done infra tasks (plan/review) are just containers — hide them
                    snap.review_tasks[task_id] = task
                else:
                    verdict = self._get_verdict(task)
                    if verdict and verdict.get("verdict") == "pass":
                        snap.merge_ready.append(task)
                    else:
                        snap.awaiting_review.append(task)

        return snap

    def _is_kicked_back(self, task: dict) -> bool:
        """Check if task has a superseded attempt (was kicked back)."""
        return any(attempt.get("status") == "superseded" for attempt in task.get("attempts", []))

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
        return any(attempt.get("status") == "merged" for attempt in task.get("attempts", []))

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

        # Progress bar — only count implementation tasks, not infra (plan/review) tasks
        impl_tasks = [t for t in tasks if not is_infra_task(t)]
        total = len(impl_tasks)
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
            "plan": len(snap.planning),
            "building": len(snap.building),
            "waiting": len(snap.waiting_new) + len(snap.waiting_rework),
            "awaiting_review": len(snap.awaiting_review),
            "under_review": len(snap.under_review),
            "merge_ready": len(snap.merge_ready),
            "merged": len(snap.recently_merged),
        }
        table.add_row("Pipeline", self._render_pipeline_bar(counts))

        return table

    def _render_missions(
        self,
        missions: list[dict],
        tasks: list[dict] | None = None,
        max_shown: int = 5,
    ) -> Table:
        """Render mission list with status, task counts, and progress time.

        Live merged counts come from the tasks list (fixes #331 — reports are
        only populated after a mission completes, so in-flight missions would
        otherwise show 0/N until done). Falls back to report["merged_tasks"]
        when no tasks list is supplied, preserving legacy behaviour.
        """
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("ID", max_width=25, no_wrap=True)
        table.add_column("Status", max_width=15, no_wrap=True)
        table.add_column("Tasks", max_width=10, no_wrap=True)
        table.add_column("Progress", max_width=20, no_wrap=True)

        if not missions:
            table.add_row("[dim]--[/dim]", "[dim]No active missions.[/dim]", "", "")
            return table

        status_style = {
            "planning": "magenta",
            "reviewing_plan": "magenta",
            "building": "yellow",
            "blocked": "red",
            "complete": "green",
            "failed": "red",
            "cancelled": "dim",
        }

        task_by_id = {t.get("id", ""): t for t in (tasks or [])}

        shown = missions[:max_shown]
        for m in shown:
            mid = m.get("mission_id", "")
            mstatus = m.get("status", "")
            style = status_style.get(mstatus, "dim")

            # Task counts: total / merged / blocked
            task_ids = m.get("task_ids", [])
            blocked_ids = m.get("blocked_task_ids", [])
            total = len(task_ids)
            blocked = len(blocked_ids)

            # Prefer live count from task attempts so in-flight missions show
            # real progress. Fall back to the report only when the caller
            # didn't supply a tasks list (legacy/test callers).
            if tasks is not None:
                merged = self._count_merged_tasks(task_ids, task_by_id)
            else:
                report = m.get("report")
                merged = report.get("merged_tasks", 0) if report else 0
            tasks_text = f"{merged}/{total}"
            if blocked > 0:
                tasks_text += f" ({blocked}blk)"

            # Progress column
            progress = self._format_mission_progress(m)

            table.add_row(
                Text(mid[:23], style=style),
                Text(mstatus, style=style),
                Text(tasks_text, style="dim"),
                Text(progress, style="dim"),
            )

        self._add_overflow_hint(table, len(missions), max_shown)
        return table

    @staticmethod
    def _count_merged_tasks(task_ids: list[str], task_by_id: dict[str, dict]) -> int:
        """Count task_ids whose task has at least one merged attempt."""
        merged = 0
        for tid in task_ids:
            task = task_by_id.get(tid)
            if not task:
                continue
            for att in task.get("attempts", []):
                if att.get("status") == "merged":
                    merged += 1
                    break
        return merged

    def _format_mission_progress(self, mission: dict) -> str:
        """Format progress column for a mission.

        - complete/failed/cancelled: "done"
        - blocked with last_progress_at: "stalled <elapsed>"
        - active with last_progress_at: "<elapsed> ago"
        - no last_progress_at: "--"
        """
        mstatus = mission.get("status", "")
        if mstatus in ("complete", "failed", "cancelled"):
            return "done"

        last_progress = mission.get("last_progress_at", "")
        if not last_progress:
            return "--"

        elapsed = self._format_elapsed_since(last_progress)
        if not elapsed:
            return "--"

        if mstatus == "blocked":
            return f"stalled {elapsed}"
        return f"{elapsed} ago"

    def _render_pipeline_bar(self, counts: dict, width: int = 50) -> Text:
        """Render colored block chars representing pipeline distribution."""
        total = sum(counts.values())
        if total == 0:
            return Text("no tasks", style="dim")

        color_map = {
            "plan": "magenta",
            "building": "yellow",
            "waiting": "blue",
            "awaiting_review": "magenta",
            "under_review": "cyan",
            "merge_ready": "green",
            "merged": "bright_green",
        }

        abbrev_map = {
            "plan": "plan",
            "building": "bld",
            "waiting": "wt",
            "awaiting_review": "await",
            "under_review": "review",
            "merge_ready": "ready",
            "merged": "merged",
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

    def _render_planning(self, tasks: list[dict], max_shown: int = 5) -> Table:
        """Render active planning tasks."""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("ID", max_width=20, no_wrap=True)
        table.add_column("Title", max_width=30, no_wrap=True)
        table.add_column("Worker", max_width=20, no_wrap=True)
        table.add_column("Trail", max_width=35, no_wrap=True)
        table.add_column("Time", max_width=8, no_wrap=True)

        if not tasks:
            table.add_row("[dim]--[/dim]", "[dim]no active plans[/dim]", "", "", "")
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
                Text(task.get("id", "")[:18], style="magenta"),
                Text(task.get("title", "")[:28], style="magenta"),
                Text(worker[:18] if worker else "\u2014", style="dim"),
                Text(last_trail, style="dim"),
                Text(elapsed, style="dim"),
            )

        self._add_overflow_hint(table, len(tasks), max_shown)
        return table

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

    def _render_awaiting_review(self, tasks: list[dict], max_shown: int = 8) -> Table:
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

    def _render_activity(self, max_rows: int = 6) -> Text:
        """Render the tail of the activity event deque as timestamped lines."""
        with self._activity_lock:
            events = list(self._activity_events)

        text = Text()
        if not events:
            text.append("(waiting for events\u2026)", style="dim")
            return text

        tail = events[-max_rows:]
        for i, ev in enumerate(tail):
            ts = ev.get("ts", "") or ""
            try:
                dt = datetime.fromisoformat(ts)
                time_str = dt.astimezone().strftime("%H:%M:%S")
            except (ValueError, TypeError):
                time_str = "--:--:--"

            actor = (ev.get("actor") or "-")[:12].ljust(12)
            event_type = (ev.get("type") or "-")[:16].ljust(16)
            raw_tid = ev.get("task_id") or ""
            tid_short = (raw_tid.rsplit("-", 1)[-1] if raw_tid else "")[:8].ljust(10)
            detail = ev.get("detail") or "-"

            if "failed" in event_type:
                style = "red"
            elif "kick" in event_type:
                style = "yellow"
            else:
                style = ""
            text.append(
                f"{time_str}  {actor}  {event_type}  {tid_short}  {detail}",
                style=style,
            )
            if i < len(tail) - 1:
                text.append("\n")

        return text

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

    def _render_warnings(self, warnings: list[dict]) -> Panel:
        """Render a red panel listing all colony-level warnings with hints."""
        text = Text()
        for i, w in enumerate(warnings):
            message = w.get("message", "")
            hint = w.get("hint", "")
            text.append(message, style="bold red")
            if hint:
                text.append(f"\n  \u2192 {hint}", style="red")
            if i < len(warnings) - 1:
                text.append("\n")
        return Panel(text, title="[bold red]\u26a0 Warnings[/bold red]", border_style="red")

    def _get_worker_type(self, worker: dict) -> str:
        """Determine worker type (builder/reviewer) from agent_type or touches."""
        agent_type = worker.get("agent_type", "")
        if "review" in agent_type.lower():
            return "reviewer"
        capabilities = worker.get("capabilities", [])
        if "review" in capabilities:
            return "reviewer"
        return "builder"

    def _format_activity_cell(self, worker: dict, stuck_ttl: int = 300) -> Text:
        """Format the Activity column cell for a worker row.

        Returns a Rich Text object rendering '<action[:40]> (<N>s)' when the
        worker has a current_action, dim em-dash when it does not, and red
        when the elapsed time exceeds stuck_ttl.
        """
        action = worker.get("current_action")
        action_at = worker.get("current_action_at")
        if not action or not action_at:
            return Text("—", style="dim")

        try:
            start = datetime.fromisoformat(action_at)
            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
            elapsed = int((datetime.now(UTC) - start).total_seconds())
        except (ValueError, TypeError):
            return Text(str(action)[:40], style="dim")

        truncated = action[:40]
        label = f"{truncated} ({elapsed}s)"
        style = "red" if elapsed > stuck_ttl else ""
        return Text(label, style=style)

    def _render_workers(self, workers: list, soldier_status: str = "unknown") -> Table:
        """Render worker list with type column, including Soldier."""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("Worker", max_width=22, no_wrap=True)
        table.add_column("Node", max_width=15, no_wrap=True)
        table.add_column("Status", max_width=10, no_wrap=True)
        table.add_column("Type", max_width=10, no_wrap=True)
        table.add_column("Activity", max_width=50, no_wrap=True)
        table.add_column("Rate Limit", max_width=20, no_wrap=True)

        # Soldier row (virtual — it's a thread, not a registered worker)
        if soldier_status != "disabled":
            s_style = "green" if soldier_status == "running" else "yellow"
            table.add_row(
                Text("soldier", style="bold"),
                Text("colony", style="dim"),
                Text(soldier_status, style=s_style),
                Text("soldier", style="bold"),
                Text("—", style="dim"),
                Text("—", style="dim"),
            )

        if not workers and soldier_status == "disabled":
            table.add_row("[dim]\u2014[/dim]", "[dim]no workers[/dim]", "", "", "", "")
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

            activity_text = self._format_activity_cell(worker)

            table.add_row(
                Text(worker_id[:20], style="cyan"),
                Text(node_id[:13], style="dim"),
                status_text,
                type_text,
                activity_text,
                rate_text,
            )

        return table
