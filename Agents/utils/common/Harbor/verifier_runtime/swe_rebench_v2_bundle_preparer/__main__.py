from __future__ import annotations

import sys
from pathlib import Path

if __package__:
    from . import main
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from verifier_runtime.swe_rebench_v2_bundle_preparer import main


if __name__ == "__main__":
    raise SystemExit(main())
