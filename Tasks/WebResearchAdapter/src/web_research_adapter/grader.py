from __future__ import annotations

import json
import os
import re
import textwrap
import urllib.request
from pathlib import Path

ROLLOUT_SESSION_HEADERS = {"x-session-id", "proxy-x-session-id"}

BROWSE_PROMPT = """Judge whether the following [response] to [question] is correct or not based on the precise and unambiguous [correct_answer] below.

[question]: {question}

[response]: {response}

Your judgement must be in the format and criteria specified below:

extracted_final_answer: The final exact answer extracted from the [response]. Put the extracted answer as 'None' if there is no exact, final answer to extract from the response.

[correct_answer]: {correct_answer}

reasoning: Explain why the extracted_final_answer is correct or incorrect based on [correct_answer], focusing only on if there are meaningful differences between [correct_answer] and the extracted_final_answer. Do not comment on any background to the problem, do not attempt to solve the problem, do not argue for any answer different than [correct_answer], focus only on whether the answers match.

correct: Answer 'yes' if extracted_final_answer matches the [correct_answer] given above, or is within a small margin of error for numerical problems. Answer 'no' otherwise, i.e. if there if there is any inconsistency, ambiguity, non-equivalency, or if the extracted answer is incorrect.


confidence: The extracted confidence score between 0% and 100% from [response]. Put 100 if there is no confidence score available."""

DEEPSEARCH_PROMPT = textwrap.dedent(
    """\
    Your task is to evaluate whether a given "AI Response" for a specific "User Prompt" arrived at the correct answer.

    **Answer Correctness Task**

    *   **Purpose:** Assess whether the AI response provides the correct answer(s) based on the provided "Correct Answer" and "Prompt Type".
    *   **Process:**
        *   Identify the "Prompt Type": "<prompt_type>".
        *   Refer to the "Correct Answer": "<answer>".
        *   Based on the "Prompt Type", determine if the "AI Response" contains the expected answer(s).
            *   **'Single Answer'**: Check if the response provides the answer that addresses the user's question. It does not have to match the exact wording of the provided answer.
            *   **'Set Answer'**: Check if the response includes *each* item from the provided ground truth answers. The order might not matter unless specified otherwise. The response might include more answers than the list. Determine the correctness *only* based on the list first and then check if the response includes answers not in the list.
        *   **Explanation:** Provide a brief explanation justifying your assessment of answer correctness, referencing specific parts of the AI response and the correct answer.
        *   **Correctness Details:** Provide a dictionary, one key for each expected answer part, and value is a boolean indicating whether each expected answer part was found.
            *   For 'Set Answer', this will be a list of attributes, one for each item/part in the "Correct Answer". Each key will be a string indicating the expected answer part, and the value will be a boolean indicating whether that part was found in the response.
        *   **Excessive Answers:** Provide a list of strings, each indicating an excessive answer part. If the response provides answers that are **not** in the "Correct Answer" list, add these answers as excessive answers. Return an empty list when there's no excessive answers in the response.


    **Output Format:**

    Your evaluation *must* be structured as a nested JSON dictionary with the following top-level keys: `"Answer Correctness"`. Please return NULL if any of "Prompt", "AI Response" or "Correct Answer" is empty.
    The value for `"Answer Correctness"` should be a dictionary containing `"Explanation"` (a string), `"Correctness Details"` (a dictionary where each key is the expected correct answer, and the value is a boolean indicating whether the response contains the correct answer), and `"Excessive Answers"` (a list of strings indicating the excessive answers).

    Make sure you return a valid JSON string. Pay special attention to quotes, commas and special characters in the JSON string. Make sure to escape all special characters and quotes in the JSON string.


    """
)

DEEPSEARCH_EXAMPLE = r"""**Example (Partial):**

"```json
{{
  "Answer Correctness": {{
    "Explanation": "The response correctly identified Belgium and France but also includes an excessive answer, Italy.",
    "Correctness Details": {{
      "Belgium": true,
      "France": true,
    }},
    "Excessive Answers": [ "Italy" ]
  }}
}}
```"

**Now, proceed with the evaluation using the provided User Prompt, AI Response, and Correct Answer.**

User Prompt (Wrapped in <prompt> and </prompt>):
<prompt>
{question}
</prompt>
--------------------
**  Correct Answer (Wrapped in <answer> and </answer>):
Prompt Type: {answer_type}
<answer>
{answer}
</answer>
--------------------
AI assistant response (Wrapped in <response> and </response>):
<response>
{response}
</response>

--------------------
Rating:"""


def _answer() -> str:
    for name in ("/logs/agent/response.txt", "/workspace/answer.txt"):
        path = Path(name)
        if path.exists() and (text := path.read_text().strip()):
            return text
    path = Path("/logs/agent/claude-code.txt")
    if path.exists():
        for line in reversed(path.read_text().splitlines()):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result" and event.get("result"):
                return str(event["result"]).strip()
    return ""


def _chat(prompt: str) -> str:
    llm_kwargs = json.loads(os.environ.get("JUDGE_LLM_KWARGS", "{}"))
    extra_headers = llm_kwargs.get("extra_headers", {})
    if not isinstance(extra_headers, dict):
        raise TypeError("invalid judge extra_headers")
    headers = {
        str(name): str(value)
        for name, value in extra_headers.items()
        if str(name).lower() not in ROLLOUT_SESSION_HEADERS
    }
    headers.update(
        {
            "Authorization": f"Bearer {os.environ.get('JUDGE_API_KEY', '')}",
            "Content-Type": "application/json",
        }
    )
    payload = json.dumps(
        {
            "model": os.environ["JUDGE_MODEL"],
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode()
    request = urllib.request.Request(
        os.environ["JUDGE_API_URL"],
        data=payload,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        body = json.load(response)
    return body["choices"][0]["message"]["content"].strip()


def _json(text: str) -> dict:
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return json.loads(match.group(1) if match else text)


def grade_browsecomp(reference: dict, response: str) -> dict[str, float]:
    reply = _chat(
        BROWSE_PROMPT.format(
            question=reference["question"],
            correct_answer=reference["answer"],
            response=response,
        )
    )
    match = re.search(
        r"(?im)(?:^\s*(?:[*_]{1,2})?(?:correct|judge)\s*:\s*(?:[*_]{1,2})?\s*|[\"']correct[\"']\s*:\s*[\"']?)(yes|no)\b",
        reply,
    )
    if not match:
        raise ValueError(f"invalid BrowseComp grader response: {reply}")
    return {"reward": float(match.group(1).lower() == "yes")}


def grade_deepsearchqa(reference: dict, response: str) -> dict[str, float]:
    reply = _chat(
        DEEPSEARCH_PROMPT + DEEPSEARCH_EXAMPLE.format(response=response, **reference)
    )
    result = _json(reply)["Answer Correctness"]
    details, excessive = (
        result["Correctness Details"],
        result.get("Excessive Answers", []),
    )
    if (
        not isinstance(details, dict)
        or not details
        or not all(type(v) is bool for v in details.values())
    ):
        raise ValueError("invalid Correctness Details")
    if not isinstance(excessive, list):
        raise TypeError("invalid Excessive Answers")
    tp, fp = sum(details.values()), len(excessive)
    fn = len(details) - tp
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "reward": f1,
        "precision": precision,
        "recall": recall,
        "fully_correct": float(fn == 0 and fp == 0),
        "fully_incorrect": float(tp == 0),
        "partially_correct": float(tp > 0 and fn > 0),
        "correct_with_extraneous": float(fn == 0 and fp > 0),
    }


def main() -> None:
    output = Path("/logs/verifier/reward.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    reference = json.loads(Path("/tests/reference.json").read_text())
    response = _answer()
    if not response:
        output.write_text('{"reward": 0.0}\n')
        return
    grader = (
        grade_browsecomp
        if reference["benchmark"] == "browsecomp"
        else grade_deepsearchqa
    )
    output.write_text(json.dumps(grader(reference, response)) + "\n")


if __name__ == "__main__":
    main()
