#!/usr/bin/env python3
"""Print a compact summary from a PinchBench parallel-merged.json file."""

import argparse
import json
from pathlib import Path


def format_float(value: float | None, digits: int = 4) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize PinchBench merged benchmark results")
    parser.add_argument(
        "results_json",
        nargs="?",
        default="Tasks/Pinchbench/.pinchbench-results-docker/latest/parallel-merged.json",
        help="Path to parallel-merged.json",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Number of per-task rows to print (default: 10)",
    )
    args = parser.parse_args()

    results_path = Path(args.results_json)
    if results_path.parts[-2:] == ("latest", "parallel-merged.json"):
        run_root = results_path.parent.parent
        runs = sorted((p for p in run_root.iterdir() if p.is_dir()), reverse=True)
        if not runs:
            raise SystemExit(f"No benchmark runs found under {run_root}")
        latest_run = runs[0]
        direct_merged = latest_run / "parallel-merged.json"
        if direct_merged.exists():
            results_path = direct_merged
        else:
            iteration_merged = sorted(
                latest_run.glob("iteration-*/parallel-merged.json"),
                reverse=True,
            )
            if not iteration_merged:
                raise SystemExit(
                    f"No merged results found under {latest_run} "
                    "(expected parallel-merged.json or iteration-*/parallel-merged.json)"
                )
            results_path = iteration_merged[0]

    if not results_path.exists():
        raise SystemExit(f"Results file not found: {results_path}")

    data = json.loads(results_path.read_text(encoding="utf-8"))
    tasks = data.get("tasks", [])
    efficiency = data.get("efficiency", {}) or {}

    scored = [
        float((task.get("grading", {}) or {}).get("mean", 0.0) or 0.0)
        for task in tasks
    ]
    passed = sum(1 for score in scored if score >= 1.0)
    mean_score = (sum(scored) / len(scored)) if scored else 0.0

    print(f"Run file: {results_path}")
    print(f"Model: {data.get('model', '-')}")
    print(f"Suite: {data.get('suite', '-')}")
    print(f"Workers: {data.get('parallel_workers', '-')}")
    print(f"Tasks: {len(tasks)}")
    print(f"Pass rate: {passed}/{len(tasks)}")
    print(f"Mean score: {format_float(mean_score)}")
    print(f"Total tokens: {efficiency.get('total_tokens', 0)}")
    print(f"Input tokens: {efficiency.get('total_input_tokens', 0)}")
    print(f"Output tokens: {efficiency.get('total_output_tokens', 0)}")
    print(f"Total cost (USD): {format_float(efficiency.get('total_cost_usd'), 6)}")
    print(f"Execution time (s): {format_float(efficiency.get('total_execution_time_seconds'), 2)}")
    print(f"Score / 1K tokens: {format_float(efficiency.get('score_per_1k_tokens'), 6)}")
    print(f"Score / dollar: {format_float(efficiency.get('score_per_dollar'), 4)}")
    print()
    print("Per-task:")
    print("task_id\tstatus\tscore\ttokens\tcost_usd")
    for task in tasks[: args.top]:
        usage = task.get("usage", {}) or {}
        grading = task.get("grading", {}) or {}
        print(
            f"{task.get('task_id', '-')}\t"
            f"{task.get('status', '-')}\t"
            f"{format_float(float(grading.get('mean', 0.0) or 0.0))}\t"
            f"{usage.get('total_tokens', 0)}\t"
            f"{format_float(float(usage.get('cost_usd', 0.0) or 0.0), 6)}"
        )


if __name__ == "__main__":
    main()
