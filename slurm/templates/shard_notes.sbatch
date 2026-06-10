#!/bin/bash
#SBATCH --job-name=ehr_shard_notes
#SBATCH --cpus-per-task=1
#SBATCH --mem=16G
#SBATCH --time=04:00:00
#SBATCH --output=<LOG_DIR>/%x_%j.out
#SBATCH --error=<LOG_DIR>/%x_%j.err

set -euo pipefail
umask 077

PROJECT_DIR=<PROJECT_DIR>
MIMIC_ZIP=<PATH_TO_RESTRICTED_MIMIC_ZIP>
OUTPUT_DIR=<PRIVATE_NOTE_SHARD_DIR>

cd "${PROJECT_DIR}"
source .venv/bin/activate

python scripts/shard_mimic_notes.py \
  --zip "${MIMIC_ZIP}" \
  --output-dir "${OUTPUT_DIR}" \
  --notes-per-shard 5000

