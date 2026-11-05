from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .dataset import DEFAULT_ROOT, FLIGHTS, discover, list_dates


class DatasetSelector(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        selected = app.cfg.get("dataset", {})
        self.root_var = tk.StringVar(value=selected.get("root", DEFAULT_ROOT))
        self.date_var = tk.StringVar(value=selected.get("date", ""))
        self.flight_var = tk.StringVar(value=selected.get("flight", "1st")[0] + "회차")
        self.basis_var = tk.StringVar(value=selected.get("basis", "Panel"))
        self.events = queue.Queue()
        self.columnconfigure(1, weight=1)
        ttk.Label(self, text="데이터셋").grid(row=0, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.root_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(self, text="찾기", command=self.browse).grid(row=0, column=2)
        controls = ttk.Frame(self)
        controls.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(5, 0))
        ttk.Label(controls, text="날짜").pack(side="left")
        self.dates = ttk.Combobox(controls, textvariable=self.date_var, state="readonly", width=25)
        self.dates.pack(side="left", padx=(6, 12))
        ttk.Combobox(controls, textvariable=self.flight_var, state="readonly", width=7,
                     values=[str(i) + "회차" for i in range(1, 7)]).pack(side="left", padx=(0, 10))
        ttk.Combobox(controls, textvariable=self.basis_var, state="readonly", width=8,
                     values=("Panel", "ELC")).pack(side="left", padx=(0, 10))
        self.scan_button = ttk.Button(controls, text="경로 확인·연결", command=self.scan)
        self.scan_button.pack(side="left")
        ttk.Button(controls, text="날짜 새로고침", command=self.refresh).pack(side="left", padx=5)
        self.note = tk.StringVar(value="날짜·회차 선택 후 연결하세요.")
        ttk.Label(self, textvariable=self.note, style="Subtitle.TLabel").grid(row=2, column=0, columnspan=3, sticky="w", pady=(3, 0))
        self.refresh(silent=True)
        self.after(150, self.poll)

    def browse(self):
        path = filedialog.askdirectory(parent=self, title="데이터셋 최상위 폴더")
        if path:
            self.root_var.set(path)
            self.refresh()

    def refresh(self, silent=False):
        try:
            dates = list_dates(self.root_var.get())
            self.dates["values"] = dates
            if self.date_var.get() not in dates:
                self.date_var.set(dates[0] if dates else "")
        except (OSError, ValueError) as exc:
            self.dates["values"] = ()
            self.date_var.set("")
            self.note.set("데이터셋 드라이브·경로를 확인하세요.")
            if not silent:
                messagebox.showerror("경로 확인", str(exc), parent=self)

    def scan(self):
        if self.app.process is not None or self.app.launch_pending:
            messagebox.showwarning("실행 중", "작업 종료 후 회차를 변경하세요.", parent=self)
            return
        self.scan_button.configure(state="disabled")
        self.note.set("선택 회차의 실제 자료를 확인하고 있습니다.")
        args = (self.root_var.get(), self.date_var.get(), FLIGHTS[int(self.flight_var.get()[0])-1], self.basis_var.get())
        def worker():
            try:
                self.events.put((True, discover(*args)))
            except Exception as exc:
                self.events.put((False, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            ok, result = self.events.get_nowait()
            self.scan_button.configure(state="normal")
            if ok:
                self.note.set("경로 확인 완료 · 연결 전 내용을 확인하세요.")
                self.preview(result)
            else:
                self.note.set("경로 확인 실패")
                messagebox.showerror("경로 확인 실패", result, parent=self)
        except queue.Empty:
            pass
        self.after(150, self.poll)

    def preview(self, found):
        window = tk.Toplevel(self)
        window.title(f"{found['date']} · {found['flight'][0]}회차 · {found['basis']} 경로")
        window.geometry("1000x620")
        window.transient(self.app.root)
        window.grab_set()
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="입력 자료 및 저장 경로", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="없음·선택 필요 항목은 비워 둡니다. 이전 회차 경로를 가져오지 않습니다.",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(4, 12))
        wrap = ttk.Frame(frame)
        wrap.pack(fill="both", expand=True)
        tree = ttk.Treeview(wrap, columns=("role", "state", "path"), show="headings", height=11)
        for key, title, width in (("role", "자료", 170), ("state", "상태", 90), ("path", "경로", 630)):
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=70, stretch=key == "path")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for i, row in enumerate(found["rows"]):
            tree.insert("", "end", iid=str(i), values=(row["label"], row["status"], row["path"] or row["expected"]), tags=(row["status"],))
        tree.tag_configure("있음", foreground="#75d4bc")
        tree.tag_configure("선택 필요", foreground="#edcc82")
        detail = tk.Text(frame, height=4, wrap="word", bg="#131518", fg="#edf0f2", font=("Malgun Gothic", 10))
        detail.pack(fill="x", pady=10)
        def show(event=None):
            if not tree.selection():
                return
            row = found["rows"][int(tree.selection()[0])]
            text = row["label"] + " · " + row["status"] + "\n" + "\n".join(row["candidates"] or [row["expected"] or "자동 연결 가능한 자료 없음"])
            detail.configure(state="normal")
            detail.delete("1.0", "end")
            detail.insert("end", text)
            detail.configure(state="disabled")
        tree.bind("<<TreeviewSelect>>", show)
        detail.insert("end", "\n".join(found["warnings"]))
        detail.configure(state="disabled")
        controls = ttk.Frame(frame)
        controls.pack(fill="x")
        def apply():
            if self.app.apply_dataset(found):
                self.note.set(f"연결됨: {found['date']} · {found['flight'][0]}회차 · {found['basis']}")
                window.destroy()
        ttk.Button(controls, text="취소", command=window.destroy).pack(side="right")
        ttk.Button(controls, text="이 회차 연결", style="Primary.TButton", command=apply).pack(side="right", padx=8)
