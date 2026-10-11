# ALE Harbor adapter boundaries

| File | Responsibility |
| --- | --- |
| `setup.sh`, `run.sh` | Shared configuration and process entrypoints |
| `ale_adapter/prepare.py` | Explicit native source/dependency installation |
| `pyproject.toml`, `ale_adapter/workflow.py` | Prepared Harbor environment, dependency validation and exact task selection |
| `ale_adapter/source.py` | Revision, source integrity and image-map checks |
| `ale_adapter/adapter.py` | Native Linux/Windows CPU variant discovery and conversion; exclude GPU before imports |
| `ale_adapter/launch.py` | WAA-style selection/workers/output options, prepared dataset validation and Harbor CLI handoff |
| `ale_adapter/environment.py` | Harbor guest I/O, Windows KubeVirt/Dockur delegation and native worker ownership |
| `ale_adapter/linux.py` | Existing SBX/Docker backend selection and container definition |
| `ale_adapter/cua_proxy.py`, `guest_http.py` | Native CUA HTTP connection through existing backend command/file APIs |
| `ale_adapter/native.py` | Unchanged ALE driver/session, staging and setup/evaluation |
| `ale_adapter/verifier.py` | Native score validation and Harbor rewards |
| `../../Agents/AgentsLastExam/agent.py` | Mixed-OS image-provided command agent; Windows delegates to the existing bridge |

Custom Harbor agents can replace the default command agent. Native dependencies
run outside the Harbor interpreter. Grading remains in unchanged ALE source;
no alternate scheduler, reporting or resume implementation belongs here.

Windows Docker reuses WAA's `docker_windows.environment.DockerWindowsEnvironment`
and `windows_guest.WindowsGuestIO`. `ALEEnvironment` selects the native snapshot's
`docker_storage` and supplies ALE settings/transport; the shared backend owns
container creation, disk copying, readiness, guest I/O and cleanup. ALE keeps
one mixed-OS environment so Linux and Windows variants can share a Harbor job.

The workflow follows WAA: explicit `setup.sh`, prepared-state validation in
`run.sh`, selection and backend handoff in `launch.py`, then Harbor orchestration.
`--backend docker` (alias `--windows-backend docker`) selects Windows Dockur;
`--linux-backend` remains independent. The resolved Windows backend is stored
in Harbor environment kwargs, so resume keeps the original selection.
