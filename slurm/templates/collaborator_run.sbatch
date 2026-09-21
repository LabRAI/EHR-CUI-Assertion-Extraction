#!/bin/bash
#SBATCH --job-name=ehr_collaborator_run
#SBATCH --cpus-per-task=4
#SBATCH --mem=68G
#SBATCH --time=06:00:00
#SBATCH --output=collaborator_%j.out
#SBATCH --error=collaborator_%j.err

set -euo pipefail
umask 077

case "$(hostname)" in
  login*|*login*) echo "Refusing login-node execution" >&2; exit 2;;
esac

: "${CONFIG:?submit with --export=ALL,CONFIG=/path/to/harvard.local.json}"
PROJECT=${PROJECT:-${SLURM_SUBMIT_DIR}}
cd "${PROJECT}"
source .venv/bin/activate
python scripts/run_collaborator.py run --config "${CONFIG}"
