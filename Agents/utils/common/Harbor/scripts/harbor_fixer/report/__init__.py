"""Harbor Fixer report generation and rendering."""

from pathlib import Path

from ..agent_invocation import AgentInvoker
from .deterministic import render_fix_report, write_fix_report
from .generation import generate_report_from_paths
from .markdown import render_human_report, write_report_markdown
from .runtime import generate_report_summary


def run_report_from_paths(
    verification_result_path: Path,
    analyzer_output_path: Path,
    output_dir: Path,
    invoker: AgentInvoker,
    *,
    baseline_run_dir: Path | None = None,
    baseline_monitor_policy: str = "auto",
) -> dict:
    """Generate the machine report, then attach its deterministic Markdown view."""

    report = generate_report_from_paths(
        verification_result_path,
        analyzer_output_path,
        output_dir,
        invoker,
        baseline_run_dir=baseline_run_dir,
        baseline_monitor_policy=baseline_monitor_policy,
    )
    report_output_dir = Path(report["artifacts"]["report_input_path"]).parent
    return write_report_markdown(report, report_output_dir)

__all__ = [
    "generate_report_from_paths",
    "generate_report_summary",
    "render_fix_report",
    "render_human_report",
    "run_report_from_paths",
    "write_fix_report",
    "write_report_markdown",
]
