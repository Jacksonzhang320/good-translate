import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import pdf_document


def span(text):
    return {"content": text}


class PdfDocumentTests(unittest.TestCase):
    def test_cross_page_deleted_placeholder_reassigns_line_reference(self):
        block = {"lines": [
            {"bbox": [30, 720, 140, 740], "spans": [span("page one ending")]},
            {"bbox": [30, 40, 180, 60], "spans": [span("page two continuation")]},
            {"bbox": [30, 150, 180, 170], "spans": [span("still on page two")]},
        ]}
        refs = pdf_document._text_source_refs(
            block, 1, [20, 700, 200, 750],
            {1: [[20, 30, 200, 70]], 2: [[20, 30, 200, 70]]})
        self.assertEqual([ref["page"] for ref in refs], [1, 2, 2])

    def test_source_text_disambiguates_column_reset_from_page_break(self):
        block = {"lines": [
            {"bbox": [160, 40, 290, 60], "spans": [span("right column same page")]},
        ]}
        pages = [
            {"lines": [{"bbox": [160, 40, 290, 60], "text": "right column same page"}]},
            {"lines": [{"bbox": [160, 40, 290, 60], "text": "different next page text"}]},
        ]
        refs = pdf_document._text_source_refs(
            block, 1, [20, 700, 150, 750],
            {2: [[150, 30, 295, 70]]}, pages)
        self.assertEqual(refs[0]["page"], 1)

    def test_formula_box_includes_printed_number(self):
        page = {"lines": [
            {"text": "(3)", "bbox": [270, 100, 290, 115]},
            {"text": "not a number", "bbox": [260, 100, 295, 115]},
        ]}
        self.assertEqual(pdf_document._complete_formula_box(
            [40, 95, 240, 120], page), [40, 95, 290, 120])

    def make_fixture(self, root):
        import fitz
        from PIL import Image
        pdf_path = root / "paper.pdf"
        document = fitz.open()
        page = document.new_page(width=300, height=400)
        page.insert_text((30, 45), "Introduction", fontsize=18)
        page.insert_text((30, 80), "Alpha sentence.", fontsize=11)
        image = Image.new("RGB", (100, 50), "#4b78a8")
        stream = io.BytesIO()
        image.save(stream, "PNG")
        page.insert_image(fitz.Rect(30, 120, 170, 200), stream=stream.getvalue())
        page.insert_text((30, 220), "Fig. 1 Complete caption.", fontsize=9)
        page.insert_text((30, 265), "E = mc2   (1)", fontsize=12)
        page.insert_text((30, 330), "Cell A     Cell B", fontsize=9)
        document.save(pdf_path)
        document.close()

        mineru = root / "mineru"
        mineru.mkdir()
        middle = {"pdf_info": [{"page_idx": 0, "page_size": [300, 400],
            "discarded_blocks": [], "para_blocks": [
                {"type": "title", "index": 0, "bbox": [20, 25, 220, 55],
                 "lines": [{"spans": [span("Introduction")]}]},
                {"type": "text", "index": 1, "bbox": [20, 60, 250, 95],
                 "lines": [{"spans": [span("Alpha sentence.")]}]},
                {"type": "image", "index": 2, "bbox": [25, 115, 210, 230],
                 "blocks": [
                     {"type": "image_body", "bbox": [30, 120, 170, 200]},
                     {"type": "image_caption", "bbox": [20, 200, 230, 230],
                      "lines": [{"spans": [span("Fig. 1 Complete caption.")]}]},
                 ]},
                {"type": "formula", "index": 3, "bbox": [20, 240, 210, 280],
                 "spans": [span(r"E=mc^2\\tag{1}")]},
                {"type": "table", "index": 4, "bbox": [20, 300, 260, 345],
                 "blocks": [{"type": "table_body", "bbox": [20, 300, 260, 345],
                             "spans": [{"html": "<table><tr><td>Cell A</td><td>Cell B</td></tr></table>"}]}]},
            ]}]}
        (mineru / "paper_middle.json").write_text(json.dumps(middle), encoding="utf-8")
        return pdf_path, mineru

    def test_mineru_document_preserves_formula_figure_caption_and_style(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pdf, mineru = self.make_fixture(root)
            out = root / "run"
            doc = pdf_document.build_document(pdf, out, mineru_dir=mineru)
            errors = [x for x in doc["issues"] if x.get("severity") == "error"]
            self.assertEqual(errors, [])
            kinds = [block["kind"] for block in doc["blocks"]]
            self.assertEqual(kinds.count("figure"), 1)
            self.assertEqual(kinds.count("formula"), 1)
            self.assertEqual(kinds.count("table"), 1)
            figure = next(block for block in doc["blocks"] if block["kind"] == "figure")
            caption = next(block for block in doc["blocks"] if block["kind"] == "caption")
            formula = next(block for block in doc["blocks"] if block["kind"] == "formula")
            table = next(block for block in doc["blocks"] if block["kind"] == "table")
            self.assertEqual(caption["caption_of"], figure["id"])
            self.assertEqual(formula["latex"], r"E=mc^2\\tag{1}")
            self.assertTrue(table["translatable"])
            self.assertIn("<td>Cell A</td>", table["text"])
            self.assertTrue((out / table["fallback_image"]).is_file())
            self.assertTrue((out / figure["text"].split("(", 1)[1][:-1]).is_file())
            self.assertTrue((out / formula["text"].split("(", 1)[1][:-1]).is_file())
            style = json.loads((out / "style.json").read_text(encoding="utf-8"))
            self.assertIn("margin_left_pt", style["page"])
            self.assertGreater(style["body"]["font_size_pt"], 0)

    def test_axis_label_overlapping_figure_is_not_emitted_as_prose(self):
        import fitz
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pdf = root / "axis-label.pdf"
            source = fitz.open()
            page = source.new_page(width=300, height=400)
            page.insert_text((90, 205), "Average firing rate (Hz)", fontsize=9)
            source.save(pdf)
            source.close()

            mineru = root / "mineru"
            mineru.mkdir()
            middle = {"pdf_info": [{"page_idx": 0, "page_size": [300, 400],
                "discarded_blocks": [], "para_blocks": [
                    {"type": "text", "index": 0, "bbox": [85, 193, 190, 207],
                     "lines": [{"spans": [span("Average firing rate (Hz)")]}]},
                    {"type": "chart", "index": 1, "bbox": [30, 50, 250, 200]},
                    {"type": "text", "index": 2, "bbox": [30, 215, 250, 245],
                     "lines": [{"spans": [span("Fig. 1 | Complete caption.")]}]},
                ]}]}
            (mineru / "axis_middle.json").write_text(json.dumps(middle), encoding="utf-8")
            doc = pdf_document.build_document(pdf, root / "run", mineru_dir=mineru)
            blocks = doc["blocks"]
            self.assertFalse(any(block["text"] == "Average firing rate (Hz)" for block in blocks))
            figure = next(block for block in blocks if block["kind"] == "figure")
            self.assertGreaterEqual(figure["source_refs"][0]["bbox"][3], 211)

    def test_top_level_and_next_page_figure_captions_are_linked(self):
        figure = {"kind": "figure", "text": "![](f.png)",
                  "source_refs": [{"page": 4, "bbox": [1, 1, 10, 10]}]}
        same_page = {"kind": "heading", "text": "Extended Data Fig. 2 | Caption",
                     "source_refs": [{"page": 4, "bbox": [1, 12, 10, 14]}]}
        next_page = {"kind": "text", "text": "Extended Data Fig. 2 | Continued caption",
                     "source_refs": [{"page": 5, "bbox": [1, 1, 10, 10]}]}
        pdf_document._label_external_captions([figure, same_page, next_page])
        self.assertEqual(same_page["kind"], "caption")
        self.assertEqual(next_page["kind"], "caption")
        self.assertIs(same_page["_caption_target"], figure)
        self.assertIs(next_page["_caption_target"], figure)

    def test_cross_column_paragraph_uses_line_geometry(self):
        import fitz
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pdf = root / "columns.pdf"
            source = fitz.open()
            page = source.new_page(width=300, height=400)
            page.insert_text((25, 350), "Sentence begins in the left column", fontsize=10)
            page.insert_text((165, 45), "and continues in the right column.", fontsize=10)
            source.save(pdf)
            source.close()

            mineru = root / "mineru"
            mineru.mkdir()
            middle = {"pdf_info": [{"page_idx": 0, "page_size": [300, 400],
                "discarded_blocks": [], "para_blocks": [
                    {"type": "text", "index": 0, "bbox": [20, 335, 150, 365],
                     "lines": [
                         {"bbox": [20, 335, 150, 355], "spans": [span("Sentence begins in the left column")]},
                         {"bbox": [160, 30, 290, 50], "spans": [span("and continues in the right column.")]},
                     ]},
                    {"type": "text", "index": 1, "bbox": [160, 30, 290, 55],
                     "lines": [], "lines_deleted": True},
                ]}]}
            (mineru / "columns_middle.json").write_text(json.dumps(middle), encoding="utf-8")
            doc = pdf_document.build_document(pdf, root / "run", mineru_dir=mineru)
            errors = [x for x in doc["issues"] if x.get("severity") == "error"]
            self.assertEqual(errors, [])
            text = next(block for block in doc["blocks"] if block["kind"] == "text")
            self.assertEqual(len(text["source_refs"]), 2)
            self.assertIn("right column", text["text"])


if __name__ == "__main__":
    unittest.main()
