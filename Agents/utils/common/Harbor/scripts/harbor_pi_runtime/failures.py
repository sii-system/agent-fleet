"""Classify Pi failures before repeating an unchanged model request."""

from __future__ import annotations

import re

_PERMANENT_PROVIDER_ERROR = re.compile(
    r"(?:^|_)(?:400|401|403|404|413|422)(?:_|$)"
    r"|authentication_error|authenticationerror|permission_denied|permissiondeniederror"
    r"|not_found_error|notfounderror|model_not_found"
    r"|context_window_exceeded|contextwindowexceedederror"
    r"|request_body_exceeded|request_too_large|badrequesterror"
)


def retryable_pi_failure(reason: str) -> bool:
    """Retry transient provider failures and output failures a new turn can repair."""
    if reason.startswith("pi_provider_request_failed:"):
        detail = reason.partition(":")[2].lower()
        return _PERMANENT_PROVIDER_ERROR.search(detail) is None
    return reason in {
        "pi_dispatch_timeout",
        "pi_final_message_invalid_json",
        "pi_final_message_truncated",
    }


class PiInvocationError(RuntimeError):
    """Keep the sanitized failure code available to Fixer retry boundaries."""

    def __init__(self, reason: str, *, label: str, attempt: int) -> None:
        # Some OS-error reasons carry raw paths. Preserve only safe error codes.
        safe_reason = reason if re.fullmatch(r"[a-z0-9_:-]+", reason) else reason.partition(":")[0]
        self.block_reason = safe_reason
        self.retryable = retryable_pi_failure(safe_reason)
        super().__init__(f"pi agent failed for {label} attempt {attempt}: {safe_reason}")
