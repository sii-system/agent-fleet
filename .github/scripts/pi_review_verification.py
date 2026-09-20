"""Bounded independent verification of live review candidates."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import pi_pr_review as pi
import pi_review_replay as replay

MAX_CANDIDATES = 6
MAX_CONTEXT = 20
MAX_WORKERS = 3
VERIFY_TIMEOUT_SECONDS = 120


def _same_candidate(finding: pi._review.Finding, raw: dict) -> bool:
    return (
        raw.get("path") == finding.path and raw.get("line") == finding.line
        and isinstance(raw.get("title"), str)
        and isinstance(raw.get("failure_scenario"), str)
        and pi._text_similarity(raw["title"], finding.title) >= pi.MIN_FINDING_SIMILARITY
        and pi._text_similarity(raw["failure_scenario"], finding.failure_scenario) >= pi.MIN_FINDING_SIMILARITY
    )


def candidate_context(finding: pi._review.Finding, payloads: list[dict], raw_files: list[dict]) -> list[dict]:
    if not replay._path(finding.path):
        return []
    line = finding.line or 1
    start = max(1, line - 75)
    context = [
        {"revision": revision, "path": finding.path, "start_line": start, "end_line": start + 199}
        for revision in ("base", "head")
    ]
    for item in raw_files:
        previous = item.get("previous_filename")
        if item.get("filename") == finding.path and previous and replay._path(previous):
            context.extend(
                {"revision": revision, "path": previous, "start_line": start, "end_line": start + 199}
                for revision in ("base", "head")
            )
    for payload in payloads:
        for raw in payload.get("findings", []):
            if not isinstance(raw, dict) or not _same_candidate(finding, raw):
                continue
            hints = raw.get("verification_context", [])
            if not isinstance(hints, list):
                continue
            for hint in hints[:MAX_CONTEXT]:
                if not isinstance(hint, dict):
                    continue
                first, last = hint.get("start_line"), hint.get("end_line")
                if (hint.get("revision") not in ("base", "head") or not replay._path(hint.get("path"))
                        or type(first) is not int or type(last) is not int or not 1 <= first <= last
                        or last - first >= 200):
                    continue
                spec = {key: hint[key] for key in ("revision", "path", "start_line", "end_line")}
                if spec not in context:
                    context.append(spec)
                if len(context) >= MAX_CONTEXT:
                    return context
    return context


def verify_candidates(github, client: pi.PiClient, pull: dict, findings: list[pi._review.Finding],
                      payloads: list[dict], raw_files: list[dict]) -> list[dict]:
    if not findings:
        return []
    verifier = copy.copy(client)
    verifier.timeout = min(client.timeout, VERIFY_TIMEOUT_SECONDS)
    prompt = replay.PROMPT_PATH.read_text()
    records = [{"finding": asdict(finding), "status": "skipped", "reason": "candidate_budget"}
               for finding in findings]
    count = min(len(findings), MAX_CANDIDATES)
    errors = (pi.PiReviewError, pi._review.ModelResponseError, subprocess.SubprocessError, OSError, ValueError)
    try:
        with tempfile.TemporaryDirectory(prefix="pi-review-verify-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
            roots = {revision: Path(temporary) / f"{revision}.git" for revision in ("base", "head")}
            for revision, root in roots.items():
                verifier.prepare_source(github, pull[revision]["sha"], root)

            def verify(index):
                record = {"finding": asdict(findings[index])}
                try:
                    context = candidate_context(findings[index], payloads, raw_files)
                    if not context:
                        return {**record, "status": "skipped", "reason": "invalid_source_path"}
                    sources = [replay._source(spec, pull[spec["revision"]]["sha"], roots[spec["revision"]], f"s{number}")
                               for number, spec in enumerate(context, 1)]
                    record["sources"] = [{key: value for key, value in source.items() if key != "text"}
                                         for source in sources]
                    model_input = json.dumps({"candidate": record["finding"], "sources": sources}, ensure_ascii=False)
                    record["input_sha256"] = hashlib.sha256(model_input.encode()).hexdigest()
                    record["prompt_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
                    record["verifier_code_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
                    if len(model_input.encode()) > replay.MAX_INPUT_BYTES:
                        return {**record, "status": "skipped", "reason": "input_budget"}
                    payload = verifier.review(prompt, model_input, no_tools=True, retry_malformed=True,
                                              response_validator=replay.parse_verdict)
                    if payload.get("_pi_tool_calls", 0):
                        raise pi.PiReviewError("tool-free verifier unexpectedly executed tools")
                    return {**record, "status": "completed", "verification": replay.validate_evidence(payload, sources)}
                except errors as exc:
                    return {**record, "status": "failed", "error_type": type(exc).__name__}

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                records[:count] = list(executor.map(verify, range(count)))
    except errors as exc:
        for index in range(count):
            records[index] = {"finding": asdict(findings[index]), "status": "failed", "error_type": type(exc).__name__}
    return records
