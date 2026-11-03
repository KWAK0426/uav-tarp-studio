from __future__ import annotations

import argparse
import csv
import hashlib
import json
import locale
import os
import queue
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent
APP_LAST_UPDATED_KST = "2026-10-25 · ROI 추적·간편 ELC"
PROGRESS_RE = re.compile(
    r"(?P<done>\d+)/(?P<total>\d+)\s*\((?P<pct>[\d.]+)%\).*?"
    r"(?P<rate>[\d.]+)\s*files/s,\s*ETA\s*(?P<eta>[^,{\r\n]+)",
    re.IGNORECASE,
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from uav_pipeline.config import DEFAULTS, load_config, save_config
from uav_pipeline.preprocess import tool_status
from uav_pipeline.preparation import initialize, generate, build, SENSOR
from uav_pipeline.assistant_ui import AssistantPanel
from uav_pipeline.dataset import config_from_discovery, selection_file
from uav_pipeline.dataset_ui import DatasetSelector


def _app_source_signature():
    paths = [Path(__file__).resolve(), ROOT / "launch_gui.py"]
    paths.extend(sorted((ROOT / "uav_pipeline").rglob("*.py")))
    digest = hashlib.sha256()
    for path in paths:
        if path.is_file():
            stat = path.stat()
            digest.update(f"{path.relative_to(ROOT)}|{stat.st_size}|{stat.st_mtime_ns}\n".encode("utf-8"))
    return digest.hexdigest()


def apply_studio_theme(root):
    style = ttk.Style(root)
    style.theme_use("clam")
    bg, surface, field = "#17191c", "#202327", "#131518"
    text, muted, border, accent = "#edf0f2", "#a4abb3", "#363b42", "#75d4bc"
    root.configure(background=bg)
    style.configure(".", background=bg, foreground=text, bordercolor=border,
                    font=("Malgun Gothic", 10),
                    lightcolor=border, darkcolor=border, troughcolor=field,
                    selectbackground="#31554e", selectforeground=text)
    style.configure("TFrame", background=bg)
    style.configure("TLabel", background=bg, foreground=text)
    style.configure("Title.TLabel", font=("Malgun Gothic", 20, "bold"))
    style.configure("Subtitle.TLabel", font=("Malgun Gothic", 10), foreground=muted)
    style.configure("TButton", background="#30353b", foreground=text,
                    padding=(12, 5), borderwidth=1, relief="flat")
    style.map("TButton", background=[("disabled", surface), ("pressed", "#46514e"), ("active", "#3d464b")],
              foreground=[("disabled", "#737a82")], bordercolor=[("focus", accent)])
    style.configure("Primary.TButton", background=accent, foreground="#10251e",
                    font=("Malgun Gothic", 10, "bold"), padding=(12, 5))
    style.map("Primary.TButton", background=[("disabled", surface), ("pressed", "#57b29b"), ("active", "#96e4d0")],
              foreground=[("disabled", "#737a82"), ("!disabled", "#10251e")])
    style.configure("TEntry", fieldbackground=field, foreground=text, insertcolor=text,
                    padding=(7, 4), borderwidth=1)
    style.map("TEntry", bordercolor=[("focus", accent)])
    style.configure("Path.TButton", padding=(10, 4))
    style.configure("Path.TEntry", padding=(7, 4))
    for widget in ("TCombobox", "TSpinbox"):
        style.configure(widget, fieldbackground=field, foreground=text, background=surface,
                        arrowcolor=muted, insertcolor=text, padding=(7, 4))
        style.map(widget, fieldbackground=[("readonly", field), ("!disabled", field)],
                  foreground=[("readonly", text), ("!disabled", text)])
    root.option_add("*TCombobox*Listbox.background", field)
    root.option_add("*TCombobox*Listbox.foreground", text)
    root.option_add("*TCombobox*Listbox.selectBackground", "#31554e")
    style.configure("TLabelframe", background=bg, bordercolor=border, relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=bg, foreground=muted, font=("Malgun Gothic", 10, "bold"))
    style.configure("TNotebook", background=bg, borderwidth=0, tabmargins=(0, 2, 0, 4))
    style.configure("TNotebook.Tab", background=surface, foreground=muted,
                    padding=(16, 6), font=("Malgun Gothic", 10, "bold"))
    style.map("TNotebook.Tab", background=[("selected", "#29443c"), ("active", "#30353b")],
              foreground=[("selected", accent), ("active", text)])
    style.configure("TCheckbutton", background=bg, foreground=text, indicatorbackground=field)
    style.map("TCheckbutton", background=[("active", bg)],
              indicatorbackground=[("selected", accent), ("!selected", field)])
    style.configure("Treeview", background=field, fieldbackground=field, foreground=text,
                    font=("Malgun Gothic", 10), rowheight=30, borderwidth=0)
    style.configure("Treeview.Heading", background=surface, foreground=muted,
                    font=("Malgun Gothic", 10, "bold"), relief="flat", padding=(8, 6))
    style.map("Treeview", background=[("selected", "#29443c")], foreground=[("selected", text)])
    style.map("Treeview.Heading", background=[("active", "#30353b")])
    style.configure("Vertical.TScrollbar", background="#444b53", arrowcolor=muted,
                    troughcolor=field, borderwidth=0, width=12)
    style.configure("Work.Horizontal.TProgressbar", troughcolor=field, background=accent,
                    bordercolor=border, lightcolor=accent, darkcolor=accent, thickness=12)


class PipelineGUI:
    def __init__(self, root: tk.Tk, config_path: Path, elc_selector=False):
        self.root = root
        self.config_path = config_path
        self.elc_selector = bool(elc_selector)
        self.process: subprocess.Popen | None = None
        self.background_process: subprocess.Popen | None = None
        self.background_launch_pending = False
        self.start_background_after_priority = False
        self.launch_pending = False
        self.messages: queue.Queue = queue.Queue()
        self.vars: dict[str, tk.Variable] = {}
        self.managed_path_controls = {}
        self.auto_path_controls = []
        self.edit_pre_paths = tk.BooleanVar(value=False)
        self.elc_only = tk.BooleanVar(value=False)
        self.stage_rows = {}
        self.last_failure_log: Path | None = None
        self.foreground_timing = None
        self.background_timing = None
        self.started_source_signature = _app_source_signature()
        self.file_progress_sample = None
        self.root.title("UAV Reflectance Studio · 빠른 ELC 영상 선별" if self.elc_selector
                        else "UAV Reflectance Studio · Plus 연결")
        height = min(940, self.root.winfo_screenheight() - 110)
        width = min(1580, self.root.winfo_screenwidth() - 60)
        self.root.geometry(f"{width}x{height}+20+20")
        self.root.minsize(980, 700)
        self._style()
        self.cfg = load_config(config_path) if config_path.is_file() else json.loads(json.dumps(DEFAULTS))
        self._build()
        self._load_to_form()
        if self.elc_selector:
            self._activate_elc_selector()
        self.refresh_status()
        self.root.after(120, self._poll_messages)
        self.root.after(1500, self._poll_file_progress)

    def _style(self):
        apply_studio_theme(self.root)

    def _var(self, key: str, default=""):
        if key not in self.vars:
            self.vars[key] = tk.StringVar(value=str(default))
        return self.vars[key]

    def _build(self):
        shell = ttk.Frame(self.root)
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(0, weight=1)
        outer = ttk.Frame(shell, padding=10)
        outer.grid(row=0, column=0, sticky="nsew")
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 10))
        ttk.Button(header, text="AI 도우미", command=self.toggle_assistant).pack(side="right")
        self.update_button = ttk.Button(header, text="업데이트 확인", command=self.check_for_app_update)
        self.update_button.pack(side="right", padx=(0, 6))
        ttk.Button(header, text="진행 현황 갱신", command=self.refresh_file_progress).pack(side="right", padx=(0, 6))
        title = "UAV Reflectance Studio · 빠른 ELC 영상 선별" if self.elc_selector else "UAV Reflectance Studio"
        ttk.Label(header, text=title, style="Title.TLabel").pack(anchor="w")
        self.last_updated_label = ttk.Label(
            header,
            text=f"마지막 업데이트  {APP_LAST_UPDATED_KST}",
            style="Subtitle.TLabel",
        )
        self.last_updated_label.pack(anchor="w", pady=(1, 0))

        config_bar = ttk.Frame(outer)
        config_bar.pack(fill="x", pady=(0, 9))
        ttk.Label(config_bar, text="설정 파일").pack(side="left")
        self.config_var = tk.StringVar(value=str(self.config_path))
        ttk.Entry(config_bar, textvariable=self.config_var).pack(side="left", fill="x", expand=True, padx=7)
        ttk.Button(config_bar, text="불러오기", command=self.choose_config).pack(side="left", padx=2)
        ttk.Button(config_bar, text="저장", command=self.save).pack(side="left", padx=2)
        ttk.Button(config_bar, text="출력 열기", command=self.open_output).pack(side="left", padx=(8, 2))
        ttk.Button(config_bar, text="처리시간 기록", command=self.open_timing_history).pack(side="left", padx=2)

        self.dataset_selector = DatasetSelector(outer, self)
        self.dataset_selector.pack(fill="x", pady=(0, 8))

        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True)
        self.tab_pre = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(self.tab_pre, text="1  반사도 영상 준비")
        self.tab_geo = self._scroll_tab("2  기하각과 근사 모자이크")
        self.tab_brdf = self._scroll_tab("3  필지 반사도와 BRDF")
        self._build_preprocess_tab()
        self._build_geometry_tab()
        self._build_brdf_tab()

        bottom = ttk.Panedwindow(outer, orient="horizontal")
        self.details_panel = bottom
        bottom.pack(side="bottom", before=self.tabs, fill="x", expand=False, pady=(10, 0))
        status_box = ttk.Labelframe(bottom, text="단계별 현황", padding=8)
        log_box = ttk.Labelframe(bottom, text="실행 로그", padding=8)
        bottom.add(status_box, weight=2)
        bottom.add(log_box, weight=3)
        tree_wrap = ttk.Frame(status_box)
        tree_wrap.pack(fill="both", expand=True)
        self.status_tree = ttk.Treeview(tree_wrap, columns=("stage", "state"), show="headings", height=3)
        self.status_tree.heading("stage", text="단계")
        self.status_tree.heading("state", text="현재 상태")
        self.status_tree.column("stage", width=260)
        self.status_tree.column("state", width=120, anchor="center")
        status_scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.status_tree.yview)
        self.status_tree.configure(yscrollcommand=status_scroll.set)
        self.status_tree.pack(side="left", fill="both", expand=True)
        status_scroll.pack(side="right", fill="y")
        self.log = tk.Text(log_box, height=4, wrap="word", font=("Consolas", 10), state="disabled",
                           background="#131518", foreground="#bbc4cc", insertbackground="#edf0f2",
                           selectbackground="#31554e", relief="flat", borderwidth=0,
                           padx=12, pady=10, highlightthickness=0)
        self.log.pack(fill="both", expand=True)
        controls = ttk.Frame(outer)
        controls.pack(side="bottom", before=self.tabs, fill="x", pady=(8, 0))
        progress_row=ttk.Frame(controls);progress_row.pack(fill='x')
        self.running_label = ttk.Label(progress_row, text="대기 중", width=22)
        self.running_label.pack(side="left")
        self.progress = ttk.Progressbar(progress_row, style="Work.Horizontal.TProgressbar", mode="determinate", maximum=100, length=250)
        self.progress.pack(side="left", padx=(8, 8),fill='x',expand=True)
        self.progress_detail = ttk.Label(controls, text="", style="Subtitle.TLabel",wraplength=850)
        self.progress_detail.pack(fill='x')
        control_actions=ttk.Frame(controls);control_actions.pack(fill='x',pady=(3,0))
        ttk.Button(control_actions, text="현황 새로고침", command=self.refresh_status).pack(side="right", padx=2)
        self.ask_failure_button = ttk.Button(control_actions, text="AI에 오류 묻기", command=self.ask_about_failure, state="disabled")
        self.ask_failure_button.pack(side="right", padx=2)
        self.save_failure_button = ttk.Button(control_actions, text="실패 로그 저장", command=self.save_failure_log, state="disabled")
        self.save_failure_button.pack(side="right", padx=2)
        self.stop_button = ttk.Button(control_actions, text="현재 작업 중지", command=self.stop, state="disabled")
        self.stop_button.pack(side="right", padx=2)
        ttk.Button(control_actions, text="현황·로그 접기/펼치기", command=self.toggle_details).pack(side="right", padx=2)
        bottom.pack_forget()
        self.assistant = AssistantPanel(shell, self)
        self.assistant.grid(row=0, column=1, sticky="nsew")
        self.assistant.grid_propagate(False)
        self.assistant.pack_propagate(False)
        self.assistant.configure(width=380)
        self.assistant.grid_remove()

    def _activate_elc_selector(self):
        """Open the shared Studio in a compact candidate-selection mode."""
        self.elc_only.set(True)
        self._update_elc_only_controls()
        self.tabs.hide(2)
        self.tabs.hide(1)
        self.tabs.select(self.tab_pre)
        self.pre_steps.select(1 if self.cfg.get("dataset") else 0)
        self._append(
            "[빠른 ELC 영상 선별] 날짜·회차별 선택 결과는 동일한 studio.json과 "
            "회차 ELC_Process 폴더에 저장됩니다. 정식 스튜디오에서 그대로 이어서 사용할 수 있습니다.\n"
        )

    def toggle_details(self):
        if self.details_panel.winfo_ismapped():
            self.details_panel.pack_forget()
        else:
            self.details_panel.pack(side="bottom", before=self.tabs, fill="x", pady=(8, 0))

    def toggle_assistant(self):
        if self.assistant.winfo_ismapped():
            self.assistant.grid_remove()
        else:
            self.assistant.grid()

    def _record_failure(self, stage, code, detail=""):
        stamp = datetime.now().astimezone()
        safe_stage = re.sub(r"[^A-Za-z0-9_-]+", "_", str(stage)).strip("_") or "job"
        directory = self.config_path.parent / "Logs"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"failure_{stamp:%Y%m%d_%H%M%S}_{safe_stage}.log"
        visible_log = self.log.get("1.0", "end-1c").rstrip()
        text = (
            "UAV Reflectance Studio failure report\n"
            f"time_kst: {stamp.isoformat()}\n"
            f"stage: {stage}\n"
            f"exit_code: {code}\n"
            f"config: {self.config_path}\n"
            f"app_updated: {APP_LAST_UPDATED_KST}\n"
            + (f"detail: {detail}\n" if detail else "")
            + "\n--- execution log ---\n"
            + visible_log
            + "\n"
        )
        path.write_text(text, encoding="utf-8")
        self.last_failure_log = path
        self.save_failure_button.configure(state="normal")
        self.ask_failure_button.configure(state="normal")
        self._append(f"실패 로그 자동 저장: {path}\n")
        return path

    def save_failure_log(self):
        if not self.last_failure_log or not self.last_failure_log.is_file():
            messagebox.showinfo("실패 로그", "아직 저장할 실패 로그가 없습니다.")
            return
        destination = filedialog.asksaveasfilename(
            title="실패 로그 저장",
            initialfile=self.last_failure_log.name,
            defaultextension=".log",
            filetypes=[("실패 로그", "*.log"), ("텍스트", "*.txt")],
        )
        if destination:
            Path(destination).write_bytes(self.last_failure_log.read_bytes())
            messagebox.showinfo("실패 로그 저장", f"저장했습니다.\n{destination}")

    def ask_about_failure(self):
        if not self.last_failure_log or not self.last_failure_log.is_file():
            messagebox.showinfo("실패 로그", "먼저 실패한 작업이 있어야 합니다.")
            return
        self.assistant.grid()
        self.assistant.mode.set("work")
        self.assistant.change_mode()
        self.assistant.attach_logs.set(True)
        self.assistant.input.delete("1.0", "end")
        self.assistant.input.insert(
            "1.0",
            "방금 실패한 작업의 원인을 최근 실행 로그 기준으로 설명하고, 제가 다음에 누를 버튼이나 고칠 설정만 간단히 알려줘.",
        )
        self.assistant.input.focus_set()

    def _timing_history_path(self):
        dataset_root = str(self.cfg.get("dataset", {}).get("root", "")).strip()
        directory = Path(dataset_root) / "_Processing_History" if dataset_root else self.config_path.parent / "Logs"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / "processing_runtime_history.csv"

    def _write_timing_event(self, job, event, code=""):
        path = self._timing_history_path()
        fields = [
            "run_id", "event", "date", "flight", "basis", "stage", "background",
            "start_kst", "end_kst", "elapsed_seconds", "exit_code", "status",
            "processed_files", "expected_files", "average_files_per_second", "config",
        ]
        now = datetime.now().astimezone()
        elapsed = (now - job["started"]).total_seconds() if event == "finish" else ""
        done, total = job.get("done", ""), job.get("total", "")
        row = {
            "run_id": job["run_id"], "event": event,
            "date": self.cfg.get("dataset", {}).get("date", ""),
            "flight": self.cfg.get("dataset", {}).get("flight", ""),
            "basis": self.cfg.get("dataset", {}).get("basis", ""),
            "stage": job["stage"], "background": job["background"],
            "start_kst": job["started"].isoformat(),
            "end_kst": now.isoformat() if event == "finish" else "",
            "elapsed_seconds": round(elapsed, 3) if elapsed != "" else "",
            "exit_code": code, "status": "started" if event == "start" else ("success" if code == 0 else "failed"),
            "processed_files": done, "expected_files": total,
            "average_files_per_second": round(done / elapsed, 4) if event == "finish" and elapsed and isinstance(done, int) else "",
            "config": str(self.config_path),
        }
        new_file = not path.is_file()
        with path.open("a", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            if new_file:
                writer.writeheader()
            writer.writerow(row)

    def _begin_timing(self, stage, background=False):
        started = datetime.now().astimezone()
        job = {
            "run_id": f"{started:%Y%m%d_%H%M%S_%f}_{stage}",
            "stage": stage, "background": background, "started": started,
        }
        if background:
            self.background_timing = job
        else:
            self.foreground_timing = job
        try:
            self._write_timing_event(job, "start")
        except OSError as exc:
            self._append(f"처리시간 시작 기록 실패: {exc}\n")

    def _finish_timing(self, background, code):
        job = self.background_timing if background else self.foreground_timing
        if not job:
            return
        try:
            self._write_timing_event(job, "finish", code)
        except OSError as exc:
            self._append(f"처리시간 종료 기록 실패: {exc}\n")
        if background:
            self.background_timing = None
        else:
            self.foreground_timing = None

    def open_timing_history(self):
        path = self._timing_history_path()
        target = path if path.is_file() else path.parent
        os.startfile(str(target))

    def check_for_app_update(self):
        if _app_source_signature() == self.started_source_signature:
            messagebox.showinfo("업데이트 확인", "현재 실행 중인 앱이 최신 버전입니다.")
            return
        self.update_button.configure(text="업데이트 있음")
        if self.process is not None or self.launch_pending or self.background_process is not None or self.background_launch_pending:
            messagebox.showinfo("업데이트 대기", "새 버전이 있습니다. 현재 처리가 끝난 뒤 다시 누르면 자동으로 재시작합니다.")
            return
        if not messagebox.askyesno("업데이트 적용", "새 버전이 있습니다. 지금 앱을 자동으로 재시작할까요?"):
            return
        os.startfile(str(ROOT / "run_gui.bat"))
        self.root.after(300, self.root.destroy)

    def _scroll_tab(self, title):
        page = ttk.Frame(self.tabs)
        self.tabs.add(page, text=title)
        return self._scroll_content(page)

    def _scroll_content(self, page):
        canvas = tk.Canvas(page, background="#17191c", highlightthickness=0)
        bar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = ttk.Frame(canvas, padding=10)
        item = canvas.create_window(0, 0, anchor="nw", window=content)
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(item, width=e.width))
        content.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        def wheel(event):
            if str(event.widget).startswith(str(page)) and content.winfo_height() > canvas.winfo_height():
                canvas.yview_scroll(-int(event.delta / 120), "units")
        self.root.bind("<MouseWheel>", wheel, add="+")
        return content

    def _path_row(self, parent, row, label, key, kind="file", managed=False):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        entry = ttk.Entry(parent, textvariable=self._var(key), style="Path.TEntry")
        entry.grid(row=row, column=1, sticky="ew", padx=8, pady=2)
        command = lambda: self.browse(key, kind)
        button = ttk.Button(parent, text="찾기", style="Path.TButton", command=command)
        button.grid(row=row, column=2, pady=2)
        parent.columnconfigure(1, weight=1)
        if managed:
            self.managed_path_controls[key] = (entry, button, kind)
            self.auto_path_controls.extend((entry, button))
        return entry, button

    def _build_preprocess_tab(self):
        self.pre_steps = ttk.Notebook(self.tab_pre)
        self.pre_steps.pack(fill="both", expand=True)
        panes = []
        for title in ("01 입력·모자이크", "02 타프 위치·후보", "03 원영상 ROI·방사보정",
                      "04 촬영시간·Ed·센서", "05 ELC 계수·반사도"):
            pane = ttk.Frame(self.pre_steps, padding=10)
            self.pre_steps.add(pane, text=title)
            if len(panes) in (3, 4):
                content=self._scroll_content(pane)
                if len(panes) == 3:
                    self.integrated_canvas=content.master
                else:
                    self.easy_canvas=content.master
                panes.append(content)
            else:panes.append(pane)
        setup = ttk.Frame(panes[0])
        setup.pack(fill="both", expand=True)
        setup.columnconfigure(0, weight=1)
        setup.columnconfigure(1, weight=1)
        paths = ttk.Labelframe(setup, text="해당 날짜·회차 입력", padding=7)
        paths.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self._path_row(paths, 0, "원영상 폴더", "preparation.raw_directory", "directory", managed=True)
        self._path_row(paths, 1, "유효 영상 GPS.txt", "preparation.gps_path", managed=True)
        ttk.Checkbutton(paths, text="경로 직접 수정", variable=self.edit_pre_paths,
                        command=self._toggle_pre_paths).grid(row=2, column=1, sticky="w", padx=8)
        self._toggle_pre_paths()
        options = ttk.Frame(paths)
        options.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(options, text="센서").pack(side="left")
        ttk.Combobox(options, textvariable=self._var("preparation.sensor"), values=(SENSOR,), state="readonly", width=30).pack(side="left", padx=8)
        ttk.Label(options, text="MX Dual 10밴드 기본", style="Subtitle.TLabel").pack(side="left")
        ttk.Checkbutton(paths, text="ELC 영상만 처리 (선택 후보 → 기준·검증)",
                        variable=self.elc_only, command=self._update_elc_only_controls).grid(
                            row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        mosaic = ttk.Labelframe(setup, text="타프 위치 참조 모자이크", padding=7)
        mosaic.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        self.reference_mosaic_entry, self.reference_mosaic_browse = self._path_row(
            mosaic, 0, "대체 모자이크", "tarp_workflow.reference_mosaic"
        )
        self.reference_mosaic_find = ttk.Button(mosaic, text="대체 모자이크 찾기", command=self.connect_reference_mosaic)
        self.reference_mosaic_find.grid(row=1, column=1, sticky="w", padx=8, pady=(5, 2))
        self.mosaic_status_label = ttk.Label(mosaic, text="빈 값: 해당 회차 자동", style="Subtitle.TLabel")
        self.mosaic_status_label.grid(row=2, column=1, sticky="w", padx=8)
        ttk.Label(mosaic, text="해당 회차에 없으면 다른 회차·날짜의 패널 모자이크를 위치 참조용으로만 연결합니다.",
                  style="Subtitle.TLabel", wraplength=400).grid(row=3, column=0, columnspan=3, sticky="w", pady=(3, 0))

        locate = ttk.Labelframe(panes[1], text="1. 모자이크에서 타프 군집 위치 지정", padding=7)
        locate.pack(fill="x")
        ttk.Label(locate, text="모자이크를 확대·이동한 뒤 타프가 모인 중심과 반경을 지정합니다.").pack(anchor="w")
        ttk.Button(locate, text="앱 팝업에서 타프 위치 지정", style="Primary.TButton",
                   command=self.open_tarp_workflow_dn).pack(anchor="w", pady=(7, 2))
        candidates = ttk.Labelframe(panes[1], text="2. 현재 회차 Panel Photos 후보 선택", padding=7)
        candidates.pack(fill="x", pady=(6, 0))
        ttk.Label(candidates, text="지정한 원 전체가 들어오는 Photos만 추린 뒤 전체 선택·개별 제외하고 저장합니다.").pack(anchor="w")
        ttk.Button(candidates, text="앱 팝업에서 후보 선택 계속", command=self.open_tarp_workflow_dn).pack(anchor="w", pady=(7, 2))
        ttk.Button(candidates, text="저장된 기준 ROI 가져오기", command=self.import_tarp_snapshot).pack(anchor="w", pady=(2, 0))

        roi = ttk.Labelframe(panes[2], text="1. ROI Studio · 원영상 타프 경계 승인", padding=7)
        roi.pack(fill="x")
        ttk.Label(roi, text="선택한 촬영번호의 native 원영상에서 중심·경계를 먼저 확정합니다. 같은 화소 좌표를 이후 방사보정·Ed·센서 보정 영상에 적용합니다.").pack(anchor="w")
        ttk.Button(roi, text="ROI Studio 열기 · 02 선정 영상", style="Primary.TButton",
                   command=self.open_tarp_roi_workflow).pack(anchor="w", pady=(7, 4))
        reference = ttk.Frame(roi)
        reference.pack(fill="x")
        ttk.Label(reference, text="ROI Studio에서 승인한 ELC·Ed 기준 영상").pack(side="left")
        ttk.Entry(reference, textvariable=self._var("preparation.reference_shot"), width=20).pack(side="left", padx=8)
        priority = ttk.Labelframe(panes[2], text="2. 선택 후보 우선 처리·전체 방사보정", padding=7)
        priority.pack(fill="x", pady=(6, 0))
        ttk.Label(priority, text="빠른 ELC 확인이 필요하면 선택 후보 10밴드만 먼저 방사보정합니다. 전체 처리는 별도로 백그라운드에서 이어갑니다.").pack(anchor="w")
        actions = ttk.Frame(priority)
        actions.pack(fill="x", pady=(7, 1))
        ttk.Button(actions, text="선택 후보만 우선 방사보정", command=self.run_priority_only).pack(side="left")
        self.full_radiometric_after_priority = ttk.Button(actions, text="후보 우선 → 전체 백그라운드", style="Primary.TButton",
                                                          command=self.run_priority_then_background)
        self.full_radiometric_after_priority.pack(side="left", padx=6)
        self.full_radiometric_button = ttk.Button(actions, text="전체 방사보정만 백그라운드", command=self.run_background_radiometric)
        self.full_radiometric_button.pack(side="right")

        timing = ttk.Frame(panes[3])
        timing.pack(fill="x", pady=(0, 5))
        ttk.Label(timing, text="원영상 메타데이터").pack(side="left")
        ttk.Combobox(timing, textvariable=self._var("preparation.source_timezone"), values=("UTC", "Asia/Seoul"), state="readonly", width=13).pack(side="left", padx=6)
        ttk.Label(timing, text="→ 결과 한국시간(KST)", style="Subtitle.TLabel").pack(side="left", padx=(0, 12))
        ttk.Label(timing, text="대표 밴드").pack(side="left")
        ttk.Combobox(timing, textvariable=self._var("preparation.time_band"), values=tuple(range(1, 11)), state="readonly", width=3).pack(side="left", padx=6)
        ttk.Label(timing,text="ELC·Ed 기준영상").pack(side="left",padx=(8,4))
        ttk.Entry(timing,textvariable=self._var('preparation.reference_shot'),width=13).pack(side="left")
        time_actions = ttk.Frame(panes[3])
        time_actions.pack(fill="x", pady=(0, 7))
        ttk.Button(time_actions, text="촬영시간·태양각 CSV 생성", command=lambda: self.export_time_ed(False)).pack(side="left")
        ttk.Button(time_actions, text="시간·Ed CSV 결과 폴더", command=self.open_time_results).pack(side="left", padx=6)
        ed = ttk.Labelframe(panes[3], text="촬영시간 CSV와 실측 RAMSES Ed 연결", padding=8)
        ed.pack(fill="x")
        self.ed_csv_entry, self.ed_csv_browse = self._path_row(
            ed, 0, "RAMSES 10밴드 Ed CSV (자동)", "preparation.ed_csv"
        )
        self.ed_csv_status = ttk.Label(ed, text="회차 연결 시 RAMSES 원자료에서 자동 생성", style="Subtitle.TLabel")
        self.ed_csv_status.grid(row=1, column=1, sticky="w", padx=8, pady=(0, 3))
        self._path_row(ed, 2, "센서 gain CSV (MX Dual 공통값 기본)", "preparation.gain_csv", managed=True)
        ed_options = ttk.Frame(panes[3])
        ed_options.pack(fill="x", pady=6)
        ttk.Label(ed_options, text="Ed 허용 시간차(초)").pack(side="left")
        ttk.Entry(ed_options, textvariable=self._var("preparation.max_ed_delta_seconds"), width=6).pack(side="left", padx=6)
        ttk.Button(ed_options, text="실측 Ed 매칭", command=lambda: self.export_time_ed(True)).pack(side="left", padx=(12, 6))
        ttk.Button(ed_options, text="Ed 정규화·센서 상호교정 · 백그라운드", style="Primary.TButton", command=lambda: self.run("ed")).pack(side="right")

        integrated = ttk.Labelframe(panes[3], text="방사·센서·Ed 통합 처리", padding=7)
        integrated.pack(fill="x", pady=(3,0))
        self._path_row(integrated,0,"새 통합 결과 루트","preparation.integrated_output_root","directory")
        self.integrated_save_radiance=tk.BooleanVar(value=False)
        integrated_options=ttk.Frame(integrated);integrated_options.grid(row=1,column=0,columnspan=3,sticky="ew",pady=4)
        ttk.Checkbutton(integrated_options,text="중간 센서교정 radiance 저장",variable=self.integrated_save_radiance,
                        command=lambda:self.integrated_status.configure(text='저장 옵션 변경 · 입력·용량 재검사 필요')).pack(side="left")
        ttk.Button(integrated_options,text="입력·용량 검사",command=lambda:self.run_background_stage('integrated_preflight')).pack(side="left",padx=8)
        ttk.Button(integrated_options,text="통합 처리 실행",style="Primary.TButton",command=lambda:self.run_background_stage('integrated')).pack(side="left")
        integrated_links=ttk.Frame(integrated);integrated_links.grid(row=2,column=0,columnspan=3,sticky="w")
        ttk.Button(integrated_links,text="완료 결과 → ELC 입력 연결",command=self.connect_integrated_result).pack(side="left")
        ttk.Button(integrated_links,text="기존 단계별 입력으로 복귀",command=self.disconnect_integrated_result).pack(side="left",padx=8)
        self.integrated_status=ttk.Label(integrated,text="통합 입력 미검사",style="Subtitle.TLabel",wraplength=850)
        self.integrated_status.grid(row=3,column=0,columnspan=3,sticky="w",pady=(5,0))

        easy = ttk.Labelframe(panes[4], text="간편 ELC · 승인 ROI 사용", padding=8)
        easy.pack(fill="x", pady=(0, 8))
        self.easy_full = tk.BooleanVar(value=False)
        ttk.Checkbutton(easy, text="GPS 전체 촬영분 생성 (해제: 선정한 기준·검증 영상만)", variable=self.easy_full).pack(anchor="w")
        easy_actions = ttk.Frame(easy)
        easy_actions.pack(fill="x", pady=5)
        ttk.Button(easy_actions, text="1. 자동 보정·계수 준비", style="Primary.TButton", command=lambda:self.run_easy_elc("prepare")).pack(side="left")
        ttk.Button(easy_actions, text="2. 계수 확인·반사도 생성", command=lambda:self.run_easy_elc("finish")).pack(side="left", padx=6)
        ttk.Button(easy_actions, text="품질 보고서", command=self.open_easy_report).pack(side="left")
        ttk.Button(easy_actions, text="결과 폴더", command=self.open_easy_folder).pack(side="left", padx=6)
        self.easy_status = ttk.Label(easy, text="미실행", wraplength=850)
        self.easy_status.pack(anchor="w")
        elc = ttk.Labelframe(panes[4], text="기존 단계별 처리", padding=8)
        elc.pack(fill="x")
        self._path_row(elc, 0, "자동 생성된 밴드별 ELC a·b CSV", "preparation.coefficient_csv")
        self._path_row(elc, 1, "타프 실측 반사도 CSV", "preparation.tarp_reflectance_csv")
        self.coefficient_confirmed = tk.BooleanVar(value=False)
        ttk.Checkbutton(elc, text="계수 산출과 적용 영상의 Ed·센서 보정 기준 일치 확인", variable=self.coefficient_confirmed).grid(row=2, column=1, sticky="w", padx=8, pady=5)
        elc_actions = ttk.Frame(panes[4])
        elc_actions.pack(fill="x", pady=8)
        ttk.Button(elc_actions, text="1. ELC a·b 산출", command=lambda: self.run("elc_fit")).pack(side="left", padx=(0, 6))
        ttk.Button(elc_actions, text="2. 기준·검증 영상 우선 생성", style="Primary.TButton", command=lambda: self.run("elc_apply_priority")).pack(side="left", padx=6)
        ttk.Button(elc_actions, text="3. 검증 결과 보기", command=self.open_elc_priority_report).pack(side="left", padx=6)
        self.full_reflectance_button = ttk.Button(elc_actions, text="4. 전체 반사도 생성", command=lambda: self.run("elc_apply"))
        self.full_reflectance_button.pack(side="right")
        self.allow_gains = tk.BooleanVar(value=False)
        footer = ttk.Frame(self.tab_pre)
        footer.pack(side="bottom", before=self.pre_steps, fill="x", pady=5)
        ttk.Button(footer, text="준비 상태 확인", command=self.check_preparation).pack(side="left")
        ttk.Button(footer, text="다음 단계", command=lambda: self._move_pre_step(1)).pack(side="right")
        ttk.Button(footer, text="이전 단계", command=lambda: self._move_pre_step(-1)).pack(side="right", padx=6)

    def _toggle_pre_paths(self):
        manual = self.edit_pre_paths.get()
        for key, (entry, button, kind) in self.managed_path_controls.items():
            value = self._var(key).get().strip()
            path = Path(value) if value else None
            resolved = bool(value) and (
                key == "output.directory"
                or (kind == "directory" and path.is_dir())
                or (kind != "directory" and path.is_file())
            )
            state = "disabled" if resolved and not manual else "normal"
            entry.configure(state=state)
            button.configure(state=state)
        if hasattr(self, "ed_csv_entry"):
            self._update_ed_csv_controls()

    def connect_reference_mosaic(self):
        if not self.cfg.get("dataset"):
            messagebox.showwarning("회차 선택", "먼저 날짜·회차를 연결하세요.")
            return
        if not self.save(quiet=True):
            return
        try:
            from uav_pipeline.tarp_workflow import reference_mosaic_candidates, reference_mosaic_status
            current_status = reference_mosaic_status(self.cfg)
            if current_status["role"] == "same_flight":
                messagebox.showinfo("해당 회차 모자이크", "해당 회차 모자이크가 있어 자동으로 사용합니다.\n대체 모자이크는 선택하지 않습니다.")
                self._update_reference_mosaic_controls()
                return
            rows = reference_mosaic_candidates(self.cfg)
        except Exception as exc:
            messagebox.showwarning("모자이크 찾기", str(exc))
            return
        if not rows:
            messagebox.showinfo("모자이크 찾기", "데이터셋 안에서 패널 모자이크를 찾지 못했습니다. 오른쪽 찾기로 GeoTIFF를 직접 선택하세요.")
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("위치 참조 모자이크 선택")
        dialog.geometry("980x470")
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="해당 회차 모자이크가 없을 때만 사용합니다. 가까운 날짜·회차 순입니다.").pack(anchor="w", pady=(0, 8))
        tree = ttk.Treeview(frame, columns=("scope", "date", "flight", "file", "path"), show="headings", height=12)
        for key, label, width in (("scope", "구분", 130), ("date", "날짜", 105), ("flight", "회차", 65),
                                  ("file", "파일", 240), ("path", "경로", 390)):
            tree.heading(key, text=label)
            tree.column(key, width=width, anchor="center" if key in ("date", "flight") else "w")
        for index, row in enumerate(rows):
            tree.insert("", "end", iid=str(index), values=(row["scope"], row["date"], row["flight"][0] + "회차", Path(row["path"]).name, row["path"]))
        tree.pack(fill="both", expand=True)
        current = self._var("tarp_workflow.reference_mosaic").get().strip()
        selected = next((str(i) for i, row in enumerate(rows) if current and Path(row["path"]) == Path(current)), "0")
        tree.selection_set(selected)
        tree.focus(selected)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))
        def use_selected():
            choice = tree.selection()
            if not choice:
                return
            row = rows[int(choice[0])]
            self._var("tarp_workflow.reference_mosaic").set(row["path"])
            self.mosaic_status_label.configure(text=row["scope"] + " · 연결됨")
            self.save(quiet=True)
            self._update_reference_mosaic_controls()
            dialog.destroy()
        ttk.Button(buttons, text="취소", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="선택 연결", style="Primary.TButton", command=use_selected).pack(side="right", padx=6)
        tree.bind("<Double-1>", lambda _event: use_selected())

    def import_tarp_snapshot(self):
        if self.process is not None or self.launch_pending:
            messagebox.showwarning('실행 중', '현재 작업 종료 후 가져오세요.')
            return
        if not self.cfg.get('dataset'):
            messagebox.showwarning('회차 선택', '먼저 날짜·회차를 연결하세요.')
            return
        path = filedialog.askopenfilename(title='사이드 추적 결과의 input_snapshot.json 선택', filetypes=[('입력 스냅샷', '*.json')])
        if not path or not self.save(quiet=True): return
        if not messagebox.askyesno('원영상 ROI 가져오기', '현재 회차의 원영상과 일치하는지 확인한 뒤 별도 복사합니다.\n원영상 ROI 모드와 스냅샷의 모자이크·Photos를 연결합니다.\n반사도 검증 또는 ROI 최종 승인은 아닙니다. 계속할까요?'): return
        try:
            from uav_pipeline.tarp_handoff import import_snapshot
            self.cfg, directory = import_snapshot(self.cfg, path)
            self._load_to_form()
            self.pre_steps.select(2)
            self.save(quiet=True)
            messagebox.showinfo('기준 ROI 복사 완료', '03의 타프 ROI 설정 화면을 열면 저장된 두 기준 ROI를 불러옵니다.\n'+str(directory))
        except Exception as exc:
            messagebox.showerror('기준 ROI 가져오기 실패', str(exc))

    def open_tarp_workflow(self, page=""):
        if self.process is not None or self.launch_pending:
            messagebox.showwarning("실행 중","현재 작업 종료 후 열어주세요.");return
        if not self.cfg.get('dataset'):
            messagebox.showwarning("회차 선택","날짜·회차를 먼저 연결하세요.");return
        if not self.save(quiet=True):return
        import copy
        config=copy.deepcopy(self.cfg)
        self.launch_pending=True
        self.running_label.configure(text="타프 도구 연결 중")
        def work():
            try:
                from uav_pipeline.tarp_workflow import launch
                url=launch(config, page=page)
                self.messages.put(('tarp_ready',(url,page)))
            except Exception as exc:self.messages.put(('tarp_error',str(exc)))
        threading.Thread(target=work,daemon=True).start()

    def open_tarp_workflow_dn(self):
        self._var("tarp_workflow.boundary_mode").set("원영상 ROI")
        self.open_tarp_workflow("")

    def open_tarp_roi_workflow(self):
        if self.process or self.launch_pending or self.background_process or self.background_launch_pending:
            messagebox.showwarning("처리 중", "현재 작업 종료 후 ROI를 수정하세요.")
            return
        if self.roi_editor_open():
            messagebox.showinfo("ROI 작업창", "새 ROI Studio가 이미 열려 있습니다. 해당 창에서 계속하세요.")
            return
        if not self.save(quiet=True):
            return
        try:
            from uav_pipeline.roi_studio_bridge import Session, open_editor
            session = Session(self.config_path)
            self.roi_handoff_id = None
            self.roi_manifest_applied = None
            self.roi_handoff_path = session.handoff_path
            self.roi_config_path = self.config_path
            self.roi_editor = open_editor(self.root, self.config_path)
            self._append("[03 ROI Studio] 02 선택 후보를 연결했습니다. 프로젝트에 자동 저장합니다.\n")
        except Exception as exc:
            messagebox.showerror("ROI Studio 연결", str(exc))

    def roi_editor_open(self):
        process = getattr(self, "roi_editor", None)
        return process is not None and process.poll() is None

    def _candidate_roi_ready(self, result):
        self.pre_steps.select(2)
        self.root.after(0, self.open_tarp_roi_workflow)

    def _poll_roi_handoff(self):
        path = getattr(self, "roi_handoff_path", None)
        if path is None or not path.is_file():
            return
        try:
            from uav_pipeline.roi_studio_bridge import read_json, digest
            from uav_pipeline.elc_fit import _latest_manifest
            from uav_pipeline.preparation import locations
            data = read_json(path)
            if data.get("id") == getattr(self, "roi_handoff_id", None):
                return
            if (self.config_path != self.roi_config_path or data["date"] != self.cfg["dataset"]["date"]
                    or data["flight"] != self.cfg["dataset"]["flight"]):
                raise ValueError("ROI 전달 날짜·회차와 현재 프로젝트가 다릅니다.")
            if self.process or self.launch_pending or self.background_process or self.background_launch_pending:
                return
            manifest = _latest_manifest(locations(self.cfg)["roi"])
            if str(manifest) != data["manifest"] or digest(manifest) != data["manifest_sha256"]:
                raise ValueError("현재 승인 ROI와 전달 요청이 다릅니다.")
            if data["manifest_sha256"] != getattr(self, "roi_manifest_applied", None):
                self._use_reference_shot(data["reference_shot"], "ROI Studio 사용자 승인")
                self.coefficient_confirmed.set(False)
                self.cfg["preparation"]["coefficient_basis_confirmed"] = False
                self.cfg["preparation"]["approved_roi_manifest"] = str(manifest)
                save_config(self.config_path, self.cfg)
                self.roi_manifest_applied = data["manifest_sha256"]
                self._append("[03 → 04·05] 기준 영상·native ROI·Trim·백분위 필터 전달 완료. 기존 계수 승인은 해제합니다.\n")
            self.roi_handoff_id = data["id"]
            if data.get("step") in (3, 4):
                self.pre_steps.select(data["step"])
                self.root.deiconify()
                self.root.lift()
            self.refresh_status()
        except Exception as exc:
            self.roi_handoff_id = data.get("id") if "data" in locals() else None
            self._append(f"[ROI 전달 보류] {exc}\n")

    def _use_reference_shot(self, shot, method=""):
        self._var("preparation.reference_shot").set(shot)
        self._set_nested(self.cfg, "preparation.reference_shot", shot)
        save_config(self.config_path, self.cfg)
        self._append(f"[타프 ROI] 기준 영상 자동 선택: {shot} · {method}\n")

    def run_priority_only(self):
        self.start_background_after_priority = False
        self.run("radiometric_priority")

    def _elc_only_enabled(self):
        variable = getattr(self, "elc_only", None)
        return bool(variable and variable.get())

    def _update_elc_only_controls(self):
        if self.process is not None or self.launch_pending or self.background_process is not None or self.background_launch_pending:
            self.elc_only.set(not self.elc_only.get())
            messagebox.showwarning("처리 중", "현재 작업이 끝난 뒤 처리 범위를 변경하세요.")
            return
        state = "disabled" if self.elc_only.get() else "normal"
        for button in (self.full_radiometric_after_priority, self.full_radiometric_button, self.full_reflectance_button):
            button.configure(state=state)

    def run_priority_then_background(self):
        if self._elc_only_enabled():
            self.run_priority_only()
            return
        if self.background_process is not None or self.background_launch_pending:
            messagebox.showwarning("백그라운드 실행 중", "전체 방사보정이 이미 실행 중입니다.")
            return
        self.start_background_after_priority = True
        self.run("radiometric_priority")
        if self.process is None and not self.launch_pending:
            self.start_background_after_priority = False

    def run_background_radiometric(self):
        if self._elc_only_enabled():
            messagebox.showwarning("ELC 영상만 처리", "전체 방사보정은 이 모드에서 실행하지 않습니다.")
            return
        self.run_background_stage("radiometric")

    def run_background_stage(self, stage):
        if self.roi_editor_open():
            messagebox.showwarning("ROI 편집 중", "ROI Studio의 04·05 버튼으로 이동하거나 창을 닫은 뒤 처리를 시작하세요.")
            return
        if self._elc_only_enabled() and stage in ("radiometric", "elc_apply"):
            messagebox.showwarning("ELC 영상만 처리", "전체 영상 단계는 이 모드에서 실행하지 않습니다.")
            return
        if self.process is not None or self.launch_pending:
            messagebox.showwarning("실행 중", "현재 앞 단계가 끝난 뒤 시작하세요.")
            return
        if self.background_process is not None or self.background_launch_pending:
            messagebox.showinfo("백그라운드 실행", "현재 대용량 작업이 이미 실행 중입니다.")
            return
        if not self.save(quiet=True):
            return
        labels = {"radiometric": "전체 방사보정", "ed": "Ed·센서 교정", "elc_apply_priority": "ELC 기준·검증 우선 생성", "elc_apply": "ELC 전체 반사도 생성",
                  "integrated_preflight":"통합 입력·용량 검사","integrated":"방사·센서·Ed 통합 처리"}
        if stage in ('radiometric','ed') and self.cfg.get('preparation',{}).get('integrated_plan_file'):
            messagebox.showwarning('통합 결과 연결 중','기존 단계별 입력으로 복귀한 뒤 개별 방사보정·Ed 단계를 실행하세요.');return
        command = [sys.executable, "-m", "uav_pipeline.runner", "--config", str(self.config_path), "--stage", stage]
        if stage.startswith('integrated'):
            command=[sys.executable,'-m','uav_pipeline.integrated','--config',str(self.config_path)]
            if stage=='integrated_preflight':command.append('--prepare')
            else:
                try:
                    from uav_pipeline.integrated import load_plan
                    plan,_=load_plan(self.cfg)
                    if not messagebox.askyesno('통합 처리 실행',f"{plan['shots']:,}촬영분 / {plan['files']:,}개\n중간 radiance 저장: {'ON' if plan['save_radiance'] else 'OFF'}\n권장 여유: {plan['recommended_free_bytes']/1024**3:.2f} GiB\n새 폴더: {plan['run']}\n\n기존 원본·5·6회차 결과는 덮어쓰지 않습니다. 실행할까요?"):return
                except Exception as exc:
                    from uav_pipeline.ui_feedback import problem_text
                    messagebox.showwarning('통합 입력 검사 필요',problem_text(exc, action="04에서 '입력·용량 검사'를 다시 실행하고 현재 입력·기준 영상·Ed·gain·출력 경로를 확인하세요.", step="04 방사·센서·Ed"),parent=self.root);return
        self.background_launch_pending = True
        self._reset_progress(labels[stage] + " · 백그라운드")
        self.stop_button.configure(state="normal")
        self._append(f"\n[{labels[stage]} · 백그라운드] 시작\n")
        self._begin_timing(stage, background=True)
        threading.Thread(target=self._background_worker, args=(command, stage), daemon=True).start()

    def connect_integrated_result(self):
        if self.process is not None or self.background_process is not None or self.launch_pending or self.background_launch_pending:return
        if not self.save(quiet=True):return
        try:
            from uav_pipeline.integrated import connect_result
            directory=connect_result(self.cfg)
            self.coefficient_confirmed.set(False)
            if self.save(quiet=True):self.integrated_status.configure(text='ELC 입력 연결 · '+directory)
        except Exception as exc:
            from uav_pipeline.ui_feedback import problem_text
            messagebox.showwarning('통합 결과 확인',problem_text(exc),parent=self.root)

    def disconnect_integrated_result(self):
        if self.process is not None or self.background_process is not None or self.launch_pending or self.background_launch_pending:return
        self.cfg.setdefault('preparation',{})['integrated_plan_file']=''
        self.coefficient_confirmed.set(False)
        if self.save(quiet=True):self.integrated_status.configure(text='기존 단계별 입력 · 계수 적용 기준 재확인 필요')

    def refresh_integrated_status(self):
        try:
            from uav_pipeline.preparation import locations
            pointer=locations(self.cfg)['configs']/'integrated_latest.json'
            if not pointer.is_file():return
            plan=json.loads(Path(json.loads(pointer.read_text(encoding='utf8'))['plan']).read_text(encoding='utf8'))
            state=json.loads((Path(plan['run'])/'state.json').read_text(encoding='utf8'))
            label={'checked':'검사 통과','running':'실행 중','complete':'최종 검증 완료','failed':'실패'}.get(state['status'],state['status'])
            from uav_pipeline.integrated import settings_signature,sha
            if plan['settings_signature']!=settings_signature(self.cfg) or any(sha(self.cfg['preparation'][field])!=plan[key] for key,field in (('ed_sha256','ed_csv'),('gain_sha256','gain_csv'),('gps_sha256','gps_path'))):
                label='현재 설정 재검사 필요 · 이전 결과 '+label
            self.integrated_status.configure(text=f"{label} · {plan['files']:,}개 · 비압축 {plan['uncompressed_bytes']/1024**3:.2f} GiB · 권장 여유 {plan['recommended_free_bytes']/1024**3:.2f} GiB · 중간 저장 {'ON' if plan['save_radiance'] else 'OFF'}")
        except (OSError,ValueError,KeyError) as exc:self.integrated_status.configure(text='통합 상태 확인 필요 · '+str(exc))

    def _move_pre_step(self, delta):
        index = self.pre_steps.index(self.pre_steps.select())
        self.pre_steps.select(max(0, min(len(self.pre_steps.tabs()) - 1, index + delta)))

    def _build_geometry_tab(self):
        paths = ttk.Labelframe(self.tab_geo, text="Metashape 산출물", padding=10)
        paths.pack(fill="x")
        self._path_row(paths, 0, "영상별 ortho photos 폴더", "photos.directory", "directory", managed=True)
        self._path_row(paths, 1, "DEM GeoTIFF", "geometry.dem_path", managed=True)
        self._path_row(paths, 2, "GPS_est TXT/CSV", "geometry.gps_path", managed=True)
        self._path_row(paths, 3, "영상별 촬영시각 CSV (04에서 자동 연결)", "geometry.frame_times_csv", managed=True)
        self._path_row(paths, 4, "시간 메타데이터 원본 폴더", "geometry.frame_time_source_directory", "directory", managed=True)
        self._path_row(paths, 5, "결과 저장 폴더", "output.directory", "directory", managed=True)
        options = ttk.Frame(paths)
        options.grid(row=6, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(options, text="반사도 배율").pack(side="left")
        ttk.Entry(options, textvariable=self._var("photos.reflectance_scale", "1.0"), width=12).pack(side="left", padx=(6, 18))
        ttk.Label(options, text="GPS 좌표계").pack(side="left")
        ttk.Entry(options, textvariable=self._var("geometry.gps_crs", "EPSG:4326"), width=16).pack(side="left", padx=6)
        ttk.Label(options, text="원본 시간대").pack(side="left", padx=(14, 4))
        ttk.Entry(options, textvariable=self._var("geometry.frame_time_source_timezone", "UTC"), width=12).pack(side="left", padx=6)
        self.inputs_reviewed = tk.BooleanVar(value=False)
        ttk.Checkbutton(paths, text="입력 반사도 배율·GPS 좌표계·촬영 시간대 확인 완료",
                        variable=self.inputs_reviewed).grid(row=7, column=1, sticky="w", padx=8)
        actions = ttk.Labelframe(self.tab_geo, text="실행", padding=10)
        actions.pack(fill="x", pady=(10, 0))
        for column, (stage, text) in enumerate((("preflight", "사전점검"), ("geometry", "영상별 관측기하 생성"), ("mosaic", "최소 VZA 모자이크 생성"))):
            ttk.Button(actions, text=text, style="Primary.TButton" if stage == "preflight" else "TButton", command=lambda s=stage: self.run(s)).grid(row=0, column=column, padx=4, sticky="ew")
            actions.columnconfigure(column, weight=1)
        ttk.Label(
            actions,
            text="원시 영상의 정사투영은 Metashape에서 수행합니다. 이 탭은 내보낸 photos가 지리참조됐는지 확인하고 DEM으로 VZA·VAA·RAA를 계산합니다.",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=4, pady=(7, 0))

    def _build_brdf_tab(self):
        paths = ttk.Labelframe(self.tab_brdf, text="필지와 적합 기준", padding=10)
        paths.pack(fill="x")
        self._path_row(paths, 0, "필지 마스크 GeoJSON", "fields.mask_geojson")
        self._path_row(paths, 1, "Pixel 마스크 JSON (대안)", "fields.pixel_mask_json")
        self._path_row(paths, 2, "Pixel 마스크 기준 GeoTIFF", "fields.pixel_mask_reference_geotiff")
        self._path_row(paths, 3, "Ed 시계열 CSV (선택)", "viewer.ed_timeseries_csv")
        self._path_row(paths, 4, "Skyfish GIF (선택)", "viewer.skyfish_gif")
        row = ttk.Frame(paths)
        row.grid(row=5, column=1, sticky="w", padx=8, pady=4)
        for label, key, width in (
            ("최소 관측수", "brdf.min_observations", 7),
            ("최소 VZA 범위", "brdf.min_vza_range_deg", 8),
            ("최소 RAA 빈", "brdf.min_raa_bin_count", 7),
        ):
            ttk.Label(row, text=label).pack(side="left", padx=(0, 4))
            ttk.Entry(row, textvariable=self._var(key), width=width).pack(side="left", padx=(0, 15))
        actions = ttk.Labelframe(self.tab_brdf, text="결과 생성", padding=10)
        actions.pack(fill="x", pady=(10, 0))
        entries = (
            ("fields", "필지별 각도 반사도 CSV"),
            ("brdf", "화소별 RTLSR · NBAR"),
            ("plots", "극좌표 · 주/직교평면"),
            ("viewer", "뷰어용 PNG · CSV"),
            ("all", "후처리 전체 실행"),
        )
        for column, (stage, text) in enumerate(entries):
            ttk.Button(actions, text=text, style="Primary.TButton" if stage == "all" else "TButton", command=lambda s=stage: self.run(s)).grid(row=0, column=column, padx=4, sticky="ew")
            actions.columnconfigure(column, weight=1)
        ttk.Label(
            actions,
            text="최종 정량 해석은 quality_flag=2만 사용합니다. NNLS가 기본이며 OLS는 음수 계수와 불안정성을 확인하는 진단 결과로 함께 저장됩니다.",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, columnspan=5, sticky="w", padx=4, pady=(7, 0))

    def browse(self, key, kind):
        if key == "tarp_workflow.reference_mosaic":
            try:
                from uav_pipeline.tarp_workflow import reference_mosaic_status
                if reference_mosaic_status(self.cfg)["role"] == "same_flight":
                    messagebox.showinfo("해당 회차 모자이크", "해당 회차 모자이크가 있어 자동으로 사용합니다.")
                    return
            except Exception:
                pass
        if kind == "directory":
            value = filedialog.askdirectory()
        else:
            value = filedialog.askopenfilename(filetypes=[("지원 파일", "*.json *.csv *.txt *.tif *.tiff *.geojson"), ("모든 파일", "*.*")])
        if value:
            self.vars[key].set(value)
            if key == "tarp_workflow.reference_mosaic" and hasattr(self, "mosaic_status_label"):
                self.mosaic_status_label.configure(text="직접 선택·연결 대기")

    def choose_config(self):
        if self.process is not None or self.launch_pending or self.background_process is not None or self.background_launch_pending:
            messagebox.showwarning("실행 중", "작업 종료 후 설정을 변경하세요.")
            return
        value = filedialog.askopenfilename(filetypes=[("JSON 설정", "*.json")])
        if not value:
            return
        self.config_path = Path(value)
        self.config_var.set(value)
        self.cfg = load_config(value)
        self._load_to_form()
        self.refresh_status()

    def apply_dataset(self, found):
        if self.process is not None or self.launch_pending or self.background_process is not None or self.background_launch_pending:
            messagebox.showwarning("실행 중", "작업 종료 후 회차를 변경하세요.")
            return False
        path = Path(found["config_path"])
        try:
            if path.is_file():
                if not messagebox.askyesno("저장된 회차 설정", "이 회차에 저장된 설정이 있습니다. 수동 지정값을 유지해서 불러올까요?"):
                    return False
                cfg = load_config(path)
                identity = cfg.get("dataset", {})
                if any(identity.get(k) != found[k] for k in ("root", "date", "flight", "basis")):
                    raise ValueError("저장된 설정의 날짜·회차·기준이 다릅니다. 자동 연결하지 않습니다.")
                if not identity.get("paths", {}).get("ed_source"):
                    identity.setdefault("paths", {})["ed_source"] = found["paths"]["ed_source"]
            else:
                cfg = config_from_discovery(found)
                path.parent.mkdir(parents=True, exist_ok=True)
                save_config(path, cfg)
            initialize(cfg)
            from uav_pipeline.ramses_ed import connect as connect_ramses_ed
            connect_ramses_ed(cfg)
            generate(cfg)
            save_config(path, cfg)
            self.cfg = cfg
            self.config_path = path
            self.config_var.set(str(path))
            self._load_to_form()
            marker = selection_file()
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"config_path": str(path)}, ensure_ascii=False), encoding="utf-8")
            self.refresh_status()
            self._append(f"[데이터셋] {found['prefix']} / {found['basis']} 연결\n" + "\n".join(found["warnings"]) + "\n")
            return True
        except Exception as exc:
            messagebox.showerror("회차 연결 실패", str(exc))
            return False

    @staticmethod
    def _get_nested(data, dotted, default=""):
        value = data
        for key in dotted.split("."):
            value = value.get(key, {}) if isinstance(value, dict) else default
        return default if isinstance(value, dict) else value

    @staticmethod
    def _set_nested(data, dotted, value):
        keys = dotted.split(".")
        cursor = data
        for key in keys[:-1]:
            cursor = cursor.setdefault(key, {})
        cursor[keys[-1]] = value

    def _load_to_form(self):
        initialize(self.cfg)
        self.elc_only.set(self.elc_selector or bool(self.cfg.get("preparation", {}).get("elc_only", False)))
        self._update_elc_only_controls()
        if self.cfg.get('dataset'):
            from uav_pipeline.tarp_workflow import defaults
            from uav_pipeline.time_ed import connect_prepared_time_csv
            defaults(self.cfg)
            if connect_prepared_time_csv(self.cfg):
                save_config(self.config_path, self.cfg)
        self.edit_pre_paths.set(False)
        self._toggle_pre_paths()
        self.pre_steps.select(1 if self.elc_selector and self.cfg.get("dataset") else 0)
        for key, variable in self.vars.items():
            variable.set(str(self._get_nested(self.cfg, key, "" if key.startswith(("preparation.","tarp_workflow.")) else variable.get())))
        self._toggle_pre_paths()
        if hasattr(self, "ed_csv_entry"):
            self._update_ed_csv_controls()
        if hasattr(self, "mosaic_status_label"):
            self._update_reference_mosaic_controls()
        self.coefficient_confirmed.set(bool(self.cfg.get("preparation", {}).get("coefficient_basis_confirmed", False)))
        self.integrated_save_radiance.set(bool(self.cfg.get('preparation',{}).get('integrated_save_radiance',False)))
        self.refresh_integrated_status()
        self.allow_gains.set(bool(self.cfg["preprocess"].get("allow_nonunity_sensor_gains", False)))
        self.inputs_reviewed.set(bool(self.cfg.get("dataset", {}).get("input_settings_reviewed", False)))
        if hasattr(self, "dataset_selector") and self.cfg.get("dataset"):
            d = self.cfg["dataset"]
            self.dataset_selector.root_var.set(d["root"])
            self.dataset_selector.date_var.set(d["date"])
            self.dataset_selector.flight_var.set(d["flight"][0] + "회차")
            self.dataset_selector.basis_var.set(d["basis"])
            self.dataset_selector.refresh(silent=True)

    def save(self, quiet=False):
        try:
            for key, variable in self.vars.items():
                value = variable.get().strip()
                if key in {"photos.reflectance_scale", "brdf.min_vza_range_deg"}:
                    value = float(value)
                elif key in {"brdf.min_observations", "brdf.min_raa_bin_count"}:
                    value = int(value)
                self._set_nested(self.cfg, key, value)
            self.cfg["preprocess"]["allow_nonunity_sensor_gains"] = bool(self.allow_gains.get())
            if "dataset" in self.cfg:
                self.cfg["dataset"]["input_settings_reviewed"] = self.inputs_reviewed.get()
                self.cfg["preparation"]["coefficient_basis_confirmed"] = self.coefficient_confirmed.get()
                self.cfg["preparation"]["elc_only"] = self.elc_only.get()
                self.cfg["preparation"]["integrated_save_radiance"] = self.integrated_save_radiance.get()
                generate(self.cfg)
            requested = Path(self.config_var.get())
            requested.parent.mkdir(parents=True, exist_ok=True)
            save_config(requested, self.cfg)
            self.config_path = requested
            self.refresh_status()
            self.refresh_integrated_status()
            if not quiet:
                self._append("설정을 저장했습니다.\n")
            return True
        except Exception as exc:
            messagebox.showerror("설정 저장 실패", str(exc))
            return False

    def open_time_results(self):
        from uav_pipeline.preparation import locations
        try:
            p = locations(self.cfg)['base']
            directory = p / p.name.replace('_00_ELC_Process', '_03_Time_Ed')
            if not directory.is_dir():
                raise ValueError('아직 생성된 시간·Ed CSV가 없습니다.')
            os.startfile(str(directory))
        except Exception as exc:
            messagebox.showwarning('시간·Ed 결과', str(exc))

    def open_elc_priority_report(self):
        from uav_pipeline.preparation import locations
        from uav_pipeline.elc_fit import _latest_manifest
        try:
            report = locations(self.cfg)["coef"] / "elc_priority_reference_validation.html"
            if not report.is_file():
                raise ValueError("기준·검증 영상 우선 생성을 완료하면 결과를 볼 수 있습니다.")
            inputs = [_latest_manifest(locations(self.cfg)["roi"]), Path(self.cfg["preparation"]["coefficient_csv"])]
            if any(path.stat().st_mtime > report.stat().st_mtime for path in inputs):
                raise ValueError("승인 ROI 또는 ELC 계수가 바뀌었습니다. 기준·검증 영상을 다시 생성하세요.")
            os.startfile(str(report))
        except Exception as exc:
            messagebox.showwarning("ELC 우선 검증", str(exc))

    def export_time_ed(self, with_ed):
        if self.process is not None or self.launch_pending:
            messagebox.showwarning('실행 중', '현재 작업이 끝난 뒤 실행하세요.')
            return
        if not self.cfg.get('dataset'):
            messagebox.showwarning('회차 선택', '먼저 날짜·회차를 연결하세요.')
            return
        if not self.save(quiet=True):
            return
        command = [sys.executable, '-m', 'uav_pipeline.time_ed', '--config', str(self.config_path)]
        if with_ed:
            command.append('--with-ed')
        self.time_ed_result = None
        self.launch_pending = True
        self.running_label.configure(text='실측 Ed 매칭 중' if with_ed else '촬영시간·태양각 추출 중')
        self.stop_button.configure(state='normal')
        self._begin_timing('time_ed_with_ed' if with_ed else 'time_solar')
        threading.Thread(target=self._worker, args=(command, 'time_ed'), daemon=True).start()

    def check_preparation(self):
        if not self.cfg.get("dataset"):
            messagebox.showinfo("회차 선택", "먼저 날짜·회차를 연결하세요.")
            return
        if self.save(quiet=True):
            _, issues, _ = build(self.cfg)
            messagebox.showinfo("준비 상태", "\n\n".join(label + "\n" + ("\n".join(issues[stage]) or "기본 자료 연결 완료 · 실행 시 상세 검사") for stage, label in (("radiometric", "1. 방사보정"), ("ed", "2. Ed·센서 교정"), ("elc_apply", "3. ELC 반사도"))))

    def refresh_easy_status(self):
        from uav_pipeline.easy_elc import pointer_path, read_json
        try:
            record = read_json(pointer_path(self.cfg))
            labels = {"running":"실행 중 또는 중단됨 · 로그 확인", "awaiting_review":"계수 준비 완료 · 적용 승인 대기",
                      "complete":"반사도·품질 보고서 완료", "failed":"중단 · 보고서·로그 확인"}
            label = labels.get(record["status"], record["status"])
            phases = {"time":"촬영시간", "integrated_preflight":"Ed·입력·용량 검사", "radiometric_sensor_ed":"방사·센서·Ed 보정",
                      "elc_fit":"ELC 계수 산출", "independent_validation":"독립 검증", "full_reflectance":"전체 반사도 생성"}
            if record["status"] == "running" and (self.process or self.launch_pending):
                label = "처리 중 · " + phases.get(record["phase"], record["phase"])
            self.easy_status.configure(text=label)
        except (OSError, ValueError, KeyError):
            self.easy_status.configure(text="미실행")

    def open_easy_report(self):
        from uav_pipeline.easy_elc import pointer_path, read_json
        try:
            path = Path(read_json(pointer_path(self.cfg))["report"])
            if not path.is_file():
                raise ValueError("아직 품질 보고서가 없습니다.")
            os.startfile(str(path))
        except (OSError, ValueError, KeyError) as exc:
            messagebox.showinfo("간편 ELC", str(exc))

    def open_easy_folder(self):
        from uav_pipeline.easy_elc import pointer_path, read_json
        try:
            path = Path(read_json(pointer_path(self.cfg))["config"]).parent
            os.startfile(str(path))
        except (OSError, ValueError, KeyError) as exc:
            messagebox.showinfo("간편 ELC", str(exc))

    def run_easy_elc(self, action):
        if self.process or self.background_process or self.launch_pending or self.background_launch_pending or self.roi_editor_open():
            messagebox.showwarning("처리 중", "현재 처리·ROI 편집을 마친 뒤 실행하세요.")
            return
        if not self.save(quiet=True):
            return
        from uav_pipeline.easy_elc import pointer_path, read_json, digest
        command = [sys.executable, "-m", "uav_pipeline.easy_elc", "--config", str(self.config_path), "--action", action]
        try:
            if action == "prepare":
                full = self.easy_full.get()
                if full and self._elc_only_enabled():
                    raise ValueError("ELC 영상만 처리 모드에서는 전체 생성을 선택할 수 없습니다.")
                scope = ("등록된 전체 촬영분" if self.cfg.get("general", {}).get("enabled") else "GPS 전체 촬영분") if full else "02 선정 기준·검증 촬영분"
                if not messagebox.askyesno("간편 ELC 시작", f"{scope}을 새 실행 폴더에 저장합니다.\n"
                        "촬영시간 → 방사·센서·Ed 보정 → ELC 계수까지 진행합니다.\n"
                        "중간 radiance 저장 옵션은 04 설정을 따르며 기존 결과는 변경하지 않습니다.\n"
                        "실행 전 용량 검사 후, 계수 산출 완료 시 승인을 기다립니다. 계속할까요?"):
                    return
                if full:
                    command.append("--full")
            else:
                record = read_json(pointer_path(self.cfg))
                if record["status"] != "awaiting_review":
                    raise ValueError("먼저 1번으로 계수를 준비하세요. 중단된 실행은 새 실행으로 다시 시작하세요.")
                config_hash = digest(record["config"])
                os.startfile(record["report"])
                if not messagebox.askyesno("계수·적용 기준 승인", "열린 보고서에서 타프·밴드별 계수와 기준을 확인했나요?\n"
                        "확인하면 같은 실행의 Ed·센서 보정 영상에 해당 계수를 적용합니다.\n"
                        "독립 검증 영상·품질 보고서를 생성하고, 전체를 선택했다면 나머지도 생성합니다.\n"
                        "처리 완료는 품질 합격을 뜻하지 않습니다. 계속할까요?"):
                    return
                command += ["--approved-config-sha256", config_hash]
        except (OSError, ValueError, KeyError) as exc:
            messagebox.showwarning("간편 ELC 확인", str(exc))
            return
        self.launch_pending = True
        self._reset_progress("간편 ELC · " + action)
        self.stop_button.configure(state="normal")
        self._begin_timing("easy_elc")
        threading.Thread(target=self._worker, args=(command, "easy_elc"), daemon=True).start()

    def run(self, stage):
        if self.roi_editor_open():
            messagebox.showwarning("ROI 편집 중", "ROI Studio의 04·05 버튼으로 이동하거나 창을 닫은 뒤 처리를 시작하세요.")
            return
        if self._elc_only_enabled() and stage in ("radiometric", "elc_apply", "geometry", "mosaic", "fields", "brdf", "plots", "viewer", "all"):
            messagebox.showwarning("ELC 영상만 처리", "전체 영상·필지·BRDF 단계는 이 모드에서 실행하지 않습니다.")
            return
        if stage in {"ed", "elc_apply_priority", "elc_apply"}:
            self.run_background_stage(stage)
            return
        if self.process is not None or self.launch_pending or self.background_process is not None or self.background_launch_pending:
            messagebox.showwarning("실행 중", "현재 작업이 끝난 뒤 실행하세요.")
            if stage != "radiometric_priority":
                self.start_background_after_priority = False
            return
        if self.cfg.get("dataset") and stage not in {"preflight", "radiometric_priority", "radiometric", "ed", "elc_fit", "elc_apply_priority", "elc_apply", "elc_gui"} and not self.inputs_reviewed.get():
            messagebox.showwarning("입력 기준 확인", "기하각 탭에서 반사도 배율·GPS 좌표계·촬영 시간대를 확인한 뒤 체크하세요.")
            return
        if not self.save(quiet=True):
            return
        self.launch_pending = True
        command = [sys.executable, "-m", "uav_pipeline.runner", "--config", str(self.config_path), "--stage", stage]
        self._reset_progress("ELC 후보 우선 방사보정" if stage == "radiometric_priority" else f"실행 중: {stage}")
        self.stop_button.configure(state="normal")
        self._append(f"\n[{stage}] 시작\n")
        self._begin_timing(stage)
        threading.Thread(target=self._worker, args=(command, stage), daemon=True).start()

    def _worker(self, command, stage):
        try:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            env["PYTHONIOENCODING"] = "utf-8"
            encoding = "utf-8"
            self.process = subprocess.Popen(
                command, cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding=encoding, errors="replace", bufsize=1,
            )
            for line in self.process.stdout:
                self.messages.put(("log", line))
                if stage == 'time_ed' and line.startswith('{'):
                    try:
                        result = json.loads(line)
                        if 'images' in result:
                            self.messages.put(('time_ed_result', result))
                    except ValueError:
                        pass
            code = self.process.wait()
            self.messages.put(("done", (stage, code)))
        except Exception as exc:
            self.messages.put(("error", str(exc)))

    def _background_worker(self, command, stage):
        try:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            env["PYTHONIOENCODING"] = "utf-8"
            self.background_process = subprocess.Popen(
                command, cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            self.background_launch_pending = False
            for line in self.background_process.stdout:
                self.messages.put(("background_log", line))
            code = self.background_process.wait()
            self.messages.put(("background_done", (stage, code)))
        except Exception as exc:
            self.messages.put(("background_error", (stage, str(exc))))

    def _reset_progress(self, label):
        self.progress.configure(value=0, maximum=100)
        self.progress_detail.configure(text="준비 중")
        self.running_label.configure(text=label)

    def _update_progress(self, line, background=False):
        match = PROGRESS_RE.search(line)
        if not match:
            return
        done, total = int(match["done"]), int(match["total"])
        timing = self.background_timing if background else self.foreground_timing
        if timing is not None:
            timing.update(done=done, total=total)
        pct, rate = float(match["pct"]), float(match["rate"])
        self.progress.configure(maximum=100, value=min(100, pct))
        finish = ""
        if rate > 0 and total >= done:
            finish_at = datetime.now().astimezone() + timedelta(seconds=(total - done) / rate)
            finish = f" · 종료 예상 {finish_at:%H:%M}"
        self.running_label.configure(text=("백그라운드 처리 중" if background else "처리 중"))
        self.progress_detail.configure(text=f"{done:,}/{total:,} · {pct:.1f}% · {rate:.2f}개/초 · 남은 {match['eta'].strip()}{finish}")

    @staticmethod
    def _raster_count(directory):
        if not directory.is_dir():
            return 0
        return sum(1 for path in directory.rglob("*") if path.is_file() and path.suffix.lower() in (".tif", ".tiff"))

    def refresh_file_progress(self):
        if not self.cfg.get("dataset"):
            return
        active = self.foreground_timing or self.background_timing
        active_stage = active["stage"] if active else None
        stage_names = {"radiometric_priority": "방사보정", "radiometric": "방사보정",
                       "ed": "Ed·센서 교정", "elc_apply_priority": "ELC 기준·검증", "elc_apply": "ELC 반사도"}
        if (self.process is not None or self.background_process is not None) and active_stage not in stage_names:
            return
        try:
            from uav_pipeline.preparation import latest_candidate_selection, locations
            paths = locations(self.cfg)
            elc_only = bool(self.cfg.get("preparation", {}).get("elc_only"))
            qc = paths["qc"].with_name(paths["qc"].name + "_ELC_Priority") if elc_only else paths["qc"]
            frame_groups = qc / "frame_groups.csv"
            if not frame_groups.is_file():
                return
            with frame_groups.open(encoding="utf-8-sig") as stream:
                total = max(0, sum(1 for _ in stream) - 1)
            if not total:
                return
            if active_stage == "radiometric_priority" or (elc_only and active_stage == "ed"):
                _, _, shots = latest_candidate_selection(self.cfg)
                total = len(shots) * 10
                stage = stage_names[active_stage]
                target = paths["rad" if active_stage == "radiometric_priority" else "ed"]
                done = sum(1 for path in target.rglob("*")
                           if path.is_file() and path.suffix.lower() in (".tif", ".tiff")
                           and path.stem.rsplit("_", 1)[0] in shots)
                done = min(total, done)
            elif active_stage == "elc_apply_priority":
                from uav_pipeline.elc_priority import approved_shots
                _, _, _, shots = approved_shots(self.cfg)
                total = len(shots) * 10
                stage = stage_names[active_stage]
                done = sum(1 for path in paths["ref"].rglob("*")
                           if path.is_file() and path.suffix.lower() in (".tif", ".tiff")
                           and path.stem.rsplit("_", 1)[0] in shots)
                done = min(total, done)
            elif active_stage in stage_names:
                stage = stage_names[active_stage]
                target = paths[{"방사보정": "rad", "Ed·센서 교정": "ed", "ELC 반사도": "ref"}[stage]]
                done = min(total, self._raster_count(target))
            else:
                counts = {
                    "방사보정": self._raster_count(paths["rad"]),
                    "Ed·센서 교정": self._raster_count(paths["ed"]),
                    "ELC 반사도": self._raster_count(paths["ref"]),
                }
                if 0 < counts["Ed·센서 교정"] < total:
                    stage = "Ed·센서 교정"
                elif 0 < counts["방사보정"] < total:
                    stage = "방사보정"
                elif 0 < counts["ELC 반사도"] < total:
                    stage = "ELC 반사도"
                elif counts["Ed·센서 교정"] >= total and counts["ELC 반사도"] == 0:
                    stage = "Ed·센서 교정"
                else:
                    return
                done = min(total, counts[stage])
            now = datetime.now().timestamp()
            rate = 0.0
            if self.file_progress_sample and self.file_progress_sample[0] == stage:
                _, previous, sampled_at = self.file_progress_sample
                if done > previous and now > sampled_at:
                    rate = (done - previous) / (now - sampled_at)
            self.file_progress_sample = (stage, done, now)
            pct = 100 * done / total
            suffix = ""
            if rate > 0:
                seconds = (total - done) / rate
                finish = datetime.now().astimezone() + timedelta(seconds=seconds)
                suffix = f" · {rate:.2f}개/초 · 남은 {seconds / 60:.1f}분 · 종료 예상 {finish:%H:%M}"
            self.progress.configure(maximum=100, value=pct)
            self.running_label.configure(text=f"파일 기준 · {stage}")
            self.progress_detail.configure(text=f"{done:,}/{total:,} · {pct:.1f}%{suffix}")
        except (OSError, ValueError, KeyError):
            return

    def _poll_file_progress(self):
        self.refresh_file_progress()
        self.root.after(5000, self._poll_file_progress)

    def _poll_messages(self):
        self._poll_roi_handoff()
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "log":
                    self._append(value)
                    self._update_progress(value)
                    if value.startswith("EASY_STATUS "):
                        self.refresh_easy_status()
                elif kind == "background_log":
                    self._append(value)
                    self._update_progress(value, background=True)
                elif kind == 'time_ed_result':
                    self.time_ed_result = value
                elif kind == 'tarp_ready':
                    url, page = value
                    self.launch_pending = False
                    self.running_label.configure(text="타프 팝업 열림")
                    self._append("[타프 위치·ROI] 앱 내부 팝업 연결 완료\n")
                    from uav_pipeline.tarp_popup import open_candidate_popup, open_roi_popup
                    if page == "/roi.html":
                        open_roi_popup(self.root, url.rsplit("/roi.html", 1)[0], self._use_reference_shot)
                    else:
                        open_candidate_popup(self.root, url, saved=self._candidate_roi_ready)
                elif kind == "done":
                    stage, code = value
                    self._finish_timing(False, code)
                    self.process = None
                    self.launch_pending = False
                    self.running_label.configure(text="완료" if code == 0 else "실패")
                    self.stop_button.configure(state="disabled")
                    self._append(f"[{stage}] 종료 코드 {code}\n")
                    if code != 0:
                        self._record_failure(stage, code)
                    self.refresh_status()
                    if stage == "easy_elc":
                        self.refresh_easy_status()
                        if code == 0:
                            self.open_easy_report()
                        else:
                            self.easy_status.configure(text="이번 실행 실패 · 로그 확인 · 이전 결과는 유지")
                    if stage == "elc_fit" and code == 0:
                        self.load(self.config_path)
                        messagebox.showinfo("ELC 계수 산출 완료", "밴드별 L = aρ + b 계수와 검증표를 생성했습니다.\n결과를 확인한 뒤 기준 일치 확인을 체크하세요.")
                    if stage == "radiometric_priority":
                        should_continue = self.start_background_after_priority and code == 0
                        self.start_background_after_priority = False
                        if should_continue:
                            self.root.after(100, self.run_background_radiometric)
                    if stage == 'time_ed':
                        if code == 0:
                            result = self.time_ed_result or {}
                            time_csv = Path(result.get('source_time_csv') or result.get('image_time_solar_csv', ''))
                            if time_csv.is_file():
                                self._var('geometry.frame_times_csv').set(str(time_csv))
                                self._set_nested(self.cfg, 'geometry.frame_times_csv', str(time_csv))
                                save_config(self.config_path, self.cfg)
                                self._toggle_pre_paths()
                            detail = f"GPS 촬영번호: {result.get('images', '?')}개\n촬영시간 유효: {result.get('time_valid_images', result.get('valid_images', '?'))}개\n태양각 유효: {result.get('solar_valid_images', result.get('valid_images', '?'))}개"
                            if result.get('observation_start_kst'):
                                detail += f"\n관측 시작: {result['observation_start_kst']}\n관측 종료: {result['observation_end_kst']}\n관측 구간: {result['observation_duration_minutes']:.2f}분"
                                detail += f"\n중간시각 기준 후보: {result.get('midpoint_candidate_not_approved', '')} (타프 확인 후 확정)"
                            if result.get('ed_requested'):
                                detail += f"\nEd 매칭: {result.get('matched', '?')}개 / 미매칭: {result.get('unmatched', '?')}개 / 제외: {result.get('excluded_images', 0)}개"
                            messagebox.showinfo('시간·Ed CSV 생성 완료', detail + '\n\nCSV 결과 폴더에서 세부 상태를 확인하세요.')
                        elif not hasattr(self, 'action_hints'):
                            messagebox.showwarning('시간·Ed 확인 필요', '생성 중 오류가 발생했습니다. 처리 기록을 확인하세요.\n시간 CSV가 먼저 생성되었다면 결과 폴더에 보존됩니다.')
                elif kind == "background_done":
                    stage, code = value
                    self._finish_timing(True, code)
                    self.background_process = None
                    self.background_launch_pending = False
                    label = {"radiometric": "전체 방사보정", "ed": "Ed·센서 교정", "elc_apply_priority": "ELC 기준·검증 우선 생성", "elc_apply": "ELC 전체 반사도 생성", "integrated":"통합 처리", "integrated_preflight":"통합 검사"}[stage]
                    self.running_label.configure(text=label + (" 완료" if code == 0 else " 실패"))
                    self.progress.configure(value=100 if code == 0 else self.progress["value"])
                    self.progress_detail.configure(text="완료" if code == 0 else f"종료 코드 {code} · 로그 확인")
                    self.stop_button.configure(state="disabled")
                    self._append(f"[{label} · 백그라운드] 종료 코드 {code}\n")
                    if code != 0:
                        self._record_failure(stage, code)
                    self.refresh_status()
                    if stage.startswith('integrated'):self.refresh_integrated_status()
                    if stage == "elc_apply_priority" and code == 0:
                        self.open_elc_priority_report()
                elif kind == "background_error":
                    stage, error = value
                    self._finish_timing(True, "exception")
                    self.background_process = None
                    self.background_launch_pending = False
                    label = {"radiometric": "전체 방사보정", "ed": "Ed·센서 교정", "elc_apply_priority": "ELC 기준·검증 우선 생성", "elc_apply": "ELC 전체 반사도 생성", "integrated":"통합 처리", "integrated_preflight":"통합 검사"}[stage]
                    self.running_label.configure(text=label + " 실패")
                    self.progress_detail.configure(text="로그 확인")
                    self.stop_button.configure(state="disabled")
                    self._append(f"[{label} · 백그라운드] 오류: {error}\n")
                    self._record_failure(stage, "exception", error)
                else:
                    self._finish_timing(False, "exception")
                    self.process = None
                    self.launch_pending = False
                    self.start_background_after_priority = False
                    self.running_label.configure(text="실패")
                    self.stop_button.configure(state="disabled")
                    self._append(f"오류: {value}\n")
                    self._record_failure(kind, "exception", value)
                    if kind == 'tarp_error' and not hasattr(self, 'action_hints'): messagebox.showwarning('타프 도구 · 자료 확인',value)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_messages)

    def _update_reference_mosaic_controls(self):
        try:
            from uav_pipeline.tarp_workflow import reference_mosaic_status
            status = reference_mosaic_status(self.cfg)
        except Exception:
            status = {"role": "missing", "selected": ""}
        own = status.get("role") == "same_flight"
        state = "disabled" if own else "normal"
        self.reference_mosaic_entry.configure(state=state)
        self.reference_mosaic_browse.configure(state=state)
        self.reference_mosaic_find.configure(state=state)
        if own:
            self._var("tarp_workflow.reference_mosaic").set("")
            self.cfg.setdefault("tarp_workflow", {})["reference_mosaic"] = ""
            self.mosaic_status_label.configure(text="해당 회차 모자이크 자동 사용 · " + Path(status["selected"]).name)
        elif status.get("selected"):
            self.mosaic_status_label.configure(text="해당 회차 모자이크 없음 · 대체 위치 참조 연결됨")
        else:
            self.mosaic_status_label.configure(text="해당 회차 모자이크 없음 · 대체 모자이크 필요")

    def _update_ed_csv_controls(self):
        from uav_pipeline.ramses_ed import is_connected
        connected = is_connected(self.cfg)
        state = "disabled" if connected and not self.edit_pre_paths.get() else "normal"
        self.ed_csv_entry.configure(state=state)
        self.ed_csv_browse.configure(state=state)
        if connected:
            self.ed_csv_status.configure(text="자동 연결됨 · RAMSES 원자료 → MX Dual 10밴드 Ed")
        elif self.cfg.get("dataset", {}).get("paths", {}).get("ed_source"):
            self.ed_csv_status.configure(text="RAMSES 원자료 발견 · 회차를 다시 연결하면 자동 생성")
        else:
            self.ed_csv_status.configure(text="RAMSES 원자료 없음 · 필요하면 CSV를 직접 지정")

    def stop(self):
        if self.process is not None:
            self._terminate_tree(self.process)
            self._append("현재 작업과 하위 처리를 중지했습니다.\n")
        elif self.background_process is not None:
            self._terminate_tree(self.background_process)
            self._append("백그라운드 작업과 하위 처리를 중지했습니다.\n")

    @staticmethod
    def _terminate_tree(process):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            process.terminate()

    def _append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def refresh_status(self):
        for item in self.status_tree.get_children():
            self.status_tree.delete(item)
        rows = []
        for tool in tool_status(self.cfg):
            if tool["stage"] == "elc_gui":
                state = "02에서 자료 연결" if self.cfg.get("dataset") else ("준비" if tool["script_exists"] else "도구 없음")
            else:
                state = "입력 확인 필요" if tool.get("blockers") else ("기본 연결" if tool["script_exists"] and tool["config_exists"] else "설정 필요")
            rows.append((tool["title"], state))
        out = Path(self.cfg["output"].get("directory", ""))
        rows.extend(
            [
                ("5. 영상별 관측기하", "완료" if (out / "01_영상별_관측기하" / "영상별_기하와_촬영시각.csv").is_file() else "대기"),
                ("6. 최소 VZA 모자이크", "완료" if (out / "02_근사연직_모자이크" / "minimum_vza_reflectance_mosaic.tif").is_file() else "대기"),
                ("7. 필지별 각도 반사도", "완료" if (out / "03_필지별_각도반사도" / "필지별_영상별_관측반사도.csv").is_file() else "대기"),
                ("8. 화소별 BRDF · NBAR", "완료" if (out / "04_화소별_BRDF_파라미터" / "brdf_model_definition.json").is_file() else "대기"),
                ("9. 뷰어용 경량자료", "완료" if (out / "06_뷰어용_경량자료" / "viewer_manifest.json").is_file() else "대기"),
            ]
        )
        for title, state in rows:
            self.status_tree.insert("", "end", values=(title, state), tags=(state,))
        self.status_tree.tag_configure("완료", foreground="#75d4bc")
        self.status_tree.tag_configure("준비", foreground="#75d4bc")
        self.status_tree.tag_configure("설정 필요", foreground="#edcc82")
        self.status_tree.tag_configure("도구 없음", foreground="#ef929b")
        self.status_tree.tag_configure("대기", foreground="#a4abb3")
        self.refresh_easy_status()

    def open_output(self):
        path = Path(self.vars.get("output.directory", tk.StringVar(value="")).get())
        if path.exists():
            os.startfile(path)
        else:
            messagebox.showinfo("출력 폴더", "아직 출력 폴더가 없습니다.")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--elc-selector", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args(argv)
    config_path = Path(args.config) if args.config else ROOT / "example_config.json"
    if not args.config and selection_file().is_file():
        try:
            saved = Path(json.loads(selection_file().read_text(encoding="utf-8"))["config_path"])
            if saved.is_file():
                config_path = saved
        except (OSError, ValueError, KeyError):
            pass
    root = tk.Tk()
    app = PipelineGUI(root, config_path, elc_selector=args.elc_selector)
    if args.smoke_test:
        root.update_idletasks()
        print(f"GUI_SMOKE_OK {root.winfo_width()}x{root.winfo_height()} tabs={app.tabs.index('end')}")
        root.destroy()
        return 0
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit("This package exposes the ELC-only workbench. Use 01_START.bat.")
