#!/usr/bin/env python3
"""Prepare a dataset using the same Bundle API as the single-task CLI."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "task_image_manager"

from . import task_cli
from .build_engine.frontend import FrontendBuildError
from .task_preparation import prepare_task_images

PREFIX = "HARBOR_TASK_IMAGE_"


def setting(name: str, default: str) -> str:
    return os.environ.get(PREFIX + name, default)


def boolean(name: str, default: str) -> bool:
    value = setting(name, default)
    if value not in {"0", "1"}:
        raise ValueError(f"{PREFIX}{name} must be 0 or 1")
    return value == "1"


def positive(value: str) -> int:
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Additional image options follow the positional arguments; see task_cli.py --help.",
    )
    parser.add_argument("dataset_root", type=Path)
    parser.add_argument("benchmark")
    parser.add_argument(
        "--task-list", type=Path,
        help="newline-separated task names to prepare instead of the full dataset",
    )
    parser.add_argument(
        "--concurrency", type=positive, default=setting("PREBUILD_CONCURRENCY", "1")
    )
    parser.add_argument(
        "--prebuild-root",
        type=Path,
        default=setting("PREBUILD_ROOT", "/data/harbor-runs/opensandbox-prebuild"),
    )
    parser.add_argument(
        "--build-timeout-sec",
        type=positive,
        default=setting("PREBUILD_BUILD_TIMEOUT_SEC", "7200"),
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=boolean("DRY_RUN", "0")
    )
    parser.add_argument(
        "--reuse-local-upload-cache",
        action=argparse.BooleanOptionalAction,
        default=boolean("PREBUILD_USE_LOCAL_UPLOAD_CACHE", "1"),
    )
    parser.add_argument(
        "--skip-hash-verification",
        action="store_true",
        default=boolean("PREBUILD_SKIP_HASH_VERIFICATION", "0"),
    )
    parser.add_argument(
        "--gc-interval-sec",
        type=int,
        default=setting("PREBUILD_GC_INTERVAL_SEC", "1800"),
    )
    for name, default in (
        ("max-used-space", "500GB"),
        ("min-free-space", "300GB"),
        ("reserved-space", "100GB"),
    ):
        parser.add_argument(
            f"--gc-{name}",
            default=setting("PREBUILD_GC_" + name.upper().replace("-", "_"), default),
        )
    args, task_options = parser.parse_known_args(argv)
    if not args.dataset_root.is_dir():
        parser.error("dataset_root must be an existing directory")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.benchmark):
        parser.error(
            "benchmark must be a single name using letters, digits, '.', '_' or '-'"
        )
    if args.gc_interval_sec < 0:
        parser.error("--gc-interval-sec must be non-negative")
    for name in ("max_used_space", "min_free_space", "reserved_space"):
        if not re.fullmatch(
            r"[0-9]+(?:\.[0-9]+)?(?:[kKmMgGtT][bB]?)?", getattr(args, "gc_" + name)
        ):
            parser.error(f"invalid GC space value: {name}")
    forbidden = {
        "--task-dir",
        "--dataset-root",
        "--include",
        "--bundle-manifest-output",
        "--task-repository",
        "--benchmark-name",
    }
    if any(option.split("=", 1)[0] in forbidden for option in task_options):
        parser.error(
            "task selection and output paths are owned by the dataset entrypoint"
        )
    options = [
        "--task-dir",
        str(args.dataset_root),
        "--benchmark-name",
        args.benchmark,
        "--build-timeout-sec",
        str(args.build_timeout_sec),
        *task_options,
    ]
    if args.dry_run:
        options.append("--dry-run")
    if args.reuse_local_upload_cache:
        options.append("--reuse-local-upload-cache")
    if args.skip_hash_verification:
        options.append("--skip-hash-verification")
    args.task_args = task_cli.parse_args(options)
    if args.task_args.task_repository:
        parser.error("a dataset must derive its repository separately for each task")
    return args


def discover_tasks(root: Path):
    tasks, skipped = [], []
    for task in sorted(root.resolve().iterdir()):
        if not task.is_dir() or task.name.startswith("."):
            continue
        if not (task / "task.toml").is_file():
            skipped.append(f"{task.name}\tmissing-task.toml")
        elif not any(
            (task / "environment" / name).is_file()
            for name in ("Dockerfile", "docker-compose.yml", "docker-compose.yaml")
        ):
            skipped.append(f"{task.name}\tmissing-environment-definition")
        else:
            tasks.append(task)
    return tasks, skipped


def select_tasks(tasks, skipped, task_list: Path | None):
    if task_list is None:
        return tasks, skipped
    names = task_list.read_text(encoding="utf-8").splitlines()
    if not names or any(not name or name != name.strip() for name in names):
        raise ValueError("task list must contain nonempty task names, one per line")
    if len(names) != len(set(names)):
        raise ValueError("task list contains duplicate task names")
    selected = set(names)
    available = {task.name for task in tasks}
    unavailable = sorted(selected - available)
    if unavailable:
        raise ValueError(f"task list contains unknown or unsupported tasks: {', '.join(unavailable)}")
    return [task for task in tasks if task.name in selected], []


@dataclass(frozen=True)
class TaskResult:
    task: str
    status: str
    image_ref: str = ""
    manifest_path: str = ""
    error: str = ""


def prepare_task(task: Path, args, run_dir: Path) -> TaskResult:
    task_args = copy.deepcopy(args.task_args)
    task_args.task_dir = task
    task_args.bundle_manifest_output = run_dir / "bundles" / f"{task.name}.json"
    try:
        prepared = prepare_task_images(task_args)
        return TaskResult(
            task.name, "ready", prepared.main_image_ref, str(prepared.manifest_path)
        )
    except FrontendBuildError as exc:
        return TaskResult(task.name, "fatal", error=str(exc))
    except Exception as exc:  # noqa: BLE001 -- isolate ordinary task failures
        return TaskResult(task.name, "failed", error=f"{type(exc).__name__}: {exc}")


@contextmanager
def periodic_gc(args, run_dir: Path):
    stop = threading.Event()

    def prune():
        with (run_dir / "buildkit-gc.log").open("a") as log:
            try:
                subprocess.run(
                    [
                        "docker",
                        "buildx",
                        "prune",
                        "--force",
                        "--max-used-space",
                        args.gc_max_used_space,
                        "--min-free-space",
                        args.gc_min_free_space,
                        "--reserved-space",
                        args.gc_reserved_space,
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=60,
                    check=True,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                log.write(f"prune failed: {type(exc).__name__}\n")
                print(
                    f"[prebuild][warning] cache prune failed; log={log.name}",
                    flush=True,
                )

    def loop():
        while not stop.wait(args.gc_interval_sec):
            prune()

    thread = None
    try:
        if not args.dry_run:
            prune()
            if args.gc_interval_sec:
                thread = threading.Thread(target=loop, name="image-cache-gc")
                thread.start()
        yield
    finally:
        stop.set()
        if thread is not None:
            thread.join()


def dispatch(tasks, args, run_dir: Path, emit) -> list[TaskResult]:
    """Keep only concurrency tasks in flight; stop new work on shared failure."""
    results = []
    remaining = iter(tasks)
    pool = ThreadPoolExecutor(max_workers=args.concurrency)
    pending = set()
    fatal = False
    try:
        for _ in range(args.concurrency):
            task = next(remaining, None)
            if task is not None:
                pending.add(pool.submit(prepare_task, task, args, run_dir))
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                result = future.result()
                results.append(result)
                fatal |= result.status == "fatal"
                emit(
                    f"[prebuild][{result.status}] task={result.task} image_ref={result.image_ref} "
                    f"bundle={result.manifest_path}"
                    + (f" error={result.error}" if result.error else "")
                )
            if not fatal:
                for _ in done:
                    task = next(remaining, None)
                    if task is not None:
                        pending.add(pool.submit(prepare_task, task, args, run_dir))
    finally:
        # Active builds finish under their own timeout; no new work is submitted.
        pool.shutdown(wait=True, cancel_futures=True)
    return results


def run(args) -> int:
    task_args = args.task_args
    if not task_args.registry:
        raise ValueError("--registry or YICLOUD_HARBOR_HOST is required")
    if not args.dry_run:
        if not task_args.docker_config.is_file():
            raise ValueError("Docker config not found")
        subprocess.run(
            ["docker", "buildx", "version"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    tasks, skipped = select_tasks(*discover_tasks(args.dataset_root), args.task_list)
    args.prebuild_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(
        tempfile.mkdtemp(prefix=f"{args.benchmark}-{stamp}-", dir=args.prebuild_root)
    )
    (run_dir / "bundles").mkdir()
    (run_dir / "supported.txt").write_text("".join(f"{task.name}\n" for task in tasks))
    (run_dir / "supported.nul").write_bytes(
        b"".join(os.fsencode(task) + b"\0" for task in tasks)
    )
    (run_dir / "skipped.txt").write_text("".join(f"{line}\n" for line in skipped))
    previous_batch = os.environ.get(PREFIX + "PREBUILD_RUN_DIR")
    # Registry and base-image transport bypass build proxies.
    old_proxy = {name: os.environ.get(name) for name in ("NO_PROXY", "no_proxy")}
    hosts = os.environ.get("NO_PROXY", os.environ.get("no_proxy", "")).split(",")
    for value in (task_args.registry, task_args.dockerhub_mirror_prefix):
        host = urlsplit(value if "://" in value else "//" + value).hostname
        if host and host not in hosts:
            hosts.append(host)
    try:
        os.environ[PREFIX + "PREBUILD_RUN_DIR"] = str(run_dir)
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(filter(None, hosts))
        with (run_dir / "prebuild.log").open("w") as log:

            def emit(message):
                print(message, flush=True)
                log.write(message + "\n")
                log.flush()

            emit(
                f"[prebuild] benchmark={args.benchmark} supported={len(tasks)} "
                f"skipped={len(skipped)} concurrency={args.concurrency} run_dir={run_dir}"
            )
            with periodic_gc(args, run_dir):
                results = dispatch(tasks, args, run_dir, emit)
            summary = {
                "total": len(tasks),
                "skipped": len(skipped),
                "ready": sum(r.status.startswith("ready") for r in results),
                "failed": sum(r.status == "failed" for r in results),
                "fatal": sum(r.status == "fatal" for r in results),
                "not_dispatched": len(tasks) - len(results),
            }
            (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            emit(f"[prebuild] complete; summary={run_dir / 'summary.json'}")
            return 124 if summary["fatal"] else 123 if summary["failed"] else 0
    finally:
        for name, value in {
            PREFIX + "PREBUILD_RUN_DIR": previous_batch,
            **old_proxy,
        }.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def main(argv=None) -> int:
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        return run(parse_args(argv))
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:  # noqa: BLE001 -- CLI error boundary
        print(f"[prebuild][failed] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
