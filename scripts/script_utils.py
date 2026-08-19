#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from urllib.parse import urlparse


def url_hostname(value: str) -> str:
    return urlparse(value).hostname or ""


def verify_sha256(source: Path, checksum: Path) -> bool:
    expected = checksum.read_text(encoding="utf-8").split()[0].lower()
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected


def main() -> int:
    parser = argparse.ArgumentParser(description="Helpers for Agent Fleet shell scripts")
    subparsers = parser.add_subparsers(dest="command", required=True)
    hostname = subparsers.add_parser("url-hostname")
    hostname.add_argument("url")
    checksum = subparsers.add_parser("verify-sha256")
    checksum.add_argument("source", type=Path)
    checksum.add_argument("checksum", type=Path)
    args = parser.parse_args()

    if args.command == "url-hostname":
        print(url_hostname(args.url))
        return 0
    return 0 if verify_sha256(args.source, args.checksum) else 1


if __name__ == "__main__":
    raise SystemExit(main())
