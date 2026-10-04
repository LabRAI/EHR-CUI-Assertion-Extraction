# CPU Efficiency Validation

This checkout retains the fixed Harvard extraction rules and reduces repeated
regex compilation, target searches, lab-alias preparation, and CDB loading.
Large assertion jobs use the allocated workers in bounded batches. It adds no learned
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

References: `harvard-default-v1.1` (`957c700`) and the first accepted efficiency
pass (`ed11227`). Each pair used the same node, four
CPUs, memory allocation, notes, modelpack, filters, task sizes, 7,000-character
linker cap, and hash seed. Times include fresh model loading and every extraction
stage. Neither prior note results nor prior-run lexicons were reused. OS disk
caches were not flushed; these are single paired measurements.

| Dataset | Reference | Reference time | This checkout | Speedup |
|---|---|---:|---:|---:|
| Natural MIMIC, 1,000 notes | Original Harvard release | 721.89 s | 355.18 s | 2.03x |
| Natural MIMIC, 1,000 notes | First efficiency pass | 495.95 s | 357.77 s | 1.39x |
| MIMIC-style synthetic, 240 | First efficiency pass | 59.39 s | 61.10 s | 0.97x |
| MIMIC-style synthetic, reverse order | First efficiency pass | 58.40 s | 57.27 s | 1.02x |
| Adversarial synthetic, 360 | First efficiency pass | 61.62 s | 57.20 s | 1.08x |

The two natural-note rows are separate paired experiments, not a three-way run.
The first small synthetic comparison was slightly slower; reversing the order
gave a small gain. Both are reported: no stable gain is established on the short
MIMIC-style corpus over the first efficiency pass. Model loading dominates it.

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
end in 58.83 seconds, reproducing all 5,405 reference MIMIC-style records. Ten
tests passed, including compiled-pattern eviction semantics, fixed default
settings, mutable return-value isolation, serial/parallel row and summary
parity, malformed-input propagation, hash seeds, and killed-worker failure.
Another 95,040 scoped-rule differential checks passed.

An isolated assertion-stage ablation on the same natural-note input took
60.50 seconds in the first efficiency pass, 40.95 seconds with target-search
reuse, and 10.94 seconds with reuse plus four workers. Ordered full records
and summary counts were identical. This stage-only ratio is not an end-to-end
speedup.

## Runtime Behavior

- Compiled patterns and static aliases use bounded in-process caches. Clinical
  predictions are not cached or skipped.
- A scoped rule is skipped only when its own required cue subexpression is
  absent. Present cues run the exact original target patterns and distances.
- Target occurrence matches are reused within a bounded, process-local cache
  of 64 text/target pairs. Callers receive independent lists.
- Assertion processing uses batches of 512 rows with at most twice the worker
  count pending. It preserves row order and all summary counts. The pipeline
  enables multiple workers at 20,000 linker rows; small inputs remain serial.
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
