# ALE Harbor adapter boundaries

| File | Responsibility |
| --- | --- |
| `setup.sh`, `run.sh` | Shared configuration and process entrypoints |
| `ale_adapter/prepare.py` | Explicit native source/dependency installation |
| `pyproject.toml`, `ale_adapter/workflow.py` | Prepared Harbor environment, dependency validation and exact task selection |
| `ale_adapter/source.py` | Revision, source integrity and image-map checks |
| `ale_adapter/adapter.py` | Native Linux/Windows CPU variant discovery and conversion; exclude GPU before imports |
| `ale_adapter/launch.py` | WAA-style selection/workers/output options, prepared dataset validation and Harbor CLI handoff |
| `ale_adapter/environment.py` | Harbor guest I/O, Windows PVC delegation and native worker ownership |
| `ale_adapter/linux.py` | Existing SBX/Docker backend selection and container definition |
| `ale_adapter/cua_proxy.py`, `guest_http.py` | Native CUA HTTP connection through existing backend command/file APIs |
| `ale_adapter/native.py` | Unchanged ALE driver/session, staging and setup/evaluation |
| `ale_adapter/verifier.py` | Native score validation and Harbor rewards |
| `../../Agents/AgentsLastExam/agent.py` | Mixed-OS image-provided command agent; Windows delegates to the existing bridge |

Custom Harbor agents can replace the default command agent. Native dependencies
run outside the Harbor interpreter. Grading remains in unchanged ALE source;
no alternate scheduler, reporting or resume implementation belongs here.
