# Agent Fleet Rename Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the public product, repository paths, runtime resources, and tracing submodule references to Agent Fleet names without changing the tracing plugin's Python namespace.

**Architecture:** Apply one clean-break naming contract across user-facing documentation, setup-generated state, DinD resources, runtime metadata, and the submodule boundary. Preserve `sii-system`, `Shanghai Innovation Institute`, and `sii_opik_plugin`, and keep the existing submodule gitlink commit.

**Tech Stack:** Bash, Python, Docker/DinD, Git submodules, Markdown, unittest

---

## File Map

- Product and repository documentation: `README.md`, `STRUCT.md`, `AGENTS.md`,
  `CONTRIBUTING.md`, `config.env`, `skills/README.md`,
  `Agents/Openclaw/GUIDE.md`, `Agents/utils/common/Harbor/README.md`.
- Setup-generated names: `scripts/setup.sh`, `scripts/README.md`.
- DinD runtime names: `scripts/dind-run.sh`, `scripts/dind/Dockerfile`,
  `scripts/tests/test_dind_run.sh`.
- Runtime identity: `Agents/utils/rl/rollout_remote_harbor.py`.
- Submodule boundary: `.gitmodules`, `third_party/agent-opik-plugin`,
  `Agents/Harbor-claude-code/{README.md,STRUCT.md}`,
  `Agents/Harbor-opencode/{README.md,STRUCT.md,finalize_opencode_sessions.py,opik_opencode_harbor.py}`,
  `Agents/Openclaw/{Dockerfile.opik,scripts/build-openclaw-image.sh,tests/test_build_openclaw_image.sh}`,
  `Agents/utils/common/Harbor/{README.md,STRUCT.md,env.sh,tests/test_harboropik_extra_compose.sh}`,
  `skills/harbor-benchmark-runner/SKILL.md`.

### Task 1: Lock the New DinD Contract in Tests

**Files:**
- Modify: `scripts/tests/test_dind_run.sh`

- [ ] **Step 1: Change the expected DinD identifiers**

Update the assertions to require:

```text
agent-fleet.default-address-pools
agent-fleet-dind-docker:/var/lib/docker
agent-fleet-dind-home:/home/agent
agent-fleet-dind:28
```

- [ ] **Step 2: Run the test and verify it fails against old defaults**

Run:

```bash
bash scripts/tests/test_dind_run.sh
```

Expected: non-zero exit because `scripts/dind-run.sh` still emits the old
container, image, volume, label, or home names.

- [ ] **Step 3: Commit the test contract**

```bash
git add scripts/tests/test_dind_run.sh
git commit -m "test: define Agent Fleet DinD names"
```

### Task 2: Rename DinD Runtime Resources

**Files:**
- Modify: `scripts/dind-run.sh`
- Modify: `scripts/dind/Dockerfile`
- Modify: `scripts/README.md`

- [ ] **Step 1: Implement the new defaults**

Apply these exact mappings:

```text
DIND_NAME default:  agent-fleet-dind
DIND_DEFAULT_IMAGE: agent-fleet-dind:28
DIND_USER default:  agent
DIND_HOME_DIR:       /home/agent
Docker labels:       agent-fleet.registry-mirrors
                     agent-fleet.default-address-pools
Skill probe path:    $DIND_HOME_DIR/.claude/skills/agent-fleet
```

In `scripts/dind/Dockerfile`, create the `agent` user/group and home:

```dockerfile
addgroup -S agent
adduser -S -D -h /home/agent -s /bin/bash -G agent agent
addgroup agent docker
```

- [ ] **Step 2: Update the DinD documentation**

Replace old default container, image, volume, and user examples in
`scripts/README.md` with the mappings above.

- [ ] **Step 3: Verify syntax and behavior**

Run:

```bash
bash -n scripts/dind-run.sh
bash scripts/tests/test_dind_run.sh
```

Expected: both commands exit 0.

- [ ] **Step 4: Commit the runtime rename**

```bash
git add scripts/dind-run.sh scripts/dind/Dockerfile scripts/README.md
git commit -m "Rename Agent Fleet DinD resources"
```

### Task 3: Rename Product, Repository, Setup, and Skill Identifiers

**Files:**
- Modify: `README.md`
- Modify: `STRUCT.md`
- Modify: `AGENTS.md`
- Modify: `CONTRIBUTING.md`
- Modify: `config.env`
- Modify: `skills/README.md`
- Modify: `scripts/setup.sh`
- Modify: `Agents/Openclaw/GUIDE.md`
- Modify: `Agents/Openclaw/config/ansible/deploy.yml`
- Modify: `Agents/utils/common/Harbor/README.md`
- Modify: `Agents/utils/rl/rollout_remote_harbor.py`

- [ ] **Step 1: Apply the public naming mappings**

Use these exact replacements outside the preserved plugin namespace:

```text
SII Agent Fleet                         -> Agent Fleet
sii-agent-fleet                        -> agent-fleet
https://github.com/sii-system/sii-agent-fleet.git
                                       -> https://github.com/sii-system/agent-fleet.git
$HOME/sii-agent-fleet                  -> $HOME/agent-fleet
/workspace/sii-agent-fleet             -> /workspace/agent-fleet
sii-agent-fleet-rl-rollout/0.2         -> agent-fleet-rl-rollout/0.2
```

This also changes setup backup suffixes to `.bak.agent-fleet`, shell markers
to `# >>> agent-fleet env >>>`, the skill install directory to
`~/.claude/skills/agent-fleet`, and skill metadata name to `agent-fleet`.

- [ ] **Step 2: Verify setup and Python syntax**

Run:

```bash
bash -n scripts/setup.sh
python3 -m py_compile Agents/utils/rl/rollout_remote_harbor.py
python3 -m unittest discover -s tests
```

Expected: all commands exit 0 and the unittest output reports `OK`.

- [ ] **Step 3: Commit the product rename**

```bash
git add README.md STRUCT.md AGENTS.md CONTRIBUTING.md config.env \
  skills/README.md scripts/setup.sh Agents/Openclaw/GUIDE.md \
  Agents/Openclaw/config/ansible/deploy.yml \
  Agents/utils/common/Harbor/README.md \
  Agents/utils/rl/rollout_remote_harbor.py
git commit -m "Rename SII Agent Fleet to Agent Fleet"
```

### Task 4: Rename the Tracing Submodule Boundary

**Files:**
- Modify: `.gitmodules`
- Move: `third_party/sii-opik-plugin` to `third_party/agent-opik-plugin`
- Modify: `AGENTS.md`
- Modify: `STRUCT.md`
- Modify: `skills/harbor-benchmark-runner/SKILL.md`
- Modify: `Agents/Harbor-claude-code/README.md`
- Modify: `Agents/Harbor-claude-code/STRUCT.md`
- Modify: `Agents/Harbor-opencode/README.md`
- Modify: `Agents/Harbor-opencode/STRUCT.md`
- Modify: `Agents/Harbor-opencode/finalize_opencode_sessions.py`
- Modify: `Agents/Harbor-opencode/opik_opencode_harbor.py`
- Modify: `Agents/Openclaw/Dockerfile.opik`
- Modify: `Agents/Openclaw/scripts/build-openclaw-image.sh`
- Modify: `Agents/Openclaw/tests/test_build_openclaw_image.sh`
- Modify: `Agents/utils/common/Harbor/README.md`
- Modify: `Agents/utils/common/Harbor/STRUCT.md`
- Modify: `Agents/utils/common/Harbor/env.sh`
- Modify: `Agents/utils/common/Harbor/tests/test_harboropik_extra_compose.sh`

- [ ] **Step 1: Move the gitlink without changing its commit**

Move the gitlink path and update `.gitmodules`:

```text
submodule path: third_party/agent-opik-plugin
submodule URL:  https://github.com/sii-system/agent-opik-plugin.git
gitlink commit: dc15690f72739c0a57028eaf98184361474a812e
```

- [ ] **Step 2: Update consumer paths**

Replace repository, Docker, test-fixture, and documentation directory names
from `sii-opik-plugin` to `agent-opik-plugin`. Keep every
`src/sii_opik_plugin/...` and Python namespace reference unchanged.

- [ ] **Step 3: Verify the submodule and consumers**

Run:

```bash
git config -f .gitmodules --get-regexp '^submodule\..*\.\(path\|url\)$'
git ls-files -s third_party/agent-opik-plugin
bash -n Agents/Openclaw/scripts/build-openclaw-image.sh
python3 -m py_compile \
  Agents/Harbor-opencode/finalize_opencode_sessions.py \
  Agents/Harbor-opencode/opik_opencode_harbor.py
bash Agents/Openclaw/tests/test_build_openclaw_image.sh
bash Agents/utils/common/Harbor/tests/test_harboropik_extra_compose.sh
```

Expected: the config and gitlink show the new path/URL and original commit;
all syntax checks and tests exit 0.

- [ ] **Step 4: Commit the submodule rename**

Stage only `.gitmodules`, the old/new gitlink paths, and the consumer files
listed above, then run:

```bash
git commit -m "Rename tracing submodule to agent-opik-plugin"
```

### Task 5: Full Rename Verification

**Files:**
- Verify all modified files

- [ ] **Step 1: Scan for stale product and repository identifiers**

Run:

```bash
rg -n --hidden --glob '!.git/**' \
  --glob '!docs/superpowers/specs/**' \
  --glob '!docs/superpowers/plans/**' \
  '(sii-agent-fleet|SII Agent Fleet|sii-opik-plugin)' .
```

Expected: no output.

- [ ] **Step 2: Verify preserved identities**

Run:

```bash
rg -n --hidden --glob '!.git/**' \
  '(sii-system|Shanghai Innovation Institute|sii_opik_plugin)' \
  .gitmodules LICENSE Agents skills
```

Expected: organization and copyright references remain, and plugin source
paths still use `sii_opik_plugin`.

- [ ] **Step 3: Run affected suites**

Run:

```bash
python3 -m unittest discover -s tests
python3 -m unittest discover -s scripts/tests
bash scripts/tests/test_dind_run.sh
bash Agents/Openclaw/tests/test_build_openclaw_image.sh
bash Agents/utils/common/Harbor/tests/test_harboropik_extra_compose.sh
git diff --check
```

Expected: all tests and checks exit 0.

- [ ] **Step 4: Review final scope**

Run:

```bash
git status --short
git diff --stat
git log --oneline -8
```

Expected: only rename-related files are changed or committed; no generated
OpenClaw files, credentials, or unrelated changes are present.
