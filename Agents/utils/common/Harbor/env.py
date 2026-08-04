import argparse
import json
import math
import os
import sys
import tarfile
import urllib.request
from collections.abc import Callable
from pathlib import Path


def parse_finite_float(name: str, value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def build_llm_kwargs() -> dict[str, object]:
    payload: dict[str, object] = {
        "api_key": os.environ.get("TB_ANTHROPIC_AUTH_TOKEN", ""),
        "temperature": parse_finite_float(
            "HARBOR_TEMPERATURE",
            os.environ.get("HARBOR_TEMPERATURE") or "1.0",
        ),
    }
    if top_p := os.environ.get("HARBOR_TOP_P"):
        payload["top_p"] = parse_finite_float("HARBOR_TOP_P", top_p)
    return payload


def build_model_info() -> dict[str, object]:
    max_output_tokens = int(os.environ["_HARBOR_OUTPUT_TOKEN_LIMIT"])
    if max_output_tokens <= 0:
        raise ValueError("HARBOR_MAX_TOKENS must be greater than zero")
    return {
        "max_input_tokens": 204800,
        "max_output_tokens": max_output_tokens,
    }


def build_opencode_config() -> dict[str, object]:
    base_url = os.environ.get("TB_ANTHROPIC_BASE_URL", "").rstrip("/") + "/v1"
    api_key = os.environ.get("TB_ANTHROPIC_AUTH_TOKEN", "")
    provider, separator, model = os.environ.get("TB_MODEL", "").partition("/")
    if not separator:
        model = provider
    temperature = os.environ.get("HARBOR_TEMPERATURE", "")
    top_p = os.environ.get("HARBOR_TOP_P", "")
    max_tokens = os.environ.get("HARBOR_MAX_TOKENS", "")
    existing_config = os.environ.get("OPENCODE_CONFIG_CONTENT", "")

    payload = json.loads(existing_config) if existing_config else {}
    if not isinstance(payload, dict):
        raise ValueError("OPENCODE_CONFIG_CONTENT must be a JSON object")  # noqa: TRY004

    provider_config = payload.setdefault("provider", {}).setdefault(provider, {})
    options = provider_config.setdefault("options", {})
    options.setdefault("baseURL", base_url)

    if provider == "custom":
        provider_config.setdefault("npm", "@ai-sdk/openai-compatible")
        options.setdefault("apiKey", api_key)
        model_config = provider_config.setdefault("models", {}).setdefault(model, {})
        model_config.setdefault("name", model)
    elif max_tokens:
        model_config = (
            payload.setdefault("provider", {})
            .setdefault(provider, {})
            .setdefault("models", {})
            .setdefault(model, {})
        )
    else:
        model_config = None

    if max_tokens and model_config is not None:
        model_config.setdefault("limit", {})["output"] = int(max_tokens)

    agent_name = payload.get("default_agent") or "build"
    agent_config = payload.setdefault("agent", {}).setdefault(agent_name, {})
    if temperature:
        agent_config["temperature"] = parse_finite_float(
            "HARBOR_TEMPERATURE", temperature
        )
    if top_p:
        agent_config["top_p"] = parse_finite_float("HARBOR_TOP_P", top_p)
    if not agent_config:
        payload["agent"].pop(agent_name)
        if not payload["agent"]:
            payload.pop("agent")

    return payload


def generate_task_file(dataset: Path, destination: Path) -> None:
    tasks = []
    for task_dir in dataset.iterdir():
        if not task_dir.is_dir():
            continue
        instruction = task_dir / "instruction.md"
        task_yaml = task_dir / "task.yaml"
        if instruction.is_file():
            try:
                if instruction.read_text(errors="ignore").strip():
                    tasks.append(task_dir.name)
            except OSError:
                continue
        elif task_yaml.is_file():
            tasks.append(task_dir.name)
    destination.write_text("\n".join(sorted(tasks)) + ("\n" if tasks else ""))


def filter_task_file(source: Path, destination: Path, raw_tasks: str) -> None:
    requested = []
    seen = set()
    for part in raw_tasks.split(","):
        task = part.strip()
        if task and task not in seen:
            requested.append(task)
            seen.add(task)

    available = {
        line.strip()
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    missing = [task for task in requested if task not in available]
    if missing:
        print("[ERROR] unknown task(s): " + ", ".join(missing), file=sys.stderr)
        raise SystemExit(2)

    destination.write_text(
        "\n".join(requested) + ("\n" if requested else ""),
        encoding="utf-8",
    )


def tar_file_ready(path: Path) -> None:
    if not path.is_file():
        raise SystemExit(1)
    try:
        with tarfile.open(path) as archive:
            archive.getmembers()
    except (EOFError, OSError, tarfile.TarError):
        raise SystemExit(1) from None


def url_is_reachable(url: str) -> None:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            raise SystemExit(0 if response.status < 400 else 1)
    except Exception:  # noqa: BLE001 - any failed probe is not reachable.
        raise SystemExit(1) from None


def manifest_url_ready(url: str) -> None:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            content = response.read(1024 * 64).decode("utf-8", "replace")
        raise SystemExit(0 if "cache_schema=3\n" in content else 1)
    except Exception:  # noqa: BLE001 - any failed probe is not ready.
        raise SystemExit(1) from None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Harbor environment helpers")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("llm-kwargs", "model-info", "opencode-config"):
        subparsers.add_parser(command)
    generate_parser = subparsers.add_parser("generate-task-file")
    generate_parser.add_argument("dataset", type=Path)
    generate_parser.add_argument("destination", type=Path)
    filter_parser = subparsers.add_parser("filter-task-file")
    filter_parser.add_argument("source", type=Path)
    filter_parser.add_argument("destination", type=Path)
    filter_parser.add_argument("tasks")
    tar_parser = subparsers.add_parser("tar-file-ready")
    tar_parser.add_argument("path", type=Path)
    url_parser = subparsers.add_parser("url-reachable")
    url_parser.add_argument("url")
    manifest_parser = subparsers.add_parser("manifest-url-ready")
    manifest_parser.add_argument("url")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "generate-task-file":
        generate_task_file(args.dataset, args.destination)
        return
    if args.command == "filter-task-file":
        filter_task_file(args.source, args.destination, args.tasks)
        return
    if args.command == "tar-file-ready":
        tar_file_ready(args.path)
        return
    if args.command == "url-reachable":
        url_is_reachable(args.url)
        return
    if args.command == "manifest-url-ready":
        manifest_url_ready(args.url)
        return

    builders: dict[str, Callable[[], dict[str, object]]] = {
        "llm-kwargs": build_llm_kwargs,
        "model-info": build_model_info,
        "opencode-config": build_opencode_config,
    }
    payload = builders[args.command]()
    print(json.dumps(payload, separators=(",", ":")))


if __name__ == "__main__":
    main()
