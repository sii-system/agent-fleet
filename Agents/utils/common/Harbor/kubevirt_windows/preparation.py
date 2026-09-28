"""Deliver pinned local artifacts and prepare tools in a fresh Windows guest."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

from .transport import windows_path


class WindowsPreparation:
    def __init__(self, manifest):
        path = Path(manifest).expanduser().resolve()
        raw = path.read_bytes()
        spec = json.loads(raw)
        if (
            not isinstance(spec, dict)
            or type(spec.get("version")) is not int
            or spec["version"] != 1
            or spec.keys() - {"version", "files", "commands", "env"}
        ):
            raise ValueError("Expected a version 1 Windows preparation manifest")
        self.digest = hashlib.sha256(raw).hexdigest()
        self.env = spec.get("env", {})
        if not isinstance(self.env, dict) or any(
            not key
            or any(c in key for c in "=\x00")
            or not isinstance(value, str)
            or "\x00" in value
            for key, value in self.env.items()
        ):
            raise ValueError("Preparation env must contain valid string values")
        self.commands = spec.get("commands", [])
        if not isinstance(self.commands, list) or any(
            not isinstance(command, str) or not command.strip() or "\x00" in command
            for command in self.commands
        ):
            raise ValueError("Preparation commands must be a list of nonempty strings")
        files = spec.get("files", [])
        if not isinstance(files, list):
            raise TypeError("Preparation files must be a list")
        self.files = []
        for item in files:
            if (
                not isinstance(item, dict)
                or set(item) != {"source", "target", "sha256"}
                or not all(isinstance(value, str) for value in item.values())
                or not item["source"]
                or not re.fullmatch(r"[a-fA-F0-9]{64}", item["sha256"])
            ):
                raise ValueError(
                    "Each preparation file needs source, target and SHA256"
                )
            source = Path(item["source"]).expanduser()
            if not source.is_absolute():
                source = path.parent / source
            if source.is_symlink() or not source.is_file():
                raise ValueError("Preparation source must be a regular local file")
            self.files.append(
                (source, windows_path(item["target"]), item["sha256"].lower())
            )

    async def apply(self, environment, timeout):
        async with asyncio.timeout(timeout):
            # Validate the entire immutable cache input before guest mutation.
            for source, _, expected in self.files:
                actual = await asyncio.to_thread(self._checksum, source)
                if actual != expected:
                    raise ValueError("Windows preparation artifact checksum mismatch")
            for source, target, _ in self.files:
                await environment.upload_file(source, target)
            for index, command in enumerate(self.commands, 1):
                result = await environment.exec(
                    command, env=self.env, timeout_sec=timeout
                )
                if result.return_code:
                    raise RuntimeError(
                        f"Windows preparation command {index} failed (exit {result.return_code})"
                    )

    @staticmethod
    def _checksum(path):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
