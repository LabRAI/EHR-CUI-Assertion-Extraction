#!/bin/bash
#SBATCH --job-name=ehr_site_profile_test
#SBATCH --partition=hpg-default
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=00:10:00
#SBATCH --output=site_adaptation_unit_%j.out
#SBATCH --error=site_adaptation_unit_%j.err

set -euo pipefail
umask 077
case "$(hostname)" in login*|*login*) echo "Refusing login-node execution" >&2; exit 2;; esac

PROJECT=${PROJECT:-${SLURM_SUBMIT_DIR}}
cd "${PROJECT}"
python3 -m unittest discover -s tests -p 'test_*.py' -v
