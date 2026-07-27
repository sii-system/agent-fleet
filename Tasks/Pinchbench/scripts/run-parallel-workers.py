#!/usr/bin/env python3
"""Run PinchBench in Docker containers against local Dockerized OpenClaw gateways."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

SCRIPT_DIR = Path(__file__).resolve().parent
BENCH_DIR = SCRIPT_DIR.parent
REPO_ROOT = BENCH_DIR.parent.parent
OPENCLAW_DIR = REPO_ROOT / "Agents" / "Openclaw"
CONFIG_DIR = BENCH_DIR / "config"
ENV_FILE = OPENCLAW_DIR / ".env"
FLEET_ENV_FILE = OPENCLAW_DIR / "config" / "fleet.env"
CONFIG_ENV_FILE = REPO_ROOT / "config.env"
CONFIG_LOCAL_ENV_FILE = REPO_ROOT / "config.local.env"
PINCHBENCH_ENV_FILE = CONFIG_DIR / "pinchbench.env"
DEFAULT_PINCHBENCH_REF = "f3f1cb560c252541cef6a106c05ba4f2e8068be0"
OPENCLAW_CONTAINER_STATE_DIR = "/home/node/openclaw-state"
OPENCLAW_CONTAINER_WORKSPACE_DIR = "/home/node/workspace"
OPENCLAW_CONTAINER_CONFIG_PATH = f"{OPENCLAW_CONTAINER_STATE_DIR}/openclaw.json"
PINCHBENCH_OPIK_CONTAINER_STATE_DIR = "/home/node/pinchbench-opik-state"
OPIK_TRACER_CONTAINER_DIR = "/opt/openclaw-plugins/openclaw-opik-tracer"
PINCHBENCH_TRACE_MODE_FILE = "/opt/pinchbench-trace-to-opik"


def load_env_file(path: Path) -> dict[str, str]:
    """Load key=value pairs from an env file, ignoring comments and blank lines."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def expand_path(value: str, relative_to: Path | None = None) -> str:
    expanded = Path(os.path.expanduser(os.path.expandvars(value)))
    if relative_to is not None and not expanded.is_absolute():
        expanded = relative_to / expanded
    return str(expanded)


def is_remote_repo_url(value: str) -> bool:
    return "://" in value or value.startswith("git@")


def expand_repo_url(value: str, relative_to: Path) -> str:
    expanded = os.path.expanduser(os.path.expandvars(value))
    if is_remote_repo_url(expanded):
        return expanded
    path = Path(expanded)
    if not path.is_absolute():
        path = relative_to / path
    return str(path.resolve())


def config_bool(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def trace_to_opik_enabled(value: str | None) -> bool:
    """Match the fleet-wide trace switch used by the Harbor/OpenClaw runners."""
    return (value or "true") not in {"false", "0"}


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def normalize_base_url(url: str) -> str:
    """Return the OpenAI-compatible base URL with a single ``/v1`` path suffix.

    config.env documents BASE_URL as the API root (no version suffix); the
    PinchBench agent uses an OpenAI client that needs the ``/v1`` endpoint.
    Idempotent: a path already ending in ``/v1`` is left unchanged. Any
    query/fragment is preserved."""
    if not url:
        return url
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if not path.endswith("/v1"):
        path = f"{path}/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def load_runner_config() -> dict[str, str]:
    config_env = load_env_file(CONFIG_ENV_FILE)
    config_local_env = load_env_file(CONFIG_LOCAL_ENV_FILE)
    fleet_env = load_env_file(FLEET_ENV_FILE)
    generated_env = load_env_file(ENV_FILE)
    runner_env = load_env_file(PINCHBENCH_ENV_FILE)

    def shared(key: str, default: str = "") -> str:
        # Shared infra lives in config.env (base layer); config.local.env holds
        # private overrides/secrets and wins over it; fleet.env overrides both.
        for source in (fleet_env, config_local_env, config_env):
            if key in source:
                return source[key]
        return default

    def shared_model(default: str = "") -> str:
        for source in (fleet_env, config_local_env, config_env):
            if "MODEL" in source:
                return source["MODEL"]
        return default

    config = {
        "COUNT": fleet_env.get("COUNT", "4"),
        "MODEL": shared_model(),
        "BASE_URL": shared("BASE_URL"),
        "API_KEY": shared("API_KEY"),
        "TRACE_TO_OPIK": shared("TRACE_TO_OPIK", "true"),
        "PINCHBENCH_MODEL_PROVIDER": "auto",
        "PINCHBENCH_TIMEOUT_MULTIPLIER": "1.0",
        "JUDGE_MODEL": "",
        "PINCHBENCH_DIR": "/tmp/pinchbench-skill",
        "PINCHBENCH_REF": DEFAULT_PINCHBENCH_REF,
        "PINCHBENCH_OUTPUT_DIR": str(BENCH_DIR / ".pinchbench-results-docker"),
        "PINCHBENCH_DOCKER_IMAGE": "pinchbench-runner:local",
        "PINCHBENCH_FORCE_BUILD": "false",
        "PINCHBENCH_UPLOAD": "false",
        "PINCHBENCH_REPO_URL": "https://github.com/pinchbench/skill",
        "PINCHBENCH_UV_CACHE_DIR": str(BENCH_DIR / ".uv-cache"),
        "CONFIG_BASE": fleet_env.get("CONFIG_BASE", str(Path.home() / "openclaw-instances")),
        "WORKSPACE_BASE": fleet_env.get("WORKSPACE_BASE", str(Path.home() / "openclaw-workspaces")),
        "PLUGIN_CACHE_DIR": fleet_env.get("PLUGIN_CACHE_DIR", ""),
        "CONTAINER_NAME_PREFIX": generated_env.get(
            "CONTAINER_NAME_PREFIX",
            fleet_env.get("CONTAINER_NAME_PREFIX", ""),
        ),
        "OPENCLAW_CONTAINER_USER": "node",
        "NPM_CONFIG_REGISTRY": shared("NPM_CONFIG_REGISTRY"),
        "PIP_INDEX_URL": shared("PIP_INDEX_URL"),
        "PIP_EXTRA_INDEX_URL": shared("PIP_EXTRA_INDEX_URL"),
        "PIP_TRUSTED_HOST": shared("PIP_TRUSTED_HOST"),
    }
    config.update(runner_env)
    # This is a fleet-wide switch, not a PinchBench-specific setting. Keep it
    # on the shared config precedence chain even if an old runner file happens
    # to contain a conflicting key; the caller environment still wins below.
    config["TRACE_TO_OPIK"] = shared("TRACE_TO_OPIK", "true")
    for key in config:
        if key in os.environ:
            config[key] = os.environ[key]
    for key in ("PINCHBENCH_DIR", "PINCHBENCH_OUTPUT_DIR", "PINCHBENCH_UV_CACHE_DIR"):
        config[key] = expand_path(config[key], relative_to=REPO_ROOT)
    for key in ("CONFIG_BASE", "WORKSPACE_BASE"):
        config[key] = expand_path(config[key])
    config["PINCHBENCH_REPO_URL"] = expand_repo_url(config["PINCHBENCH_REPO_URL"], REPO_ROOT)
    return config


def gateway_config_enables_opik_plugin(config_path: Path) -> bool:
    """Return whether a generated OpenClaw config can load the Opik plugin."""
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"Error: cannot inspect OpenClaw gateway config {config_path}: {exc}")

    plugins = payload.get("plugins", {}) if isinstance(payload, dict) else {}
    if not isinstance(plugins, dict):
        return False

    allow = plugins.get("allow", [])
    if isinstance(allow, list) and "openclaw-opik-tracer" in allow:
        return True

    entries = plugins.get("entries", {})
    if isinstance(entries, dict):
        entry = entries.get("openclaw-opik-tracer")
        if isinstance(entry, dict) and entry.get("enabled") is True:
            return True

    load = plugins.get("load", {})
    paths = load.get("paths", []) if isinstance(load, dict) else []
    return isinstance(paths, list) and any(
        isinstance(path, str) and "openclaw-opik-tracer" in path
        for path in paths
    )


def validate_trace_off_gateway_configs(config_base: Path, instances: int) -> None:
    """Fail before launching workers if an existing gateway can still trace."""
    stale_instances: list[int] = []
    for instance_index in range(1, instances + 1):
        config_path = config_base / str(instance_index) / "openclaw.json"
        if config_path.is_file() and gateway_config_enables_opik_plugin(config_path):
            stale_instances.append(instance_index)

    if not stale_instances:
        return

    instance_label = (
        f"instance {stale_instances[0]}"
        if len(stale_instances) == 1
        else "instances " + ",".join(str(index) for index in stale_instances)
    )
    sys.exit(
        "Error: TRACE_TO_OPIK=false, but the OpenClaw gateway config for "
        f"{instance_label} still enables openclaw-opik-tracer. "
        f"Rerun TRACE_TO_OPIK=false ./Agents/Openclaw/scripts/setup.sh {instances}, "
        "then restart the OpenClaw fleet before running PinchBench."
    )


def build_benchmark_command(
    *,
    model: str,
    model_provider: str,
    base_url: str,
    api_key: str,
    suite_chunk: str,
    judge: str,
    upload_enabled: bool,
    prepare_agent_only: bool,
    require_prepared_agent: bool,
    timeout_multiplier: str,
) -> str:
    args = [
        "/tmp/uv run /workspace/scripts/benchmark.py",
        f"--model {shlex.quote(model)}",
        f"--model-provider {shlex.quote(model_provider)}",
    ]
    if base_url:
        args.append(f"--base-url {shlex.quote(base_url)}")
    if api_key:
        args.append(f"--api-key {shlex.quote(api_key)}")
    if prepare_agent_only:
        args.append("--prepare-agent-only")
    else:
        args.extend(
            [
                f"--suite {shlex.quote(suite_chunk)}",
                "--output-dir /results",
            ]
        )
        if judge:
            args.append(f"--judge {shlex.quote(judge)}")
        if not upload_enabled:
            args.append("--no-upload")
    if require_prepared_agent:
        args.append("--require-prepared-agent")
    if timeout_multiplier and timeout_multiplier.strip():
        args.append(f"--timeout-multiplier {shlex.quote(timeout_multiplier)}")

    return "set -euo pipefail\ncd /runner\n" + " ".join(args)


def build_worker_docker_command(
    *,
    image: str,
    instance_index: int,
    container_prefix: str,
    token: str,
    openrouter_key: str,
    openai_api_key: str,
    model_provider: str,
    uv_cache_dir: Path,
    pinchbench_dir: Path,
    worker_dir: Path,
    config_dir: Path,
    workspace_dir: Path,
    plugin_cache_dir: Path | None,
    results_dir: Path,
    opik_state_dir: Path | None,
    bench_cmd: str,
    container_env: dict[str, str] | None = None,
) -> list[str]:
    command = [
        "docker", "run", "--rm",
        "-e", "HOME=/home/node",
        "-e", f"OPENCLAW_STATE_DIR={OPENCLAW_CONTAINER_STATE_DIR}",
        "-e", f"OPENCLAW_CONFIG_PATH={OPENCLAW_CONTAINER_CONFIG_PATH}",
        "-e", f"OPENCLAW_WORKSPACE_DIR={OPENCLAW_CONTAINER_WORKSPACE_DIR}",
        "-e", f"OPENCLAW_GATEWAY_TOKEN={token}",
        "-e", f"OPENROUTER_API_KEY={openrouter_key}",
        "-e", f"OPENAI_API_KEY={openai_api_key}",
        "-e", f"PINCHBENCH_MODEL_PROVIDER={model_provider}",
        "-e", "UV_CACHE_DIR=/home/node/.cache/uv",
        "-e", f"PINCHBENCH_TOKEN={os.environ.get('PINCHBENCH_TOKEN', '')}",
        "-e", f"PINCHBENCH_OFFICIAL_KEY={os.environ.get('PINCHBENCH_OFFICIAL_KEY', '')}",
        "--network", f"container:{container_prefix}-{instance_index}",
        "-v", f"{pinchbench_dir}:/workspace",
        "-v", f"{worker_dir}:/runner",
        "-v", f"{config_dir}:{OPENCLAW_CONTAINER_STATE_DIR}",
        "-v", f"{workspace_dir}:{OPENCLAW_CONTAINER_WORKSPACE_DIR}",
        "-v", f"{results_dir}:/results",
    ]
    if opik_state_dir is not None:
        command.extend(["-v", f"{opik_state_dir}:{PINCHBENCH_OPIK_CONTAINER_STATE_DIR}"])
    command.extend(["-v", f"{uv_cache_dir}:/home/node/.cache/uv"])
    if plugin_cache_dir is not None and plugin_cache_dir.is_dir():
        command.extend(["-v", f"{plugin_cache_dir}:/opt/plugin-cache:ro"])
    for key, value in (container_env or {}).items():
        if value:
            command.extend(["-e", f"{key}={value}"])
    command.extend([image, bench_cmd])
    return command


def wrap_worker_command(
    *, run_as_user: str, bench_cmd: str, tracing_enabled: bool = True
) -> str:
    worker_cmd = "umask 000\n" + bench_cmd
    opik_dirs = (
        f" {OPENCLAW_CONTAINER_STATE_DIR}/opik-state {PINCHBENCH_OPIK_CONTAINER_STATE_DIR}"
        if tracing_enabled
        else ""
    )
    opik_setup = ""
    opik_paths = ""
    opik_drain = ""
    if tracing_enabled:
        opik_setup = (
            "if [ \"${PINCHBENCH_RESET_GATEWAY_OPIK_STATE:-true}\" = \"true\" ]; then\n"
            f"  rm -f {OPENCLAW_CONTAINER_STATE_DIR}/opik-state/opik_tracer_state.json "
            f"{OPENCLAW_CONTAINER_STATE_DIR}/opik-state/opik_tracer_state.lock\n"
            "fi\n"
            "[ -L /home/node/.openclaw/state ] || rm -rf /home/node/.openclaw/state\n"
            f"ln -sfn {PINCHBENCH_OPIK_CONTAINER_STATE_DIR} /home/node/.openclaw/state\n"
        )
        opik_paths = (
            f" {OPENCLAW_CONTAINER_STATE_DIR}/opik-state {PINCHBENCH_OPIK_CONTAINER_STATE_DIR}"
        )
        opik_drain = (
            "for _ in $(seq 1 \"${PINCHBENCH_OPIK_DRAIN_SECONDS:-60}\"); do\n"
            "  if ps -eo args | grep -F "
            "'/opt/openclaw-plugins/openclaw-opik-tracer/tracer/openclaw_opik_tracer.py' "
            "| grep -v grep >/dev/null; then\n"
            "    sleep 1\n"
            "  else\n"
            "    break\n"
            "  fi\n"
            "done\n"
            "echo \"pinchbench opik drain complete\"\n"
        )
    return (
        "set -euo pipefail\n"
        "umask 000\n"
        "install -m 0755 /root/.local/bin/uv /tmp/uv\n"
        f"mkdir -p /home/node/.openclaw {OPENCLAW_CONTAINER_STATE_DIR}/agents{opik_dirs}\n"
        f"{opik_setup}"
        f"ln -sfn {OPENCLAW_CONTAINER_STATE_DIR}/agents /home/node/.openclaw/agents\n"
        "touch /runner/benchmark.log\n"
        f"chown {shlex.quote(run_as_user)}:{shlex.quote(run_as_user)} /runner/benchmark.log\n"
        f"for path in /results /home/node/.cache/uv {OPENCLAW_CONTAINER_STATE_DIR}/agents "
        f"{opik_paths} "
        f"{OPENCLAW_CONTAINER_WORKSPACE_DIR} {OPENCLAW_CONTAINER_STATE_DIR}/identity "
        f"{OPENCLAW_CONTAINER_CONFIG_PATH} "
        f"{OPENCLAW_CONTAINER_CONFIG_PATH}.bak; do\n"
        "  [ -e \"$path\" ] || continue\n"
        f"  chown -R {shlex.quote(run_as_user)}:{shlex.quote(run_as_user)} \"$path\"\n"
        "done\n"
        "status=0\n"
        f"su {shlex.quote(run_as_user)} -s /bin/bash -c {shlex.quote(worker_cmd)} || status=$?\n"
        "echo \"pinchbench worker command exit status=$status\"\n"
        f"{opik_drain}"
        "[ \"$status\" -eq 0 ]"
    )


def run_worker_phase(
    *,
    phase_name: str,
    worker_specs: list[dict[str, object]],
    build_command: Callable[[dict[str, object]], str],
    config: dict[str, str],
    pinchbench_dir: Path,
    openrouter_key: str,
    openai_api_key: str,
    run_dir: Path,
) -> None:
    run_as_user = config["OPENCLAW_CONTAINER_USER"]
    tracing_enabled = trace_to_opik_enabled(config.get("TRACE_TO_OPIK"))
    procs: list[subprocess.Popen] = []
    for spec in worker_specs:
        log_file = Path(spec["worker_dir"]) / f"{phase_name}.log"
        worker_uv_cache_dir = Path(config["PINCHBENCH_UV_CACHE_DIR"]) / f"worker-{spec['instance_index']}"
        worker_uv_cache_dir.mkdir(parents=True, exist_ok=True)
        docker_cmd = build_worker_docker_command(
            image=config["PINCHBENCH_DOCKER_IMAGE"],
            instance_index=int(spec["instance_index"]),
            container_prefix=config["CONTAINER_NAME_PREFIX"],
            token=str(spec["token"]),
            openrouter_key=openrouter_key,
            openai_api_key=openai_api_key,
            model_provider=config.get("PINCHBENCH_MODEL_PROVIDER", "auto"),
            uv_cache_dir=worker_uv_cache_dir,
            pinchbench_dir=pinchbench_dir,
            worker_dir=Path(spec["worker_dir"]),
            config_dir=Path(spec["config_dir"]),
            workspace_dir=Path(spec["workspace_dir"]),
            plugin_cache_dir=(
                Path(config["PLUGIN_CACHE_DIR"])
                if config.get("PLUGIN_CACHE_DIR")
                else None
            ),
            results_dir=Path(spec["results_dir"]),
            opik_state_dir=(
                Path(spec["opik_state_dir"])
                if spec.get("opik_state_dir") is not None
                else None
            ),
            container_env={
                key: config.get(key, "")
                for key in (
                    "NPM_CONFIG_REGISTRY",
                    "PIP_INDEX_URL",
                    "PIP_EXTRA_INDEX_URL",
                    "PIP_TRUSTED_HOST",
                    "TRACE_TO_OPIK",
                )
            },
            bench_cmd=wrap_worker_command(
                run_as_user=run_as_user,
                bench_cmd=build_command(spec),
                tracing_enabled=tracing_enabled,
            ),
        )
        with open(log_file, "w", encoding="utf-8") as log_fh:
            proc = subprocess.Popen(docker_cmd, stdout=log_fh, stderr=log_fh)
        procs.append(proc)

    failed = False
    for proc in procs:
        if proc.wait() != 0:
            failed = True

    if failed:
        sys.exit(f"One or more {phase_name} workers failed. Check logs under {run_dir}.")


def parse_args(config: dict[str, str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run-parallel-workers.py",
        description="Run PinchBench in parallel across Dockerized OpenClaw instances.",
    )
    parser.add_argument(
        "--instances",
        type=int,
        default=int(config.get("COUNT", "4")),
        metavar="N",
        help="Number of OpenClaw instances/workers (default: COUNT from config/fleet.env)",
    )
    parser.add_argument(
        "--suite",
        default="automated-only",
        help=(
            "Task suite: all, automated-only, core, a manifest category, "
            "category+category, or comma-separated task IDs"
        ),
    )
    parser.add_argument(
        "--core",
        action="store_true",
        help="Run the manifest-defined core task subset.",
    )
    parser.add_argument(
        "-n",
        "--iterations",
        type=positive_int,
        default=1,
        metavar="N",
        help="Number of benchmark iterations to run (default: 1).",
    )
    return parser.parse_args()


def validate(args: argparse.Namespace, config: dict[str, str]) -> None:
    if args.instances < 1:
        sys.exit("Error: --instances must be at least 1.")

    if not config.get("MODEL"):
        sys.exit(
            "Error: missing model. Set MODEL in config.env/config.local.env "
            "or Tasks/Pinchbench/config/pinchbench.env "
            "or Agents/Openclaw/config/fleet.env."
        )

    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit("Error: docker is not available or daemon is not reachable.")

    if not ENV_FILE.exists():
        sys.exit(f"Error: missing {ENV_FILE}. Run ./Agents/Openclaw/scripts/setup.sh first.")

    if not config.get("CONTAINER_NAME_PREFIX"):
        sys.exit(
            f"Error: missing CONTAINER_NAME_PREFIX in {ENV_FILE}. "
            "Run ./Agents/Openclaw/scripts/setup.sh first."
        )

    fleet_count = sum(
        1 for line in ENV_FILE.read_text(encoding="utf-8").splitlines()
        if line.startswith("TOKEN_")
    )
    if fleet_count < args.instances:
        sys.exit(
            f"Error: requested {args.instances} workers, "
            f"but only {fleet_count} OpenClaw instances are configured."
        )


def apply_patch(pinchbench_dir: Path, patch_file: Path) -> None:
    git = ["git", "-C", str(pinchbench_dir)]
    # Accept both a clean checkout and one where the target patch is already present.
    forward_check = subprocess.run(
        git + ["apply", "--check", str(patch_file)],
        capture_output=True,
        check=False,
    )
    if forward_check.returncode == 0:
        subprocess.run(git + ["apply", str(patch_file)], check=True)
        return
    reverse_check = subprocess.run(
        git + ["apply", "-R", "--check", str(patch_file)],
        capture_output=True,
        check=False,
    )
    if reverse_check.returncode == 0:
        return  # patch already applied
    sys.exit(f"Error: failed to apply patch {patch_file} to {pinchbench_dir}")


def ensure_clean_checkout(pinchbench_dir: Path) -> None:
    git = ["git", "-C", str(pinchbench_dir)]
    status = subprocess.run(
        git + ["status", "--short"],
        check=True,
        capture_output=True,
        text=True,
    )
    if status.stdout.strip():
        sys.exit(
            "Error: existing PinchBench checkout has local changes. "
            f"Use a clean checkout for PINCHBENCH_DIR or move it aside first: {pinchbench_dir}"
        )


def prepare_checkout(pinchbench_dir: Path, repo_url: str, ref: str, patches_dir: Path) -> None:
    """Clone or update the patched PinchBench checkout before worker containers start."""
    created_checkout = False
    if not (pinchbench_dir / ".git").exists():
        pinchbench_dir.parent.mkdir(parents=True, exist_ok=True)
        if pinchbench_dir.exists():
            sys.exit(f"Error: {pinchbench_dir} exists but is not a pinchbench git checkout.")
        subprocess.run(["git", "init", str(pinchbench_dir)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(pinchbench_dir), "remote", "add", "origin", repo_url],
            check=True, capture_output=True,
        )
        created_checkout = True

    git = ["git", "-C", str(pinchbench_dir)]
    if not created_checkout:
        # Refuse to mutate a user-supplied checkout if it has local edits or untracked files.
        ensure_clean_checkout(pinchbench_dir)

    subprocess.run(git + ["fetch", "--depth", "1", "origin", ref], check=True)
    subprocess.run(git + ["checkout", "--detach", "FETCH_HEAD"], check=True, capture_output=True)

    if patches_dir.is_dir():
        for patch_file in sorted(patches_dir.glob("*.patch")):
            apply_patch(pinchbench_dir, patch_file)


def _manifest_item(line: str) -> str:
    return line.split("#", 1)[0].strip()


def _load_task_manifest(tasks_root: Path) -> dict[str, object] | None:
    manifest_path = tasks_root / "manifest.yaml"
    if not manifest_path.exists():
        return None

    run_first: list[str] = []
    core: list[str] = []
    categories: dict[str, list[str]] = {}
    section = ""
    category = ""

    for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue

        if not line.startswith(" "):
            key = line.strip().rstrip(":")
            section = key if key in {"run_first", "core", "categories"} else ""
            category = ""
            continue

        if section in {"run_first", "core"}:
            item = line.strip()
            if item.startswith("- "):
                task_id = _manifest_item(item[2:])
                if task_id:
                    if section == "run_first":
                        run_first.append(task_id)
                    else:
                        core.append(task_id)
            continue

        if section == "categories":
            stripped = line.strip()
            if line.startswith("  ") and not line.startswith("    ") and stripped.endswith(":"):
                category = stripped[:-1]
                categories.setdefault(category, [])
                continue
            if category and stripped.startswith("- "):
                task_id = _manifest_item(stripped[2:])
                if task_id:
                    categories[category].append(task_id)

    existing = {
        path.stem
        for path in tasks_root.glob("task_*.md")
        if path.stem != "task_XX_name"
    }
    category_by_task: dict[str, str] = {}
    ordered: list[str] = []
    seen: set[str] = set()

    for task_id in run_first:
        if task_id in existing and task_id not in seen:
            ordered.append(task_id)
            seen.add(task_id)

    for category_name, task_ids in categories.items():
        for task_id in task_ids:
            category_by_task[task_id] = category_name
            if task_id in existing and task_id not in seen:
                ordered.append(task_id)
                seen.add(task_id)

    return {
        "ordered": ordered,
        "core": [task_id for task_id in core if task_id in existing],
        "categories": categories,
        "category_by_task": category_by_task,
    }


def _task_grading_type(task_path: Path) -> str:
    in_frontmatter = False
    for line in task_path.read_text(encoding="utf-8").splitlines():
        if line.strip() == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter and line.strip().startswith("grading_type:"):
            return line.split(":", 1)[1].strip()
    return "automated"


def expand_suite(pinchbench_dir: Path, suite: str) -> list[str]:
    """Expand a suite name or comma-separated task list into sorted task IDs."""
    tasks_root = pinchbench_dir / "tasks"
    manifest = _load_task_manifest(tasks_root)

    if suite == "all":
        if manifest:
            return list(manifest["ordered"])
        return sorted(p.stem for p in tasks_root.glob("task_*.md"))

    if suite == "core" and manifest:
        return list(manifest["core"])

    if suite == "automated-only":
        if manifest:
            return [
                task_id
                for task_id in manifest["ordered"]
                if _task_grading_type(tasks_root / f"{task_id}.md") == "automated"
            ]
        selected = []
        for path in sorted(tasks_root.glob("task_*.md")):
            if _task_grading_type(path) == "automated":
                selected.append(path.stem)
        return selected

    if manifest:
        categories = manifest["categories"]
        category_by_task = manifest["category_by_task"]
        requested_categories = [part.strip() for part in suite.split("+") if part.strip()]
        if requested_categories and all(category in categories for category in requested_categories):
            requested = set(requested_categories)
            return [
                task_id
                for task_id in manifest["ordered"]
                if category_by_task.get(task_id) in requested
            ]

    return [part.strip() for part in suite.split(",") if part.strip()]


def shard_tasks(task_ids: list[str], n: int) -> list[str]:
    """Distribute tasks round-robin across n workers; returns one comma-separated suite per worker."""
    buckets: list[list[str]] = [[] for _ in range(n)]
    for idx, task_id in enumerate(task_ids):
        buckets[idx % n].append(task_id)
    return [",".join(bucket) for bucket in buckets]


def token_for(idx: int) -> str:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"TOKEN_{idx}="):
            return line.partition("=")[2]
    return ""


def compute_efficiency(tasks: list[dict]) -> dict:
    total_input = total_output = total_tokens = total_requests = 0
    total_cost = total_time = total_score = 0.0
    tasks_with_usage = 0
    per_task = []

    for entry in tasks:
        usage = entry.get("usage") or {}
        grading = entry.get("grading") or {}
        score = float(grading.get("mean") or 0.0)
        inp = int(usage.get("input_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        tot = int(usage.get("total_tokens") or 0)
        cost = float(usage.get("cost_usd") or 0.0)
        reqs = int(usage.get("request_count") or 0)
        total_input += inp
        total_output += out
        total_tokens += tot
        total_cost += cost
        total_requests += reqs
        total_time += float(entry.get("execution_time") or 0.0)
        total_score += score
        if tot > 0:
            tasks_with_usage += 1
        per_task.append({
            "task_id": entry.get("task_id"),
            "score": round(score, 4),
            "total_tokens": tot,
            "cost_usd": round(cost, 6),
            "tokens_per_score_point": round(tot / score, 1) if score > 0 else None,
        })

    n = len(tasks)
    return {
        "total_tokens": total_tokens,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_cost_usd": round(total_cost, 6),
        "total_requests": total_requests,
        "total_execution_time_seconds": round(total_time, 2),
        "tasks_with_usage_data": tasks_with_usage,
        "tokens_per_task": round(total_tokens / n, 1) if n else 0,
        "cost_per_task_usd": round(total_cost / n, 6) if n else 0,
        "score_per_1k_tokens": round(total_score / (total_tokens / 1000), 6) if total_tokens else None,
        "score_per_dollar": round(total_score / total_cost, 4) if total_cost else None,
        "per_task": per_task,
    }


def merge_results(
    merged_json: Path,
    suite: str,
    worker_suites: list[str],
    partial_dirs: list[Path],
) -> None:
    partials = []
    for worker_dir in partial_dirs:
        json_files = sorted(worker_dir.glob("*.json"))
        if json_files:
            # Each worker writes its own result bundle; use the newest JSON in case retries produced multiples.
            partials.append(json.loads(json_files[-1].read_text(encoding="utf-8")))

    if not partials:
        sys.exit("No partial JSON results found.")

    all_tasks: list[dict] = []
    for partial in partials:
        all_tasks.extend(partial.get("tasks", []))
    all_tasks.sort(key=lambda t: t.get("task_id", ""))

    merged = {
        "model": partials[0].get("model"),
        "benchmark_version": partials[0].get("benchmark_version"),
        "run_id": partials[0].get("run_id"),
        "timestamp": partials[0].get("timestamp"),
        "suite": suite,
        "worker_suites": [
            {"worker": idx + 1, "suite": s}
            for idx, s in enumerate(worker_suites)
            if s
        ],
        "runs_per_task": partials[0].get("runs_per_task"),
        "parallel_workers": len(partials),
        "tasks": all_tasks,
        "efficiency": compute_efficiency(all_tasks),
    }
    merged_json.write_text(json.dumps(merged, indent=2), encoding="utf-8")


def find_task_transcript_path(run_dir: Path, task_id: str) -> Path | None:
    matches = sorted(run_dir.glob(f"worker-*/results/*_transcripts/{task_id}.jsonl"))
    return matches[0] if matches else None


def transcript_has_web_search_disabled(transcript_path: Path) -> bool:
    marker = "web_search is disabled or no provider is available"
    try:
        return marker in transcript_path.read_text(encoding="utf-8")
    except OSError:
        return False


def normalize_task_results_for_summary(merged: dict, run_dir: Path | None = None) -> dict:
    tasks = merged.get("tasks", [])
    for task in tasks:
        status = str(task.get("status", "")).strip().lower()
        if status not in {"timeout", "timed_out"}:
            continue

        transcript_path = task.get("transcript_path")
        transcript = Path(transcript_path) if transcript_path else None
        if transcript is None and run_dir is not None:
            transcript = find_task_transcript_path(run_dir, str(task.get("task_id", "")))

        if transcript and transcript_has_web_search_disabled(transcript):
            task["original_status"] = task.get("status")
            task["status"] = "skipped_web_search_disabled"
            task["skip_reason"] = "web_search_disabled"
            task["transcript_path"] = str(transcript)

    return merged


def validate_iteration_completion(merged: dict, expected_task_ids: list[str]) -> dict:
    """Ensure all expected tasks reached a terminal state before next iteration."""
    tasks = merged.get("tasks", [])
    expected_set = set(expected_task_ids)
    actual_ids = {str(task.get("task_id", "")) for task in tasks}
    missing_ids = sorted(expected_set - actual_ids)

    terminal_ok = True
    non_terminal_ids: list[str] = []
    terminal_statuses = {
        "success",
        "failed",
        "error",
        "timeout",
        "timed_out",
        "cancelled",
        "canceled",
        "skipped",
        "skipped_web_search_disabled",
        "completed",
    }
    non_terminal_statuses = {
        "running",
        "in_progress",
        "pending",
        "queued",
        "started",
    }

    for task in tasks:
        task_id = str(task.get("task_id", ""))
        status = str(task.get("status", "")).strip().lower()
        has_error = bool(task.get("error"))
        # Treat explicit non-terminal states as incomplete; otherwise accept
        # known terminal states and payload-level errors as terminal.
        is_terminal = (
            has_error
            or status in terminal_statuses
            or (status and status not in non_terminal_statuses)
        )
        if not is_terminal:
            terminal_ok = False
            non_terminal_ids.append(task_id)

    return {
        "ok": not missing_ids and terminal_ok,
        "expected_count": len(expected_task_ids),
        "actual_count": len(tasks),
        "missing_task_ids": missing_ids,
        "non_terminal_task_ids": sorted(non_terminal_ids),
    }


def summarize_iteration(
    *,
    iteration: int,
    merged: dict,
    run_dir: Path,
    started_at: datetime,
    completion: dict,
) -> dict:
    tasks = merged.get("tasks", [])
    failed_tasks = []
    skipped_web_search_disabled = []
    failure_stage_counts: dict[str, int] = {}
    duration_values: list[float] = []
    for task in tasks:
        status = str(task.get("status", "")).lower()
        error = task.get("error")
        duration_values.append(float(task.get("execution_time") or 0.0))
        if status == "skipped_web_search_disabled":
            skipped_web_search_disabled.append(task)
        elif status in {"failed", "error", "timeout", "timed_out"} or error:
            failed_tasks.append(task)
            msg = (str(error or "") + " " + str(task.get("notes") or "")).lower()
            stage = "task"
            if status in {"timeout", "timed_out"}:
                stage = "timeout"
            elif "sandbox" in msg:
                stage = "sandbox"
            elif "agent" in msg or "transcript" in msg or "unknown agent id" in msg:
                stage = "agent"
            elif "judge" in msg:
                stage = "judge"
            elif "timeout" in msg:
                stage = "timeout"
            failure_stage_counts[stage] = failure_stage_counts.get(stage, 0) + 1

    total = len(tasks)
    runtime_failed = len(failed_tasks)
    runtime_skipped = len(skipped_web_search_disabled)
    runtime_succeeded = total - runtime_failed - runtime_skipped
    finished_at = datetime.now()  # noqa: DTZ005 - preserve naive persisted timestamps
    return {
        "iteration": iteration,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "task_count": total,
        "success_count": runtime_succeeded,
        "failure_count": runtime_failed,
        "success_rate": round(runtime_succeeded / total, 4) if total else 0.0,
        "wall_time_seconds": round((finished_at - started_at).total_seconds(), 3),
        "avg_duration_seconds": round(sum(duration_values) / total, 3) if total else 0.0,
        "failure_stage_counts": failure_stage_counts,
        "skipped_web_search_disabled_count": runtime_skipped,
        "skipped_web_search_disabled_task_ids": [
            str(task.get("task_id", "")) for task in skipped_web_search_disabled
        ],
        "expected_task_count": completion["expected_count"],
        "actual_task_count": completion["actual_count"],
        "missing_task_ids": completion["missing_task_ids"],
        "non_terminal_task_ids": completion["non_terminal_task_ids"],
        "completion_ok": completion["ok"],
        "results_json": str(run_dir / "parallel-merged.json"),
    }


def render_iterations_markdown(summaries: list[dict]) -> str:
    lines = [
        "# PinchBench Iteration Summary",
        "",
        f"- Iterations: `{len(summaries)}`",
        "",
        "| Iteration | Task Count | Success | Failure | Success Rate | Wall Time (s) | Avg Duration (s) | Failure Stage Counts | Skipped (web_search disabled) | Completion OK | Non-terminal |",
        "|---:|---:|---:|---:|---:|---:|---:|---|---:|---|---:|",
    ]
    for item in summaries:
        lines.append(
            f"| {item['iteration']} | {item['task_count']} | {item['success_count']} | "
            f"{item['failure_count']} | {item['success_rate']:.2%} | "
            f"{item['wall_time_seconds']:.3f} | {item['avg_duration_seconds']:.3f} | "
            f"`{item['failure_stage_counts']}` | {item['skipped_web_search_disabled_count']} | "
            f"{item['completion_ok']} | {len(item['non_terminal_task_ids'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_iterations_summary_files(run_root_dir: Path, iteration_summaries: list[dict]) -> tuple[Path, Path]:
    iterations_summary_json = run_root_dir / "iterations-summary.json"
    iterations_summary_md = run_root_dir / "iterations-summary.md"
    iterations_summary_json.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(),  # noqa: DTZ005
                "iterations": iteration_summaries,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    iterations_summary_md.write_text(render_iterations_markdown(iteration_summaries), encoding="utf-8")
    return iterations_summary_json, iterations_summary_md


def worker_image_probe_command(image: str, *, tracing_enabled: bool) -> list[str]:
    expected_mode = "true" if tracing_enabled else "false"
    checks = (
        "command -v uv >/dev/null 2>&1 && uv --version >/dev/null 2>&1"
        f" && test \"$(cat {PINCHBENCH_TRACE_MODE_FILE} 2>/dev/null)\" = {expected_mode}"
    )
    if tracing_enabled:
        checks += (
            " && test -x /opt/opik-venv/bin/python"
            " && /opt/opik-venv/bin/python -c 'import opik, uuid6'"
            f" && test -r {OPIK_TRACER_CONTAINER_DIR}/dist/index.js"
            f" && test -r {OPIK_TRACER_CONTAINER_DIR}/tracer/openclaw_opik_tracer.py"
        )
    else:
        checks += (
            " && test ! -e /opt/opik-venv"
            f" && test ! -e {OPIK_TRACER_CONTAINER_DIR}"
        )
    return [
        "docker", "run", "--rm",
        "--entrypoint", "/bin/bash",
        image,
        "-lc",
        checks,
    ]


def worker_image_build_command(config: dict[str, str], *, tracing_enabled: bool) -> list[str]:
    build_cmd = [
        "docker", "build", "-f", str(BENCH_DIR / "Dockerfile"),
        "-t", config["PINCHBENCH_DOCKER_IMAGE"],
        "--build-arg", f"TRACE_TO_OPIK={'true' if tracing_enabled else 'false'}",
        "--build-arg",
        f"OPENCLAW_BASE_IMAGE={'openclaw:local-opik' if tracing_enabled else 'openclaw:local'}",
    ]
    for key in ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST"):
        value = config.get(key, "")
        if value:
            build_cmd.extend(["--build-arg", f"{key}={value}"])
    build_cmd.append(str(REPO_ROOT))
    return build_cmd


def main() -> None:
    config = load_runner_config()
    args = parse_args(config)
    validate(args, config)
    tracing_enabled = trace_to_opik_enabled(config.get("TRACE_TO_OPIK"))

    pinchbench_dir = Path(config["PINCHBENCH_DIR"])
    patches_dir = BENCH_DIR / "patches"
    repo_url = config["PINCHBENCH_REPO_URL"]
    config_base = Path(config["CONFIG_BASE"])
    workspace_base = Path(config["WORKSPACE_BASE"])
    Path(config["PINCHBENCH_UV_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)

    if not tracing_enabled:
        validate_trace_off_gateway_configs(config_base, args.instances)

    prepare_checkout(pinchbench_dir, repo_url, config["PINCHBENCH_REF"], patches_dir)

    image_missing = subprocess.run(
        ["docker", "image", "inspect", config["PINCHBENCH_DOCKER_IMAGE"]],
        capture_output=True,
        check=False,
    ).returncode != 0
    force_build = config_bool(config.get("PINCHBENCH_FORCE_BUILD"), False)
    image_unusable = False
    if not image_missing and not force_build:
        image_unusable = subprocess.run(
            worker_image_probe_command(
                config["PINCHBENCH_DOCKER_IMAGE"],
                tracing_enabled=tracing_enabled,
            ),
            capture_output=True,
            check=False,
        ).returncode != 0

    if force_build or image_missing or image_unusable:
        subprocess.run(
            worker_image_build_command(config, tracing_enabled=tracing_enabled),
            check=True,
        )

    output_dir = Path(config["PINCHBENCH_OUTPUT_DIR"])
    output_dir.mkdir(parents=True, exist_ok=True)
    run_root_dir = output_dir / datetime.now().strftime(  # noqa: DTZ005
        "%Y%m%d-%H%M%S"
    )
    run_root_dir.mkdir()

    suite_name = "core" if args.core else args.suite
    task_ids = expand_suite(pinchbench_dir, suite_name)
    if not task_ids:
        sys.exit(f"Error: no tasks selected for suite '{suite_name}'.")

    worker_suites = shard_tasks(task_ids, args.instances)

    print("Worker task allocation:")
    for i, suite_chunk in enumerate(worker_suites, start=1):
        count = len([t for t in suite_chunk.split(",") if t]) if suite_chunk else 0
        print(f"  worker {i}: {count} task(s)")

    # BASE_URL/API_KEY come from the shared config (config.env / config.local.env
    # / fleet.env), with os.environ already applied as the override layer above.
    api_key = config["API_KEY"]
    base_url = normalize_base_url(config["BASE_URL"])
    openai_api_key = api_key
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", openai_api_key)
    model = config["MODEL"]
    model_provider = config.get("PINCHBENCH_MODEL_PROVIDER", "auto")
    judge = config.get("JUDGE_MODEL", "")
    upload_enabled = config_bool(config.get("PINCHBENCH_UPLOAD"), False)

    iteration_summaries: list[dict] = []
    had_failures = False

    for iteration in range(1, args.iterations + 1):
        iteration_started_at = datetime.now()  # noqa: DTZ005
        run_dir = run_root_dir / f"iteration-{iteration:03d}"
        run_dir.mkdir(parents=True, exist_ok=True)

        partial_dirs: list[Path] = []
        worker_specs: list[dict[str, object]] = []

        for i, suite_chunk in enumerate(worker_suites, start=1):
            if not suite_chunk:
                continue

            token = token_for(i)
            if not token:
                sys.exit(f"Error: missing TOKEN_{i} in {ENV_FILE}")

            worker_dir = run_dir / f"worker-{i}"
            config_dir = config_base / str(i)
            workspace_dir = workspace_base / str(i)
            results_dir = worker_dir / "results"
            home_dir = worker_dir / "home"
            opik_state_dir = worker_dir / "opik-state" if tracing_enabled else None
            worker_dir.mkdir(parents=True, exist_ok=True)
            results_dir.mkdir(parents=True, exist_ok=True)
            home_dir.mkdir(parents=True, exist_ok=True)
            if opik_state_dir is not None:
                opik_state_dir.mkdir(parents=True, exist_ok=True)

            if not config_dir.is_dir() or not workspace_dir.is_dir():
                sys.exit(
                    f"Error: missing config/workspace dirs for instance {i}: "
                    f"{config_dir} / {workspace_dir}"
                )

            worker_specs.append(
                {
                    "instance_index": i,
                    "suite_chunk": suite_chunk,
                    "token": token,
                    "worker_dir": worker_dir,
                    "config_dir": config_dir,
                    "workspace_dir": workspace_dir,
                    "results_dir": results_dir,
                    "opik_state_dir": opik_state_dir,
                }
            )
            partial_dirs.append(results_dir)

        print(f"[iteration {iteration}/{args.iterations}] start: {run_dir}")
        run_worker_phase(
            phase_name="prepare",
            worker_specs=worker_specs,
            build_command=lambda spec: build_benchmark_command(
                model=model,
                model_provider=model_provider,
                base_url=base_url,
                api_key=api_key,
                suite_chunk=str(spec["suite_chunk"]),
                judge=judge,
                upload_enabled=upload_enabled,
                prepare_agent_only=True,
                require_prepared_agent=False,
                timeout_multiplier=config.get("PINCHBENCH_TIMEOUT_MULTIPLIER", "1.0"),
            ),
            config=config,
            pinchbench_dir=pinchbench_dir,
            openrouter_key=openrouter_key,
            openai_api_key=openai_api_key,
            run_dir=run_dir,
        )

        run_worker_phase(
            phase_name="run",
            worker_specs=worker_specs,
            build_command=lambda spec: build_benchmark_command(
                model=model,
                model_provider=model_provider,
                base_url=base_url,
                api_key=api_key,
                suite_chunk=str(spec["suite_chunk"]),
                judge=judge,
                upload_enabled=upload_enabled,
                prepare_agent_only=False,
                require_prepared_agent=True,
                timeout_multiplier=config.get("PINCHBENCH_TIMEOUT_MULTIPLIER", "1.0"),
            ),
            config=config,
            pinchbench_dir=pinchbench_dir,
            openrouter_key=openrouter_key,
            openai_api_key=openai_api_key,
            run_dir=run_dir,
        )

        merged_json = run_dir / "parallel-merged.json"
        merge_results(merged_json, suite_name, worker_suites, partial_dirs)
        merged_payload = json.loads(merged_json.read_text(encoding="utf-8"))
        merged_payload = normalize_task_results_for_summary(merged_payload, run_dir=run_dir)
        merged_json.write_text(json.dumps(merged_payload, indent=2), encoding="utf-8")
        completion = validate_iteration_completion(merged_payload, task_ids)
        iteration_summary = summarize_iteration(
            iteration=iteration,
            merged=merged_payload,
            run_dir=run_dir,
            started_at=iteration_started_at,
            completion=completion,
        )
        iteration_summaries.append(iteration_summary)
        if not completion["ok"]:
            print(
                f"[iteration {iteration}/{args.iterations}] incomplete: "
                f"expected={completion['expected_count']} actual={completion['actual_count']} "
                f"missing={len(completion['missing_task_ids'])} "
                f"non_terminal={len(completion['non_terminal_task_ids'])}"
            )
            if completion["missing_task_ids"]:
                print(f"  missing task ids: {','.join(completion['missing_task_ids'])}")
            if completion["non_terminal_task_ids"]:
                print(f"  non-terminal task ids: {','.join(completion['non_terminal_task_ids'])}")
            write_iterations_summary_files(run_root_dir, iteration_summaries)
            sys.exit(1)

        print(
            f"[iteration {iteration}/{args.iterations}] complete: "
            f"success={iteration_summary['success_count']} "
            f"failure={iteration_summary['failure_count']} "
            f"skipped_web_search_disabled={iteration_summary['skipped_web_search_disabled_count']} "
            f"success_rate={iteration_summary['success_rate']:.2%} "
            f"wall_time={iteration_summary['wall_time_seconds']:.3f}s "
            f"avg_duration={iteration_summary['avg_duration_seconds']:.3f}s "
            f"failure_stages={iteration_summary['failure_stage_counts']} "
            f"completion_ok={iteration_summary['completion_ok']} "
            f"non_terminal={len(iteration_summary['non_terminal_task_ids'])}"
        )
        if iteration_summary["failure_count"] > 0:
            had_failures = True

    iterations_summary_json, iterations_summary_md = write_iterations_summary_files(
        run_root_dir,
        iteration_summaries,
    )

    print("Docker PinchBench run complete.")
    print(f"  Logs/results root: {run_root_dir}")
    print(f"  Iteration summary JSON: {iterations_summary_json}")
    print(f"  Iteration summary MD:   {iterations_summary_md}")
    if had_failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
