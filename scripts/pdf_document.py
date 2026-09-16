"""Preserve PDF structure, source crops and measured typography before translation.

MinerU supplies reading order and semantic boxes; PyMuPDF supplies original
pixels and typography. The geometry-only path is intentionally limited: complex
or uncovered content creates explicit errors and a source-page safety copy.
No OCR or model inference is started by this module.
"""

from collections import Counter
from difflib import SequenceMatcher
from hashlib import sha256
import html
import json
from pathlib import Path
import re
from statistics import median

import normalize_mineru as norm


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':')).encode('utf-8')).hexdigest()


def _issue(issues, code, message, page=None, severity='error', **details):
    item = {'code': code, 'severity': severity, 'message': message}
    if page is not None:
        item['page'] = page
    item.update(details)
    issues.append(item)


def _area(box):
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _covered(box, containers, threshold=.8):
    if not _area(box):
        return False
    return any(max(0, min(box[2], b[2] + 3) - max(box[0], b[0] - 3)) *
               max(0, min(box[3], b[3] + 3) - max(box[1], b[1] - 3)) /
               _area(box) >= threshold for b in containers)


def _box(block):
    if block.get('bbox'):
        return [round(float(x), 3) for x in block['bbox']]
    boxes = [line['bbox'] for line in block.get('lines', []) if line.get('bbox')]
    if not boxes:
        boxes = [s['bbox'] for inner in norm._walk_blocks(block)
                 for s in norm._spans(inner) if s.get('bbox')]
    return norm._bbox_union(boxes) if boxes else None


def _text(block):
    lines = norm._span_text(block)
    if not lines:
        lines = [str(s['content']) for s in block.get('spans', []) if s.get('content')]
    return norm._join_lines(lines)


def _normalized_line_text(value):
    value = html.unescape(re.sub(r'<[^>]+>', '', str(value)))
    return re.sub(r'\W+', '', value, flags=re.UNICODE).lower()


def _source_line_page(text, box, page_num, pages):
    """Resolve MinerU cross-page lines against the actual PDF text layer."""
    needle = _normalized_line_text(text)
    if not needle or not pages:
        return None
    scores = []
    for candidate in (page_num, page_num + 1):
        if not 1 <= candidate <= len(pages):
            continue
        for source in pages[candidate - 1]['lines']:
            if not _covered(box, [source['bbox']], .45):
                continue
            score = SequenceMatcher(
                None, needle, _normalized_line_text(source['text'])).ratio()
            scores.append((score, candidate))
    best = max(scores, default=(0, None))
    return best[1] if best[0] >= .35 else None


def _text_source_refs(block, page_num, fallback_box, deleted_boxes=None, pages=None):
    """Use actual line geometry because MinerU can join a paragraph across
    columns while leaving its top-level bbox around only the first fragment."""
    boxes = []
    active_page = page_num
    previous_y = fallback_box[1]
    for inner in norm._walk_blocks(block):
        for line in inner.get('lines', []):
            if not line.get('bbox'):
                continue
            if any(str(span.get('content', '')).strip()
                   for span in line.get('spans', [])):
                box = [round(float(x), 3) for x in line['bbox']]
                line_text = ''.join(str(span.get('content', ''))
                                    for span in line.get('spans', []))
                matched_page = _source_line_page(line_text, box, page_num, pages)
                assigned_page = matched_page or active_page
                if matched_page:
                    active_page = matched_page
                elif deleted_boxes and not _covered(box, [fallback_box], .5):
                    wrapped_to_top = box[1] + 100 < previous_y
                    next_page_match = any(_covered(box, [candidate], .5)
                                          for candidate in deleted_boxes.get(page_num + 1, []))
                    if wrapped_to_top and next_page_match:
                        assigned_page = page_num + 1
                        active_page = assigned_page
                    elif active_page != page_num:
                        assigned_page = active_page
                    elif any(_covered(box, [candidate], .5)
                             for candidate in deleted_boxes.get(page_num, [])):
                        assigned_page = page_num
                    elif any(_covered(box, [candidate], .5)
                             for candidate in deleted_boxes.get(page_num + 1, [])):
                        assigned_page = page_num + 1
                        active_page = assigned_page
                boxes.append((assigned_page, box))
                previous_y = box[1]
    if not boxes:
        boxes = [(page_num, fallback_box)]
    return [{'page': assigned_page, 'bbox': box}
            for assigned_page, box in boxes]


def _complete_formula_box(box, page_data):
    """Include a nearby printed equation number in an independent crop."""
    candidates = [list(box)]
    for line in page_data['lines']:
        text = line.get('text', '').strip()
        line_box = line.get('bbox')
        if (line_box and re.fullmatch(r'\(\d{1,3}[a-z]?\)', text, re.I)
                and box[1] - 14 <= (line_box[1] + line_box[3]) / 2 <= box[3] + 14
                and line_box[0] >= box[0]):
            candidates.append(list(line_box))
    return norm._bbox_union(candidates)


def _write(path, data):
    """Idempotent writes only; never silently replace another run's evidence."""
    if path.exists():
        if path.read_bytes() != data:
            raise FileExistsError(f'{path} already contains different data; use a new output directory')
    else:
        path.write_bytes(data)


def _json_write(path, value):
    _write(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


def _crop(page, box, out_dir, name, padding=3, zoom=2):
    import fitz
    rect = (fitz.Rect(box) + (-padding, -padding, padding, padding)) & page.rect
    if rect.is_empty or not rect.is_valid:
        raise ValueError(f'invalid crop box on page {page.number + 1}: {box}')
    rel = f'assets/{name}.png'
    _write(out_dir / rel, page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                                        clip=rect, alpha=False).tobytes('png'))
    return f'![]({rel})', [round(x, 3) for x in rect]


def _page_data(pdf):
    import fitz
    pages = []
    for page in pdf:
        blocks = page.get_text('dict', flags=fitz.TEXTFLAGS_TEXT)['blocks']
        lines = []
        for block in blocks:
            for line in block.get('lines', []):
                line = dict(line)
                line['text'] = ''.join(s.get('text', '') for s in line.get('spans', []))
                if line['text'].strip():
                    lines.append(line)
        pages.append({'width': page.rect.width, 'height': page.rect.height,
                      'blocks': blocks, 'lines': lines,
                      'images': [dict(i) for i in page.get_image_info()],
                      'drawings': [list(d['rect']) for d in page.get_drawings()
                                   if _area(d['rect']) > 1]})
    return pages


def _columns(lines, width):
    """Find two text columns only when both sides have substantial prose."""
    prose = [l for l in lines if len(l['text'].strip()) >= 35]
    left = [l for l in prose if l['bbox'][2] <= width * .54]
    right = [l for l in prose if l['bbox'][0] >= width * .45]
    if len(left) >= 3 and len(right) >= 3:
        gutter = median(l['bbox'][0] for l in right) - median(l['bbox'][2] for l in left)
        if gutter > 2:
            return 2
    return 1


def _style(pages):
    spans = [s for p in pages for l in p['lines'] for s in l['spans'] if s.get('text', '').strip()]
    if spans:
        counts = Counter((s['font'], round(s['size'], 1), s.get('color', 0))
                         for s in spans for _ in range(min(300, len(s['text']))))
        family, size, color = counts.most_common(1)[0][0]
    else:
        family, size, color = 'sans-serif', 10, 0
    body_lines = [l for p in pages for l in p['lines'] if len(l['text']) >= 35]
    # Page margins describe every visible source object, including a short
    # equation or a figure near the page edge. Long prose alone can make a
    # half-used source page look as if it had an enormous bottom margin.
    boxes = [l['bbox'] for p in pages for l in p['lines'] if len(l['text'].strip()) > 2]
    for p in pages:
        page_area = max(1, p['width'] * p['height'])
        boxes.extend(i['bbox'] for i in p['images'] if _area(i['bbox']) < page_area * .75)
    if not boxes:
        boxes = [l['bbox'] for p in pages for l in p['lines']]
    widths = [p['width'] for p in pages]
    heights = [p['height'] for p in pages]
    width, height = median(widths), median(heights)
    # Glyph extents are not line spacing; use the baseline distance of adjacent
    # body lines in the same PDF text block when it is plausible.
    gaps = []
    for p in pages:
        for b in p['blocks']:
            lines = b.get('lines', [])
            for a, c in zip(lines, lines[1:]):
                if not a.get('spans') or not c.get('spans'):
                    continue
                gap = c['spans'][0]['origin'][1] - a['spans'][0]['origin'][1]
                if size * 1.05 <= gap <= size * 2:
                    gaps.append(gap / size)
    heading_sizes = sorted({round(s['size'], 1) for s in spans
                            if s['size'] >= size * 1.15}, reverse=True)[:6]
    headings = {}
    for level, heading_size in enumerate(heading_sizes, 1):
        heading_color = Counter(s.get('color', 0) for s in spans
                                if round(s['size'], 1) == heading_size).most_common(1)[0][0]
        headings[str(level)] = {'font_size_pt': heading_size, 'color': f'#{heading_color:06x}'}
    if not headings:
        headings = {'1': {'font_size_pt': round(size * 1.4, 1), 'color': f'#{color:06x}'}}
    margin_left = min((b[0] for b in boxes), default=width * .08)
    margin_right = min((width - b[2] for b in boxes), default=width * .08)
    margin_top = min((b[1] for b in boxes), default=height * .06)
    margin_bottom = min((height - b[3] for b in boxes), default=height * .06)
    columns = Counter(_columns(p['lines'], p['width']) for p in pages).most_common(1)[0][0]
    return {'schema_version': 1,
            'page': {'width_pt': round(width, 2), 'height_pt': round(height, 2),
                     'margin_left_pt': round(max(0, margin_left), 2),
                     'margin_right_pt': round(max(0, margin_right), 2),
                     'margin_top_pt': round(max(0, margin_top), 2),
                     'margin_bottom_pt': round(max(0, margin_bottom), 2), 'columns': columns},
            'body': {'font_family': family, 'font_size_pt': size,
                     'line_height': round(median(gaps), 3) if gaps else 1.35,
                     'color': f'#{color:06x}'}, 'headings': headings}


def _nearest_style(block, page, style):
    box = block['source_refs'][0]['bbox']
    spans = [s for l in page['lines'] for s in l['spans']
             if _covered(s['bbox'], [box], .5)]
    role = 'body'
    if block['kind'] == 'heading':
        size = max((s['size'] for s in spans), default=style['body']['font_size_pt'] * 1.4)
        level = min(style['headings'], key=lambda k: abs(style['headings'][k]['font_size_pt'] - size))
        block['level'] = int(level)
        role = 'heading-' + level
    elif block['kind'] != 'text':
        role = block['kind']
    block['style_id'] = role


def _append(blocks, kind, text, page, box, **extra):
    block = {'kind': kind, 'text': text, 'source_refs': [{'page': page, 'bbox': box}],
             'style_id': kind if kind != 'text' else 'body',
             'translatable': kind in ('text', 'heading', 'caption', 'footnote')}
    block.update(extra)
    blocks.append(block)
    return block


def _mineru_blocks(pdf, pages, mineru_dir, out_dir, issues):
    middle = norm._find_middle_json(str(mineru_dir))
    if not middle:
        raise FileNotFoundError(f'no MinerU middle.json under {mineru_dir}')
    parsed = json.loads(Path(middle).read_text(encoding='utf-8'))
    infos = parsed.get('pdf_info')
    if not isinstance(infos, list):
        raise ValueError('MinerU middle.json has no pdf_info list')
    blocks, discarded = [], {}
    seen_pages = set()
    deleted_boxes = {}
    for sequence, info in enumerate(infos):
        candidate_page = info.get('page_idx', sequence) + 1
        deleted_boxes[candidate_page] = [
            _box(block) for block in info.get('para_blocks', [])
            if block.get('lines_deleted') and not _text(block) and _box(block)
        ]
    for sequence, info in enumerate(infos):
        index = info.get('page_idx', sequence)
        page_num = index + 1
        if index in seen_pages or not 0 <= index < len(pdf):
            raise ValueError(f'duplicate or invalid MinerU page_idx {index}')
        seen_pages.add(index)
        page = pdf[index]
        page_size = info.get('page_size')
        if page_size and (abs(page_size[0] - page.rect.width) > 2 or abs(page_size[1] - page.rect.height) > 2):
            _issue(issues, 'coordinate_mismatch', 'MinerU coordinates do not match the source page.', page_num,
                   mineru_size=page_size, pdf_size=[page.rect.width, page.rect.height])
        discarded[page_num] = []
        for furniture in info.get('discarded_blocks', []):
            if _box(furniture):
                discarded[page_num].append({'type': furniture.get('type'), 'bbox': _box(furniture),
                                            'text': _text(furniture)})
        ordered = sorted(info.get('para_blocks', []), key=lambda b: b.get('index', 0))
        try:
            groups = norm._figure_groups(ordered)
        except (KeyError, TypeError, ValueError):
            groups = []
            _issue(issues, 'missing_figure_bbox', 'Figure grouping requires valid source coordinates.', page_num)
        starts = {g['items'][0][0]: g for g in groups}
        embedded_figure_text = set()
        for group in groups:
            embedded_figure_text.update(norm._embedded_figure_text_positions(group, ordered))
        for pos, source in enumerate(ordered):
            typ, box = source.get('type'), _box(source)
            if box is None:
                _issue(issues, 'missing_bbox', f'Cannot locate MinerU {typ!r} block.', page_num)
                continue
            if pos in embedded_figure_text:
                continue
            if typ in norm._FIGURE_TYPES:
                if pos not in starts:
                    continue
                group = starts[pos]
                md, crop_box = _crop(page, norm._group_visual_bbox(group, ordered), out_dir,
                                     f'fig-p{page_num:03d}-{pos:03d}', padding=4)
                figure = _append(blocks, 'figure', md, page_num, crop_box)
                for caption in norm._group_captions(group):
                    _append(blocks, 'caption', caption['text'], page_num,
                            caption['bbox'], _caption_target=figure)
            elif typ in ('interline_equation', 'equation', 'formula'):
                box = _complete_formula_box(box, pages[index])
                md, crop_box = _crop(page, box, out_dir, f'eq-p{page_num:03d}-{pos:03d}', padding=3)
                _append(blocks, 'formula', md, page_num, crop_box,
                        latex=norm._equation_latex(source))
            elif typ == 'table':
                body = norm._body_bbox(source)
                md, crop_box = _crop(page, body, out_dir, f'tab-p{page_num:03d}-{pos:03d}')
                table_html = norm._inner_span(source, 'table_body', 'html')
                if table_html:
                    table = _append(blocks, 'table', table_html, page_num, crop_box,
                                    fallback_image=md[4:-1])
                    table['translatable'] = True
                else:
                    table = _append(blocks, 'table', md, page_num, crop_box,
                                    role='source_image_fallback')
                for caption in norm._captions(source):
                    if _text(caption):
                        _append(blocks, 'caption', _text(caption), page_num, _box(caption) or box,
                                _caption_target=table)
                for child in norm._walk_blocks(source):
                    if child.get('type') in ('table_footnote', 'footnote') and _text(child):
                        _append(blocks, 'footnote', _text(child), page_num, _box(child) or box)
            else:
                kinds = {'text': 'text', 'abstract': 'text', 'ref_text': 'text',
                         'title': 'heading', 'list': 'text', 'caption': 'caption',
                         'image_caption': 'caption', 'chart_caption': 'caption',
                         'table_caption': 'caption', 'footnote': 'footnote',
                         'table_footnote': 'footnote'}
                if typ not in kinds:
                    _issue(issues, 'unknown_block_type', f'Unsupported MinerU block type {typ!r}.', page_num)
                    md, crop_box = _crop(page, box, out_dir, f'unknown-p{page_num:03d}-{pos:03d}')
                    _append(blocks, 'figure', md, page_num, crop_box, role='unclassified')
                    continue
                text = _text(source)
                if text:
                    block = _append(blocks, kinds[typ], text, page_num, box)
                    block['source_refs'] = _text_source_refs(
                        source, page_num, box, deleted_boxes, pages)
                elif source.get('lines_deleted'):
                    # MinerU emits an empty duplicate placeholder after joining
                    # a paragraph across columns. Do not accept it by itself:
                    # coverage below still fails if no retained line refs cover
                    # the source text inside this box.
                    continue
                else:
                    _issue(issues, 'empty_text', f'MinerU {typ!r} block has no readable text.', page_num)
    for index in range(len(pdf)):
        if index not in seen_pages:
            _issue(issues, 'missing_page', 'Page missing from MinerU output.', index + 1)
    return blocks, discarded


_CAPTION = re.compile(r'^(?:(?:Extended Data |Supplementary )?Fig(?:ure)?\.?\s*\d+|Table\s*\d+|图\s*\d+|表\s*\d+)', re.I)
_NUMBERED_EQ = re.compile(r'^\s*\(\d{1,3}[a-z]?\)\s*$')


def _geometry_blocks(pdf, pages, out_dir, style, issues, scanning):
    blocks = []
    for index, data in enumerate(pages):
        page, page_num = pdf[index], index + 1
        source_blocks = data['blocks']
        columns = _columns(data['lines'], data['width'])
        if columns > 1:
            _issue(issues, 'reading_order_unverified', 'Multiple columns require a reviewed layout parse.', page_num)
        if len(data['drawings']) > 15:
            _issue(issues, 'vector_layout_unverified', 'Dense vector graphics require figure boundaries from a layout parser.', page_num,
                   drawing_count=len(data['drawings']))
        if not data['lines']:
            _issue(issues, 'ocr_required', 'No readable text; supply an OCR PDF or MinerU parse.', page_num)
        if scanning:
            _issue(issues, 'scan_layout_unverified', 'OCR text is retained; original-page imagery preserves unclassified figures.', page_num)
        ordered = sorted(source_blocks, key=lambda b: (b['bbox'][1], b['bbox'][0]))
        for pos, source in enumerate(ordered):
            lines = source.get('lines', [])
            text = norm._join_lines([''.join(s.get('text', '') for s in l.get('spans', [])) for l in lines])
            if not text.strip():
                continue
            box = list(source['bbox'])
            spans = [s for l in lines for s in l.get('spans', []) if s.get('text', '').strip()]
            size = max((s['size'] for s in spans), default=style['body']['font_size_pt'])
            if _CAPTION.match(text):
                kind = 'caption'
            elif size >= style['body']['font_size_pt'] * 1.15 and len(text) < 240:
                kind = 'heading'
            else:
                kind = 'text'
            math_spans = sum(len(s.get('text', '')) for s in spans if re.search(r'math|symbol', s['font'], re.I))
            if math_spans > len(text) * .3 or any(_NUMBERED_EQ.match(
                    ''.join(s.get('text', '') for s in l.get('spans', []))) for l in lines):
                md, box = _crop(page, box, out_dir, f'eq-p{page_num:03d}-{pos:03d}')
                _append(blocks, 'formula', md, page_num, box)
                _issue(issues, 'formula_boundary_unverified', 'Formula-like text was preserved as a source crop; verify its full boundary.', page_num)
            else:
                _append(blocks, kind, text, page_num, box)
        if not scanning:
            for seq, info in enumerate(data['images']):
                box = list(info['bbox'])
                # Large scan layers must be preserved as a complete source page
                # rather than treated as an independent semantic figure.
                if _area(box) >= data['width'] * data['height'] * .75:
                    _issue(issues, 'scan_layout_unverified', 'A page-sized image needs OCR/layout segmentation.', page_num)
                    continue
                md, box = _crop(page, box, out_dir, f'fig-p{page_num:03d}-{seq:03d}')
                _append(blocks, 'figure', md, page_num, box)
        page_blocks = [b for b in blocks if b['source_refs'][0]['page'] == page_num]
        page_blocks.sort(key=lambda b: (b['source_refs'][0]['bbox'][1], b['source_refs'][0]['bbox'][0]))
        blocks = [b for b in blocks if b['source_refs'][0]['page'] != page_num] + page_blocks
    return blocks, {}


def _join_continuations(blocks):
    """Repair clear lowercase continuations before IDs are assigned.

    Source references are concatenated, so a repaired paragraph still records
    every original page and box. Caption relationships remain untouched.
    """
    merged, pending = [], []
    for block in blocks:
        if block['kind'] == 'figure' and block.get('role') != 'page_fallback':
            pending.append(block)
            continue
        previous = merged[-1] if merged else None
        if (previous and previous['kind'] == block['kind'] == 'text'
                and norm._looks_like_continuation(block['text'])
                and not previous['text'].rstrip().endswith(norm._TERMINAL_PUNCT)):
            if previous['text'].endswith('-') and block['text'][:1].islower():
                previous['text'] = previous['text'][:-1] + block['text'].lstrip()
            else:
                previous['text'] += ' ' + block['text'].lstrip()
            previous['source_refs'].extend(block['source_refs'])
            continue
        merged.extend(pending)
        pending = []
        merged.append(block)
    return merged + pending


def _label_external_captions(blocks):
    """Relabel MinerU captions emitted as top-level text or title blocks."""
    last_visual = None
    for block in blocks:
        page_num = block['source_refs'][0]['page']
        if block['kind'] in ('figure', 'table') and block.get('role') != 'page_fallback':
            last_visual = block
            continue
        if not norm._CAPTION_START.match(block.get('text', '').strip()) or last_visual is None:
            continue
        visual_page = last_visual['source_refs'][0]['page']
        if 0 <= page_num - visual_page <= 1:
            block['kind'] = 'caption'
            block['_caption_target'] = last_visual


def _coverage(pages, blocks, discarded, issues):
    result = []
    for page_num, data in enumerate(pages, 1):
        boxes = [ref['bbox'] for b in blocks if b.get('role') != 'page_fallback'
                 for ref in b['source_refs'] if ref['page'] == page_num]
        ignored = [b['bbox'] for b in discarded.get(page_num, [])]
        missed_lines = [l for l in data['lines'] if not _covered(l['bbox'], boxes + ignored)]
        missed_images = [i for i in data['images']
                         if not _covered(i['bbox'], boxes + ignored)]
        missed_drawings = [b for b in data['drawings'] if not _covered(b, boxes)]
        if missed_lines:
            _issue(issues, 'uncovered_text', f'{len(missed_lines)} source text lines lack a parsed block.', page_num,
                   samples=[{'text': l['text'][:160], 'bbox': list(l['bbox'])} for l in missed_lines[:12]])
        if missed_images:
            _issue(issues, 'uncovered_image', f'{len(missed_images)} source image placements lack a figure.', page_num)
        if missed_drawings:
            _issue(issues, 'uncovered_vector', f'{len(missed_drawings)} vector paths lie outside parsed objects.', page_num,
                   severity='warning', bboxes=missed_drawings[:12])
        result.append({'page': page_num, 'text_lines': len(data['lines']),
                       'covered_text_lines': len(data['lines']) - len(missed_lines),
                       'image_placements': len(data['images']),
                       'covered_image_placements': len(data['images']) - len(missed_images),
                       'vector_paths': len(data['drawings']),
                       'covered_vector_paths': len(data['drawings']) - len(missed_drawings),
                       'discarded_blocks': discarded.get(page_num, [])})
    return {'method': 'source object bounding-box coverage; not semantic completeness certification',
            'pages': result}


def _supplement_uncovered_text(pages, blocks, discarded, issues):
    """Preserve meaningful PDF text lines MinerU omitted from its contract.

    This is deliberately narrow: short panel letters stay inside figure crops;
    only readable lines longer than four characters become translatable source
    fallback blocks, with their exact original geometry retained.
    """
    added = []
    for page_num, data in enumerate(pages, 1):
        boxes = [ref['bbox'] for block in blocks
                 for ref in block.get('source_refs', []) if ref['page'] == page_num]
        ignored = [item['bbox'] for item in discarded.get(page_num, [])]
        missing = [line for line in data['lines']
                   if len(line['text'].strip()) > 4
                   and not _covered(line['bbox'], boxes + ignored)]
        for line in missing:
            text = line['text'].strip()
            box = [round(float(x), 3) for x in line['bbox']]
            kind = 'caption' if _CAPTION.match(text) else 'text'
            block = {'kind': kind, 'text': text,
                     'source_refs': [{'page': page_num, 'bbox': box}],
                     'style_id': 'source-text-fallback', 'translatable': True,
                     'role': 'source_text_fallback'}
            insert_at = next((index for index, existing in enumerate(blocks)
                              if existing['source_refs'][0]['page'] == page_num
                              and existing['source_refs'][0]['bbox'][1] > box[1]),
                             None)
            if insert_at is None:
                insert_at = next((index for index in range(len(blocks) - 1, -1, -1)
                                  if blocks[index]['source_refs'][0]['page'] == page_num),
                                 len(blocks) - 1) + 1
            blocks.insert(insert_at, block)
            boxes.append(box)
            added.append({'page': page_num, 'text': text, 'bbox': box})
    if added:
        _issue(issues, 'source_text_supplement',
               f'{len(added)} meaningful source lines omitted by MinerU were preserved from the PDF text layer.',
               severity='warning', items=added)


def build_document(pdf_path, out_dir, mineru_dir=None, ocr_pdf=None):
    """Write doc.json, style.json and original-PDF assets; return the document.

    Errors in ``doc['issues']`` mean parsing has not passed acceptance. All
    formula/figure/table bodies are protected original-source crops. A supplied
    OCR PDF contributes text coordinates only, never replacement image pixels.
    """
    import fitz
    pdf_path, out_dir = Path(pdf_path).resolve(), Path(out_dir).resolve()
    source_hash = sha256(pdf_path.read_bytes()).hexdigest()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'assets').mkdir(exist_ok=True)
    issues = []
    with fitz.open(pdf_path) as pdf:
        if not len(pdf):
            raise ValueError('PDF has no pages')
        original_pages = _page_data(pdf)
        pages = original_pages
        if ocr_pdf:
            with fitz.open(ocr_pdf) as ocr:
                if len(ocr) != len(pdf) or any(abs(a.rect.width - b.rect.width) > 1 or
                                              abs(a.rect.height - b.rect.height) > 1
                                              for a, b in zip(pdf, ocr)):
                    raise ValueError('OCR PDF page count and dimensions must match the original PDF')
                pages = _page_data(ocr)
            _issue(issues, 'ocr_text_source', 'Text geometry comes from OCR; crops come from the original PDF.', severity='warning')
        style = _style(original_pages if any(p['lines'] for p in original_pages) else pages)
        if mineru_dir:
            blocks, discarded = _mineru_blocks(pdf, pages, mineru_dir, out_dir, issues)
            parser = 'mineru'
        else:
            blocks, discarded = _geometry_blocks(pdf, pages, out_dir, style, issues, bool(ocr_pdf))
            parser = 'pymupdf-geometry'
        if mineru_dir:
            _supplement_uncovered_text(pages, blocks, discarded, issues)
        for block in blocks:
            _nearest_style(block, original_pages[block['source_refs'][0]['page'] - 1], style)
        blocks = _join_continuations(blocks)
        _label_external_captions(blocks)
        coverage = _coverage(pages, blocks, discarded, issues)
        # Source scans still need their original visual placements audited.
        if ocr_pdf:
            coverage['original_pages'] = _coverage(original_pages, blocks, discarded, []).get('pages')
        fallback_pages = sorted({i['page'] for i in issues if i.get('page') and i['severity'] == 'error'})
        for page_num in fallback_pages:
            md, box = _crop(pdf[page_num - 1], list(pdf[page_num - 1].rect), out_dir,
                            f'page-{page_num:03d}', padding=0, zoom=1.5)
            _append(blocks, 'figure', md, page_num, box, role='page_fallback')
        # Resolve explicit relationships only after continuation repair. External
        # caption blocks attach by nearest preceding figure on the same page.
        for index, block in enumerate(blocks, 1):
            block['id'] = f'b{index:06d}'
        last_figure = {}
        for block in blocks:
            page_num = block['source_refs'][0]['page']
            if block['kind'] in ('figure', 'table') and block.get('role') != 'page_fallback':
                last_figure[page_num] = block['id']
            if block['kind'] == 'caption':
                target = block.pop('_caption_target', None)
                target_id = target['id'] if target else last_figure.get(page_num)
                if target_id:
                    block['caption_of'] = target_id
                else:
                    _issue(issues, 'unlinked_caption', 'Caption has no identifiable figure/table on its page.', page_num)
        frozen = [{k: b[k] for k in ('id', 'kind', 'text', 'source_refs', 'level',
                                    'caption_of', 'translatable', 'fallback_image') if k in b} for b in blocks]
        document = {'schema_version': 1, 'doc_id': source_hash,
                    'structure_hash': _digest(frozen),
                    'source': {'path': str(pdf_path), 'sha256': source_hash},
                    'parser': parser, 'blocks': blocks, 'issues': issues, 'coverage': coverage}
        if ocr_pdf:
            document['source']['ocr_path'] = str(Path(ocr_pdf).resolve())
        _json_write(out_dir / 'doc.json', document)
        _json_write(out_dir / 'style.json', style)
        return document


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf')
    parser.add_argument('--out-dir', required=True)
    parser.add_argument('--mineru-dir')
    parser.add_argument('--ocr-pdf')
    args = parser.parse_args()
    result = build_document(args.pdf, args.out_dir, args.mineru_dir, args.ocr_pdf)
    print(json.dumps({'blocks': len(result['blocks']), 'issues': result['issues']}, ensure_ascii=False))
    raise SystemExit(2 if any(i['severity'] == 'error' for i in result['issues']) else 0)
