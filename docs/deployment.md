# Deployment Notes

The recommended deployment entry point is:

```bash
python3 scripts/run_final_extraction.py
```

It expects private note shards, a licensed MedCAT modelpack, and its CDB. The runner:

- detects available CPU cores from `SLURM_CPUS_PER_TASK`, then
  `SLURM_CPUS_ON_NODE`, then Linux CPU affinity, then `os.cpu_count()`;
- splits the provided note shards into explicit tasks;
- loads MedCAT once in a parent process and forks CPU workers;
- uses MedCAT batched `get_entities_multi_texts` inside each worker;
- applies measurement, conservative ontology, assertion-scope, and final
  boundary/category refinement;
- optionally applies a frozen CPU-generated site profile to local assertion
  and measurement scope;
- writes a final run manifest and stage-level summaries;
- blocks restricted clinical-data runs outside SLURM unless `--allow-no-slurm`
  is explicitly used for synthetic smoke tests.

## Minimal SLURM Run

```bash
python3 scripts/run_final_extraction.py \
  --notes-jsonl-gz /private/note_shards/notes_shard_000000.jsonl.gz \
  --modelpack /private/models/medcat_modelpack.zip \
  --cdb-path /private/models/medcat_modelpack/cdb \
  --filter-policy configs/linker_filter_policy.local.json \
  --output-root /private/outputs/extraction_auto \
  --variant-name shard000_auto
```

By default, the runner uses all CPU cores assigned by SLURM. To cap usage:

```bash
python3 scripts/run_final_extraction.py ... --workers 4
```

To use a profile built only from the site's calibration split:

```bash
python3 scripts/run_final_extraction.py ... \
  --site-profile /private/config/site_profile.json
```

The manifest records the adaptation mode and profile SHA-256. Omit
`--site-profile` to reproduce the frozen default exactly.

For the collaborator-facing one-config entry point, use
`docs/harvard_quickstart.md` and `scripts/run_collaborator.py`. It exposes the
validated linker, ontology, refinement, CPU, batch, and site-distance settings
without requiring edits to the extraction scripts.

For many shards, pass `--notes-jsonl-gz` multiple times:

```bash
python3 scripts/run_final_extraction.py \
  --notes-jsonl-gz /private/note_shards/notes_shard_000000.jsonl.gz \
  --notes-jsonl-gz /private/note_shards/notes_shard_000001.jsonl.gz \
  --modelpack /private/models/medcat_modelpack.zip \
  --cdb-path /private/models/medcat_modelpack/cdb \
  --filter-policy configs/linker_filter_policy.local.json \
  --output-root /private/outputs/extraction_auto \
  --variant-name multi_shard_auto
```

## Outputs

```text
<output-root>/
  <variant>.config.json
  <variant>.run_manifest.json
  variant_<variant>/
    final_pipeline_manifest.json
    05_final/medcat_candidates.jsonl.gz
    reports/
    extraction/prefork4_summary.json
```

`run_extraction_auto.py` is the linker-only entry point for runtime benchmarks;
it is not the complete collaborator pipeline.

## Dependency Boundary

The repository installs Python dependencies only. It does not install or
download restricted assets:

- MIMIC notes must be obtained through the official credentialed process;
- UMLS/SNOMED files must be downloaded under the user's license;
- MedCAT modelpacks must be downloaded under their license/access process.
