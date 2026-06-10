# Data And License Boundary

This repository is designed to be code-only.

Do not commit:

- MIMIC clinical notes or derived note text;
- UMLS, SNOMED CT, RxNorm, or other licensed terminology releases;
- MedCAT modelpacks;
- generated extraction outputs, logs, audit files, or benchmark files that may
  contain note snippets;
- local SLURM account names, private storage paths, tokens, API keys, or email
  content.

Recommended local layout outside git:

```text
<PROJECT_DIR>/
  data/restricted/        # MIMIC archive and private note shards
  models/medcat/          # licensed MedCAT modelpacks
  umls/                   # licensed UMLS release
  outputs/extraction/     # candidate records and summaries
  outputs/pair_splits/    # strict CUI-assertion pair splits
  logs/                   # SLURM logs
```

For cluster runs:

- run restricted processing through SLURM, not on login nodes;
- keep output directories private, typically mode `700`;
- keep output files private, typically mode `600`;
- write bounded context snippets only when needed for audit or debugging;
- never write full notes into reports, README files, issue text, or chat.

