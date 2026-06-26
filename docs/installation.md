# Installation

This repository is a code package. It does not download restricted clinical
data, UMLS terminology, SNOMED CT releases, or MedCAT modelpacks.

## Python Environment

```bash
git clone https://github.com/LabRAI/EHR-CUI-Assertion-Extraction.git
cd EHR-CUI-Assertion-Extraction

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## External Assets

Place restricted assets outside git:

```text
<PRIVATE_PROJECT_DIR>/
  data/restricted/mimic.zip
  data/interim/note_shards/
  models/medcat/<licensed-modelpack>.zip
  umls/META/MRCONSO.RRF.gz
  outputs/
  logs/
```

Required external dependencies:

| Asset | Why it is needed | Included here? |
|---|---|---|
| MIMIC-style note archive | source clinical notes | no |
| MedCAT modelpack | span-level concept linker | no |
| UMLS `MRCONSO.RRF(.gz)` | SNOMED-to-UMLS CUI mapping | no |

## Smoke Test Without Restricted Data

You can verify argument parsing and auto CPU detection on synthetic notes:

```bash
python3 scripts/run_extraction_auto.py \
  --notes-jsonl-gz examples/toy_notes.jsonl \
  --modelpack /tmp/not_loaded_in_dry_run.zip \
  --filter-policy configs/linker_filter_policy.example.json \
  --output-root /tmp/ehr_extraction_smoke \
  --variant-name toy_dry_run \
  --workers 2 \
  --allow-no-slurm \
  --dry-run
```

This writes a config and run manifest but does not load MedCAT.
