# Extraction Pipeline

The production entry point is `scripts/run_final_extraction.py`. It runs one
fixed five-stage extraction profile and writes the final records under
`variant_<name>/05_final/medcat_candidates.jsonl.gz`.

```text
clinical note
  -> concept candidates
  -> measurement fields
  -> disease coverage
  -> assertion scope
  -> final structured records
```

## 1. Concept Candidates

The optimized MedCAT path links clinical spans and supplies the initial concept
ID, preferred name, semantic type, confidence, and evidence window. The linker
emits every non-rejected candidate because later stages need the review and
context candidates to recover measurements and exact disease mentions.

The runner detects its allocated CPUs, loads MedCAT once before forking workers,
uses batched inference, and streams compressed output.

## 2. Measurement Fields

`scripts/add_medcat_measurement_overlay.py` attaches target-local lab and vital
information when it is explicitly available:

- numeric value and comparator;
- unit;
- value status such as high, low, normal, or abnormal;
- measurement context such as observed value, target, or medication dose.

The parser handles narrative, list, table, reference-range, trend, and compact
panel layouts while keeping the clinical concept span separate from its value.

## 3. Disease Coverage

`scripts/add_medcat_cdb_ontology_overlay.py` uses the licensed MedCAT CDB as a
conservative SNOMED disease dictionary. Longest exact matches recover specific
disease spans missed by contextual linking. Broad anatomy and generic terms are
filtered, and existing linker candidates remain unchanged.

The final profile uses `--overlap-mode add_missing`; ontology matching adds only
missing disease anchors and does not replace candidates that already carry
assertion or value context.

## 4. Assertion Scope

`scripts/apply_assertion_scope_overlay.py` assigns target-local status rather
than applying a cue to an entire sentence. It separates present, absent,
possible, conditional, hypothetical, and historical mentions across clauses,
lists, sections, family context, and coordinated concepts.

## 5. Final Refinement

`scripts/refine_tracka_candidates.py` resolves remaining exact-boundary,
specificity, category, and duplicate-candidate conflicts. This stage produces
the collaborator-facing concept/assertion/value records.

## Output Contract

Each final row includes the available fields below:

```json
{
  "span": "sodium",
  "concept_id": "...",
  "concept_category_preview": "lab_result_or_value",
  "assertion_status_preview": "present",
  "value_status": "low",
  "numeric_value": "128",
  "unit": "mmol/L",
  "measurement_context": "observed_patient",
  "section": "laboratory data",
  "evidence": "Sodium down to 128 mmol/L after diuresis."
}
```

Some modelpacks return SNOMED identifiers. If UMLS CUI output is required,
build a licensed SNOMED-to-UMLS crosswalk with
`scripts/build_snomed_umls_crosswalk.py` and annotate the final records with
`scripts/annotate_snomed_umls_candidates.py`.

`scripts/build_full_shard_strict_pair_splits.py` can then create subject-level
train/dev/test splits for downstream model training. These normalization and
split-building steps are downstream of extraction and are not hidden inside
the production wrapper.

## Validation And Scaling

`docs/validation.md` records the frozen evaluation scope. Runtime sweeps use
`scripts/build_extraction_scaling_benchmark.py` and
`scripts/summarize_extraction_scaling_benchmark.py`. Real clinical data must be
processed through the cluster scheduler; project-scale login-node execution is
blocked by default.
