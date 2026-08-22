# Security

## Container-Level Controls (`docker-compose.yml`)

| Policy | Setting |
|---|---|
| Capability drop | `cap_drop: ALL` |
| Privilege escalation | `no-new-privileges: true` |
| Read-only filesystem | Generated from `DOCKER_COMPOSE_READ_ONLY` + `tmpfs: /tmp` |
| Network isolation | Separate Docker bridge network per instance |
| Resource limits | Configurable `CPU_LIMIT` / `MEM_LIMIT` per container |

## Image-Level Defaults (`openclaw:local`)

| Policy | Setting |
|---|---|
| Non-root user | Runs as `node` (uid 1000) in the OpenClaw image |

## OpenClaw Config (`openclaw.json`)

| Policy | Setting |
|---|---|
| Gateway auth | Unique token per instance (`openssl rand -hex 32`) |
| Exec tools | `tools.exec.security` and `tools.exec.ask` come from setup defaults / CLI flags |
| File access | `tools.fs.workspaceOnly` defaults to `true`; configurable via `WORKSPACE_ONLY` |
| Sandbox | `agents.defaults.sandbox.mode` comes from setup defaults / CLI flags |
| Plugins | Restrictive `plugins.allow` with only `openai` by default; a configured `OPIK_URL` additionally allows `openclaw-opik-tracer` and enables `allowConversationAccess` for it |

## Infrastructure-Level Controls

| Policy | How |
|---|---|
| Volume isolation | Separate config/state + workspace directory per instance |
| Port isolation | Unique gateway port per instance, no conflicts |
| Token isolation | Each instance has its own gateway token |

## Not Fully Enforced by the Implementation

These are important security controls that are not fully enforced by the generated files and runtime configuration in this implementation:

| Risk area | Current status | Mitigation direction |
|---|---|---|
| Channel allowlists | No channel configs are generated, so no channels are enabled by default | For fleet-wide defaults, add channel config and `channels.<type>.allowlist` in `openclaw.json.template`; for an already-generated instance, update that instance's `openclaw.json` and restart |
| Secret storage | API keys may still live in config/env files | Move secrets to SecretRef / Vault / external secret manager |
| Host file permissions | Defaults `OPENCLAW_CONFIG_CHMOD=a+rwX` / `OPENCLAW_WORKSPACE_CHMOD=a+rwX` let benchmark runners write into the mounts; this also exposes generated configs (including provider keys) and workspace contents to any local user on the host | Set either value to an empty string for stricter local isolation |
| Prompt-injection lateral movement | Cannot be fully eliminated by config alone | Minimize tool/network access and isolate the host/network |
| Docker socket escape | `/var/run/docker.sock` is not mounted by default in this setup; risk appears only if you add it | Keep `/var/run/docker.sock` unmounted unless required |
