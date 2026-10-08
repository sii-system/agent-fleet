"""Check every native setup/getter/metric before provisioning any VMs."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from .dataset import load_tasks
from .desktop import create_desktop


def check_client(client, cache_dir, *, benchmark="waa-v2", pcagent_client=None):
    tasks = load_tasks(client, benchmark=benchmark)
    env = create_desktop("waa-guest.invalid", cache_dir, screen_size=(1280, 720), action_space="pyautogui", benchmark=benchmark)
    setups, metrics, getters = set(), set(), set()
    for task in tasks:
        config = json.loads(task.path.read_text(encoding="utf-8"))
        env._set_task_info(config)
        for item in config.get("config", []) + config["evaluator"].get("postconfig", []):
            name = item["type"]
            method = getattr(env.setup_controller, "_" + name + "_setup", None)
            if not callable(method):
                raise TypeError(f"Unsupported setup {name} in {task.key}")
            inspect.signature(method).bind(**item["parameters"])
            setups.add(name)
        funcs = config["evaluator"]["func"]
        metrics.update(funcs if isinstance(funcs, list) else [funcs])
        for key in ("result", "expected"):
            values = config["evaluator"].get(key)
            for value in values if isinstance(values, list) else [values]:
                if value:
                    getters.add(value["type"])
    # Also validate the reference agent's import dependencies without constructing
    # its OpenAI client or making model requests.
    from Agents.WindowsAgentArena.agent import pcagent_class

    PCAgent = pcagent_class(pcagent_client)
    assert callable(PCAgent.predict)
    return {"tasks": len(tasks), "domains": len({t.domain for t in tasks}),
            "setups": sorted(setups), "metrics": sorted(metrics), "getters": sorted(getters)}


def main():
    import argparse
    import sys
    import tempfile

    from .dataset import BENCHMARKS
    from .source import validate_runtime

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", choices=BENCHMARKS, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--pcagent-runtime", type=Path, required=True)
    args = parser.parse_args()
    validate_runtime(args.runtime, benchmark=args.benchmark)
    validate_runtime(args.pcagent_runtime)
    sys.path.insert(0, str(args.runtime / "client"))
    with tempfile.TemporaryDirectory(prefix="waa-preflight-") as cache:
        coverage = check_client(args.runtime / "client", cache, benchmark=args.benchmark,
                                pcagent_client=args.pcagent_runtime / "client")
    print(json.dumps({"benchmark": args.benchmark, "coverage": coverage}, indent=2))


def environment_marker(env_dir, lockfile):
    import importlib.metadata
    import sys

    from .dataset import digest

    marker = json.loads((Path(env_dir) / ".agent-fleet-waa.json").read_text())
    if (sys.version_info[:2] != (3, 12) or marker.get("version") != 1
            or marker.get("lock_sha256") != digest(lockfile) or not marker.get("packages")):
        raise ValueError("WAA host environment is stale; run Tasks/WindowsAgentArena/setup.sh")
    for name, version in marker["packages"].items():
        if importlib.metadata.version(name) != version:
            raise ValueError(f"WAA host package {name} changed; run Tasks/WindowsAgentArena/setup.sh")


if __name__ == "__main__":
    main()
