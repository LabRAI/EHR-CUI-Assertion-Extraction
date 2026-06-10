#!/bin/bash
#SBATCH --job-name=ehr_extract
#SBATCH --cpus-per-task=4
#SBATCH --mem=68G
#SBATCH --time=06:00:00
#SBATCH --output=<LOG_DIR>/%x_%j.out
#SBATCH --error=<LOG_DIR>/%x_%j.err

set -euo pipefail
umask 077

PROJECT_DIR=<PROJECT_DIR>
NOTES_JSONL_GZ=<PRIVATE_NOTE_SHARD_JSONL_GZ>
MODELPACK=<LICENSED_MEDCAT_MODELPACK_ZIP>
POLICY=${PROJECT_DIR}/configs/linker_filter_policy.local.json
OUTPUT_DIR=<PRIVATE_EXTRACTION_OUTPUT_DIR>

cd "${PROJECT_DIR}"
source .venv/bin/activate

python scripts/run_extraction_auto.py \
  --notes-jsonl-gz "${NOTES_JSONL_GZ}" \
  --modelpack "${MODELPACK}" \
  --filter-policy "${POLICY}" \
  --output-root "${OUTPUT_DIR}" \
  --variant-name shard_auto \
  --progress-every 500
