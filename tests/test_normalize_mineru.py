import json
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import normalize_mineru as norm


class NormalizeMinerUTests(unittest.TestCase):
    def test_direct_and_nested_equation_spans_are_preserved(self):
        direct = {"type": "formula", "spans": [{"content": "E=mc^2"}]}
        nested = {"type": "interline_equation", "blocks": [
            {"type": "interline_equation", "lines": [
                {"spans": [{"content": "x^2"}, {"content": "+y^2"}]}
            ]}
        ]}
        self.assertEqual(norm._equation_latex(direct), "E=mc^2")
        self.assertEqual(norm._equation_latex(nested), "x^2 +y^2")

    def test_all_supported_equation_block_names_emit_math(self):
        for kind in ("interline_equation", "equation", "formula"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "x_middle.json").write_text(json.dumps({"pdf_info": [{
                    "page_idx": 0,
                    "para_blocks": [{"type": kind, "index": 0,
                                     "bbox": [10, 10, 100, 30],
                                     "spans": [{"content": r"a=b"}]}]
                }]}), encoding="utf-8")
                result, images = norm.normalize(str(root), "unused.pdf", merge_figures=False)
                self.assertEqual(result, "$$\na=b\n$$\n")
                self.assertEqual(images, [])

    def test_duplicate_contained_panel_does_not_duplicate_figure(self):
        wrapper = {"type": "image", "bbox": [10, 10, 200, 150], "blocks": [
            {"type": "image_body", "bbox": [10, 10, 200, 120]},
            {"type": "image_caption", "bbox": [10, 125, 200, 150],
             "lines": [{"spans": [{"content": "Fig. 1 Complete caption"}]}]},
        ]}
        duplicate = {"type": "image", "bbox": [20, 20, 80, 80], "blocks": [
            {"type": "image_body", "bbox": [20, 20, 80, 80]}
        ]}
        groups = norm._figure_groups([wrapper, {"type": "text", "bbox": [1, 1, 2, 2]}, duplicate])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["items"]), 2)
        self.assertEqual(norm._captions(wrapper)[0]["type"], "image_caption")

    def test_connected_panels_merge_and_panel_letters_are_not_captions(self):
        blocks = [
            {"type": "image", "bbox": [50, 10, 250, 80], "index": 0},
            {"type": "image", "bbox": [50, 92, 250, 125], "index": 1,
             "blocks": [{"type": "image_caption", "bbox": [35, 84, 44, 94],
                         "lines": [{"spans": [{"content": "b"}]}]}]},
            {"type": "text", "bbox": [1, 1, 2, 2], "index": 2},
            {"type": "chart", "bbox": [50, 137, 250, 220], "index": 3,
             "blocks": [
                 {"type": "chart_caption", "bbox": [30, 222, 150, 245],
                  "lines": [{"spans": [{"content": "Fig. 1 | Full caption."}]}]},
                 {"type": "chart_caption", "bbox": [155, 222, 280, 245],
                  "lines": [{"spans": [{"content": "g, Caption continuation."}]}]},
             ]},
        ]
        groups = norm._figure_groups(blocks)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["items"]), 3)
        captions = norm._group_captions(groups[0])
        self.assertEqual(len(captions), 1)
        self.assertEqual(captions[0]["text"],
                         "Fig. 1 | Full caption. g, Caption continuation.")

    def test_top_level_axis_label_overlapping_figure_is_included_and_consumed(self):
        blocks = [
            {"type": "text", "bbox": [185, 443, 284, 451], "index": 1,
             "lines": [{"spans": [{"content": "Avg. firing rate of excitatory neurons (Hz)"}]}]},
            {"type": "chart", "bbox": [41, 47, 297, 444], "index": 2},
            {"type": "text", "bbox": [36, 455, 293, 745], "index": 3,
             "lines": [{"spans": [{"content": "Fig. 5 | Full caption."}]}]},
        ]
        groups = norm._figure_groups(blocks)
        positions = norm._embedded_figure_text_positions(groups[0], blocks)
        self.assertEqual(positions, {0})
        self.assertEqual(norm._group_visual_bbox(groups[0], blocks), [41, 47, 297, 451])

    def test_html_table_text_can_change_but_structure_cannot(self):
        import manifest
        source = "<!-- tb:b000001 -->\n<table><tr><td>Cell A</td><td><math>x</math></td></tr></table>\n"
        translated = "<!-- tb:b000001 -->\n<table><tr><td>单元格 A</td><td><math>x</math></td></tr></table>\n"
        blocks = [{"id": "b000001", "kind": "table", "translatable": True}]
        self.assertEqual(manifest.validate_translation(source, translated, blocks), [])
        changed = translated.replace("<td>单元格 A</td>", "<th>单元格 A</th>")
        self.assertTrue(any("table" in error for error in
                            manifest.validate_translation(source, changed, blocks)))
        math_changed = translated.replace("<math>x</math>", "<math>y</math>")
        self.assertTrue(any("mathml" in error for error in
                            manifest.validate_translation(source, math_changed, blocks)))


if __name__ == "__main__":
    unittest.main()
