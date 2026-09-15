#!/usr/bin/env python3
"""Check the nightly model routes before starting expensive Harbor trials."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


class PreflightError(Exception):
    """An actionable error that is safe to put in Actions logs."""


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
                    f"Model preflight {route}: HTTP {status}; check "
                    "HARBOR_NIGHTLY_MODEL (or LLM_REVIEW_MODEL), gateway access, "
                    "and LLM_REVIEW_API_KEY before rerunning"
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=("claude-code", "opencode"), default="claude-code")
    args = parser.parse_args(argv)
    try:
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
