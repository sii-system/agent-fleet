"""Local Harbor queue operations. Shell callers hold the queue lock."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from harbor_monitor.artifacts import claimed_task_count, load_task_file_manifest
from harbor_runtime.artifacts import write_text_atomic


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def count_running_workers(queue: Path, *, clean_stale: bool = False) -> int:
    running = 0
    for current in queue.glob("worker-*.current"):
        try:
            fields = current.read_text().rstrip("\n").split("\t")
        except OSError:
            continue
        if len(fields) < 3 and fields[0]:
            running += 1  # Older workers did not publish a PID.
        elif len(fields) == 3 and fields[2].isdecimal() and pid_is_alive(int(fields[2])):
            running += 1
        elif clean_stale:
            current.unlink(missing_ok=True)
    return running


def recorded_indices(queue: Path) -> set[str]:
    indices = set()
    for filename in ("done.txt", "failed.txt"):
        with (queue / filename).open() as records:
            indices.update(line.partition("\t")[0] for line in records)
    return indices


def recover_claims(
    queue: Path, cursor: Path, attempts: int,
) -> tuple[int, list[tuple[str, str, int]]]:
    claims = queue / "claims"
    claims.mkdir(exist_ok=True)
    # Runs started with a separate reclaim list may still have pending retries.
    reclaim = queue / "reclaim.txt"
    if reclaim.exists():
        for line in reclaim.read_text().splitlines():
            index, name, attempt = line.split("\t")
            path = claims / str(int(index))
            if not path.exists():
                write_text_atomic(path, f"{index}\t{name}\t0\t{attempt}\n")
        reclaim.unlink()

    paths = [path for path in claims.iterdir() if path.name.isdecimal()]
    next_index = int(cursor.read_text()) if cursor.exists() else 1
    # A worker may die between recording its claim and advancing the cursor.
    if paths:
        after_claims = max(int(path.name) for path in paths) + 1
        if after_claims > next_index:
            write_text_atomic(cursor, f"{after_claims}\n")
            next_index = after_claims

    retry = []
    terminal = None
    for path in paths:
        index = path.name
        _, name, pid, attempt = path.read_text().rstrip("\n").split("\t")
        if pid_is_alive(int(pid)):
            continue
        if terminal is None:
            terminal = recorded_indices(queue)
        if index not in terminal:
            if int(attempt) < attempts:
                retry.append((index, name, int(attempt) + 1))
                continue
            with (queue / "failed.txt").open("a") as failed:
                failed.write(f"{index}\t{name}\tworker-lost\tWorkerLost\n")
        path.unlink()
    return next_index, retry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("observe")
    pick = commands.add_parser("pick")
    release = commands.add_parser("release")
    for command in (pick, release):
        command.add_argument("pid", type=int)
        command.add_argument("worker")
    release.add_argument("index", type=int)
    args = parser.parse_args()
    queue = Path(os.environ["QUEUE_DIR"])
    if args.command == "release":
        path = queue / "claims" / str(args.index)
        if path.exists() and path.read_text().split("\t")[2] == str(args.pid):
            path.unlink()
            current = queue / f"worker-{args.worker}.current"
            if current.exists() and current.read_text().partition("\t")[0] == str(args.index):
                current.unlink()
        return

    cursor = Path(os.environ["NEXT_INDEX_FILE"])
    limit = os.environ.get("HARBOR_QUEUE_CLAIM_ATTEMPTS", "2")
    attempts = int(limit) if limit.isdecimal() and int(limit) > 0 else 2
    next_index, retry = recover_claims(queue, cursor, attempts)
    manifest = load_task_file_manifest(Path(os.environ["TASK_FILE"]))
    if args.command == "observe":
        running = count_running_workers(queue, clean_stale=True)
        print(len(manifest), claimed_task_count(manifest, next_index), running)
        return

    if retry:
        index, name, attempt = min(retry, key=lambda row: int(row[0]))
    else:
        for index, name in manifest.items():
            if int(index) >= next_index:
                break
        else:
            return
        attempt = 1

    claim = queue / "claims" / index
    write_text_atomic(claim, f"{index}\t{name}\t{args.pid}\t{attempt}\n")
    if int(index) >= next_index:
        write_text_atomic(cursor, f"{int(index) + 1}\n")
    current = queue / f"worker-{args.worker}.current"
    write_text_atomic(current, f"{index}\t{name}\t{args.pid}\n")
    print(f"{index}\t{name}")


if __name__ == "__main__":
    main()
