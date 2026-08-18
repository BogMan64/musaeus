#!/usr/bin/env python3
"""
MUSAEUS — Progress Tracking & Debug Display

Provides rich progress bars, performance metrics, and verbose logging
for monitoring MUSAEUS pipeline execution in real-time.

Features:
  - Progress bars with file counters
  - Stage timing and throughput metrics
  - Memory usage tracking
  - Color-coded status messages
  - Detailed debug logging
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .context import StageResult

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
    )
    from rich.table import Table
    _HAVE_RICH = True
except ImportError:
    _HAVE_RICH = False

logger = logging.getLogger(__name__)


@dataclass
class StageMetrics:
    """Performance metrics for a single stage."""

    stage_name: str
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    files_processed: int = 0
    files_changed: int = 0
    files_skipped: int = 0
    files_errored: int = 0
    bytes_processed: int = 0
    peak_memory_mb: float = 0.0

    @property
    def duration(self) -> float:
        """Duration in seconds."""
        if self.end_time is None:
            return time.time() - self.start_time
        return self.end_time - self.start_time

    @property
    def throughput(self) -> float:
        """Files per second."""
        if self.duration == 0:
            return 0.0
        return self.files_processed / self.duration

    def finish(self) -> None:
        """Mark stage as complete."""
        self.end_time = time.time()


class ProgressTracker:
    """
    Tracks and displays progress for MUSAEUS pipeline execution.

    Usage:
        tracker = ProgressTracker(verbose=True)

        with tracker.stage("forge") as progress:
            for i, file in enumerate(files):
                progress.update(i+1, len(files))
                # ... process file ...

        tracker.print_summary()
    """

    def __init__(self, verbose: bool = False, use_rich: bool = True):
        self.verbose = verbose
        self.use_rich = use_rich and _HAVE_RICH
        self.metrics: dict[str, StageMetrics] = {}
        self.console = Console() if self.use_rich else None
        self.start_time = time.time()

    def _get_memory_mb(self) -> float:
        """Get current memory usage in MB."""
        try:
            import psutil
            process = psutil.Process()
            return process.memory_info().rss / 1024 / 1024
        except ImportError:
            return 0.0

    def _format_duration(self, seconds: float) -> str:
        """Format duration as human-readable string."""
        if seconds < 60:
            return f"{seconds:.1f}s"
        elif seconds < 3600:
            return f"{seconds/60:.1f}m"
        else:
            return f"{seconds/3600:.1f}h"

    def _format_bytes(self, bytes_: int) -> str:
        """Format bytes as human-readable string."""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if bytes_ < 1024:
                return f"{bytes_:.1f}{unit}"
            bytes_ /= 1024
        return f"{bytes_:.1f}TB"

    @contextmanager
    def stage(self, stage_name: str):
        """
        Context manager for tracking a stage's progress.

        Example:
            with tracker.stage("forge") as progress:
                for i, file in enumerate(files):
                    progress.update(i+1, len(files), f"Processing {file}")
        """
        metrics = StageMetrics(stage_name=stage_name)
        self.metrics[stage_name] = metrics

        if self.use_rich:
            progress_tracker = self._rich_stage_progress(stage_name, metrics)
        else:
            progress_tracker = self._simple_stage_progress(stage_name, metrics)

        yield progress_tracker

        metrics.finish()
        self._print_stage_summary(stage_name, metrics)

    def _rich_stage_progress(self, stage_name: str, metrics: StageMetrics):
        """Create a rich progress tracker."""
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("•"),
            TimeElapsedColumn(),
            TextColumn("•"),
            TextColumn("[cyan]{task.fields[status]}"),
            console=self.console,
        )

        class RichProgressUpdater:
            def __init__(self, progress, task_id, metrics):
                self.progress = progress
                self.task_id = task_id
                self.metrics = metrics
                self.started = False

            def __enter__(self):
                self.progress.__enter__()
                return self

            def __exit__(self, *args):
                self.progress.__exit__(*args)

            def update(self, current: int, total: int, status: str = ""):
                if not self.started:
                    self.progress.start_task(self.task_id)
                    self.started = True

                self.metrics.files_processed = current
                self.metrics.peak_memory_mb = max(
                    self.metrics.peak_memory_mb,
                    ProgressTracker._get_memory_mb(None)
                )

                self.progress.update(
                    self.task_id,
                    completed=current,
                    total=total,
                    status=status or f"{current}/{total} files"
                )

        task_id = progress.add_task(
            f"[{stage_name}]",
            total=100,
            status="Starting...",
            start=False
        )

        return RichProgressUpdater(progress, task_id, metrics)

    def _simple_stage_progress(self, stage_name: str, metrics: StageMetrics):
        """Create a simple text-based progress tracker."""
        class SimpleProgressUpdater:
            def __init__(self, stage_name, metrics, verbose):
                self.stage_name = stage_name
                self.metrics = metrics
                self.verbose = verbose
                self.last_print = 0

            def __enter__(self):
                print(f"\n▶ Starting stage: {self.stage_name}")
                return self

            def __exit__(self, *args):
                pass

            def update(self, current: int, total: int, status: str = ""):
                self.metrics.files_processed = current

                # Print every 10% or every 100 files
                if self.verbose or current % 100 == 0 or current == total:
                    percent = (current / total * 100) if total > 0 else 0
                    if percent - self.last_print >= 10 or current == total:
                        elapsed = time.time() - self.metrics.start_time
                        rate = current / elapsed if elapsed > 0 else 0
                        print(f"  [{percent:5.1f}%] {current}/{total} files ({rate:.1f} files/sec) {status}")
                        self.last_print = percent

        return SimpleProgressUpdater(stage_name, metrics, self.verbose)

    def _print_stage_summary(self, stage_name: str, metrics: StageMetrics):
        """Print summary for a completed stage."""
        if self.use_rich:
            self._rich_stage_summary(stage_name, metrics)
        else:
            self._simple_stage_summary(stage_name, metrics)

    def _rich_stage_summary(self, stage_name: str, metrics: StageMetrics):
        """Print rich formatted stage summary."""
        if not self.verbose:
            return

        table = Table(show_header=False, box=None)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="white")

        table.add_row("Duration", self._format_duration(metrics.duration))
        table.add_row("Files", str(metrics.files_processed))
        table.add_row("Changed", str(metrics.files_changed))
        table.add_row("Skipped", str(metrics.files_skipped))
        table.add_row("Errors", str(metrics.files_errored))
        table.add_row("Throughput", f"{metrics.throughput:.1f} files/sec")

        if metrics.bytes_processed > 0:
            table.add_row("Data", self._format_bytes(metrics.bytes_processed))

        if metrics.peak_memory_mb > 0:
            table.add_row("Peak Memory", f"{metrics.peak_memory_mb:.1f} MB")

        panel = Panel(table, title=f"✓ {stage_name}", border_style="green")
        self.console.print(panel)

    def _simple_stage_summary(self, stage_name: str, metrics: StageMetrics):
        """Print simple text stage summary."""
        print(f"✓ {stage_name} complete:")
        print(f"    Duration:   {self._format_duration(metrics.duration)}")
        print(f"    Files:      {metrics.files_processed}")
        print(f"    Changed:    {metrics.files_changed}")
        print(f"    Skipped:    {metrics.files_skipped}")
        print(f"    Errors:     {metrics.files_errored}")
        print(f"    Throughput: {metrics.throughput:.1f} files/sec")
        if metrics.bytes_processed > 0:
            print(f"    Data:       {self._format_bytes(metrics.bytes_processed)}")
        if metrics.peak_memory_mb > 0:
            print(f"    Memory:     {metrics.peak_memory_mb:.1f} MB")

    def print_summary(self):
        """Print overall pipeline summary."""
        total_duration = time.time() - self.start_time
        total_files = sum(m.files_processed for m in self.metrics.values())
        total_changed = sum(m.files_changed for m in self.metrics.values())
        total_errors = sum(m.files_errored for m in self.metrics.values())

        if self.use_rich:
            self._rich_summary(total_duration, total_files, total_changed, total_errors)
        else:
            self._simple_summary(total_duration, total_files, total_changed, total_errors)

    def _rich_summary(self, duration: float, files: int, changed: int, errors: int):
        """Print rich formatted overall summary."""
        table = Table(title="Pipeline Summary", show_header=True)
        table.add_column("Stage", style="cyan")
        table.add_column("Duration", justify="right")
        table.add_column("Files", justify="right")
        table.add_column("Changed", justify="right")
        table.add_column("Errors", justify="right")
        table.add_column("Rate", justify="right")

        for stage_name, metrics in self.metrics.items():
            table.add_row(
                stage_name,
                self._format_duration(metrics.duration),
                str(metrics.files_processed),
                str(metrics.files_changed),
                str(metrics.files_errored),
                f"{metrics.throughput:.1f}/s"
            )

        table.add_section()
        table.add_row(
            "[bold]TOTAL[/bold]",
            f"[bold]{self._format_duration(duration)}[/bold]",
            f"[bold]{files}[/bold]",
            f"[bold]{changed}[/bold]",
            f"[bold]{errors}[/bold]",
            f"[bold]{files/duration if duration > 0 else 0:.1f}/s[/bold]"
        )

        self.console.print("\n")
        self.console.print(table)

    def _simple_summary(self, duration: float, files: int, changed: int, errors: int):
        """Print simple text overall summary."""
        print("\n" + "="*60)
        print("PIPELINE SUMMARY")
        print("="*60)

        for stage_name, metrics in self.metrics.items():
            print(f"{stage_name:20s} {self._format_duration(metrics.duration):>8s} "
                  f"{metrics.files_processed:>6d} files  "
                  f"{metrics.throughput:>6.1f}/s")

        print("-"*60)
        print(f"{'TOTAL':20s} {self._format_duration(duration):>8s} "
              f"{files:>6d} files  "
              f"{files/duration if duration > 0 else 0:>6.1f}/s")
        print("="*60)

        if changed > 0:
            print(f"Changed: {changed} files")
        if errors > 0:
            print(f"Errors:  {errors} files")
        print()

    def record_result(self, result: StageResult):
        """Record a StageResult into metrics."""
        if result.stage_name in self.metrics:
            metrics = self.metrics[result.stage_name]
            metrics.files_processed = result.files_processed
            metrics.files_changed = result.files_changed
            metrics.files_skipped = result.files_skipped
            metrics.files_errored = result.files_errored


def enable_verbose_logging():
    """
    Enable verbose logging for all MUSAEUS modules.
    Shows DEBUG-level messages with timestamps and module names.
    """
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)7s] %(name)s - %(message)s",
        datefmt="%H:%M:%S"
    )


def get_tracker(verbose: bool = False) -> ProgressTracker:
    """
    Get a ProgressTracker instance.

    Args:
        verbose: Enable verbose output with detailed metrics

    Returns:
        ProgressTracker instance
    """
    return ProgressTracker(verbose=verbose)
