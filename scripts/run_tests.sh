#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "--all" ]]; then
  pytest
else
  pytest -m "not local_data"
fi
