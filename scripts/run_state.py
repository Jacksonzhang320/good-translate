"""
run_state.py - Selective re-translation state for good-translate.

The state file records what glossary and source/output hashes were used for
each translated chunk. Future runs can then decide which chunks need actual
re-translation after glossary or source changes, and which existing outputs
only need their state recorded.
"""

import argparse
import copy
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import glossary as glossary_mod
from manifest import file_hash, load_manifest, read_output_text


RUN_STATE_VERSION = 2
RUN_STATE_FILE = "run_state.json"
DEFAULT_PROMPT_VERSION = "tb-v2"


def _run_state_path(temp_dir):
    return os.path.join(temp_dir, RUN_STATE_FILE)


def _empty_state():
    return {"version": RUN_STATE_VERSION, "chunks": {}}


def load_run_state(temp_dir):
    path = _run_state_path(temp_dir)
    if not os.path.exists(path):
        return _empty_state()
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if data.get("version") not in (1, RUN_STATE_VERSION):
        raise ValueError(
            f"run_state.json version mismatch: expected {RUN_STATE_VERSION}, "
            f"got {data.get('version')!r}"
        )
    chunks = data.get("chunks")
    if not isinstance(chunks, dict):
        raise ValueError("run_state.json field 'chunks' must be an object")
    if data.get('version') == 1:
        # Preserve old evidence, but never claim knowledge of its historical
        # language, instructions or prompt. Migration remains explicit on disk.
        data['legacy_version'] = 1
        data['version'] = RUN_STATE_VERSION
        for record in chunks.values():
            record['provenance'] = 'legacy_unverified'
    return data


def save_run_state(temp_dir, state):
    path = _run_state_path(temp_dir)
    os.makedirs(temp_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix='.run_state.', suffix='.json', dir=temp_dir)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write('\n')
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _chunk_entries_from_manifest(temp_dir):
    manifest = load_manifest(temp_dir)
    if manifest:
        entries = []
        for chunk in sorted(manifest.get("chunks", []), key=lambda c: c.get("order", 0)):
            entries.append({
                "id": chunk["id"],
                "source_file": chunk["source_file"],
                "output_file": chunk["output_file"],
                "manifest_source_hash": chunk.get("source_hash", ""),
                "order": chunk.get("order", 0),
            })
        return entries

    temp_path = Path(temp_dir)
    source_files = sorted(
        p for p in temp_path.glob('chunk*.md')
        if not p.name.startswith('output_')
    )
    return [
        {
            "id": p.stem,
            "source_file": p.name,
            "output_file": f"output_{p.name}",
            "manifest_source_hash": "",
            "order": i,
        }
        for i, p in enumerate(source_files, 1)
    ]


def _load_glossary(temp_dir):
    path = os.path.join(temp_dir, 'glossary.json')
    if not os.path.exists(path):
        return None, "", [], {}
    glossary = glossary_mod.load_glossary(path)
    glossary_hash = glossary_mod.glossary_hash(glossary)
    terms = glossary.get('terms', [])
    term_by_id = {t.get('id', t.get('source')): t for t in terms}
    return glossary, glossary_hash, terms, term_by_id


def _selected_terms_for_chunk(glossary, source_path):
    if glossary is None or not os.path.exists(source_path):
        return []
    text = Path(source_path).read_text(encoding='utf-8')
    return glossary_mod.select_terms_for_chunk(glossary, text)


def _term_ids_and_hashes(terms):
    ids = []
    hashes = {}
    for term in terms:
        term_id = term.get('id', term.get('source'))
        ids.append(term_id)
        hashes[term_id] = glossary_mod.term_hash(term)
    return ids, hashes


def _relevant_term_hashes(terms, source_text):
    """Return only glossary entries whose source or alias occurs in a chunk.

    The prompt also carries a global high-frequency glossary tail. Adding an
    unrelated global term changes that tail but cannot change this chunk's
    translation. Retranslation is therefore keyed to terms that actually occur
    in the source, including newly added aliases.
    """
    return {
        term.get('id', term.get('source')): glossary_mod.term_hash(term)
        for term in terms
        if glossary_mod._term_appears_in_text(term, source_text)
    }


def _now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def translation_contract(target_lang, custom_instructions='', prompt_version=DEFAULT_PROMPT_VERSION):
    """Build the exact, JSON-serializable prompt contract; never strip prose."""
    if not isinstance(target_lang, str) or not target_lang:
        raise ValueError('target_lang must be a nonempty string')
    if not isinstance(custom_instructions, str):
        raise ValueError('custom_instructions must be a string')
    if not isinstance(prompt_version, str) or not prompt_version:
        raise ValueError('prompt_version must be a nonempty string')
    return {'target_lang': target_lang, 'custom_instructions': custom_instructions,
            'prompt_version': prompt_version}


def _contract_hash(contract):
    return hashlib.sha256(json.dumps(contract, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _current_contract(temp_dir, state):
    if state.get('translation_contract'):
        return state['translation_contract']
    # Legacy CLI calls still notice config language changes, but the contract
    # is only an observation at record time, never a verified dispatch history.
    config = Path(temp_dir) / 'config.txt'
    if config.exists():
        values = dict(line.split('=', 1) for line in config.read_text(encoding='utf-8').splitlines()
                      if '=' in line)
        if values.get('output_lang'):
            return translation_contract(values['output_lang'], values.get('custom_instructions', ''),
                                        values.get('prompt_version', DEFAULT_PROMPT_VERSION))
    return None


def init_run(temp_dir, target_lang, custom_instructions='', prompt_version=DEFAULT_PROMPT_VERSION):
    """Set the requested contract without rewriting any chunk's past record."""
    state = load_run_state(temp_dir)
    contract = translation_contract(target_lang, custom_instructions, prompt_version)
    if state.get('translation_contract') != contract:
        state.pop('frozen_glossary_hash', None)
        state.pop('frozen_at', None)
    state['translation_contract'] = contract
    state['contract_hash'] = _contract_hash(contract)
    save_run_state(temp_dir, state)
    return state


def dispatch_chunks(temp_dir, chunk_ids):
    """Persist prompt/source/glossary snapshots BEFORE sending work to agents."""
    state = load_run_state(temp_dir)
    contract = state.get('translation_contract')
    if contract is None:
        raise ValueError('init_run must precede dispatch')
    entries = {entry['id']: entry for entry in _chunk_entries_from_manifest(temp_dir)}
    glossary, glossary_hash, _, _ = _load_glossary(temp_dir)
    if 'frozen_glossary_hash' in state and state['frozen_glossary_hash'] != glossary_hash:
        raise ValueError('Frozen glossary changed; freeze the final glossary again before correction')
    snapshots = {}
    pending = state.setdefault('dispatches', {})
    for chunk_id in chunk_ids:
        if chunk_id not in entries:
            raise ValueError(f'Unknown chunk id {chunk_id!r}')
        if chunk_id in pending:
            raise ValueError(f'Chunk {chunk_id} already has a pending dispatch; record or cancel it first')
        entry = entries[chunk_id]
        source_path = os.path.join(temp_dir, entry['source_file'])
        selected = _selected_terms_for_chunk(glossary, source_path)
        entity_ids, entity_hashes = _term_ids_and_hashes(selected)
        snapshot = {
            'source_file': entry['source_file'], 'output_file': entry['output_file'],
            'source_hash': file_hash(source_path),
            'translation_contract': copy.deepcopy(contract), 'contract_hash': _contract_hash(contract),
            'glossary_version_used': glossary_hash, 'entity_ids_used': entity_ids,
            'entity_hashes_used': entity_hashes, 'terms_used': copy.deepcopy(selected),
            'term_table': glossary_mod.format_terms_for_prompt(selected),
            'dispatched_at': _now_utc(), 'provenance': 'dispatched',
        }
        pending[chunk_id] = snapshot
        snapshots[chunk_id] = copy.deepcopy(snapshot)
    save_run_state(temp_dir, state)
    return snapshots


def cancel_dispatch(temp_dir, chunk_ids):
    """Cancel pending attempts without altering completed output provenance."""
    state = load_run_state(temp_dir)
    for chunk_id in chunk_ids:
        state.setdefault('dispatches', {}).pop(chunk_id, None)
    save_run_state(temp_dir, state)


def freeze_glossary(temp_dir):
    state = load_run_state(temp_dir)
    if not state.get('translation_contract'):
        raise ValueError('init_run must precede glossary freeze')
    _, glossary_hash, _, _ = _load_glossary(temp_dir)
    state['frozen_glossary_hash'] = glossary_hash
    state['frozen_at'] = _now_utc()
    save_run_state(temp_dir, state)
    return glossary_hash


def assert_build_ready(temp_dir):
    """Require a frozen glossary and verified, current output for every chunk."""
    state = load_run_state(temp_dir)
    if not state.get('translation_contract') or 'frozen_glossary_hash' not in state:
        raise ValueError('Build requires an initialized run and a frozen glossary')
    _, glossary_hash, _, _ = _load_glossary(temp_dir)
    if state['frozen_glossary_hash'] != glossary_hash:
        raise ValueError('Frozen glossary changed')
    planned = plan(temp_dir, require_verified=True)
    if planned['translation_chunk_ids'] or planned['record_only_chunk_ids'] or state.get('dispatches'):
        raise ValueError('Build blocked: translation/record/pending dispatch queue is not empty')
    if not planned['chunks']:
        raise ValueError('Build blocked: no source chunks')
    return planned


def build_chunk_record(temp_dir, chunk_id):
    entries = {entry["id"]: entry for entry in _chunk_entries_from_manifest(temp_dir)}
    if chunk_id not in entries:
        raise ValueError(f"Unknown chunk id {chunk_id!r}")

    entry = entries[chunk_id]
    source_path = os.path.join(temp_dir, entry["source_file"])
    output_path = os.path.join(temp_dir, entry["output_file"])
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Source chunk not found: {source_path}")
    if not os.path.exists(output_path):
        raise FileNotFoundError(f"Output chunk not found: {output_path}")
    if os.path.getsize(output_path) == 0:
        raise ValueError(f"Output chunk is empty: {output_path}")
    output_text = read_output_text(output_path)
    if output_text is None:
        raise ValueError(f"Output chunk is not readable UTF-8 text: {output_path}")
    if not output_text.strip():
        raise ValueError(f"Output chunk is blank (whitespace-only): {output_path}")

    glossary, glossary_hash, _, _ = _load_glossary(temp_dir)
    selected_terms = _selected_terms_for_chunk(glossary, source_path)
    entity_ids, entity_hashes = _term_ids_and_hashes(selected_terms)

    return {
        "source_file": entry["source_file"],
        "output_file": entry["output_file"],
        "source_hash": file_hash(source_path),
        "output_hash": file_hash(output_path),
        "glossary_version_used": glossary_hash,
        "entity_ids_used": entity_ids,
        "entity_hashes_used": entity_hashes,
        "updated_at": _now_utc(),
    }


def record_chunks(temp_dir, chunk_ids, provenance=None):
    state = load_run_state(temp_dir)
    recorded = []
    for chunk_id in chunk_ids:
        current = build_chunk_record(temp_dir, chunk_id)
        snapshot = state.get('dispatches', {}).get(chunk_id)
        if snapshot is not None:
            if snapshot['source_hash'] != current['source_hash']:
                raise ValueError(f'Source changed after dispatch for {chunk_id}')
            manifest = load_manifest(temp_dir)
            if manifest and manifest.get('expected_block_ids') is not None:
                from manifest import validate_translation
                blocks = None
                if manifest.get('doc_file'):
                    blocks = json.loads(Path(temp_dir, manifest['doc_file']).read_text(encoding='utf-8'))['blocks']
                errors = validate_translation(
                    Path(temp_dir, current['source_file']).read_text(encoding='utf-8'),
                    read_output_text(Path(temp_dir, current['output_file'])), blocks)
                if errors:
                    raise ValueError(f'Invalid translation {chunk_id}: ' + '; '.join(errors))
            # Glossary may have changed while work ran. Record what was sent,
            # then the planner will request a correction against final terms.
            record = copy.deepcopy(snapshot)
            record['output_hash'] = current['output_hash']
            record['updated_at'] = _now_utc()
            record['provenance'] = 'verified_dispatch'
            state['dispatches'].pop(chunk_id)
        elif provenance == 'bypassed_reference':
            record = current
            record['provenance'] = 'bypassed_reference'
            observed = _current_contract(temp_dir, state)
            if observed:
                record['contract_hash'] = _contract_hash(observed)
                record['observed_contract'] = observed
                record['observed_contract_hash'] = _contract_hash(observed)
        elif state.get('translation_contract'):
            raise ValueError(f'No dispatch snapshot for {chunk_id}; cannot verify prompt provenance')
        else:
            record = current
            record['provenance'] = 'legacy_unverified'
            observed = _current_contract(temp_dir, state)
            if observed:
                record['observed_contract'] = observed
                record['observed_contract_hash'] = _contract_hash(observed)
        state["chunks"][chunk_id] = record
        recorded.append(chunk_id)
    save_run_state(temp_dir, state)
    return recorded


def _reason(item, code, detail=None):
    if detail is None:
        item["reasons"].append(code)
    else:
        item["reasons"].append({"code": code, "detail": detail})


def plan(temp_dir, retranslate_untracked=False, require_verified=None, retranslate_on_term_drift=None):
    state = load_run_state(temp_dir)
    glossary, glossary_hash, _, _ = _load_glossary(temp_dir)
    contract = _current_contract(temp_dir, state)
    if require_verified is None:
        require_verified = bool(state.get('translation_contract'))
    if retranslate_on_term_drift is None:
        retranslate_on_term_drift = os.environ.get("TB_RETRANSLATE_ON_TERM_DRIFT", "0") != "0"

    result = {
        "temp_dir": temp_dir,
        "glossary_hash": glossary_hash,
        "translation_contract": contract,
        "require_verified": require_verified,
        "unverified_chunk_ids": [],
        "translation_chunk_ids": [],
        "record_only_chunk_ids": [],
        "unchanged_chunk_ids": [],
        "chunks": [],
        "decision_rules": [
            "missing_output_or_empty_output",
            "blank_or_unreadable_output",
            "manifest_source_hash_changed",
            "untracked_existing_output",
            "source_hash_changed_since_record",
            "glossary_term_selection_or_term_hash_changed",
        ],
        "record_update_rules": [
            "output_hash_changed_since_record",
        ],
    }

    entries = _chunk_entries_from_manifest(temp_dir)
    records = state.get("chunks", {})

    for entry in entries:
        chunk_id = entry["id"]
        source_path = os.path.join(temp_dir, entry["source_file"])
        output_path = os.path.join(temp_dir, entry["output_file"])
        item = {
            "chunk_id": chunk_id,
            "source_file": entry["source_file"],
            "output_file": entry["output_file"],
            "action": "unchanged",
            "reasons": [],
        }

        if not os.path.exists(output_path):
            item["action"] = "translate"
            _reason(item, "missing_output")
        elif os.path.getsize(output_path) == 0:
            item["action"] = "translate"
            _reason(item, "empty_output")
        else:
            output_text = read_output_text(output_path)
            if output_text is None:
                item["action"] = "translate"
                _reason(item, "unreadable_output")
            elif not output_text.strip():
                item["action"] = "translate"
                _reason(item, "blank_output")

        current_source_hash = file_hash(source_path) if os.path.exists(source_path) else ""
        manifest_source_hash = entry.get("manifest_source_hash", "")
        if item["action"] == "unchanged" and manifest_source_hash:
            if current_source_hash != manifest_source_hash:
                item["action"] = "translate"
                _reason(item, "manifest_source_hash_changed")

        record = records.get(chunk_id)
        if item["action"] == "unchanged" and record is None:
            if retranslate_untracked or require_verified:
                item["action"] = "translate"
                _reason(item, "untracked_existing_output")
            else:
                item["action"] = "record"
                _reason(item, "untracked_existing_output")

        if item["action"] == "unchanged" and record is not None:
            if require_verified and record.get('provenance') not in ('verified_dispatch', 'bypassed_reference'):
                item['action'] = 'translate'
                _reason(item, 'legacy_unverified')
            recorded_contract_hash = record.get('contract_hash', record.get('observed_contract_hash'))
            if contract and recorded_contract_hash != _contract_hash(contract):
                item['action'] = 'translate'
                _reason(item, 'translation_contract_changed')

        if item["action"] == "unchanged" and record is not None:
            if record.get("source_hash") != current_source_hash:
                item["action"] = "translate"
                _reason(item, "source_hash_changed_since_record")

        if item["action"] == "unchanged" and record is not None and record.get('provenance') != 'bypassed_reference':
            source_text = Path(source_path).read_text(encoding='utf-8')
            selected_terms = _selected_terms_for_chunk(glossary, source_path)
            current_relevant = _relevant_term_hashes(selected_terms, source_text)
            if record.get('terms_used') is not None:
                recorded_relevant = _relevant_term_hashes(record.get('terms_used', []), source_text)
            else:
                # Legacy/unverified records did not persist complete term
                # objects. Their id/hash pair is still enough when the same
                # term remains relevant under the current glossary.
                recorded_hashes = record.get('entity_hashes_used', {})
                recorded_relevant = {
                    term_id: recorded_hashes.get(term_id)
                    for term_id in record.get('entity_ids_used', [])
                    if term_id in current_relevant
                }

            if set(current_relevant) != set(recorded_relevant):
                if retranslate_on_term_drift:
                    item["action"] = "translate"
                    _reason(item, "glossary_term_selection_changed")
                else:
                    _reason(item, "glossary_term_selection_changed_ignored")
            else:
                changed_ids = [
                    term_id for term_id in current_relevant
                    if current_relevant.get(term_id) != recorded_relevant.get(term_id)
                ]
                if changed_ids:
                    item["action"] = "translate"
                    _reason(item, "glossary_term_hash_changed", changed_ids)

        if item["action"] == "unchanged" and record is not None:
            current_output_hash = file_hash(output_path) if os.path.exists(output_path) else ""
            if record.get("output_hash") != current_output_hash:
                item["action"] = "translate" if require_verified else "record"
                _reason(item, "output_hash_changed_since_record")

        if record is not None and record.get('provenance') not in ('verified_dispatch', 'bypassed_reference'):
            result['unverified_chunk_ids'].append(chunk_id)

        if item["action"] == "translate":
            result["translation_chunk_ids"].append(chunk_id)
        elif item["action"] == "record":
            result["record_only_chunk_ids"].append(chunk_id)
        else:
            result["unchanged_chunk_ids"].append(chunk_id)
        result["chunks"].append(item)

    return result


def status(temp_dir):
    state = load_run_state(temp_dir)
    entries = _chunk_entries_from_manifest(temp_dir)
    planned = plan(temp_dir)
    return {
        "temp_dir": temp_dir,
        "tracked_chunks": len(state.get("chunks", {})),
        "source_chunks": len(entries),
        "translation_needed": len(planned["translation_chunk_ids"]),
        "record_only_needed": len(planned["record_only_chunk_ids"]),
        "unchanged": len(planned["unchanged_chunk_ids"]),
    }


def _print_json(data):
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        print(payload)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or 'ascii'
        print(payload.encode(encoding, errors='backslashreplace').decode(encoding, errors='replace'))


def main():
    parser = argparse.ArgumentParser(description="Track selective re-translation state")
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_init = sub.add_parser('init', help='Initialize the exact translation contract')
    p_init.add_argument('temp_dir')
    p_init.add_argument('--lang', required=True)
    p_init.add_argument('--instructions', default='')
    p_init.add_argument('--instructions-file')
    p_init.add_argument('--prompt-version', default=DEFAULT_PROMPT_VERSION)

    p_dispatch = sub.add_parser('dispatch', help='Snapshot inputs before translation')
    p_dispatch.add_argument('temp_dir')
    p_dispatch.add_argument('chunk_ids', nargs='+')

    p_freeze = sub.add_parser('freeze', help='Freeze the glossary before final correction')
    p_freeze.add_argument('temp_dir')

    p_ready = sub.add_parser('verify', help='Require empty correction queues before building')
    p_ready.add_argument('temp_dir')

    p_plan = sub.add_parser('plan', help="Decide which chunks need translation or state recording")
    p_plan.add_argument('temp_dir')
    p_plan.add_argument(
        '--retranslate-untracked',
        action='store_true',
        help="Treat existing outputs without run_state records as needing translation",
    )

    p_record = sub.add_parser('record', help="Record one or more completed output chunks")
    p_record.add_argument('temp_dir')
    p_record.add_argument('chunk_ids', nargs='+')

    p_record_all = sub.add_parser('record-all', help="Record every complete output chunk")
    p_record_all.add_argument('temp_dir')

    p_status = sub.add_parser('status', help="Show run_state progress summary")
    p_status.add_argument('temp_dir')

    args = parser.parse_args()

    try:
        if args.cmd == 'init':
            instructions = args.instructions
            if args.instructions_file:
                with open(args.instructions_file, encoding='utf-8', newline='') as handle:
                    instructions = handle.read()
            _print_json(init_run(args.temp_dir, args.lang, instructions, args.prompt_version))
        elif args.cmd == 'dispatch':
            _print_json(dispatch_chunks(args.temp_dir, args.chunk_ids))
        elif args.cmd == 'freeze':
            _print_json({'frozen_glossary_hash': freeze_glossary(args.temp_dir)})
        elif args.cmd == 'verify':
            _print_json(assert_build_ready(args.temp_dir))
        elif args.cmd == 'plan':
            _print_json(plan(args.temp_dir, retranslate_untracked=args.retranslate_untracked))
        elif args.cmd == 'record':
            _print_json({"recorded_chunk_ids": record_chunks(args.temp_dir, args.chunk_ids)})
        elif args.cmd == 'record-all':
            plan_data = plan(args.temp_dir)
            eligible = [
                item["chunk_id"] for item in plan_data["chunks"]
                if item["action"] in ("record", "unchanged")
            ]
            _print_json({"recorded_chunk_ids": record_chunks(args.temp_dir, eligible)})
        elif args.cmd == 'status':
            _print_json(status(args.temp_dir))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
