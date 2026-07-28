from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
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
SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
API_VERSION = "2022-11-28"
REQUEST_TIMEOUT_SECONDS = 600
MAX_PR_METADATA_CHARS = 4_000
MAX_SKIPPED_PATHS_IN_SUMMARY = 50
DEFAULT_REVIEW_ID = "pi-pr-review"


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


def _bounded_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_FIELD_CHARS:
        return None
    return text


def _neutralize_mentions(text: str) -> str:
    return text.replace("@", "@\u200b")


def _lens_attribution(finding: Finding) -> str:
    if not finding.lenses:
        return ""
    labels = " + ".join(_neutralize_mentions(lens) for lens in finding.lenses)
    return f"\n\n_Flagged by: {labels}_"


def validate_findings(
    payload: dict[str, Any],
    _files: dict[str, ParsedFile],
) -> tuple[list[Finding], int]:
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
                    f"{_lens_attribution(item)}"
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
        and marker in (item.get("body") or "")
        for item in reviews
    )


def build_summary(
    head_sha: str,
    findings: list[Finding],
    rejected: int,
    skipped: list[tuple[str, str]],
    truncated: bool,
    incomplete_lenses: int = 0,
    review_id: str = DEFAULT_REVIEW_ID,
    base_sha: str | None = None,
    summary_findings: list[Finding] | None = None,
    changed_paths: frozenset[str] = frozenset(),
    label: str | None = None,
    failed_lenses: tuple[str, ...] = (),
    tool_calls: dict[str, int] | None = None,
) -> str:
    coverage = (
        "Partial"
        if skipped or truncated or incomplete_lenses or failed_lenses
        else "Complete"
    )
    headline = (
        f"Automated review found {len(findings)} actionable finding(s)."
        if findings
        else "Automated review found no actionable findings."
    )
    lines = [
        review_marker(head_sha, review_id, base_sha),
    ]
    if label:
        lines.extend([f"## {_neutralize_mentions(label)}", ""])
    lines.extend(
        [
            headline,
            "",
            f"Reviewed head: `{head_sha}`",
            f"Coverage: {coverage}",
            f"Rejected model findings: {rejected}",
        ]
    )
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
    if incomplete_lenses:
        lines.extend(
            [
                "",
                (
                    f"- {incomplete_lenses} review lens(es) returned an empty model "
                    "response and were not reviewed."
                ),
            ]
        )
    if failed_lenses:
        lines.extend(["", f"Failed lenses: {', '.join(failed_lenses)}"])
    if tool_calls:
        counts = ", ".join(
            f"{_neutralize_mentions(lens)}={count}"
            for lens, count in tool_calls.items()
        )
        lines.extend(["", f"Tool calls: {counts}"])
    if summary_findings:
        changed = [
            item
            for item in summary_findings
            if item.severity != "P3" and item.path in changed_paths
        ]
        minor = [item for item in summary_findings if item.severity == "P3"]
        other = [
            item
            for item in summary_findings
            if item.severity != "P3" and item.path not in changed_paths
        ]
        lines.extend(["", "## Summary findings"])
        for path in dict.fromkeys(item.path for item in changed):
            lines.extend(["", f"### `{_neutralize_mentions(path)}`"])
            lines.extend(_summary_finding(item) for item in changed if item.path == path)
        if minor:
            lines.extend(["", "### Minor"])
            lines.extend(_summary_finding(item) for item in minor)
        if other:
            lines.extend(["", "### Other observations"])
            lines.extend(_summary_finding(item) for item in other)
    return "\n".join(lines)


def _summary_finding(finding: Finding) -> str:
    location = f" (`{_neutralize_mentions(finding.path)}"
    if finding.line is not None:
        location += f":{finding.line}"
    location += "`)"
    return (
        f"- **{finding.severity}: {_neutralize_mentions(finding.title)}**"
        f"{location} — {_neutralize_mentions(finding.failure_scenario)} "
        f"Suggested remediation: {_neutralize_mentions(finding.remediation)}"
        f"{_lens_attribution(finding)}"
    )
