#!/usr/bin/env python3
"""PR review powered by pi agent with trusted codebase context."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# -- shared review components from the existing Python reviewer ----------
_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))
import llm_pr_review as _review  # noqa: E402

# -- pi integration helpers from the control-plane prompt translator -----
_PROJECT_ROOT = _SCRIPTS_DIR.parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))
from scripts.pi_prompt import (  # noqa: E402
    PROVIDER,
    PromptFailure,
    final_assistant_message,
    message_text,
    minimal_environment,
    models_config,
    normalized_base_url,
    parse_jsonl,
    provider_error,
)

PI_REVIEW_ID = "pi-pr-review"
PI_TIMEOUT_SECONDS = 900  # 15 min — agent tool calls take longer than raw API


class PiReviewError(RuntimeError):
    """pi subprocess failed and the review could not be completed."""


def _chat_url_to_base(url: str) -> str:
    """Convert a chat-completions endpoint URL to a pi-compatible base URL."""
    parsed = urlparse(url)
    path = re.sub(r"/chat/completions/?$", "", parsed.path)
    base = f"{parsed.scheme}://{parsed.netloc}{path}"
    normalized_base = normalized_base_url(base)
    if path != parsed.path:
        return base
    return normalized_base


def _extract_json(text: str) -> dict[str, Any]:
    """Parse the final assistant text as a JSON object.

    Pi's ``--mode json`` returns a JSONL event stream, but the final model
    message can still put its JSON in a final fenced block.
    """
    content = text.strip()
    fenced = re.search(
        r"(?:^|\n)```(?:json)?[ \t]*\n(?P<body>.*?)\n```[ \t]*\Z",
        content,
        flags=re.DOTALL,
    )
    if fenced:
        content = fenced.group("body").strip()
    decoder = json.JSONDecoder()
    try:
        value, end = decoder.raw_decode(content)
    except json.JSONDecodeError as exc:
        raise PiReviewError("pi response is not valid JSON") from exc
    if content[end:].strip() or not isinstance(value, dict):
        raise PiReviewError("pi response must contain one JSON object")
    return value


def _validate_pi_stream(raw_stdout: str) -> dict[str, Any]:
    """Parse pi's JSONL stdout and extract the final assistant message.

    Returns the parsed JSON payload (the ``findings`` dict) on success.
    Raises :exc:`PiReviewError` on lifecycle or provider failures.
    """
    if not raw_stdout.strip():
        raise PiReviewError("pi produced no output")

    try:
        events = parse_jsonl(raw_stdout)
    except PromptFailure as exc:
        raise PiReviewError(str(exc)) from exc

    # -- lifecycle checks (same discipline as pi_prompt.py) --------------
    session_ids = [
        str(e["id"])
        for e in events
        if e.get("type") == "session" and e.get("id")
    ]
    if len(session_ids) != 1:
        raise PiReviewError("pi session lifecycle was not observed exactly once")

    agent_start = sum(e.get("type") == "agent_start" for e in events)
    agent_end = sum(e.get("type") == "agent_end" for e in events)
    if agent_start < 1 or agent_start != agent_end:
        raise PiReviewError("pi agent lifecycle is incomplete")

    turn_start = sum(e.get("type") == "turn_start" for e in events)
    turn_end = sum(e.get("type") == "turn_end" for e in events)
    if turn_start < 1 or turn_start != turn_end:
        raise PiReviewError("pi turn lifecycle is incomplete")

    err = provider_error(events)
    if err:
        raise PiReviewError(f"pi provider request failed: {err}")

    message = final_assistant_message(events)
    if message is None:
        raise PiReviewError("pi returned no final assistant message")

    stop_reason = str(message.get("stopReason") or "")
    if not stop_reason:
        raise PiReviewError("pi final assistant message has no stop reason")
    if stop_reason != "stop":
        raise PiReviewError(
            f"pi final assistant message stopped with {stop_reason}"
        )

    text = message_text(message)
    if not text:
        return {"findings": [], "incomplete": True}
    return _extract_json(text)


class PiClient:
    """PR review client powered by the pi coding agent."""

    def __init__(
        self,
        pi_binary: str,
        base_url: str,
        api_key: str,
        model: str,
        repository_root: Path,
        provider: str = PROVIDER,
        timeout: int = PI_TIMEOUT_SECONDS,
    ) -> None:
        self.pi_binary = pi_binary
        self.base_url = _chat_url_to_base(base_url)
        self.api_key = api_key
        self.model = model
        self.repository_root = repository_root
        self.provider = provider
        self.timeout = timeout

    def review(self, system_prompt: str, diff_chunk: str) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="pi-pr-review-") as tmp:
            root = Path(tmp)
            runtime_dir = root / "pi-agent"
            runtime_dir.mkdir()

            (runtime_dir / "models.json").write_text(
                json.dumps(
                    models_config(self.base_url, self.model), indent=2
                )
                + "\n",
                encoding="utf-8",
            )

            command = [
                self.pi_binary,
                "--mode", "json",
                "--print",
                "--provider", self.provider,
                "--model", self.model,
                "--no-session",
                "--approve",
                "--system-prompt", system_prompt,
                diff_chunk,
            ]

            try:
                completed = subprocess.run(
                    command,
                    cwd=self.repository_root,
                    env=minimal_environment(runtime_dir, self.api_key),
                    stdin=subprocess.DEVNULL,
                    text=True,
                    capture_output=True,
                    timeout=self.timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise PiReviewError(
                    f"pi timed out after {self.timeout}s"
                ) from exc
            except OSError as exc:
                raise PiReviewError(f"could not launch pi: {exc}") from exc

            if completed.returncode != 0:
                detail = (completed.stderr or "").strip().splitlines()
                suffix = f": {detail[-1]}" if detail else ""
                raise PiReviewError(
                    f"pi exited with code {completed.returncode}{suffix}"
                )

            return _validate_pi_stream(completed.stdout or "")


def run_review(
    github: _review.GitHubClient,
    pi_client: PiClient,
    pull_number: int,
    prompt: str,
    review_id: str = PI_REVIEW_ID,
    *,
    expected_head_sha: str | None = None,
    expected_base_sha: str | None = None,
) -> str:
    try:
        return _review.run_review(
            github,
            pi_client,
            pull_number,
            prompt,
            review_id,
            expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha,
        )
    except _review.ModelResponseError as exc:
        raise PiReviewError(str(exc)) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-path", required=True, type=Path)
    parser.add_argument("--prompt-path", required=True, type=Path)
    parser.add_argument(
        "--pi-bin",
        default="pi",
        help="path or name of the pi binary (default: pi)",
    )
    return parser.parse_args()


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"required environment variable is missing: {name}"
        )
    return value


def main() -> int:
    args = parse_args()
    event = json.loads(args.event_path.read_text())
    repository = require_env("GITHUB_REPOSITORY")
    event_pull = event["pull_request"]
    pull_number = int(event_pull["number"])
    github = _review.GitHubClient(repository, require_env("GITHUB_TOKEN"))
    pi_client = PiClient(
        pi_binary=args.pi_bin,
        base_url=require_env("LLM_REVIEW_BASE_URL"),
        api_key=require_env("LLM_REVIEW_API_KEY"),
        model=require_env("LLM_REVIEW_MODEL"),
        repository_root=Path(require_env("GITHUB_WORKSPACE")),
    )
    prompt = args.prompt_path.read_text()
    review_id = os.environ.get("LLM_REVIEW_ID", PI_REVIEW_ID)
    try:
        result = run_review(
            github,
            pi_client,
            pull_number,
            prompt,
            review_id=review_id,
            expected_head_sha=event_pull["head"]["sha"],
            expected_base_sha=event_pull["base"]["sha"],
        )
    except PiReviewError as exc:
        print(f"pi PR review failed: {exc}", file=sys.stderr)
        return 1
    print(f"pi PR review result: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
