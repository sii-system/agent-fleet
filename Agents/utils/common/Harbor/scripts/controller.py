#!/usr/bin/env python3
"""Submit and inspect Harbor controller user decisions."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object in {path}")
    return payload


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _notification_path(run_dir: Path) -> Path:
    return run_dir / "monitor" / "user-notify-latest.json"


def _decision_path(run_dir: Path) -> Path:
    return run_dir / "monitor" / "user-decision.json"


def _status(run_dir: Path) -> int:
    notification = _load_json(_notification_path(run_dir))
    fields = {
        "run_id": notification.get("run_id", run_dir.name),
        "controller_status": notification.get("controller_status"),
        "status_reason": notification.get("status_reason"),
        "allowed_decisions": notification.get("allowed_decisions", []),
        "decision_request_id": notification.get("decision_request_id"),
        "decision_status": notification.get("decision_status"),
        "submitted_decision": notification.get("submitted_decision"),
        "external_control_performed": notification.get("external_control_performed"),
    }
    print(json.dumps(fields, ensure_ascii=False, indent=2))
    return 0


def _decide(run_dir: Path, decision: str, wait_seconds: int) -> int:
    notification = _load_json(_notification_path(run_dir))
    allowed = notification.get("allowed_decisions")
    allowed_decisions = [str(value) for value in allowed] if isinstance(allowed, list) else []
    if notification.get("controller_status") != "awaiting_user_decision":
        raise ValueError("controller is not awaiting a user decision")
    if decision not in allowed_decisions:
        raise ValueError(
            f"decision {decision!r} is not allowed; allowed decisions: "
            f"{', '.join(allowed_decisions) or '<none>'}"
        )
    request_id = str(notification.get("decision_request_id") or "")
    if not request_id:
        raise ValueError("notification has no decision_request_id")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": "harbor_user_decision",
        "run_id": str(notification.get("run_id") or run_dir.name),
        "decision_id": f"decision-{uuid.uuid4()}",
        "decision_request_id": request_id,
        "decision": decision,
    }
    if decision == "wait":
        if wait_seconds <= 0:
            raise ValueError("wait-seconds must be positive")
        payload["wait_seconds"] = wait_seconds
    path = _decision_path(run_dir)
    _write_json_atomic(path, payload)
    print(json.dumps({"status": "submitted", "path": str(path), **payload}, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Harbor controller user interaction")
    parser.add_argument("--run-dir", required=True, type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    decide = subparsers.add_parser("decide")
    decide.add_argument("decision", choices=("wait", "restart", "stop"))
    decide.add_argument("--wait-seconds", type=int, default=300)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "status":
            return _status(args.run_dir)
        return _decide(args.run_dir, args.decision, args.wait_seconds)
    except (TypeError, ValueError) as exc:
        print(f"controller decision rejected: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
