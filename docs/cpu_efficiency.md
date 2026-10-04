# CPU Efficiency Validation

This checkout retains the fixed Harvard extraction rules and reduces repeated
regex compilation, target searches, lab-alias preparation, and CDB loading.
Large assertion jobs use the allocated workers in bounded batches; measurement,
ontology and final refinement process whole notes in parallel. It adds no learned
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

Reference: the prior accepted efficiency release (`d4c6847`). Each pair used the same node, four
CPUs, memory allocation, notes, modelpack, filters, task sizes, 7,000-character
linker cap, and hash seed. Times include fresh model loading and every extraction
stage. Neither prior note results nor prior-run lexicons were reused. OS disk
caches were not flushed; these are single paired measurements.

| Dataset | Reference | Reference time | This checkout | Speedup |
|---|---|---:|---:|---:|
| Natural MIMIC, 1,000 notes | Prior efficiency release | 353.43 s | 291.40 s | 1.21x |
| MIMIC-style synthetic, 240 | Prior efficiency release | 57.70 s | 62.41 s | 0.92x |
| MIMIC-style synthetic, reverse order | Prior efficiency release | 59.63 s | 58.40 s | 1.02x |
| Adversarial synthetic, 360 | Prior efficiency release | 62.21 s | 57.28 s | 1.09x |

Natural-note runtime falls by 17.55%. The first small synthetic comparison was
slower; reversing the order gave a small gain. Both are reported: no stable gain
is established on the short MIMIC-style corpus. Model loading dominates it, and
most of the initial slowdown was in the unchanged linker stage. Each pair used
one shared compute node; the reverse-order pair ran on another node.

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
end in 61.99 seconds, reproducing all 5,405 original Harvard MIMIC-style records. Thirteen
tests passed, including compiled-pattern eviction semantics, fixed default
settings, mutable return-value isolation, serial/parallel row and summary
parity, malformed-input propagation, hash seeds, and killed-worker failure.
The previous scoped-rule implementation had also passed 95,040 differential
checks; it is unchanged in this pass.

Isolated natural-note stage comparisons, including I/O, give:

| Stage | Prior release | Current, one worker | Current, four workers |
|---|---:|---:|---:|
| Measurement | 43.89 s | 43.68 s | 19.93 s |
| Ontology | 41.16 s | 40.23 s | 19.88 s |
| Final refinement | 40.22 s | 37.13 s | 18.01 s |

Ordered full records and summary counts matched in every variant. Differential
fixtures cover duplicate/interleaved rows, missing notes, notes without entities,
empty inputs, lab repair, ontology overlap modes and malformed ontology offsets.

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
- Measurement, ontology and final refinement use the same activation threshold
  and process each note with all its rows together. Workers inherit input state
  under fork and return serialized output in order. Pending note tasks are
  bounded; total input memory is not, since the stages still load their rows.
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

A 10,000-natural-note paired test has been submitted with four CPUs and 68 GiB
on both sides. It is not a completed scaling claim. The input contains 10,000
distinct note IDs, retains natural duplicate text, and extends the same ordered
1K sample. Both variants retain the existing 7,000-character linker cap.
