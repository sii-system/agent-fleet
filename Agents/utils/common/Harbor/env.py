import argparse
import json
import math
import os
from collections.abc import Callable


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Harbor environment JSON")
    parser.add_argument(
        "command",
        choices=("llm-kwargs", "model-info", "opencode-config"),
    )
    return parser.parse_args()


def main() -> None:
    builders: dict[str, Callable[[], dict[str, object]]] = {
        "llm-kwargs": build_llm_kwargs,
        "model-info": build_model_info,
        "opencode-config": build_opencode_config,
    }
    payload = builders[parse_args().command]()
    print(json.dumps(payload, separators=(",", ":")))


if __name__ == "__main__":
    main()
