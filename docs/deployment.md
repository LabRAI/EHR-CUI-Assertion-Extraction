# Deployment Notes

The recommended deployment entry point is:

```bash
python3 scripts/run_extraction_auto.py
```

It expects private note shards plus a licensed MedCAT modelpack. The runner:

- detects available CPU cores from `SLURM_CPUS_PER_TASK`, then
  `SLURM_CPUS_ON_NODE`, then Linux CPU affinity, then `os.cpu_count()`;
- splits the provided note shards into explicit tasks;
- loads MedCAT once in a parent process and forks CPU workers;
- uses MedCAT batched `get_entities_multi_texts` inside each worker;
- writes a run manifest, task config, shard-level stats, and extraction summary;
- blocks restricted clinical-data runs outside SLURM unless `--allow-no-slurm`
  is explicitly used for synthetic smoke tests.

## Minimal SLURM Run

```bash
python3 scripts/run_extraction_auto.py \
  --notes-jsonl-gz /private/note_shards/notes_shard_000000.jsonl.gz \
  --modelpack /private/models/medcat_modelpack.zip \
  --filter-policy configs/linker_filter_policy.local.json \
  --output-root /private/outputs/extraction_auto \
  --variant-name shard000_auto
```

By default, the runner uses all CPU cores assigned by SLURM. To cap usage:

```bash
python3 scripts/run_extraction_auto.py ... --workers 4
```

For many shards, pass `--notes-jsonl-gz` multiple times:

```bash
python3 scripts/run_extraction_auto.py \
  --notes-jsonl-gz /private/note_shards/notes_shard_000000.jsonl.gz \
  --notes-jsonl-gz /private/note_shards/notes_shard_000001.jsonl.gz \
  --modelpack /private/models/medcat_modelpack.zip \
  --filter-policy configs/linker_filter_policy.local.json \
  --output-root /private/outputs/extraction_auto \
  --variant-name multi_shard_auto
```

## Outputs

```text
<output-root>/
  <variant>.config.json
  <variant>.run_manifest.json
  variant_<variant>/extraction/
    prefork4_summary.json
    shard_*/medcat_candidate_stats.json
```

Candidate JSONL files are kept by default because the next UMLS-mapping stage
needs them. Add `--delete-candidate-files` only for benchmark runs where
shard-level stats are sufficient.

## Dependency Boundary

The repository installs Python dependencies only. It does not install or
download restricted assets:

- MIMIC notes must be obtained through the official credentialed process;
- UMLS/SNOMED files must be downloaded under the user's license;
- MedCAT modelpacks must be downloaded under their license/access process.
