# Pre-Loop Harvard Handoff

This is the final validated extraction release before the proposed black-box
distance familiarization loop. The loop is intentionally not included in this
release.

## Release Identity

- Algorithmic extraction release: commit `103803c`
- Collaborator packaging and one-config entry point: commit `01878b5`
- Release tag: `pre-loop-handoff-20260921`
- Runtime: CPU-only; no LLM or GPU is required

Commit `01878b5` only adds the collaborator configuration interface and
documentation. It does not change the extraction logic or the validated
metrics from `103803c`.

## Best Validated Result

The best internally validated configuration is the bounded site-adaptive
profile: structure is evaluated first, and local cue/value distance limits may
only tighten the frozen defaults. The profile is generated from calibration
data and is not a trained neural model.

| Evaluation | Frozen default | Site-adaptive | Result |
|---|---:|---:|---|
| MIMIC-style entity exact F1 | 0.9143 | 0.9143 | unchanged |
| MIMIC-style assertion macro-F1 | 0.9437 | 0.9437 | unchanged |
| MIMIC-style value bundle | 0.8462 | 0.8462 | unchanged |
| Independent blind entity exact F1 | 0.7134 | 0.7134 | unchanged |
| Independent blind assertion macro-F1 | 0.8486 | 0.8608 | +0.0122 |
| Independent blind Conditional F1 | 0.9286 | 1.0000 | +0.0714 |
| Independent blind value bundle | 0.4378 | 0.4378 | unchanged |

The strict non-regression gates passed. The default path also reproduced the
previous frozen candidate outputs byte-for-byte after decompression.

## What Harvard Should Run

Start with the frozen default on the local data. This establishes the direct
second-round baseline:

```bash
python scripts/run_collaborator.py init \
  --output configs/harvard.local.json
python scripts/run_collaborator.py validate \
  --config configs/harvard.local.json
sbatch --export=ALL,CONFIG=$PWD/configs/harvard.local.json \
  slurm/templates/collaborator_run.sbatch
```

If a calibration split is available, build a local site profile and compare it
against the frozen run using the fail-closed metric gate. Use the local profile
only if it passes the required non-regression checks. The internal synthetic
profile used for the result table is not a Harvard profile and should not be
copied into the Harvard run.

## Included Scope

The release contains the validated linker, ontology overlay, measurement/value
parsing, assertion-scope handling, entity-boundary refinement, and bounded CPU
site adaptation. All restricted notes, MedCAT modelpacks, CDB files, UMLS files,
profiles, and output files remain local to the collaborating site.

The proposed distance sweep/replay loop, black-box structure discovery, and
automatic setting selection are future work and are not part of this handoff.
