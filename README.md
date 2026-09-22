# EHR CUI-Assertion Extraction

Schema-aware extraction code for turning clinical notes into structured
concept/assertion records:

```text
MIMIC-style notes
  -> MedCAT span-level entity linking
  -> schema-aware candidate filtering
  -> lab/vital measurement parsing
  -> conservative MedCAT-CDB ontology coverage
  -> target-local assertion inference
  -> final boundary/category quality gate
  -> structured concept-assertion-value records
```

This repository contains code only. It does not include MIMIC notes, UMLS
terminology files, MedCAT modelpacks, generated candidate files, logs, or any
patient text.

## For Collaborators

If you are using this package to run extraction at another site, start with
[`docs/harvard_quickstart.md`](docs/harvard_quickstart.md). It gives the
shortest complete path from a fresh clone to a private extraction run using one
validated config file. The longer [`docs/genie_handoff.md`](docs/genie_handoff.md)
documents the individual stages.

For the current validation scope and limitations, see
[`docs/validation.md`](docs/validation.md).

The repository is designed so each site keeps its own restricted assets outside
git:

- local clinical note shards;
- a licensed MedCAT modelpack;
- licensed UMLS files if UMLS CUI normalization is needed;
- private output and log directories.

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
  collaborator_run.example.json          # one-config collaborator preset
  collaborator_run.schema.json           # config contract and ranges
  site_profile.schema.json               # optional CPU site-adaptation contract
  site_profile.example.json              # distance settings template
docs/
  harvard_quickstart.md                  # short collaborator start guide
  pre_loop_handoff.md                    # pinned release and performance handoff
  genie_handoff.md                       # external collaborator run guide
  data_and_license.md                    # restricted-data handling notes
  pipeline.md                            # stage-by-stage extraction workflow
  validation.md                          # current evaluation scope and limits
examples/
  toy_notes.jsonl                        # synthetic notes only
scripts/
  run_collaborator.py                    # one-config collaborator entry point
  run_final_extraction.py                # complete validated extraction stack
  build_site_profile.py                  # aggregate local layout/distance profiler
  run_extraction_auto.py                 # linker-only auto CPU runner
  shard_mimic_notes.py                   # local restricted-note sharding
  extract_medcat_candidates.py           # standard MedCAT + schema filter
  extract_medcat_candidates_prefork4.py  # optimized prefork linker engine
  add_medcat_cdb_ontology_overlay.py     # optional strict disease coverage overlay
  add_medcat_measurement_overlay.py      # optional lab/vital value overlay
  apply_candidate_specificity_gate.py    # optional broad-anchor quality gate
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
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

For more installation detail, see [`docs/installation.md`](docs/installation.md).

Copy the policy template and fill in local paths outside git:

```bash
cp configs/linker_filter_policy.example.json configs/linker_filter_policy.local.json
```

Check the wrapper without loading restricted data:

```bash
python3 scripts/run_final_extraction.py \
  --notes-jsonl-gz examples/toy_notes.jsonl \
  --modelpack /tmp/not_loaded_in_dry_run.zip \
  --cdb-path /tmp/not_loaded_in_dry_run_cdb \
  --filter-policy configs/linker_filter_policy.example.json \
  --output-root /tmp/ehr_extraction_smoke \
  --variant-name toy_dry_run \
  --workers 2 \
  --allow-no-slurm \
  --dry-run
```

Run the complete extractor. It automatically detects CPU cores from SLURM,
uses the optimized prefork/batched linker path, and then applies the validated
measurement, ontology, assertion, and final refinement stages:

```bash
python3 scripts/run_final_extraction.py \
  --notes-jsonl-gz /path/to/private/note_shards/notes_shard_000000.jsonl.gz \
  --modelpack /path/to/licensed/medcat_modelpack.zip \
  --cdb-path /path/to/licensed/medcat_modelpack/cdb \
  --filter-policy configs/linker_filter_policy.local.json \
  --output-root /path/to/private/extraction_auto \
  --variant-name shard000_auto
```

Shard notes through a SLURM job:

```bash
python3 scripts/shard_mimic_notes.py \
  --zip /path/to/restricted/mimic.zip \
  --output-dir /path/to/private/note_shards \
  --notes-per-shard 5000
```

Extract linker candidates from a shard:

```bash
python3 scripts/extract_medcat_candidates.py \
  --notes-jsonl-gz /path/to/private/note_shards/notes_shard_000000.jsonl.gz \
  --modelpack /path/to/licensed/medcat_modelpack.zip \
  --filter-policy configs/linker_filter_policy.local.json \
  --output-dir /path/to/private/extraction/shard_000000 \
  --selection-mode range \
  --start-note-index 1 \
  --end-note-index 5000 \
  --output-mode all_non_rejected \
  --compress-output
```

Use `all_non_rejected` when any downstream measurement, ontology, assertion,
or final-refinement stage will consume the linker output. The stricter
`strict_pre_umls` mode is reserved for standalone linker benchmarks.

Optionally add a strict disease ontology overlay from the licensed MedCAT CDB.
This can recover exact disease anchors missed by contextual linking while
keeping broad single-word terms filtered out. For clinical notes, prefer
`--overlap-mode add_missing` so existing MedCAT rows keep their local assertion
and context fields:

```bash
python3 scripts/add_medcat_cdb_ontology_overlay.py \
  --cdb-path /path/to/licensed/medcat_modelpack/cdb \
  --notes-jsonl-gz /path/to/private/note_shards/notes_shard_000000.jsonl.gz \
  --candidate-jsonl /path/to/private/extraction/shard_000000 \
  --output-dir /path/to/private/extraction_overlay/shard_000000 \
  --overlap-mode add_missing \
  --summary-json /path/to/private/extraction_overlay/ontology_overlay_summary.json
```

Optionally apply a broad-anchor specificity gate before strict pair construction.
This demotes generic disease candidates such as `disease`, `disorder`, `brain`,
`liver`, and `renal` to `reject_candidate`, while preserving the same candidate
JSONL schema:

```bash
python3 scripts/apply_candidate_specificity_gate.py \
  --candidate-jsonl /path/to/private/extraction_overlay/shard_000000 \
  --output-dir /path/to/private/extraction_specific/shard_000000 \
  --summary-json /path/to/private/extraction_specific/specificity_gate_summary.json
```

Optionally apply a conservative measurement overlay for common lab/vital aliases
that are easy for a general linker to miss or over-expand:

```bash
python3 scripts/add_medcat_measurement_overlay.py \
  --notes-jsonl-gz /path/to/private/note_shards/notes_shard_000000.jsonl.gz \
  --candidate-jsonl /path/to/private/extraction_specific/shard_000000 \
  --output-dir /path/to/private/extraction_measurements/shard_000000 \
  --summary-json /path/to/private/extraction_measurements/measurement_overlay_summary.json
```

`scripts/run_extraction_auto.py` remains available for linker-only scaling
benchmarks. Use `scripts/run_final_extraction.py` for collaborator extraction.

Build a SNOMED-to-UMLS crosswalk from licensed UMLS files:

```bash
python3 scripts/build_snomed_umls_crosswalk.py \
  --mrconso /path/to/UMLS/META/MRCONSO.RRF.gz \
  --candidate-jsonl /path/to/private/extraction/shard_000000/medcat_candidates.jsonl.gz \
  --output-dir /path/to/private/umls_crosswalk
```

Annotate candidates and create strict pair splits:

```bash
python3 scripts/annotate_snomed_umls_candidates.py \
  --crosswalk-jsonl /path/to/private/umls_crosswalk/snomed_umls_crosswalk.jsonl.gz \
  --candidate-jsonl /path/to/private/extraction/shard_000000/medcat_candidates.jsonl.gz \
  --output-jsonl /path/to/private/annotated/shard_000000/medcat_candidates_with_umls.jsonl.gz \
  --summary-json /path/to/private/annotated/shard_000000/summary.json

python3 scripts/build_full_shard_strict_pair_splits.py \
  --input-root /path/to/private/annotated \
  --output-dir /path/to/private/pair_splits \
  --release-name strict_pairs_v1
```

For real MIMIC or licensed MedCAT/UMLS processing, run these commands inside
SLURM. The scripts intentionally block project-scale processing on login nodes
unless `--allow-no-slurm` is explicitly supplied for local synthetic tests.

## Optional CPU Site Adaptation

For a new institution, build a sanitized profile from a local calibration split
and freeze it before evaluating a held-out test split:

```bash
python3 scripts/build_site_profile.py \
  --notes-jsonl-gz /private/calibration/notes.jsonl.gz \
  --candidate-jsonl /private/calibration/05_final/medcat_candidates.jsonl.gz \
  --output /private/config/site_profile.json

python3 scripts/run_final_extraction.py \
  ... \
  --site-profile /private/config/site_profile.json
```

The profile uses aggregate cue-to-target and concept-to-value distances. It
does not contain note text, entity strings, CUIs, section names, or source
paths. Structural boundaries remain authoritative, and unlabeled adaptation
can only tighten the validated windows. The frozen default is unchanged when
`--site-profile` is omitted. See [`docs/site_adaptation.md`](docs/site_adaptation.md).
Use `scripts/compare_adaptive_metrics.py` as a fail-closed calibration gate
before accepting a profile.

## Current Benchmark Reference

On the internal 10K-note benchmark, the optimized prefork/batched linker path
processed 10K notes on 4 CPU cores in about 35 minutes. Use
`scripts/build_extraction_scaling_benchmark.py` and
`scripts/summarize_extraction_scaling_benchmark.py` to reproduce a site-local
runtime sweep.

## Deployment Entry Point

`scripts/run_final_extraction.py` is the main deployment wrapper. It detects
SLURM CPU allocation or local CPU affinity, runs the prefork MedCAT linker,
applies the validated post-linker stages, and records a final manifest. See
[`docs/deployment.md`](docs/deployment.md) for details.

## Notes

- The code treats MedCAT as a candidate linker, not as the final labeler.
- The final profile uses the licensed MedCAT CDB as a conservative
  exact-match dictionary for disease anchors; it is not a substitute for the
  schema-aware assertion and strict-pair gates.
- The optional specificity gate is a final candidate-quality filter for broad
  disease anchors that are poor strict-pair training events.
- The final profile's measurement stage recovers common lab/vital aliases and keeps
  numeric value/unit fields in the same candidate JSONL schema.
- The strict training view uses UMLS-normalized CUI IDs when a licensed
  SNOMED/UMLS crosswalk is available.
- Numeric lab/vital mentions are routed through value parsing instead of being
  reduced to plain present/absent assertions.
- Family/social context is preserved in the schema so it can be included or
  excluded by downstream training views.
