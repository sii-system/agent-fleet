"""Pass a prepared ALE CPU dataset to Harbor's native job CLI."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from .source import REVISION, select_image, source_digest, validate_source
from .workflow import REPO, environment_marker, select_tasks


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--cache", type=Path)
    cli.add_argument("--dataset", type=Path)
    cli.add_argument("--source", type=Path)
    cli.add_argument("--native-python", type=Path)
    image_map = os.environ.get("HARBOR_ALE_IMAGE_MAP")
    cli.add_argument("--image-map", type=Path, default=Path(image_map) if image_map else None)
    selection = cli.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--domain", action="append", default=[])
    selection.add_argument("--task", action="append", default=[])
    cli.add_argument("--os", choices=("linux", "windows"))
    cli.add_argument("--agent", default="ale-command")
    cli.add_argument("--model", default=os.environ.get("MODEL", ""))
    cli.add_argument("--workers", type=int, default=1)
    output = os.environ.get("OUTPUT_PATH")
    if run_id := os.environ.get("RUN_ID"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", run_id):
            raise ValueError("RUN_ID must be a safe Harbor job name")
        output = output or str(Path(os.environ.get("OUTPUT_ROOT", REPO / "runs")) / run_id)
    cli.add_argument("--output", type=Path, default=Path(output) if output else None)
    cli.add_argument("--task-data-source", default="baked_in_sandbox")
    cli.add_argument("--linux-backend", choices=("auto", "sbx", "docker"), default="auto")
    cli.add_argument("--windows-backend", "--backend", choices=("kubevirt", "docker"),
                     default=os.environ.get("HARBOR_ALE_WINDOWS_BACKEND", "kubevirt"))
    cli.add_argument("--dry-run", action="store_true")
    cli.add_argument("harbor_args", nargs=argparse.REMAINDER)
    return cli


def command(args):
    runner = Path(os.environ.get("HARBOR_RUNNER_DIR", "/opt/harbor-runner"))
    if not runner.exists() and "HARBOR_RUNNER_DIR" not in os.environ:
        runner = Path.home() / ".local/share/agent-fleet/harbor-runner"
    if os.environ.get("OPIK_URL"):
        cmd = [os.environ.get("HARBOR_OPIK_BIN", str(runner / "bin/opik")), "harbor"]
    else:
        cmd = [os.environ.get("HARBOR_CLI_BIN", str(Path(sys.executable).with_name("harbor")))]
    agent = "Agents.AgentsLastExam.agent:ALECommandAgent" if args.agent == "ale-command" else args.agent
    cmd += ["run", "--path", str(args.dataset.resolve()),
            "--agent", agent,
            "--env", "ale_adapter.environment:ALEEnvironment",
            "--verifier", "ale_adapter.verifier:ALEVerifier",
            "--ek", f"source={args.source.resolve()}",
            "--ek", f"native_python={args.native_python.absolute()}",
            "--ek", f"image_map={args.image_map.resolve()}",
            "--ek", f"task_data_source={args.task_data_source}",
            "--ek", f"linux_backend={args.linux_backend}",
            "--ek", f"windows_backend={getattr(args, 'windows_backend', 'kubevirt')}"]
    cmd += ["--n-concurrent", str(getattr(args, "workers", 1))]
    if model := getattr(args, "model", None):
        cmd += ["--model", model]
    if output := getattr(args, "output", None):
        output = output.resolve()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", output.name):
            raise ValueError("Output directory name must be a safe Harbor job name")
        cmd += ["--jobs-dir", str(output.parent), "--job-name", output.name]
    else:
        cmd += ["--jobs-dir", os.environ.get("OUTPUT_ROOT", str(REPO / "runs"))]
    for definition in getattr(args, "selected", []):
        cmd += ["--include-task-name", definition.parent.parent.name]
    return cmd + (args.harbor_args[1:] if args.harbor_args[:1] == ["--"] else args.harbor_args)


def main():
    cli = parser()
    args = cli.parse_args()
    if args.windows_backend not in ("kubevirt", "docker"):
        cli.error("HARBOR_ALE_WINDOWS_BACKEND must be kubevirt or docker")
    if args.workers < 1:
        cli.error("--workers must be positive")
    if args.cache is not None:
        if not (args.all or args.domain or args.task):
            cli.error("Choose --all, --domain or --task")
        marker = environment_marker(Path(sys.prefix), REPO / "Tasks/AgentsLastExam/uv.lock")
        args.source = args.source or Path(marker["source"])
        args.dataset = args.dataset or Path(marker["dataset"])
        args.native_python = args.native_python or Path(marker["native_python"])
    if not all((args.source, args.dataset, args.native_python, args.image_map)):
        cli.error("Run ALE setup first and configure HARBOR_ALE_IMAGE_MAP or --image-map")
    source = validate_source(args.source)
    provenance = json.loads((args.dataset / "dataset.json").read_text())
    if provenance.get("scope") != "cpu" or provenance["revision"] != REVISION or provenance["source_sha256"] != source_digest(source):
        cli.error("ALE dataset differs from the prepared native source")
    if not args.native_python.is_file():
        cli.error("Run ALE setup before launch")
    mapping = json.loads(args.image_map.read_text())
    definitions = sorted(args.dataset.glob("*/environment/ale.json"))
    if len(definitions) != provenance["tasks"]:
        cli.error("ALE dataset is incomplete")
    for definition in definitions:
        native = json.loads(definition.read_text())
        if native["revision"] != REVISION or native["source_sha256"] != provenance["source_sha256"]:
            cli.error("ALE task provenance changed")
        if native.get("requires_gpu") or native["os"] not in ("linux", "windows"):
            cli.error("ALE dataset must contain Linux/Windows CPU tasks only")
    try:
        selected = select_tasks(definitions, domains=args.domain, task_ids=args.task, os_type=args.os)
    except ValueError as error:
        cli.error(str(error))
    for definition in selected:
        select_image(json.loads(definition.read_text()), mapping, args.windows_backend)
    if args.domain or args.task or args.os:
        args.selected = selected
    cmd = command(args)
    if args.dry_run:
        # Native CLI arguments can contain credentials. Do not print them.
        counts = {os_type: sum(json.loads(path.read_text())["os"] == os_type for path in selected)
                  for os_type in ("linux", "windows")}
        print(json.dumps({"benchmark": "ale-cpu", "tasks": len(selected), "os": counts,
                          "selected": [path.parent.parent.name for path in selected],
                          "agent": args.agent, "windows_backend": args.windows_backend, "revision": REVISION}))
        return
    os.execvpe(cmd[0], cmd, os.environ)


if __name__ == "__main__":
    main()
