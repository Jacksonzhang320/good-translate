import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import audit_layout
import pipeline


class TermPatchingTests(unittest.TestCase):
    def test_extract_candidate_terms(self):
        doc = {
            "blocks": [
                {"id": "b0001", "text": "Artificial intelligence (AI) in drug discovery. SAR and PK/PD modeling."},
                {"id": "b0002", "text": "High-throughput screening and hit-to-lead optimization with HTS assays."},
                {"id": "b0003", "text": "THE PDF URL FOR DOI 10.1038 was cited."},
                {"id": "b0004", "text": "References [1] Nature 2026", "is_reference": True}
            ]
        }
        candidates = pipeline.extract_candidate_terms(doc, top_n=10)
        sources = [c["source"] for c in candidates]
        self.assertIn("SAR", sources)
        self.assertIn("PK/PD", sources)
        self.assertIn("HTS", sources)
        self.assertIn("hit-to-lead", sources)
        # Noise words filtered
        self.assertNotIn("THE", sources)
        self.assertNotIn("PDF", sources)
        self.assertNotIn("DOI", sources)

    def test_seed_glossary(self):
        with tempfile.TemporaryDirectory() as td:
            temp_dir = Path(td)
            doc = {
                "blocks": [
                    {"id": "b0001", "text": "PROTAC technology in cancer therapy."},
                    {"id": "b0002", "text": "Structure-activity relationship (SAR) analysis."}
                ]
            }
            (temp_dir / "doc.json").write_text(json.dumps(doc), encoding="utf-8")
            res = pipeline.seed_glossary(temp_dir, top_n=5)
            self.assertEqual(res["status"], "seeded")
            self.assertTrue((temp_dir / "glossary.json").is_file())
            gloss = json.loads((temp_dir / "glossary.json").read_text(encoding="utf-8"))
            sources = [t["source"] for t in gloss["terms"]]
            self.assertIn("PROTAC", sources)
            self.assertIn("SAR", sources)

    def test_patch_glossary_terms_preserves_invariants(self):
        with tempfile.TemporaryDirectory() as td:
            temp_dir = Path(td)
            gloss = {
                "version": 2,
                "terms": [
                    {
                        "source": "hit-to-lead",
                        "target": "苗头化合物至先导化合物",
                        "aliases": ["命中至先导", "苗头至先导"]
                    },
                    {
                        "source": "SAR",
                        "target": "构效关系 (SAR)",
                        "aliases": ["结构活性关系"]
                    }
                ]
            }
            (temp_dir / "glossary.json").write_text(json.dumps(gloss, ensure_ascii=False), encoding="utf-8")
            
            chunk_content = (
                "<!-- tb:b000001 -->\n"
                "在药物设计中，命中至先导优化必须考虑结构活性关系。\n"
                "数学关系为 $y = x^2$。结构图见 ![图1](assets/fig_命中至先导.png)。\n"
                "代码块为 `var test = 命中至先导`。\n"
            )
            (temp_dir / "output_chunk0001.md").write_text(chunk_content, encoding="utf-8")

            res = pipeline.patch_glossary_terms(temp_dir)
            self.assertEqual(res["status"], "patched")
            self.assertEqual(res["total_replacements"], 2)

            patched_content = (temp_dir / "output_chunk0001.md").read_text(encoding="utf-8")
            # Replaced in text
            self.assertIn("苗头化合物至先导化合物优化", patched_content)
            self.assertIn("构效关系 (SAR)", patched_content)
            # Preserved in comments, math, images, and code
            self.assertIn("<!-- tb:b000001 -->", patched_content)
            self.assertIn("$y = x^2$", patched_content)
            self.assertIn("assets/fig_命中至先导.png", patched_content)
            self.assertIn("`var test = 命中至先导`", patched_content)

    def test_audit_glossary_consistency(self):
        with tempfile.TemporaryDirectory() as td:
            temp_dir = Path(td)
            gloss = {
                "version": 2,
                "terms": [
                    {
                        "source": "hit-to-lead",
                        "target": "苗头化合物至先导化合物",
                        "aliases": ["命中至先导"]
                    }
                ]
            }
            gloss_path = temp_dir / "glossary.json"
            gloss_path.write_text(json.dumps(gloss, ensure_ascii=False), encoding="utf-8")

            # Leak case
            leaked_html = "<html><body><p>这是命中至先导阶段的实验结果。</p></body></html>"
            res1 = audit_layout.audit_html_content(leaked_html, filename="test.html", glossary_path=gloss_path)
            self.assertTrue(any(i["code"] == "term_drift_alias_leak" for i in res1["issues"]))

            # Clean case
            clean_html = "<html><body><p>这是苗头化合物至先导化合物阶段的实验结果。</p></body></html>"
            res2 = audit_layout.audit_html_content(clean_html, filename="test.html", glossary_path=gloss_path)
            self.assertFalse(any(i["code"] == "term_drift_alias_leak" for i in res2["issues"]))


if __name__ == "__main__":
    unittest.main()
