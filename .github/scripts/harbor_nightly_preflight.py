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


BENCHMARK_MODELS = {
    "scale-ai/swe-atlas-qna": (
        ("Atlas judge", "EVAL_MODEL", "anthropic/claude-opus-4-5-20251101"),
    ),
    "sierra-research/tau3-bench": (
        ("Tau3 user simulator", "TAU2_USER_MODEL", "gpt-5.2"),
        ("Tau3 assertion judge", "TAU2_NL_ASSERTIONS_MODEL", "gpt-5.2"),
    ),
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


def check_benchmark_dependencies(benchmark: str) -> None:
    models = BENCHMARK_MODELS.get(benchmark, ())
    if not models:
        return
    separate_url = os.environ.get("VERIFIER_BASE_URL_RAW", "").strip()
    separate_key = os.environ.get("VERIFIER_API_KEY", "")
    if bool(separate_url) != bool(separate_key):
        raise PreflightError(
            "Set both HARBOR_NIGHTLY_VERIFIER_BASE_URL and "
            "HARBOR_NIGHTLY_VERIFIER_API_KEY for a separate benchmark gateway"
        )
    base = separate_url or os.environ.get(
        "OPENAI_BASE_URL", os.environ.get("BASE_URL_RAW", "")
    )
    key = separate_key or os.environ.get(
        "OPENAI_API_KEY", os.environ.get("API_KEY", "")
    )
    if not key or "\n" in key or "\r" in key:
        raise PreflightError("Benchmark model preflight requires a valid API credential")
    root = api_root(base)
    checked = set()
    for label, variable, default in models:
        model = os.environ.get(variable, default).strip()
        if not model:
            raise PreflightError(f"Benchmark model preflight requires nonempty {variable}")
        if model not in checked:
            try:
                probe(root, model, key, "chat/completions")
            except PreflightError as exc:
                raise PreflightError(
                    f"{label} unavailable: {exc}. Keep the benchmark model and "
                    "configure HARBOR_NIGHTLY_VERIFIER_BASE_URL / "
                    "HARBOR_NIGHTLY_VERIFIER_API_KEY with access to it"
                ) from None
            checked.add(model)
        print(f"Benchmark model preflight {label}: passed")
    if separate_url and os.environ.get("GITHUB_ENV"):
        # Task manifests interpolate these host values for Atlas's EVAL_*
        # settings and Tau3's environment/verifier OPENAI_* settings. The
        # agent retains BASE_URL / API_KEY and its existing selected model.
        with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as output:
            output.write(
                f"OPENAI_BASE_URL={root}\nOPENAI_API_BASE={root}\nOPENAI_API_KEY={key}\n"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=("claude-code", "opencode"), default="claude-code")
    parser.add_argument("--benchmark", help="Check only this benchmark's judge/simulator dependencies")
    args = parser.parse_args(argv)
    try:
        if args.benchmark is not None:
            check_benchmark_dependencies(args.benchmark)
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
