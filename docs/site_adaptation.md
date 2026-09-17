# CPU Site Adaptation

The optional site-adaptive path handles note-layout shift without sending raw
notes off site and without adding an LLM or GPU to production extraction.

## What Is Learned

`scripts/build_site_profile.py` makes one streaming pass over local calibration
notes and candidate records. It records only aggregate information:

- cue-to-target distances for each assertion family;
- concept-to-number distances for measurements;
- proportions of section-header, list, and table-like lines;
- sample sizes and the quantile used for each decision.

The profile never stores note text, entity strings, CUIs, section names, note
identifiers, or source paths. The JSON contract is defined in
`configs/site_profile.schema.json`.

## Scope Policy

Structure has priority over distance. A cue cannot cross a sentence, clause,
newline, contrast, or incompatible list boundary merely because it falls
inside a learned character window.

Distances are not represented by one global number. Negation, conditional,
possible, hypothetical, historical, current, uncertain, and measurement
contexts each receive separate before/after limits. Unlabeled profiling uses a
bounded quantile and may only tighten the frozen production limits. Sparse
families retain their frozen defaults.

## Private-Site Workflow

1. Create a local calibration split that is disjoint from the final test set.
2. Run the frozen extractor on calibration notes through SLURM.
3. Build `site_profile.json` from calibration notes and candidates.
4. Inspect aggregate diagnostics, record the SHA-256, and freeze the profile.
5. Run frozen-default and site-adaptive extraction on a labeled calibration or
   regression set.
6. Accept the profile only if required entity, assertion, and value metrics do
   not regress.
7. Run the accepted profile once on the held-out private test set.

The included metric gate checks entity precision/recall/F1, assertion accuracy,
macro-F1 and every supported assertion label, plus the value bundle and each
value component. It fails closed if any required metric decreases.

```bash
python3 scripts/build_site_profile.py \
  --notes-jsonl-gz /private/calibration/notes.jsonl.gz \
  --candidate-jsonl /private/calibration/05_final/medcat_candidates.jsonl.gz \
  --output /private/config/site_profile.json \
  --quantile 0.95 \
  --minimum-observations 25
```

Then pass the frozen file to the deployment wrapper:

```bash
python3 scripts/run_final_extraction.py ... \
  --site-profile /private/config/site_profile.json
```

Do not tune the profile on the held-out Harvard test labels. If a profile is
built from the unlabeled test corpus itself, report the evaluation as
transductive rather than held-out adaptation.

## Compute Requirement

The profiler and adaptive rules are CPU-only. Four CPU cores are sufficient;
memory use is bounded because distance distributions are maintained as
histograms rather than retaining note text or individual matches.

## Internal A/B Check

The implementation was checked on a 360-note development calibration set, an
independent 360-note blind stress set, and the frozen 240-note MIMIC-style
regression set. These are internal synthetic checks, not Harvard private-data
results.

| Evaluation | Frozen default | Site adaptive | Result |
|---|---:|---:|---|
| MIMIC-style entity exact F1 | 0.9143 | 0.9143 | unchanged |
| MIMIC-style assertion macro-F1 | 0.9437 | 0.9437 | unchanged |
| MIMIC-style value bundle | 0.8462 | 0.8462 | unchanged |
| Blind entity exact / relaxed F1 | 0.7134 / 0.8119 | 0.7134 / 0.8119 | unchanged |
| Blind assertion macro-F1 | 0.8486 | 0.8608 | +0.0122 |
| Blind value bundle / numeric | 0.4378 / 0.8514 | 0.4378 / 0.8514 | unchanged |

The blind assertion gain came from removing two distant PRN-to-entity leaks:
Conditional F1 increased from 0.9286 to 1.0000 while all other supported
assertion-label F1 values were unchanged or improved. Re-running the new code
without a profile reproduced both frozen candidate files byte-for-byte after
decompression.
