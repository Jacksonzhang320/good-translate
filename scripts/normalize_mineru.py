"""
normalize_mineru.py - Rebuild clean Markdown from a MinerU middle.json.

Fixes the failure modes of MinerU's stock markdown output for book/paper
translation:

  1. Figures split into panels/strips: consecutive image/chart blocks on the
     same page (no intervening text, small bbox gaps) are re-cropped from the
     source PDF as ONE image.
  2. Text fragmented by page/column breaks and interrupted by figures:
     paragraphs that continue mid-sentence are merged back together, and an
     interrupting figure is moved after the reunited paragraph.
  3. Repeated noise (running headers, footers, page numbers): never included,
     because MinerU already separates them into discarded_blocks, which we
     ignore.

middle.json is used instead of content_list.json because its bboxes are in
real PDF points and its para_blocks already exclude discarded furniture.

Usage (standalone):
  normalize_mineru.py --mineru-dir <dir> --pdf <source.pdf> --out <out.md> \
                      [--images-out <dir>] [--zoom 2.0]

Library use:
  md_text, extra_images = normalize(mineru_dir, pdf_path)
  # extra_images: list of (relative_name, png_bytes) for merged figure crops
"""

import os
import re
import glob
import json
import warnings

_FIGURE_TYPES = {'image', 'chart'}

# Same-figure proximity thresholds in PDF points: panels sit side by side in
# a row (small horizontal gap, strong vertical overlap) or stacked (small
# vertical gap, strong horizontal overlap).
_H_GAP = 90
_V_GAP = 36

_TERMINAL_PUNCT = tuple('.。!？?；;…”"\'）)]…')
_CAPTION_START = re.compile(
    r'^(?:(?:Extended Data |Supplementary )?Fig(?:ure)?\.?\s*\d+|Table\s*\d+)',
    re.I,
)


def _find_middle_json(mineru_dir):
    """Locate the main middle.json inside a MinerU output directory."""
    hits = sorted(glob.glob(os.path.join(mineru_dir, '**', '*middle.json'),
                            recursive=True))
    if not hits:
        return None
    hits.sort(key=lambda p: os.path.getsize(p), reverse=True)
    return hits[0]


def _span_text(block):
    """Reconstruct the text of a text/title/ref_text block from its lines.
    Inline-equation spans are wrapped in $...$ so their LaTeX survives as
    math in the markdown."""
    lines = []
    for line in block.get('lines', []):
        parts = []
        for s in line.get('spans', []):
            c = s.get('content', '')
            if not c:
                continue
            if s.get('type') == 'inline_equation':
                c = ' $' + c.strip() + '$ '
            parts.append(c)
        line_text = ''.join(parts).strip()
        if line_text:
            lines.append(line_text)
    return lines


def _join_lines(lines):
    """Join hard-wrapped lines into one paragraph, de-hyphenating line-end
    splits ('infor-' + 'mation' -> 'information')."""
    out = ''
    for ln in lines:
        if not out:
            out = ln
        elif out.endswith('-') and ln[:1].islower():
            out = out[:-1] + ln
        else:
            out += ' ' + ln
    return out


def _inner_span(block, inner_type, key):
    """Read both direct lines/spans and nested MinerU wrapper layouts."""
    for inner in _walk_blocks(block):
        if inner.get('type') == inner_type:
            for span in _spans(inner):
                if span.get(key):
                    return span[key]
    return None


def _walk_blocks(block):
    yield block
    for child in block.get('blocks', []):
        yield from _walk_blocks(child)


def _spans(block):
    yield from block.get('spans', [])
    for line in block.get('lines', []):
        yield from line.get('spans', [])


def _equation_latex(block):
    """Retain every formula span, including the common direct-span shape."""
    parts = []
    for inner in _walk_blocks(block):
        for span in _spans(inner):
            content = span.get('content')
            if content:
                parts.append(str(content).strip())
    return ' '.join(parts)


def _captions(block):
    return [child for child in _walk_blocks(block)
            if child.get('type', '').endswith('_caption')
            or child.get('type') in ('caption', 'image_footnote', 'chart_footnote')]


def _body_bbox(block):
    bodies = [b['bbox'] for b in _walk_blocks(block)
              if b.get('type') in ('image_body', 'chart_body', 'table_body')
              and b.get('bbox')]
    return _bbox_union(bodies) if bodies else list(block['bbox'])


def _contained(a, b):
    overlap = max(0, _overlap((a[0], a[2]), (b[0], b[2]))) * max(
        0, _overlap((a[1], a[3]), (b[1], b[3])))
    small = min((a[2] - a[0]) * (a[3] - a[1]),
                (b[2] - b[0]) * (b[3] - b[1]))
    return small > 0 and overlap / small >= .85


def _figure_groups(ordered):
    """Build connected same-figure components from all page visual bodies.

    MinerU can interleave panels with text and attach one caption to a middle
    panel, so spatial connectivity is more reliable than block adjacency.
    """
    groups = [{'items': [(pos, block)], 'bbox': _body_bbox(block)}
              for pos, block in enumerate(ordered)
              if block.get('type') in _FIGURE_TYPES]
    changed = True
    while changed:
        changed = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                if any(_contained(_body_bbox(a), _body_bbox(b)) or
                       _can_merge(_body_bbox(a), _body_bbox(b))
                       for _, a in groups[i]['items'] for _, b in groups[j]['items']):
                    groups[i]['items'].extend(groups[j]['items'])
                    groups[i]['items'].sort(key=lambda item: item[0])
                    groups[i]['bbox'] = _bbox_union(
                        [_body_bbox(block) for _, block in groups[i]['items']])
                    del groups[j]
                    changed = True
                    break
            if changed:
                break
    groups.sort(key=lambda group: group['items'][0][0])
    return groups


def _group_captions(group):
    """Return complete caption records while excluding panel-letter labels."""
    rows = []
    bottom = group['bbox'][3]
    for _, block in group['items']:
        for child in _captions(block):
            text = _join_lines(_span_text(child)).strip()
            box = child.get('bbox')
            if not text or not box:
                continue
            if _CAPTION_START.match(text) or (box[1] >= bottom - 8 and len(text) > 8):
                rows.append({'text': text, 'bbox': list(box)})
    # Captions commonly continue from the left column into the right column;
    # minor baseline differences must not put the right continuation first.
    rows.sort(key=lambda row: (row['bbox'][0], row['bbox'][1]))
    if not rows:
        return []
    records, current = [], None
    for row in rows:
        if _CAPTION_START.match(row['text']):
            if current:
                records.append(current)
            current = {'parts': [row['text']], 'boxes': [row['bbox']]}
        elif current:
            current['parts'].append(row['text'])
            current['boxes'].append(row['bbox'])
    if current:
        records.append(current)
    else:
        records.append({'parts': [row['text'] for row in rows],
                        'boxes': [row['bbox'] for row in rows]})
    return [{'text': ' '.join(record['parts']),
             'bbox': _bbox_union(record['boxes'])} for record in records]


def _embedded_figure_text_positions(group, ordered):
    """Find short text boxes that physically overlap a grouped figure.

    MinerU occasionally emits an axis label or panel letter as a top-level text
    block instead of attaching it to the chart.  Keep those labels in the crop
    and suppress the duplicate prose block.  Requiring real vertical overlap
    avoids consuming captions immediately below the figure.
    """
    base = group['bbox']
    positions = set()
    member_positions = {pos for pos, _ in group['items']}
    for pos, block in enumerate(ordered):
        if pos in member_positions or block.get('type') != 'text' or not block.get('bbox'):
            continue
        text = _join_lines(_span_text(block)).strip()
        if not text or len(text) > 120 or _CAPTION_START.match(text):
            continue
        box = block['bbox']
        horizontal = _overlap((base[0], base[2]), (box[0], box[2]))
        vertical = _overlap((base[1], base[3]), (box[1], box[3]))
        if horizontal > 0 and vertical > 0:
            positions.add(pos)
    return positions


def _group_visual_bbox(group, ordered=None):
    """Include panel and axis labels without pulling in prose captions."""
    boxes = [group['bbox']]
    for _, block in group['items']:
        for child in _captions(block):
            text = _join_lines(_span_text(child)).strip()
            if child.get('bbox') and len(text) <= 4 and not _CAPTION_START.match(text):
                boxes.append(list(child['bbox']))
    if ordered is not None:
        for pos in _embedded_figure_text_positions(group, ordered):
            boxes.append(list(ordered[pos]['bbox']))
    return _bbox_union(boxes)


def _overlap(a, b):
    """Overlap length of two 1-D spans (0 when disjoint)."""
    return min(a[1], b[1]) - max(a[0], b[0])


def _can_merge(b1, b2):
    x1, y1, x2, y2 = b1
    u1, v1, u2, v2 = b2
    v_ov = _overlap((y1, y2), (v1, v2))
    h_ov = _overlap((x1, x2), (u1, u2))
    same_row = v_ov >= 0.5 * min(y2 - y1, v2 - v1) and max(u1 - x2, x1 - u2, 0) <= _H_GAP
    stacked = h_ov >= 0.5 * min(x2 - x1, u2 - u1) and max(v1 - y2, y1 - v2, 0) <= _V_GAP
    return same_row or stacked


def _cluster(figs):
    """Group figure blocks into proximity clusters (fixpoint union)."""
    clusters = [[f] for f in figs]
    changed = True
    while changed:
        changed = False
        for i in range(len(clusters) - 1):
            a, b = clusters[i], clusters[i + 1]
            if any(_can_merge(x['bbox'], y['bbox']) for x in a for y in b):
                clusters[i] = a + b
                del clusters[i + 1]
                changed = True
                break
    return clusters


def _bbox_union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _looks_like_continuation(text):
    """A block that starts mid-sentence: begins lowercase (or digit)."""
    first = text.lstrip()[:1]
    return bool(first) and (first.islower() or first.isdigit())


def normalize(mineru_dir, pdf_path, merge_figures=True, merge_sentences=True,
              zoom=2.0, images_dirname='images'):
    """Rebuild clean markdown from a MinerU output directory.

    Returns (md_text, extra_images) where extra_images is a list of
    (relative_name, png_bytes) crops for merged figures — the caller writes
    them next to the regular images.
    """
    middle_json = _find_middle_json(mineru_dir)
    if not middle_json:
        raise FileNotFoundError(f'no middle.json under {mineru_dir}')
    with open(middle_json, 'r', encoding='utf-8') as f:
        pdf_info = json.load(f).get('pdf_info') or []

    doc = None
    if merge_figures:
        import fitz
        doc = fitz.open(pdf_path)
    md_blocks = []
    caption_texts = set()
    extra_images = []
    crop_seq = 0

    for page in pdf_info:
        # Blocks on one page, in reading order.
        ordered = sorted(page.get('para_blocks', []),
                         key=lambda b: b.get('index', 0))
        groups = _figure_groups(ordered)
        starts = {g['items'][0][0]: g for g in groups}
        embedded_figure_text = set()
        for group in groups:
            embedded_figure_text.update(_embedded_figure_text_positions(group, ordered))

        def emit_figure(group):
            nonlocal crop_seq
            if doc is None or page.get('page_idx', 0) >= doc.page_count:
                refs = set()
                for _, b in group['items']:
                    path = _inner_span(b, b['type'] + '_body', 'image_path')
                    if path and path not in refs:
                        refs.add(path)
                        md_blocks.append(f'![]({images_dirname}/{os.path.basename(path)})')
                if not refs:
                    raise ValueError('figure has no crop and no image_path')
            else:
                # Model boxes can end on glyph edges. A small page-clamped
                # margin preserves edge labels without borrowing the caption.
                source_page = doc[page.get('page_idx', 0)]
                union = fitz_rect(_group_visual_bbox(group, ordered)) + (-4, -4, 4, 4)
                union &= source_page.rect
                pix = source_page.get_pixmap(matrix=fitz_matrix(zoom), clip=union)
                crop_seq += 1
                name = f'figure_p{page.get("page_idx", 0) + 1:02d}_{crop_seq:02d}.png'
                extra_images.append((f'{images_dirname}/{name}',
                                     pix.tobytes('png')))
                md_blocks.append(f'![]({images_dirname}/{name})')
            for caption in _group_captions(group):
                text = caption['text']
                caption_texts.add(text)
                md_blocks.append(text)

        for pos, b in enumerate(ordered):
            btype = b.get('type')
            if pos in embedded_figure_text:
                continue
            if btype in _FIGURE_TYPES:
                if pos in starts:
                    emit_figure(starts[pos])
                continue
            if btype in ('text', 'title', 'abstract', 'ref_text', 'caption',
                         'image_caption', 'chart_caption', 'table_caption',
                         'footnote', 'table_footnote'):
                text = _join_lines(_span_text(b))
                if not text:
                    continue
                if btype.endswith('caption'):
                    caption_texts.add(text)
                md_blocks.append('# ' + text if btype == 'title' else text)
            elif btype in ('interline_equation', 'equation', 'formula'):
                latex = _equation_latex(b)
                if latex:
                    md_blocks.append(f'$$\n{latex.strip()}\n$$')
                else:
                    path = _inner_span(b, 'interline_equation', 'image_path')
                    if path:
                        md_blocks.append(f'![]({images_dirname}/{os.path.basename(path)})')
                    elif doc is not None and b.get('bbox'):
                        source_page = doc[page.get('page_idx', 0)]
                        box = (fitz_rect(b['bbox']) + (-2, -2, 2, 2)) & source_page.rect
                        crop_seq += 1
                        name = f'{images_dirname}/formula_p{page.get("page_idx", 0)+1:02d}_{crop_seq:02d}.png'
                        extra_images.append((name, source_page.get_pixmap(
                            matrix=fitz_matrix(zoom), clip=box).tobytes('png')))
                        md_blocks.append(f'![]({name})')
                    else:
                        raise ValueError('independent equation has no text, image or source crop')
            elif btype == 'table':
                html = _inner_span(b, 'table_body', 'html')
                if html:
                    md_blocks.append(html)
                else:
                    raise ValueError('table has no HTML; use build_document for a source-image fallback')
                for child in _captions(b):
                    text = _join_lines(_span_text(child))
                    if text:
                        caption_texts.add(text)
                        md_blocks.append(text)
            else:
                warnings.warn(f'unhandled MinerU block type {btype!r}; use build_document for coverage checks')

    if doc is not None:
        doc.close()

    # --- sentence-continuation merging across breaks ------------------------
    if merge_sentences:
        merged = []
        pending_images = []
        for block in md_blocks:
            if block.startswith('!['):
                # A figure between two halves of one sentence never separates
                # them: hold it back and place it after the reunited text.
                pending_images.append(block)
                continue
            is_text = block not in caption_texts and not (block.startswith('$$') or block.startswith('#')
                           or block.startswith('<') or block.startswith('|'))
            prev = merged[-1] if merged else None
            prev_is_text = prev is not None and prev not in caption_texts and not (
                prev.startswith('![') or prev.startswith('$$') or prev.startswith('#')
                or prev.startswith('<') or prev.startswith('|'))
            if (is_text and prev_is_text and _looks_like_continuation(block)
                    and not prev.rstrip().endswith(_TERMINAL_PUNCT)):
                merged[-1] = prev.rstrip() + ' ' + block.lstrip()
                continue
            for img in pending_images:
                merged.append(img)
            pending_images = []
            merged.append(block)
        for img in pending_images:
            merged.append(img)
        md_blocks = merged

    md_text = '\n\n'.join(md_blocks) + '\n'
    return md_text, extra_images


def fitz_matrix(zoom):
    import fitz
    return fitz.Matrix(zoom, zoom)


def fitz_rect(union):
    import fitz
    return fitz.Rect(*union)


def main():
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(
        description='Rebuild clean markdown from a MinerU middle.json')
    parser.add_argument('--mineru-dir', required=True)
    parser.add_argument('--pdf', required=True, help='source PDF (for figure re-cropping)')
    parser.add_argument('--out', required=True, help='output markdown path')
    parser.add_argument('--images-out', default=None,
                        help='directory for merged figure crops (default: <out dir>/images)')
    parser.add_argument('--zoom', type=float, default=2.0, help='crop render scale (default 2.0)')
    parser.add_argument('--no-merge-figures', action='store_true')
    parser.add_argument('--no-merge-sentences', action='store_true')
    args = parser.parse_args()

    md_text, extra_images = normalize(
        args.mineru_dir, args.pdf,
        merge_figures=not args.no_merge_figures,
        merge_sentences=not args.no_merge_sentences,
        zoom=args.zoom)

    out_path = Path(args.out).resolve()
    images_out = (Path(args.images_out).resolve() if args.images_out
                  else out_path.parent / 'images')
    images_out.mkdir(parents=True, exist_ok=True)
    for rel_name, png_bytes in extra_images:
        # basename() strips directory components: the crop can only land
        # inside images_out, never outside it.
        target = images_out / os.path.basename(rel_name)
        if target.parent != images_out:
            raise SystemExit(f'invalid image name: {rel_name!r}')
        target.write_bytes(png_bytes)

    out_path.write_text(md_text, encoding='utf-8')

    print(f'normalized markdown -> {out_path} ({len(md_text):,} chars, '
          f'{len(extra_images)} merged figures)')


if __name__ == '__main__':
    main()
