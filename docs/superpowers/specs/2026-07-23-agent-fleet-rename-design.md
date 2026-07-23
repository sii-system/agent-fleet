# Agent Fleet Rename Design

## Goal

Align the repository, product, runtime resources, and tracing submodule with
the public names `Agent Fleet`, `agent-fleet`, and `agent-opik-plugin`.

## Scope

- Replace the product name `SII Agent Fleet` with `Agent Fleet`.
- Replace the repository identifier `sii-agent-fleet` with `agent-fleet` in
  clone URLs, default checkout paths, documentation, setup output, skill
  metadata, backup suffixes, shell marker blocks, and runtime identifiers.
- Rename DinD defaults from the old prefix to:
  - container and volume prefix: `agent-fleet-dind`
  - image: `agent-fleet-dind:28`
  - labels: `agent-fleet.*`
  - user and home: `agent` and `/home/agent`
- Rename the tracing submodule path and repository reference from
  `third_party/sii-opik-plugin` to `third_party/agent-opik-plugin`, including
  Docker build paths and documentation.
- Keep the existing submodule commit and `v0.1.0` pin.

## Explicitly Preserved

- GitHub organization `sii-system`.
- Copyright owner `Shanghai Innovation Institute`.
- The tracing plugin's internal Python namespace `sii_opik_plugin`.
- Git history and existing commit metadata.

The `agent-opik-plugin` repository itself is outside this change and will not
be modified.

## Compatibility

This is a clean pre-release rename. Old DinD containers, images, volumes,
labels, shell marker blocks, backup suffixes, and skill installation paths
will not receive compatibility aliases or automatic migration.

## Verification

- Update tests that assert renamed DinD resources and paths.
- Run shell syntax checks for changed shell scripts.
- Run the affected setup, DinD, skill, Harbor integration, and OpenClaw build
  tests.
- Verify the submodule path, URL, and gitlink commit.
- Scan the current tree for stale `sii-agent-fleet`, `SII Agent Fleet`, and
  `sii-opik-plugin` identifiers while allowing `sii-system`,
  `sii_opik_plugin`, and the copyright owner.
- Run `git diff --check`.
