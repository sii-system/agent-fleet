"""Redact credentials from a staged nightly artifact tree before uploading it."""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

SECRET_NAME = re.compile(r"(?:key|token(?:_id)?|secret|password|credentials?)$", re.IGNORECASE)
JSON_CONFIGS = (
    "HARBOR_LLM_KWARGS",
    "OPENCODE_CONFIG_CONTENT",
    "OPENCODE_RUNTIME_SECRETS_JSON",
)


def credential_values(environment):
    values = set()

    def collect(value, sensitive=False):
        if isinstance(value, dict):
            for key, child in value.items():
                collect(
                    child,
                    sensitive
                    or bool(SECRET_NAME.search(key))
                    or key.lower().endswith("headers"),
                )
        elif isinstance(value, list):
            for child in value:
                collect(child, sensitive)
        elif sensitive and isinstance(value, str) and value:
            values.add(value)

    for name, value in environment.items():
        if not value:
            continue
        if SECRET_NAME.search(name):
            values.add(value)
        if name in JSON_CONFIGS:
            collect(json.loads(value), name == "OPENCODE_RUNTIME_SECRETS_JSON")
        if name in ("ANTHROPIC_CUSTOM_HEADERS", "HARBOR_ANTHROPIC_CUSTOM_HEADERS"):
            for line in value.splitlines():
                _, separator, header = line.partition(":")
                if separator and header.strip():
                    values.add(header.strip())
        if "://" in value and "@" in value:
            try:
                password = urlsplit(value).password
            except ValueError:
                continue
            if password:
                values.add(password)
    return sorted((value.encode() for value in values), key=len, reverse=True)


def redact(directory, values):
    pattern = (
        re.compile(b"|".join(re.escape(value) for value in values)) if values else None
    )
    files = []
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError("symlink in staged artifacts")
        if any(value in os.fsencode(path.relative_to(directory)) for value in values):
            raise ValueError("credential in artifact filename")
        if path.is_file():
            files.append(path)
            data = path.read_bytes()
            replaced = pattern.sub(b"***", data) if pattern else data
            if replaced != data:
                path.write_bytes(replaced)
    for path in files:
        data = path.read_bytes()
        if any(value in data for value in values):
            raise ValueError("credential remains in staged artifacts")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        redact(args.directory, credential_values(os.environ))
    except (OSError, ValueError):
        # Parser errors and paths may contain credentials; do not print them.
        print("::error::Artifact redaction failed; refusing to upload", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
