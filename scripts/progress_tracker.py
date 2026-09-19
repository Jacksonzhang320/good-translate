"""Progress tracking engine and state synchronization for good-translate.

Maintains an atomic progress.json in the run directory, aggregates subagent
chunk translation states, and provides safe, decoupled launcher for the
desktop floating GUI.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional


PROGRESS_FILE_NAME = "progress.json"

PHASES = [
    {"key": "prepare", "name": "版面视觉解析", "weight_start": 0, "weight_end": 15},
    {"key": "split", "name": "语义大纲分块", "weight_start": 15, "weight_end": 25},
    {"key": "translate", "name": "多代理并发翻译", "weight_start": 25, "weight_end": 75},
    {"key": "patch_terms", "name": "术语闭环统一", "weight_start": 75, "weight_end": 85},
    {"key": "build", "name": "公文版编译排版", "weight_start": 85, "weight_end": 95},
    {"key": "audit_layout", "name": "质检红线门禁", "weight_start": 95, "weight_end": 100},
]

PHASE_KEYS = [p["key"] for p in PHASES]


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ProgressTracker:
    """Manages progress.json state updates and GUI lifecycle."""

    def __init__(self, run_dir: str | Path, task_name: Optional[str] = None):
        self.run_dir = Path(run_dir).resolve()
        self.progress_file = self.run_dir / PROGRESS_FILE_NAME
        self.state: Dict[str, Any] = self._load()
        if task_name and not self.state.get("task_name"):
            self.state["task_name"] = task_name
            self._save()

    def _load(self) -> Dict[str, Any]:
        if self.progress_file.is_file():
            try:
                with open(self.progress_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "schema_version": 1,
            "task_name": "",
            "status": "running",
            "current_phase": "prepare",
            "phase_title": "版面视觉解析",
            "phase_index": 1,
            "total_phases": len(PHASES),
            "overall_percent": 0,
            "detail": "初始化解析流程...",
            "chunks": [],
            "started_at": _now_iso(),
            "updated_at": _now_iso(),
            "elapsed_seconds": 0,
            "start_timestamp": time.time(),
            "outputs": {},
            "error_message": None,
        }

    def _save(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.state["updated_at"] = _now_iso()
        start_ts = self.state.get("start_timestamp")
        if start_ts:
            self.state["elapsed_seconds"] = int(time.time() - start_ts)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.run_dir,
                                         prefix=".progress.", suffix=".tmp", delete=False) as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)
            f.write("\n")
            tmp_path = Path(f.name)
        tmp_path.replace(self.progress_file)

    def set_task_name(self, name: str):
        self.state["task_name"] = name
        self._save()

    def update_phase(self, phase_key: str, detail: Optional[str] = None,
                     custom_percent: Optional[int] = None):
        """Update current active phase."""
        if phase_key not in PHASE_KEYS and phase_key != "completed":
            phase_key = "prepare"

        self.state["current_phase"] = phase_key
        if phase_key == "completed":
            self.state["phase_title"] = "全部发布完成"
            self.state["phase_index"] = len(PHASES)
            self.state["overall_percent"] = 100
            self.state["status"] = "completed"
            if detail:
                self.state["detail"] = detail
            self._save()
            return

        idx = PHASE_KEYS.index(phase_key)
        phase_info = PHASES[idx]
        self.state["phase_title"] = phase_info["name"]
        self.state["phase_index"] = idx + 1
        if custom_percent is not None:
            self.state["overall_percent"] = max(0, min(100, int(custom_percent)))
        else:
            self.state["overall_percent"] = phase_info["weight_start"]
        if detail:
            self.state["detail"] = detail
        self._save()

    def update_chunks(self, chunks_info: List[Dict[str, Any]]):
        """Update chunks status during split/translation/record.
        
        chunks_info: list of dicts with keys:
          - id (e.g. "chunk0001")
          - order (1-indexed int)
          - status ("pending", "running", "done", "bypassed")
          - chars (optional)
        """
        self.state["chunks"] = chunks_info
        # If in translate phase, compute weighted percent
        if self.state.get("current_phase") == "translate" and chunks_info:
            done_count = sum(1 for c in chunks_info if c.get("status") in ("done", "bypassed"))
            total = len(chunks_info)
            phase_info = PHASES[2]  # translate
            w_start = phase_info["weight_start"]
            w_end = phase_info["weight_end"]
            ratio = done_count / max(1, total)
            self.state["overall_percent"] = int(w_start + (w_end - w_start) * ratio)
            self.state["detail"] = f"并发翻译进度: {done_count}/{total} 分块完成 ({int(ratio*100)}%)"
        self._save()

    def set_output(self, name: str, path: str):
        if "outputs" not in self.state:
            self.state["outputs"] = {}
        self.state["outputs"][name] = str(Path(path).resolve())
        self._save()

    def complete(self, detail: str = "翻译及公文版排版全部就绪！", outputs: Optional[Dict[str, str]] = None):
        self.state["status"] = "completed"
        self.state["current_phase"] = "completed"
        self.state["phase_title"] = "全部发布完成"
        self.state["overall_percent"] = 100
        self.state["detail"] = detail
        if outputs:
            self.state.setdefault("outputs", {}).update(outputs)
        self._save()

    def fail(self, error_message: str):
        self.state["status"] = "error"
        self.state["error_message"] = str(error_message)
        self.state["detail"] = f"流程中断: {error_message}"
        self._save()

    def launch_gui(self, force: bool = False) -> bool:
        """Launch progress_gui.py in a detached, visible desktop window.

        On Windows, uses PowerShell Start-Process so the new process gets the
        interactive desktop session and the Tkinter window actually appears.
        Uses pythonw.exe to avoid a flashing black console behind the GUI.

        Returns True if process was spawned, False if skipped or errored.
        """
        script_path = Path(__file__).resolve().parent / "progress_gui.py"
        if not script_path.is_file():
            return False

        # In non-Windows headless environments, skip unless forced
        if not force and sys.platform != "win32" and not os.environ.get("DISPLAY"):
            return False

        try:
            if sys.platform == "win32":
                # Use pythonw.exe (no console) from the same venv
                venv_scripts = Path(sys.executable).parent
                pythonw = venv_scripts / "pythonw.exe"
                if not pythonw.is_file():
                    pythonw = Path(sys.executable)  # fallback to python.exe

                # Use PowerShell Start-Process so the child gets the interactive
                # desktop session (avoids "no display" when launched from a service).
                ps_cmd = [
                    "powershell.exe", "-NonInteractive", "-WindowStyle", "Hidden",
                    "-Command",
                    f'Start-Process -FilePath "{pythonw}" '
                    f'-ArgumentList "{script_path}", "--watch", "{self.progress_file}" '
                    f'-WindowStyle Normal'
                ]
                subprocess.Popen(
                    ps_cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                python_exe = sys.executable
                cmd = [python_exe, str(script_path), "--watch", str(self.progress_file)]
                subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            return True
        except Exception:
            return False

