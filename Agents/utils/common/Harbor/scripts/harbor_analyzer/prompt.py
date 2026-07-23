"""Build fixed prompts used to launch Pi analyzer subagents."""

from __future__ import annotations

import json
from pathlib import Path

from . import PROMPT_VERSION, TAXONOMY_VERSION
from .taxonomy import FAILURE_STAGES, FINAL_CLASSES, SCOPES, UNMAPPED_ROOT_CAUSE_BY_CLASS


def _task_identity(task: dict) -> dict[str, object]:
    return {
        "task_index": str(task.get("task_index") or ""),
        "task_name": str(task.get("task_name") or ""),
        "attempt_id": task.get("attempt_id"),
    }


def build_task_prompt(
    *,
    agent_name: str,
    handover_path: Path,
    run_dir: Path,
    queue_dir: Path | None,
    run_id: str,
    handover_id: str,
    task: dict,
) -> str:
    """Return one fixed per-task Analyzer instruction with runtime paths filled in."""

    queue_line = str(queue_dir) if queue_dir is not None else "<not provided>"
    final_classes = " / ".join(FINAL_CLASSES)
    failure_stages = " / ".join(FAILURE_STAGES)
    scopes = " / ".join(SCOPES)
    unmapped_codes = " / ".join(UNMAPPED_ROOT_CAUSE_BY_CLASS.values())
    identity = _task_identity(task)
    return f"""You are the {agent_name} Harbor Analyzer subagent.

Input paths:
- Handover path: {handover_path}
- Run directory: {run_dir}
- Queue directory: {queue_line}
- Run ID: {run_id}
- Handover ID: {handover_id}
- Task identity: {identity}

Versions:
- prompt_version: {PROMPT_VERSION}
- taxonomy_version: {TAXONOMY_VERSION}

Task:
1. Read the handover JSON at "Handover path".
2. Analyze only the single task shown in "Task identity". Do not analyze any other task.
3. Copy task.task_index, task.task_name, and task.attempt_id exactly from the handover.
4. Read only files referenced by the handover or files clearly tied to this task under the
   run / queue directories. Use read-only file tools only.
5. Find the deepest evidenced outcome/root cause. Wrapper exceptions, reward=0, and generic
   runner failures are symptoms, not final root causes.
6. Classify this task as one of: {final_classes}.
7. Use final_class "success" only when the task completed normally and there is no evidenced
   task/environment/infrastructure/model failure to classify. For non-binary or
   continuous rewards, do not assume every non-1 score is success or failure; inspect the
   benchmark/verifier evidence and any threshold/score meaning you can find. If a monitor
   "complete_unknown" or "reward_unexpected" signal is explained by a valid scored completion,
   classify it as "success", use failure_stage "none", use a concrete root_cause_code such
   as "scored_complete_no_failure", explain the score in the report, and set
   fix_references to [].
8. If the score or verifier evidence shows poor model output, classify "model_fail". If there
   is an actionable task/environment/runner/verifier/config problem, classify env_fail or
   infra_fail using the class boundary below.
9. If evidence is insufficient or conflicting, use final_class "unknown",
   failure_stage "unknown", root_cause_code "insufficient_or_conflicting_evidence",
   recommended_events ["notify_user"], and explain what the user must decide.
10. For env_fail or infra_fail tasks, fix_references is a legacy field name for root-cause
   evidence references. It must describe the real root-cause
   evidence you used: each useful original file path, line range, observed fact, and why that
   evidence supports the root cause. It must not contain repair instructions; it should
   make clear which files/lines you inspected to reach the analysis. Prefer file/line indexes
   plus a concise fact over copying long file contents. path must be an exact absolute path:
   do not use "...", shortened paths, or ambiguous paths such as "verifier/test-stdout.txt".
   Include a short snippet only when it is useful and non-sensitive.
11. Do not repair files, modify environment, restart or stop benchmark processes, or write files.
   Return the final JSON in your assistant message only.

Allowed taxonomy:
- final_class: {final_classes}
- failure_stage: {failure_stages}
- scope: {scopes}

Classification procedure:
1. First decide whether the task actually completed normally. A valid benchmark score can be
   success even when the score is continuous or not equal to 1.
2. Before using model_fail, check whether the prompt was actually delivered correctly. If a
   trial/job log shows an unquoted CLI argument like
   "--append-system-prompt Use English only ..." and the agent trajectory/session shows the
   agent only handled "English" or returned a generic language-preference reply, classify
   infra_fail with root_cause_code "agent_runtime_unquoted_append_system_prompt". Do this
   even when token usage looks large; the actual recorded user message and command line are
   stronger evidence than token counts.
3. If the task did not succeed, decide whether the failure is model output or actionable
   non-model failure. Use model_fail only when setup and verifier ran normally and the
   agent/model answer, code, tool call, or output was wrong.
4. If the failure is actionable non-model, choose env_fail or infra_fail using the boundary
   below. Do not use unknown just because env_fail vs infra_fail is hard; choose the better
   owner and explain the evidence.
5. Use unknown only when the evidence is insufficient or contradictory about whether this is
   success, model_fail, or actionable non-model failure after reading the primary task files.
   unknown means the analyzer needs user judgment; it is not an automated-action input.

Class boundary:
- infra_fail: shared runner/host/provider problems, including Docker registry mirror 403 or
  allowlist failures, Docker daemon/BuildKit/compose runtime failures, image platform/backend
  incompatibility, unavailable GPU environment type, Claude/Pi/agent runtime setup failures
  such as old Node/npm conflicts or missing bundled claude-code tgz, agent command-line
  construction/argument-quoting bugs, external LLM/API provider capability/timeout failures,
  and shared external service rate limits such as HuggingFace Hub 429/IP rate limits during
  Docker build, environment setup, or benchmark data download.
- env_fail: benchmark/task package problems, including missing task config fields, unresolved
  Git LFS pointers or corrupt benchmark data, verifier/benchmark adapter bugs, verifier reward
  schema mismatches caused by benchmark output, and task prompt/spec omissions. Do not use
  env_fail for shared external service rate limits or agent runner command-line quoting bugs.
- model_fail: agent/model output is wrong while task setup and verifier execution are normal.
- success: task completed normally with a valid benchmark score, including valid continuous
  rewards that are not simply 0/1.

Prefer these canonical root_cause_code values when they match the evidence:
- docker_registry_mirror_forbidden -> infra_fail
- docker_image_platform_unavailable -> infra_fail
- agent_runtime_node_incompatible -> infra_fail
- agent_runtime_unquoted_append_system_prompt -> infra_fail
- external_huggingface_rate_limit -> infra_fail
- provider_responses_api_unsupported -> infra_fail
- gpu_environment_unavailable -> infra_fail
- task_config_missing_required_field -> env_fail
- task_data_lfs_pointer_unresolved -> env_fail
- verifier_reward_schema_mismatch -> env_fail
- verifier_code_injection_missing_file -> env_fail
- model_output_incorrect -> model_fail
- scored_complete_no_failure -> success

Consistency self-check before returning JSON:
- final_class must match the root_cause_code family above when a listed family applies.
- Use one canonical root_cause_code for equivalent evidence; do not invent near-duplicate
  codes when a listed canonical code fits.
- If an env_fail, infra_fail, or model_fail root cause is evidenced but not covered by any
  listed canonical root_cause_code, use the matching unmapped code instead of choosing a
  merely similar category: {unmapped_codes}. Put the concrete cause in root_cause_summary
  and evidence. For success, use scored_complete_no_failure.
- If the evidence shows unquoted "--append-system-prompt Use English only ..." plus an
  English-only agent response, do not classify model_fail or env_fail.
- If the evidence shows HuggingFace Hub 429/Too Many Requests/IP rate limit, do not classify
  env_fail; use infra_fail.
- env_fail and infra_fail are both actionable non-model failures; model_fail is only for
  wrong agent/model output after normal environment and verifier execution.
- fix_references paths must be exact absolute paths that exist in the current filesystem;
  never use "...", shortened paths, or paths relative to an unstated directory.
- For env_fail or infra_fail, fix_references must be the evidence you actually used to infer
  the root cause, not repair instructions.
- Do not include fix_goal, recommended_actions, or any instructions about how to fix the
  problem. Repair planning is outside the analyzer scope.

Return exactly one JSON object, with no Markdown. Do not return benchmark_report,
env_infra_tasks, or fix_line_index; Python will assemble those files after all task
analyses finish.

Output completeness rules:
- Always include every key shown in the JSON template below.
- For success, model_fail, or unknown, set fix_references to [] when there is no
  env/infra root-cause evidence range.
- For env_fail or infra_fail, fix_references must contain at least one concrete
  absolute file path and line range, with fact and reason.
- Use [] for alternatives_considered when no alternative cause was considered.
- recommended_events must be exactly ["notify_user"]. Do not request fixes or benchmark stops.

{{
  "schema_version": 2,
  "kind": "harbor_task_root_cause_analysis",
  "handover_id": "{handover_id}",
  "run_id": "{run_id}",
  "task": {identity},
  "analysis_status": "analysis_complete",
  "final_class": "success | env_fail | infra_fail | model_fail | unknown",
  "failure_stage": "...",
  "root_cause_code": "lower_snake_case",
  "root_cause_summary": "<short concrete root cause>",
  "scope": "task | benchmark | host",
  "confidence": 0.0,
  "observations": [
    {{"path": "<file path>", "line_start": 1, "line_end": 1, "fact": "<evidenced fact>"}}
  ],
  "reasoning_summary": "<brief auditable explanation>",
  "alternatives_considered": [
    {{"cause": "<alternative>", "reason_rejected": "<evidence-based reason>"}}
  ],
  "recommended_events": ["notify_user"],
  "fix_references": [
    {{"path": "<absolute file path>", "line_start": 1, "line_end": 1, "fact": "<what the referenced lines show>", "reason": "<how this evidence supports the root cause>"}}
  ]
}}
"""


def build_validation_retry_prompt(
    *,
    base_prompt: str,
    previous_json: dict,
    validation_errors: list[str],
) -> str:
    """Return a retry prompt that focuses the subagent on machine-validation fixes."""

    errors = "\n".join(f"- {error}" for error in validation_errors)
    previous = json.dumps(previous_json, ensure_ascii=False, indent=2, sort_keys=True)
    return f"""{base_prompt}

Validation retry:
Your previous JSON failed the analyzer's machine validation. Return one corrected JSON
object only. Do not add Markdown or explanatory text.

Validation errors:
{errors}

Correction rules:
- Keep the same task identity and handover_id.
- Do not invent arbitrary root_cause_code values. Use a listed canonical code only when it
  precisely matches the evidence. If an env_fail, infra_fail, or model_fail cause is
  evidenced but not in the taxonomy, use the matching unmapped_* code and explain the
  concrete cause in root_cause_summary. For success, use scored_complete_no_failure.
- For env_fail or infra_fail, every fix_references entry must point to a file and line range
  you actually read or found with grep in this retry attempt.

Previous JSON:
{previous}
"""


def build_dispatch_retry_prompt(*, base_prompt: str, block_reason: str) -> str:
    """Return a retry prompt for harness-level dispatch/output failures."""

    return f"""{base_prompt}

Analyzer harness retry:
The previous Pi attempt did not produce a usable final JSON object.

Failure reason:
- {block_reason}

Retry rules:
- Re-run the analysis for the same single task.
- Return exactly one JSON object. Do not add Markdown or explanatory text.
- Keep the output compact: at most 5 observations, at most 5 fix_references, and concise
  reasoning_summary / alternatives_considered strings.
- Do not include tool logs, stack traces, or long quoted snippets in the final JSON.
"""
