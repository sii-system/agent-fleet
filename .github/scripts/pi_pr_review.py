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
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
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
WORKFLOW_TIMEOUT_SECONDS = 20 * 60
WORKFLOW_RESERVE_SECONDS = 5 * 60
PI_REVIEW_BUDGET_SECONDS = WORKFLOW_TIMEOUT_SECONDS - WORKFLOW_RESERVE_SECONDS
PI_LENS_TIMEOUT_SECONDS = 12 * 60
LENS_INSTRUCTIONS = {
    "correctness": (
        "Focus on runtime correctness, state transitions, error handling, and "
        "cross-file behavior. Use at most 16 tool calls."
    ),
    "security": (
        "Focus on trust boundaries, injection, credential exposure, permissions, "
        "and unsafe data flow. Use at most 16 tool calls."
    ),
    "tests/regression": (
        "Focus on behavioral regressions and missing tests that would let a "
        "concrete defect escape. Use at most 16 tool calls."
    ),
}


class PiReviewError(RuntimeError):
    """pi subprocess failed and the review could not be completed."""


class PiResponseFormatError(PiReviewError):
    """pi completed but its final response was not one JSON object."""

    def __init__(self, message: str, *, tool_calls: int = 0) -> None:
        super().__init__(message)
        self.tool_calls = tool_calls


def _title_similarity(left: str, right: str) -> float:
    left_tokens = set(re.findall(r"[a-z0-9]+", left.casefold()))
    right_tokens = set(re.findall(r"[a-z0-9]+", right.casefold()))
    union = left_tokens | right_tokens
    return len(left_tokens & right_tokens) / len(union) if union else 0.0


def _attribute_lens(
    finding: _review.Finding,
    lens: str,
) -> _review.Finding:
    fields = getattr(_review.Finding, "__dataclass_fields__", {})
    if "lenses" not in fields:
        return finding
    return replace(finding, lenses=(lens,))


def merge_lens_findings(
    findings: list[_review.Finding],
) -> list[_review.Finding]:
    merged: list[_review.Finding] = []
    for finding in findings:
        for index, existing in enumerate(merged):
            if (
                existing.path == finding.path
                and existing.line == finding.line
                and _title_similarity(existing.title, finding.title) >= 0.5
            ):
                winner = min(
                    (existing, finding),
                    key=lambda item: _review.SEVERITY_ORDER[item.severity],
                )
                if hasattr(winner, "lenses"):
                    lenses = tuple(
                        dict.fromkeys(existing.lenses + finding.lenses)
                    )
                    winner = replace(winner, lenses=lenses)
                merged[index] = winner
                break
        else:
            merged.append(finding)
    return sorted(
        merged,
        key=lambda item: (
            _review.SEVERITY_ORDER[item.severity],
            item.path,
            item.line is None,
            item.line or 0,
            item.title.casefold(),
        ),
    )


def _shared_routing_available() -> bool:
    return callable(getattr(_review, "parse_findings", None)) and callable(
        getattr(_review, "route_findings", None)
    )


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
        raise PiResponseFormatError("pi response is not valid JSON") from exc
    if content[end:].strip() or not isinstance(value, dict):
        raise PiResponseFormatError(
            "pi response must contain one JSON object"
        )
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
    tool_calls = sum(
        event.get("type") == "tool_execution_start" for event in events
    )
    if not text:
        return {
            "findings": [],
            "incomplete": True,
            "_pi_tool_calls": tool_calls,
        }
    try:
        payload = _extract_json(text)
    except PiResponseFormatError as exc:
        raise PiResponseFormatError(
            str(exc),
            tool_calls=tool_calls,
        ) from exc
    payload["_pi_tool_calls"] = tool_calls
    return payload


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

    def review(
        self,
        system_prompt: str,
        diff_chunk: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        effective_timeout = (
            self.timeout if timeout is None else min(self.timeout, timeout)
        )
        deadline = time.monotonic() + effective_timeout
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

            format_error: PiResponseFormatError | None = None
            prior_tool_calls = 0
            for attempt in range(2):
                remaining = max(0.0, deadline - time.monotonic())
                if attempt and remaining == 0:
                    assert format_error is not None
                    raise format_error
                try:
                    completed = subprocess.run(
                        command,
                        cwd=self.repository_root,
                        env=minimal_environment(runtime_dir, self.api_key),
                        stdin=subprocess.DEVNULL,
                        text=True,
                        capture_output=True,
                        timeout=remaining,
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise PiReviewError(
                        f"pi timed out after {remaining:g}s"
                    ) from exc
                except OSError as exc:
                    raise PiReviewError(
                        f"could not launch pi: {exc}"
                    ) from exc

                if completed.returncode != 0:
                    detail = (completed.stderr or "").strip().splitlines()
                    suffix = f": {detail[-1]}" if detail else ""
                    raise PiReviewError(
                        f"pi exited with code {completed.returncode}{suffix}"
                    )

                try:
                    payload = _validate_pi_stream(completed.stdout or "")
                    payload["_pi_tool_calls"] += prior_tool_calls
                    return payload
                except PiResponseFormatError as exc:
                    format_error = exc
                    prior_tool_calls += exc.tool_calls
            assert format_error is not None
            raise format_error


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
        deadline = time.monotonic() + PI_REVIEW_BUDGET_SECONDS
        pull = github.get_pull(pull_number)
        head_sha = pull["head"]["sha"]
        if expected_head_sha is not None and head_sha != expected_head_sha:
            return "stale"
        if (
            expected_base_sha is not None
            and pull["base"]["sha"] != expected_base_sha
        ):
            return "stale"
        if _review.has_existing_review(
            github.list_reviews(pull_number),
            head_sha,
            review_id,
            expected_base_sha,
        ):
            return "duplicate"

        files, skipped = _review.collect_files(github.list_files(pull_number))
        by_path = {item.path: item for item in files}
        chunks, truncated = _review.build_chunks(
            files,
            max_chunk_chars=_review.MAX_TOTAL_CHARS,
        )
        whole_diff = chunks[0] if chunks else ""
        model_input = _review.build_model_input(pull, whole_diff)

        with ThreadPoolExecutor(max_workers=len(LENS_INSTRUCTIONS)) as executor:
            futures = {}
            for lens, instruction in LENS_INSTRUCTIONS.items():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PiReviewError(
                        f"pi review deadline exhausted before {lens} lens"
                    )
                futures[lens] = executor.submit(
                    pi_client.review,
                    prompt.replace("{{LENS}}", instruction),
                    model_input,
                    timeout=min(float(PI_LENS_TIMEOUT_SECONDS), remaining),
                )

        findings: list[_review.Finding] = []
        rejected = 0
        incomplete_lenses = 0
        failed_lenses: list[str] = []
        tool_calls_by_lens: dict[str, int] = {}
        for lens, future in futures.items():
            try:
                payload = future.result()
                raw_tool_calls = payload.get("_pi_tool_calls", 0)
                tool_calls_by_lens[lens] = (
                    raw_tool_calls
                    if type(raw_tool_calls) is int and raw_tool_calls >= 0
                    else 0
                )
                if payload.get("incomplete"):
                    incomplete_lenses += 1
                if _shared_routing_available():
                    lens_findings, lens_rejected = _review.parse_findings(
                        payload
                    )
                else:
                    lens_findings, lens_rejected = _review.validate_findings(
                        payload,
                        by_path,
                    )
            except (PiReviewError, _review.ModelResponseError):
                failed_lenses.append(lens)
                continue
            findings.extend(
                _attribute_lens(finding, lens)
                for finding in lens_findings
            )
            rejected += lens_rejected

        if len(failed_lenses) == len(LENS_INSTRUCTIONS):
            raise PiReviewError("all review lenses failed")

        findings = merge_lens_findings(findings)
        summary_findings: list[_review.Finding] = []
        if _shared_routing_available():
            inline_findings, summary_findings = _review.route_findings(
                findings,
                by_path,
            )
        else:
            rejected += max(0, len(findings) - _review.MAX_COMMENTS)
            inline_findings = findings[: _review.MAX_COMMENTS]
        current = github.get_pull(pull_number)
        if current["head"]["sha"] != head_sha or (
            expected_base_sha is not None
            and current["base"]["sha"] != expected_base_sha
        ):
            return "stale"

        summary_options: dict[str, Any] = {
            "review_id": review_id,
            "base_sha": expected_base_sha,
        }
        if _shared_routing_available():
            summary_options.update(
                summary_findings=summary_findings,
                changed_paths=frozenset(by_path),
            )
        summary = _review.build_summary(
            head_sha,
            findings,
            rejected,
            skipped,
            truncated,
            **summary_options,
        )
        tool_call_counts = ", ".join(
            f"{lens}={tool_calls_by_lens.get(lens, 'unavailable')}"
            for lens in LENS_INSTRUCTIONS
        )
        summary += f"\n\nTool calls by lens: {tool_call_counts}"
        if incomplete_lenses or failed_lenses:
            summary = summary.replace(
                "Coverage: Complete",
                "Coverage: Partial",
                1,
            )
        if incomplete_lenses:
            summary += (
                f"\n\n- {incomplete_lenses} review lens(es) returned an empty "
                "model response and were not reviewed."
            )
        if failed_lenses:
            summary += f"\n\nFailed lenses: {', '.join(failed_lenses)}"
        github.create_review(pull_number, head_sha, summary, inline_findings)
        return "published"
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
