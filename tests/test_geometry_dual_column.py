import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fitz
import manifest
import pdf_document
import pipeline


class TestGeometryDualColumnAndSplitting(unittest.TestCase):
    def test_sort_page_blocks_single_column(self):
        blocks = [
            {'source_refs': [{'page': 1, 'bbox': [50, 200, 400, 250]}], 'id': 'b1'},
            {'source_refs': [{'page': 1, 'bbox': [50, 100, 400, 150]}], 'id': 'b2'},
            {'source_refs': [{'page': 1, 'bbox': [50, 300, 400, 350]}], 'id': 'b3'},
        ]
        sorted_blocks = pdf_document._sort_page_blocks(blocks, width=500, height=800, columns=1)
        ids = [b['id'] for b in sorted_blocks]
        self.assertEqual(ids, ['b2', 'b1', 'b3'])

    def test_sort_page_blocks_dual_column(self):
        blocks = [
            {'id': 'right_1', 'source_refs': [{'page': 1, 'bbox': [280, 100, 450, 150]}]},
            {'id': 'left_2', 'source_refs': [{'page': 1, 'bbox': [50, 160, 220, 210]}]},
            {'id': 'top_title', 'source_refs': [{'page': 1, 'bbox': [50, 40, 450, 80]}]},
            {'id': 'footer', 'source_refs': [{'page': 1, 'bbox': [50, 750, 450, 770]}]},
            {'id': 'left_1', 'source_refs': [{'page': 1, 'bbox': [50, 100, 220, 150]}]},
            {'id': 'right_2', 'source_refs': [{'page': 1, 'bbox': [280, 160, 450, 210]}]},
        ]
        sorted_blocks = pdf_document._sort_page_blocks(blocks, width=500, height=800, columns=2)
        ids = [b['id'] for b in sorted_blocks]
        self.assertEqual(ids, ['top_title', 'left_1', 'left_2', 'right_1', 'right_2', 'footer'])

    def test_sort_page_blocks_multi_band_box(self):
        blocks = [
            {'id': 'body_left', 'source_refs': [{'page': 1, 'bbox': [50, 300, 220, 500]}]},
            {'id': 'box_title', 'source_refs': [{'page': 1, 'bbox': [50, 100, 450, 130]}]},
            {'id': 'body_right', 'source_refs': [{'page': 1, 'bbox': [280, 300, 450, 500]}]},
            {'id': 'box_left', 'source_refs': [{'page': 1, 'bbox': [50, 140, 220, 240]}]},
            {'id': 'box_right', 'source_refs': [{'page': 1, 'bbox': [280, 140, 450, 240]}]},
        ]
        sorted_blocks = pdf_document._sort_page_blocks(blocks, width=500, height=800, columns=2)
        ids = [b['id'] for b in sorted_blocks]
        self.assertEqual(ids, ['box_title', 'box_left', 'box_right', 'body_left', 'body_right'])

    def test_plan_split_and_split_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            blocks = []
            for i in range(1, 21):
                b_id = f"b{i:06d}"
                if i == 1:
                    kind = "heading"
                    text = "Section 1 Introduction"
                elif i == 8:
                    kind = "heading"
                    text = "Section 2 Results"
                elif i == 15:
                    kind = "heading"
                    text = "References"
                else:
                    kind = "text"
                    text = f"Paragraph content for block {i} with substantial words " * 20
                blocks.append({
                    "id": b_id,
                    "kind": kind,
                    "text": text,
                    "source_refs": [{"page": 1, "bbox": [50, 50 + i * 20, 400, 65 + i * 20]}]
                })

            doc = {
                "schema_version": 1,
                "structure_hash": "test_hash_12345",
                "source": {"path": str(root / "src.pdf"), "sha256": "abcdef"},
                "parser": "pymupdf-geometry",
                "blocks": blocks
            }
            (root / "doc.json").write_text(json.dumps(doc), encoding="utf-8")
            full_md = "\n".join(f"<!-- tb:{b['id']} -->\n{b['text'].strip()}" for b in blocks) + "\n"
            (root / "input.md").write_text(full_md, encoding="utf-8")

            plan = pipeline.plan_split(root, target_chars=2000)
            self.assertEqual(plan["status"], "success")
            self.assertIn("outline_tree", plan)
            self.assertTrue(len(plan["suggested_cuts"]) >= 1)

            custom_cuts = ["b000008", "b000015"]
            chunks = pipeline.split_document_by_cuts(doc, custom_cuts)
            self.assertEqual(len(chunks), 3)
            self.assertIn("b000001", chunks[0])
            self.assertIn("b000008", chunks[1])
            self.assertIn("b000015", chunks[2])

            res = pipeline.split_run(root, cuts=custom_cuts)
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["chunk_count"], 3)
            self.assertTrue((root / "chunk0001.md").is_file())
            self.assertTrue((root / "chunk0002.md").is_file())
            self.assertTrue((root / "chunk0003.md").is_file())

            errors = manifest.validate_manifest(str(root))
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
