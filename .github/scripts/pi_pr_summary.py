#!/usr/bin/env python3
"""Generate one Pi-powered summary when a pull request is opened."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))
import llm_pr_review as _review  # noqa: E402
from pi_pr_review import PiClient, PiReviewError  # noqa: E402

MAX_DESCRIPTION_ITEMS = 6
MAX_DESCRIPTION_ITEM_CHARS = 1_000
MAX_ASSESSMENT_CHARS = 3_000
MAX_DIAGRAM_CHARS = 8_000
MAX_SUMMARY_DIFF_CHARS = _review.MAX_CHUNK_CHARS
MAX_SUMMARY_INPUT_BYTES = 90_000
MAX_FILE_INVENTORY = 100
MAX_FILE_INVENTORY_BYTES = 20_000
MARKDOWN_ESCAPE_TABLE = str.maketrans(
    {character: f"\\{character}" for character in r"\`*_{}[]()#+-.!|~:/?="}
)
MERMAID_IMAGE_NODE_RE = re.compile(r"@\{\s*img\s*:", re.IGNORECASE)
FLOWCHART_NODE_START_RE = re.compile(
    r"""
    (?P<prefix>
        ^[ \t]*(?:subgraph[ \t]+)?
        |
        (?:(?:--+[xo]|--+>|==+>|==+|-\.\->|\.\->|-\.+-|---|~~~)(?:[ \t]*\|[^|\n]*\|)?|[&;])[ \t]*
    )
    (?P<node>[A-Za-z0-9_]+)
    [ \t]*
    (?P<opening>
        \[(?![\[(/>\\])
        |
        \{(?!\{)
    )
    """,
    re.MULTILINE | re.VERBOSE,
)


class PiSummaryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Summary:
    description: tuple[str, ...]
    diagram: str | None
    assessment: str


def _required_text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise PiSummaryError(f"{field} must be text")
    text = value.strip()
    if not text or len(text) > limit:
        raise PiSummaryError(f"{field} is empty or too long")
    return text


def _validate_diagram(value: Any) -> str | None:
    if value is None or value == "":
        return None
    diagram = _required_text(value, "diagram", MAX_DIAGRAM_CHARS)
    first_line = diagram.splitlines()[0].strip()
    if first_line != "sequenceDiagram" and not first_line.startswith(
        ("flowchart ", "graph ")
    ):
        raise PiSummaryError("diagram must be a Mermaid flowchart or sequence")
    lowered = diagram.casefold()
    if any(
        token in lowered
        for token in ("```", "%%{", "click ", "href", "javascript:", "<")
    ):
        raise PiSummaryError("diagram contains unsupported content")
    if MERMAID_IMAGE_NODE_RE.search(diagram):
        raise PiSummaryError("diagram contains unsupported image node")
    if first_line.startswith(("flowchart ", "graph ")):
        diagram = _quote_flowchart_labels(diagram)
    return diagram


def _quote_flowchart_labels(diagram: str) -> str:
    def ignored_context(index: int) -> bool:
        quoted = False
        edge_text = False
        commented = False
        delimiters: list[str] = []
        closing_for = {"[": "]", "{": "}", "(": ")"}
        prefix = diagram[:index]
        for offset, character in enumerate(prefix):
            if character == "\n":
                commented = False
                if not quoted:
                    edge_text = False
                continue
            if commented:
                continue
            if (
                character == "%"
                and prefix[offset : offset + 2] == "%%"
                and not quoted
                and not edge_text
                and not delimiters
            ):
                commented = True
                continue
            if character == '"':
                quoted = not quoted
            elif not quoted:
                if edge_text:
                    if character == "|":
                        edge_text = False
                elif delimiters:
                    active = delimiters[-1]
                    if character == active:
                        delimiters.append(character)
                    elif character == closing_for[active]:
                        delimiters.pop()
                elif character in closing_for:
                    delimiters.append(character)
                elif character == "|":
                    edge_text = True
        return quoted or edge_text or commented or bool(delimiters)

    def node_end(start: int, opening: str) -> int | None:
        closing = "]" if opening == "[" else "}"
        depth = 1
        quoted = False
        for index in range(start, len(diagram)):
            character = diagram[index]
            if character == "\n" and not quoted:
                return None
            if character == '"':
                quoted = not quoted
            elif not quoted:
                if character == opening:
                    depth += 1
                elif character == closing:
                    depth -= 1
                    if depth == 0:
                        return index
        return None

    parts: list[str] = []
    cursor = 0
    search_from = 0
    while match := FLOWCHART_NODE_START_RE.search(diagram, search_from):
        if ignored_context(match.start()):
            search_from = match.start() + 1
            continue
        opening = match.group("opening")
        end = node_end(match.end(), opening)
        if end is None:
            raise PiSummaryError("diagram contains an unterminated node")
        label = diagram[match.end() : end]
        parts.append(diagram[cursor : match.end()])
        if label.startswith('"') and label.endswith('"'):
            inner_label = label[1:-1].replace('"', "#quot;")
            parts.append(f'"{inner_label}"')
        else:
            label = label.replace('"', "#quot;")
            parts.append(f'"{label}"')
        parts.append(diagram[end])
        cursor = end + 1
        search_from = cursor
    parts.append(diagram[cursor:])
    return "".join(parts)


def validate_summary(payload: dict[str, Any]) -> Summary:
    raw_description = payload.get("description")
    if not isinstance(raw_description, list) or not (
        1 <= len(raw_description) <= MAX_DESCRIPTION_ITEMS
    ):
        raise PiSummaryError("description must contain 1-6 items")
    description = tuple(
        _required_text(item, "description item", MAX_DESCRIPTION_ITEM_CHARS)
        for item in raw_description
    )
    assessment = _required_text(
        payload.get("assessment"),
        "assessment",
        MAX_ASSESSMENT_CHARS,
    )
    return Summary(description, _validate_diagram(payload.get("diagram")), assessment)


def _safe_prose(value: str) -> str:
    escaped = value.replace("@", "@\u200b").translate(MARKDOWN_ESCAPE_TABLE)
    return html.escape(escaped)


def _truncate_utf8(value: str, limit: int) -> str:
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def render_summary(title: str, summary: Summary) -> str:
    lines = [
        "<h3>PR Summary by Pi</h3>",
        "",
        _safe_prose(title),
        "",
        "<details>",
        "<summary>High-Level Assessment</summary>",
        "",
        _safe_prose(summary.assessment),
        "",
        "</details>",
        "",
        "<details>",
        "<summary>AI Description</summary>",
        "",
    ]
    lines.extend(f"- {_safe_prose(item)}" for item in summary.description)
    lines.extend(["", "</details>"])
    if summary.diagram:
        lines.extend(
            [
                "",
                "<details>",
                "<summary>Diagram</summary>",
                "",
                "```mermaid",
                summary.diagram,
                "```",
                "",
                "</details>",
            ]
        )
    return "\n".join(lines)


def build_summary_input(
    pull: dict[str, Any],
    raw_files: list[dict[str, Any]],
    diff: str,
    *,
    truncated: bool,
) -> str:
    title = str(pull.get("title") or "")[: _review.MAX_PR_METADATA_CHARS]
    description = str(pull.get("body") or "")[: _review.MAX_PR_METADATA_CHARS]
    inventory: list[str] = []
    inventory_bytes = 0
    inventory_truncated = len(raw_files) > MAX_FILE_INVENTORY
    for raw in raw_files[:MAX_FILE_INVENTORY]:
        path = raw.get("filename")
        if not isinstance(path, str):
            continue
        status = str(raw.get("status") or "changed")
        additions = int(raw.get("additions") or 0)
        deletions = int(raw.get("deletions") or 0)
        entry = f"- {path} ({status}, +{additions}/-{deletions})"
        entry_bytes = len(entry.encode("utf-8")) + bool(inventory)
        if inventory_bytes + entry_bytes > MAX_FILE_INVENTORY_BYTES:
            inventory_truncated = True
            break
        inventory.append(entry)
        inventory_bytes += entry_bytes
    inventory_text = "\n".join(inventory)
    if not inventory_text:
        inventory_text = (
            "- omitted (inventory limit)" if inventory_truncated else "- none"
        )

    diff_text = diff or "(no textual diff available)"
    effective_truncated = truncated or inventory_truncated

    def prefix() -> str:
        return (
            f"PR TITLE: {title}\n"
            f"PR DESCRIPTION: {description}\n\n"
            "CHANGED FILES:\n"
            f"{inventory_text}\n\n"
            f"DIFF TRUNCATED: {'yes' if effective_truncated else 'no'}\n\n"
            "UNTRUSTED DIFF:\n"
        )

    input_prefix = prefix()
    remaining = MAX_SUMMARY_INPUT_BYTES - len(input_prefix.encode("utf-8"))
    bounded_diff = _truncate_utf8(diff_text, max(0, remaining))
    if bounded_diff != diff_text and not effective_truncated:
        effective_truncated = True
        input_prefix = prefix()
        remaining = MAX_SUMMARY_INPUT_BYTES - len(input_prefix.encode("utf-8"))
        bounded_diff = _truncate_utf8(diff_text, max(0, remaining))
    return input_prefix + bounded_diff


def run_summary(
    github: Any,
    pi_client: Any,
    pull_number: int,
    prompt: str,
) -> str:
    pull = github.get_pull(pull_number)
    raw_files = github.list_files(pull_number)
    files, skipped = _review.collect_files(raw_files)
    chunks, truncated = _review.build_chunks(
        files,
        max_chunk_chars=MAX_SUMMARY_DIFF_CHARS,
        max_total_chars=MAX_SUMMARY_DIFF_CHARS,
    )
    model_input = build_summary_input(
        pull,
        raw_files,
        "\n".join(chunks),
        truncated=truncated or bool(skipped),
    )
    payload = pi_client.review(prompt, model_input)
    summary = validate_summary(payload)
    github.create_issue_comment(
        pull_number,
        render_summary(str(pull.get("title") or ""), summary),
    )
    return "published"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-path", required=True, type=Path)
    parser.add_argument("--prompt-path", required=True, type=Path)
    parser.add_argument("--pi-bin", default="pi")
    return parser.parse_args()


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def main() -> int:
    args = parse_args()
    event = json.loads(args.event_path.read_text(encoding="utf-8"))
    pull_number = int(event["pull_request"]["number"])
    github = _review.GitHubClient(
        require_env("GITHUB_REPOSITORY"),
        require_env("GITHUB_TOKEN"),
    )
    pi_client = PiClient(
        pi_binary=args.pi_bin,
        base_url=require_env("LLM_REVIEW_BASE_URL"),
        api_key=require_env("LLM_REVIEW_API_KEY"),
        model=require_env("LLM_REVIEW_MODEL"),
        repository_root=Path(require_env("GITHUB_WORKSPACE")),
    )
    try:
        result = run_summary(
            github,
            pi_client,
            pull_number,
            args.prompt_path.read_text(encoding="utf-8"),
        )
    except (PiReviewError, PiSummaryError) as exc:
        print(f"pi PR summary failed: {exc}", file=sys.stderr)
        return 1
    print(f"pi PR summary result: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
