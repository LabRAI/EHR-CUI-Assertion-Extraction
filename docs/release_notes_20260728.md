# Release Notes: 2026-07-28 Extraction Refinement

This version publishes the current clinical extraction profile for collaborator
testing. It contains code only. No clinical notes, modelpacks, UMLS files,
generated outputs, logs, or patient text are included.

## What Changed

- Entity coverage: added a stricter MedCAT-CDB disease ontology overlay mode
  and safer overlap handling so exact disease anchors can be recovered without
  replacing useful non-disease context rows.
- Entity quality: retained the broad-anchor specificity gate for generic spans
  that are poor strict-pair events.
- Assertion status: expanded local context logic for negation, possible,
  hypothetical, conditional, historical, family/social, and low-value assertion
  spans.
- Lab/vital values: improved value-span normalization, trend cues such as
  `down to` and `up to`, common aliases, units, and numeric parsing.
- Measurement overlay: added `scripts/add_medcat_measurement_overlay.py` as an
  optional conservative repair stage for missed lab/vital aliases.
- Deployment: kept the optimized prefork/batched CPU extraction path and the
  auto-detecting wrapper as the recommended runtime path.

## Recommended Clinical Profile

For clinical-note extraction, use the deployment wrapper plus conservative
post-linker stages:

```text
run_extraction_auto.py
  -> add_medcat_cdb_ontology_overlay.py --overlap-mode add_missing
  -> apply_candidate_specificity_gate.py
  -> optional add_medcat_measurement_overlay.py
  -> UMLS annotation, if UMLS CUI output is required
  -> strict pair construction
```

Use the broader public-benchmark span profiles only for controlled benchmark
ablations. They are not the default clinical-note profile.

## Validation Snapshot

Entity refinement was checked against clinical-style synthetic notes and public
disease/entity benchmarks:

| Dataset | Before | Current | Metric |
|---|---:|---:|---|
| MIMIC-style synthetic | 0.913 | 0.913 | exact entity F1 |
| LLM synthetic clinical | 0.870 | 0.870 | exact entity F1 |
| NCBI Disease test | 0.687 | 0.695 | exact entity F1 |
| BC5CDR Disease test | 0.698 | 0.705 | exact entity F1 |
| BioRED Disease test | 0.685 | 0.696 | exact entity F1 |
| MedMentions disease-ish 1K | 0.431 | 0.447 | exact entity F1 |

Clinical assertion checks remain strong under the accepted profile:

| Check | Current |
|---|---:|
| MIMIC3 natural assertion audit macro F1 | 0.921 |
| MIMIC3 natural assertion audit accuracy | 0.944 |
| Balanced assertion stress macro F1 | 0.966 |

Lab/value checks support the new value parsing and measurement overlay path:

| Check | Current |
|---|---:|
| MIMIC synthetic value coverage | 100.0% |
| LLM synthetic clinical value coverage | 95.1% |
| Bundle-level value extraction score | 1.000 |

## Readiness Note

This version is ready for a second collaborator-side evaluation under the
clinical extraction profile above. The remaining high-value checks are
site-local: cancer/procedure/lab coverage, boundary granularity, and assertion
status on each site's gold-label notes.
