# Parse and page-style QA

## Parse acceptance

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
- links and images load from portable relative paths.

Record exact sheets/pages/objects inspected. Vague evidence such as “looks
good” is insufficient. Do not mark acceptance from DOM checks alone.
