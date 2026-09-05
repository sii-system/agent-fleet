#!/bin/bash
set -euo pipefail

python /tests/openai_judge.py /tests/metadata.json /workspace/answer.txt
