"""Unit tests for progress_tracker.py."""
import json
from pathlib import Path
import tempfile
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from progress_tracker import ProgressTracker, PHASES, PHASE_KEYS


class TestProgressTracker(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_init_and_state(self):
        tracker = ProgressTracker(self.root, task_name="TestPaper.pdf")
        pfile = self.root / "progress.json"
        self.assertTrue(pfile.is_file())

        with open(pfile, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["task_name"], "TestPaper.pdf")
        self.assertEqual(data["status"], "running")
        self.assertEqual(data["current_phase"], "prepare")
        self.assertEqual(data["phase_index"], 1)
        self.assertEqual(data["overall_percent"], 0)

    def test_update_phase(self):
        tracker = ProgressTracker(self.root, task_name="TestPaper.pdf")
        tracker.update_phase("split", detail="大纲分块完成")

        with open(self.root / "progress.json", "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["current_phase"], "split")
        self.assertEqual(data["phase_title"], "语义大纲分块")
        self.assertEqual(data["phase_index"], 2)
        self.assertEqual(data["overall_percent"], 15)
        self.assertEqual(data["detail"], "大纲分块完成")

    def test_chunks_parallel_progress(self):
        tracker = ProgressTracker(self.root, task_name="TestPaper.pdf")
        tracker.update_phase("translate", detail="8 子代理并发翻译")

        chunks = [
            {"id": "chunk0001", "order": 1, "status": "done"},
            {"id": "chunk0002", "order": 2, "status": "done"},
            {"id": "chunk0003", "order": 3, "status": "running"},
            {"id": "chunk0004", "order": 4, "status": "pending"},
        ]
        tracker.update_chunks(chunks)

        with open(self.root / "progress.json", "r", encoding="utf-8") as f:
            data = json.load(f)

        # 2 out of 4 done -> 50% between 25 and 75 => 25 + 25 = 50%
        self.assertEqual(data["overall_percent"], 50)
        self.assertIn("2/4 分块完成", data["detail"])

    def test_completion_and_outputs(self):
        tracker = ProgressTracker(self.root, task_name="TestPaper.pdf")
        tracker.set_output("gov_pdf", str(self.root / "book_gov.pdf"))
        tracker.complete(detail="全部完成测试")

        with open(self.root / "progress.json", "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["current_phase"], "completed")
        self.assertEqual(data["overall_percent"], 100)
        self.assertEqual(data["detail"], "全部完成测试")
        self.assertIn("gov_pdf", data["outputs"])

    def test_fail_state(self):
        tracker = ProgressTracker(self.root, task_name="TestPaper.pdf")
        tracker.fail("MinerU 显存溢出")

        with open(self.root / "progress.json", "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["status"], "error")
        self.assertEqual(data["error_message"], "MinerU 显存溢出")
        self.assertIn("流程中断", data["detail"])


if __name__ == "__main__":
    unittest.main()
