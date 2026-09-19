# Parse and page-style QA

## Parse acceptance

### 1. Document Outline Sanity Review (目录大纲结构审查)
Run:
```text
pipeline.py outline <run>
```
The Agent MUST inspect the generated document outline tree:
- **Spurious Running Headers**: Check for running headers or journal section names (e.g. `Perspective`, `Review`, `Article`, `Table of Contents`) promoted to H1/H2 headings. If any `[WARN: 疑似页眉]` appears, inspect the block and downgrade or remove it before chunking.
- **Heading Continuity**: Verify major sections (Introduction, Results, Discussion, Methods, References) form a logical sequence.
- **Reference Section Isolation**: Ensure References begins after the core body text and is cleanly isolated.

### 2. Crop and Reading Order Review
Create object sheets with:

```text
qa_preview.py objects <run>
```

Create source-page sheets with:

```text
qa_preview.py pdf <source.pdf> --out <run>/qa/previews --label source
```

Inspect every figure, formula, and table crop. Confirm the crop includes all
panels, axes, labels, equation numbers, and boundaries without borrowing the
next paragraph. Confirm every caption exists as its own block and is linked to
the correct object. Review reading order on the title page, the first two body
pages, every layout transition, and every page reported by a parser warning.
Do not accept any blocking issue or page fallback.

## Publication acceptance

Create sheets for both output PDFs with `qa_preview.py pdf`. Review all pages
for a short document. For a long book, review at least the first and last page,
every heading level and layout transition, every page containing a figure,
table, or independent formula, and an evenly spaced sample of remaining pages.

Check:

- page size, margins, font feel, heading scale/color, and source column count;
- bilingual source/translation adjacency and readable one-column flow;
- no clipped text, headings split at page tops, blank overflow pages, or
  horizontal overflow;
- each full figure appears once, with its caption;
- every formula is legible and retains its number;
- tables fit and remain associated with captions/footnotes;
- links and images load from portable relative paths;
- Official Document edition (`gov_doc`) red header:
  - Header issuing authority (`.gov-header-org` / `.gov-org-name`) MUST reflect the verified journal (e.g., `自然·神经科学 参阅文件`), NEVER the unverified fallback `学术期刊译情参阅`;
  - Header issue number (`.gov-header-docno` / `.gov-doc-number`) MUST reflect the verified official DOI and publication year (e.g., `DOI〔2026〕s41593-026-02314-z 号`), NEVER a generic hex hash like `编号：39e516e484da`.

## Agent Layout & Typography Audit Checklist (排版终审清单)

Automated script checks (`status: "passed"`) only verify DOM block counts and MathJax completion. They do NOT detect semantic layout decay. The Agent **MUST** complete this visual and structural audit before accepting any publication:

### 1. Mechanical Linting Pre-check
Run:
```text
pipeline.py audit-layout <run>
```
Verify that all HTML editions report `status: passed` with 0 critical errors. If any spurious running header or reference numbering anomaly is flagged, diagnose and fix the source chunks or publisher templates immediately.

### 2. Spurious Running Header & Outline Inspection
- **Scan Headings**: Review all generated H1/H2/H3 headings across the document.
- **Header Keywords**: Ensure no running headers or journal section labels (e.g. `Perspective`, `Review`, `Article`, `Analysis`, `Commentary`, `述评`, `综述`, `快讯`) were misclassified by OCR as major section titles.
- **Official Doc Hierarchy**: In `gov_doc`, confirm that section numbering strictly follows GB/T 9704 hierarchy (`一、` -> `（一）` -> `1.` -> `（1）`) without skipped numbers or out-of-order levels.

### 3. Reference Consistency & GB/T 7714 Standards
- **Integrity**: Confirm that the reference section starts cleanly after the main body and is NOT prematurely broken by running headers or stray headings.
- **Monotonic Sequence**: Verify references strictly follow numerical sequence $1, 2, \dots, N$. Check that multi-column OCR has not reversed, duplicated, or dropped citations.
- **Typography**: In `gov_doc`, ensure every single citation uses standard GB/T 7714 compact hanging indent (`.gov-ref-item` in 10pt font). Reject any publication where citations degrade into generic 16pt body-text ordered lists (`<ol><li>`).

### 4. Terminology Consistency (零术语漂移终审)
- **Check Linter Warnings**: Review `pipeline.py audit-layout` output for any `term_drift_alias_leak` warnings.
- **Surgical Patching**: If any deprecated variant translation or alias is reported as leaked, immediately run `pipeline.py patch-terms <run>` to achieve 100% zero drift across all output files before proceeding.
- **Uniform Authority**: Confirm core domain acronyms and proper nouns maintain complete consistency across all sections.

### 5. End-matter & Appendices Delineation
- **Section Order**: Ensure end-matter sections (致谢 Acknowledgements, 利益冲突 Competing Interests, 补充信息 Supplementary Information, 相关链接 Related Links, 作者单位 Author Affiliations) appear after references and are cleanly demarcated without bleeding into citations.
- **Appendix Formatting**: Appendices must be centered (`.gov-appendix-title`) with independent sub-numbering.

### 6. Visual Sheet Inspection via `view_file`
The Agent MUST use `view_file` to visually review contact sheets generated by `qa_preview.py`:
- Title page (sheet 1);
- First figure / table page;
- Reference list entry page and final reference page;
- End-matter / affiliation pages.

Record exact sheets/pages/objects inspected in the `--evidence` parameter of `accept-publish`. Vague evidence such as “looks good” is strictly insufficient. Do not mark acceptance from DOM checks alone.
