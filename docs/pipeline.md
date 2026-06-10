# Extraction Pipeline

## 1. Shard Notes

`scripts/shard_mimic_notes.py` streams `NOTEEVENTS` from a locally staged MIMIC
archive and writes private compressed JSONL shards. The shard manifest is the
input for full-corpus extraction and scaling benchmarks.

Output shape:

```json
{
  "note_index": 1,
  "subject_id": "...",
  "hadm_id": "...",
  "category": "Discharge summary",
  "text": "..."
}
```

## 2. Link Candidate Concepts

`scripts/extract_medcat_candidates.py` loads a licensed MedCAT modelpack and
creates span-level concept candidates. The linker supplies anchors: span,
concept ID, preferred name, score, and semantic type IDs.

The schema-aware filter then adds local context:

- section title;
- bounded evidence window;
- note category;
- negation, uncertainty, history, family/social, and lab/value flags;
- candidate action such as keep, review, reject, value parse, or context only.

The optimized benchmark path,
`scripts/extract_medcat_candidates_prefork4.py`, loads MedCAT once in the parent
process and forks CPU workers. This reduces repeated model loading while keeping
the same candidate logic.

## 3. Normalize To UMLS CUI

Some MedCAT modelpacks return SNOMED IDs. When UMLS CUI output is required:

1. `scripts/build_snomed_umls_crosswalk.py` builds a target-specific
   SNOMED-to-UMLS mapping from a licensed `MRCONSO.RRF(.gz)`;
2. `scripts/annotate_snomed_umls_candidates.py` adds preferred UMLS CUI and
   mapping-status fields to each candidate record.

## 4. Create Strict Pair Splits

`scripts/build_full_shard_strict_pair_splits.py` streams annotated candidates,
applies `strict_decision`, and writes train/dev/test splits by subject hash.

Pair shape:

```json
{
  "cui": "C0000000",
  "canonical_name": "synthetic clinical concept",
  "span": "concept mention",
  "concept_category": "finding",
  "assertion_status": "possible",
  "experiencer": "patient",
  "temporality": "current",
  "value_status": "not_applicable",
  "numeric_value": null,
  "unit": null,
  "section": "assessment",
  "evidence": "... bounded context ...",
  "linker_name": "medcat_umls_snomed",
  "linker_score": 0.99,
  "confidence": "high",
  "needs_review": false
}
```

## 5. Validate And Benchmark

Use the validation and summary utilities to check:

- shard completion;
- candidate record counts;
- strict eligible rate;
- assertion/category distribution;
- runtime and memory scaling across CPU settings.

`scripts/build_extraction_scaling_benchmark.py` creates portable SLURM configs
and job scripts. `scripts/summarize_extraction_scaling_benchmark.py` turns
completed runs into CSV, JSON, Markdown, and an optional runtime curve.

