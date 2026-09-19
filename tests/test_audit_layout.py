import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import audit_layout


class AuditLayoutTests(unittest.TestCase):
    def test_audit_html_content_valid(self):
        html = """<!DOCTYPE html>
<html>
<head><title>Test Title</title></head>
<body>
  <div class="gov-header-org">自然·神经科学 参阅文件</div>
  <div class="gov-header-docno">DOI〔2026〕s41593-026-02395-w 号</div>
  <h1 class="title">人工智能药物研发</h1>
  <h2>一、引言</h2>
  <p>正文内容。</p>
  <h2>二、结果</h2>
  <p>实验结果。</p>
  <div class="gov-ref-section">
    <div class="gov-ref-title">参考文献</div>
    <div class="gov-ref-item"><span class="ref-index">[1]</span> Author A. Paper Title. Nature, 2026.</div>
    <div class="gov-ref-item"><span class="ref-index">[2]</span> Author B. Another Paper. Science, 2026.</div>
    <div class="gov-ref-item"><span class="ref-index">[3]</span> Author C. Third Paper. Cell, 2026.</div>
  </div>
</body>
</html>"""
        res = audit_layout.audit_html_content(html, filename="paper_公文版.html")
        self.assertEqual(res["status"], "passed")
        self.assertEqual(res["metrics"]["total_headings"], 3)
        self.assertEqual(res["metrics"]["total_references"], 3)
        self.assertEqual(res["metrics"]["ref_range"], [1, 3])
        self.assertEqual(res["issues"], [])

    def test_audit_html_content_catches_spurious_header(self):
        html = """<!DOCTYPE html>
<html>
<body>
  <h2>Perspective</h2>
  <p>Content</p>
</body>
</html>"""
        res = audit_layout.audit_html_content(html, filename="test.html")
        self.assertEqual(res["status"], "failed")
        self.assertTrue(any(issue["code"] == "spurious_running_header" for issue in res["issues"]))

    def test_audit_html_content_catches_broken_references(self):
        html = """<!DOCTYPE html>
<html>
<body>
  <div class="gov-ref-item"><span class="ref-index">[1]</span> First Ref</div>
  <div class="gov-ref-item"><span class="ref-index">[3]</span> Third Ref (missing 2)</div>
</body>
</html>"""
        res = audit_layout.audit_html_content(html, filename="test_公文版.html")
        self.assertEqual(res["status"], "failed")
        self.assertTrue(any(issue["code"] == "ref_numbering_anomaly" for issue in res["issues"]))

    def test_audit_html_content_catches_ol_downgrade(self):
        html = """<!DOCTYPE html>
<html>
<body>
  <ol>
    <li>1. First citation in wrong tag</li>
    <li>2. Second citation</li>
    <li>3. Third citation</li>
    <li>4. Fourth citation</li>
  </ol>
</body>
</html>"""
        res = audit_layout.audit_html_content(html, filename="paper_公文版.html")
        self.assertEqual(res["status"], "failed")
        self.assertTrue(any(issue["code"] == "ref_style_downgrade" for issue in res["issues"]))

    def test_audit_html_content_catches_url_in_heading(self):
        html = """<!DOCTYPE html>
<html>
<body>
  <h1>Article https://doi.org/10.1038/s41467-026-76011-7 人类大脑</h1>
</body>
</html>"""
        res = audit_layout.audit_html_content(html, filename="paper.html")
        self.assertEqual(res["status"], "failed")
        self.assertTrue(any(issue["code"] == "heading_contains_url_or_doi" for issue in res["issues"]))

    def test_audit_html_content_catches_table_caption_below(self):
        html = """<!DOCTYPE html>
<html>
<body>
  <div class="gov-header-org">自然·通讯 参阅文件</div>
  <div class="gov-header-docno">DOI〔2026〕s41467-026-76011-7 号</div>
  <section class="block table">
    <table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>
  </section>
  <section class="block caption">
    <div class="gov-caption-title">表1 实验数据表</div>
  </section>
</body>
</html>"""
        res = audit_layout.audit_html_content(html, filename="test_公文版.html")
        self.assertEqual(res["status"], "failed")
        self.assertTrue(any(issue["code"] == "table_caption_below_table" for issue in res["issues"]))

    def test_audit_html_content_catches_dot_ref_format(self):
        html = """<!DOCTYPE html>
<html>
<body>
  <div class="gov-header-org">自然 参阅文件</div>
  <div class="gov-header-docno">DOI〔2026〕s41586-026-0001-1 号</div>
  <div class="gov-ref-item">1. Author A. Nature, 2026.</div>
  <div class="gov-ref-item">2. Author B. Science, 2026.</div>
</body>
</html>"""
        res = audit_layout.audit_html_content(html, filename="test_公文版.html")
        self.assertEqual(res["status"], "failed")
        self.assertTrue(any(issue["code"] == "ref_format_not_gbt7714" for issue in res["issues"]))


if __name__ == "__main__":
    unittest.main()

