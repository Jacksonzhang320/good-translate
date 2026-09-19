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

Digital vector PDF (single or dual-column, e.g. Nature/Science/Cell papers):

```text
pipeline.py prepare paper.pdf --temp-dir runs/paper-01 --lang zh-CN --instructions-file style.txt
```
(Uses column-aware geometry flow in ~1s: auto reading order, micro-figure clustering, zero GPU/CUDA/MinerU dependency).

Complex mathematical proofs or dense formula layouts:

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
2. `pipeline.py outline <run>` (Agent inspects TOC tree for suspicious headers)
3. `pipeline.py plan-split <run> --target-chunk-size 12000` (Agent reviews chapter landmarks)
4. `pipeline.py split <run> [--cuts <id1> <id2> ...]` (Deterministic 0.01s cut into 6~9 section chunks)
5. `pipeline.py seed-glossary <run> --top-n 30` (Phase 0: Anchor Glossary pre-mining, Agent reviews domain targets)
6. `pipeline.py bypass-refs <run>` (Code automatically bypasses pure citation chunks in 0.1s)
7. `run_state.py plan <run>`
8. `pipeline.py dispatch <run> <listed chunk IDs>` (Phase 1: Injects Anchor Glossary prompt contract; 1 Subagent = 1 Chunk flat concurrency)
9. Translate each chunk with a dedicated subagent concurrently (6~8 subagents simultaneously, finishes in 90~120s).
10. `pipeline.py record <run> <completed IDs>`
11. `merge_meta.py prepare-merge <run>`
12. Resolve all evidence-backed decisions and apply them with `merge_meta.py apply-merge <run>`.
13. `pipeline.py patch-terms <run>` (Phase 2: Surgical regex/AST term replacement across all chunks in 0.05s, zero retranslation loop)
14. `pipeline.py freeze <run>` and `run_state.py verify <run>`.

A failed subagent attempt keeps its dispatch snapshot. Repair the output and
record it, or explicitly cancel that dispatch before starting another attempt.
Never infer or backfill a dispatch history after the fact.

## Division of Labor: Code vs. Agent vs. Skill (各司其职协作体系)

To guarantee high reproducibility, token economy, and zero layout decay, the translation pipeline enforces strict separation of responsibilities:

| 阶段 (Stage) | 代码职责 (Code / Deterministic) | Agent职责 (Agent / Semantic Cognition) | Skill规范 (Skill / Invariants) |
|---|---|---|---|
| **1. 解析与切分**<br>(Parse & Chunking) | • 原生双栏几何流解析（1 秒无依赖）<br>• 提取大纲标桩与推荐切分点 (`plan-split`)<br>• 毫秒级确定性执行物理切割 (`split --cuts`)<br>• 疑似页眉降级（`SUSPICIOUS_RUNNING_HEADERS`） | • 运行 `plan-split` 审查大纲树与语义章节切分点<br>• 识别并处置漏网的伪标题/走马页眉<br>• 审核核心切图与阅读顺序 | • 严禁引文中间跨块截断<br>• 大纲必须通过人工/Agent初审后方可切分 |
| **2. 翻译与调度**<br>(Translation & Dispatch) | • Phase 0 高频专有名词预挖掘 (`pipeline.py seed-glossary`)<br>• 纯引文块一键免译直通 (`pipeline.py bypass-refs`)<br>• 表格单元格富标签放行 (`<sup>`, `<sub>`)<br>• 默认禁用全块推倒重译雪崩 (`TB_RETRANSLATE_ON_TERM_DRIFT=0`) | • Phase 0 核实基准词表标准中文译名（30 秒初审）<br>• Phase 1 实行 **1 Subagent = 1 Chunk** 绝对全并发<br>• 确保块标 `<!-- tb:b... -->` 与数学公式 100% 留存 | • 参考文献零篡改、零幻觉、零额外 Token 消耗<br>• 保持顶级中文学术期刊文风与术语统一 |
| **3. 术语与排版**<br>(Surgical Patch & Layout) | • Phase 2 定点外科手术替换 (`pipeline.py patch-terms`)<br>• 鲁棒引文状态机（未知标题不跳出 `in_refs`）<br>• 自动解析并连续排序 1..N 引用文献<br>• 自动生成版面健康矩阵 (`layout_matrix`) | • 检查引文区域与正文/尾注的边界<br>• 验证公文版红头元数据（期刊名、DOI）<br>• 确认附录与作者单位未被错误卷入引文 | • GB/T 9704-2012 标题层级与红头规范<br>• GB/T 7714-2015 引用格式规范 |
| **4. 质量验收**<br>(QA & Acceptance) | • 静态版面巡检器 (`pipeline.py audit-layout`)<br>• Phase 3 术语一致性审查（拦截废弃别名泄漏）<br>• 作为 `accept-publish` 的硬阻塞前置门禁 | • 执行 4 类核心页面视觉抽检（`view_file`）：<br>  1. 首页红头/标题; 2. 首个图表页;<br>  3. 引文起止页; 4. 尾注/附录页<br>• 提交具备具体凭证的证据字符串 | • 拒绝仅凭 DOM 校验即盲目验收<br>• 无明确页面证据不得闭环 |

## Academic metadata retrieval gate

For scientific papers, passive regex extraction often misses preprints, uncorrected proofs, or papers with nonstandard header layouts. Relying on silent fallbacks produces generic placeholders (`学术期刊译情参阅 编号：<hash>`), which violates publication quality standards.

The Agent must actively execute this retrieval gate:
1. **Detect**: In Step 1/2, check if the source paper's journal and DOI are definitively known.
2. **Search**: If missing or ambiguous, run a web/academic search query:
   `"<Paper Title>" "<First Author>"`
   Verify whether the paper has been published in a peer-reviewed journal or remains a preprint.
3. **Resolve**:
   - Journal Name -> Standard Chinese publication header: `{Journal_ZH} 参阅文件` (e.g. `自然·神经科学 参阅文件`, `细胞 参阅文件`, `科学 参阅文件`, `神经元 参阅文件`, `bioRxiv 预印参阅文件`).
   - DOI & Year -> Standard document number: `DOI〔{Year}〕{short_doi} 号`.
4. **Inject**: Update `style.json` (`gov_header`) or pass `--journal` / `--doi` / `--org-name` / `--doc-number` during `pipeline.py build`.

## Publication

The default build produces both HTML/PDF editions. Use `--formats
html,pdf,docx,epub` only when additional formats are requested. HTML files are
bundles: keep their sibling `assets/` directory.

Outputs are generated strictly once per requested edition (`{Title}_{Edition}.pdf` / `.html`).
Do not generate or copy redundant backward-compatibility duplicates (`book.html`, `book.pdf`, etc.)
unless `--legacy-aliases` is explicitly requested. Final deliverables to users must include only the
semantic edition files and their required `assets/`.

Generation is mechanical. `accept-publish` hashes every artifact again and
requires a reviewer plus concrete visual evidence. `pipeline.py status` is the
canonical completion check.
