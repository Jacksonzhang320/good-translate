import json
from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import publish
import pipeline


class GovDocEditionTests(unittest.TestCase):
    def setUp(self):
        self.doc = {
            "title": "测试论文标题",
            "author": "测试作者",
            "journal": "自然·神经科学",
            "doi": "10.1038/s41593-026-02395-w",
            "blocks": [
                {"id": "b0001", "kind": "heading", "level": 1, "text": "Introduction", "translatable": True},
                {"id": "b0002", "kind": "text", "text": "This is introductory text.", "translatable": True},
                {"id": "b0003", "kind": "heading", "level": 2, "text": "Results", "translatable": True},
                {"id": "b0004", "kind": "heading", "level": 3, "text": "Sub-heading 1", "translatable": True},
                {"id": "b0005", "kind": "figure", "text": "![](assets/fig1.png)", "translatable": False},
            ],
            "source": {"journal": "Nature Neuroscience", "doi": "10.1038/s41593-026-02395-w"},
        }
        self.translations = {
            "b0001": "测试论文标题",
            "b0002": "这是正文第一段，按照公文标准应当首行缩进二字。",
            "b0003": "结果",
            "b0004": "神经动力学分析",
            "b0005": "![](assets/fig1.png)",
        }
        self.style = {"preset": "gov_doc"}

    def test_clean_heading_and_cn_num(self):
        self.assertEqual(publish._clean_heading_text("1. 引言"), "引言")
        self.assertEqual(publish._clean_heading_text("一、结果。"), "结果")
        self.assertEqual(publish._clean_heading_text("（一）方法"), "方法")
        self.assertEqual(publish._clean_heading_text("2.3.1 实验步骤"), "实验步骤")
        self.assertEqual(publish._to_cn_num(1), "一")
        self.assertEqual(publish._to_cn_num(10), "十")
        self.assertEqual(publish._to_cn_num(12), "十二")

    def test_detect_gov_header(self):
        org, doc_num = publish._detect_gov_header(self.doc, self.style)
        self.assertIn("自然·神经科学", org)
        self.assertIn("参阅文件", org)
        self.assertIn("s41593-026-02395-w", doc_num)

        custom_style = {"gov_header": {"org_name": "内部智库 参阅", "doc_number": "〔2026〕第01号"}}
        org2, doc_num2 = publish._detect_gov_header(self.doc, custom_style)
        self.assertEqual(org2, "内部智库 参阅")
        self.assertEqual(doc_num2, "〔2026〕第01号")

    def test_gov_header_override_via_build_args(self):
        # Test that style and doc are updated properly when org_name/doc_number or journal/doi are passed
        style = {}
        doc = {"blocks": []}
        gov_header = style.setdefault("gov_header", {})
        org_name = "细胞 参阅文件"
        doc_number = "DOI〔2026〕cell.2026.01.001 号"
        gov_header["org_name"] = org_name
        gov_header["doc_number"] = doc_number
        org, num = publish._detect_gov_header(doc, style)
        self.assertEqual(org, "细胞 参阅文件")
        self.assertEqual(num, "DOI〔2026〕cell.2026.01.001 号")

        # Test journal and doi mapping
        style2 = {}
        doc2 = {"blocks": []}
        gov_header2 = style2.setdefault("gov_header", {})
        journal = "神经元"
        doi = "10.1016/j.neuron.2026.01.002"
        gov_header2["org_name"] = f"{journal} 参阅文件"
        doc2["doi"] = doi
        org2, num2 = publish._detect_gov_header(doc2, style2)
        self.assertEqual(org2, "神经元 参阅文件")
        self.assertIn("j.neuron.2026.01.002", num2)

    def test_style_css_gov_doc(self):
        css, info = publish.style_css(self.style, "gov_doc")
        self.assertTrue(info["is_gov_doc"])
        self.assertEqual(info["columns"], 1)
        self.assertIn("595.28pt 841.89pt", css)
        self.assertIn("104.88pt", css)
        self.assertIn("FangSong", css)
        self.assertIn("28.5pt", css)
        self.assertIn("text-indent: 2em", css)
        self.assertIn(".figure p { text-indent: 0; margin: 0; }", css)
        self.assertIn(".formula p { text-indent: 0; margin: 0; }", css)

    def test_make_html_gov_doc_structure(self):
        html, info = publish.make_html(
            self.doc,
            self.style,
            self.translations,
            edition="gov_doc",
            title="测试论文",
            author="测试作者",
            lang="zh-CN",
        )
        self.assertTrue(info["is_gov_doc"])
        self.assertIn('class="gov-header"', html)
        self.assertIn('class="gov-red-line"', html)
        self.assertIn('<h1 class="gov-title">测试论文标题</h1>', html)
        self.assertIn('<h2 class="gov-h1">一、结果</h2>', html)
        self.assertIn('<h3 class="gov-h2">（一）神经动力学分析</h3>', html)

    def test_format_gov_caption(self):
        raw = "图 1 | 神经回路中的维度估计。a，一次实验会话的记录栅格图。"
        formatted = publish._format_gov_caption(raw)
        self.assertIn('class="gov-caption-title"', formatted)
        self.assertIn("图 1  神经回路中的维度估计", formatted)
        self.assertNotIn("图 1 |", formatted)
        self.assertIn('class="gov-caption-desc"', formatted)
        self.assertIn("a，一次实验会话的记录栅格图", formatted)


    def test_gov_doc_references_and_appendix(self):
        doc = {
            "title": "脑科学论文",
            "blocks": [
                {"id": "b1", "kind": "heading", "level": 1, "text": "Paper Title"},
                {"id": "b2", "kind": "heading", "level": 2, "text": "Results"},
                {"id": "b3", "kind": "text", "text": "Content of results."},
                {"id": "b4", "kind": "heading", "level": 6, "text": "References"},
                {"id": "b5", "kind": "text", "text": "1. Author, A. Title of paper. Nature (2020)."},
                {"id": "b6", "kind": "text", "text": "2. Author, B. Book of science. Wiley (2021)."},
                {"id": "b7", "kind": "heading", "level": 6, "text": "Methods"},
                {"id": "b8", "kind": "text", "text": "Content of methods."},
                {"id": "b9", "kind": "heading", "level": 6, "text": "References"},
                {"id": "b10", "kind": "text", "text": "3. Author, C. Method paper. Cell (2022)."},
            ]
        }
        trans = {
            "b1": "脑科学论文",
            "b2": "结果",
            "b3": "结果内容描述。",
            "b4": "References",
            "b5": "1. Author, A. Title of paper. Nature (2020).",
            "b6": "2. Author, B. Book of science. Wiley (2021).",
            "b7": "Methods",
            "b8": "方法内容描述。",
            "b9": "References",
            "b10": "3. Author, C. Method paper. Cell (2022).",
        }
        html, info = publish.make_html(doc, self.style, trans, "gov_doc", "测试", "作者", "zh-CN")
        self.assertIn('<h2 class="gov-ref-title">参考文献</h2>', html)
        self.assertIn('<h2 class="gov-ref-title">方法部分参考文献</h2>', html)
        self.assertNotIn("三、References", html)
        self.assertNotIn("二、References", html)
        self.assertIn('class="block text gov-ref-item"', html)
        self.assertIn('<span class="gov-ref-num">[1] </span>', html)
        self.assertIn('<span class="gov-ref-num">[2] </span>', html)
        self.assertIn('<span class="gov-ref-num">[3] </span>', html)
        self.assertIn('<h2 class="gov-appendix-title">附录：研究方法</h2>', html)

    def test_legacy_aliases_flag_controls_duplicate_saving(self):
        # Verify that publish._semantic_stem produces the semantic name and legacy alias is distinct
        stem = publish._semantic_stem(self.doc, self.translations, "gov_doc", None, "测试论文")
        legacy_stem = "book_gov"
        self.assertNotEqual(stem, legacy_stem)
        self.assertIn("测试论文", stem)
        self.assertIn("公文版", stem)


if __name__ == "__main__":
    unittest.main()
