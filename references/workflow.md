# PDF workflow details

## Run directories

Use a short ASCII slug such as `runs/book-20260915-01`. Keep smoke output in a
separate sibling. Never overwrite another source or render version. The
publisher uses `publish/<render-hash-prefix>/` so changed style, metadata,
content, renderer, MathJax, or asset bytes cannot silently reuse stale output.

## External jobs

`scripts/job.py` accepts a JSON spec with an argv array, working directory,
environment object, label, and required outputs. It does not accept shell code.
For Windows, start a job with `scripts/start_task.ps1`; it validates
`references/runner-smoke-pass.json` against the current runner before invoking
the global long-task runner.

An OCR spec calls `scripts/ocr_pdf.py <source> <ocr-output> --langs
eng+chi_sim`. Add the OCR PDF to `required_outputs`.

A MinerU spec calls the registered MinerU Python with `-m mineru.cli.client -p
<input> -o <output> -b pipeline`; include `MINERU_MODEL_SOURCE=modelscope` only
when that is the configured model source. Use a short input filename on Windows.
The scientific smoke should use one or two representative pages with the same
backend and method. Its `smoke_pass.json` records the normalized method
fingerprint while allowing page range, output directory, and run ID to vary.

Full OCR/MinerU specs set `scientific_smoke_required: true`,
`scientific_smoke_pass`, and `scientific_fingerprint`. `job.py` refuses a
missing, failed, or incompatible record and copies both smoke references into
`run_info.json`.

## Prepare examples

Simple text PDF:

```text
pipeline.py prepare book.pdf --temp-dir runs/book-01 --lang zh-CN --instructions-file style.txt
```

Two-column or formula PDF:

```text
pipeline.py prepare paper.pdf --temp-dir runs/paper-01 --lang zh-CN --instructions-file style.txt --mineru-dir runs/paper-mineru/output
```

Scanned PDF:

```text
pipeline.py prepare scan.pdf --temp-dir runs/scan-01 --lang zh-CN --instructions-file style.txt --ocr-pdf runs/scan-ocr/ocr.pdf --mineru-dir runs/scan-mineru/output
```

`prepare` may leave `doc.json`, assets, and `pipeline_state.json` when it finds
blocking issues. These are failure evidence. Fix the route and use a new run
directory; do not delete or disguise them.

## Batch loop

1. `pipeline.py status <run>`
2. `run_state.py plan <run>`
3. `pipeline.py dispatch <run> <listed chunk IDs>`
4. Translate each packet with a fresh subagent and write both requested files.
5. `pipeline.py record <run> <completed IDs>`
6. `merge_meta.py prepare-merge <run>`
7. Resolve all evidence-backed decisions and apply them with
   `merge_meta.py apply-merge <run>`.
8. Return to step 1 until no corrections remain.
9. `pipeline.py freeze <run>` and `run_state.py verify <run>`.

A failed subagent attempt keeps its dispatch snapshot. Repair the output and
record it, or explicitly cancel that dispatch before starting another attempt.
Never infer or backfill a dispatch history after the fact.

## Publication

The default build produces both HTML/PDF editions. Use `--formats
html,pdf,docx,epub` only when additional formats are requested. HTML files are
bundles: keep their sibling `assets/` directory.

Generation is mechanical. `accept-publish` hashes every artifact again and
requires a reviewer plus concrete visual evidence. `pipeline.py status` is the
canonical completion check.
