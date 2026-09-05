"""Replace DeepSearchQA's judge call with an OpenAI-compatible endpoint."""

from __future__ import annotations

import json
import os
import random
import time
from urllib import error, request


def judge_endpoint(base_url: str) -> str:
    endpoint = base_url.rstrip("/")
    if endpoint.endswith("/chat/completions"):
        return endpoint
    if endpoint.endswith("/v1"):
        return f"{endpoint}/chat/completions"
    return f"{endpoint}/v1/chat/completions"


def _required_judge_setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Set {name} for the DeepSearchQA verifier.")
    return value


def call_judge(prompt: str) -> str:
    base_url = _required_judge_setting("JUDGE_BASE_URL")
    api_key = _required_judge_setting("JUDGE_API_KEY")
    model = _required_judge_setting("JUDGE_MODEL")
    max_retries = int(os.environ.get("DEEPSEARCHQA_GRADER_MAX_RETRIES", "5"))
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode()
    judge_request = request.Request(
        judge_endpoint(base_url),
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    response_body: bytes | None = None
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            with request.urlopen(judge_request, timeout=120) as response:
                response_body = response.read()
            break
        except (error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == max_retries - 1:
                break
            time.sleep(1 + (2 ** (attempt + random.random())))

    if response_body is None:
        raise RuntimeError(
            f"Judge request failed after {max_retries} attempts: {last_error}"
        )

    try:
        parsed = json.loads(response_body)
        content = parsed["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Judge returned a malformed chat completion.") from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Judge returned a malformed chat completion.")
    return content


def main() -> None:
    import verifier as official_verifier

    official_verifier.call_gemini = call_judge
    official_verifier.main()


if __name__ == "__main__":
    main()
