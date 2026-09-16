# AGENTS.md

## Project

good-translate is an agent skill for Codex, Claude Code, and OpenClaw that translates books (PDF/DOCX/EPUB) into any language using parallel subagents. Published on ClawHub as `good-translate` and on GitHub as `Jacksonzhang320/good-translate`.

## Structure

- `SKILL.md` — Skill definition, the orchestration logic that Codex / Claude Code / OpenClaw follows
- `scripts/convert.py` — PDF/DOCX/EPUB → Markdown chunks (via Calibre HTMLZ)
- `scripts/pipeline.py` — reliable PDF preflight, prepare, dispatch, acceptance, status, and cleanup entry point
- `scripts/pdf_document.py` — source PDF + MinerU/OCR geometry → block-addressed `doc.json`, `style.json`, and source crops
- `scripts/publish.py` — versioned source-style HTML/PDF publisher with browser and acceptance gates
- `scripts/qa_preview.py` — versioned source/object/output contact sheets for visual QA
- `scripts/job.py`, `scripts/start_task.ps1` — JSON-argv adapter for the global long-task runner
- `scripts/manifest.py` — SHA-256 chunk tracking and merge validation
- `scripts/glossary.py` — Term-consistency glossary; per-chunk term tables injected into sub-agent prompts
- `scripts/chunk_context.py` — Read-only previous/next chunk excerpts injected into sub-agent prompts
- `scripts/meta.py` — Per-chunk sub-agent observation file schema
- `scripts/merge_meta.py` — Batch-boundary merge of sub-agent observations into the canonical glossary
- `scripts/run_state.py` — Selective re-translation planner and run_state.json recorder
- `scripts/merge_and_build.py` — Merge translated chunks → HTML/DOCX/EPUB/PDF
- `scripts/calibre_html_publish.py` — Calibre format conversion wrapper
- `scripts/template.html`, `scripts/template_ebook.html` — HTML templates
- `references/translate-prompt.md` — hashed per-chunk translation contract
- `references/workflow.md`, `references/qa.md` — detailed PDF execution and visual acceptance rules

## Testing changes

Use a small file for quick checks, or the checked-in baseline book for the repository's full-pipeline test.

Reliable PDF unit and render checks:

```bash
uv run python -m unittest discover -s tests -p 'test_*.py' -v
uv run python tests/render_smoke.py --out tests/.artifacts/render-<new-id>
```

Full baseline test:

```bash
mkdir -p tests/.artifacts
cd tests/.artifacts
python3 ../../scripts/convert.py ../baselines/standard-alice/standard-alice.epub --olang zh
# then run translation via the skill
python3 ../../scripts/merge_and_build.py --temp-dir standard-alice_temp --title "test"
```

Verify: all output_chunk*.md files exist, manifest validation passes, output formats generate.

## Conventions

- Only `chunk*.md` naming — no `page*` legacy support
- Legacy pipeline artifacts keep the canonical names `book.html`, `book_doc.html`, `book.docx`, `book.epub`, `book.pdf`. Reliable PDF publication keeps those names inside a content-addressed `publish/<render-hash>/` directory.
- PDF runs never equate `generated` with `accepted`; parse and publication acceptance records include hashes and concrete review evidence.
- SKILL.md frontmatter must stay single-line per field (OpenClaw parser requirement)
- Script paths in SKILL.md use `{baseDir}` not hardcoded paths
- Subagent instructions in SKILL.md must be platform-neutral (work on Codex, Claude Code, OpenClaw)
- Checked-in baseline inputs live under `tests/baselines/<book-id>/`; generated full-pipeline outputs live under `tests/.artifacts/`
- README changes must be synced to both README.md and README.zh-CN.md
- Releases follow `.claude/commands/release.md` — three commands in order: `git push origin main`, `git tag vX.Y.Z && git push --tags`, `npx clawhub@latest publish ./ --version X.Y.Z`. Do not skip the git tag; it's the only version anchor in the repo

## Do not

- Do not reintroduce `page*` file support — it was intentionally removed
- Do not hardcode per-runtime skill paths (`~/.agents/skills/`, `~/.claude/skills/`) in SKILL.md — use `{baseDir}`
- Do not put platform-specific tool names (Agent, sessions_spawn) in `allowed-tools` as the only option — keep the whitelist cross-platform
- Do not restore existence-only publication caching for the reliable PDF path. Its render hash covers content, style, metadata, assets, renderer, and MathJax.

## Cursor Cloud specific instructions

### Environment

- Python 3.12+ is pre-installed; no version manager needed.
- System dependencies (Calibre, Pandoc) and pip packages (pypandoc, beautifulsoup4) are installed by the update script.
- Sync `uv.lock` before tests. PDF and publishing tests use the locked PyMuPDF, Pillow, Markdown, and Playwright dependencies.

### Running tests

- **Unit tests:** `uv run python -m unittest discover -s tests -p 'test_*.py' -v`
- **Browser render smoke:** `uv run python tests/render_smoke.py --out tests/.artifacts/render-<new-id>`

### Full pipeline integration test

Run from `tests/.artifacts/` to keep generated files out of the repo root:

```bash
mkdir -p tests/.artifacts && cd tests/.artifacts
python3 ../../scripts/convert.py ../baselines/standard-alice/standard-alice.epub --olang zh
# Create mock output_chunk*.md files (copy source chunks) since actual translation requires LLM subagents
for f in standard-alice_temp/chunk*.md; do cp "$f" "standard-alice_temp/output_$(basename $f)"; done
python3 ../../scripts/merge_and_build.py --temp-dir standard-alice_temp --title "test"
```

### Known issues

- Ubuntu's Calibre 7.6.0 package has an EPUB generation bug (bytes/str mismatch in `container.py`). DOCX and PDF generation work fine. This is a distro packaging issue, not a codebase bug.
- `pypandoc` installs its CLI script to `~/.local/bin` which may not be on PATH, but the Python library import works regardless.
