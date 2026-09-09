"""Stop this nightly run's detached processes before reading its artifacts."""

import argparse
import os
import signal
import sys
import time
from pathlib import Path


def process_state(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return int(fields[1]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def run_processes(run_dir, run_id):
    processes = {}
    selected = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        state = process_state(pid)
        if state is None:
            continue
        processes[pid] = state
        try:
            environment = dict(
                item.split(b"=", 1)
                for item in (entry / "environ").read_bytes().split(b"\0")
                if b"=" in item
            )
        except (OSError, ValueError):
            continue
        if environment.get(b"RUN_ID") == run_id.encode() and environment.get(
            b"OUTPUT_PATH"
        ) == os.fsencode(run_dir):
            selected.add(pid)
        # Pi intentionally drops RUN_ID/OUTPUT_PATH from its minimal environment.
        pi_home = environment.get(b"PI_CODING_AGENT_DIR")
        if pi_home:
            home = Path(os.fsdecode(pi_home)).resolve()
            if home.is_relative_to(
                run_dir / "analyzer/.pi-analyzer-home"
            ) or home.is_relative_to(run_dir / "benchmark-summary/.pi-home"):
                selected.add(pid)
    previous = set()
    while previous != selected:
        previous = selected.copy()
        selected.update(
            pid for pid, (parent, _) in processes.items() if parent in selected
        )
    # A cleanup step can inherit the same run environment. Never signal itself
    # or the Actions runner/step shell that invoked it.
    ancestor = os.getpid()
    while ancestor in processes:
        selected.discard(ancestor)
        ancestor = processes[ancestor][0]
    return {pid: processes[pid] for pid in selected}


def stop_run(run_dir, run_id, grace_seconds):
    deadline = time.monotonic() + grace_seconds
    tracked = {}
    while True:
        tracked.update(
            {
                pid: state[1]
                for pid, state in run_processes(run_dir, run_id).items()
            }
        )
        tracked = {
            pid: start
            for pid, start in tracked.items()
            if (state := process_state(pid)) and state[1] == start
        }
        if not tracked:
            return
        now = time.monotonic()
        if now > deadline + 5:
            raise RuntimeError(
                "Nightly processes did not stop; refusing to stage artifacts"
            )
        sig = signal.SIGTERM if now < deadline else signal.SIGKILL
        for pid, start in tracked.items():
            if (state := process_state(pid)) and state[1] == start:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
        time.sleep(0.1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--grace-seconds", default=10, type=float)
    args = parser.parse_args()
    if (
        not sys.platform.startswith("linux")
        or not args.run_id
        or not args.run_dir.is_absolute()
    ):
        parser.error("requires Linux, an absolute run directory, and a nonempty run ID")
    stop_run(
        args.run_dir,
        args.run_id,
        args.grace_seconds,
    )


if __name__ == "__main__":
    main()
