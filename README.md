# EHR CUI-Assertion Extraction

Schema-aware extraction code for turning clinical notes into structured
concept/assertion records:

```text
MIMIC-style notes
  -> MedCAT span-level entity linking
  -> schema-aware candidate filtering
  -> SNOMED/UMLS normalization
  -> strict CUI-assertion pairs
```

This repository contains code only. It does not include MIMIC notes, UMLS
terminology files, MedCAT modelpacks, generated candidate files, logs, or any
patient text.

## Why This Exists

The downstream EHR generator is trained on structured clinical events rather
than raw note text. The extraction layer therefore needs to preserve clinically
important signals that are easy to lose in naive note processing:

- positive, negative, possible, historical, family, and social context;
- lab/vital value status such as high, low, normal, or abnormal;
- numeric value and unit fields when they are available;
- section and note-category cues that disambiguate abbreviations and context.

The linker is used as a high-recall anchor generator. The final training view is
created by conservative schema rules and strict gates, not by accepting every
raw linker output.

## Repository Layout

```text
configs/
  extraction_schema.json                 # final pair schema
  linker_filter_policy.example.json      # sanitized schema-aware filter policy
docs/
  data_and_license.md                    # restricted-data handling notes
  pipeline.md                            # stage-by-stage extraction workflow
examples/
  toy_notes.jsonl                        # synthetic notes only
scripts/
  run_extraction_auto.py                 # deployment-friendly auto CPU runner
  shard_mimic_notes.py                   # local restricted-note sharding
  extract_medcat_candidates.py           # standard MedCAT + schema filter
  extract_medcat_candidates_prefork4.py  # optimized prefork benchmark extractor
  build_snomed_umls_crosswalk.py         # licensed UMLS crosswalk builder
  annotate_snomed_umls_candidates.py     # add UMLS CUI mapping to candidates
  build_full_shard_strict_pair_splits.py # strict pair split builder
  build_strict_sft_dataset.py            # strict decision and pair construction
  validate_medcat_full_shards.py         # shard-level validation
  summarize_*.py                         # summary and scaling reports
slurm/templates/
  *.sbatch                               # cluster job templates
```

## Data Boundary

You must stage restricted assets locally before running the pipeline:

- MIMIC-III or equivalent credentialed clinical note archive;
- licensed MedCAT modelpack;
- licensed UMLS release files such as `MRCONSO.RRF` or `MRCONSO.RRF.gz`.

Do not commit these assets. Do not commit generated extraction outputs unless
they are synthetic and manually verified to contain no patient text.

## Quick Start

Create an environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

For more installation detail, see [`docs/installation.md`](docs/installation.md).

Copy the policy template and fill in local paths outside git:

```bash
cp configs/linker_filter_policy.example.json configs/linker_filter_policy.local.json
```

Run the deployment-friendly extractor. It automatically detects CPU cores from
SLURM and uses the optimized prefork/batched linker path:

```bash
python scripts/run_extraction_auto.py \
  --notes-jsonl-gz /path/to/private/note_shards/notes_shard_000000.jsonl.gz \
  --modelpack /path/to/licensed/medcat_modelpack.zip \
  --filter-policy configs/linker_filter_policy.local.json \
  --output-root /path/to/private/extraction_auto \
  --variant-name shard000_auto
```

Shard notes through a SLURM job:

```bash
python scripts/shard_mimic_notes.py \
  --zip /path/to/restricted/mimic.zip \
  --output-dir /path/to/private/note_shards \
  --notes-per-shard 5000
```

Extract linker candidates from a shard:

```bash
python scripts/extract_medcat_candidates.py \
  --notes-jsonl-gz /path/to/private/note_shards/notes_shard_000000.jsonl.gz \
  --modelpack /path/to/licensed/medcat_modelpack.zip \
  --filter-policy configs/linker_filter_policy.local.json \
  --output-dir /path/to/private/extraction/shard_000000 \
  --selection-mode range \
  --start-note-index 1 \
  --end-note-index 5000 \
  --output-mode strict_pre_umls \
  --compress-output
```

For deployable CPU auto-detection and parallel task splitting, prefer
`scripts/run_extraction_auto.py`; the lower-level extractor remains available
for debugging and controlled ablations.

Build a SNOMED-to-UMLS crosswalk from licensed UMLS files:

```bash
python scripts/build_snomed_umls_crosswalk.py \
  --mrconso /path/to/UMLS/META/MRCONSO.RRF.gz \
  --candidate-jsonl /path/to/private/extraction/shard_000000/medcat_candidates.jsonl.gz \
  --output-dir /path/to/private/umls_crosswalk
```

Annotate candidates and create strict pair splits:

```bash
python scripts/annotate_snomed_umls_candidates.py \
  --crosswalk-jsonl /path/to/private/umls_crosswalk/snomed_umls_crosswalk.jsonl.gz \
  --candidate-jsonl /path/to/private/extraction/shard_000000/medcat_candidates.jsonl.gz \
  --output-jsonl /path/to/private/annotated/shard_000000/medcat_candidates_with_umls.jsonl.gz \
  --summary-json /path/to/private/annotated/shard_000000/summary.json

python scripts/build_full_shard_strict_pair_splits.py \
  --input-root /path/to/private/annotated \
  --output-dir /path/to/private/pair_splits \
  --release-name strict_pairs_v1
```

For real MIMIC or licensed MedCAT/UMLS processing, run these commands inside
SLURM. The scripts intentionally block project-scale processing on login nodes
unless `--allow-no-slurm` is explicitly supplied for local synthetic tests.

## Current Benchmark Reference

On the internal 10K-note benchmark, the optimized prefork/batched linker path
processed 10K notes on 4 CPU cores in about 35 minutes while preserving the same
strict extraction target as the slower baseline. Use
`scripts/build_extraction_scaling_benchmark.py` and
`scripts/summarize_extraction_scaling_benchmark.py` to reproduce a site-local
runtime sweep.

## Deployment Entry Point

`scripts/run_extraction_auto.py` is the main deployment wrapper. It detects
SLURM CPU allocation or local CPU affinity, builds an explicit task config,
runs the prefork MedCAT extractor, and records a run manifest. See
[`docs/deployment.md`](docs/deployment.md) for details.

## Notes

- The code treats MedCAT as a candidate linker, not as the final labeler.
- The strict training view uses UMLS-normalized CUI IDs when a licensed
  SNOMED/UMLS crosswalk is available.
- Numeric lab/vital mentions are routed through value parsing instead of being
  reduced to plain present/absent assertions.
- Family/social context is preserved in the schema so it can be included or
  excluded by downstream training views.
