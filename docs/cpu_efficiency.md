# CPU Efficiency Validation

This checkout retains the fixed Harvard extraction rules and reduces repeated
regex compilation, lab-alias preparation, and CDB loading. It adds no learned
classifier or adaptive pass. Dependencies, input arguments, and output schema
are unchanged.

Run it through the existing entry point described in
[the handoff guide](harvard_default_handoff.md):

```bash
sbatch slurm/templates/run_harvard_default.sbatch
```

Fill the three local asset paths in the template. Use this checkout directly;
the historical `harvard-default-v1.1` tag does not contain this optimization.
The production template retains its 4-CPU / 68-GB allocation. Validation used
4 CPUs / 40 GiB; that is not a certified minimum RAM requirement.

## Validation

Reference: `harvard-default-v1.1` (`957c700`). Each pair used the same node, four
CPUs, memory allocation, notes, modelpack, filters, task sizes, 7,000-character
linker cap, and hash seed. Times include fresh model loading and every extraction
stage. Neither prior note results nor prior-run lexicons were reused. OS disk
caches were not flushed; these are single paired measurements.

| Dataset | Notes | Reference | This checkout | Speedup |
|---|---:|---:|---:|---:|
| Natural MIMIC discharge notes | 1,000 | 716.20 s | 490.60 s | 1.46x |
| MIMIC-style synthetic | 240 | 93.05 s | 63.37 s | 1.47x |
| Independent adversarial synthetic | 360 | 86.54 s | 58.67 s | 1.47x |

All five stages had identical complete record multisets on all three corpora,
including 318,591 final records on the natural notes. The original evaluators
were rerun on the labeled synthetic sets:

| Dataset | Metric | Reference | This checkout |
|---|---|---:|---:|
| MIMIC-style synthetic | Entity exact F1 | 0.9143 | 0.9143 |
| MIMIC-style synthetic | Assertion macro-F1 | 0.9437 | 0.9437 |
| MIMIC-style synthetic | Value bundle accuracy | 0.8462 | 0.8462 |
| Independent adversarial synthetic | Entity exact F1 | 0.7134 | 0.7134 |
| Independent adversarial synthetic | Assertion macro-F1 | 0.8486 | 0.8486 |
| Independent adversarial synthetic | Value bundle accuracy | 0.4378 | 0.4378 |

Assertion F1 is conditional on relaxed entity matches, not joint extraction F1.
The two established evaluators keep their existing value-subset definitions.
No comprehensive gold was available for the natural 1,000-note run, so its
result is output parity rather than an F1 claim. These are not Harvard private
test-set scores.

The actual `run_harvard_default.py` entry point was separately executed end to
end, reproducing all 5,405 reference MIMIC-style records. Seven tests passed,
including compiled-pattern eviction semantics, fixed default settings, mutable
alias-list isolation, hash-seed propagation, and explicit failure on a killed
worker.

## Runtime Behavior

- Compiled patterns and static aliases use bounded in-process caches. Clinical
  predictions are not cached or skipped.
- If the ontology CDB is the same as the modelpack CDB, the parent exports its
  dictionary once for that run. It is regenerated each invocation, checked
  against the requested settings, and removed after the final stage. A separate
  CDB keeps its original loading path.
- `PYTHONHASHSEED` defaults to `20260610` for extraction subprocesses. An explicit
  environment setting is respected. Equal-score MedCAT spelling candidates can
  otherwise change between fresh interpreter runs.
- A killed worker fails the pipeline rather than waiting indefinitely for a
  lost task. Insufficient memory still needs to be corrected before rerunning.

The historical linker-only benchmark is a different timing contract. These
measurements do not establish recovery of Harvard's reported fivefold slowdown;
the next deployment check is the same private notes and hardware on both releases.
