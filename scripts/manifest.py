"""
manifest.py - Manifest management for chunk tracking and merge validation.
"""

import os
import json
import hashlib
import re
import tempfile
from pathlib import Path


BLOCK_MARKER_RE = re.compile(r'^<!--\s*tb:(b\d+)\s*-->[ \t]*$', re.MULTILINE)


def parse_block_markers(text):
    """Return ordered (block_id, markdown) pairs; reject unowned content."""
    matches = list(BLOCK_MARKER_RE.finditer(text))
    if not matches or text[:matches[0].start()].strip():
        raise ValueError('Expected block markers before all content')
    blocks = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        blocks.append((match.group(1), text[match.end():end].strip()))
    ids = [block_id for block_id, _ in blocks]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate block ids')
    return blocks


def _protected_objects(text):
    """Structural objects that a translation must retain exactly."""
    return {
        'math': re.findall(r'(?<!\\)\$\$[\s\S]*?(?<!\\)\$\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]|(?<![\\$])\$(?!\$)[^\n$]*?(?<!\\)\$', text),
        'destinations': re.findall(r'!?\[[^\]\n]*\]\(([^)\n]+)\)', text),
        'html_paths': re.findall(r'\b(?:src|href)\s*=\s*["\']([^"\']+)["\']', text, re.I),
        'footnotes': re.findall(r'\[\^[^\]]+\]', text),
        'code': re.findall(r'```[\s\S]*?```|`[^`\n]+`', text),
        'mathml': re.findall(r'<math\b[\s\S]*?</math>', text, re.I),
    }


def validate_translation(source_text, output_text, blocks=None):
    """Return structural errors for a marked translation (no semantic claims).

    ``blocks`` accepts doc.json's block list or an id-to-block mapping.
    """
    errors = []
    try:
        source = parse_block_markers(source_text)
        output = parse_block_markers(output_text)
    except ValueError as exc:
        return [str(exc)]
    if [bid for bid, _ in source] != [bid for bid, _ in output]:
        return ['Output block ids/order differ from source (missing, duplicate or reordered blocks)']
    by_id = blocks if isinstance(blocks, dict) else {b['id']: b for b in (blocks or [])}
    for (block_id, original), (_, translated) in zip(source, output):
        descriptor = by_id.get(block_id, {})
        if original and not translated:
            errors.append(f'{block_id}: empty translated block')
        if descriptor.get('translatable') is False and original != translated:
            errors.append(f'{block_id}: non-translatable object changed')
        for kind, values in _protected_objects(original).items():
            if values != _protected_objects(translated)[kind]:
                errors.append(f'{block_id}: protected {kind} changed')
        original_heading = re.match(r'^(#{1,6})\s', original)
        translated_heading = re.match(r'^(#{1,6})\s', translated)
        if ((original_heading.group(1) if original_heading else None) !=
                (translated_heading.group(1) if translated_heading else None)):
            errors.append(f'{block_id}: heading level changed')
        if descriptor.get('kind') == 'table':
            if '<table' in original.lower():
                shape = lambda value: re.findall(r'<[^>]+>', value)
            else:
                shape = lambda value: [len(re.findall(r'(?<!\\)\|', line))
                                       for line in value.splitlines() if line.strip()]
            if shape(original) != shape(translated):
                errors.append(f'{block_id}: table row/column structure changed')
    return errors


def _atomic_json(path, data):
    fd, temporary = tempfile.mkstemp(prefix='.manifest.', suffix='.json', dir=str(Path(path).parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def file_hash(filepath):
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for block in iter(lambda: f.read(8192), b''):
            h.update(block)
    return h.hexdigest()


def read_output_text(filepath):
    """Read a translated output chunk as UTF-8 text.

    Returns None when the file cannot be read or decoded. Callers treat None
    the same as blank content: the chunk has no usable translation.
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def create_manifest(temp_dir, chunk_files, source_md_path, *, expected_block_ids=None,
                    split_contract=None, doc_path=None, expected_chunk_files=None):
    """Create manifest.json after splitting.

    Args:
        temp_dir: temp directory path
        chunk_files: list of chunk filenames (e.g. ['chunk0001.md', ...])
        source_md_path: path to the source input.md
    """
    chunk_files = list(chunk_files)
    if len(chunk_files) != len(set(chunk_files)) or not chunk_files:
        raise ValueError('Chunk list must be nonempty and unique')
    if any(not re.fullmatch(r'chunk\d+\.md', name) for name in chunk_files):
        raise ValueError('Invalid source chunk filename')
    if any(not os.path.isfile(os.path.join(temp_dir, name)) for name in chunk_files):
        raise ValueError('Cannot publish manifest: source chunks are missing')
    source_hash = file_hash(source_md_path) if os.path.exists(source_md_path) else ""
    doc_path = Path(doc_path) if doc_path else Path(temp_dir) / 'doc.json'
    doc = json.loads(doc_path.read_text(encoding='utf-8')) if doc_path.exists() else None
    version2 = doc is not None or expected_block_ids is not None or split_contract is not None or expected_chunk_files is not None

    chunks = []
    for order, filename in enumerate(chunk_files, 1):
        filepath = os.path.join(temp_dir, filename)
        # Derive output filename: chunk0001.md -> output_chunk0001.md
        output_filename = f"output_{filename}"
        chunk_id = os.path.splitext(filename)[0]  # e.g. "chunk0001"

        chunks.append({
            "id": chunk_id,
            "order": order,
            "source_file": filename,
            "source_hash": file_hash(filepath) if os.path.exists(filepath) else "",
            "output_file": output_filename,
        })

    manifest = {
        "chunk_count": len(chunks),
        "source_hash": source_hash,
        "chunks": chunks,
    }

    if version2:
        if not source_hash:
            raise ValueError('Cannot publish v2 manifest without input markdown')
        expected_files = list(expected_chunk_files) if expected_chunk_files is not None else chunk_files
        if chunk_files != expected_files:
            raise ValueError('Incomplete split: source chunks differ from expected chunk list')
        manifest.update({'version': 2, 'split_complete': True,
                         'source_file': os.path.relpath(source_md_path, temp_dir),
                         'expected_chunk_files': expected_files,
                         'split_contract': split_contract or {}})
        if doc is not None:
            doc_ids = [block['id'] for block in doc['blocks']]
            if expected_block_ids is not None and list(expected_block_ids) != doc_ids:
                raise ValueError('Expected block ids differ from doc.json')
            expected_block_ids = doc_ids
            manifest['doc_file'] = os.path.relpath(doc_path, temp_dir)
            manifest['doc_hash'] = file_hash(doc_path)
            manifest['structure_hash'] = doc.get('structure_hash')
        if expected_block_ids is not None:
            manifest['expected_block_ids'] = list(expected_block_ids)
            for chunk in chunks:
                content = Path(temp_dir, chunk['source_file']).read_text(encoding='utf-8')
                chunk['block_ids'] = [bid for bid, _ in parse_block_markers(content)]
        errors = validate_manifest(temp_dir, manifest)
        if errors:
            raise ValueError('Cannot publish incomplete manifest: ' + '; '.join(errors))

    manifest_path = os.path.join(temp_dir, "manifest.json")
    _atomic_json(manifest_path, manifest)

    print(f"Created manifest.json ({len(chunks)} chunks)")
    return manifest


def load_manifest(temp_dir):
    """Load manifest.json from temp_dir. Returns None if not found."""
    manifest_path = os.path.join(temp_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        return None
    with open(manifest_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def validate_manifest(temp_dir, manifest=None):
    """Validate source coverage without needing any translated output."""
    manifest = manifest if manifest is not None else load_manifest(temp_dir)
    if manifest is None:
        return ['Missing manifest.json']
    errors = []
    chunks = manifest.get('chunks', [])
    if not chunks or manifest.get('chunk_count') != len(chunks):
        errors.append('Manifest chunk_count is empty or inconsistent')
    for field in ('id', 'order', 'source_file', 'output_file'):
        values = [chunk.get(field) for chunk in chunks]
        if None in values or len(values) != len(set(values)):
            errors.append(f'Manifest has missing or duplicate {field}')
    if manifest.get('version', 1) not in (1, 2):
        errors.append('Unsupported manifest version')
    if manifest.get('version') != 2:
        return errors
    if manifest.get('split_complete') is not True:
        errors.append('Split was not marked complete')
    if [c.get('order') for c in chunks] != list(range(1, len(chunks) + 1)):
        errors.append('Manifest chunk ordering is invalid')
    if [c.get('source_file') for c in chunks] != manifest.get('expected_chunk_files'):
        errors.append('Manifest does not cover expected chunks')
    source = Path(temp_dir) / manifest.get('source_file', 'input.md')
    if not source.is_file() or file_hash(source) != manifest.get('source_hash'):
        errors.append('Input markdown changed since splitting')
    doc = None
    if manifest.get('doc_file'):
        doc_path = Path(temp_dir) / manifest['doc_file']
        if not doc_path.is_file() or file_hash(doc_path) != manifest.get('doc_hash'):
            errors.append('doc.json changed since splitting')
        else:
            doc = json.loads(doc_path.read_text(encoding='utf-8'))
    expected_ids = manifest.get('expected_block_ids')
    if expected_ids is not None:
        if not expected_ids or len(expected_ids) != len(set(expected_ids)):
            errors.append('Expected block ids must be nonempty and unique')
        flattened = [bid for chunk in chunks for bid in chunk.get('block_ids', [])]
        if flattened != expected_ids:
            errors.append('Manifest block coverage is missing, duplicated or reordered')
        if doc and [block['id'] for block in doc['blocks']] != expected_ids:
            errors.append('Manifest does not cover all doc.json blocks in order')
        if source.is_file():
            try:
                if [bid for bid, _ in parse_block_markers(source.read_text(encoding='utf-8'))] != expected_ids:
                    errors.append('Input markdown block coverage differs from manifest')
            except ValueError as exc:
                errors.append(str(exc))
    doc_blocks = {b['id']: b for b in doc['blocks']} if doc else {}
    for chunk in chunks:
        path = Path(temp_dir) / chunk['source_file']
        if not path.is_file() or file_hash(path) != chunk.get('source_hash'):
            errors.append(f"Source changed or missing: {chunk['source_file']}")
            continue
        if expected_ids is not None:
            try:
                marked = parse_block_markers(path.read_text(encoding='utf-8'))
                if [bid for bid, _ in marked] != chunk.get('block_ids'):
                    errors.append(f"Source block ids differ: {chunk['source_file']}")
                for bid, body in marked:
                    if bid in doc_blocks and body != doc_blocks[bid]['text'].strip():
                        errors.append(f'{bid}: source chunk differs from doc.json text')
            except ValueError as exc:
                errors.append(f'{path.name}: {exc}')
    return errors


def validate_for_merge(temp_dir):
    """Validate that all chunks have been translated before merging.

    Returns (ok, ordered_output_files, warnings) where:
        ok: True if merge can proceed
        ordered_output_files: list of output file paths in order
        warnings: list of warning strings
    """
    manifest = load_manifest(temp_dir)
    if manifest is None:
        # No manifest — fall back to legacy glob-based merge
        return True, None, ["No manifest.json found, using legacy merge"]

    errors = validate_manifest(temp_dir, manifest)
    warnings = []
    ordered_output_files = []
    doc_blocks = []
    if manifest.get('doc_file') and not errors:
        doc_blocks = json.loads(Path(temp_dir, manifest['doc_file']).read_text(encoding='utf-8'))['blocks']
    if manifest.get('version') != 2:
        warnings.append('Legacy manifest: complete source coverage is unverified')

    for chunk in sorted(manifest["chunks"], key=lambda c: c["order"]):
        output_path = os.path.join(temp_dir, chunk["output_file"])
        source_path = os.path.join(temp_dir, chunk["source_file"])

        # Check source file exists — reject outputs without source chunks
        if not os.path.exists(source_path):
            errors.append(
                f"Missing source: {chunk['source_file']} (chunk {chunk['id']}) — "
                f"cannot verify output integrity without source chunk"
            )
            continue

        # Check source hash matches — detect stale outputs from changed sources
        if chunk.get("source_hash"):
            current_hash = file_hash(source_path)
            if current_hash != chunk["source_hash"]:
                errors.append(
                    f"Source changed since splitting: {chunk['source_file']} "
                    f"(chunk {chunk['id']}). "
                    f"Expected hash {chunk['source_hash'][:12]}..., "
                    f"got {current_hash[:12]}... — "
                    f"delete output and re-translate, or re-run convert.py to re-split"
                )
                continue

        # Check output exists
        if not os.path.exists(output_path):
            errors.append(f"Missing output: {chunk['output_file']} (chunk {chunk['id']})")
            continue

        # Check non-empty. Whitespace-only files have bytes on disk but merge
        # to nothing after strip(), silently dropping the chunk's content —
        # treat them exactly like empty files.
        output_size = os.path.getsize(output_path)
        if output_size == 0:
            errors.append(f"Empty output: {chunk['output_file']} (chunk {chunk['id']})")
            continue
        output_text = read_output_text(output_path)
        if output_text is None:
            errors.append(
                f"Unreadable output: {chunk['output_file']} (chunk {chunk['id']}) — "
                f"not valid UTF-8 text"
            )
            continue
        if not output_text.strip():
            errors.append(
                f"Blank output: {chunk['output_file']} (chunk {chunk['id']}) — "
                f"whitespace-only content would be silently dropped on merge"
            )
            continue

        if manifest.get('expected_block_ids') is not None:
            original = Path(source_path).read_text(encoding='utf-8')
            errors.extend(f"{chunk['id']}: {error}" for error in
                          validate_translation(original, output_text, doc_blocks))

        # Check abnormally short
        if os.path.exists(source_path):
            source_size = os.path.getsize(source_path)
            if source_size > 0 and output_size < source_size * 0.1:
                warnings.append(
                    f"Suspiciously short: {chunk['output_file']} "
                    f"({output_size} bytes vs source {source_size} bytes)"
                )

        ordered_output_files.append(output_path)

    state_path = Path(temp_dir) / 'run_state.json'
    if state_path.exists():
        # New initialized runs must finish the feedback/correction loop before
        # either renderer can publish. Old uninitialized libraries stay usable.
        import run_state
        state = run_state.load_run_state(temp_dir)
        if state.get('translation_contract'):
            try:
                run_state.assert_build_ready(temp_dir)
            except ValueError as exc:
                errors.append(str(exc))

    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        return False, None, warnings

    for w in warnings:
        print(f"WARNING: {w}")

    return True, ordered_output_files, warnings
