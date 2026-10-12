# Numeric Structure Parsing

The fixed extraction pipeline recognizes selected lab layouts using original
note offsets before falling back to the contextual parser. It does not add a
second extraction run, network call, model dependency, or site calibration.

## Supported Layouts

| Synthetic example | Numeric output |
|---|---|
| `Creatinine 0. 79 mg/dL` | `0.79` |
| `BP 120 / 80 mmHg` | `120/80` |
| `Hemoglobin Ref Range: 13.5-16.5 g/dL` followed on the next line by `16.0` | `16.0` |
| `Troponin Ref Range: <0.04 ng/mL; 0.20` | `0.20` |
| `Potassium L 2.9 mmol/L` | `2.9`, status `low` |

The number, unit, comparator and high/low/normal status remain separate fields
under the existing schema. Qualifiers in `numeric_value` retain the previous
representation, for example `<0.79`. The rule does not invent a result from a
reference range alone. Unsupported or ambiguous layouts, conflicting flags,
conflicting units and incomplete matches retain the existing parser. This is
not a general table parser and does not establish correctness for every lab
layout. The fallback's prior handling of goals and pending results is unchanged.

## Integration

Use the same [Harvard default command](harvard_default_handoff.md); the serial
linker, prefork linker and measurement overlay supply original note offsets
automatically. The public record schema and deployment dependencies are unchanged.

For integrations calling `parse_lab_value` directly, the optional keyword
arguments `note_text`, `note_start` and `note_end` enable original-layout matching.
They use Unicode character positions, end-exclusive, in the unmodified note.
The positional `context`, `start` and `end` arguments still describe the cleaned
local context. Do not interchange the two coordinate systems. Calls without
the optional original-note arguments retain the contextual parser.

Precompiled patterns and bounded lab-alias template caches reuse static work.
No clinical predictions are cached. Rules remain CPU-only.

## Validation

Reference: accepted CPU batch implementation `ba0264b`, not the older
collaborator package. Both versions use identical model assets, filters,
7,000-character linker cap and seeds.

| Existing regression set | Targets | Numeric accuracy, before / after | Bundle accuracy, before / after |
|---|---:|---:|---:|
| MIMIC-style synthetic, 240 notes | 780 | 100.00% / 100.00% | 84.62% / 84.62% |
| Adversarial synthetic, 360 notes | 285 | 87.37% / 87.37% | 40.35% / 40.35% |
| Separate GPT synthetic set, 947 notes | 5,006 | 97.72% / 97.72% | 86.90% / 86.90% |

This table supplies gold mention positions and concept names; it is a parser
regression check, not end-to-end F1. Bundle includes category (given), numeric
value, unit, value status and measurement context. No clinical field regressions
occurred among the 6,071 targets. There were no numeric gains on these sets.

Full extraction reruns on the 240/360-note sets retained entity exact F1
(.9143/.7134), matched assertion macro-F1 (.9437/.8486), and matched value bundle
accuracy (.8462/.4378). Full-record comparison also passed on 100 and 1,000
natural MIMIC notes; natural notes lack comprehensive gold annotations, so this
is output preservation rather than an F1 estimate.

Ten authored layout examples passed both parser and complete-entrypoint checks:
numeric 3/10 to 10/10, unit 7/10 to 10/10, status 3/10 to 10/10. Ten confusing
controls retained the reference clinical fields. These deliberately constructed
cases demonstrate the implementation, not independent clinical generalization.
The 18-method regression suite also covers CPU execution and offset safety.

## Incremental Runtime

Times include fresh model loading and all five extraction stages; evaluation
and output comparison are excluded. The first three rows use two reversed-order
pairs on the same node; 1K and 10K each have one pair. OS caches are not
flushed.

| Notes | CPUs | Accepted CPU base | Numeric candidate |
|---|---:|---:|---:|
| Natural MIMIC, 100 | 1 | 137.27 s | 134.85 s |
| MIMIC-style synthetic, 240 | 4 | 59.36 s | 58.77 s |
| Adversarial synthetic, 360 | 4 | 58.99 s | 58.24 s |
| Natural MIMIC, 1,000 | 4 | 379.78 s | 386.59 s |
| Natural MIMIC, 10,000 | 4 | 3369.00 s | 3439.77 s |

The patch is a layout-coverage improvement, not a demonstrated general speedup.
Earlier CPU optimizations are documented separately in
[CPU efficiency](cpu_efficiency.md). Harvard's private 1,177-target result was
reported for a different frozen numeric wrapper and cannot be independently
reproduced without those notes. It is not claimed as this release's accuracy.

The completed 10K paired run used 4 CPUs and 192 GiB on both sides. The full
pipeline took 3369.00 seconds for the accepted base and 3439.77 seconds for the
candidate, a 2.10% candidate overhead. Slurm recorded a peak RSS of
152,122,572 KiB (about 145 GiB) for the job. This is an observed allocation
requirement for this dense 10K run, not a universal minimum; smaller or less
dense batches may use less memory. A 40 GiB run failed during the unchanged
baseline measurement stage. The 10K comparison passed all structural checks:
3,141,261 final rows were aligned, with six numeric-only changes and no entity,
assertion, or nonnumeric-field changes.
