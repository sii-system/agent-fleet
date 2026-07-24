from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MAX_FILE_PATCH_CHARS = 60_000
GENERATED_PATHS = frozenset({"Agents/Openclaw/docker-compose.yml"})
GENERATED_SUFFIXES = (".min.js", ".min.css", ".map")
LOCKFILE_NAMES = frozenset(
    {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "uv.lock"}
)
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
FILE_HEADER_RE = re.compile(r"(?m)^FILE [^\n]*")
SUBMODULE_LINE_RE = re.compile(r"(?m)^[+-]Subproject commit [0-9a-f]+$")
MAX_CHUNK_CHARS = 50_000
MAX_TOTAL_CHARS = 200_000
MAX_COMMENTS = 20
MAX_FIELD_CHARS = 2_000
MAX_RESPONSE_TOKENS = 12_000
SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
API_VERSION = "2022-11-28"
REQUEST_TIMEOUT_SECONDS = 90
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_PR_METADATA_CHARS = 4_000
MAX_SKIPPED_PATHS_IN_SUMMARY = 50
DEFAULT_REVIEW_ID = "llm-pr-review"


@dataclass(frozen=True)
class ParsedFile:
    path: str
    review_text: str
    right_lines: frozenset[int]


@dataclass(frozen=True)
class Finding:
    severity: str
    path: str
    line: int
    title: str
    failure_scenario: str
    remediation: str


class ModelResponseError(ValueError):
    pass


def skip_reason(path: str, patch: str | None) -> str | None:
    if path in GENERATED_PATHS or path.endswith(GENERATED_SUFFIXES):
        return "generated"
    if path.rsplit("/", 1)[-1] in LOCKFILE_NAMES:
        return "lockfile"
    if patch is None:
        return "binary-or-missing"
    if patch.startswith("Subproject commit ") or SUBMODULE_LINE_RE.search(patch):
        return "submodule"
    if len(patch) > MAX_FILE_PATCH_CHARS:
        return "oversized"
    return None


def parse_patch(path: str, patch: str) -> ParsedFile:
    old_line = 0
    new_line = 0
    right_lines: set[int] = set()
    rendered = [f"FILE {path}"]

    for raw_line in patch.splitlines():
        hunk = HUNK_RE.match(raw_line)
        if hunk:
            old_line = int(hunk.group(1))
            new_line = int(hunk.group(3))
            rendered.append(raw_line)
            continue
        if raw_line.startswith("\\ No newline at end of file"):
            rendered.append(raw_line)
            continue
        if raw_line.startswith("+"):
            right_lines.add(new_line)
            rendered.append(f"+ RIGHT {new_line}: {raw_line[1:]}")
            new_line += 1
        elif raw_line.startswith("-"):
            rendered.append(f"- OLD {old_line}: {raw_line[1:]}")
            old_line += 1
        else:
            content = raw_line[1:] if raw_line.startswith(" ") else raw_line
            rendered.append(f"  OLD {old_line} RIGHT {new_line}: {content}")
            old_line += 1
            new_line += 1

    return ParsedFile(path, "\n".join(rendered), frozenset(right_lines))


def build_chunks(
    files: list[ParsedFile],
    *,
    max_chunk_chars: int = MAX_CHUNK_CHARS,
    max_total_chars: int = MAX_TOTAL_CHARS,
) -> tuple[list[str], bool]:
    source = "\n\n".join(file.review_text for file in files)
    chunks: list[str] = []
    cursor = 0
    output_chars = 0

    while cursor < len(source) and output_chars < max_total_chars:
        remaining_total = max_total_chars - output_chars
        chunk_budget = min(max_chunk_chars, remaining_total)
        prefix = ""
        if cursor:
            headers = list(FILE_HEADER_RE.finditer(source, 0, cursor))
            if headers:
                prefix = f"{headers[-1].group()}\n"
                prefix = prefix[: max(0, chunk_budget - 1)]
        payload_budget = chunk_budget - len(prefix)
        payload = source[cursor : cursor + payload_budget]
        if cursor + len(payload) < len(source):
            line_boundary = payload.rfind("\n")
            if line_boundary >= 0:
                payload = payload[: line_boundary + 1]
        chunk = prefix + payload
        chunks.append(chunk)
        cursor += len(payload)
        output_chars += len(chunk)

    return chunks, cursor < len(source)


def extract_json(content: str) -> dict[str, Any]:
    text = content.strip()
    opening, separator, fenced = text.partition("\n")
    if separator and opening in {"```", "```json"} and fenced.endswith("```"):
        text = fenced[:-3].strip()
    decoder = json.JSONDecoder()
    try:
        value, end = decoder.raw_decode(text)
    except json.JSONDecodeError as exc:
        raise ModelResponseError("model response is not valid JSON") from exc
    if text[end:].strip() or not isinstance(value, dict):
        raise ModelResponseError("model response must contain one JSON object")
    return value


def _bounded_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_FIELD_CHARS:
        return None
    return text


def _neutralize_mentions(text: str) -> str:
    return text.replace("@", "@\u200b")


def validate_findings(
    payload: dict[str, Any],
    files: dict[str, ParsedFile],
    *,
    limit: int = MAX_COMMENTS,
) -> tuple[list[Finding], int]:
    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        raise ModelResponseError("findings must be an array")

    valid: list[Finding] = []
    seen: set[tuple[str, int, str, str]] = set()
    rejected = 0
    for raw in raw_findings:
        if not isinstance(raw, dict):
            rejected += 1
            continue
        severity = raw.get("severity")
        path = _bounded_text(raw.get("path"))
        line = raw.get("line")
        title = _bounded_text(raw.get("title"))
        scenario = _bounded_text(raw.get("failure_scenario"))
        remediation = _bounded_text(raw.get("remediation"))
        parsed = files.get(path) if path else None
        if (
            severity not in SEVERITY_ORDER
            or parsed is None
            or type(line) is not int
            or line not in parsed.right_lines
            or title is None
            or scenario is None
            or remediation is None
        ):
            rejected += 1
            continue
        key = (path, line, title.casefold(), scenario.casefold())
        if key in seen:
            rejected += 1
            continue
        seen.add(key)
        valid.append(Finding(severity, path, line, title, scenario, remediation))

    valid.sort(key=lambda item: (SEVERITY_ORDER[item.severity], item.path, item.line))
    rejected += max(0, len(valid) - limit)
    return valid[:limit], rejected


def _json_request(
    request: Request,
    *,
    opener: Callable[..., Any],
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> Any:
    with opener(request, timeout=timeout) as response:
        return json.loads(response.read())


class LlmClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        opener: Callable[..., Any] = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.opener = opener
        self.sleeper = sleeper

    def review(self, system_prompt: str, diff_chunk: str) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": diff_chunk},
            ],
            "temperature": 0.1,
            "max_tokens": MAX_RESPONSE_TOKENS,
        }
        request = Request(
            self.base_url,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        for attempt in range(3):
            try:
                response = _json_request(request, opener=self.opener)
                content = response["choices"][0]["message"].get("content")
                if not isinstance(content, str):
                    raise ModelResponseError("model content must be text")
                if not content.strip():
                    return {"findings": [], "incomplete": True}
                return extract_json(content)
            except HTTPError as exc:
                if exc.code not in RETRYABLE_STATUS or attempt == 2:
                    raise
                self.sleeper(attempt + 1)
            except (KeyError, IndexError, TypeError, AttributeError) as exc:
                raise ModelResponseError(
                    "unexpected chat completion response"
                ) from exc

        raise AssertionError("retry loop exhausted")


class GitHubClient:
    def __init__(
        self,
        repository: str,
        token: str,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.api_root = f"https://api.github.com/repos/{repository}"
        self.token = token
        self.opener = opener

    def _request(self, method: str, path: str, payload: object | None = None) -> Any:
        request = Request(
            f"{self.api_root}{path}",
            data=None if payload is None else json.dumps(payload).encode(),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": API_VERSION,
                "Content-Type": "application/json",
            },
            method=method,
        )
        return _json_request(request, opener=self.opener)

    def _list_pages(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            separator = "&" if "?" in path else "?"
            batch = self._request(
                "GET",
                f"{path}{separator}{urlencode({'per_page': 100, 'page': page})}",
            )
            if not isinstance(batch, list):
                raise ValueError("GitHub list response must be an array")
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1

    def get_pull(self, number: int) -> dict[str, Any]:
        return self._request("GET", f"/pulls/{number}")

    def list_files(self, number: int) -> list[dict[str, Any]]:
        return self._list_pages(f"/pulls/{number}/files")

    def list_reviews(self, number: int) -> list[dict[str, Any]]:
        return self._list_pages(f"/pulls/{number}/reviews")

    def create_review(
        self,
        number: int,
        head_sha: str,
        body: str,
        findings: list[Finding],
    ) -> dict[str, Any]:
        comments = [
            {
                "path": item.path,
                "line": item.line,
                "side": "RIGHT",
                "body": (
                    f"**{item.severity}: {_neutralize_mentions(item.title)}**\n\n"
                    f"{_neutralize_mentions(item.failure_scenario)}\n\n"
                    "Suggested remediation: "
                    f"{_neutralize_mentions(item.remediation)}"
                ),
            }
            for item in findings
        ]
        return self._request(
            "POST",
            f"/pulls/{number}/reviews",
            {
                "commit_id": head_sha,
                "body": body,
                "event": "COMMENT",
                "comments": comments,
            },
        )


def collect_files(
    raw_files: list[dict[str, Any]],
) -> tuple[list[ParsedFile], list[tuple[str, str]]]:
    parsed: list[ParsedFile] = []
    skipped: list[tuple[str, str]] = []
    for raw in raw_files:
        path = raw.get("filename")
        patch = raw.get("patch")
        if not isinstance(path, str):
            continue
        reason = skip_reason(path, patch if isinstance(patch, str) else None)
        if reason:
            skipped.append((path, reason))
            continue
        parsed.append(parse_patch(path, patch))
    return parsed, skipped


def review_marker(head_sha: str, review_id: str = DEFAULT_REVIEW_ID) -> str:
    return f"<!-- {review_id}:{head_sha} -->"


def build_model_input(pull: dict[str, Any], chunk: str) -> str:
    title = str(pull.get("title") or "")[:MAX_PR_METADATA_CHARS]
    description = str(pull.get("body") or "")[:MAX_PR_METADATA_CHARS]
    return (
        f"PR TITLE: {title}\n"
        f"PR DESCRIPTION: {description}\n\n"
        f"UNTRUSTED DIFF:\n{chunk}"
    )


def has_existing_review(
    reviews: list[dict[str, Any]],
    head_sha: str,
    review_id: str = DEFAULT_REVIEW_ID,
) -> bool:
    marker = review_marker(head_sha, review_id)
    return any(
        (item.get("user") or {}).get("login") == "github-actions[bot]"
        and marker in (item.get("body") or "")
        for item in reviews
    )


def build_summary(
    head_sha: str,
    findings: list[Finding],
    rejected: int,
    skipped: list[tuple[str, str]],
    truncated: bool,
    incomplete_chunks: int = 0,
    review_id: str = DEFAULT_REVIEW_ID,
) -> str:
    coverage = (
        "Partial" if skipped or truncated or incomplete_chunks else "Complete"
    )
    headline = (
        f"Automated review found {len(findings)} actionable finding(s)."
        if findings
        else "Automated review found no actionable findings."
    )
    lines = [
        review_marker(head_sha, review_id),
        headline,
        "",
        f"Reviewed head: `{head_sha}`",
        f"Coverage: {coverage}",
        f"Rejected model findings: {rejected}",
    ]
    if skipped:
        lines.extend(["", "Skipped files:"])
        lines.extend(
            f"- `{_neutralize_mentions(path)}` ({reason})"
            for path, reason in skipped[:MAX_SKIPPED_PATHS_IN_SUMMARY]
        )
        omitted = len(skipped) - MAX_SKIPPED_PATHS_IN_SUMMARY
        if omitted > 0:
            lines.append(f"- {omitted} additional skipped file(s) omitted.")
    if truncated:
        lines.extend(
            ["", "- Additional diff content exceeded the total review budget."]
        )
    if incomplete_chunks:
        lines.extend(
            [
                "",
                f"- {incomplete_chunks} diff chunk(s) returned an empty model "
                "response and were not reviewed.",
            ]
        )
    return "\n".join(lines)


def run_review(
    github: GitHubClient,
    llm: LlmClient,
    pull_number: int,
    prompt: str,
    review_id: str = DEFAULT_REVIEW_ID,
) -> str:
    pull = github.get_pull(pull_number)

    head_sha = pull["head"]["sha"]
    if has_existing_review(github.list_reviews(pull_number), head_sha, review_id):
        return "duplicate"

    files, skipped = collect_files(github.list_files(pull_number))
    by_path = {item.path: item for item in files}
    chunks, truncated = build_chunks(files)
    findings: list[Finding] = []
    rejected = 0
    incomplete_chunks = 0
    for chunk in chunks:
        payload = llm.review(prompt, build_model_input(pull, chunk))
        if payload.get("incomplete"):
            incomplete_chunks += 1
        chunk_findings, chunk_rejected = validate_findings(payload, by_path)
        findings.extend(chunk_findings)
        rejected += chunk_rejected

    aggregate_payload = {"findings": [item.__dict__ for item in findings]}
    findings, aggregate_rejected = validate_findings(aggregate_payload, by_path)
    rejected += aggregate_rejected

    current = github.get_pull(pull_number)
    if current["head"]["sha"] != head_sha:
        return "stale"

    summary = build_summary(
        head_sha,
        findings,
        rejected,
        skipped,
        truncated,
        incomplete_chunks=incomplete_chunks,
        review_id=review_id,
    )
    github.create_review(pull_number, head_sha, summary, findings)
    return "published"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-path", required=True, type=Path)
    parser.add_argument("--prompt-path", required=True, type=Path)
    return parser.parse_args()


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def main() -> int:
    args = parse_args()
    event = json.loads(args.event_path.read_text())
    repository = require_env("GITHUB_REPOSITORY")
    pull_number = int(event["pull_request"]["number"])
    github = GitHubClient(repository, require_env("GITHUB_TOKEN"))
    llm = LlmClient(
        require_env("LLM_REVIEW_BASE_URL"),
        require_env("LLM_REVIEW_API_KEY"),
        require_env("LLM_REVIEW_MODEL"),
    )
    prompt = args.prompt_path.read_text()
    review_id = os.environ.get("LLM_REVIEW_ID", DEFAULT_REVIEW_ID)
    result = run_review(github, llm, pull_number, prompt, review_id)
    print(f"LLM PR review result: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
