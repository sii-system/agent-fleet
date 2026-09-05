#!/usr/bin/env python3
"""Small healthcheck entrypoint for operators and CI."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

launcher = Path(__file__).with_name("launcher.py")
raise SystemExit(subprocess.call([sys.executable, str(launcher), "health", *sys.argv[1:]]))
