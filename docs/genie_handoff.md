# Collaborator Handoff

This document is the practical handoff for running the EHR CUI-assertion
extractor at a collaborator site. It assumes you have your own approved access
to the clinical notes, terminology files, and MedCAT modelpack.

## What This Package Does

The extractor turns MIMIC-style clinical notes into structured clinical records:

```text
raw note text
  -> span-level medical concept anchors
  -> schema-aware assertion, context, and value parsing
  -> structured CUI-assertion records
```

The main extraction path does not require an LLM. MedCAT is used as the
span-level concept linker. The schema layer then uses local sentence/window
context, section title, note category, semantic type, negation, uncertainty,
history, family/social cues, and lab/value cues to produce the final fields.
LLMs can be used later for audit or evaluation, but they are not required to
run extraction.

## What You Need Locally

Keep all restricted assets outside this git repository:

| Asset | Purpose |
|---|---|
| Clinical note JSONL or JSONL.GZ shards | input notes |
| Licensed MedCAT modelpack ZIP | concept span linking |
| Licensed MedCAT CDB directory/file | conservative disease coverage overlay |
| `MRCONSO.RRF` or `MRCONSO.RRF.gz` | optional SNOMED-to-UMLS CUI mapping |
| Private output directory | candidate records, summaries, strict pairs |

Input notes should contain a `text` field. These optional fields are preserved
when present and help with downstream grouping: `note_index`, `row_id`,
`subject_id`, `hadm_id`, `chartdate`, `category`, and `description`.

Example input record:

```json
{
  "note_index": 1,
  "subject_id": "local_subject_id",
  "hadm_id": "local_visit_id",
  "category": "Discharge summary",
  "text": "Patient denies chest pain. Sodium down to 128 after diuresis."
}
```

Do not commit note text, modelpacks, UMLS files, generated outputs, logs, local
paths, credentials, or API keys.

## Install

```bash
git clone https://github.com/LabRAI/EHR-CUI-Assertion-Extraction.git
cd EHR-CUI-Assertion-Extraction

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## Smoke Test

This verifies argument parsing, CPU detection, config writing, and manifest
writing without loading MedCAT or restricted data:

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

Expected smoke-test outputs:

```text
/tmp/ehr_extraction_smoke/
  toy_dry_run.config.json
  toy_dry_run.run_manifest.json
```

## Run Extraction

Copy the example policy and keep the local copy out of git:

```bash
cp configs/linker_filter_policy.example.json configs/linker_filter_policy.local.json
```

Run the deployment wrapper inside your cluster scheduler for restricted data:

```bash
python3 scripts/run_final_extraction.py \
  --notes-jsonl-gz /private/note_shards/notes_shard_000000.jsonl.gz \
  --modelpack /private/models/medcat_modelpack.zip \
  --cdb-path /private/models/medcat_modelpack/cdb \
  --filter-policy configs/linker_filter_policy.local.json \
  --output-root /private/outputs/ehr_extraction \
  --variant-name genie_shard000
```

For multiple shards, repeat `--notes-jsonl-gz`:

```bash
python3 scripts/run_final_extraction.py \
  --notes-jsonl-gz /private/note_shards/notes_shard_000000.jsonl.gz \
  --notes-jsonl-gz /private/note_shards/notes_shard_000001.jsonl.gz \
  --modelpack /private/models/medcat_modelpack.zip \
  --cdb-path /private/models/medcat_modelpack/cdb \
  --filter-policy configs/linker_filter_policy.local.json \
  --output-root /private/outputs/ehr_extraction \
  --variant-name genie_batch001
```

By default, the wrapper uses the CPU allocation exposed by SLURM or Linux CPU
affinity. To cap the run manually:

```bash
python3 scripts/run_final_extraction.py ... --workers 4
```

The wrapper blocks real non-SLURM processing unless `--allow-no-slurm` is
provided. Use that flag only for synthetic smoke tests or local development.

## Optional Site Profile

For a new note layout, first run the frozen extractor on a local calibration
split, then build and freeze a CPU-only aggregate profile:

```bash
python3 scripts/build_site_profile.py \
  --notes-jsonl-gz /private/calibration/notes.jsonl.gz \
  --candidate-jsonl /private/calibration/05_final/medcat_candidates.jsonl.gz \
  --output /private/config/site_profile.json
```

Pass the frozen file to the held-out run with
`--site-profile /private/config/site_profile.json`. Do not build or select the
profile using held-out test labels. See `docs/site_adaptation.md` for the
non-regression gate and privacy contract.

## Outputs To Check

A successful run writes:

```text
<output-root>/
  <variant>.config.json
  <variant>.run_manifest.json
  variant_<variant>/
    final_pipeline_manifest.json
    05_final/medcat_candidates.jsonl.gz
    reports/
    extraction/
      prefork4_summary.json
      shard_*/medcat_candidate_stats.json
      shard_*/medcat_candidates.jsonl.gz
```

Check these first:

- `final_pipeline_manifest.json`: note count, CPU workers, and final output path;
- `05_final/medcat_candidates.jsonl.gz`: validated concept/assertion/value records;
- `reports/`: measurement, ontology, assertion, and final-refinement summaries.

## Component-Level Runs

The final wrapper runs measurement, ontology, assertion, and final refinement
by default. Run an individual stage only for a controlled audit or ablation.

```bash
python3 scripts/add_medcat_measurement_overlay.py \
  --notes-jsonl-gz /private/note_shards/notes_shard_000000.jsonl.gz \
  --candidate-jsonl /private/outputs/ehr_extraction/variant_genie_shard000/01_linker/medcat_candidates.jsonl.gz \
  --output-dir /private/outputs/measurement_overlay/shard_000000 \
  --summary-json /private/outputs/measurement_overlay/shard_000000/summary.json
```

The overlay preserves the candidate schema and fills `value_status`,
`numeric_value`, `unit`, `value_text`, and `measurement_context` when a
conservative local numeric value can be parsed.

## Optional UMLS Normalization

If your MedCAT modelpack returns SNOMED identifiers and your downstream use
requires UMLS CUIs, build a local crosswalk from your licensed UMLS release:

```bash
python3 scripts/build_snomed_umls_crosswalk.py \
  --mrconso /private/umls/META/MRCONSO.RRF.gz \
  --candidate-jsonl /private/outputs/ehr_extraction/variant_genie_shard000/05_final/medcat_candidates.jsonl.gz \
  --output-dir /private/outputs/umls_crosswalk
```

Then annotate candidates:

```bash
python3 scripts/annotate_snomed_umls_candidates.py \
  --crosswalk-jsonl /private/outputs/umls_crosswalk/snomed_umls_crosswalk.jsonl.gz \
  --candidate-jsonl /private/outputs/ehr_extraction/variant_genie_shard000/05_final/medcat_candidates.jsonl.gz \
  --output-jsonl /private/outputs/annotated/shard_000000/medcat_candidates_with_umls.jsonl.gz \
  --summary-json /private/outputs/annotated/shard_000000/summary.json
```

## Build Strict Pair Splits

After UMLS annotation, create train/dev/test pair splits:

```bash
python3 scripts/build_full_shard_strict_pair_splits.py \
  --input-root /private/outputs/annotated \
  --output-dir /private/outputs/pair_splits \
  --release-name strict_pairs_v1
```

The resulting records follow `configs/extraction_schema.json`.

Important fields include:

- `cui`, `canonical_name`, `span`;
- `concept_category`;
- `assertion_status`;
- `experiencer`, `temporality`;
- `value_status`, `numeric_value`, `unit`, `measurement_context`;
- `section`, `evidence`;
- `linker_name`, `linker_score`, `confidence`, `needs_review`.

## Operational Notes

- Run real clinical-data extraction through the cluster scheduler.
- Keep output directories private, typically mode `700`.
- Keep output files private, typically mode `600`.
- The current optimized path uses one loaded MedCAT parent process, forked CPU
  workers, batched MedCAT inference, and compressed streaming output.

## What To Share Back

For routine troubleshooting or benchmarking, share only aggregate artifacts:

- `<variant>.run_manifest.json`;
- `prefork4_summary.json`;
- `medcat_candidate_stats.json`;
- runtime, CPU count, memory request, and note count.

Do not share candidate JSONL files, evidence windows, raw note snippets, UMLS
release files, MedCAT modelpacks, or any patient text unless a separate data
sharing agreement explicitly covers them.

## Minimal Success Checklist

1. Environment installs successfully.
2. Smoke test writes a config and run manifest.
3. One small private shard completes under SLURM.
4. `final_pipeline_manifest.json` reports the expected note count and worker count.
5. The final file contains nonzero kept/review/value-parse records.
6. Optional UMLS annotation succeeds if UMLS CUI output is required.
7. Strict pair split creation completes and validates against the schema.
8. Any optional site profile passes the local non-regression gate before use.
