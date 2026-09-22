#!/bin/bash
#SBATCH --job-name=harvard_ehr_default
#SBATCH --cpus-per-task=4
#SBATCH --mem=68G
#SBATCH --time=24:00:00
#SBATCH --output=harvard_default_%j.out
#SBATCH --error=harvard_default_%j.err

set -euo pipefail
umask 077

case "$(hostname)" in
  login*|*login*) echo "Refusing login-node execution" >&2; exit 2;;
esac

# Fill in only these three paths.
NOTES_JSONL_GZ="<PRIVATE_NOTES_JSONL_OR_JSONL_GZ>"
MODELPACK="<LICENSED_MEDCAT_MODELPACK_ZIP>"
CDB_PATH="<LICENSED_MEDCAT_CDB_PATH>"

PROJECT_DIR=${PROJECT_DIR:-${SLURM_SUBMIT_DIR}}
OUTPUT_ROOT=${OUTPUT_ROOT:-${PROJECT_DIR}/harvard_default_output}

cd "${PROJECT_DIR}"
source .venv/bin/activate

python scripts/run_harvard_default.py \
  --notes-jsonl-gz "${NOTES_JSONL_GZ}" \
  --modelpack "${MODELPACK}" \
  --cdb-path "${CDB_PATH}" \
  --output-root "${OUTPUT_ROOT}"
