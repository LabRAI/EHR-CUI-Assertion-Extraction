#!/bin/bash
# This template submits a generated scaling benchmark script.
# First run scripts/build_extraction_scaling_benchmark.py with local paths, then:
#   sbatch <PRIVATE_SLURM_DIR>/scaling_stage1_n10000_cpu4_prefork_batchlinker.sbatch

set -euo pipefail

echo "Generate benchmark sbatch files with scripts/build_extraction_scaling_benchmark.py"
