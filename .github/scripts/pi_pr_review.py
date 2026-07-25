#!/usr/bin/env python3
"""PR review powered by pi agent — explores codebase context with read-only tools."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# -- shared review components from the existing Python reviewer ----------
_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))
import llm_pr_review as _review  # noqa: E402

# -- pi integration helpers from the control-plane prompt translator -----
_PROJECT_ROOT = _SCRIPTS_DIR.parents[1]
PI_PATH_GATE_EXTENSION = (
    _PROJECT_ROOT
    / "Agents"
    / "utils"
    / "common"
    / "Harbor"
    / "scripts"
    / "harbor_analyzer"
    / "pi_extensions"
    / "analyzer_path_gate.ts"
)
PI_ALLOWED_PATHS_ENV = "HARBOR_ANALYZER_ALLOWED_PATHS_JSON"
PI_GREP_LITERAL_ONLY_ENV = "HARBOR_ANALYZER_GREP_LITERAL_ONLY"
PI_MAX_TOOL_CALLS_ENV = "HARBOR_ANALYZER_MAX_TOOL_CALLS"
PI_MAX_TOTAL_TOOL_OUTPUT_ENV = (
    "HARBOR_ANALYZER_MAX_TOTAL_TOOL_OUTPUT_BYTES"
)
sys.path.insert(0, str(_PROJECT_ROOT))
from scripts.pi_prompt import (  # noqa: E402
    PROVIDER,
    PromptFailure,
    final_assistant_message,
    message_text,
    minimal_environment,
    models_config,
    parse_jsonl,
    provider_error,
)

PI_REVIEW_ID = "pi-pr-review"
PI_TIMEOUT_SECONDS = 900  # 15 min — agent tool calls take longer than raw API
WORKFLOW_TIMEOUT_SECONDS = 20 * 60
WORKFLOW_RESERVE_SECONDS = 5 * 60
PI_REVIEW_BUDGET_SECONDS = (
    WORKFLOW_TIMEOUT_SECONDS - WORKFLOW_RESERVE_SECONDS
)
PI_MAX_TOOL_CALLS = 16
PI_MAX_TOTAL_TOOL_OUTPUT_BYTES = 128 * 1024


class PiReviewError(RuntimeError):
    """pi subprocess failed and the review could not be completed."""


def _chat_url_to_base(url: str) -> str:
    """Convert a chat-completions endpoint URL to a pi-compatible base URL."""
    value = url.strip()
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
    except ValueError as exc:
        raise PiReviewError(
            "invalid LLM_REVIEW_BASE_URL for pi provider"
        ) from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise PiReviewError("invalid LLM_REVIEW_BASE_URL for pi provider")
    if parsed.params or parsed.query or parsed.fragment:
        raise PiReviewError(
            "LLM_REVIEW_BASE_URL query parameters or fragments are not "
            "supported by pi provider"
        )
    path = parsed.path
    for suffix in ("/chat/completions/", "/chat/completions"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return parsed._replace(path=path).geturl()


def _extract_json(text: str) -> dict[str, Any]:
    """Parse the final assistant text as a JSON object.

    Mirrors llm_pr_review.extract_json but tailored for pi output: pi's
    ``--mode json`` returns clean JSON, but some models still fence it.
    """
    content = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.DOTALL)
    if fenced:
        content = fenced.group(1)
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

    tool_executions = sum(
        e.get("type") == "tool_execution_start" for e in events
    )
    if tool_executions > PI_MAX_TOOL_CALLS:
        raise PiReviewError(
            "pi exceeded reviewer tool-call limit of "
            f"{PI_MAX_TOOL_CALLS}: observed {tool_executions} executions"
        )

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
    """PR review client powered by the pi coding agent in read-only mode."""

    def __init__(
        self,
        pi_binary: str,
        base_url: str,
        api_key: str,
        model: str,
        repository_root: Path,
        provider: str = PROVIDER,
        timeout: int = PI_TIMEOUT_SECONDS,
        path_gate_extension: Path = PI_PATH_GATE_EXTENSION,
    ) -> None:
        try:
            self.repository_root = repository_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PiReviewError(
                "pi repository root is unavailable"
            ) from exc
        if not self.repository_root.is_dir():
            raise PiReviewError("pi repository root is not a directory")

        try:
            self.path_gate_extension = path_gate_extension.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PiReviewError(
                "pi path-gate extension is unavailable"
            ) from exc
        if not self.path_gate_extension.is_file():
            raise PiReviewError("pi path-gate extension is not a file")

        self.pi_binary = pi_binary
        self.base_url = _chat_url_to_base(base_url)
        self.api_key = api_key
        self.model = model
        self.provider = provider
        self.timeout = timeout

    def review(
        self,
        system_prompt: str,
        diff_chunk: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        effective_timeout = (
            self.timeout
            if timeout is None
            else min(timeout, self.timeout)
        )
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

            environment = minimal_environment(runtime_dir, self.api_key)
            environment[PI_ALLOWED_PATHS_ENV] = json.dumps(
                [str(self.repository_root)]
            )
            environment[PI_GREP_LITERAL_ONLY_ENV] = "1"
            environment[PI_MAX_TOOL_CALLS_ENV] = str(PI_MAX_TOOL_CALLS)
            environment[PI_MAX_TOTAL_TOOL_OUTPUT_ENV] = str(
                PI_MAX_TOTAL_TOOL_OUTPUT_BYTES
            )

            command = [
                self.pi_binary,
                "--mode", "json",
                "--print",
                "--provider", self.provider,
                "--model", self.model,
                "--no-session",
                "--no-builtin-tools",
                "--tools", "read,grep,find,ls",
                "--extension", str(self.path_gate_extension),
                "--no-extensions",
                "--no-skills",
                "--no-prompt-templates",
                "--no-themes",
                "--no-context-files",
                "--no-approve",
                "--system-prompt", system_prompt,
                diff_chunk,
            ]

            try:
                completed = subprocess.run(
                    command,
                    cwd=self.repository_root,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=effective_timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise PiReviewError(
                    f"pi timed out after {effective_timeout:g}s"
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


# -- orchestration (mirrors llm_pr_review.run_review) --------------------


def _matches_event_revision(
    pull: dict[str, Any],
    expected_base_sha: str,
    expected_head_sha: str,
) -> bool:
    return (
        pull["base"]["sha"] == expected_base_sha
        and pull["head"]["sha"] == expected_head_sha
    )


def run_review(
    github: _review.GitHubClient,
    pi_client: PiClient,
    pull_number: int,
    prompt: str,
    review_id: str = PI_REVIEW_ID,
    *,
    expected_base_sha: str,
    expected_head_sha: str,
) -> str:
    deadline = time.monotonic() + PI_REVIEW_BUDGET_SECONDS
    pull = github.get_pull(pull_number)
    if not _matches_event_revision(
        pull, expected_base_sha, expected_head_sha
    ):
        return "stale"

    head_sha = expected_head_sha
    if _review.has_existing_review(
        github.list_reviews(pull_number),
        head_sha,
        review_id,
        base_sha=expected_base_sha,
    ):
        return "duplicate"

    files, skipped = _review.collect_files(github.list_files(pull_number))
    by_path = {item.path: item for item in files}
    chunks, truncated = _review.build_chunks(files)
    findings: list[_review.Finding] = []
    rejected = 0
    incomplete_chunks = 0
    for index, chunk in enumerate(chunks):
        remaining_chunks = len(chunks) - index
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise PiReviewError(
                "pi review deadline exhausted before chunk "
                f"{index + 1} of {len(chunks)}"
            )
        chunk_timeout = min(
            float(pi_client.timeout),
            remaining_seconds / remaining_chunks,
        )
        payload = pi_client.review(
            prompt,
            _review.build_model_input(pull, chunk),
            timeout=chunk_timeout,
        )
        if payload.get("incomplete"):
            incomplete_chunks += 1
        chunk_findings, chunk_rejected = _review.validate_findings(
            payload, by_path
        )
        findings.extend(chunk_findings)
        rejected += chunk_rejected

    # Aggregate dedup pass
    aggregate_payload = {
        "findings": [item.__dict__ for item in findings]
    }
    findings, aggregate_rejected = _review.validate_findings(
        aggregate_payload, by_path
    )
    rejected += aggregate_rejected

    summary = _review.build_summary(
        head_sha,
        findings,
        rejected,
        skipped,
        truncated,
        incomplete_chunks=incomplete_chunks,
        review_id=review_id,
        base_sha=expected_base_sha,
    )
    current = github.get_pull(pull_number)
    if not _matches_event_revision(
        current, expected_base_sha, expected_head_sha
    ):
        return "stale"
    github.create_review(pull_number, head_sha, summary, findings)
    return "published"


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
    pull_request = event["pull_request"]
    pull_number = int(pull_request["number"])
    github = _review.GitHubClient(repository, require_env("GITHUB_TOKEN"))
    prompt = args.prompt_path.read_text()
    review_id = os.environ.get("LLM_REVIEW_ID", PI_REVIEW_ID)
    try:
        pi_client = PiClient(
            pi_binary=args.pi_bin,
            base_url=require_env("LLM_REVIEW_BASE_URL"),
            api_key=require_env("LLM_REVIEW_API_KEY"),
            model=require_env("LLM_REVIEW_MODEL"),
            repository_root=Path(require_env("GITHUB_WORKSPACE")),
        )
        result = run_review(
            github,
            pi_client,
            pull_number,
            prompt,
            review_id=review_id,
            expected_base_sha=pull_request["base"]["sha"],
            expected_head_sha=pull_request["head"]["sha"],
        )
    except PiReviewError as exc:
        print(f"pi PR review failed: {exc}", file=sys.stderr)
        return 1
    print(f"pi PR review result: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
