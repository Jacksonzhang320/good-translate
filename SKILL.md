---
name: good-translate
description: 高保真翻译 PDF 图书与学术论文（Reliably translate PDF books and papers）。保留原版式、双栏几何排版、完整图片、图注、表格与独立公式。默认一键输出三种标准格式：期刊原版、双语对照版和党政机关公文版（GB/T 9704—2012 / GB/T 7714—2015/2025）。具备跨栏跨页断句自动缝合、8子代理并行隔离翻译、术语反馈闭环防死锁收敛、学术文献元数据主动检索门禁。触发词：good translate、translate、翻译、论文翻译、PDF翻译、图书翻译、书籍翻译、双语翻译、公文版翻译。
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent, AskUserQuestion
metadata: {"openclaw":{"requires":{"bins":["uv"]},"homepage":"https://github.com/Jacksonzhang320/good-translate"}}
---

# Reliable book and paper translation

Treat a translation as complete only when parsing, every translated block,
terminology feedback, browser rendering, and visual review have all passed.
The word `generated` is not acceptance.

## Defaults

- Target: Chinese (`zh-CN`) unless the user names another language.
- Deliver: translated `book.html` + `book.pdf` and interleaved bilingual
  `book_bilingual.html` + `book_bilingual.pdf`.
- Layout: preserve measured page size, margins, body typography, heading scale
  and color, source column count, full figures, captions, tables, and formulas.
  Repagination is allowed. The bilingual edition uses one column for legibility.
- Formula policy: keep inline math byte-identical. Keep independent formulas as
  source-PDF crops when parse fidelity is uncertain. Never output glyph soup.
- Translation voice: use the user's per-book instructions. Otherwise use
  faithful, fluent prose with consistent terminology.
- Academic metadata gate: When translating academic papers, never accept blind
  code fallbacks (such as generic `学术期刊译情参阅 编号：<hash>`). If the parser
  does not definitively extract the official Journal Name, DOI, and Publication
  Year, the Agent MUST actively search (via web search / academic lookup) using
  the paper's Title and Authors, retrieve the official publication venue, map
  the journal to its standard Chinese formal translation (e.g., `自然·神经科学 参阅文件`,
  `细胞 参阅文件`, `科学 参阅文件`), format the issue number (`DOI〔{Year}〕{short_doi} 号`),
  and inject them into `<run>/style.json` (`gov_header`) or pass them during `build`.
- Work directory: create a new short ASCII run slug. Never reuse a directory
  belonging to different source bytes, instructions, language, or parser data.

Read [workflow.md](references/workflow.md) before the first PDF run and
[qa.md](references/qa.md) before accepting parsing or publication.

## Environment

Run project Python through the locked project:

```text
uv run --project "{baseDir}" python "{baseDir}/scripts/<script>.py" ...
```

Do not use bare `python` or `pip`. Sync `uv.lock` when the environment is
missing. OCR uses the `ocr` extra plus system Tesseract language packs and
Ghostscript. MinerU may use its own registered Python environment.

## Reliable PDF route

### 1. Preflight and route

Run:

```text
uv run --project "{baseDir}" python "{baseDir}/scripts/pipeline.py" inspect "<pdf>"
```

Honor `recommended_route`:

- `geometry`: clean single-column digital vector PDFs. Prepared directly in ~1 second with column-aware geometry flow (zero CUDA/GPU or neural net dependencies).
- `mineru`: complex dual-column layout, dense scientific figures/tables, or formula evidence; parse with MinerU, then prepare with `--mineru-dir`. MinerU provides visual neural layout detection, preventing running header / DOI / title concatenation and TOC box pollution.
- `ocr+mineru`: scan-like PDFs (no readable text layer); OCR first, run MinerU on the OCR PDF, then prepare using both `--ocr-pdf` and `--mineru-dir`. Crops still come from the untouched source.

OCR and MinerU exceed one minute when needed. Run them through the available
long-task runner with independent run directories, logs, progress, heartbeat,
checkpoint, exit record, final summary, and resume command. On Windows use
`scripts/start_task.ps1` and the bundled compatible runner evidence at
`references/runner-smoke-pass.json`. Before the first real OCR or MinerU job,
run a minimal representative scientific smoke and reference its compatible
`smoke_pass.json` from the full job spec. Never bypass a failed or rejected
smoke. See [workflow.md](references/workflow.md).

### 2. Build the immutable source contract (1-second native geometry)

Run `pipeline.py prepare` with the selected parser outputs, exact target
language, and exact custom instructions. It writes `doc.json`, `style.json`,
source-object crops, ID-marked chunks, a complete v2 manifest, glossary, and
run state. Paragraphs split across page breaks, column boundaries, or figure
interruptions are automatically rejoined before translation to preserve full
sentence semantics. Dual-column pages are automatically sorted in natural reading
order with overlapping figure elements merged into unified crops.

Generate source-page and object contact sheets with `qa_preview.py`. Inspect all
figure, formula, and table crops and representative source pages.

**TOC Outline Review**: Run `pipeline.py outline <run>` to review the extracted
heading structure. Verify that running headers (e.g., *Perspective*, *Review*, *述评*)
are not misclassified as section titles. Accept only a complete parse:

```text
uv run --project "{baseDir}" python "{baseDir}/scripts/pipeline.py" accept-parse "<run>" --reviewer "<name>" --evidence "<concrete pages, objects and outline verified>"
```

### 2.5 Agent-planned, code-executed chunking & Phase 0 Anchor Glossary
- **Agent-Planned Chunking (大纲决策，代码切割)**:
  Instead of mechanical character-count slicing that fragments sections or citations, Planning Agent inspects the outline tree and decides logical chapter cut points:
  1. Inspect outline landmarks:
     ```text
     uv run --project "{baseDir}" python "{baseDir}/scripts/pipeline.py" plan-split "<run>" --target-chunk-size 12000
     ```
     This outputs a compact table with Block IDs, character offsets, headings, and algorithm-suggested cut points.
  2. Agent spends 3 seconds reviewing the outline (e.g. ensuring headings like *Introduction*, *Results*, *Box 1*, *Discussion*, and *References* stay intact).
  3. Execute deterministic cut in 0.01s:
     ```text
     uv run --project "{baseDir}" python "{baseDir}/scripts/pipeline.py" split "<run>" --cuts b000069 b000199 b000285 b000404
     ```
     (If `--cuts` is omitted, the recommended section-aware cut points are applied automatically).
  4. Keeps discourse coherent, guarantees clean references isolation, and results in only **6~9 large chunks**.
- **Phase 0 前置锚定 (Anchor Glossary Pre-mining)**:
  Before dispatching translation, automatically seed high-frequency domain acronyms and compound terms into `glossary.json`:
  ```text
  uv run --project "{baseDir}" python "{baseDir}/scripts/pipeline.py" seed-glossary "<run>" --top-n 30
  ```
  This extracts key domain terms (e.g. `SAR`, `PK/PD`, `HTS`, `ADMET`, `hit-to-lead`). The Agent quickly verifies or completes standard Chinese targets in `glossary.json` (30-second review) so all subagents start with an authoritative baseline, eliminating 90% of initial term drift.

### 2.6 Academic metadata and citation retrieval gate

Never rely on passive code regexes or silent fallbacks for academic papers. If a paper's PDF text lacks explicit journal or DOI metadata, or if `_detect_gov_header` would produce the fallback placeholder `学术期刊译情参阅 编号：<hex>`:

1. **Active Search & Verification**:
   The Agent **MUST** use search tools (`search_web`, Crossref, PubMed, or academic search) with the exact paper title and first author to retrieve:
   - **Authoritative Publication Venue**: The official peer-reviewed journal where the paper appeared (e.g., *Nature Neuroscience*, *Neuron*, *Cell*, *Science*, *PNAS*, *Nature Communications*, etc.). If exclusively a preprint, identify the repository (e.g., *bioRxiv* / *arXiv*).
   - **Standard Chinese Translation**: Map the journal name to its standard Chinese formal translation (e.g., `自然·神经科学`, `细胞`, `科学`, `神经元`, `美国科学院院报`；or keep the authoritative Latin/English name if untranslated).
   - **Canonical DOI & Publication Year**: Retrieve the official registered DOI (e.g., `10.1038/s41593-026-02314-z`) and publication year (e.g., `2026`).

2. **Configuration Injection**:
   Inject the verified metadata before building:
   - Option A: Write directly into `<run>/style.json` under `gov_header`:
     ```json
     "gov_header": {
       "org_name": "自然·神经科学 参阅文件",
       "doc_number": "DOI〔2026〕s41593-026-02314-z 号"
     }
     ```
   - Option B: Pass via `pipeline.py build`:
     ```text
     uv run --project "{baseDir}" python "{baseDir}/scripts/pipeline.py" build "<run>" --journal "自然·神经科学" --doi "10.1038/s41593-026-02314-z"
     ```
     or `--org-name "自然·神经科学 参阅文件" --doc-number "DOI〔2026〕s41593-026-02314-z 号"`.

Never proceed with unverified generic placeholders (`学术期刊译情参阅` or random hex hashes) for published scientific literature.

### 3. Translate in resumable batches

#### 3.1 Academic paper references bypass (免译直通)
When translating academic papers with Western references (author names, journal titles, DOIs), run:
```text
uv run --project "{baseDir}" python "{baseDir}/scripts/pipeline.py" bypass-refs "<run>"
```
This automatically identifies pure citation chunks, writes verbatim output files, and marks them verified. This saves thousands of LLM tokens, prevents citation hallucination, and guarantees 100% citation accuracy without LLM intervention.
*Strict Boundary Gate*: Only pure reference bibliography entries are bypassed. End-matter prose (Acknowledgements, Competing Interests, Supplementary Information, Author Contributions, and Institutional Affiliations) are strictly guarded and dispatched for LLM translation.

#### 3.2 Phase 1: Flat 1:1 Parallel Dispatch (扁平化全并发调度)
Use `pipeline.py status` and `run_state.py plan`; dispatch remaining prose chunks.
- **Strict 1:1 Mapping**: Enforce **1 Subagent = 1 Chunk**. Never assign multiple chunks to a single subagent in a loop.
- **Concurrent Launch**: With 12,000-character chunks, a full paper only produces 6~8 prose chunks. Launch all 6~8 subagents simultaneously in a single `invoke_subagent` call:
  ```text
  uv run --project "{baseDir}" python "{baseDir}/scripts/pipeline.py" dispatch "<run>" chunk0001 chunk0002 ... chunk0008
  ```
- **Total translation time drops to the single-chunk latency (90~120 seconds)**.
- **Prompt Term Contract**: Each packet injects the Phase 0 baseline glossary. Tables and inline math support standard formatting tags (`<sup>`, `<sub>`, `<em>`, `<strong>`) within cells without triggering structural validation errors. Call `pipeline.py record` for each chunk as it completes.

### 4. Phase 2: Term conflict resolution & surgical patching (定点外科手术替换)

After each batch, use `merge_meta.py prepare-merge`, resolve proposed decisions, and use `merge_meta.py apply-merge`.

**Surgical Term Patching instead of Retranslation**:
When aliases are established or terms unified, do NOT retranslate entire chunks. Instead, run:
```text
uv run --project "{baseDir}" python "{baseDir}/scripts/pipeline.py" patch-terms "<run>"
```
This performs byte-exact regex/AST replacement across all `output_chunk*.md` files in milliseconds:
- Replaces deprecated variant translations with canonical targets;
- Strictly preserves Markdown comments (`<!-- tb:b... -->`), images, LaTeX equations (`$...$`, `$$...$$`), and HTML structures;
- Takes 0.05 seconds, costs 0 LLM tokens, and carries 0 regression risk;
- Full-chunk retranslation cascade is disabled by default (`TB_RETRANSLATE_ON_TERM_DRIFT=0`).

Repeat until the translation queue is empty and every current meta hash is merged. Then run `pipeline.py freeze`.

### 5. Publish and visually accept

Run:

```text
uv run --project "{baseDir}" python "{baseDir}/scripts/pipeline.py" build "<run>"
```

Default builds automatically generate all three editions with standardized semantic filenames (`{Title}_{Edition}`):
- `mono` (`{Title}_期刊原版.pdf` / `.html`): High-fidelity layout matching source journal/book geometry (e.g., 2-column or 1-column).
- `bilingual` (`{Title}_双语对照.pdf` / `.html`): Interleaved bilingual edition with aligned source and target blocks.
- `gov_doc` (`{Title}_公文版.pdf` / `.html`): Chinese Government Official Document mode (**党政机关公文格式**), conforming to national standard **GB/T 9704—2012**, **GB/T 7714—2015/2025**, and **GB/T 7713**:
  - Standard A4 page margins (Top 37mm, Right 26mm, Bottom 35mm, Left 28mm).
  - Body text: 3号仿宋 (16pt), line height 28.5pt (22 lines per page / 28 chars per line standard), 2em paragraph indentation.
  - Red header: Issuing authority in 2号/28pt 小标宋 (`#e60012`), issue number in 3号仿宋 (auto-inferred from DOI/metadata or configured via `gov_header` in `style.json`), and 156mm red divider.
  - Standard heading normalization: Title in 2号小标宋 (22pt), 一级标题 in 3号黑体 (`一、`), 二级标题 in 3号楷体 (`（一）`), 三级标题 in 3号仿宋加粗 (`1.`), 四级标题 in 3号仿宋 (`（1）`).
  - Figure and caption standardization: Figure title in 11pt/小4号黑体 centered (without trailing period/pipe), figure explanation in 10pt/5号仿宋 justified with 2em indent, no caption page breaks.
  - References (参考文献) standardization (**GB/T 7714**): Centered unnumbered bold title (`.gov-ref-title`), 5号字 (10pt Times New Roman + 仿宋), 1.4 compact line height, standard Hanging Indent (`padding-left: 2.8em; text-indent: -2.8em;`) with square bracket numbering (`<span class="gov-ref-num">[1] </span>`), eliminating margin overflow for numbers up to `[999]`.
  - Appendices (附录/附件) standardization (**GB/T 9704 / GB/T 7713**): Methods and reporting materials cleanly delineated as centered `附录：研究方法` (`.gov-appendix-title`), resetting internal sub-item numbering.
  - Page footer: 4号半角宋体 with em-dash (`— 2 —`), centered at bottom. First page suppresses footer per GB/T 9704.
  - Tables (**GB/T 7713 / GB/T 9704**): Standard 3-line tables (三线表: 1.5pt top/bottom border, 1.0pt header bottom border, no internal cell borders). Table captions automatically hoisted above tables (`caption-side: top`). Wide tables (>= 4 columns) automatically adopt dense 9.5pt font size (`.dense-table`), eliminating single-character vertical stacking.
  - Content Abstract & Callout Boxes: Abstract formatted as bold indented `【内容摘要】` (16pt 楷体); Callout boxes (`Box 1/2/3`) rendered as independent containers (`.gov-callout`), preventing disruption of the formal document section numbering.

Options:
- `--file-stem <stem>`: Custom base filename stem (defaults to cleaned title/first H1 heading).
- `--mono-preset gov_doc`: Renders the monolingual edition directly according to GB/T 9704-2012.
- `--editions <editions>`: Override edition selection (defaults to `mono,bilingual,gov_doc`).
- `--journal <name>`: Journal name (auto-infers `{name} 参阅文件` red header).
- `--doi <doi>`: Official publication DOI (auto-infers `DOI〔{Year}〕{short_doi} 号`).
- `--org-name <header>`: Explicit issuing authority red header text.
- `--doc-number <number>`: Explicit document issue number.
- `--legacy-aliases`: Generate backward-compatible `book.html`/`book.pdf` alias copies (default: false, strictly avoiding redundant duplicate files).

The versioned publisher validates exact document coverage, assets, formulas,
fonts, MathJax readiness, overflow, image decoding, and PDF creation. It writes
`build_result.json` with status `generated`. By default, only the canonical
semantic files (`{Title}_{Edition}`) are produced, eliminating redundant double
copies. When delivering or presenting outputs to the user, deliver only the
canonical edition files and required `assets/`.

### 5.5 Agent Layout & Typography Audit Gate (排版终审与版式质检门禁)

Never rely solely on automated script exit codes or DOM counts (`status: "passed"`). Automated scripts only verify tag closure and block existence; they cannot detect visual layout degradation, misplaced running headers, or broken list styling.

The Agent **MUST** actively inspect the publication before calling `accept-publish`:

1. **First-line Mechanical Linting**:
   Run the layout audit helper:
   ```text
   uv run --project "{baseDir}" python "{baseDir}/scripts/pipeline.py" audit-layout "<run>"
   ```
   This automatically catches:
   - Spurious running headers and leaked URLs/DOIs in headings (`heading_contains_url_or_doi`);
   - Isolated H2 headings before H1, or excessive subheading collapsing (`excessive_subheadings_under_section`);
   - Broken reference numbering sequences, reference downgrade into `<ol>`, and non-compliant dot numbering (`ref_format_not_gbt7714`);
   - Table captions positioned below tables (`table_caption_below_table`) and wide tables lacking dense styling;
   - Leaked draft placeholders (`draft_placeholder_leaked`) and untranslated English body prose (`untranslated_body_text`);
   - Missing gov_doc metadata and Phase 3 term alias leakage (`term_drift_alias_leak`). If any term alias leakage is reported, run `pipeline.py patch-terms <run>` to eliminate it immediately before acceptance. Fix any `[ERROR]` before proceeding.

2. **Reference Integrity & GB/T 7714 Styling Review**:
   - **No Premature Break**: Confirm the reference list is not cut short or broken into by OCR page-top headers (e.g. *Perspective*, *Review*, *述评*, *综述*).
   - **Monotonic Continuity**: Verify references run strictly 1 to N without missing numbers, duplicates, or reversals.
   - **Consistent Typography**: In `gov_doc`, verify all references are formatted with standard GB/T 7714 hanging indent (`.gov-ref-item`) in compact 10pt font, never falling back to generic 16pt `<ol><li>` body text.

3. **Heading Hierarchy & Spurious Header Review**:
   - Inspect document outline: ensure no OCR running headers or column tags (e.g., `Perspective`, `Review`, `Article`, `Analysis`, `Commentary`, `述评`, `综述`, `快讯`) became visible `<h1>` or `<h2>` headings.
   - For `gov_doc`, ensure heading numbering adheres strictly to standard official hierarchy (`一、` -> `（一）` -> `1.` -> `（1）`).

4. **Visual Contact Sheet Inspection via `view_file`**:
   - Generate preview sheets:
     ```text
     uv run --project "{baseDir}" python "{baseDir}/scripts/qa_preview.py" pdf "<run>/publish/<hash>/<edition>.pdf" --out "<run>/qa/previews" --label "<edition>"
     ```
   - The Agent MUST call `view_file` to visually inspect contact sheets of critical layout transitions:
     - The first title page (red header, title, authors);
     - The first figure / table page (image placement, caption alignment);
     - The reference list transition page and final reference page (hanging indent consistency, no stray headings);
     - The end-matter pages (acknowledgments, competing interests, appendices).

5. **Official Document Metadata Verification**:
   - Verify the red issuing authority displays the verified journal name (e.g., `自然·神经科学 参阅文件`).
   - Verify the issue number displays the official DOI (e.g., `DOI〔2026〕s41593-026-02314-z 号`).
   - **STRICT REJECTION**: Reject any publication displaying generic placeholders (`学术期刊译情参阅` or random hex hashes `编号：...`) when translating published papers.

6. **Acceptance Evidence Commitment**:
   Only after completing the audit, call `pipeline.py accept-publish` with concrete evidence (pages checked, reference range, absence of false headings). Report the run complete only when `pipeline.py status` says `accepted`. Return the exact paths and distinguish monolingual from bilingual and official document files.

Run `pipeline.py cleanup` only after acceptance and only when cleanup is wanted.
It preserves source contracts, chunks, outputs, glossary, state, assets, QA,
logs, checkpoints, and failure evidence.

## DOCX and EPUB input compatibility

For DOCX/EPUB source files, use `scripts/convert.py`, the same chunk translation
and glossary discipline, then `scripts/merge_and_build.py`. This compatibility
route preserves document structure and images but does not promise source-page
style fidelity. If a user needs the new PDF layout contract, convert the source
to PDF first or explain this limitation. PDF-source runs may request DOCX/EPUB
as additional publisher formats; formulas use explicit source-image fallback.

## Completion report

Include source hash, parser route, target language and custom style, translated
and unchanged chunk counts, remaining blockers, parse/publish acceptance
evidence, final artifact paths, and whether cleanup ran. If any stage is
`running`, `generated`, `review_pending`, `failed`, or `resumable`, state that
the requested translation is not complete.
