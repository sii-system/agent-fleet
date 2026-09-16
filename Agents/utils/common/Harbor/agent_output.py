"""Bound host capture for agent commands without changing generic exec semantics.

The sandbox keeps full command streams as artifacts. Only tails cross the exec
transport on success; failures additionally scan fixed-size chunks to preserve
Harbor 0.18's error classification. No sandbox Python installation is required.
"""

from __future__ import annotations

import base64
import codecs
import logging
import re
import shlex
from pathlib import PurePosixPath

OUTPUT_BUDGET_BYTES = 256 * 1024
READ_CHUNK_BYTES = 64 * 1024
# Reserved for the classification witness and diagnostic framing.
TAIL_BYTES = (OUTPUT_BUDGET_BYTES - 1024) // 2
# Audit this allowlist when updating Harbor. Unknown classifiers retain the
# original execution path instead of silently changing retry semantics.
_SUPPORTED_PATTERNS = {
    r"rate.?limit", r"too many requests", r"specified API usage limits",
    r"Quota exceeded.", r"API Error: 500 Internal server error",
    r"API Error: Overloaded", r"API Error: Connection closed mid-response",
    r"API Error", r"SSL_ERROR_SYSCALL", r"SSL_connect",
    r"Could not resolve host", r"Connection refused", r"Connection timed out",
    r"curl: \(\d+\)",
}
_CURL_PATTERN = r"curl: \(\d+\)"


class _FailureScanner:
    """Match supported patterns across chunks with constant retained state."""

    def __init__(self, patterns):
        self.patterns = patterns
        self.carry = ""
        self.best = len(patterns)
        self.witness = ""

    def feed(self, text: str) -> None:
        text = self.carry + text
        for index, (pattern, _exception) in enumerate(self.patterns[:self.best]):
            match = pattern.search(text)
            if match:
                self.best = index
                # This pattern has unbounded width. The canonical witness
                # preserves classification without copying a huge digit run.
                self.witness = (
                    "curl: (0)" if pattern.pattern == _CURL_PATTERN else match.group()
                )
                break
        # Finite supported patterns are shorter than 256 characters. Preserve
        # the state of an unfinished curl error with an arbitrarily long code.
        pending_curl = re.search(r"curl: \(\d+$", text, re.IGNORECASE)
        if pending_curl:
            self.carry = "curl: (0"
        else:
            self.carry = text[-256:]


class _LoggedEnvironment:
    def __init__(self, environment, patterns, log_dir: str):
        self._environment = environment
        self._patterns = patterns
        self._log_dir = PurePosixPath(log_dir)

    def __getattr__(self, name):
        return getattr(self._environment, name)

    async def exec(self, command: str, **kwargs):
        stdout_path = shlex.quote(str(self._log_dir / "command.stdout"))
        stderr_path = shlex.quote(str(self._log_dir / "command.stderr"))
        # The subshell preserves explicit exit/exec and pipeline semantics.
        # Do not put it in an if/|| condition: Bash would disable errexit in
        # the original command. Existing agent-specific tee files stay intact.
        wrapped = (
            f"mkdir -p {shlex.quote(str(self._log_dir))} || exit $?; "
            "set +e; (\n" + command + "\n) "
            f">{stdout_path} 2>{stderr_path}; "
            "harbor_capture_rc=$?; "
            f"tail -c {TAIL_BYTES} -- {stdout_path}; "
            f"tail -c {TAIL_BYTES} -- {stderr_path} >&2; "
            'exit "$harbor_capture_rc"'
        )
        result = await self._environment.exec(command=wrapped, **kwargs)
        if result.return_code and self._patterns:
            scanner = _FailureScanner(self._patterns)
            for path in (stdout_path, stderr_path):
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                offset = 0
                while True:
                    chunk = await self._environment.exec(
                        command=(
                            "set -o pipefail; "
                            f"dd if={path} bs={READ_CHUNK_BYTES} skip={offset} "
                            "count=1 status=none | base64 -w0"
                        ),
                        user=kwargs.get("user"),
                        timeout_sec=30,
                    )
                    if chunk.return_code:
                        raise RuntimeError("Cannot classify failed agent: command log read failed")
                    data = base64.b64decode(chunk.stdout or "", validate=True)
                    if len(data) > READ_CHUNK_BYTES:
                        raise RuntimeError("Agent command log read exceeded chunk limit")
                    scanner.feed(decoder.decode(data, final=len(data) < READ_CHUNK_BYTES))
                    if len(data) < READ_CHUNK_BYTES or scanner.best == 0:
                        break
                    offset += 1
                scanner.feed("\n")
                if scanner.best == 0:
                    break
            if scanner.witness:
                result.stderr = (
                    f"[Harbor error classification: {scanner.witness}]\n"
                    + (result.stderr or "")
                )
        return result


async def exec_logged_agent(
    agent, environment, command: str, *, executor=None,
    log_dir: str = "/logs/agent", **kwargs,
):
    """Run one main agent invocation; keep the agent's own error handling."""
    patterns = getattr(agent, "_compiled_error_patterns", ())
    supported = all(
        pattern.pattern in _SUPPORTED_PATTERNS
        and pattern.flags == (re.IGNORECASE | re.UNICODE)
        for pattern, _exception in patterns
    )
    if supported:
        environment = _LoggedEnvironment(environment, patterns, log_dir)
    else:
        logging.getLogger(__name__).warning(
            "Unrecognized Harbor error classifier; retaining full command capture"
        )
    execute = executor if executor is not None else agent.exec_as_agent
    return await execute(environment, command=command, **kwargs)
