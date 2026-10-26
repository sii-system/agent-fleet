#!/usr/bin/env python3
"""Check the nightly model routes before starting expensive Harbor trials."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


class PreflightError(Exception):
    """An actionable error that is safe to put in Actions logs."""


BENCHMARK_MODEL_VARS = {
    "scale-ai/swe-atlas-qna": ("EVAL_MODEL",),
    "sierra-research/tau3-bench": ("TAU2_USER_MODEL", "TAU2_NL_ASSERTIONS_MODEL"),
}


def api_root(base_url: str) -> str:
    parsed = urlsplit(base_url.strip())
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise PreflightError("Set LLM_REVIEW_BASE_URL to a valid model API URL")
    path = parsed.path.rstrip("/").removesuffix("/chat/completions")
    if not path.endswith("/v1"):
        path += "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def probe(root: str, model: str, key: str, route: str) -> None:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply OK."}],
        "max_tokens": 16,
        "stream": False,
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    if route == "chat/completions" and model.startswith("gpt-5"):
        payload["max_completion_tokens"] = payload.pop("max_tokens")
    if route == "messages":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    request = Request(
        f"{root}/{route}", data=json.dumps(payload).encode(), headers=headers
    )
    for attempt in range(3):
        try:
            with urlopen(request, timeout=20) as response:
                result = json.load(response)
            valid = isinstance(result, dict) and not result.get("error")
            if route == "messages":
                valid = valid and result.get("type") == "message"
            else:
                valid = valid and bool(result.get("choices"))
            if not valid:
                raise PreflightError(f"Model preflight {route}: invalid completion response")
            return
        except HTTPError as exc:
            status = exc.code
            exc.close()
            retryable = status in (408, 429) or 500 <= status < 600
            if not retryable or attempt == 2:
                # Never log response bodies: gateways can echo credentials.
                raise PreflightError(
                    f"Model preflight {route}: HTTP {status}; check requested "
                    "model availability, gateway access, and credentials before rerunning"
                ) from None
        except (URLError, TimeoutError, OSError):
            if attempt == 2:
                raise PreflightError(
                    f"Model preflight {route}: connection failed after 3 attempts"
                ) from None
        except (ValueError, UnicodeError):
            raise PreflightError(
                f"Model preflight {route}: response was not valid JSON"
            ) from None
        time.sleep(2 ** attempt)


def tau_user_args() -> str:
    # Suppress the task's implicit GPT-specific reasoning parameter for custom
    # aliases, while retaining parameters the caller configured explicitly.
    args = {}
    effort = os.environ.get("TAU2_USER_REASONING_EFFORT", "")
    if effort:
        args["reasoning_effort"] = effort
    try:
        temperature = os.environ.get("TAU2_USER_TEMPERATURE", "")
        if temperature:
            args["temperature"] = float(temperature)
        return json.dumps(args, allow_nan=False, separators=(",", ":"))
    except ValueError:
        raise PreflightError("TAU2_USER_TEMPERATURE must be a finite number") from None


def configure_benchmark_models(benchmark: str) -> None:
    """Forward the checked run configuration; probe only distinct overrides."""
    model_vars = BENCHMARK_MODEL_VARS.get(benchmark, ())
    if not model_vars:
        return
    base = os.environ.get("BASE_URL", os.environ.get("BASE_URL_RAW", ""))
    run_model = os.environ.get("MODEL", "").strip()
    run_key = os.environ.get("API_KEY", "")
    run_root = api_root(base)
    for name, value in (("MODEL", run_model), ("API_KEY", run_key)):
        if not value or "\n" in value or "\r" in value:
            raise PreflightError(f"Benchmark model configuration requires a valid {name}")
    separate_url = os.environ.get("VERIFIER_BASE_URL_RAW", "").strip()
    separate_key = os.environ.get("VERIFIER_API_KEY", "")
    if bool(separate_url) != bool(separate_key):
        raise PreflightError(
            "Set both HARBOR_NIGHTLY_VERIFIER_BASE_URL and "
            "HARBOR_NIGHTLY_VERIFIER_API_KEY for a separate benchmark gateway"
        )
    root, key = (api_root(separate_url), separate_key) if separate_url else (run_root, run_key)
    if not key or "\n" in key or "\r" in key:
        raise PreflightError("Benchmark model preflight requires a valid API credential")
    is_tau = benchmark == "sierra-research/tau3-bench"
    exported = {"OPENAI_BASE_URL": root, "OPENAI_API_BASE": root, "OPENAI_API_KEY": key}
    selected = []
    for variable in model_vars:
        model = os.environ.get(variable, run_model).strip()
        if is_tau and variable in os.environ:
            model = model.removeprefix("openai/")
        if not model or "\n" in model or "\r" in model:
            raise PreflightError(f"Benchmark model preflight requires a valid {variable}")
        # LiteLLM requires a provider for custom gateway aliases. Its
        # OpenAI adapter strips this prefix before sending the model ID.
        exported[variable] = f"openai/{model}" if is_tau else model
        selected.append((variable, model))
    if is_tau and "TAU2_USER_LLM_ARGS_JSON" not in os.environ:
        exported["TAU2_USER_LLM_ARGS_JSON"] = tau_user_args()

    # The first workflow preflight already checked the run's chat route.
    checked = {(run_root, run_model, run_key)}
    for variable, model in selected:
        configuration = (root, model, key)
        if configuration not in checked:
            try:
                probe(root, model, key, "chat/completions")
            except PreflightError as exc:
                raise PreflightError(f"Benchmark override {variable}: {exc}") from None
            checked.add(configuration)
    if os.environ.get("GITHUB_ENV"):
        with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as output:
            output.write("".join(f"{variable}={value}\n" for variable, value in exported.items()))
    print(f"Configured benchmark model settings for {benchmark}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=("claude-code", "opencode"), default="claude-code")
    parser.add_argument("--benchmark", help="Configure benchmark roles after the run preflight")
    args = parser.parse_args(argv)
    try:
        if args.benchmark is not None:
            configure_benchmark_models(args.benchmark)
            return 0
        model = os.environ.get("MODEL", "").strip()
        key = os.environ.get("API_KEY", "")
        if not model or not key:
            raise PreflightError("Model preflight requires nonempty MODEL and API_KEY")
        root = api_root(os.environ.get("BASE_URL_RAW", ""))
        # Pi summaries use chat completions; Claude Code also needs Messages.
        routes = ["chat/completions"]
        if args.agent == "claude-code":
            routes.append("messages")
        for route in routes:
            probe(root, model, key, route)
            print(f"Model preflight {route}: passed")
    except PreflightError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
