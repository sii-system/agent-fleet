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
from collections.abc import Callable
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
# Reserve context for the fixed 32K output budget, prompt, and tool turns.
MAX_MODEL_INPUT_BYTES = 120_000
MIN_FINDING_SIMILARITY = 0.8
MAX_EQUIVALENT_LINE_DISTANCE = 5
SIMILARITY_FILLER_TOKENS = {
    "a",
    "an",
    "are",
    "be",
    "been",
    "being",
    "is",
    "the",
    "was",
    "were",
}
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
INLINE_ROUTING_INSTRUCTION = (
    "Report only defects that can be tied to an added RIGHT-side line shown "
    "in the input. Use the exact changed path and added line. Prefer no "
    "finding over an unanchorable finding."
)
SUMMARY_ROUTING_INSTRUCTION = (
    "This explicit routing instruction overrides the prompt's default "
    "added-RIGHT-line restriction. Report concrete defects caused by the "
    "change even when the best evidence is on contextual unchanged lines or "
    "a related path. Use the exact relevant path and an integer line when "
    "available; set line to null only when no precise line exists. These "
    "findings will be published in the review summary."
)
SUMMARY_FINDING_PATTERN = re.compile(
    r"^- \*\*(?P<severity>P[0-3]): (?P<title>.+)\*\* "
    r"\(`(?P<location>.+)`\) — (?P<scenario>.+?) "
    r"Suggested remediation: (?P<remediation>.+?)"
    r"(?: Flagged by: (?P<lenses>.+))?$"
)


class PiReviewError(RuntimeError):
    """pi subprocess failed and the review could not be completed."""


class PiResponseFormatError(PiReviewError):
    """pi completed but its final response was not one JSON object."""

    def __init__(self, message: str, *, tool_calls: int = 0) -> None:
        super().__init__(message)
        self.tool_calls = tool_calls


class GitHubClient(_review.GitHubClient):
    def list_review_comments(self, number: int) -> list[dict[str, Any]]:
        return self._list_pages(f"/pulls/{number}/comments")


def _text_similarity(left: str, right: str) -> float:
    left_tokens = set(re.findall(r"[a-z0-9]+", left.casefold()))
    left_tokens -= SIMILARITY_FILLER_TOKENS
    right_tokens = set(re.findall(r"[a-z0-9]+", right.casefold()))
    right_tokens -= SIMILARITY_FILLER_TOKENS
    union = left_tokens | right_tokens
    return len(left_tokens & right_tokens) / len(union) if union else 0.0


def _same_finding(left: _review.Finding, right: _review.Finding) -> bool:
    if left.line is None or right.line is None:
        anchors_match = left.line == right.line
    else:
        anchors_match = abs(left.line - right.line) <= MAX_EQUIVALENT_LINE_DISTANCE
    return (
        left.path == right.path
        and anchors_match
        and _text_similarity(left.title, right.title)
        >= MIN_FINDING_SIMILARITY
        and _text_similarity(left.failure_scenario, right.failure_scenario)
        >= MIN_FINDING_SIMILARITY
    )


def _limit_model_input(value: str) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_MODEL_INPUT_BYTES:
        return value, False
    return encoded[:MAX_MODEL_INPUT_BYTES].decode("utf-8", errors="ignore"), True


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
            if _same_finding(existing, finding):
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


def _reconcile_recovered_findings(
    previous: list[_review.Finding],
    current: list[_review.Finding],
) -> tuple[list[_review.Finding], list[_review.Finding], list[_review.Finding]]:
    recovered = merge_lens_findings(previous)
    new_findings: list[_review.Finding] = []
    severity_upgrades: list[_review.Finding] = []
    for finding in current:
        for index, published in enumerate(recovered):
            if not _same_finding(published, finding):
                continue
            merged = merge_lens_findings([published, finding])[0]
            if (
                _review.SEVERITY_ORDER[merged.severity]
                < _review.SEVERITY_ORDER[published.severity]
            ):
                severity_upgrades.append(merged)
            recovered[index] = merged
            break
        else:
            new_findings.append(finding)
    return recovered, new_findings, merge_lens_findings(severity_upgrades)


def _shared_routing_available() -> bool:
    return callable(getattr(_review, "parse_findings", None)) and callable(
        getattr(_review, "route_findings", None)
    )


def _completed_lens_marker(review_id: str, lens: str) -> str:
    return f"<!-- {review_id}-completed-lens:{lens} -->"


def _published_comments_marker(review_id: str, count: int) -> str:
    return f"<!-- {review_id}-published-comments:{count} -->"


def _matching_partial_reviews(
    reviews: list[dict[str, Any]],
    head_sha: str,
    review_id: str,
    base_sha: str | None,
) -> list[dict[str, Any]]:
    partial_marker = _review.review_marker(
        head_sha,
        f"{review_id}-partial",
        base_sha,
    )
    matching: list[dict[str, Any]] = []
    for review in reviews:
        body = review.get("body") or ""
        if (
            (review.get("user") or {}).get("login") == "github-actions[bot]"
            and partial_marker in body
        ):
            matching.append(review)
    return matching


def _completed_lenses_from_bodies(
    bodies: list[str],
    review_id: str,
) -> set[str]:
    return {
        lens
        for lens in LENS_INSTRUCTIONS
        if any(_completed_lens_marker(review_id, lens) in body for body in bodies)
    }


def _published_comments_from_bodies(
    bodies: list[str],
    review_id: str,
) -> int:
    pattern = re.compile(
        rf"<!-- {re.escape(review_id)}-published-comments:(\d+) -->"
    )
    published = sum(
        int(match.group(1))
        for body in bodies
        if (match := pattern.search(body)) is not None
    )
    return min(_review.MAX_COMMENTS, published)


def _published_findings_from_comments(
    comments: list[dict[str, Any]],
    review_ids: set[int],
) -> list[_review.Finding]:
    findings: list[_review.Finding] = []
    for comment in comments:
        if (
            comment.get("pull_request_review_id") not in review_ids
            or (comment.get("user") or {}).get("login")
            != "github-actions[bot]"
        ):
            continue
        header, separator, remainder = (comment.get("body") or "").partition(
            "\n\n"
        )
        scenario, scenario_separator, _rest = remainder.partition("\n\n")
        title = re.fullmatch(r"\*\*(P[0-3]): (.+)\*\*", header)
        path = comment.get("path")
        line = comment.get("line")
        if (
            not separator
            or not scenario_separator
            or title is None
            or not isinstance(path, str)
            or type(line) is not int
        ):
            continue
        findings.append(
            _review.Finding(
                title.group(1),
                path,
                line,
                title.group(2),
                scenario,
                "published previously",
            )
        )
    return findings


def _published_summary_findings_from_bodies(
    bodies: list[str],
) -> list[_review.Finding]:
    findings: list[_review.Finding] = []
    supports_lenses = "lenses" in getattr(
        _review.Finding,
        "__dataclass_fields__",
        {},
    )
    for body in bodies:
        in_findings_section = False
        for line in body.splitlines():
            if line in {"## Summary findings", "## Recovered findings"}:
                in_findings_section = True
                continue
            if line.startswith("## "):
                in_findings_section = False
                continue
            if not in_findings_section:
                continue
            match = SUMMARY_FINDING_PATTERN.fullmatch(line)
            if match is None:
                continue
            location = match.group("location").replace("@\u200b", "@")
            path, line_separator, raw_line = location.rpartition(":")
            if line_separator and raw_line.isdigit():
                line_number: int | None = int(raw_line)
            else:
                path = location
                line_number = None
            raw_lenses = match.group("lenses")
            lenses = (
                tuple(item.strip() for item in raw_lenses.split(" + "))
                if raw_lenses
                else ()
            )
            if any(lens not in LENS_INSTRUCTIONS for lens in lenses):
                continue
            values: dict[str, Any] = {
                "severity": match.group("severity"),
                "path": path,
                "line": line_number,
                "title": match.group("title").replace("@\u200b", "@"),
                "failure_scenario": match.group("scenario").replace(
                    "@\u200b", "@"
                ),
                "remediation": match.group("remediation").replace(
                    "@\u200b", "@"
                ),
            }
            if supports_lenses:
                values["lenses"] = lenses
            findings.append(_review.Finding(**values))
    return merge_lens_findings(findings)


def _recovered_findings_summary(findings: list[_review.Finding]) -> str:
    lines = ["", "## Recovered findings"]
    for finding in findings[: _review.MAX_COMMENTS]:
        location = f" (`{_review._neutralize_mentions(finding.path)}"
        if finding.line is not None:
            location += f":{finding.line}"
        location += "`)"
        rendered = (
            f"- **{finding.severity}: "
            f"{_review._neutralize_mentions(finding.title)}**{location} — "
            f"{_review._neutralize_mentions(finding.failure_scenario)} "
            "Suggested remediation: "
            f"{_review._neutralize_mentions(finding.remediation)}"
        )
        lenses = getattr(finding, "lenses", ())
        if lenses:
            rendered += f" Flagged by: {' + '.join(lenses)}"
        lines.append(rendered)
    omitted = len(findings) - _review.MAX_COMMENTS
    if omitted > 0:
        lines.append(f"- {omitted} additional recovered finding(s) omitted.")
    return "\n".join(lines)


def _review_deadline() -> float:
    deadline_epoch = os.environ.get("PI_REVIEW_DEADLINE_EPOCH")
    if deadline_epoch is None:
        return time.monotonic() + PI_REVIEW_BUDGET_SECONDS
    try:
        remaining = float(deadline_epoch) - time.time()
    except ValueError as exc:
        raise PiReviewError("PI_REVIEW_DEADLINE_EPOCH must be numeric") from exc
    return time.monotonic() + max(
        0.0,
        min(float(PI_REVIEW_BUDGET_SECONDS), remaining),
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
        retry_malformed: bool = False,
        response_validator: Callable[[dict[str, Any]], None] | None = None,
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
            ]

            format_error: PiResponseFormatError | None = None
            prior_tool_calls = 0
            attempts = 2 if retry_malformed else 1
            for attempt in range(attempts):
                remaining = max(0.0, deadline - time.monotonic())
                if attempt and remaining == 0:
                    assert format_error is not None
                    raise format_error
                try:
                    completed = subprocess.run(
                        command,
                        cwd=self.repository_root,
                        env=minimal_environment(runtime_dir, self.api_key),
                        input=diff_chunk,
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
                    if response_validator is not None:
                        try:
                            response_validator(payload)
                        except _review.ModelResponseError as exc:
                            raise PiResponseFormatError(
                                str(exc),
                                tool_calls=payload["_pi_tool_calls"],
                            ) from exc
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
        deadline = _review_deadline()
        pull = github.get_pull(pull_number)
        head_sha = pull["head"]["sha"]
        if expected_head_sha is not None and head_sha != expected_head_sha:
            return "stale"
        if (
            expected_base_sha is not None
            and pull["base"]["sha"] != expected_base_sha
        ):
            return "stale"
        reviews = github.list_reviews(pull_number)
        if _review.has_existing_review(
            reviews,
            head_sha,
            review_id,
            expected_base_sha,
        ):
            return "duplicate"
        partial_reviews = _matching_partial_reviews(
            reviews,
            head_sha,
            review_id,
            expected_base_sha,
        )
        partial_review_bodies = [
            str(review.get("body") or "") for review in partial_reviews
        ]
        partial_review_ids = {
            review["id"]
            for review in partial_reviews
            if type(review.get("id")) is int
        }
        previously_completed_lenses = _completed_lenses_from_bodies(
            partial_review_bodies,
            review_id,
        )
        previously_published_comments = _published_comments_from_bodies(
            partial_review_bodies,
            review_id,
        )
        previously_published_inline_findings = _published_findings_from_comments(
            github.list_review_comments(pull_number)
            if partial_review_ids
            else [],
            partial_review_ids,
        )
        previously_published_summary_findings = (
            _published_summary_findings_from_bodies(partial_review_bodies)
        )
        previously_published_findings = merge_lens_findings(
            previously_published_inline_findings
            + previously_published_summary_findings
        )
        pending_lenses = {
            lens: instruction
            for lens, instruction in LENS_INSTRUCTIONS.items()
            if lens not in previously_completed_lenses
        }
        if not pending_lenses:
            return "duplicate"

        files, skipped = _review.collect_files(github.list_files(pull_number))
        by_path = {item.path: item for item in files}
        chunks, truncated = _review.build_chunks(
            files,
            max_chunk_chars=_review.MAX_TOTAL_CHARS,
        )
        whole_diff = "".join(chunks)
        model_input = _review.build_model_input(pull, whole_diff)
        model_input, input_truncated = _limit_model_input(model_input)
        truncated = truncated or input_truncated
        shared_routing = _shared_routing_available()
        routing_instruction = (
            SUMMARY_ROUTING_INSTRUCTION
            if shared_routing
            else INLINE_ROUTING_INSTRUCTION
        )

        def validate_lens_response(payload: dict[str, Any]) -> None:
            if shared_routing:
                _review.parse_findings(payload)
            else:
                _review.validate_findings(
                    payload,
                    by_path,
                    limit=sys.maxsize,
                )

        with ThreadPoolExecutor(max_workers=len(pending_lenses)) as executor:
            futures = {}
            for lens, instruction in pending_lenses.items():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PiReviewError(
                        f"pi review deadline exhausted before {lens} lens"
                    )
                futures[lens] = executor.submit(
                    pi_client.review,
                    f"{prompt.rstrip()}\n\n{routing_instruction}\n\n{instruction}",
                    model_input,
                    timeout=min(float(PI_LENS_TIMEOUT_SECONDS), remaining),
                    retry_malformed=True,
                    response_validator=validate_lens_response,
                )

        findings: list[_review.Finding] = []
        rejected = 0
        incomplete_lenses: list[str] = []
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
                    incomplete_lenses.append(lens)
                if shared_routing:
                    lens_findings, lens_rejected = _review.parse_findings(
                        payload
                    )
                else:
                    lens_findings, lens_rejected = _review.validate_findings(
                        payload,
                        by_path,
                        limit=sys.maxsize,
                    )
            except (PiReviewError, _review.ModelResponseError):
                failed_lenses.append(lens)
                continue
            findings.extend(
                _attribute_lens(finding, lens)
                for finding in lens_findings
            )
            rejected += lens_rejected

        if len(failed_lenses) == len(futures):
            raise PiReviewError("all review lenses failed")

        partial = bool(incomplete_lenses or failed_lenses)
        completed_lenses = previously_completed_lenses | (
            set(futures) - set(incomplete_lenses) - set(failed_lenses)
        )
        findings = merge_lens_findings(findings)
        (
            previously_published_findings,
            findings,
            recovered_updates,
        ) = _reconcile_recovered_findings(
            previously_published_findings,
            findings,
        )
        reported_findings = findings
        summary_findings: list[_review.Finding] = []
        recovered_summary_findings: list[_review.Finding] = []
        remaining_inline_comments = max(
            0,
            _review.MAX_COMMENTS - previously_published_comments,
        )
        if shared_routing:
            inline_findings, summary_findings = _review.route_findings(
                findings,
                by_path,
            )
            summary_findings = merge_lens_findings(
                previously_published_summary_findings
                + recovered_updates
                + inline_findings[remaining_inline_comments:]
                + summary_findings
            )
            inline_findings = inline_findings[:remaining_inline_comments]
        else:
            inline_findings = findings[:remaining_inline_comments]
            overflow_findings = findings[remaining_inline_comments:]
            if partial_reviews:
                recovered_summary_findings = merge_lens_findings(
                    recovered_updates + overflow_findings
                )
            else:
                rejected += len(overflow_findings)
                reported_findings = inline_findings
        reported_findings = merge_lens_findings(
            previously_published_findings + reported_findings
        )
        current = github.get_pull(pull_number)
        if current["head"]["sha"] != head_sha or (
            expected_base_sha is not None
            and current["base"]["sha"] != expected_base_sha
        ):
            return "stale"

        summary_options: dict[str, Any] = {
            "review_id": (
                f"{review_id}-partial" if partial else review_id
            ),
            "base_sha": expected_base_sha,
        }
        if shared_routing:
            summary_options.update(
                summary_findings=summary_findings,
                changed_paths=frozenset(by_path),
            )
        summary = _review.build_summary(
            head_sha,
            reported_findings,
            rejected,
            skipped,
            truncated,
            **summary_options,
        )
        if recovered_summary_findings:
            summary += _recovered_findings_summary(recovered_summary_findings)
        reported_tool_calls = dict.fromkeys(
            previously_completed_lenses,
            "previously completed",
        )
        reported_tool_calls.update(tool_calls_by_lens)
        tool_call_counts = ", ".join(
            f"{lens}={reported_tool_calls.get(lens, 'unavailable')}"
            for lens in LENS_INSTRUCTIONS
        )
        summary += f"\n\nTool calls by lens: {tool_call_counts}"
        if partial:
            markers = "\n".join(
                _completed_lens_marker(review_id, lens)
                for lens in LENS_INSTRUCTIONS
                if lens in completed_lenses
            )
            summary += (
                f"\n\n{markers}\n"
                f"{_published_comments_marker(review_id, len(inline_findings))}"
            )
            summary = summary.replace(
                "Coverage: Complete",
                "Coverage: Partial",
                1,
            )
        if incomplete_lenses:
            summary += (
                f"\n\n- {len(incomplete_lenses)} review lens(es) returned an empty "
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
    github = GitHubClient(repository, require_env("GITHUB_TOKEN"))
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
