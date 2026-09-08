#!/usr/bin/env python3
"""Verify frozen Pi findings without publishing GitHub reviews."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any

import pi_pr_review as pi

VERDICTS = ("confirmed", "rejected", "insufficient_evidence")
MAX_BLOB_BYTES = 2 * 1024 * 1024
MAX_INPUT_BYTES = 100_000
PROMPT_PATH = Path(__file__).with_name("pi_review_verification_prompt.md")
ARTIFACT_TEXT_FIELDS = frozenset({
    "id", "repository", "model", "path", "title", "failure_scenario", "remediation",
    "rationale", "introduced_by_change", "counterevidence", "quote", "validation_errors",
    "source_id",
})


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 2_000


def _path(value: Any) -> bool:
    return (
        _text(value) and not value.startswith("/")
        and not any(ord(char) < 32 for char in value)
        and str(PurePosixPath(value)) == value
        and ".." not in PurePosixPath(value).parts
    )


def validate_case(case: dict) -> None:
    if not isinstance(case, dict) or type(case.get("schema_version")) is not int or case["schema_version"] != 1:
        raise ValueError("expected replay case schema_version 1")
    if not _text(case.get("id")) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(case.get("repository", ""))):
        raise ValueError("invalid case identity or repository")
    for field in ("base_sha", "head_sha"):
        if not re.fullmatch(r"[0-9a-f]{40}", str(case.get(field, ""))):
            raise ValueError("replay requires full base/head commit SHAs")
    if not isinstance(case.get("finding"), dict) or not isinstance(case["finding"].get("severity"), str):
        raise pi._review.ModelResponseError("invalid replay finding")
    findings, rejected = pi._review.parse_findings({"findings": [case["finding"]]})
    if rejected or len(findings) != 1 or not _path(findings[0].path):
        raise ValueError("invalid replay finding")
    if case.get("expected_verdict") is not None and case["expected_verdict"] not in VERDICTS:
        raise ValueError("invalid expected verdict")
    context = case.get("context")
    if not isinstance(context, list) or not 1 <= len(context) <= 20:
        raise ValueError("replay requires 1 to 20 source excerpts")
    for item in context:
        if not isinstance(item, dict) or item.get("revision") not in ("base", "head") or not _path(item.get("path")):
            raise ValueError("invalid source revision or path")
        start, end = item.get("start_line"), item.get("end_line")
        if type(start) is not int or type(end) is not int or not 1 <= start <= end or end - start >= 200:
            raise ValueError("source excerpts require 1 to 200 positive numbered lines")


def _source(spec: dict, sha: str, directory: Path, source_id: str) -> dict:
    result = {"source_id": source_id, **spec, "commit_sha": sha}
    command = ["git", f"--git-dir={directory.resolve()}"]

    def git(*args: str) -> bytes:
        return subprocess.run(
            [*command, *args], check=True, capture_output=True, timeout=30,
        ).stdout

    entry = git("ls-tree", "-z", sha, "--", f":(literal){spec['path']}")
    if not entry:
        return {**result, "status": "absent"}
    mode, kind, blob = entry.split(b"\t", 1)[0].split()
    if kind != b"blob" or mode not in {b"100644", b"100755"}:
        return {**result, "status": "omitted", "reason": "not a regular file"}
    result["blob_sha"] = blob.decode("ascii")
    size = int(git("cat-file", "-s", result["blob_sha"]))
    if size > MAX_BLOB_BYTES:
        return {**result, "status": "omitted", "reason": "blob exceeds byte limit"}
    try:
        content = git("cat-file", "blob", result["blob_sha"]).decode("utf-8")
    except UnicodeDecodeError:
        return {**result, "status": "omitted", "reason": "not UTF-8 source"}
    lines = content.split("\n")
    if lines[-1] == "":
        lines.pop()
    start, end = spec["start_line"], min(spec["end_line"], len(lines))
    if start > end:
        return {**result, "status": "omitted", "reason": "range outside file"}
    return {**result, "status": "available", "end_line": end, "text": "\n".join(lines[start - 1:end])}


def parse_verdict(payload: dict) -> dict:
    if payload.get("verdict") not in VERDICTS or not _text(payload.get("rationale")):
        raise pi._review.ModelResponseError("invalid verdict or rationale; rationale must contain 1 to 2000 characters")
    fields = ("failure_scenario", "introduced_by_change", "counterevidence")
    for field in fields:
        value = payload.get(field)
        if not isinstance(value, str) or len(value) > 2_000:
            raise pi._review.ModelResponseError(f"{field} must be a string of at most 2000 characters")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or len(evidence) > 8:
        raise pi._review.ModelResponseError("expected at most eight evidence citations")
    citations = []
    for item in evidence:
        if not isinstance(item, dict) or not _text(item.get("source_id")):
            raise pi._review.ModelResponseError("invalid citation source ID")
        if item.get("absent") is True:
            citations.append({"source_id": item["source_id"], "absent": True})
            continue
        if (type(item.get("start_line")) is not int or type(item.get("end_line")) is not int
                or not 1 <= item["start_line"] <= item["end_line"] or not _text(item.get("quote"))):
            raise pi._review.ModelResponseError("invalid citation range or quote; quote must contain 1 to 2000 characters")
        citations.append({key: item[key] for key in ("source_id", "start_line", "end_line", "quote")})
    return {key: payload[key] for key in ("verdict", "rationale", *fields)} | {"evidence": citations}


def validate_evidence(payload: dict, sources: list[dict]) -> dict:
    result = parse_verdict(payload)
    by_id = {item["source_id"]: item for item in sources}
    errors, cited = [], []
    for citation in result["evidence"]:
        source = by_id.get(citation["source_id"])
        valid = False
        if source is not None:
            if citation.get("absent"):
                valid = source["status"] == "absent"
            elif source["status"] == "available":
                start, end = citation["start_line"], citation["end_line"]
                if source["start_line"] <= start <= end <= source["end_line"]:
                    lines = source["text"].split("\n")
                    actual = "\n".join(lines[start - source["start_line"]:end - source["start_line"] + 1])
                    valid = actual == citation["quote"]
        if valid:
            cited.append(source)
        else:
            errors.append(f"unverified citation: {citation['source_id']}")
    revisions = {item["revision"] for item in cited}
    if result["verdict"] == "confirmed":
        if revisions != {"base", "head"}:
            errors.append("confirmation requires base and head evidence")
        for field in ("failure_scenario", "introduced_by_change", "counterevidence"):
            if not result[field].strip():
                errors.append(f"confirmation requires {field}")
        changed = any(
            left["revision"] == "base" and right["revision"] == "head"
            and left["path"] == right["path"] and left.get("blob_sha") != right.get("blob_sha")
            for left in cited for right in cited
        )
        if not changed:
            errors.append("confirmation requires cited evidence of a changed file")
    elif result["verdict"] == "rejected" and "head" not in revisions:
        errors.append("rejection requires head counterevidence")
    return {
        **result, "model_verdict": result["verdict"],
        "verdict": "insufficient_evidence" if errors else result["verdict"],
        "validation_errors": errors,
    }


def _redact_artifact(artifact: dict, secrets: tuple[str, ...]) -> dict:
    values = sorted({secret for secret in secrets if secret}, key=len, reverse=True)
    if not values:
        return artifact
    pattern = re.compile("|".join(re.escape(secret) for secret in values))
    source_ids = {source["source_id"] for source in artifact.get("sources", [])}

    def redact(value: Any, field: str = "") -> Any:
        if isinstance(value, dict):
            return {key: redact(item, key) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [redact(item, field) for item in value]
        if isinstance(value, str) and field in ARTIFACT_TEXT_FIELDS:
            if field == "source_id" and value in source_ids:
                return value
            return pattern.sub("[REDACTED]", value)
        return value

    return redact(artifact)


def run_replay(case: dict, client: pi.PiClient, github: pi._review.GitHubClient) -> dict:
    validate_case(case)
    prompt = PROMPT_PATH.read_text()
    started = time.monotonic()
    finding = asdict(pi._review.parse_findings({"findings": [case["finding"]]})[0][0])
    artifact = {
        "schema_version": 1, "mode": "no-publish-verification-replay", "id": case["id"],
        "repository": case["repository"], "base_sha": case["base_sha"], "head_sha": case["head_sha"],
        "model": client.model, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "verifier_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "pi_client_code_sha256": hashlib.sha256(Path(pi.__file__).read_bytes()).hexdigest(),
        "finding": finding,
    }
    stage = "source"
    try:
        with tempfile.TemporaryDirectory(prefix="pi-review-replay-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
            roots = {revision: Path(temporary) / f"{revision}.git" for revision in ("base", "head")}
            for revision, root in roots.items():
                client.prepare_source(github, case[f"{revision}_sha"], root)
            sources = [
                _source({key: spec[key] for key in ("revision", "path", "start_line", "end_line")},
                        case[f"{spec['revision']}_sha"], roots[spec["revision"]], f"s{index}")
                for index, spec in enumerate(case["context"], 1)
            ]
            artifact["sources"] = [{key: value for key, value in source.items() if key != "text"} for source in sources]
            model_input = json.dumps({"candidate": finding, "sources": sources}, ensure_ascii=False)
            if len(model_input.encode()) > MAX_INPUT_BYTES:
                raise ValueError("source excerpts exceed replay input budget; narrow the ranges")
            artifact["input_sha256"] = hashlib.sha256(model_input.encode()).hexdigest()
            stage = "verifier"
            payload = client.review(prompt, model_input, no_tools=True, retry_malformed=True, response_validator=parse_verdict)
            if payload.get("_pi_tool_calls", 0):
                raise pi.PiReviewError("tool-free verifier unexpectedly executed tools")
            artifact.update(status="completed", verification=validate_evidence(payload, sources))
            artifact["tool_calls"] = 0
    except (pi.PiReviewError, pi._review.ModelResponseError, subprocess.SubprocessError, OSError, ValueError) as exc:
        artifact.update(status="failed", failed_stage=stage, error_type=type(exc).__name__)
    artifact["elapsed_seconds"] = round(time.monotonic() - started, 3)
    if "expected_verdict" in case:
        artifact["expected_verdict"] = case["expected_verdict"]
        artifact["matches_expected"] = (
            artifact["verification"]["verdict"] == case["expected_verdict"]
            if artifact["status"] == "completed" else None
        )
    return _redact_artifact(artifact, (client.api_key, github.token))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--pi-bin", default="pi")
    args = parser.parse_args(argv)
    try:
        if args.case.stat().st_size > MAX_INPUT_BYTES:
            raise ValueError("case exceeds byte limit")
        case = json.loads(args.case.read_text())
        validate_case(case)
        client = pi.PiClient(
            args.pi_bin, pi.require_env("LLM_REVIEW_BASE_URL"), pi.require_env("LLM_REVIEW_API_KEY"),
            pi.require_env("LLM_REVIEW_MODEL"), args.repository_root.resolve(), timeout=120,
        )
        github = pi._review.GitHubClient(case["repository"], os.environ.get("GITHUB_TOKEN", ""))
        with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
            artifact = run_replay(case, client, github)
            json.dump(artifact, output, indent=2)
            output.write("\n")
        print(f"Replay {artifact['status']}: {args.output}")
        return 0 if artifact["status"] == "completed" else 1
    except (ValueError, OSError, RuntimeError):
        print("Replay could not start: check the case, environment, and unused output path", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
