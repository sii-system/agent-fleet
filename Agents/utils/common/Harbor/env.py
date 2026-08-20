import argparse
import json
import math
import os
import sys
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse


def parse_finite_float(name: str, value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def build_llm_kwargs() -> dict[str, object]:
    payload: dict[str, object] = {
        "api_key": os.environ.get("HARBOR_ANTHROPIC_AUTH_TOKEN", ""),
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


def model_request_headers() -> dict[str, str]:
    raw = os.environ.get("HARBOR_LLM_KWARGS", "")
    if not raw:
        return {}
    llm_kwargs = json.loads(raw)
    if not isinstance(llm_kwargs, dict):
        raise TypeError("HARBOR_LLM_KWARGS must be a JSON object")
    headers = llm_kwargs.get("extra_headers", {})
    if not isinstance(headers, dict) or not all(
        isinstance(name, str) and isinstance(value, str)
        for name, value in headers.items()
    ):
        raise TypeError("HARBOR_LLM_KWARGS.extra_headers must be a string map")
    return headers


def build_opencode_config() -> dict[str, object]:
    base_url = os.environ.get("HARBOR_ANTHROPIC_BASE_URL", "").rstrip("/") + "/v1"
    api_key = os.environ.get("HARBOR_ANTHROPIC_AUTH_TOKEN", "")
    provider, separator, model = os.environ.get("HARBOR_MODEL", "").partition("/")
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
    if request_headers := model_request_headers():
        configured_headers = options.setdefault("headers", {})
        if not isinstance(configured_headers, dict):
            raise TypeError("OpenCode provider options.headers must be an object")
        configured_headers.update(request_headers)

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


def _pi_base_url_from_env() -> str:
    base_url = (
        os.environ.get("HARBOR_ANTHROPIC_BASE_URL", "").strip()
        or os.environ.get("BASE_URL", "").strip()
    ).rstrip("/")
    if not base_url:
        raise ValueError("BASE_URL must not be empty")
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(
            f"BASE_URL must be an absolute URL (got {base_url!r}); "
            "set BASE_URL to the gateway's OpenAI-compatible /v1 root, "
            "e.g. https://host:port/v1"
        )
    if parsed.path in ("", "/"):
        base_url += "/v1"
    elif not base_url.endswith("/v1"):
        raise ValueError(
            f"BASE_URL must point at an OpenAI-compatible /v1 API root "
            f"(got {base_url!r}); set BASE_URL to e.g. https://host:port/v1"
        )
    return base_url


def _pi_provider_from_env(base_url: str) -> str:
    provider = os.environ.get("PI_PROVIDER", "").strip()
    if provider:
        return provider
    # No placeholder provider name: derive the custom gateway provider from
    # the BASE_URL host when PI_PROVIDER is not set explicitly.
    host = urlparse(base_url).hostname or ""
    if not host:
        raise ValueError("BASE_URL must be an absolute URL")
    return host.lower()


def build_pi_models_config() -> dict[str, object]:
    base_url = _pi_base_url_from_env()
    provider = _pi_provider_from_env(base_url)
    model = os.environ.get("HARBOR_MODEL", "").strip()
    if "/" in model:
        model_provider, model = model.split("/", 1)
        if model_provider != provider:
            raise ValueError(
                f"Pi model provider must be {provider!r}, got {model_provider!r}"
            )
    if not provider or not model:
        raise ValueError("PI_PROVIDER and HARBOR_MODEL must not be empty")
    max_tokens_override = ""
    if os.environ.get("ROLLOUT") == "1":
        # Rollout workers budget per-request tokens via RL_/HARBOR_MAX_NEW_TOKENS;
        # env.sh clears HARBOR_MAX_TOKENS in rollout mode.
        max_tokens_override = (
            os.environ.get("HARBOR_MAX_TOKENS", "").strip()
            or os.environ.get("HARBOR_MAX_NEW_TOKENS", "").strip()
            or os.environ.get("RL_MAX_NEW_TOKENS", "").strip()
            or ""
        )
    # Benchmark runs keep the pi default unless the user pinned a value;
    # env.sh always exports HARBOR_MAX_NEW_TOKENS, so it must not leak into
    # the benchmark default.
    max_tokens_raw = max_tokens_override or os.environ.get(
        "HARBOR_MAX_TOKENS", ""
    ).strip() or "32768"
    if not max_tokens_raw.isdigit() or int(max_tokens_raw) <= 0:
        raise ValueError(
            "HARBOR_MAX_TOKENS/HARBOR_MAX_NEW_TOKENS must be a positive integer"
        )
    max_tokens = int(max_tokens_raw)

    raw_context_window = os.environ.get("PI_CONTEXT_WINDOW", "").strip() or "204800"
    if not raw_context_window.isdigit() or int(raw_context_window) <= 0:
        raise ValueError("PI_CONTEXT_WINDOW must be a positive integer")
    context_window = int(raw_context_window)

    return {
        "providers": {
            provider: {
                "baseUrl": base_url,
                "api": "openai-completions",
                "apiKey": "$AGENT_FLEET_API_KEY",
                "compat": {
                    "sendSessionAffinityHeaders": True,
                    "sessionAffinityFormat": "openai",
                    "supportsDeveloperRole": False,
                    "supportsReasoningEffort": False,
                    "supportsUsageInStreaming": True,
                    "maxTokensField": "max_tokens",
                    "thinkingFormat": "zai",
                },
                "models": [
                    {
                        "id": model,
                        "name": "Agent Fleet Harbor",
                        "reasoning": True,
                        "input": ["text"],
                        "contextWindow": context_window,
                        "maxTokens": max_tokens,
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                        },
                    }
                ],
            }
        }
    }


def build_pi_settings_config() -> dict[str, object]:
    base_url = _pi_base_url_from_env()
    provider = _pi_provider_from_env(base_url)
    model = os.environ.get("HARBOR_MODEL", "").strip()
    if "/" in model:
        _, model = model.split("/", 1)
    if not provider or not model:
        raise ValueError("PI_PROVIDER and HARBOR_MODEL must not be empty")
    return {
        "defaultProvider": provider,
        "defaultModel": model,
        "defaultThinkingLevel": os.environ.get("PI_THINKING_LEVEL", "high"),
        "enableInstallTelemetry": False,
    }


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


def build_portable_tar(source_path: Path, output_path: Path) -> None:
    """Rewrite an xz tar as gzip so task images only need ubiquitous gzip."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    os.close(fd)
    try:
        with (
            tarfile.open(source_path) as source,
            tarfile.open(temporary, "w:gz") as output,
        ):
            for member in source:
                payload = source.extractfile(member) if member.isfile() else None
                output.addfile(member, payload)
        os.replace(temporary, output_path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def url_is_reachable(url: str) -> None:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            raise SystemExit(0 if response.status < 400 else 1)
    except Exception:  # noqa: BLE001 - any failed probe is not reachable.
        raise SystemExit(1) from None


def manifest_url_ready(url: str, required_line: str = "") -> None:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            content = response.read(1024 * 64).decode("utf-8", "replace")
        lines = set(content.splitlines())
        ready = "cache_schema=3" in lines
        if required_line:
            ready = ready and required_line in lines
        raise SystemExit(0 if ready else 1)
    except Exception:  # noqa: BLE001 - any failed probe is not ready.
        raise SystemExit(1) from None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Harbor environment helpers")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in (
        "llm-kwargs",
        "model-info",
        "opencode-config",
        "pi-models-config",
        "pi-settings-config",
        "has-model-request-headers",
    ):
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
    portable_tar_parser = subparsers.add_parser("portable-tar")
    portable_tar_parser.add_argument("source", type=Path)
    portable_tar_parser.add_argument("output", type=Path)
    url_parser = subparsers.add_parser("url-reachable")
    url_parser.add_argument("url")
    manifest_parser = subparsers.add_parser("manifest-url-ready")
    manifest_parser.add_argument("url")
    manifest_parser.add_argument("required_line", nargs="?", default="")
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
    if args.command == "portable-tar":
        build_portable_tar(args.source, args.output)
        return
    if args.command == "url-reachable":
        url_is_reachable(args.url)
        return
    if args.command == "manifest-url-ready":
        manifest_url_ready(args.url, args.required_line)
        return
    if args.command == "has-model-request-headers":
        print("1" if model_request_headers() else "0")
        return

    builders: dict[str, Callable[[], dict[str, object]]] = {
        "llm-kwargs": build_llm_kwargs,
        "model-info": build_model_info,
        "opencode-config": build_opencode_config,
        "pi-models-config": build_pi_models_config,
        "pi-settings-config": build_pi_settings_config,
    }
    payload = builders[args.command]()
    print(json.dumps(payload, separators=(",", ":")))


if __name__ == "__main__":
    main()
