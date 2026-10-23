"""Classify Pi failures before repeating an unchanged model request."""

from __future__ import annotations

import re

_PROVIDER_HTTP_STATUS = re.compile(
    r"^(?:http_)?(?:status_(?:code_)?)?([45][0-9]{2})(?:_|$)"
)
_PERMANENT_PROVIDER_STATUSES = {"400", "401", "403", "404", "413", "422"}
_PERMANENT_PROVIDER_ERRORS = {
    "authentication_error": re.compile(r"authentication_error|authenticationerror"),
    "permission_denied": re.compile(r"permission_denied|permissiondeniederror"),
    "not_found_error": re.compile(r"not_found_error|notfounderror|model_not_found"),
    "context_window_exceeded": re.compile(r"context_window_exceeded|contextwindowexceedederror"),
    "request_too_large": re.compile(r"request_body_exceeded|request_too_large"),
    "invalid_request": re.compile(r"invalid_request|badrequesterror"),
}
_RETRYABLE_FAILURES = {
    "pi_dispatch_timeout",
    "pi_dispatch_exit_code",
    "pi_dispatch_os_error",
    "pi_event_stream_os_error",
    "pi_event_compaction_error",
    "pi_jsonl_invalid",
    "pi_subagent_session_not_observed",
    "pi_subagent_lifecycle_invalid",
    "pi_subagent_turn_invalid",
    "pi_final_message_invalid_json",
    "pi_final_message_truncated",
    "pi_final_json_missing",
}
_KNOWN_FAILURES = _RETRYABLE_FAILURES | {
    "pi_binary_not_found",
    "pi_provider_not_configured",
    "pi_model_not_configured",
    "pi_api_key_env_invalid",
    "pi_api_key_env_missing",
    "pi_thinking_level_invalid",
    "pi_base_url_invalid",
    "pi_extension_missing",
    "pi_streaming_stdin_unsupported",
    "pi_prompt_too_large",
}


def _provider_error_class(detail: str) -> str:
    # Message numbers may be quotas. Only a leading HTTP status is authoritative.
    detail = detail.lower()
    status = _PROVIDER_HTTP_STATUS.match(detail)
    if status:
        return status[1]
    for error_class, pattern in _PERMANENT_PROVIDER_ERRORS.items():
        if pattern.search(detail):
            return error_class
    return "unknown_error"


def retryable_pi_failure(reason: str) -> bool:
    """Retry transient provider failures and output failures a new turn can repair."""
    failure, _, detail = reason.partition(":")
    if failure == "pi_provider_request_failed":
        error_class = _provider_error_class(detail)
        return (
            error_class not in _PERMANENT_PROVIDER_STATUSES
            and error_class not in _PERMANENT_PROVIDER_ERRORS
        )
    return failure in _RETRYABLE_FAILURES


class PiInvocationError(RuntimeError):
    """Keep the sanitized failure code available to Fixer retry boundaries."""

    def __init__(self, reason: str, *, label: str, attempt: int) -> None:
        # Normalized provider text can still contain credentials: retain only known classes.
        failure, _, detail = reason.partition(":")
        if failure == "pi_provider_request_failed":
            safe_reason = f"{failure}:{_provider_error_class(detail)}"
        else:
            safe_reason = failure if failure in _KNOWN_FAILURES else "pi_invocation_failed"
        self.block_reason = safe_reason
        self.retryable = retryable_pi_failure(safe_reason)
        super().__init__(f"pi agent failed for {label} attempt {attempt}: {safe_reason}")
