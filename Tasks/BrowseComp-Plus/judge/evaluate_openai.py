#!/usr/bin/env python3
"""BrowseComp judge adapter for OpenAI-compatible Fleet model gateways."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI

BENCHMARK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCHMARK_DIR))
from judge.cache import (  # noqa: E402
    EVALUATION_CACHE_SCHEMA_VERSION,
    evaluation_fingerprint,
    load_cached_evaluation,
)

GRADER_TEMPLATE: str | None = None


def load_grader_template(source_root: Path) -> str:
    prompt_source = source_root / "search_agent" / "prompts.py"
    spec = importlib.util.spec_from_file_location(
        "browsecomp_upstream_prompts", prompt_source
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load upstream grader prompt: {prompt_source}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    template = getattr(module, "GRADER_TEMPLATE", None)
    if not isinstance(template, str) or not template.strip():
        raise RuntimeError(f"upstream grader prompt is invalid: {prompt_source}")
    return template


def load_jsonl(path: Path) -> dict[str, dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return {str(row["query_id"]): row for row in rows}


def load_qrels(path: Path) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) == 4:
                result[fields[0]].add(fields[2])
    return result


def grader_prompt(question: str, response: str, answer: str) -> str:
    if GRADER_TEMPLATE is not None:
        return GRADER_TEMPLATE.format(
            question=question, response=response, correct_answer=answer
        )
    return f"""Judge whether the following response is correct based only on the precise correct answer.

[question]: {question}

[response]: {response}

[correct_answer]: {answer}

Reply using exactly these fields:
extracted_final_answer: the exact answer extracted from the response, or None
reasoning: why it does or does not match the correct answer
correct: yes or no
confidence: the response's confidence from 0% to 100%, or 100 if absent
""".strip()


def parse_judgement(text: str) -> dict[str, Any]:
    def field(name: str) -> str | None:
        match = re.search(
            rf"(?:\*\*)?{name}(?::\*\*|\*\*:|:)\s*(.*?)(?=\n(?:\*\*)?[a-z_]+(?:\*\*)?:|$)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        return match.group(1).strip() if match else None

    correct_value = field("correct")
    confidence_value = field("confidence")
    confidence_match = re.search(r"\d+(?:\.\d+)?", confidence_value or "")
    return {
        "extracted_final_answer": field("extracted_final_answer"),
        "reasoning": field("reasoning"),
        "correct": (
            correct_value.lower().startswith("yes") if correct_value is not None else None
        ),
        "confidence": (
            min(100.0, float(confidence_match.group())) if confidence_match else None
        ),
        "parse_error": correct_value is None,
    }


def response_text(run: dict[str, Any]) -> str:
    for item in reversed(run.get("result") or []):
        if item.get("type") == "output_text":
            return str(item.get("output") or "")
    return ""


def citations(text: str) -> list[str]:
    found: set[str] = set()
    for group in re.findall(r"\[([^\[\]]+)\]|【([^【】]+)】", text):
        found.update(re.findall(r"\d+", next(value for value in group if value)))
    return sorted(found)


def citation_metrics(cited: list[str], relevant: set[str]) -> dict[str, float | int]:
    cited_set = set(cited)
    overlap = cited_set & relevant
    return {
        "num_citations": len(cited),
        "num_relevant": len(relevant),
        "precision": len(overlap) / len(cited_set) if cited_set else 0.0,
        "recall": len(overlap) / len(relevant) if relevant else 0.0,
    }


def calibration_error(evaluations: list[dict[str, Any]], bin_size: int = 100) -> float:
    """Return the confidence-binned RMS calibration error as a percentage."""

    samples: list[tuple[float, float]] = []
    for evaluation in evaluations:
        judgement = evaluation.get("judge_result") or {}
        confidence = judgement.get("confidence") if isinstance(judgement, dict) else None
        if (
            isinstance(confidence, (int, float))
            and not judgement.get("parse_error", False)
            and judgement.get("correct") is not None
        ):
            samples.append(
                (float(confidence) / 100.0, float(bool(judgement["correct"])))
            )
    if len(samples) < bin_size:
        return 0.0
    samples.sort()
    squared_error = 0.0
    for start in range(0, len(samples), bin_size):
        bucket = samples[start : start + bin_size]
        mean_confidence = sum(item[0] for item in bucket) / len(bucket)
        mean_correct = sum(item[1] for item in bucket) / len(bucket)
        squared_error += (
            len(bucket) / len(samples) * (mean_confidence - mean_correct) ** 2
        )
    return 100.0 * math.sqrt(squared_error)


def call_judge(
    client: OpenAI, model: str, prompt: str, api_mode: str, max_tokens: int
) -> str:
    if api_mode == "chat-completions":
        result = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )
        return result.choices[0].message.content or "" if result.choices else ""
    result = client.responses.create(
        model=model, input=prompt, max_output_tokens=max_tokens
    )
    return result.output_text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_dir", required=True, type=Path)
    parser.add_argument("--ground_truth", required=True, type=Path)
    parser.add_argument("--eval_dir", required=True, type=Path)
    parser.add_argument("--qrel_evidence", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base_url")
    parser.add_argument("--api_key_env", default="API_KEY")
    parser.add_argument(
        "--api_mode", choices=["responses", "chat-completions"], default="chat-completions"
    )
    parser.add_argument("--max_output_tokens", type=int, default=1024)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    global GRADER_TEMPLATE
    GRADER_TEMPLATE = load_grader_template(args.source_root.resolve())

    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise RuntimeError(f"judge credential variable is empty: {args.api_key_env}")
    client = OpenAI(api_key=api_key, base_url=args.base_url or None)
    truth = load_jsonl(args.ground_truth)
    qrels = load_qrels(args.qrel_evidence)
    args.eval_dir.mkdir(parents=True, exist_ok=True)
    evaluations: list[dict[str, Any]] = []

    for path in sorted(args.input_dir.glob("*.json")):
        output = args.eval_dir / f"{path.stem}_eval.json"
        run = json.loads(path.read_text(encoding="utf-8"))
        query_id = str(run.get("query_id", ""))
        if query_id not in truth:
            raise KeyError(f"ground truth has no query_id {query_id}")
        response = response_text(run)
        completed = run.get("status") == "completed" and bool(response)
        prompt = grader_prompt(
            str(truth[query_id]["query"]), response, str(truth[query_id]["answer"])
        )
        relevant = qrels.get(query_id, set())
        fingerprint = evaluation_fingerprint(
            run=run,
            prompt=prompt,
            relevant_docids=relevant,
            model=args.model,
            base_url=args.base_url,
            api_mode=args.api_mode,
            max_output_tokens=args.max_output_tokens,
        )
        if output.is_file() and not args.force:
            cached = load_cached_evaluation(output, fingerprint)
            if cached is not None:
                evaluations.append(cached)
                continue
        if completed:
            judge_text = call_judge(
                client, args.model, prompt, args.api_mode, args.max_output_tokens
            )
            judgement = parse_judgement(judge_text)
        else:
            judge_text = ""
            judgement = {
                "correct": False,
                "confidence": None,
                "parse_error": True,
                "error": "response incomplete or empty",
            }
        retrieved = {str(value) for value in run.get("retrieved_docids", [])}
        cited = citations(response)
        metadata = run.get("metadata") or {}
        evaluation = {
            "json_path": str(path),
            "query_id": query_id,
            "question": truth[query_id]["query"],
            "response": response,
            "correct_answer": truth[query_id]["answer"],
            "is_completed": completed,
            "judge_prompt": prompt,
            "judge_response": judge_text,
            "judge_result": judgement,
            "tool_call_counts": run.get("tool_call_counts", {}),
            "citations": {
                "cited_docids": cited,
                "metrics": citation_metrics(cited, relevant),
            },
            "retrieval": {
                "retrieved_docids": sorted(retrieved),
                "recall": len(retrieved & relevant) / len(relevant) if relevant else None,
            },
            "model_info": {
                "agent_model": metadata.get("model")
                if isinstance(metadata, dict)
                else None,
                "judge_model": args.model,
                "api_mode": args.api_mode,
            },
            "evaluation_cache": {
                "schema_version": EVALUATION_CACHE_SCHEMA_VERSION,
                "fingerprint": fingerprint,
            },
        }
        output.write_text(
            json.dumps(evaluation, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        evaluations.append(evaluation)

    tool_counts: dict[str, float] = defaultdict(float)
    for evaluation in evaluations:
        for name, count in evaluation.get("tool_call_counts", {}).items():
            tool_counts[name] += float(count)
    total = len(evaluations)
    if total:
        tool_counts = {name: value / total for name, value in tool_counts.items()}
    recalls = [
        value
        for evaluation in evaluations
        if isinstance((value := evaluation["retrieval"]["recall"]), (int, float))
    ]
    agent_model = next(
        (
            str(value)
            for evaluation in evaluations
            if (value := (evaluation.get("model_info") or {}).get("agent_model"))
        ),
        "change me when submitting",
    )
    summary = {
        "LLM": agent_model,
        "Accuracy (%)": round(
            100 * sum(bool(e["judge_result"].get("correct")) for e in evaluations) / total,
            2,
        ) if total else 0.0,
        "Recall (%)": round(100 * sum(recalls) / len(recalls), 2) if recalls else None,
        "avg_tool_stats": tool_counts,
        "Calibration Error (%)": round(calibration_error(evaluations), 2),
        "Retriever": "Qwen/Qwen3-Embedding-0.6B (Agent Fleet FAISS MCP)",
        "Link": "change me when submitting",
        "Evaluation Date": datetime.now(timezone.utc).date().isoformat(),
        "per_query_metrics": [
            {
                "query_id": e["query_id"],
                "correct": bool(e["judge_result"].get("correct")),
                "recall": (
                    round(100 * e["retrieval"]["recall"], 2)
                    if isinstance(e["retrieval"]["recall"], (int, float))
                    else None
                ),
            }
            for e in evaluations
        ],
    }
    (args.eval_dir / "evaluation_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"[BrowseComp judge][ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
