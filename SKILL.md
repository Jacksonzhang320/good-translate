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

- `geometry`: prepare directly.
- `mineru`: parse with MinerU, then prepare with `--mineru-dir`.
- `ocr+mineru`: OCR first, run MinerU on the OCR PDF, then prepare using both
  `--ocr-pdf` and `--mineru-dir`. Crops still come from the untouched source.

OCR and MinerU normally exceed one minute. Run them through the available
long-task runner with independent run directories, logs, progress, heartbeat,
checkpoint, exit record, final summary, and resume command. On Windows use
`scripts/start_task.ps1` and the bundled compatible runner evidence at
`references/runner-smoke-pass.json`. Before the first real OCR or MinerU job,
run a minimal representative scientific smoke and reference its compatible
`smoke_pass.json` from the full job spec. Never bypass a failed or rejected
smoke. See [workflow.md](references/workflow.md).

### 2. Build the immutable source contract

Run `pipeline.py prepare` with the selected parser outputs, exact target
language, and exact custom instructions. It writes `doc.json`, `style.json`,
source-object crops, ID-marked chunks, a complete v2 manifest, glossary, and
run state. Paragraphs split across page breaks, column boundaries, or figure
interruptions are automatically rejoined before translation to preserve full
sentence semantics. Blocking coverage, reading-order, OCR, formula-boundary,
or missing object issues stop the run.

Generate source-page and object contact sheets with `qa_preview.py`. Inspect all
figure, formula, and table crops and representative source pages. Accept only a
complete parse:

```text
uv run --project "{baseDir}" python "{baseDir}/scripts/pipeline.py" accept-parse "<run>" --reviewer "<name>" --evidence "<concrete pages and objects checked>"
```

### 2.5 Academic metadata and citation retrieval gate

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

Use `pipeline.py status` and `run_state.py plan`; dispatch only the listed
chunks. Start with a small batch, then use the runtime's supported parallel
subagents. Give each subagent exactly one JSON packet created by:

```text
uv run --project "{baseDir}" python "{baseDir}/scripts/pipeline.py" dispatch "<run>" chunk0001 ...
```

The packet points to the source, output, meta file, frozen dispatch snapshot,
terminology table, neighbor context, custom style contract, and
`references/translate-prompt.md`. The subagent must write both files. After it
finishes, call `pipeline.py record` for that chunk. A missing marker, omitted
block, changed image/math/code object, malformed meta, source change, or absent
dispatch snapshot is a failure and must be retried.

### 4. Close the glossary feedback loop

After each batch, use `merge_meta.py prepare-merge`, resolve every proposed
decision from its evidence, and use `merge_meta.py apply-merge`. Run the planner
again. Retranslate only chunks whose selected terms, entity attributes,
instructions, prompt, language, or source changed.

Convergence and anti-deadlock guarantee: All merged decisions record
`applied_meta_hashes` so settled terminology never re-triggers decisions.
To prevent cascading retranslation loops across long documents, `run_state.plan`
tracks precise term hashes and respects `TB_RETRANSLATE_ON_TERM_DRIFT=0` when
new term discoveries should not invalidate already verified, completed outputs.

Repeat until the translation queue is empty and every current meta hash is
merged. Then run `pipeline.py freeze`. Freeze refuses incomplete feedback.

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
  - References (参考文献) standardization (**GB/T 7714**): Centered unnumbered bold title (`.gov-ref-title`), 5号字 (10pt Times New Roman + 仿宋), 1.4 compact line height, standard Hanging Indent (`padding-left: 2em; text-indent: -2em;`) with `<span class="gov-ref-num">`, eliminating excessive page bloat.
  - Appendices (附录/附件) standardization (**GB/T 9704 / GB/T 7713**): Methods and reporting materials cleanly delineated as centered `附录：研究方法` (`.gov-appendix-title`), resetting internal sub-item numbering.
  - Page footer: 4号半角宋体 with em-dash (`— 1 —`), centered at bottom.
  - Standard 3-line tables (三线表) and centered, unindented figures/formulas.

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

Create contact sheets for generated PDFs and inspect them using [qa.md](references/qa.md).
Check the Official Document edition (`gov_doc`):
- Verify the red issuing authority displays the verified journal name (e.g., `自然·神经科学 参阅文件`).
- Verify the issue number displays the official DOI (e.g., `DOI〔2026〕s41593-026-02314-z 号`).
- **STRICT REJECTION**: Reject any publication displaying generic placeholders (`学术期刊译情参阅` or random hex hashes `编号：...`) when translating published papers.

Only then call `pipeline.py accept-publish` with concrete evidence. Report the
run complete only when `pipeline.py status` says `accepted`. Return the exact
paths and distinguish monolingual from bilingual and official document files.

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
