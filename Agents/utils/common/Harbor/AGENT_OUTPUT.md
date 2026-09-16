# Bounded agent command output

The main Claude Code, OpenCode, Pi and DSH commands keep complete stdout and
stderr in `/logs/agent/command.stdout` and `command.stderr`. Their existing
agent-specific logs are preserved. This adds a disk copy of the command output
but avoids retaining that entire copy in Harbor and the sandbox SDK on the host.
The wrapper applies with Opik enabled or disabled.

`agent_output.py` returns at most 256 KiB of raw diagnostic output per main
command (split between stdout and stderr, with room for classification metadata).
On failure it scans 64 KiB chunks of the complete logs to preserve Harbor 0.18's
API/rate-limit/network exception classification and pattern priority. Failed
runs with large logs can therefore require additional sandbox requests.
The current classifiers are audited explicitly; an unknown classifier logs a
warning and uses the original full-capture path to avoid changing retries.

Returned console output is a completion-time tail. Complete logs remain in the
sandbox for artifact collection and existing file-based readers. The helper
does not provide a new live-output transport. Generic setup, verifier and other
`exec()` calls retain their full-output contract. Artifact downloads need their
own memory bound, especially on OpenSandbox; this change does not bound them.

The wrapper preserves the original compound command, exit status, timeout,
user, cwd and scoped environment. It isolates explicit `exit`/`exec` in a
subshell; OpenCode finalization and DSH service restoration keep their existing
behavior. Cancellation remains the backend's responsibility; partial output
files remain available for Harbor's recovery and collection path.

Regression coverage: `tests/test_agent_output.py` and the four agent suites.
