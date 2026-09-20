"""Desktop floating HUD progress window for good-translate (Tkinter).

Zero third-party dependencies, modern dark theme, smooth progress polling,
subagent chunk status grid, and post-completion action buttons.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Dict, Optional


DARK_BG = "#181825"
CARD_BG = "#1e1e2e"
TEXT_COLOR = "#cdd6f4"
TEXT_MUTED = "#a6adc8"
ACCENT_BLUE = "#89b4fa"
ACCENT_GREEN = "#a6e3a1"
ACCENT_YELLOW = "#f9e2af"
ACCENT_RED = "#f38ba8"
BORDER_COLOR = "#313244"
TITLEBAR_BG = "#11111b"

STEP_NAMES = ["1.解析", "2.分块", "3.翻译", "4.术语", "5.排版", "6.质检"]


class ProgressHUD:
    def __init__(self, root: tk.Tk, watch_target: Optional[str] = None, demo: bool = False):
        self.root = root
        self.watch_target = Path(watch_target).resolve() if watch_target else None
        if self.watch_target and self.watch_target.is_dir():
            self.watch_target = self.watch_target / "progress.json"
        self.demo = demo
        self.is_topmost = True
        self.drag_x = 0
        self.drag_y = 0

        self.last_mtime = 0.0
        self.data: Dict[str, Any] = {}
        self.demo_start_time = time.time()

        self._init_window()
        self._build_ui()
        self._poll_data()

    def _init_window(self):
        self.root.title("good-translate v2.2 进度监控")
        self.root.overrideredirect(True)  # Frameless modern card
        self.root.configure(bg=BORDER_COLOR)

        width = 460
        height = 290
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        pos_x = max(20, screen_w - width - 35)
        pos_y = max(20, screen_h - height - 60)
        self.root.geometry(f"{width}x{height}+{pos_x}+{pos_y}")

        # Force window to appear and stay on top
        self.root.attributes("-topmost", True)
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _build_ui(self):
        # Container with 1px border
        self.card = tk.Frame(self.root, bg=CARD_BG, highlightthickness=0)
        self.card.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        # Title Bar
        self.titlebar = tk.Frame(self.card, bg=TITLEBAR_BG, height=32)
        self.titlebar.pack(fill=tk.X, side=tk.TOP)
        self.titlebar.pack_propagate(False)

        # Drag bindings
        self.titlebar.bind("<ButtonPress-1>", self._start_drag)
        self.titlebar.bind("<B1-Motion>", self._on_drag)

        # Title label
        self.lbl_app = tk.Label(self.titlebar, text=" 📄 good-translate 2.2",
                                bg=TITLEBAR_BG, fg=TEXT_COLOR, font=("Segoe UI", 9, "bold"))
        self.lbl_app.pack(side=tk.LEFT, padx=6)
        self.lbl_app.bind("<ButtonPress-1>", self._start_drag)
        self.lbl_app.bind("<B1-Motion>", self._on_drag)

        # Titlebar buttons
        btn_close = tk.Label(self.titlebar, text="✕", bg=TITLEBAR_BG, fg=TEXT_MUTED,
                             font=("Segoe UI", 9), cursor="hand2", padx=8)
        btn_close.pack(side=tk.RIGHT, fill=tk.Y)
        btn_close.bind("<Button-1>", lambda e: self.root.destroy())
        btn_close.bind("<Enter>", lambda e: btn_close.configure(bg=ACCENT_RED, fg=TITLEBAR_BG))
        btn_close.bind("<Leave>", lambda e: btn_close.configure(bg=TITLEBAR_BG, fg=TEXT_MUTED))

        self.btn_pin = tk.Label(self.titlebar, text="📌", bg=TITLEBAR_BG, fg=ACCENT_BLUE,
                                font=("Segoe UI", 9), cursor="hand2", padx=6)
        self.btn_pin.pack(side=tk.RIGHT, fill=tk.Y)
        self.btn_pin.bind("<Button-1>", self._toggle_topmost)

        # Main Content Area
        self.content = tk.Frame(self.card, bg=CARD_BG, padx=14, pady=10)
        self.content.pack(fill=tk.BOTH, expand=True)

        # Task Name
        self.lbl_task = tk.Label(self.content, text="等待任务就绪...",
                                 bg=CARD_BG, fg=TEXT_COLOR, font=("Segoe UI", 10, "bold"),
                                 anchor="w")
        self.lbl_task.pack(fill=tk.X)

        # Steps Stepper Row
        self.steps_frame = tk.Frame(self.content, bg=CARD_BG, pady=6)
        self.steps_frame.pack(fill=tk.X)
        self.step_labels = []
        for i, step_name in enumerate(STEP_NAMES):
            lbl = tk.Label(self.steps_frame, text=step_name, bg=CARD_BG, fg=TEXT_MUTED,
                           font=("Segoe UI", 8))
            lbl.pack(side=tk.LEFT, expand=True)
            self.step_labels.append(lbl)

        # Progress bar + Percentage
        pb_frame = tk.Frame(self.content, bg=CARD_BG, pady=4)
        pb_frame.pack(fill=tk.X)

        self.style = ttk.Style()
        self.style.theme_use("default")
        self.style.configure("Custom.Horizontal.TProgressbar",
                             troughcolor=DARK_BG,
                             background=ACCENT_BLUE,
                             thickness=10,
                             borderwidth=0)

        self.pbar = ttk.Progressbar(pb_frame, style="Custom.Horizontal.TProgressbar",
                                    orient="horizontal", length=340, mode="determinate")
        self.pbar.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.lbl_percent = tk.Label(pb_frame, text="0%", bg=CARD_BG, fg=ACCENT_BLUE,
                                    font=("Segoe UI", 11, "bold"), width=5, anchor="e")
        self.lbl_percent.pack(side=tk.RIGHT, padx=(6, 0))

        # Detail text
        self.lbl_detail = tk.Label(self.content, text="正在启动翻译流...",
                                   bg=CARD_BG, fg=TEXT_MUTED, font=("Segoe UI", 8),
                                   anchor="w")
        self.lbl_detail.pack(fill=tk.X, pady=(2, 6))

        # Subagent Chunk Grid Row
        self.chunks_frame = tk.Frame(self.content, bg=DARK_BG, padx=8, pady=4,
                                     highlightbackground=BORDER_COLOR, highlightthickness=1)
        self.chunks_frame.pack(fill=tk.X)
        self.chunk_widgets = []
        self.lbl_chunks_title = tk.Label(self.chunks_frame, text="分块吞吐:",
                                         bg=DARK_BG, fg=TEXT_MUTED, font=("Segoe UI", 8))
        self.lbl_chunks_title.pack(side=tk.LEFT, padx=(0, 6))

        # Bottom Bar: Elapsed & Action Buttons
        self.bottom_bar = tk.Frame(self.content, bg=CARD_BG, pady=8)
        self.bottom_bar.pack(fill=tk.X, side=tk.BOTTOM)

        self.lbl_elapsed = tk.Label(self.bottom_bar, text="⏱ 00:00",
                                    bg=CARD_BG, fg=TEXT_MUTED, font=("Segoe UI", 8))
        self.lbl_elapsed.pack(side=tk.LEFT)

        self.actions_frame = tk.Frame(self.bottom_bar, bg=CARD_BG)
        self.actions_frame.pack(side=tk.RIGHT)

        self.btn_open_folder = tk.Button(self.actions_frame, text="📂 打开目录",
                                         bg=DARK_BG, fg=TEXT_COLOR, font=("Segoe UI", 8),
                                         relief=tk.FLAT, padx=6, pady=2,
                                         command=self._on_open_folder)
        self.btn_open_pdf = tk.Button(self.actions_frame, text="📑 公文版 PDF",
                                      bg=DARK_BG, fg=ACCENT_GREEN, font=("Segoe UI", 8),
                                      relief=tk.FLAT, padx=6, pady=2,
                                      command=self._on_open_pdf)

    def _start_drag(self, event):
        self.drag_x = event.x
        self.drag_y = event.y

    def _on_drag(self, event):
        x = self.root.winfo_x() + (event.x - self.drag_x)
        y = self.root.winfo_y() + (event.y - self.drag_y)
        self.root.geometry(f"+{x}+{y}")

    def _toggle_topmost(self, event=None):
        self.is_topmost = not self.is_topmost
        self.root.attributes("-topmost", self.is_topmost)
        self.btn_pin.configure(fg=ACCENT_BLUE if self.is_topmost else TEXT_MUTED)

    def _on_open_folder(self):
        if not self.watch_target:
            return
        target_dir = self.watch_target.parent if self.watch_target.is_file() else self.watch_target
        if target_dir.exists():
            if sys.platform == "win32":
                os.startfile(str(target_dir))
            else:
                subprocess.Popen(["xdg-open", str(target_dir)])

    def _on_open_pdf(self):
        outputs = self.data.get("outputs", {})
        # Look for gov, mono, or bilingual output
        pdf_path = None
        for key in ("gov", "book", "bilingual"):
            if key in outputs:
                p = Path(outputs[key].get("path", "") if isinstance(outputs[key], dict) else outputs[key])
                if p.is_file():
                    pdf_path = p
                    break
        if not pdf_path and self.watch_target:
            # Check publish dir
            pub_dir = self.watch_target.parent / "publish"
            if pub_dir.is_dir():
                gov_pdfs = list(pub_dir.glob("*公文版*.pdf")) or list(pub_dir.glob("*.pdf"))
                if gov_pdfs:
                    pdf_path = gov_pdfs[0]
        if pdf_path and pdf_path.is_file():
            if sys.platform == "win32":
                os.startfile(str(pdf_path))
            else:
                subprocess.Popen(["xdg-open", str(pdf_path)])

    def _poll_data(self):
        if self.demo:
            self._update_demo_data()
        elif self.watch_target:
            try:
                target = Path(self.watch_target)
                if target.is_file():
                    mtime = target.stat().st_mtime
                    if mtime != self.last_mtime:
                        self.last_mtime = mtime
                        with open(target, "r", encoding="utf-8") as f:
                            self.data = json.load(f)
                        self._render_data()
                else:
                    # File doesn't exist yet — show waiting state
                    if not self.data:
                        self.lbl_detail.configure(text="⏳ 等待翻译流程启动...")
            except Exception:
                pass
        self.root.after(300, self._poll_data)

    def _render_data(self):
        # 1. Task Name
        task_name = self.data.get("task_name") or "学术论文/图书翻译"
        self.lbl_task.configure(text=task_name)

        # 2. Phase Stepper
        phase_idx = self.data.get("phase_index", 1) - 1
        is_completed = self.data.get("status") == "completed"

        for i, lbl in enumerate(self.step_labels):
            if is_completed or i < phase_idx:
                lbl.configure(text=f"✓ {STEP_NAMES[i]}", fg=ACCENT_GREEN, font=("Segoe UI", 8, "bold"))
            elif i == phase_idx:
                lbl.configure(text=f"● {STEP_NAMES[i]}", fg=ACCENT_BLUE, font=("Segoe UI", 8, "bold"))
            else:
                lbl.configure(text=STEP_NAMES[i], fg=TEXT_MUTED, font=("Segoe UI", 8))

        # 3. Percentage & Progress Bar
        pct = max(0, min(100, int(self.data.get("overall_percent", 0))))
        self.pbar["value"] = pct
        self.lbl_percent.configure(text=f"{pct}%")

        if is_completed:
            self.style.configure("Custom.Horizontal.TProgressbar", background=ACCENT_GREEN)
            self.lbl_percent.configure(fg=ACCENT_GREEN)
        elif self.data.get("status") == "error":
            self.style.configure("Custom.Horizontal.TProgressbar", background=ACCENT_RED)
            self.lbl_percent.configure(fg=ACCENT_RED)
        else:
            self.style.configure("Custom.Horizontal.TProgressbar", background=ACCENT_BLUE)
            self.lbl_percent.configure(fg=ACCENT_BLUE)

        # 4. Detail message
        detail = self.data.get("detail", "")
        self.lbl_detail.configure(text=detail)

        # 5. Elapsed
        elapsed = self.data.get("elapsed_seconds", 0)
        mins = elapsed // 60
        secs = elapsed % 60
        self.lbl_elapsed.configure(text=f"⏱ {mins:02d}:{secs:02d}")

        # 6. Chunks Grid
        chunks = self.data.get("chunks", [])
        for w in self.chunk_widgets:
            w.destroy()
        self.chunk_widgets.clear()

        if chunks:
            self.lbl_chunks_title.pack(side=tk.LEFT, padx=(0, 6))
            for c in chunks:
                st = c.get("status", "pending")
                cid = c.get("id", "")[-2:]  # Last 2 digits e.g. "01"
                if st in ("done", "bypassed"):
                    bg_color = ACCENT_GREEN
                    fg_color = TITLEBAR_BG
                    sym = "✓"
                elif st == "running":
                    bg_color = ACCENT_YELLOW
                    fg_color = TITLEBAR_BG
                    sym = cid
                else:
                    bg_color = BORDER_COLOR
                    fg_color = TEXT_MUTED
                    sym = cid

                dot = tk.Label(self.chunks_frame, text=f" {sym} ", bg=bg_color, fg=fg_color,
                               font=("Segoe UI", 7, "bold"), padx=1, pady=1)
                dot.pack(side=tk.LEFT, padx=2)
                self.chunk_widgets.append(dot)
        else:
            self.lbl_chunks_title.pack_forget()

        # 7. Action buttons upon completion
        if is_completed:
            self.btn_open_folder.pack(side=tk.LEFT, padx=2)
            self.btn_open_pdf.pack(side=tk.LEFT, padx=2)

    def _update_demo_data(self):
        """Simulate real translation pipeline progress for demo mode."""
        elapsed = int(time.time() - self.demo_start_time)
        cycle = 16
        t = elapsed % cycle

        if t < 3:
            p_idx = 1
            pct = int(t / 3 * 15)
            detail = "MinerU 神经网络视觉解析双栏版面与公式..."
            chunks = []
            status = "running"
        elif t < 5:
            p_idx = 2
            pct = 15 + int((t - 3) / 2 * 10)
            detail = "语义大纲切分完成，共划分 8 个语义完整分块"
            chunks = [{"id": f"chunk000{i+1}", "status": "pending"} for i in range(8)]
            status = "running"
        elif t < 11:
            p_idx = 3
            ratio = (t - 5) / 6
            done_cnt = int(ratio * 8)
            pct = 25 + int(ratio * 50)
            detail = f"8 子代理并发隔离翻译中: {done_cnt}/8 已完成"
            chunks = []
            for i in range(8):
                if i < done_cnt:
                    chunks.append({"id": f"chunk000{i+1}", "status": "done"})
                elif i == done_cnt:
                    chunks.append({"id": f"chunk000{i+1}", "status": "running"})
                else:
                    chunks.append({"id": f"chunk000{i+1}", "status": "pending"})
            status = "running"
        elif t < 13:
            p_idx = 4
            pct = 75 + int((t - 11) / 2 * 10)
            detail = "AST 与正则术语外科手术闭环对齐 (收敛 32 处专有名词)"
            chunks = [{"id": f"chunk000{i+1}", "status": "done"} for i in range(8)]
            status = "running"
        elif t < 15:
            p_idx = 5
            pct = 85 + int((t - 13) / 2 * 10)
            detail = "GB/T 9704 机关公文标准排版引擎渲染 PDF..."
            chunks = [{"id": f"chunk000{i+1}", "status": "done"} for i in range(8)]
            status = "running"
        else:
            p_idx = 6
            pct = 100
            detail = "✅ 终审质检红线门禁全部通过，三版本已归档！"
            chunks = [{"id": f"chunk000{i+1}", "status": "done"} for i in range(8)]
            status = "completed"

        self.data = {
            "task_name": "Bender_2026_AI_Drug_Discovery_NatureRev.pdf",
            "phase_index": p_idx,
            "overall_percent": pct,
            "status": status,
            "detail": detail,
            "chunks": chunks,
            "elapsed_seconds": elapsed,
        }
        self._render_data()


def main():
    parser = argparse.ArgumentParser(description="good-translate Floating HUD Progress Monitor")
    parser.add_argument("--watch", default=None, help="Path to progress.json or run directory")
    parser.add_argument("--demo", action="store_true", help="Run interactive visual demo")
    args = parser.parse_args()

    root = tk.Tk()
    app = ProgressHUD(root, watch_target=args.watch, demo=args.demo)
    root.mainloop()


if __name__ == "__main__":
    main()
