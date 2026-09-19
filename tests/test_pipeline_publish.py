import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import glossary
import meta
import pipeline
import publish


EMPTY_META = {"schema_version": 1, "new_entities": [], "alias_hypotheses": [],
              "attribute_hypotheses": [], "used_term_sources": [], "conflicts": []}


class PipelinePublishTests(unittest.TestCase):
    def make_pdf(self, path, two_columns=False):
        import fitz
        doc = fitz.open()
        page = doc.new_page(width=420, height=500)
        page.insert_text((35, 45), "A Source Heading", fontsize=18)
        if two_columns:
            for index in range(4):
                text = "Left column sentence with enough prose to classify this line."
                page.insert_text((25, 90 + index * 25), text, fontsize=6)
                page.insert_text((225, 90 + index * 25), "Right column sentence with enough prose to classify this line.", fontsize=6)
        else:
            page.insert_text((35, 90), "First complete source sentence.", fontsize=11)
            page.insert_text((35, 120), "Second complete source sentence.", fontsize=11)
        doc.save(path)
        doc.close()

    def test_geometry_accepts_two_column_reading_order(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "columns.pdf"
            self.make_pdf(source, two_columns=True)
            report = pipeline.inspect_pdf(source)
            self.assertEqual(report["recommended_route"], "mineru")
            prepared = pipeline.prepare(source, root / "run")
            self.assertEqual(prepared["status"], "parse_review_pending")
            self.assertEqual(prepared["blocking_issues"], [])
            state = json.loads((root / "run" / "pipeline_state.json").read_text(encoding="utf-8"))
            codes = {x["code"] for x in state.get("warnings", [])}
            self.assertIn("reading_order_column_geometry", codes)

    def test_full_gate_rejects_truncation_then_accepts_reviewed_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "simple.pdf"
            run = root / "run"
            self.make_pdf(source)
            prepared = pipeline.prepare(source, run, target_lang="zh-CN",
                                        instructions="clear scientific Chinese", chunk_size=800)
            self.assertEqual(prepared["status"], "parse_review_pending")
            manifest_data = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
            chunk_ids = [item["id"] for item in manifest_data["chunks"]]
            with self.assertRaises(pipeline.PipelineError):
                pipeline.dispatch(run, chunk_ids)
            pipeline.accept_parse(run, "test", "compared page 1 reading order and headings")
            pipeline.dispatch(run, chunk_ids)
            first = manifest_data["chunks"][0]
            source_text = (run / first["source_file"]).read_text(encoding="utf-8")
            markers = list(pipeline.manifest.parse_block_markers(source_text))
            self.assertGreaterEqual(len(markers), 2)
            bad = f"<!-- tb:{markers[0][0]} -->\n{markers[0][1]}\n"
            (run / first["output_file"]).write_text(bad, encoding="utf-8")
            meta.save_meta(run / f"output_{first['id']}.meta.json", EMPTY_META)
            with self.assertRaises(ValueError):
                pipeline.record(run, [first["id"]])
            (run / first["output_file"]).write_text(source_text, encoding="utf-8")
            pipeline.record(run, [first["id"]])
            for item in manifest_data["chunks"][1:]:
                shutil.copy2(run / item["source_file"], run / item["output_file"])
                meta.save_meta(run / f"output_{item['id']}.meta.json", EMPTY_META)
                pipeline.record(run, [item["id"]])
            current = glossary.load_glossary(run / "glossary.json")
            current["applied_meta_hashes"] = {item["id"]: meta.meta_content_hash(EMPTY_META)
                                              for item in manifest_data["chunks"]}
            glossary.save_glossary(run / "glossary.json", current)
            pipeline.freeze(run)

            css, page_info = publish.style_css({"page": {"width_pt": 400, "height_pt": 500,
                                                          "margin_top_pt": 10, "margin_right_pt": 20,
                                                          "margin_bottom_pt": 30, "margin_left_pt": 40,
                                                          "columns": 2},
                                                 "body": {}}, "mono")
            self.assertIn("margin: 10.0pt 20.0pt 30.0pt 40.0pt", css)
            self.assertEqual(page_info["columns"], 2)

            def fake_render(html_path, pdf_path=None, **kwargs):
                return {"status": "passed", "problems": [], "blocks": len(markers),
                        "images": 0, "math": 0, "fonts_ready": True,
                        "pdf_visual_review": "not_requested"}

            with mock.patch.object(publish, "render_html", side_effect=fake_render):
                result = pipeline.build(run, formats=("html",), editions=("mono",), lang="zh-CN")
            self.assertEqual(result["status"], "generated")
            accepted = pipeline.accept_publish(run, "test", "opened complete HTML and checked page 1 style")
            self.assertEqual(accepted["status"], "accepted")
            # A deterministic cache hit keeps a valid acceptance rather than
            # silently demoting the run back to generated.
            with mock.patch.object(publish, "render_html", side_effect=fake_render):
                rebuilt = pipeline.build(run, formats=("html",), editions=("mono",), lang="zh-CN")
            self.assertEqual(rebuilt["status"], "accepted")
            final_status = pipeline.status(run)
            self.assertEqual(final_status["status"], "accepted")
            self.assertEqual(pipeline.cleanup(run)["removed"], [])


if __name__ == "__main__":
    unittest.main()
