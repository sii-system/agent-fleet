"""Explicit setup of the pinned WAA and WAA-V2 clients."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path

from .dataset import BENCHMARKS, digest
from .source import prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--benchmark", choices=[*BENCHMARKS, "both"], default="both")
    parser.add_argument("--source", type=Path, help="Reuse a pinned clean checkout (requires one benchmark)")
    parser.add_argument("--lockfile", type=Path, required=True)
    args = parser.parse_args()
    if args.source and args.benchmark == "both":
        parser.error("--source requires --benchmark waa or waa-v2")
    requested = list(BENCHMARKS) if args.benchmark == "both" else [args.benchmark]
    # PC-Agent is from the pinned WAA-V2 client, also when driving WAA v1.
    releases = list(dict.fromkeys(["waa-v2", *requested]))
    for benchmark in releases:
        release = BENCHMARKS[benchmark]
        source = args.source if benchmark == args.benchmark and args.source else args.cache / "source" / release["revision"]
        if not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "fetch", "--depth=1", release["url"], release["revision"]], check=True)
            subprocess.run(["git", "-C", str(source), "checkout", "--detach", "FETCH_HEAD"], check=True)
        runtime = prepare(source, args.cache / "runtime" / release["revision"], benchmark=benchmark)
        # Native packages have the same names across releases; isolate imports.
        subprocess.run([sys.executable, "-m", "waa_benchmark.preflight", "--benchmark", benchmark,
                        "--runtime", str(runtime), "--pcagent-runtime",
                        str(args.cache / "runtime" / BENCHMARKS["waa-v2"]["revision"])], check=True)
    packages = {dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions()}
    (Path(sys.prefix) / ".agent-fleet-waa.json").write_text(json.dumps({
        "version": 1, "lock_sha256": digest(args.lockfile), "packages": packages,
    }) + "\n")


if __name__ == "__main__":
    main()
