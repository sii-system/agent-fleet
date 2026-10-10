"""Select WAA tasks, then replace this process with Harbor's CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from importlib import import_module
from pathlib import Path

from .adapter import materialize, task_name
from .dataset import BENCHMARKS, COMPONENT, digest, load_tasks
from .preflight import environment_marker
from .source import validate_runtime

REPO = Path(__file__).resolve().parents[4]


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--benchmark", choices=BENCHMARKS, default="waa-v2")
    cli.add_argument("--backend", choices=("kubevirt", "docker"),
                     default=os.environ.get("HARBOR_WAA_BACKEND", "kubevirt"))
    cli.add_argument("--cache", type=Path, required=True)
    cli.add_argument("--runtime", type=Path)
    select = cli.add_mutually_exclusive_group(required=True)
    select.add_argument("--all", action="store_true")
    select.add_argument("--domain", action="append", default=[])
    select.add_argument("--task", action="append", default=[])
    cli.add_argument("--agent", default="pcagent")
    cli.add_argument("--model", default=os.environ.get("MODEL", ""))
    cli.add_argument("--workers", type=int, default=1)
    output = os.environ.get("OUTPUT_PATH")
    run_id = os.environ.get("RUN_ID")
    if run_id:
        if not COMPONENT.fullmatch(run_id):
            raise ValueError("RUN_ID must be a safe Harbor job name")
        output = output or str(Path(os.environ.get("OUTPUT_ROOT", REPO / "runs")) / run_id)
    cli.add_argument("--output", type=Path, default=Path(output) if output else None)
    cli.add_argument("--dry-run", action="store_true")
    cli.add_argument("harbor_args", nargs=argparse.REMAINDER, help="Harbor options after --")
    return cli


def command(args, dataset, tasks, runtime, pcagent_runtime):
    if os.environ.get("OPIK_URL"):
        runner = Path(os.environ.get("HARBOR_RUNNER_DIR", "/opt/harbor-runner"))
        if not runner.exists() and "HARBOR_RUNNER_DIR" not in os.environ:
            runner = Path.home() / ".local/share/agent-fleet/harbor-runner"
        cmd = [os.environ.get("HARBOR_OPIK_BIN", str(runner / "bin/opik")), "harbor"]
    else:
        cmd = [os.environ.get("HARBOR_CLI_BIN", str(Path(sys.executable).with_name("harbor")))]
    cmd += ["run", "--path", str(dataset), "--n-concurrent", str(args.workers),
            "--agent", "Agents.WindowsAgentArena.agent:WAAAgent", "--ak", f"factory={args.agent}",
            "--env", ("waa_benchmark.docker_environment:WAADockerEnvironment" if args.backend == "docker"
                       else "waa_benchmark.environment:WAAEnvironment"), "--ek", f"runtime={runtime}",
            "--ek", f"pcagent_runtime={pcagent_runtime}", "--ek", f"native_python={sys.executable}",
            "--verifier", "waa_benchmark.verifier:WAAVerifier"]
    if args.model:
        cmd += ["--model", args.model]
    for task in tasks:
        cmd += ["--include-task-name", task_name(task)]
    if args.output:
        output = args.output.resolve()
        if not COMPONENT.fullmatch(output.name):
            raise ValueError("Output directory name must be a safe Harbor job name")
        cmd += ["--jobs-dir", str(output.parent), "--job-name", output.name]
    else:
        cmd += ["--jobs-dir", os.environ.get("OUTPUT_ROOT", str(REPO / "runs"))]
    return cmd + (args.harbor_args[1:] if args.harbor_args[:1] == ["--"] else args.harbor_args)


def main():
    cli = parser()
    args = cli.parse_args()
    if args.workers < 1:
        cli.error("--workers must be positive")
    if args.backend not in ("kubevirt", "docker"):
        cli.error("HARBOR_WAA_BACKEND must be kubevirt or docker")
    environment_marker(Path(sys.prefix), REPO / "Tasks/WindowsAgentArena/uv.lock")
    runtime = (args.runtime or args.cache / "runtime" / BENCHMARKS[args.benchmark]["revision"]).resolve()
    pcagent_runtime = runtime if args.benchmark == "waa-v2" else args.cache / "runtime" / BENCHMARKS["waa-v2"]["revision"]
    validate_runtime(runtime, benchmark=args.benchmark)
    validate_runtime(pcagent_runtime)
    tasks = load_tasks(runtime / "client", benchmark=args.benchmark, domains=args.domain,
                       task_ids=[name for item in args.task for name in item.split(",")])
    dataset = args.cache / "tasks" / args.benchmark / digest(Path(__file__).with_name("adapter.py"))[:12]
    cmd = command(args, dataset, tasks, runtime, pcagent_runtime)
    if args.dry_run:
        # Harbor options can contain credentials; do not render them.
        print(json.dumps({"benchmark": args.benchmark, "backend": args.backend,
                          "selected": [t.key for t in tasks], "agent": args.agent}, indent=2))
        return
    if args.agent == "pcagent" and (not args.model or not os.environ.get("OPENAI_API_KEY")):
        cli.error("PC-Agent requires MODEL/--model and OPENAI_API_KEY or API_KEY")
    if args.agent != "pcagent":
        if ":" not in args.agent:
            cli.error("Custom agents use module:factory")
        module, factory = args.agent.rsplit(":", 1)
        if not callable(getattr(import_module(module), factory)):
            cli.error("Custom agent factory must be callable")
    materialize(runtime, dataset, benchmark=args.benchmark)
    os.execvpe(cmd[0], cmd, os.environ)


if __name__ == "__main__":
    main()
