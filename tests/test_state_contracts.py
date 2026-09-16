"""Regression contracts linking split coverage, dispatch and feedback correction."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / 'scripts'
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import glossary
import manifest
import run_state
from test_merge_meta import (empty_meta, make_glossary, make_term, run_apply_merge,
                             run_prepare_merge, temp_workspace)


class TranslationContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        for filename, text in [('input.md', 'Tai went home.\n'), ('chunk0001.md', 'Tai went home.\n'),
                               ('output_chunk0001.md', '太一回家了。\n')]:
            self.path.joinpath(filename).write_text(text, encoding='utf-8')
        manifest.create_manifest(self.path, ['chunk0001.md'], self.path / 'input.md')
        glossary.save_glossary(self.path / 'glossary.json', make_glossary(make_term('Tai', '太一')))

    def dispatch_record(self, lang='zh', instructions='', version='tb-v2'):
        run_state.init_run(self.path, lang, instructions, version)
        run_state.dispatch_chunks(self.path, ['chunk0001'])
        run_state.record_chunks(self.path, ['chunk0001'])

    def test_each_contract_field_and_exact_instruction_whitespace_invalidates(self):
        for lang, instructions, version in [('ja', 'literal\n', 'tb-v2'),
                                             ('zh', 'literal', 'tb-v2'),
                                             ('zh', 'literal\n', 'tb-v3')]:
            with self.subTest(lang=lang, instructions=instructions, version=version):
                self.dispatch_record(instructions='literal\n')
                run_state.init_run(self.path, lang, instructions, version)
                self.assertEqual(run_state.plan(self.path)['translation_chunk_ids'], ['chunk0001'])

    def test_snapshot_is_the_actual_dispatch_contract_even_after_edits(self):
        raw = '  rule\r\n第二行\n'
        run_state.init_run(self.path, 'zh', raw)
        sent = run_state.dispatch_chunks(self.path, ['chunk0001'])['chunk0001']
        updated = make_glossary(make_term('Tai', '泰'))
        glossary.save_glossary(self.path / 'glossary.json', updated)
        run_state.init_run(self.path, 'ja', 'different')
        run_state.record_chunks(self.path, ['chunk0001'])
        recorded = run_state.load_run_state(self.path)['chunks']['chunk0001']
        self.assertEqual(recorded['translation_contract']['custom_instructions'], raw)
        self.assertEqual(recorded['translation_contract']['target_lang'], 'zh')
        self.assertEqual(recorded['entity_hashes_used'], sent['entity_hashes_used'])
        self.assertEqual(run_state.plan(self.path)['translation_chunk_ids'], ['chunk0001'])

    def test_strict_record_cannot_invent_dispatch_history(self):
        run_state.init_run(self.path, 'zh')
        with self.assertRaisesRegex(ValueError, 'No dispatch'):
            run_state.record_chunks(self.path, ['chunk0001'])
        self.assertEqual(run_state.load_run_state(self.path)['chunks'], {})

    def test_v1_is_unverified_and_never_gets_fake_historical_contract(self):
        run_state.record_chunks(self.path, ['chunk0001'])
        state = run_state.load_run_state(self.path)
        state['version'] = 1
        state['chunks']['chunk0001'].pop('provenance')
        run_state.save_run_state(self.path, state)
        loaded = run_state.load_run_state(self.path)
        self.assertEqual(loaded['chunks']['chunk0001']['provenance'], 'legacy_unverified')
        self.assertNotIn('translation_contract', loaded['chunks']['chunk0001'])
        run_state.init_run(self.path, 'zh')
        self.assertEqual(run_state.plan(self.path)['translation_chunk_ids'], ['chunk0001'])

    def test_legacy_config_language_change_is_detected(self):
        self.path.joinpath('config.txt').write_text('output_lang=zh\n', encoding='utf-8')
        run_state.record_chunks(self.path, ['chunk0001'])
        self.path.joinpath('config.txt').write_text('output_lang=ja\n', encoding='utf-8')
        self.assertEqual(run_state.plan(self.path)['translation_chunk_ids'], ['chunk0001'])

    def test_gender_feedback_requires_correction_before_final_build(self):
        self.dispatch_record()
        updated = glossary.load_glossary(self.path / 'glossary.json')
        updated['terms'][0]['gender'] = 'female'
        glossary.save_glossary(self.path / 'glossary.json', updated)
        run_state.freeze_glossary(self.path)
        self.assertEqual(run_state.plan(self.path)['translation_chunk_ids'], ['chunk0001'])
        self.assertFalse(manifest.validate_for_merge(self.path)[0])
        with self.assertRaisesRegex(ValueError, 'queue'):
            run_state.assert_build_ready(self.path)
        dispatched = run_state.dispatch_chunks(self.path, ['chunk0001'])
        self.assertIn('gender=female', dispatched['chunk0001']['term_table'])
        self.path.joinpath('output_chunk0001.md').write_text('太一回家了，她很高兴。\n', encoding='utf-8')
        run_state.record_chunks(self.path, ['chunk0001'])
        planned = run_state.assert_build_ready(self.path)
        self.assertEqual(planned['translation_chunk_ids'], [])
        self.assertEqual(planned['record_only_chunk_ids'], [])
        self.assertTrue(manifest.validate_for_merge(self.path)[0])

    def test_manual_output_change_cannot_pass_verified_build(self):
        self.dispatch_record()
        run_state.freeze_glossary(self.path)
        self.path.joinpath('output_chunk0001.md').write_text('unrecorded edit', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'queue'):
            run_state.assert_build_ready(self.path)

    def test_source_edit_during_dispatch_keeps_pending_evidence(self):
        run_state.init_run(self.path, 'zh')
        snapshot = run_state.dispatch_chunks(self.path, ['chunk0001'])
        self.path.joinpath('chunk0001.md').write_text('Changed source', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Source changed'):
            run_state.record_chunks(self.path, ['chunk0001'])
        self.assertEqual(run_state.load_run_state(self.path)['dispatches'], snapshot)


class ManifestCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.blocks = [
            {'id': 'b000001', 'kind': 'text', 'text': 'Tai sees $x^2$.', 'translatable': True},
            {'id': 'b000002', 'kind': 'figure', 'text': '![panel](media/panel.png)', 'translatable': False},
        ]
        doc = {'schema_version': 1, 'doc_id': 'a' * 64, 'structure_hash': 'b' * 64,
               'blocks': self.blocks, 'source': {'path': 'source.pdf', 'sha256': 'a' * 64}, 'issues': []}
        self.path.joinpath('doc.json').write_text(json.dumps(doc), encoding='utf-8')
        self.parts = [f"<!-- tb:{b['id']} -->\n\n{b['text']}\n" for b in self.blocks]
        self.path.joinpath('input.md').write_text('\n'.join(self.parts), encoding='utf-8')
        for index, text in enumerate(self.parts, 1):
            self.path.joinpath(f'chunk{index:04d}.md').write_text(text, encoding='utf-8')
            self.path.joinpath(f'output_chunk{index:04d}.md').write_text(text, encoding='utf-8')

    def publish(self):
        return manifest.create_manifest(self.path, ['chunk0001.md', 'chunk0002.md'], self.path / 'input.md')

    def test_partial_split_does_not_publish_or_replace_manifest(self):
        self.publish()
        before = self.path.joinpath('manifest.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'coverage'):
            manifest.create_manifest(self.path, ['chunk0001.md'], self.path / 'input.md')
        self.assertEqual(self.path.joinpath('manifest.json').read_bytes(), before)

    def test_manifest_detects_duplicate_reordered_and_omitted_blocks(self):
        valid = self.publish()
        self.assertEqual(manifest.validate_manifest(self.path, valid), [])
        for ids in [['b000001', 'b000001'], ['b000002', 'b000001'], ['b000001']]:
            changed = copy.deepcopy(valid)
            changed['expected_block_ids'] = ids
            self.assertTrue(manifest.validate_manifest(self.path, changed))

    def test_changed_input_or_doc_never_passes_completeness(self):
        self.publish()
        self.path.joinpath('input.md').write_text(self.parts[0], encoding='utf-8')
        self.assertFalse(manifest.validate_for_merge(self.path)[0])

    def test_translation_rejects_lost_ids_math_and_image_destinations(self):
        self.publish()
        source = '\n'.join(self.parts)
        for output in [self.parts[0], source.replace('$x^2$', '$x^3$'),
                       source.replace('media/panel.png', 'media/other.png')]:
            self.assertTrue(manifest.validate_translation(source, output, self.blocks))
        self.assertEqual(manifest.validate_translation(source, source.replace('Tai sees', '太一看见'), self.blocks), [])

    def test_v1_is_not_silently_upgraded(self):
        self.path.joinpath('doc.json').unlink()
        legacy = manifest.create_manifest(self.path, ['chunk0001.md'], self.path / 'input.md')
        self.assertNotEqual(legacy.get('version'), 2)
        self.assertNotIn('split_complete', legacy)


class AttributeCreationTests(unittest.TestCase):
    def test_same_meta_creates_entity_then_preserves_its_gender(self):
        observations = empty_meta(
            new_entities=[{'source': 'Tai', 'target_proposal': '太一', 'category': 'person', 'evidence': 'Tai said he agreed.'}],
            attribute_hypotheses=[{'entity_source': 'Tai', 'attribute': 'gender', 'value': 'male',
                                  'confidence': 'high', 'evidence': 'Tai said he agreed.'}])
        with temp_workspace(make_glossary(), {'chunk0001': observations}) as directory:
            prepared, _ = run_prepare_merge(directory)
            _, error, code = run_apply_merge(directory, {'auto_apply': prepared['auto_apply'],
                'decisions': [], 'consumed_chunk_ids': prepared['consumed_chunk_ids']})
            self.assertEqual(code, 0, error)
            term = glossary.load_glossary(Path(directory) / 'glossary.json')['terms'][0]
        self.assertEqual(term['gender'], 'male')
        self.assertIn('gender=male', glossary.format_terms_for_prompt([term]))
        unknown = dict(term, gender='unknown')
        self.assertNotEqual(glossary.term_hash(term), glossary.term_hash(unknown))

    def test_conflicting_gender_is_not_replaced_by_next_observation(self):
        term = make_term('Tai', '太一')
        term['gender'] = 'male'
        observations = empty_meta(attribute_hypotheses=[
            {'entity_source': 'Tai', 'attribute': 'gender', 'value': value,
             'confidence': 'high', 'evidence': 'Contradictory source evidence.'}
            for value in ['female', 'male']])
        with temp_workspace(make_glossary(term), {'chunk0001': observations}) as directory:
            _, error, code = run_apply_merge(directory, {'auto_apply': [], 'decisions': [], 'consumed_chunk_ids': ['chunk0001']})
            self.assertEqual(code, 0, error)
            saved = glossary.load_glossary(Path(directory) / 'glossary.json')['terms'][0]
        self.assertEqual(saved['gender'], 'unknown')
        self.assertTrue(saved['gender_conflict'])
        self.assertIn('gender unresolved', glossary.format_terms_for_prompt([saved]))
        self.assertNotEqual(glossary.term_hash(saved), glossary.term_hash(dict(saved, gender_conflict=False)))


if __name__ == '__main__':
    unittest.main()
