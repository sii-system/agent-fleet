#!/usr/bin/env bash
set -euo pipefail

test ! -e /app/answer.txt
tr '[:lower:]' '[:upper:]' < /app/input.txt > /app/answer.txt
