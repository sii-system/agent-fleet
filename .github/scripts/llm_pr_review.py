from __future__ import annotations

import argparse
import html
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
REQUEST_TIMEOUT_SECONDS = 600
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_PR_METADATA_CHARS = 4_000
MAX_SKIPPED_PATHS_IN_SUMMARY = 50
MAX_SKIPPED_SUMMARY_BYTES = 15_000
MAX_REVIEW_BODY_BYTES = 60_000
DEFAULT_REVIEW_ID = "llm-pr-review"
SUMMARY_ROUTING_INSTRUCTION = (
    "Additional routing instruction: report concrete defects caused by the "
    "change even when the best evidence is on contextual unchanged lines or "
    "a related path. Use the exact relevant path and an integer line when "
    "available; set line to null only when no precise line exists. These "
    "findings will be published in the review summary."
)
SUMMARY_OMISSION_NOTICE = (
    "- Additional review summary content omitted to fit GitHub's body limit."
)
SUMMARY_MARKDOWN_ESCAPE_TABLE = str.maketrans(
    {character: f"\\{character}" for character in r"\`*_{}[]()#+-.!|~:/?="}
)


@dataclass(frozen=True)
class ParsedFile:
    path: str
    review_text: str
    right_lines: frozenset[int]


@dataclass(frozen=True)
class Finding:
    severity: str
    path: str
    line: int | None
    title: str
    failure_scenario: str
    remediation: str
    lenses: tuple[str, ...] = ()


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
            content = raw_line.removeprefix(" ")
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
    if (
        not text
        or len(text) > MAX_FIELD_CHARS
        or any(0xD800 <= ord(char) <= 0xDFFF for char in text)
    ):
        return None
    return text


def _neutralize_mentions(text: str) -> str:
    return text.replace("@", "@\u200b")


def _safe_summary_prose(text: str) -> str:
    escaped = _neutralize_mentions(text).translate(SUMMARY_MARKDOWN_ESCAPE_TABLE)
    return html.escape(escaped)


def _summary_code_span(text: str, *, trusted_suffix: str = "") -> str:
    longest_run = max((len(run) for run in re.findall(r"`+", text)), default=0)
    delimiter = "`" * (longest_run + 1)
    single_line = text.replace("\r", r"\r").replace("\n", r"\n")
    return f"{delimiter}{single_line}{trusted_suffix}{delimiter}"


def _skipped_summary_lines(skipped: list[tuple[str, str]]) -> list[str]:
    lines: list[str] = []
    for path, reason in skipped[:MAX_SKIPPED_PATHS_IN_SUMMARY]:
        line = f"- {_summary_code_span(path)} ({reason})"
        if len("\n".join([*lines, line]).encode("utf-8")) > MAX_SKIPPED_SUMMARY_BYTES:
            break
        lines.append(line)
    omitted = len(skipped) - len(lines)
    if omitted:
        notice = f"- {omitted} additional skipped file(s) omitted."
        while lines and (
            len("\n".join([*lines, notice]).encode("utf-8"))
            > MAX_SKIPPED_SUMMARY_BYTES
        ):
            lines.pop()
            omitted += 1
            notice = f"- {omitted} additional skipped file(s) omitted."
        lines.append(notice)
    return lines


def parse_findings(payload: dict[str, Any]) -> tuple[list[Finding], int]:
    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        raise ModelResponseError("findings must be an array")

    valid: list[Finding] = []
    seen: set[tuple[str, int | None, str, str]] = set()
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
        if (
            severity not in SEVERITY_ORDER
            or path is None
            or (line is not None and (type(line) is not int or line < 1))
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

    valid.sort(
        key=lambda item: (
            SEVERITY_ORDER[item.severity],
            item.path,
            item.line is None,
            item.line or 0,
        )
    )
    return valid, rejected


def validate_findings(
    payload: dict[str, Any],
    files: dict[str, ParsedFile],
    *,
    limit: int = MAX_COMMENTS,
) -> tuple[list[Finding], int]:
    findings, rejected = parse_findings(payload)
    anchored = [
        finding
        for finding in findings
        if (parsed := files.get(finding.path)) is not None
        and type(finding.line) is int
        and finding.line in parsed.right_lines
    ]
    rejected += len(findings) - len(anchored)
    rejected += max(0, len(anchored) - limit)
    return anchored[:limit], rejected


def route_findings(
    findings: list[Finding],
    files: dict[str, ParsedFile],
    *,
    limit: int = MAX_COMMENTS,
) -> tuple[list[Finding], list[Finding]]:
    inline: list[Finding] = []
    summary: list[Finding] = []
    for finding in findings:
        parsed = files.get(finding.path)
        can_inline = (
            finding.severity != "P3"
            and parsed is not None
            and finding.line in parsed.right_lines
        )
        if can_inline and len(inline) < limit:
            inline.append(finding)
        else:
            summary.append(finding)
    return inline, summary


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
                raise TypeError("GitHub list response must be an array")
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

    def create_issue_comment(self, number: int, body: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/issues/{number}/comments",
            {"body": body},
        )

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
                    + (
                        f"\n\nFlagged by: {' + '.join(item.lenses)}"
                        if item.lenses
                        else ""
                    )
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


def review_marker(
    head_sha: str,
    review_id: str = DEFAULT_REVIEW_ID,
    base_sha: str | None = None,
) -> str:
    revision = f"{head_sha}:{base_sha}" if base_sha is not None else head_sha
    return f"<!-- {review_id}:{revision} -->"


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
    base_sha: str | None = None,
) -> bool:
    marker = review_marker(head_sha, review_id, base_sha)
    return any(
        (item.get("user") or {}).get("login") == "github-actions[bot]"
        and (item.get("body") or "").splitlines()[:1] == [marker]
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
    base_sha: str | None = None,
    summary_findings: list[Finding] | None = None,
    changed_paths: frozenset[str] = frozenset(),
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
        review_marker(head_sha, review_id, base_sha),
        headline,
        "",
        f"Reviewed head: `{head_sha}`",
        f"Coverage: {coverage}",
        f"Rejected model findings: {rejected}",
    ]
    if skipped:
        lines.extend(["", "Skipped files:"])
        lines.extend(_skipped_summary_lines(skipped))
    if truncated:
        lines.extend(
            ["", "- Additional diff content exceeded the total review budget."]
        )
    if incomplete_chunks:
        lines.extend(
            [
                "",
                (
                    f"- {incomplete_chunks} diff chunk(s) returned an empty model "
                    "response and were not reviewed."
                ),
            ]
        )
    if summary_findings:
        summary_lines = _summary_lines(summary_findings, changed_paths)
        complete = "\n".join([*lines, *summary_lines])
        if len(complete.encode("utf-8")) <= MAX_REVIEW_BODY_BYTES:
            lines.extend(summary_lines)
        else:
            prioritized = sorted(
                summary_findings,
                key=lambda item: SEVERITY_ORDER[item.severity],
            )
            selected: list[Finding] = []
            for finding in prioritized:
                candidate = [*selected, finding]
                candidate_body = "\n".join(
                    [
                        *lines,
                        *_summary_lines(candidate, changed_paths),
                        "",
                        SUMMARY_OMISSION_NOTICE,
                    ]
                )
                if len(candidate_body.encode("utf-8")) > MAX_REVIEW_BODY_BYTES:
                    continue
                selected.append(finding)
            lines.extend(_summary_lines(selected, changed_paths))
            lines.extend(["", SUMMARY_OMISSION_NOTICE])
    return _cap_review_body("\n".join(lines))


def _summary_lines(
    findings: list[Finding],
    changed_paths: frozenset[str],
) -> list[str]:
    if not findings:
        return []
    changed = [
        item
        for item in findings
        if item.severity != "P3" and item.path in changed_paths
    ]
    minor = [item for item in findings if item.severity == "P3"]
    other = [
        item
        for item in findings
        if item.severity != "P3" and item.path not in changed_paths
    ]
    lines = ["", "## Summary findings"]
    for path in dict.fromkeys(item.path for item in changed):
        lines.extend(["", f"### {_summary_code_span(path)}"])
        lines.extend(_summary_finding(item) for item in changed if item.path == path)
    if other:
        lines.extend(["", "### Other observations"])
        lines.extend(_summary_finding(item) for item in other)
    if minor:
        lines.extend(["", "### Minor"])
        lines.extend(_summary_finding(item) for item in minor)
    return lines


def _cap_review_body(body: str) -> str:
    encoded = body.encode("utf-8")
    if len(encoded) <= MAX_REVIEW_BODY_BYTES:
        return body
    suffix = f"\n\n{SUMMARY_OMISSION_NOTICE}"
    budget = MAX_REVIEW_BODY_BYTES - len(suffix.encode("utf-8"))
    prefix = encoded[:budget].decode("utf-8", errors="ignore")
    prefix = prefix.rsplit("\n", 1)[0].rstrip()
    return f"{prefix}{suffix}"


def _summary_finding(finding: Finding) -> str:
    suffix = f":{finding.line}" if finding.line is not None else ""
    location = f" ({_summary_code_span(finding.path, trusted_suffix=suffix)})"
    rendered = (
        f"- **{finding.severity}: {_safe_summary_prose(finding.title)}**"
        f"{location} — {_safe_summary_prose(finding.failure_scenario)} "
        f"Suggested remediation: {_safe_summary_prose(finding.remediation)}"
    )
    if finding.lenses:
        rendered += f" Flagged by: {' + '.join(finding.lenses)}"
    return rendered


def run_review(
    github: GitHubClient,
    llm: LlmClient,
    pull_number: int,
    prompt: str,
    review_id: str = DEFAULT_REVIEW_ID,
    *,
    expected_head_sha: str | None = None,
    expected_base_sha: str | None = None,
) -> str:
    pull = github.get_pull(pull_number)

    head_sha = pull["head"]["sha"]
    if expected_head_sha is not None and head_sha != expected_head_sha:
        return "stale"
    if (
        expected_base_sha is not None
        and pull["base"]["sha"] != expected_base_sha
    ):
        return "stale"
    if has_existing_review(
        github.list_reviews(pull_number),
        head_sha,
        review_id,
        expected_base_sha,
    ):
        return "duplicate"

    files, skipped = collect_files(github.list_files(pull_number))
    by_path = {item.path: item for item in files}
    chunks, truncated = build_chunks(files)
    review_prompt = f"{prompt}\n{SUMMARY_ROUTING_INSTRUCTION}"
    findings: list[Finding] = []
    rejected = 0
    incomplete_chunks = 0
    for chunk in chunks:
        payload = llm.review(review_prompt, build_model_input(pull, chunk))
        if payload.get("incomplete"):
            incomplete_chunks += 1
        chunk_findings, chunk_rejected = parse_findings(payload)
        findings.extend(chunk_findings)
        rejected += chunk_rejected

    aggregate_payload = {"findings": [item.__dict__ for item in findings]}
    findings, aggregate_rejected = parse_findings(aggregate_payload)
    rejected += aggregate_rejected

    current = github.get_pull(pull_number)
    if current["head"]["sha"] != head_sha or (
        expected_base_sha is not None
        and current["base"]["sha"] != expected_base_sha
    ):
        return "stale"

    inline_findings, summary_findings = route_findings(findings, by_path)
    summary = build_summary(
        head_sha,
        findings,
        rejected,
        skipped,
        truncated,
        incomplete_chunks=incomplete_chunks,
        review_id=review_id,
        base_sha=expected_base_sha,
        summary_findings=summary_findings,
        changed_paths=frozenset(by_path),
    )
    github.create_review(pull_number, head_sha, summary, inline_findings)
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
