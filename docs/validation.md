# Validation Scope

This repository contains the current extraction profile only. The deployed
pipeline does not call an LLM. LLMs were used separately to propose adversarial
failure patterns and adjudicate synthetic test labels without access to
extractor predictions.

## Current Checks

| Evaluation set | Entity exact F1 | Entity relaxed F1 | Assertion result | Value result |
|---|---:|---:|---:|---:|
| MIMIC-style synthetic notes | 0.914 | 0.927 | macro-F1 0.944 | bundle 0.846; numeric 1.000 |
| Adversarial development notes | 0.852 | 0.872 | macro-F1 0.949 | bundle 0.564; numeric 0.970 |
| Adversarial blind notes | 0.713 | 0.812 | macro-F1 0.849 | bundle 0.438; numeric 0.851 |
| E3C diagnostic partition | 0.332 | 0.394 | polarity macro-F1 0.854 | not labeled |
| E3C frozen holdout | 0.250 | 0.349 | polarity macro-F1 0.773 | not labeled |

The blind set contains 360 synthetic notes and 1,111 gold entities across 30
failure mechanisms. Its gold labels were independently adjudicated from notes
and draft labels without access to extractor predictions. Extraction logic was
frozen before the blind predictions or scores were inspected. The optimized
deployment wrapper reproduced the frozen standard-chain MIMIC-style metrics
exactly after a full end-to-end parity check.

The E3C assertion ontology differs from the six-label extraction schema, so
only its native polarity and modality are evaluated. E3C is a domain-shift
check, not a substitute for MIMIC-style doctor-note validation.

## Failure Coverage

The adversarial audit covers 30 canonical mechanisms across:

- entity coverage, abbreviation disambiguation, and concept specificity;
- exact boundaries, nested spans, parenthetical aliases, and hyphenation;
- negation, uncertainty, conditional, hypothetical, and historical scope;
- family/social experiencer and section/clause attribution;
- numeric values, units, reference ranges, trends, and multi-value panels.

## Interpretation

Synthetic scores are stress-test evidence. They are not results on CLINES,
4CE, CORAL, or another collaborator's private data. A collaborator-side test
remains necessary because note layout, annotation policy, concept ontology,
and label prevalence differ by institution.

The blind audit supports a second-round collaborator evaluation, not a claim of
complete external validation. The largest remaining stress-test errors are:

- exact entity boundaries and missing concepts in table, nested-span,
  abbreviation, and hyphenation layouts;
- historical-versus-present scope, especially family and copied-forward text;
- value-status attachment, uncommon units, and multi-value table alignment.
