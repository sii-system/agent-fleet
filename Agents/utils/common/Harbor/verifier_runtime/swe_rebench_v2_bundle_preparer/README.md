# SWE-rebench-V2 Bundle Preparer

This directory is an executable Python package that composes the static Python
runtime into the SWE-rebench-V2 verifier bundle. It does not build task images
or install task dependencies. It produces a validated tarball that the
OpenSandbox provider materializes when a task starts.

## File responsibilities

| File | Responsibility |
| --- | --- |
| __init__.py | Bundle validation, safe extraction, construction, and CLI implementation |
| __main__.py | Supports both python <package-dir> ... and package imports |
| self_check.py | Runs parser import and a minimal grading fixture with bundled Python |
| self_check.sh | Thin compatibility entrypoint that invokes bin/python3 self_check.py |

self_check.sh contains no Python heredoc. It remains as the provider's fixed,
executable self-check entrypoint and guarantees that the bundled interpreter is
used instead of task-image python3.

## CLI

The Harbor environment scripts invoke:

~~~bash
python Agents/utils/common/Harbor/verifier_runtime/swe_rebench_v2_bundle_preparer \
  build --cache-dir "$VERIFIER_RUNTIME_BUNDLE_CACHE_DIR" \
  --output "$VERIFIER_RUNTIME_BUNDLE_ARCHIVE_SOURCE"

python Agents/utils/common/Harbor/verifier_runtime/swe_rebench_v2_bundle_preparer \
  check --archive "$VERIFIER_RUNTIME_BUNDLE_ARCHIVE_SOURCE"
~~~

build prepares or reuses the static runtime and atomically writes the bundle.
check only validates an existing archive and does not execute the task parser.

## Generated bundle layout

The top-level directory is
agent-fleet-swe-rebench-v2-verifier-bundle:

~~~text
<bundle>/
├── static-runtime.json
├── bin/
│   ├── python3.12          # environment-clearing wrapper
│   ├── python3.12.real     # validated x86_64 static ELF
│   ├── python3 -> python3.12
│   ├── python -> python3.12
│   ├── harbor-verifier-bundle-check
│   └── self_check.py
└── lib/python3.12/         # standalone Python standard library and extensions
~~~

archive_ready() validates the static-runtime manifest, required standard
library files, both Python links, the self-check shell file, and the exact
self_check.py source. Absolute links from the standalone archive are skipped
during extraction; only relative links that remain inside the bundle root are
accepted.

## Self-check contract

After extraction, the provider executes:

~~~text
<bundle>/bin/harbor-verifier-bundle-check
~~~

It reads /tests/parser.py by default and accepts an explicit parser path.
self_check.py:

1. imports the parser with the bundled Python;
2. imports JSON, regular expressions, and XML from the standard library;
3. checks expected pass, expected fail, and unexpected-pass behavior with a
   minimal grade() fixture;
4. prints the Python version, target architecture, static-linkage marker, and
   fixture result.

Any failure returns 127 and must be classified by the provider as verifier
runtime infrastructure failure, not model failure. A passing self-check only
proves that the runtime and parser can start; it does not execute the full task.

## Change rules

- Update this README and the parent runtime README when changing source,
  manifest, or wrapper semantics.
- Keep Python implementation in .py files; do not reintroduce long shell
  heredocs or Python string literals.
- Never fall back to the host interpreter when static-runtime preparation fails.
- After changes, run the preparer tests, the parser self-check fixture, Ruff,
  and bash -n.
- Run a real OpenSandbox smoke separately; local unit tests do not replace it.
