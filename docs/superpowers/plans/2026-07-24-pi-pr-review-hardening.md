# Pi PR Review Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the pi PR reviewer inspect the trusted base checkout while confining all model-driven file access to that checkout and preserving the configured Chat Completions endpoint.

**Architecture:** `PiClient` will receive and validate `GITHUB_WORKSPACE`, run pi from that directory, disable built-in tools, and explicitly load the existing Harbor path-gate extension with a one-root allowlist. The adapter will derive pi's provider base URL by removing only `/chat/completions`, while response parsing remains strict but accepts compact Markdown fences.

**Tech Stack:** Python 3 standard library, `unittest`, pi coding agent 0.81.1, GitHub Actions YAML, existing TypeScript Harbor path-gate extension.

---

## File Structure

- Modify `.github/scripts/pi_pr_review.py`: endpoint adaptation, response parsing, repository-root validation, path-gated pi invocation, and entrypoint wiring.
- Modify `.github/scripts/tests/test_pi_pr_review.py`: regression coverage for endpoint preservation, compact fences, subprocess cwd, tool confinement, startup validation, and `GITHUB_WORKSPACE`.
- Verify `.github/scripts/tests/test_llm_pr_review_workflow.py`: preserve trusted-base checkout, separate workflow identities, and current environment contracts; no workflow source change is expected.
- Modify `Agents/utils/common/Harbor/scripts/harbor_analyzer/pi_extensions/analyzer_path_gate.ts`: bound reviewer-controlled searches, force literal grep only for the PR reviewer, and precompile simple glob matchers.
- Add `Agents/utils/common/Harbor/scripts/harbor_analyzer/pi_extensions/simple_glob_matcher.mjs`: dependency-free, bitset-NFA `*`/`?` matching with linear work per input code unit.
- Preserve the user-owned untracked `PLAN.md` unchanged.

### Post-review hardening follow-up

Whole-PR review expanded the original implementation plan after the initial
tasks were complete. The shared path gate now caps grep results, context,
output bytes, search-pattern length, and glob length. The PR reviewer opts into
literal-only grep while Harbor analyzers retain regex grep by default. Simple
globs are compiled once per tool call into a bitset NFA rather than reparsed or
backtracked for every candidate. Deterministic Node tests cover fixed legacy
semantics, seeded equivalence, and 5,000 long worst-case candidates; real pi
tests cover both reviewer and Harbor policy modes.

Final runtime-budget review added a reviewer-only maximum of 16 tool calls per
diff chunk, enforced by the path-gate hook and checked again in Python JSONL
validation. The shared extension now also caps every read, grep, find, and ls
result at 50 KiB, clamps find and ls to 200 results, and emits explicit
truncation details. Harbor remains unlimited in call count when the reviewer
environment variable is absent, but inherits the per-call output caps.

### Task 1: Preserve endpoint paths and accept compact JSON fences

**Files:**
- Modify: `.github/scripts/tests/test_pi_pr_review.py:99-151`
- Modify: `.github/scripts/pi_pr_review.py:9-70`

- [ ] **Step 1: Write failing endpoint and response tests**

Replace the custom-prefix expectation and add invalid-URL and compact-fence coverage:

```python
class UrlNormalisationTest(unittest.TestCase):
    def test_strips_chat_completions_suffix(self) -> None:
        result = pi_review._chat_url_to_base(
            "https://api.example.com/v1/chat/completions"
        )
        self.assertEqual(result, "https://api.example.com/v1")

    def test_preserves_custom_prefix_when_stripping_suffix(self) -> None:
        result = pi_review._chat_url_to_base(
            "https://gateway.example.com/v3/chat/completions"
        )
        self.assertEqual(result, "https://gateway.example.com/v3")

    def test_preserves_already_clean_url(self) -> None:
        result = pi_review._chat_url_to_base("https://api.example.com/v1")
        self.assertEqual(result, "https://api.example.com/v1")

    def test_rejects_invalid_url(self) -> None:
        with self.assertRaises(pi_review.PiReviewError):
            pi_review._chat_url_to_base("not-a-url")
```

Add this method to `JsonExtractionTest`:

```python
def test_compact_fenced_object(self) -> None:
    self.assertEqual(
        pi_review._extract_json('```json{"findings": []}```'),
        {"findings": []},
    )
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python3 .github/scripts/tests/test_pi_pr_review.py \
  UrlNormalisationTest JsonExtractionTest
```

Expected: two failures. The custom prefix is rewritten to `/v3/v1`, and the compact fence raises `PiReviewError`. Existing trailing-text and non-object rejection tests remain green.

- [ ] **Step 3: Implement the minimal endpoint and fence changes**

Remove the unused `API_KEY_ENV` and `normalized_base_url` imports. Replace `_chat_url_to_base` and the opening fence logic in `_extract_json` with:

```python
def _chat_url_to_base(url: str) -> str:
    """Convert a chat-completions endpoint URL to a pi-compatible base URL."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PiReviewError("invalid LLM_REVIEW_BASE_URL for pi provider")
    suffix = "/chat/completions"
    path = parsed.path.rstrip("/")
    if path.endswith(suffix):
        path = path[: -len(suffix)]
    return parsed._replace(path=path).geturl()


def _extract_json(text: str) -> dict[str, Any]:
    """Parse the final assistant text as one JSON object."""
    content = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.DOTALL)
    if fenced:
        content = fenced.group(1)
    decoder = json.JSONDecoder()
    try:
        value, end = decoder.raw_decode(content)
    except json.JSONDecodeError as exc:
        raise PiReviewError("pi response is not valid JSON") from exc
    if content[end:].strip() or not isinstance(value, dict):
        raise PiReviewError("pi response must contain one JSON object")
    return value
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

```bash
python3 .github/scripts/tests/test_pi_pr_review.py \
  UrlNormalisationTest JsonExtractionTest
```

Expected: all endpoint and extraction tests pass.

- [ ] **Step 5: Commit the endpoint and response fix**

```bash
git add .github/scripts/pi_pr_review.py \
  .github/scripts/tests/test_pi_pr_review.py
git diff --cached --check
git commit -m "Fix pi review endpoint and response parsing"
```

### Task 2: Run pi from a path-gated repository root

**Files:**
- Modify: `.github/scripts/tests/test_pi_pr_review.py:21-382`
- Modify: `.github/scripts/pi_pr_review.py:23-208`
- Reuse: `Agents/utils/common/Harbor/scripts/harbor_analyzer/pi_extensions/analyzer_path_gate.ts`

- [ ] **Step 1: Extend the stub and client fixture for confinement assertions**

Add cwd and allowlist capture to `_stub_pi_script`:

```bash
printf 'cwd=%s\n' "$PWD"
printf 'allowed_paths=%s\n' \
  "${HARBOR_ANALYZER_ALLOWED_PATHS_JSON:-}"
```

Create a repository directory in `PiClientTest.setUp` and pass it from `_make_client`:

```python
self.repo_dir = self.root / "repository"
self.repo_dir.mkdir()
```

```python
kwargs = dict(
    pi_binary=str(self.bin_dir / "pi"),
    base_url="https://api.example.com/v1/chat/completions",
    api_key="test-api-key",
    model="test-model",
    repository_root=self.repo_dir,
    timeout=30,
)
```

- [ ] **Step 2: Write failing cwd, command-policy, and validation tests**

Add these methods to `PiClientTest`:

```python
def test_runs_from_repository_root(self) -> None:
    _stub_pi_script(self.bin_dir, stdout=_make_findings_response())

    self._make_client().review("prompt", "diff")

    captured = self.capture.read_text(encoding="utf-8")
    self.assertIn(f"cwd={self.repo_dir.resolve()}", captured)

def test_uses_only_path_gated_read_tools(self) -> None:
    _stub_pi_script(self.bin_dir, stdout=_make_findings_response())

    self._make_client().review("prompt", "diff")

    captured = self.capture.read_text(encoding="utf-8")
    self.assertIn("arg=<--no-builtin-tools>", captured)
    self.assertIn("arg=<--tools>", captured)
    self.assertIn("arg=<read,grep,find,ls>", captured)
    self.assertIn("arg=<--extension>", captured)
    self.assertIn(str(pi_review.PI_PATH_GATE_EXTENSION), captured)
    self.assertIn("arg=<--no-approve>", captured)
    self.assertNotIn("arg=<--approve>", captured)

def test_limits_path_gate_to_repository_root(self) -> None:
    _stub_pi_script(self.bin_dir, stdout=_make_findings_response())

    self._make_client().review("prompt", "diff")

    captured = self.capture.read_text(encoding="utf-8")
    expected = json.dumps([str(self.repo_dir.resolve())])
    self.assertIn(f"allowed_paths={expected}", captured)

def test_missing_repository_root_fails_before_launch(self) -> None:
    with self.assertRaises(pi_review.PiReviewError) as ctx:
        self._make_client(repository_root=self.root / "missing")
    self.assertIn("repository root", str(ctx.exception))

def test_missing_path_gate_fails_before_launch(self) -> None:
    with self.assertRaises(pi_review.PiReviewError) as ctx:
        self._make_client(path_gate_extension=self.root / "missing.ts")
    self.assertIn("path-gate extension", str(ctx.exception))
```

Update the existing policy assertion in
`test_passes_system_prompt_and_diff_chunk_to_pi` to expect
`arg=<--no-approve>` instead of `arg=<--approve>`.

- [ ] **Step 3: Run the client tests and verify RED**

Run:

```bash
python3 .github/scripts/tests/test_pi_pr_review.py PiClientTest
```

Expected: the new tests fail because `PiClient` has no `repository_root`,
uses the temp `work` directory, enables built-ins, and does not load the path
gate.

- [ ] **Step 4: Implement repository and path-gate validation**

Add these constants below `_PROJECT_ROOT`:

```python
PI_PATH_GATE_EXTENSION = (
    _PROJECT_ROOT
    / "Agents"
    / "utils"
    / "common"
    / "Harbor"
    / "scripts"
    / "harbor_analyzer"
    / "pi_extensions"
    / "analyzer_path_gate.ts"
)
PI_ALLOWED_PATHS_ENV = "HARBOR_ANALYZER_ALLOWED_PATHS_JSON"
```

Add the two keyword arguments and validation to `PiClient.__init__`:

```python
repository_root: Path,
path_gate_extension: Path = PI_PATH_GATE_EXTENSION,
```

```python
try:
    self.repository_root = repository_root.resolve(strict=True)
except OSError as exc:
    raise PiReviewError(
        f"pi repository root is unavailable: {repository_root}"
    ) from exc
if not self.repository_root.is_dir():
    raise PiReviewError(
        f"pi repository root is not a directory: {repository_root}"
    )
try:
    self.path_gate_extension = path_gate_extension.resolve(strict=True)
except OSError as exc:
    raise PiReviewError(
        f"pi path-gate extension is unavailable: {path_gate_extension}"
    ) from exc
if not self.path_gate_extension.is_file():
    raise PiReviewError(
        f"pi path-gate extension is not a file: {path_gate_extension}"
    )
```

- [ ] **Step 5: Replace the empty workdir with the gated repository invocation**

Keep only the isolated `runtime_dir` under the temporary directory. Build the
subprocess environment before the command:

```python
environment = minimal_environment(runtime_dir, self.api_key)
environment[PI_ALLOWED_PATHS_ENV] = json.dumps(
    [str(self.repository_root)]
)
```

Use this security-related command fragment:

```python
"--no-session",
"--no-builtin-tools",
"--tools", "read,grep,find,ls",
"--extension", str(self.path_gate_extension),
"--no-extensions",
"--no-skills",
"--no-prompt-templates",
"--no-themes",
"--no-context-files",
"--no-approve",
```

Pass the validated root and prepared environment to `subprocess.run`:

```python
cwd=self.repository_root,
env=environment,
```

- [ ] **Step 6: Run the client tests and verify GREEN**

Run:

```bash
python3 .github/scripts/tests/test_pi_pr_review.py PiClientTest
```

Expected: all `PiClientTest` cases pass, including existing timeout, exit,
missing-binary, JSONL, model-config, and API-key cases.

- [ ] **Step 7: Commit the path-gated runtime**

```bash
git add .github/scripts/pi_pr_review.py \
  .github/scripts/tests/test_pi_pr_review.py
git diff --cached --check
git commit -m "Confine pi review tools to repository"
```

### Task 3: Wire `GITHUB_WORKSPACE` through the workflow entrypoint

**Files:**
- Modify: `.github/scripts/tests/test_pi_pr_review.py:532-556`
- Modify: `.github/scripts/pi_pr_review.py:294-314`
- Verify: `.github/scripts/tests/test_llm_pr_review_workflow.py`

- [ ] **Step 1: Add a reusable main-test fixture**

Import `io` and `redirect_stderr`, then add:

```python
class MainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.event_path = self.root / "event.json"
        self.prompt_path = self.root / "prompt.md"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.event_path.write_text(
            json.dumps({"pull_request": {"number": 23}}),
            encoding="utf-8",
        )
        self.prompt_path.write_text("review prompt", encoding="utf-8")
        self.environment = {
            "GITHUB_REPOSITORY": "sii-system/agent-fleet",
            "GITHUB_TOKEN": "test-github-token",
            "GITHUB_WORKSPACE": str(self.workspace),
            "LLM_REVIEW_BASE_URL":
                "https://api.example.com/v1/chat/completions",
            "LLM_REVIEW_API_KEY": "test-api-key",
            "LLM_REVIEW_MODEL": "test-model",
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def argv(self) -> list[str]:
        return [
            "pi_pr_review.py",
            "--event-path", str(self.event_path),
            "--prompt-path", str(self.prompt_path),
        ]
```

- [ ] **Step 2: Write failing entrypoint tests**

Add:

```python
def test_main_passes_github_workspace_to_pi_client(self) -> None:
    with (
        mock.patch.dict(os.environ, self.environment, clear=True),
        mock.patch.object(sys, "argv", self.argv()),
        mock.patch.object(pi_review, "PiClient") as client_class,
        mock.patch.object(
            pi_review, "run_review", return_value="published"
        ),
    ):
        result = pi_review.main()

    self.assertEqual(result, 0)
    self.assertEqual(
        client_class.call_args.kwargs["repository_root"],
        self.workspace,
    )

def test_main_reports_repository_validation_error(self) -> None:
    stderr = io.StringIO()
    with (
        mock.patch.dict(os.environ, self.environment, clear=True),
        mock.patch.object(sys, "argv", self.argv()),
        mock.patch.object(
            pi_review,
            "PiClient",
            side_effect=pi_review.PiReviewError("bad repository root"),
        ),
        redirect_stderr(stderr),
    ):
        result = pi_review.main()

    self.assertEqual(result, 1)
    self.assertIn("bad repository root", stderr.getvalue())
```

- [ ] **Step 3: Run the entrypoint tests and verify RED**

Run:

```bash
python3 .github/scripts/tests/test_pi_pr_review.py MainTest
```

Expected: the first test fails because `repository_root` is absent. The second
test errors because `PiClient` construction is outside the `PiReviewError`
handler.

- [ ] **Step 4: Construct the client inside the error boundary**

Change `main` so construction and review share the existing failure handler:

```python
prompt = args.prompt_path.read_text()
review_id = os.environ.get("LLM_REVIEW_ID", PI_REVIEW_ID)
try:
    pi_client = PiClient(
        pi_binary=args.pi_bin,
        base_url=require_env("LLM_REVIEW_BASE_URL"),
        api_key=require_env("LLM_REVIEW_API_KEY"),
        model=require_env("LLM_REVIEW_MODEL"),
        repository_root=Path(require_env("GITHUB_WORKSPACE")),
    )
    result = run_review(github, pi_client, pull_number, prompt, review_id)
except PiReviewError as exc:
    print(f"pi PR review failed: {exc}", file=sys.stderr)
    return 1
```

- [ ] **Step 5: Run entrypoint and workflow contract tests**

Run:

```bash
python3 .github/scripts/tests/test_pi_pr_review.py MainTest
python3 .github/scripts/tests/test_llm_pr_review_workflow.py
```

Expected: both commands pass. The workflow contracts still prove
`pull_request_target`, `base.sha`, pinned checkout, minimal permissions,
separate environments, and separate review IDs.

- [ ] **Step 6: Commit entrypoint wiring**

```bash
git add .github/scripts/pi_pr_review.py \
  .github/scripts/tests/test_pi_pr_review.py
git diff --cached --check
git commit -m "Pass trusted workspace to pi reviewer"
```

### Task 4: Simplify and verify the complete refinement

**Files:**
- Review: `.github/scripts/pi_pr_review.py`
- Review: `.github/scripts/tests/test_pi_pr_review.py`
- Verify: `.github/scripts/tests/test_llm_pr_review_workflow.py`
- Verify: `.github/workflows/llm-pr-review.yml`
- Verify: `.github/workflows/self-hosted-llm-pr-review.yml`

- [ ] **Step 1: Apply the code-simplifier review**

Inspect only the files changed by Tasks 1-3. Remove stale comments, unused
imports, repeated fixture logic, or unnecessary nesting without changing the
public CLI, review schema, summary format, or failure behavior. Do not alter
the Harbor path-gate extension.

- [ ] **Step 2: Run a live path-gate smoke test with pi 0.81.1**

Use the existing local model configuration without printing its values:

```bash
set -a
source config.env
if [[ -f config.local.env ]]; then
  source config.local.env
fi
set +a
python3 - <<'PY'
import os
import sys
from pathlib import Path

sys.path.insert(0, ".github/scripts")
import pi_pr_review

base = os.environ["BASE_URL"].rstrip("/")
if not base.endswith("/v1"):
    base += "/v1"
endpoint = f"{base}/chat/completions"
client = pi_pr_review.PiClient(
    pi_binary="pi",
    base_url=endpoint,
    api_key=os.environ["API_KEY"],
    model=os.environ["MODEL"],
    repository_root=Path.cwd(),
    timeout=180,
)
result = client.review(
    (
        "You are testing a read-only filesystem policy. You must call read "
        "for AGENTS.md and then call read for /etc/hosts. Return exactly "
        '{"repo_read":true,"outside_read_blocked":true} only when the first '
        "read succeeds and the second returns Access denied."
    ),
    "Perform the two required reads now.",
)
assert result == {
    "repo_read": True,
    "outside_read_blocked": True,
}, result
print("pi path-gate smoke test passed")
PY
```

Expected: pi 0.81.1 reads `AGENTS.md`, receives `Access denied` for
`/etc/hosts`, returns the asserted object, and prints only the success line.
If local model configuration is unavailable or the endpoint is unreachable,
record that environmental limitation and do not substitute a mocked result.

- [ ] **Step 3: Run the complete affected test suite**

Run:

```bash
python3 -m unittest discover -s .github/scripts/tests -p 'test_*.py'
```

Expected: all tests pass with zero failures or errors.

- [ ] **Step 4: Validate syntax and repository cleanliness**

Run:

```bash
python3 -m py_compile .github/scripts/pi_pr_review.py \
  .github/scripts/tests/test_pi_pr_review.py
ruby -e '
  require "yaml"
  YAML.load_file(".github/workflows/llm-pr-review.yml")
  YAML.load_file(".github/workflows/self-hosted-llm-pr-review.yml")
'
git diff --check
git diff --check upstream/main...HEAD
git status --short --branch
```

Expected: Python and YAML parsing exit zero, both diff checks are silent, and
only the intentionally preserved untracked `PLAN.md` remains outside committed
work.

- [ ] **Step 5: Review the final diff against the design**

Run:

```bash
git diff --stat upstream/main...HEAD
git diff upstream/main...HEAD -- \
  .github/scripts/pi_pr_review.py \
  .github/scripts/tests/test_pi_pr_review.py \
  .github/scripts/tests/test_llm_pr_review_workflow.py \
  .github/workflows/llm-pr-review.yml \
  .github/workflows/self-hosted-llm-pr-review.yml
```

Expected: the diff implements repository cwd, path-gated extension loading,
minimal environment, endpoint preservation, compact fences, and regression
coverage without changing workflow triggers, permissions, or review IDs.

- [ ] **Step 6: Commit any simplification-only edits**

If Step 1 changed tracked files:

```bash
git add .github/scripts/pi_pr_review.py \
  .github/scripts/tests/test_pi_pr_review.py
git diff --cached --check
git commit -m "Simplify pi review hardening"
```

If Step 1 produced no diff, skip this commit.

- [ ] **Step 7: Refresh live PR state before publication**

Run:

```bash
git fetch upstream pull/23/head:refs/remotes/upstream/pr/23
test "$(git rev-parse refs/remotes/upstream/pr/23)" = \
  "$(git merge-base refs/remotes/upstream/pr/23 HEAD)"
gh pr view 23 --repo sii-system/agent-fleet \
  --json url,headRefOid,mergeStateStatus,reviews,statusCheckRollup
```

Expected: the fetched PR head is an ancestor of the refined local head. Review
threads and checks are refreshed from GitHub before any push or thread
resolution.
