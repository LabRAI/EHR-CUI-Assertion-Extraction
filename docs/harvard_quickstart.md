# Harvard Quickstart

This is the short path for a collaborator run. It keeps the validated
extraction defaults in one config bundle and does not require editing Python.
The production extractor remains CPU-only and does not call an LLM.

## 1. Install

```bash
git clone https://github.com/LabRAI/EHR-CUI-Assertion-Extraction.git
cd EHR-CUI-Assertion-Extraction
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Keep notes, the licensed MedCAT modelpack/CDB, and outputs outside the cloned
repository. Do not commit any of them.

## 2. Create One Local Config

Run this from the repository root. The generated file is private and ignored
by git:

```bash
python scripts/run_collaborator.py init \
  --output configs/harvard.local.json
```

Edit only `configs/harvard.local.json`:

- `inputs`: private note shards, MedCAT modelpack, CDB, and output directory;
- `execution`: `workers: 0` means use the CPU allocation automatically;
- `hyperparameters`: all validated linker, ontology, and refinement settings;
- `site_adaptation`: frozen by default; activate a profile only after it passes
  the calibration non-regression gate.

Relative paths are resolved from the config file's directory. A path may use
`$HOME` or another environment variable.

## 3. Validate Before Running

```bash
python scripts/run_collaborator.py validate \
  --config configs/harvard.local.json
```

To inspect the fully expanded command and all effective settings:

```bash
python scripts/run_collaborator.py show \
  --config configs/harvard.local.json
```

## 4. Run On a Compute Node

The repository refuses restricted-data extraction on a login node. With SLURM:

```bash
sbatch --export=ALL,CONFIG=$PWD/configs/harvard.local.json \
  slurm/templates/collaborator_run.sbatch
```

Or run `python scripts/run_collaborator.py run ...` inside an already allocated
compute job. Use `allow_no_slurm: true` only on a dedicated compute host for a
small synthetic smoke test.

## 5. Optional Site Adaptation

First run the frozen extractor on a calibration split. Then build a profile
from local notes and candidate rows:

```bash
python scripts/run_collaborator.py build-profile \
  --config configs/harvard.local.json \
  --candidate-jsonl /private/calibration/05_final/medcat_candidates.jsonl.gz \
  --output /private/config/harvard_site_profile.json
```

Set these two fields in the local run config:

```json
"site_adaptation": {
  "mode": "profile_file",
  "profile_path": "/private/config/harvard_site_profile.json"
}
```

The profile contains aggregate distances only. It cannot expand the frozen
windows. Compare frozen and adaptive outputs on a labeled calibration or
regression set with `scripts/compare_adaptive_metrics.py`; only then run the
held-out private test set.

## Output

The final records are under:

```text
<output_root>/variant_<run_name>/05_final/medcat_candidates.jsonl.gz
```

The manifest records the effective hyperparameters, profile hash, and config
hash so that a run can be reproduced without exposing note text.
