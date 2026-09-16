"""
merge_bilingual.py - Build interleaved bilingual (source + translation) outputs.

Reads each chunkNNNN.md (source) together with its output_chunkNNNN.md
(translation), aligns paragraph blocks, and emits:

  bilingual.md             - interleaved markdown (source blocks wrapped in
                             fenced divs with class "orig")
  book_bilingual.html      - web reading version with floating TOC
  book_bilingual_doc.html  - conversion source for Calibre formats
  book_bilingual.pdf       - PDF via calibre_html_publish.py (--formats pdf)

Alignment strategy (no content is ever dropped):
  1. Split both sides into heading-anchored sections; pair sections 1:1 when
     the counts match.
  2. Within a paired section, pair blocks 1:1 when the counts match.
  3. Otherwise degrade to one coarse pair per section (or per chunk), so the
     only cost of misalignment is coarser granularity, never lost text.
Image references are de-duplicated per pair: a figure shown on the source side
is removed from the translation side, and figures present only on the
translation side are kept.

Usage:
  merge_bilingual.py --temp-dir <path> [--title <t>] [--author <a>] [--lang <l>]
                     [--formats html,pdf] [--export-name <stem>]
"""

import re
import sys
import glob
import shutil
import argparse
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from manifest import validate_for_merge, read_output_text
from merge_and_build import (
    load_config,
    get_lang_config,
    check_pandoc_available,
    convert_with_pandoc,
    process_html_separators,
    apply_template_to_html,
    _check_generated_html_sanity,
    insert_toc_with_bs4,
    insert_toc_with_regex,
    natural_sort_key,
)

_FENCE_OPEN_RE = re.compile(r'^(`{3,}|~{3,})')
_HEADING_RE = re.compile(r'^#{1,6}\s+')
_MD_IMG_RE = re.compile(r'(?<!\\)!\[[^\]]*\]\(\s*([^)\s]+)[^)]*\)')
_HTML_IMG_RE = re.compile(r'<img\b[^>]*>', re.IGNORECASE)
_HTML_SRC_RE = re.compile(r'src\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)


def resolve_in_temp(temp_dir, name):
    """Resolve name under temp_dir, refusing anything that escapes it.

    All paths derived from the user-supplied temp_dir go through here so no
    '..' components or absolute names can move reads/writes outside the
    working directory.
    """
    if os_isabs(name):
        return None
    base = Path(temp_dir).resolve()
    path = (base / name).resolve()
    if path != base and base not in path.parents:
        return None
    return str(path)


def os_isabs(name):
    from os.path import isabs
    return isabs(name)


# =============================================================================
# Block model
# =============================================================================

def split_blocks(text):
    """Split markdown into blocks at blank lines, keeping fenced code intact."""
    lines = text.replace('\r\n', '\n').split('\n')
    blocks = []
    buf = []
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        m = _FENCE_OPEN_RE.match(stripped)
        if m:
            if buf:
                blocks.append('\n'.join(buf))
                buf = []
            fence_char = m.group(1)[0]
            fence_len = len(m.group(1))
            block = [lines[i]]
            i += 1
            while i < n:
                block.append(lines[i])
                close = _FENCE_OPEN_RE.match(lines[i].strip())
                if (close and close.group(1)[0] == fence_char
                        and len(close.group(1)) >= fence_len
                        and lines[i].strip() == close.group(1)):
                    i += 1
                    break
                i += 1
            blocks.append('\n'.join(block))
            continue
        if stripped == '':
            if buf:
                blocks.append('\n'.join(buf))
                buf = []
            i += 1
            continue
        buf.append(lines[i])
        i += 1
    if buf:
        blocks.append('\n'.join(buf))
    return [b.strip() for b in blocks if b.strip()]


def is_heading(block):
    """True only for single-line markdown headings."""
    return '\n' not in block and bool(_HEADING_RE.match(block))


def heading_text(block):
    return re.sub(r'^#{1,6}\s+', '', block).strip()


def is_image_only(block):
    """True when the block's visible content is only image references."""
    if '<img' in block.lower():
        return True
    for line in block.split('\n'):
        s = line.strip()
        if not s:
            continue
        if not (s.startswith('![') and s.endswith(')')):
            return False
    return True


# =============================================================================
# Image de-duplication
# =============================================================================

def extract_image_srcs(text):
    """Return the set of image URLs referenced by a markdown/HTML text."""
    srcs = set(_MD_IMG_RE.findall(text))
    for tag in _HTML_IMG_RE.findall(text):
        m = _HTML_SRC_RE.search(tag)
        if m and m.group(1):
            srcs.add(m.group(1))
    return srcs


def dedupe_images(src_text, trans_text):
    """Remove from the translation the image refs already shown on the source
    side. Returns the cleaned translation, or None if nothing remains."""
    src_set = extract_image_srcs(src_text)

    def keep_md(m):
        return m.group(0) if m.group(1) not in src_set else ''

    kept = _MD_IMG_RE.sub(keep_md, trans_text)

    def keep_html(m):
        src_m = _HTML_SRC_RE.search(m.group(0))
        return m.group(0) if not src_m or src_m.group(1) not in src_set else ''

    kept = _HTML_IMG_RE.sub(keep_html, kept)
    kept = re.sub(r'\n[ \t]*\n[ \t]*\n+', '\n\n', kept).strip()
    return kept or None


# =============================================================================
# Pairing
# =============================================================================

def split_sections(blocks):
    """Group blocks into sections; each heading starts a new section."""
    sections = []
    current = []
    for b in blocks:
        if is_heading(b) and current:
            sections.append(current)
            current = [b]
        else:
            current.append(b)
    if current:
        sections.append(current)
    return sections


def _join(blocks):
    return '\n\n'.join(blocks)


def pair_chunk(src_blocks, out_blocks):
    """Return a list of (source_text_or_None, translation_text_or_None) pairs."""
    src_secs = split_sections(src_blocks)
    out_secs = split_sections(out_blocks)

    if len(src_secs) != len(out_secs):
        # Heading anchors diverged — one coarse pair for the whole chunk.
        return [(_join(src_blocks), _join(out_blocks))]

    pairs = []
    for src_sec, out_sec in zip(src_secs, out_secs):
        if len(src_sec) == len(out_sec):
            for s, o in zip(src_sec, out_sec):
                pairs.append((s, o))
        else:
            # Block counts differ inside the section — coarse pair, no loss.
            pairs.append((_join(src_sec), _join(out_sec)))
    return pairs


def render_pairs(pairs):
    """Render pairs as interleaved bilingual markdown."""
    parts = []
    for src, trans in pairs:
        src_has_img = bool(src and extract_image_srcs(src))

        if src and is_image_only(src):
            # Figures live on the source side, unwrapped and unstyled.
            parts.append(src)
            if trans:
                cleaned = dedupe_images(src, trans)
                if cleaned:
                    parts.append(cleaned)
            continue

        if src:
            if is_heading(src):
                # Source headings render as plain text inside .orig so they do
                # not pollute the table of contents with duplicates.
                parts.append('::: {.orig}\n%s\n:::' % heading_text(src))
            else:
                parts.append('::: {.orig}\n%s\n:::' % src)

        if trans and trans.strip():
            cleaned = dedupe_images(src or '', trans) if src_has_img else trans.strip()
            if cleaned:
                parts.append(cleaned)
    return '\n\n'.join(parts)


# =============================================================================
# Build
# =============================================================================

def build_bilingual_markdown(temp_dir):
    """Merge every chunk pair into bilingual.md. Returns True on success."""
    print("=== Building bilingual markdown ===")

    ok, ordered_files, warnings = validate_for_merge(temp_dir)
    if not ok:
        print("ERROR: Merge validation failed. Fix the issues above first.")
        return False

    if ordered_files is None:
        # Legacy fallback (no manifest): glob-based 1:1 matching.
        print("WARNING: No manifest.json found — using legacy glob-based pairing.")
        sources = [f for f in glob.glob(str(Path(temp_dir) / 'chunk*.md'))
                   if not Path(f).name.startswith('output_')]
        sources.sort(key=lambda p: natural_sort_key(Path(p).name))
        ordered_files = []
        for p in sources:
            out_name = 'output_' + Path(p).name
            if not (Path(temp_dir) / out_name).exists():
                print(f"ERROR: Missing translation for {Path(p).name}")
                return False
            ordered_files.append(str(Path(temp_dir) / out_name))

    total_pairs = 0
    coarse_pairs = 0
    merged = []
    for out_path in ordered_files:
        base = Path(out_path).name
        stem = base[len('output_'):] if base.startswith('output_') else base
        src_path = resolve_in_temp(temp_dir, stem)
        if src_path is None:
            print(f"ERROR: unexpected chunk path outside temp dir: {base}")
            return False
        if not Path(src_path).exists():
            print(f"ERROR: Source chunk missing for {base}")
            return False
        src_text = read_output_text(src_path)
        trans_text = read_output_text(out_path)
        if src_text is None or trans_text is None:
            print(f"ERROR: Cannot read pair for {base}")
            return False
        pairs = pair_chunk(split_blocks(src_text), split_blocks(trans_text))
        total_pairs += len(pairs)
        coarse_pairs += sum(1 for s, t in pairs
                            if s and '\n\n' in s and not is_image_only(s))
        merged.append(render_pairs(pairs))

    bilingual_md = resolve_in_temp(temp_dir, 'bilingual.md')
    if bilingual_md is None:
        print("ERROR: cannot resolve bilingual.md inside temp dir")
        return False
    try:
        Path(bilingual_md).write_text('\n\n'.join(merged) + '\n', encoding='utf-8')
    except Exception as e:
        print(f"ERROR: cannot write bilingual.md: {e}")
        return False

    size = Path(bilingual_md).stat().st_size
    print(f"Wrote bilingual.md ({size:,} bytes) from {len(ordered_files)} chunks, "
          f"{total_pairs} paragraph pairs ({coarse_pairs} coarse/fallback pairs)")
    return True


def convert_bilingual_to_html(temp_dir, title, lang_cfg, author=None):
    """Convert bilingual.md to book_bilingual.html (web, TOC) and
    book_bilingual_doc.html (conversion source)."""
    print("=== Converting bilingual markdown to HTML ===")

    md_file = resolve_in_temp(temp_dir, 'bilingual.md')
    doc_file = resolve_in_temp(temp_dir, 'book_bilingual_doc.html')
    web_file = resolve_in_temp(temp_dir, 'book_bilingual.html')
    temp_html = resolve_in_temp(temp_dir, 'bilingual_raw.html')
    if None in (md_file, doc_file, web_file, temp_html):
        print("ERROR: cannot resolve output paths inside temp dir")
        return False

    if not Path(md_file).exists():
        print("Error: bilingual.md not found.")
        return False

    for out in (doc_file, web_file):
        if Path(out).exists():
            if Path(out).stat().st_mtime > Path(md_file).stat().st_mtime:
                if _check_generated_html_sanity(out):
                    print(f"Skipping {Path(out).name} — up to date")
                    continue
                print(f"Stale {Path(out).name} failed sanity — regenerating")
            Path(out).unlink()

    needs_doc = not Path(doc_file).exists()
    needs_web = not Path(web_file).exists()
    if not needs_doc and not needs_web:
        return True

    if Path(temp_html).exists():
        Path(temp_html).unlink()

    if not check_pandoc_available():
        print("ERROR: pandoc not found on PATH (required for bilingual HTML)")
        return False
    if not convert_with_pandoc(md_file, temp_html, title, lang_cfg['lang_attr']):
        return False

    process_html_separators(temp_html)

    html_content = Path(temp_html).read_text(encoding='utf-8')
    m = re.search(r'<body[^>]*>(.*)</body>', html_content, re.DOTALL | re.IGNORECASE)
    body = m.group(1).strip() if m else html_content

    template_doc = str(SCRIPT_DIR / 'template_bilingual_doc.html')
    template_web = str(SCRIPT_DIR / 'template_bilingual.html')
    if needs_doc and not apply_template_to_html(body, template_doc, doc_file, title, lang_cfg, author):
        return False
    if needs_web and not apply_template_to_html(body, template_web, web_file, title, lang_cfg, author):
        return False

    if not _check_generated_html_sanity(doc_file):
        return False
    if not _check_generated_html_sanity(web_file):
        return False

    Path(temp_html).unlink()
    print("Generated: book_bilingual_doc.html, book_bilingual.html")
    return True


def add_bilingual_toc(temp_dir):
    """Insert the heading TOC into book_bilingual.html."""
    print("=== Adding bilingual table of contents ===")
    web_file = resolve_in_temp(temp_dir, 'book_bilingual.html')
    if not web_file or not Path(web_file).exists():
        print("Warning: book_bilingual.html not found, skipping TOC")
        return False
    try:
        return insert_toc_with_bs4(web_file)
    except Exception as e:
        print(f"TOC insertion via bs4 failed ({e}); falling back to regex")
        return insert_toc_with_regex(web_file)


def generate_bilingual_pdf(temp_dir, lang_attr):
    """Print book_bilingual.html to PDF with headless Edge (MathJax renders
    the formulas), falling back to Calibre on the doc HTML."""
    print("=== Generating book_bilingual.pdf ===")
    web_html = resolve_in_temp(temp_dir, 'book_bilingual.html')
    doc_html = resolve_in_temp(temp_dir, 'book_bilingual_doc.html')
    output_file = resolve_in_temp(temp_dir, 'book_bilingual.pdf')
    work_dir = resolve_in_temp(temp_dir, 'book_bilingual_pdf_temp')
    if None in (web_html, doc_html, output_file, work_dir):
        print("ERROR: cannot resolve PDF build paths inside temp dir")
        return None

    from merge_and_build import print_html_to_pdf_via_edge

    if (Path(output_file).exists()
            and Path(output_file).stat().st_mtime > Path(web_html).stat().st_mtime):
        print(f"Skipping PDF — up to date ({Path(output_file).stat().st_size:,} bytes)")
        return output_file

    out = print_html_to_pdf_via_edge(web_html, output_file, timeout=900)
    if out:
        return out
    print("Falling back to Calibre for PDF (formulas will not render properly)")
    try:
        import calibre_html_publish as publisher
    except ImportError as e:
        print(f"ERROR: cannot import calibre_html_publish: {e}")
        return None

    Path(work_dir).mkdir(parents=True, exist_ok=True)
    try:
        publisher.copy_images_if_needed(doc_html, work_dir)
        work_html = publisher.prepare_html_for_conversion(doc_html, work_dir, lang_attr)
        ok = publisher.convert_html_with_calibre(
            work_html, output_file, 'pdf', timeout=1800, lang=lang_attr)
        return output_file if ok else None
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def export_aliases(temp_dir, export_name):
    stem = export_name.strip()
    if not stem or any(sep in stem for sep in ('/', '\\')) or '\x00' in stem:
        print(f"ERROR: invalid export name: {export_name!r}")
        return []
    mappings = {
        'book_bilingual.html': f'{stem}_bilingual.html',
        'book_bilingual.pdf': f'{stem}_bilingual.pdf',
    }
    copied = []
    for src_name, dst_name in mappings.items():
        src = resolve_in_temp(temp_dir, src_name)
        dst = resolve_in_temp(temp_dir, dst_name)
        if dst is None or src is None:
            print(f"ERROR: invalid export name: {export_name!r}")
            return []
        if Path(src).exists():
            shutil.copy2(src, dst)
            copied.append(Path(dst).name)
    return copied


def main():
    parser = argparse.ArgumentParser(
        description='Build interleaved bilingual (source + translation) outputs')
    parser.add_argument('--temp-dir', required=True, help='Temp directory path')
    parser.add_argument('--title', default=None, help='Translated book title (override config)')
    parser.add_argument('--author', default=None, help='Author name (override config)')
    parser.add_argument('--lang', default=None, help='Output language code (override config)')
    parser.add_argument('--formats', default='html,pdf',
                        help='Comma-separated output formats: html, pdf (default: html,pdf)')
    parser.add_argument('--export-name', default=None,
                        help='Optional filename stem for exported alias copies')
    args = parser.parse_args()
    temp_dir = args.temp_dir

    if not Path(temp_dir).is_dir():
        print(f"Error: Temp directory not found: {temp_dir}")
        sys.exit(1)

    config = load_config(temp_dir)
    lang_code = args.lang or config.get('output_lang', 'zh')
    lang_cfg = get_lang_config(lang_code)
    title = args.title or config.get('original_title', 'Translated Book')
    author = args.author or config.get('creator', 'Unknown Author')

    print("=== Bilingual Build ===")
    print(f"Temp directory: {temp_dir}")
    print(f"Title: {title}")
    print(f"Language: {lang_code} (attr: {lang_cfg['lang_attr']})")

    if not build_bilingual_markdown(temp_dir):
        sys.exit(1)

    results = {}
    formats = [f.strip().lower() for f in args.formats.split(',') if f.strip()]

    if 'html' in formats:
        if not convert_bilingual_to_html(temp_dir, title, lang_cfg, author):
            sys.exit(1)
        add_bilingual_toc(temp_dir)
        for name in ('book_bilingual.html', 'book_bilingual_doc.html'):
            p = Path(temp_dir) / name
            if p.exists():
                results[name] = f"{p.stat().st_size:,} bytes"

    if 'pdf' in formats:
        pdf_path = generate_bilingual_pdf(temp_dir, lang_cfg['lang_attr'])
        if pdf_path:
            results['book_bilingual.pdf'] = f"{Path(pdf_path).stat().st_size:,} bytes"
        else:
            results['book_bilingual.pdf'] = 'FAILED'

    if args.export_name:
        for name in export_aliases(temp_dir, args.export_name):
            print(f"Export alias: {name}")

    print("\n=== Bilingual Build Complete ===")
    for name, detail in results.items():
        print(f"  {name}: {detail}")


if __name__ == '__main__':
    main()
