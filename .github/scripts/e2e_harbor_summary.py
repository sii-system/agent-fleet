"""Adapt the generated Fixer artifact link for display in an Actions summary."""

import argparse
import re
from pathlib import Path

FIXER_LINK = "[fix-report-latest.md](fixer/fix-report-latest.md)"


def render_summary(report: str, artifact_url: str) -> str:
    location = "`fixer/fix-report-latest.md`"
    location += (
        f" ([download run artifacts]({artifact_url}))"
        if artifact_url else " (artifact upload unavailable)"
    )
    lines = []
    fence = ""
    for line in report.splitlines(keepends=True):
        marker = re.match(r" {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            if not fence:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = ""
        elif not fence and line.rstrip("\r\n") == f"Full Fixer report: {FIXER_LINK}":
            line = line.replace(FIXER_LINK, location)
        lines.append(line)
    return "".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    parser.add_argument("--artifact-url", default="")
    args = parser.parse_args()
    print(render_summary(args.summary.read_text(encoding="utf-8"), args.artifact_url), end="")


if __name__ == "__main__":
    main()
