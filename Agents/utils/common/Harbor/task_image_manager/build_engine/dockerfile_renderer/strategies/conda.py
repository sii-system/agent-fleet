"""Reviewed conda channels and a build-only system condarc."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

CONDA_CONFIG_SECRET_PREFIX = "opensandbox-conda-config"

# Match whole URL tokens before interpreting origins, so credential-bearing,
# queried, lookalike and tokenized channels cannot be partially rewritten.
_URL = re.compile(r"https?://[^\s\"'<>`\\;|&()\[\]{},]+")
_DEFAULT_ORIGINS = {
    "repo.anaconda.com": "/pkgs/",
    "repo.continuum.io": "/pkgs/",
    "mirrors.tuna.tsinghua.edu.cn": "/anaconda/pkgs/",
}
_CHANNEL_ORIGINS = {
    "conda.anaconda.org": "/",
    "mirrors.tuna.tsinghua.edu.cn": "/anaconda/cloud/",
}


def rewrite_conda_source_urls(
    source: str, defaults_url: str = "", channels_url: str = ""
) -> str:
    """Preserve channel order/specs; change only reviewed channel transports."""
    def replace(match: re.Match[str]) -> str:
        value = match.group()
        parsed = urlsplit(value)
        if parsed.username or parsed.password or "?" in value or "#" in value:
            return value
        if parsed.netloc != parsed.hostname:
            return value
        for origins, target, allowed in (
            (_DEFAULT_ORIGINS, defaults_url, {"main", "r"}),
            (_CHANNEL_ORIGINS, channels_url, {"conda-forge"}),
        ):
            prefix = origins.get(parsed.hostname or "")
            if not target or not prefix or not parsed.path.startswith(prefix):
                continue
            path = parsed.path[len(prefix):]
            parts = path.rstrip("/").split("/")
            if parts[0] not in allowed:
                continue
            if len(parts) > 1 and parts[1] not in {
                "linux-64", "noarch", "channeldata.json", "notices.json"
            }:
                continue
            if any(part in {".", ".."} for part in parts):
                continue
            return target.rstrip("/") + "/" + path
        return value

    return _URL.sub(replace, source)


def materialize_conda_config(
    destination: Path, defaults_url: str = "", channels_url: str = ""
) -> dict[str, Path]:
    """Use normal conda/mamba rc precedence, without changing active channels."""
    config: dict[str, object] = {}
    if defaults_url:
        config["default_channels"] = [
            defaults_url.rstrip("/") + "/main",
            defaults_url.rstrip("/") + "/r",
        ]
    if channels_url:
        config["custom_channels"] = {"conda-forge": channels_url.rstrip("/")}
    if not config:
        return {}
    # JSON is a YAML subset accepted by both conda and libmamba.
    content = json.dumps(config, sort_keys=True, indent=2) + "\n"
    identity = CONDA_CONFIG_SECRET_PREFIX + "-" + hashlib.sha256(content.encode()).hexdigest()
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "condarc.yaml"
    path.write_text(content, encoding="utf-8")
    return {identity: path}
