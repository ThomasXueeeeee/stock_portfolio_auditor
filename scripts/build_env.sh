#!/usr/bin/env bash
set -euo pipefail

NAME="${1:-perf_audit}"
conda env create -n "${NAME}" -f environment.lock.yml
