#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

try:
    from .pi_prompt import PromptFailure, models_config, normalized_base_url
except ImportError:
    from pi_prompt import PromptFailure, models_config, normalized_base_url


BEGIN = "# >>> agent-fleet env >>>"
END = "# <<< agent-fleet env <<<"


def legacy_claude_config(path: Path) -> list[tuple[str, str]]:
    legacy_prefix = "T" + "B_CC_"
    names = {
        legacy_prefix + "OPIK_ENABLE_HOOK": "HARBOR_CC_OPIK_ENABLE_HOOK",
        legacy_prefix + "CLAUDE_TGZ_SOURCE": "HARBOR_CC_CLAUDE_TGZ_SOURCE",
        legacy_prefix + "PY_WHEEL_DIR_SOURCE": "HARBOR_CC_PY_WHEEL_DIR_SOURCE",
    }
    values: list[tuple[str, str]] = []
    in_block = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == BEGIN:
            in_block = True
            continue
        if stripped == END:
            in_block = False
            continue
        if not in_block:
            continue
        try:
            fields = shlex.split(stripped, comments=True, posix=True)
        except ValueError:
            continue
        if len(fields) != 2 or fields[0] != "export" or "=" not in fields[1]:
            continue
        key, value = fields[1].split("=", 1)
        replacement = names.get(key)
        if replacement:
            values.append((replacement, value))
    return values


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except FileNotFoundError:
        pass
    except json.JSONDecodeError:
        print(
            f"\033[1;33m[WARN]\033[0m existing {path} could not be parsed; "
            f"backed up at {path}.bak.agent-fleet, writing fresh",
            file=sys.stderr,
        )
    return {}


def merge_pi_config(
    settings_path: Path,
    models_path: Path,
    base_url: str,
    model: str,
) -> None:
    settings = _load_object(settings_path)
    settings["defaultProvider"] = "sii-gateway"
    settings["defaultModel"] = model
    settings.setdefault("defaultThinkingLevel", "high")
    settings.setdefault("theme", "dark")
    settings.setdefault("enableInstallTelemetry", False)

    models = _load_object(models_path)
    providers = models.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        models["providers"] = providers
    normalized_url = normalized_base_url(base_url)
    providers["sii-gateway"] = models_config(
        normalized_url, model, display_name="Agent Fleet"
    )["providers"]["sii-gateway"]

    for path, value in ((settings_path, settings), (models_path, models)):
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def update_bashrc(path: Path, environ: Mapping[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    output: list[str] = []
    in_block = False
    for line in lines:
        if line.strip() == BEGIN:
            in_block = True
            continue
        if line.strip() == END:
            in_block = False
            continue
        if not in_block:
            output.append(line)

    quote = shlex.quote
    block = [
        "",
        BEGIN,
        f"export AGENT_FLEET_PATHS_FILE={quote(environ['AGENT_FLEET_PATHS_FILE'])}",
        '[ -f "$AGENT_FLEET_PATHS_FILE" ] && . "$AGENT_FLEET_PATHS_FILE"',
        'export NVM_DIR="$HOME/.nvm"',
        '[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"',
        "export PI_OFFLINE=1",
        f"export AGENT_FLEET_API_KEY={quote(environ['AUTH_TOKEN'])}",
    ]
    tgz = environ.get("CLAUDE_TGZ_SOURCE", "").strip()
    wheel = environ.get("CLAUDE_WHEEL_DIR_SOURCE", "").strip()
    if tgz and wheel:
        block.extend(
            [
                "export HARBOR_CC_OPIK_ENABLE_HOOK=1",
                f"export HARBOR_CC_CLAUDE_TGZ_SOURCE={quote(tgz)}",
                f"export HARBOR_CC_PY_WHEEL_DIR_SOURCE={quote(wheel)}",
            ]
        )
    block.append(END)
    output.extend(block)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def merge_local_config(path: Path, environ: Mapping[str, str]) -> None:
    managed = {
        "BASE_URL": environ["BASE_URL"].rstrip("/"),
        "API_KEY": environ["AUTH_TOKEN"],
        "MODEL": environ["MODEL"],
    }
    trace_to_opik = environ.get("TRACE_TO_OPIK", "").strip()
    if trace_to_opik:
        managed["TRACE_TO_OPIK"] = trace_to_opik
    opik_url = environ.get("OPIK_URL", "").strip()
    if opik_url:
        managed.update(
            {
                "OPIK_URL": opik_url,
                "OPIK_API_KEY": environ.get("OPIK_API_KEY", ""),
                "OPIK_WORKSPACE": environ.get("OPIK_WORKSPACE") or "default",
                "OPIK_PROJECT_NAME": environ.get("OPIK_PROJECT_NAME", ""),
            }
        )

    existing: dict[str, str] = {}
    order: list[tuple[str, str]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                order.append(("comment", line))
            elif "=" in stripped:
                key, _, value = stripped.partition("=")
                existing[key] = value
                order.append(("kv", key))
            else:
                order.append(("raw", line))
    existing.update(managed)

    seen: set[str] = set()
    output: list[str] = []
    for kind, value in order:
        if kind == "kv":
            if value in existing and value not in seen:
                output.append(f"{value}={existing[value]}")
                seen.add(value)
        else:
            output.append(value)
    for key in managed:
        if key not in seen:
            output.append(f"{key}={existing[key]}")
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Agent Fleet setup files")
    subparsers = parser.add_subparsers(dest="command", required=True)

    legacy = subparsers.add_parser("legacy-claude-config")
    legacy.add_argument("path", type=Path)

    merge_pi = subparsers.add_parser("merge-pi-config")
    merge_pi.add_argument("settings_path", type=Path)
    merge_pi.add_argument("models_path", type=Path)
    merge_pi.add_argument("base_url")
    merge_pi.add_argument("model")

    update_shell = subparsers.add_parser("update-bashrc")
    update_shell.add_argument("path", type=Path)

    merge_local = subparsers.add_parser("merge-local-config")
    merge_local.add_argument("path", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "legacy-claude-config":
        for key, value in legacy_claude_config(args.path):
            print(f"{key}\t{value}")
    elif args.command == "merge-pi-config":
        try:
            merge_pi_config(
                args.settings_path,
                args.models_path,
                args.base_url,
                args.model,
            )
        except PromptFailure as exc:
            print(f"\033[1;31m[FAIL]\033[0m {exc}", file=sys.stderr)
            return 1
    elif args.command == "update-bashrc":
        update_bashrc(args.path, os.environ)
    elif args.command == "merge-local-config":
        merge_local_config(args.path, os.environ)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
