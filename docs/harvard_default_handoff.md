# Harvard Default Handoff

This is the fixed extraction release for the second-round Harvard evaluation.
There is one entry point and one run; no extra tuning is required.

## Run Once

Clone the repository default branch, install the dependencies, and stage the licensed
MedCAT assets outside the repository:

```bash
git clone https://github.com/LabRAI/EHR-CUI-Assertion-Extraction.git
cd EHR-CUI-Assertion-Extraction
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The release was regression-tested with Python 3.10.8 and the dependency
versions pinned in `requirements.txt`.

Open `slurm/templates/run_harvard_default.sbatch` and fill only:

```text
NOTES_JSONL_GZ
MODELPACK
CDB_PATH
```

Then submit once from the repository directory:

```bash
sbatch slurm/templates/run_harvard_default.sbatch
```

The template uses 4 CPU cores, 68 GB RAM, and automatic CPU worker detection.
The output directory defaults to `./harvard_default_output`; set
`OUTPUT_ROOT=/private/output/path` at submission time if desired.

This is a single default run. Do not run an older tag or apply a separate
numeric patch. The current default branch includes the bounded numeric-layout
parser and falls back to the prior parser for unsupported or ambiguous layouts.
The 68 GB template is the validated collaborator setting for the expected
Harvard batch; a dense 10K-note MIMIC stress test used about 145 GiB peak RSS.

## Fixed Presettings

The wrapper fixes the validated production choices:

- packaged schema-aware linker filter policy;
- all non-rejected candidates for downstream stages;
- batched MedCAT inference with a 1,000,000-character batch budget;
- 7,000-character maximum note length;
- ontology overlap mode `add_missing`;
- aggressive final refinement;
- compressed streaming output;
- no LLM and no GPU.

No Python source file needs to be edited. The wrapper rejects missing paths and
refuses restricted-data execution outside SLURM.

## Validated Reference

The tagged release was rerun end to end on the frozen 240-note MIMIC-style
regression set using 4 CPU cores. Its 5,405 final rows matched the validated
reference row-for-row after canonicalization. The reproduced metrics were:

```text
Entity exact F1       0.9143
Entity relaxed F1     0.9266
Assertion macro-F1    0.9437
Value bundle          0.8462
Numeric value         1.0000
```

## Outputs

The final records are written to:

```text
<OUTPUT_ROOT>/variant_harvard_default/05_final/medcat_candidates.jsonl.gz
```

The main run manifest is:

```text
<OUTPUT_ROOT>/variant_harvard_default/final_pipeline_manifest.json
```

Share only aggregate manifests and summaries for troubleshooting. Keep notes,
candidate files, modelpacks, CDB files, and evidence windows private.

For handoff questions, report the repository commit from `git rev-parse HEAD`,
the Slurm job ID, and the aggregate manifest. Do not send note text or
candidate output files.
