#!/usr/bin/env python3
"""Generate docker-compose.yml and per-instance configs for N OpenClaw instances.

Usage: ./scripts/setup.py [COUNT] [options]   (default: 2)

Configuration precedence (highest first):
  1. Command-line flags and positional COUNT
  2. Environment variables already set by the caller
  3. config/fleet.env
  4. config.local.env
  5. config.env

This script replaces the previous Bash + jq/sed implementation. The Bash wrapper
(setup.sh) loads env files, re-applies caller env, and execs into this module so
existing callers and tests keep working unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

BASE_GW_PORT = 18789
CONTAINER_STATE_DIR = "/home/node/openclaw-state"
CONTAINER_WORKSPACE_DIR = "/home/node/workspace"
CONTAINER_CONFIG_PATH = f"{CONTAINER_STATE_DIR}/openclaw.json"
CONTAINER_OPENCLAW_HOME = "/home/node/.openclaw"

COMPOSE_FILE = PROJECT_DIR / "docker-compose.yml"
ENV_FILE = PROJECT_DIR / ".env"
TEMPLATE_FILE = PROJECT_DIR / "config" / "openclaw.json.template"


# ── Argument parsing ──────────────────────────────────────────────────────────

class _ParserError(Exception):
    pass


def _build_arg_parser(defaults: dict[str, str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup.py",
        description="Generate docker-compose.yml and per-instance OpenClaw configs.",
        add_help=True,
    )
    parser.add_argument("count", nargs="?", type=int, default=None, help="Number of instances")
    parser.add_argument("--sandbox_mode", default=None,
                        help=f"agents.defaults.sandbox.mode (default: {defaults['SANDBOX_MODE']})")
    parser.add_argument("--exec_security", default=None,
                        help=f"tools.exec.security (default: {defaults['EXEC_SECURITY']})")
    parser.add_argument("--exec_ask", default=None,
                        help=f"tools.exec.ask (default: {defaults['EXEC_ASK']})")
    parser.add_argument("--docker_compose_read_only", default=None,
                        help=f"compose read_only (default: {defaults['DOCKER_COMPOSE_READ_ONLY']})")
    return parser


# ── Config resolution ─────────────────────────────────────────────────────────

def trace_to_opik_enabled(value: str | None) -> bool:
    """Return whether fleet-wide tracing is enabled."""
    return (value or "true") not in {"false", "0"}


def resolve_config(env: dict[str, str], argv: list[str]) -> dict[str, Any]:
    """Resolve runtime configuration from env + CLI args. Pure-ish: only reads env/argv."""
    home = env.get("HOME", str(Path.home()))
    defaults = {
        "CONFIG_BASE": env.get("CONFIG_BASE", f"{home}/openclaw-instances"),
        "WORKSPACE_BASE": env.get("WORKSPACE_BASE", f"{home}/openclaw-workspaces"),
        "NPM_CACHE_DIR": env.get("NPM_CACHE_DIR", f"{home}/.npm"),
        "PLUGIN_CACHE_DIR": env.get("PLUGIN_CACHE_DIR", ""),
        "OPENCLAW_UID": env.get("OPENCLAW_UID", str(os.getuid())),
        "OPENCLAW_GID": env.get("OPENCLAW_GID", str(os.getgid())),
        "OPENCLAW_CONTAINER_USER": env.get("OPENCLAW_CONTAINER_USER", ""),
        "OPENCLAW_CONFIG_CHMOD": env.get("OPENCLAW_CONFIG_CHMOD", "a+rwX"),
        "OPENCLAW_CONFIG_DEFAULT_ACL": env.get("OPENCLAW_CONFIG_DEFAULT_ACL", "true"),
        "OPENCLAW_WORKSPACE_CHMOD": env.get("OPENCLAW_WORKSPACE_CHMOD", "a+rwX"),
        "OPENCLAW_WORKSPACE_DEFAULT_ACL": env.get("OPENCLAW_WORKSPACE_DEFAULT_ACL", "true"),
        "NPM_CONFIG_REGISTRY": env.get("NPM_CONFIG_REGISTRY", ""),
        "PIP_INDEX_URL": env.get("PIP_INDEX_URL", ""),
        "PIP_EXTRA_INDEX_URL": env.get("PIP_EXTRA_INDEX_URL", ""),
        "PIP_TRUSTED_HOST": env.get("PIP_TRUSTED_HOST", ""),
        "CPU_LIMIT": env.get("CPU_LIMIT", "2"),
        "MEM_LIMIT": env.get("MEM_LIMIT", "4G"),
        "MODEL": env.get("MODEL", "default-model"),
        "CONTAINER_NAME_PREFIX": env.get("CONTAINER_NAME_PREFIX", "openclaw"),
        "DEFAULT_PORTS_OFFSET": env.get("DEFAULT_PORTS_OFFSET", "0"),
        "PORT_STEP": env.get("PORT_STEP", "20"),
        "SANDBOX_MODE": env.get("SANDBOX_MODE", "off"),
        "HEARTBEAT_EVERY": env.get("HEARTBEAT_EVERY", "0m"),
        "EXEC_SECURITY": env.get("EXEC_SECURITY", "deny"),
        "EXEC_ASK": env.get("EXEC_ASK", "always"),
        "WORKSPACE_ONLY": env.get("WORKSPACE_ONLY", "true"),
        "DOCKER_COMPOSE_READ_ONLY": env.get("DOCKER_COMPOSE_READ_ONLY", "true"),
        "COUNT": env.get("COUNT", "2"),
        "BASE_URL": env.get("BASE_URL", ""),
        "API_KEY": env.get("API_KEY", ""),
        "TRACE_TO_OPIK": env.get("TRACE_TO_OPIK", "true"),
        "OPIK_PLUGIN": env.get("OPIK_PLUGIN", "disabled"),
        "OPIK_URL": env.get("OPIK_URL", ""),
        "OPIK_API_KEY": env.get("OPIK_API_KEY", ""),
        "OPIK_WORKSPACE": env.get("OPIK_WORKSPACE", "default"),
        "OPIK_PROJECT_NAME": env.get("OPIK_PROJECT_NAME", ""),
    }

    parser = _build_arg_parser(defaults)
    args = parser.parse_args(argv)

    cfg = dict(defaults)
    if args.sandbox_mode is not None:
        cfg["SANDBOX_MODE"] = args.sandbox_mode
    if args.exec_security is not None:
        cfg["EXEC_SECURITY"] = args.exec_security
    if args.exec_ask is not None:
        cfg["EXEC_ASK"] = args.exec_ask
    if args.docker_compose_read_only is not None:
        cfg["DOCKER_COMPOSE_READ_ONLY"] = args.docker_compose_read_only
    if args.count is not None:
        cfg["COUNT"] = str(args.count)

    cfg["COUNT"] = int(cfg["COUNT"])
    cfg["DEFAULT_PORTS_OFFSET"] = int(cfg["DEFAULT_PORTS_OFFSET"])
    cfg["PORT_STEP"] = int(cfg["PORT_STEP"])
    cfg["OPENCLAW_UID"] = int(cfg["OPENCLAW_UID"])
    cfg["OPENCLAW_GID"] = int(cfg["OPENCLAW_GID"])

    # TRACE_TO_OPIK is the fleet-wide kill switch. A stale subsystem-specific
    # OPIK_PLUGIN=enabled setting must not turn tracing back on.
    if not trace_to_opik_enabled(cfg["TRACE_TO_OPIK"]):
        cfg["OPIK_PLUGIN"] = "disabled"

    if cfg["DOCKER_COMPOSE_READ_ONLY"] not in ("true", "false"):
        raise _ParserError("--docker_compose_read_only must be 'true' or 'false'.")
    if cfg["WORKSPACE_ONLY"] not in ("true", "false"):
        raise _ParserError("WORKSPACE_ONLY must be 'true' or 'false'.")
    if cfg["OPENCLAW_CONFIG_DEFAULT_ACL"] not in ("true", "false"):
        raise _ParserError("OPENCLAW_CONFIG_DEFAULT_ACL must be 'true' or 'false'.")
    if cfg["OPENCLAW_WORKSPACE_DEFAULT_ACL"] not in ("true", "false"):
        raise _ParserError("OPENCLAW_WORKSPACE_DEFAULT_ACL must be 'true' or 'false'.")

    return cfg


def validate_required(cfg: dict[str, Any]) -> None:
    if not cfg["BASE_URL"] or not cfg["API_KEY"]:
        msg = (
            "Error: model provider is not configured.\n\n"
            "Set both BASE_URL and API_KEY before running setup.sh.\n\n"
            f"Option 1: set BASE_URL, API_KEY, MODEL in {PROJECT_DIR.parent.parent}/config.env "
            f"(COUNT in {PROJECT_DIR}/config/fleet.env)\n"
            f'Option 2: BASE_URL="https://api.example.com" API_KEY="sk-xxx" '
            f'MODEL="{cfg["MODEL"]}" {SCRIPT_DIR}/setup.sh {cfg["COUNT"]}'
        )
        raise _ParserError(msg)

    if cfg["OPIK_PLUGIN"] == "enabled" and (
        not cfg["OPIK_URL"] or not cfg["OPIK_PROJECT_NAME"]
    ):
        raise _ParserError(
            "Error: OPIK_PLUGIN is enabled but OPIK_URL and OPIK_PROJECT_NAME are required.\n"
            f'  OPIK_PLUGIN=enabled OPIK_URL="https://opik.example.com/api/" '
            f'OPIK_PROJECT_NAME="my-project" {SCRIPT_DIR}/setup.sh {cfg["COUNT"]}'
        )

    if not TEMPLATE_FILE.exists():
        raise _ParserError(f"Error: template not found: {TEMPLATE_FILE}")


# ── openclaw.json rendering ───────────────────────────────────────────────────

def normalize_base_url(url: str) -> str:
    """Return the OpenAI-compatible base URL with a single ``/v1`` path suffix.

    config.env documents BASE_URL as the API root (no version suffix) so one
    shared value works across runners; Harbor appends ``/v1`` itself, and
    OpenClaw's provider needs the ``/v1`` endpoint. Idempotent: a path already
    ending in ``/v1`` is left unchanged. Any query/fragment is preserved."""
    if not url:
        return url
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if not path.endswith("/v1"):
        path = f"{path}/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def build_openclaw_config(template: dict[str, Any], cfg: dict[str, Any], *, token: str,
                           gw_port: int) -> dict[str, Any]:
    """Apply per-instance edits to the template config. Pure function."""
    result = json.loads(json.dumps(template))  # deep copy

    result["gateway"]["auth"] = {"mode": "token", "token": token}
    result["gateway"]["controlUi"] = {
        "allowedOrigins": [
            f"http://127.0.0.1:{gw_port}",
            f"http://localhost:{gw_port}",
        ],
    }

    provider = result["models"]["providers"]["default"]
    provider["baseUrl"] = normalize_base_url(cfg["BASE_URL"])
    provider["apiKey"] = cfg["API_KEY"]
    provider["models"][0]["id"] = cfg["MODEL"]
    provider["models"][0]["name"] = cfg["MODEL"]

    defaults = result["agents"]["defaults"]
    defaults["workspace"] = CONTAINER_WORKSPACE_DIR
    defaults["sandbox"]["mode"] = cfg["SANDBOX_MODE"]
    defaults["heartbeat"]["every"] = cfg["HEARTBEAT_EVERY"]

    result["tools"]["exec"]["security"] = cfg["EXEC_SECURITY"]
    result["tools"]["exec"]["ask"] = cfg["EXEC_ASK"]
    result["tools"]["fs"]["workspaceOnly"] = cfg["WORKSPACE_ONLY"] == "true"

    if cfg["OPIK_PLUGIN"] == "enabled":
        result["plugins"]["allow"] = ["openai", "openclaw-opik-tracer"]
        result["plugins"]["load"] = {
            "paths": ["/opt/openclaw-plugins/openclaw-opik-tracer"],
        }
        result["plugins"]["entries"] = {
            "openclaw-opik-tracer": {
                "enabled": True,
                "hooks": {
                    "allowConversationAccess": True,
                },
                "config": {
                    "opikUrl": cfg["OPIK_URL"],
                    "opikApiKey": cfg["OPIK_API_KEY"],
                    "opikWorkspace": cfg["OPIK_WORKSPACE"],
                    "opikProjectName": cfg["OPIK_PROJECT_NAME"],
                    "pythonPath": "/opt/opik-venv/bin/python",
                    "tags": ["openclaw", "local"],
                    "includeHistory": False,
                    "dryRun": False,
                },
            },
        }

    return result


def write_openclaw_json(path: Path, config: dict[str, Any]) -> None:
    """Write openclaw.json with the same 2-space indent + trailing newline as the
    previous jq output, so byte-level diffs against the old setup.sh stay clean."""
    with path.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


# ── docker-compose.yml rendering ──────────────────────────────────────────────

def yaml_double_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_compose_service(svc: str, *, token_var: str, gw_port: int, image_default: str,
                           config_dir: Path, workspace_dir: Path, opik_state_dir: Path | None,
                           openclaw_home_dir: Path,
                           npm_cache: Path, plugin_cache: Path | None,
                           cfg: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"  {svc}:")
    lines.append(f"    image: ${{OPENCLAW_IMAGE:-{image_default}}}")
    lines.append("    pull_policy: never")
    lines.append(f"    container_name: {svc}")
    if cfg.get("OPENCLAW_CONTAINER_USER"):
        lines.append(f'    user: "{cfg["OPENCLAW_CONTAINER_USER"]}:{cfg["OPENCLAW_GID"]}"')
    else:
        lines.append(f'    user: "{cfg["OPENCLAW_UID"]}:{cfg["OPENCLAW_GID"]}"')
    lines.append("    networks:")
    lines.append(f"      - net-{svc}")
    lines.append("    environment:")
    lines.append("      HOME: /home/node")
    lines.append("      TERM: xterm-256color")
    lines.append(f"      OPENCLAW_STATE_DIR: {CONTAINER_STATE_DIR}")
    lines.append(f"      OPENCLAW_CONFIG_PATH: {CONTAINER_CONFIG_PATH}")
    lines.append(f"      OPENCLAW_WORKSPACE_DIR: {CONTAINER_WORKSPACE_DIR}")
    lines.append(f'      OPENCLAW_GATEWAY_TOKEN: "${{{token_var}}}"  # injected at runtime from .env')
    lines.append('      TZ: "${TZ:-Asia/Shanghai}"')
    for key in ("NPM_CONFIG_REGISTRY", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST"):
        if cfg.get(key):
            lines.append(f"      {key}: {yaml_double_quote(str(cfg[key]))}")
    if cfg["OPIK_PLUGIN"] == "enabled":
        lines.append('      OC_OPIK_PROCESS_TIMEOUT_S: "60"')
    lines.append("    volumes:")
    lines.append(f"      - {config_dir}:{CONTAINER_STATE_DIR}         # per-instance OpenClaw state")
    lines.append(f"      - {workspace_dir}:{CONTAINER_WORKSPACE_DIR}  # per-instance agent workspace")
    lines.append(f"      - {npm_cache}:/home/node/.npm             # shared npm cache (read/write safe)")
    lines.append(f"      - {openclaw_home_dir}:{CONTAINER_OPENCLAW_HOME}  # writable .openclaw (exec tool needs chmod)")
    if cfg["OPIK_PLUGIN"] == "enabled" and opik_state_dir is not None:
        lines.append(f"      - {opik_state_dir}:/home/node/.openclaw/state  # opik tracer state only")
    if plugin_cache is not None and plugin_cache.is_dir():
        lines.append(f"      - {plugin_cache}:/opt/plugin-cache:ro   # shared plugin cache (read-only)")
    lines.append("    ports:")
    lines.append(f'      - "{gw_port}:{BASE_GW_PORT}"')
    lines.append("    deploy:")
    lines.append("      resources:")
    lines.append("        limits:")
    lines.append(f'          cpus: "{cfg["CPU_LIMIT"]}"')
    lines.append(f"          memory: {cfg['MEM_LIMIT']}")
    lines.append("    init: true                        # reap zombie processes")
    lines.append("    restart: unless-stopped")
    lines.append("    cap_drop:")
    lines.append("      - ALL                           # drop all Linux capabilities")
    lines.append("    security_opt:")
    lines.append("      - no-new-privileges:true")
    lines.append(f"    read_only: {cfg['DOCKER_COMPOSE_READ_ONLY']}")
    lines.append("    tmpfs:")
    lines.append("      - /tmp                          # ephemeral scratch space")
    lines.append("    command:")
    lines.append(f'      ["node", "dist/index.js", "gateway", "--bind", "lan", "--port", "{BASE_GW_PORT}"]')
    lines.append("    healthcheck:")
    lines.append("      test:")
    lines.append('        ["CMD", "node", "-e",')
    lines.append(
        f"         \"fetch('http://127.0.0.1:{BASE_GW_PORT}/healthz')"
        f".then((r)=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))\"]"
    )
    lines.append("      interval: 30s")
    lines.append("      timeout: 5s")
    lines.append("      retries: 5")
    lines.append("      start_period: 20s")
    lines.append("")  # blank line between services
    return "\n".join(lines) + "\n"


COMPOSE_HEADER = "# Auto-generated by setup.sh -- do not edit manually\nservices:\n"


def render_networks(prefix: str, count: int) -> str:
    out: list[str] = ["", "networks:"]
    for i in range(1, count + 1):
        out.append(f"  net-{prefix}-{i}:")
        out.append("    driver: bridge")
    return "\n".join(out) + "\n"


# ── .env handling ─────────────────────────────────────────────────────────────

def load_existing_tokens(env_path: Path) -> dict[str, str]:
    tokens: dict[str, str] = {}
    if not env_path.exists():
        return tokens
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("TOKEN_") and "=" in line:
            key, _, value = line.partition("=")
            tokens[key] = value
    return tokens


# ── Permissions ───────────────────────────────────────────────────────────────

def ensure_owner(target: Path, *, uid: int, gid: int) -> None:
    if sys.platform != "linux":
        return
    if shutil.which("chown") is None:
        return
    try:
        stat = target.stat()
        if stat.st_uid == uid and stat.st_gid == gid:
            return
    except FileNotFoundError:
        return

    try:
        subprocess.run(
            ["chown", "-R", f"{uid}:{gid}", str(target)],
            check=True, capture_output=True,
        )
        return
    except subprocess.CalledProcessError:
        pass

    if shutil.which("sudo") is not None:
        try:
            subprocess.run(
                ["sudo", "-n", "chown", "-R", f"{uid}:{gid}", str(target)],
                check=True, capture_output=True,
            )
            return
        except subprocess.CalledProcessError:
            pass

    raise SystemExit(
        f"Error: cannot chown {target} to uid {uid} gid {gid} (container user).\n"
        "Re-run setup.sh with sudo:\n"
        "  sudo ./Agents/Openclaw/scripts/setup.sh"
    )


def run_permission_command(cmd: list[str]) -> bool:
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError:
        pass

    if shutil.which("sudo") is not None:
        try:
            subprocess.run(["sudo", "-n", *cmd], check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError:
            pass

    return False


def ensure_mount_permissions(target: Path, *, chmod_spec: str, default_acl: bool) -> None:
    if sys.platform != "linux" or not chmod_spec:
        return
    if shutil.which("chmod") is None or not target.exists():
        return

    if not run_permission_command(["chmod", "-R", chmod_spec, str(target)]):
        raise SystemExit(
            f"Error: cannot chmod {target} with mode {chmod_spec}.\n"
            "Re-run setup.sh with sudo:\n"
            "  sudo ./Agents/Openclaw/scripts/setup.sh"
        )

    if default_acl and shutil.which("find") is not None and shutil.which("setfacl") is not None:
        run_permission_command([
            "find", str(target), "-type", "d",
            "-exec", "setfacl", "-m", "d:u::rwx,d:g::rwx,d:o::rwx", "{}", "+",
        ])


# ── Main ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    try:
        cfg = resolve_config(dict(os.environ), argv)
        validate_required(cfg)
    except _ParserError as e:
        print(str(e), file=sys.stderr)
        return 1

    count = cfg["COUNT"]
    print(f"Generating {count} instance(s)...")

    image_default = "openclaw:local-opik" if cfg["OPIK_PLUGIN"] == "enabled" else "openclaw:local"

    config_base = Path(cfg["CONFIG_BASE"]).expanduser()
    workspace_base = Path(cfg["WORKSPACE_BASE"]).expanduser()
    npm_cache = Path(cfg["NPM_CACHE_DIR"]).expanduser()
    plugin_cache_str = cfg["PLUGIN_CACHE_DIR"]
    plugin_cache = Path(plugin_cache_str).expanduser() if plugin_cache_str else None

    template = json.loads(TEMPLATE_FILE.read_text(encoding="utf-8"))
    existing_tokens = load_existing_tokens(ENV_FILE)
    prefix = cfg["CONTAINER_NAME_PREFIX"]

    compose_parts = [COMPOSE_HEADER]
    env_lines: list[str] = []

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for i in range(1, count + 1):
        gw_port = BASE_GW_PORT + cfg["DEFAULT_PORTS_OFFSET"] + (i - 1) * cfg["PORT_STEP"]
        svc = f"{prefix}-{i}"
        token_var = f"TOKEN_{i}"
        config_dir = config_base / str(i)
        workspace_dir = workspace_base / str(i)
        opik_state_dir = config_dir / "opik-state" if cfg["OPIK_PLUGIN"] == "enabled" else None

        config_dir.mkdir(parents=True, exist_ok=True)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        openclaw_home_dir = config_dir / "openclaw-home"
        openclaw_home_dir.mkdir(parents=True, exist_ok=True)
        # Recreate the agents symlink that the opik image places at
        # /home/node/.openclaw/agents -> /home/node/openclaw-state/agents.
        # We use a relative target so it resolves correctly both on the host
        # ({config_dir}/openclaw-home/agents -> ../openclaw-state/agents) and
        # inside the container (/home/node/.openclaw/agents -> ../openclaw-state/agents).
        agents_link = openclaw_home_dir / "agents"
        if agents_link.exists() or agents_link.is_symlink():
            agents_link.unlink()
        agents_link.symlink_to("../openclaw-state/agents")
        if opik_state_dir is not None:
            opik_state_dir.mkdir(parents=True, exist_ok=True)

        ws_state_dir = workspace_dir / ".openclaw"
        ws_state_dir.mkdir(parents=True, exist_ok=True)
        (ws_state_dir / "workspace-state.json").write_text(
            json.dumps({"version": 1, "setupCompletedAt": timestamp}) + "\n",
            encoding="utf-8",
        )

        token = existing_tokens.get(token_var) or secrets.token_hex(32)
        env_lines.append(f"{token_var}={token}")

        config = build_openclaw_config(template, cfg, token=token, gw_port=gw_port)
        write_openclaw_json(config_dir / "openclaw.json", config)

        ensure_owner(config_dir, uid=cfg["OPENCLAW_UID"], gid=cfg["OPENCLAW_GID"])
        ensure_owner(workspace_dir, uid=cfg["OPENCLAW_UID"], gid=cfg["OPENCLAW_GID"])
        ensure_owner(openclaw_home_dir, uid=cfg["OPENCLAW_UID"], gid=cfg["OPENCLAW_GID"])
        ensure_mount_permissions(
            config_dir,
            chmod_spec=cfg["OPENCLAW_CONFIG_CHMOD"],
            default_acl=cfg["OPENCLAW_CONFIG_DEFAULT_ACL"] == "true",
        )
        ensure_mount_permissions(
            workspace_dir,
            chmod_spec=cfg["OPENCLAW_WORKSPACE_CHMOD"],
            default_acl=cfg["OPENCLAW_WORKSPACE_DEFAULT_ACL"] == "true",
        )
        if opik_state_dir is not None:
            ensure_owner(opik_state_dir, uid=cfg["OPENCLAW_UID"], gid=cfg["OPENCLAW_GID"])
            ensure_mount_permissions(
                opik_state_dir,
                chmod_spec=cfg["OPENCLAW_CONFIG_CHMOD"],
                default_acl=cfg["OPENCLAW_CONFIG_DEFAULT_ACL"] == "true",
            )

        compose_parts.append(render_compose_service(
            svc, token_var=token_var, gw_port=gw_port, image_default=image_default,
            config_dir=config_dir, workspace_dir=workspace_dir,
            opik_state_dir=opik_state_dir, openclaw_home_dir=openclaw_home_dir,
            npm_cache=npm_cache, plugin_cache=plugin_cache, cfg=cfg,
        ))

    compose_parts.append(render_networks(prefix, count))
    COMPOSE_FILE.write_text("".join(compose_parts), encoding="utf-8")

    env_lines.append(f"CONTAINER_NAME_PREFIX={prefix}")
    if cfg["OPIK_PLUGIN"] == "enabled":
        env_lines.append(f"OPIK_PLUGIN={cfg['OPIK_PLUGIN']}")
    ENV_FILE.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    print()
    print("Generated:")
    print(f"  Compose:  {COMPOSE_FILE}")
    print(f"  Env:      {ENV_FILE}")
    print(f"  Configs:  {config_base}/{{1..{count}}}/openclaw.json")
    print()
    print("Instance ports:")
    for i in range(1, count + 1):
        gw_port = BASE_GW_PORT + cfg["DEFAULT_PORTS_OFFSET"] + (i - 1) * cfg["PORT_STEP"]
        print(f"  {prefix}-{i}: gateway={gw_port}")
    print()
    print(f"Run:  cd {PROJECT_DIR} && docker compose up -d")
    print(f"Stop: cd {PROJECT_DIR} && docker compose down")
    if cfg["OPIK_PLUGIN"] == "enabled":
        print()
        print(f"Opik tracer: enabled (project: {cfg['OPIK_PROJECT_NAME']})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
