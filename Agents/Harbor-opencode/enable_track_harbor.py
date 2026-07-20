"""Wrapper that activates `opik.integrations.harbor.track_harbor` and
then runs the harbor CLI in the same interpreter.

`opik.integrations.harbor.opik_tracker` does `from harbor.job import Job`
at module import time, so opik and harbor must share an interpreter.
The default `uv tool install harbor` and `uv tool install opik` produce
two separate venvs that cannot import each other; this wrapper runs
inside the unified env produced by `setup_env.sh`
(`uv tool install harbor --with opik --with uuid6`).

Usage:

    "$HARBOR_OPIK_PYTHON" enable_track_harbor.py run \\
        --path /workspace/seta-env/Harbor-Dataset \\
        --include-task-name fix-git \\
        --agent-import-path opik_opencode_harbor:OpikOpenCodeHarbor \\
        ...

What this script does:

  1. `track_harbor(project_name=$OPIK_PROJECT_NAME)` — monkey-patches
     `Trial.run`, `Trial._setup_*`, `Trial._execute_agent`,
     `Verifier.verify`, and `Step.__init__` to emit a per-trial trace
     tree, including verifier rewards as feedback scores.
  2. Patches `HarborTrialRunDecorator._start_span_inputs_preprocessor`
     to also tag trace trees with `tb-run:`, `tb-task:`, `tb-trial:`
     when the corresponding env vars are present. The default decorator
     uses `config.agent.name` (which is `None` when `--agent-import-path`
     is used instead of `--agent`) and never reads TB_* env, so tags
     would otherwise miss the run/task/trial identifiers needed to
     correlate harbor traces with the realtime traces emitted by the
     in-container plugin.
  3. Replaces `sys.argv[0]` with `harbor` and invokes the harbor Typer
     app, mirroring the layout the harbor entry-point uses.
"""

from __future__ import annotations

import os
import sys


def _trace_to_opik_enabled() -> bool:
    return os.environ.get("TRACE_TO_OPIK", "true") not in {"false", "0"}


def _clean_tags(tags: object) -> list[str]:
    if not tags:
        return []
    return [str(tag) for tag in tags if tag is not None]


def _patch_opik_batch_tags() -> None:
    """Sanitize final Opik writes so host-side Harbor tracking stays enabled.

    `track_harbor()` can emit tags derived from Harbor's agent config. When
    Harbor is run through `--agent-import-path`, that agent name may be None.
    Opik's REST models require every tag to be a string, so filter at the last
    write boundary as a defense-in-depth patch for traces and spans.
    """
    from opik.message_processing.batching import batchers

    original_span_write = batchers.span_write.SpanWrite
    original_trace_write = batchers.trace_write.TraceWrite

    def safe_span_write(**kwargs):
        if "tags" in kwargs:
            kwargs["tags"] = _clean_tags(kwargs.get("tags"))
        return original_span_write(**kwargs)

    def safe_trace_write(**kwargs):
        if "tags" in kwargs:
            kwargs["tags"] = _clean_tags(kwargs.get("tags"))
        return original_trace_write(**kwargs)

    batchers.span_write.SpanWrite = safe_span_write
    batchers.trace_write.TraceWrite = safe_trace_write


def _install_track_harbor() -> None:
    from opik.integrations.harbor import track_harbor

    track_harbor(project_name=os.environ.get("OPIK_PROJECT_NAME"))


def _patch_trial_decorator_with_tb_tags() -> None:
    """Append `tb-run:<id>`, `tb-task:<id>`, `tb-trial:<id>` tags to
    every harbor trial trace. The default preprocessor doesn't read the
    TB env vars; without this patch, traces have only the harbor +
    agent-name tags and can't be cross-referenced with the realtime
    trace tree (which uses `tb-trial:` as its primary correlation key).
    """
    from opik.integrations.harbor.opik_tracker import HarborTrialRunDecorator

    original = HarborTrialRunDecorator._start_span_inputs_preprocessor

    def patched(self, func, track_options, args, kwargs):
        params = original(self, func, track_options, args, kwargs)
        extra: list[str] = []
        for env_key, prefix in (
            ("TB_RUN_ID", "tb-run:"),
            ("TB_TASK_ID", "tb-task:"),
            ("TB_TRIAL_ID", "tb-trial:"),
        ):
            value = os.environ.get(env_key)
            if value:
                extra.append(f"{prefix}{value}")
        params.tags = _clean_tags(params.tags) + extra
        return params

    HarborTrialRunDecorator._start_span_inputs_preprocessor = patched


def main() -> None:
    if os.environ.get("TB_ENVIRONMENT_TYPE", "docker").strip().lower() == "e2b":
        from e2b_runtime import patch_e2b_runtime_from_env

        patch_e2b_runtime_from_env()
    if _trace_to_opik_enabled():
        _patch_opik_batch_tags()
        _install_track_harbor()
        _patch_trial_decorator_with_tb_tags()

    from harbor.cli.main import app

    sys.argv = ["harbor", *sys.argv[1:]]
    app()


if __name__ == "__main__":
    main()
