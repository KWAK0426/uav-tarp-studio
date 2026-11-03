"""Desktop workspace sharing the UAV Studio charcoal and mint palette."""
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
from PIL import Image, ImageDraw, ImageFont, ImageTk

BG = "#14171b"
INK = "#edf2f7"
LINE = "#343c46"
TEAL = "#75d5b6"


def tip(widget, text):
    popup = None
    def hide(event=None):
        nonlocal popup
        if popup is not None:
            popup.destroy()
            popup = None
    def show(event):
        nonlocal popup
        hide()
        popup = tk.Toplevel(widget)
        popup.overrideredirect(True)
        popup.geometry(f"+{widget.winfo_rootx()}+{widget.winfo_rooty()+widget.winfo_height()+4}")
        tk.Label(popup, text=text, bg=INK, fg="white", padx=9, pady=6, font=("Malgun Gothic", 10)).pack()
    widget.bind("<Enter>", show)
    widget.bind("<Leave>", hide)
    widget.bind("<ButtonPress>", hide, add=True)


class ROIList:
    """Keep existing editor selection callbacks on the grouped Treeview."""
    def __init__(self, app, tree):
        self.app, self.tree, self.count = app, tree, 0

    def delete(self, *args):
        self.tree.delete(*self.tree.get_children())
        self.count = 0
        for key, text in (("calibration", "●  계수 산출용"), ("validation", "●  검증용")):
            self.tree.insert("", "end", iid=key, text=text, open=True, tags=(key,))

    def insert(self, where, text):
        index = self.count
        self.count += 1
        record = self.app.records[index]
        role = record.get("role", "calibration")
        if role not in ("calibration", "validation"):
            role = "calibration"
        parent = role+"_image"
        if not self.tree.exists(parent):
            self.tree.insert(role, "end", iid=parent, text=self.app.raster.path.name, open=True)
        mark = "✓" if record.get("reviewed") and record.get("pixel_reviewed") else "○"
        self.tree.insert(parent, "end", iid=f"roi_{index}", text=mark+"  "+text)

    def curselection(self):
        selected = self.tree.selection()
        return (int(selected[0][4:]),) if selected and selected[0].startswith("roi_") else ()

    def selection_set(self, index):
        self.tree.selection_set(f"roi_{index}")


def build(a):
    root = a.root
    root.configure(bg=BG)
    style = ttk.Style(root)
    style.theme_use("clam")
    font = ("Malgun Gothic", 11)
    style.configure(".", font=font, background=BG, foreground=INK)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=INK, font=font)
    style.configure("Head.TLabel", font=("Malgun Gothic", 12, "bold"))
    style.configure("TButton", background="#2c353f", foreground=INK, padding=(10, 7), borderwidth=1, bordercolor=LINE)
    style.map("TButton", background=[("active", "#3a4958")], foreground=[("disabled", "#78838f")])
    style.configure("Accent.TButton", background=TEAL, foreground=BG, font=("Malgun Gothic", 11, "bold"))
    style.map("Accent.TButton", background=[("active", "#a0e8cf"), ("disabled", "#34483f")])
    style.configure("TEntry", fieldbackground="#11161b", foreground=INK, insertcolor=INK, padding=4, bordercolor=LINE)
    style.configure("TCombobox", fieldbackground="#11161b", foreground=INK, padding=4, arrowcolor=INK)
    style.map("TCombobox", fieldbackground=[("readonly", "#11161b")], foreground=[("readonly", INK)], selectbackground=[("readonly", "#31564d")])
    style.configure("TSpinbox", fieldbackground="#11161b", foreground=INK, insertcolor=INK, arrowcolor=INK, padding=4)
    style.configure("Treeview", background="#1c2127", fieldbackground="#1c2127", foreground=INK, rowheight=31, bordercolor=LINE, font=font)
    style.configure("Treeview.Heading", background="#2c353f", foreground=INK, padding=6, font=("Malgun Gothic", 10, "bold"))
    style.configure("Stats.Treeview", rowheight=26)
    style.map("Treeview", background=[("selected", "#31564d")], foreground=[("selected", INK)])
    style.configure("TNotebook", borderwidth=0, background=BG)
    style.configure("TNotebook.Tab", padding=(18, 6))
    style.map("TNotebook.Tab", background=[("selected", "#1c2127")], foreground=[("selected", TEAL)])
    style.configure("TCheckbutton", background=BG, foreground=INK)
    style.configure("TRadiobutton", background=BG, foreground=INK)
    style.map("TCheckbutton", background=[("active", BG)])
    style.map("TRadiobutton", background=[("active", BG)])
    style.configure("Horizontal.TScale", background=BG, troughcolor="#343c46")
    style.configure("TScrollbar", background="#2c353f", troughcolor="#11161b", arrowcolor=INK)
    style.configure("Horizontal.TProgressbar", background=TEAL, troughcolor="#343c46", borderwidth=0, thickness=3)
    root.option_add("*TCombobox*Listbox.background", "#1c2127")
    root.option_add("*TCombobox*Listbox.foreground", INK)
    for row in range(8):
        root.rowconfigure(row, weight=0)
    root.rowconfigure(2, weight=1)
    title = tk.Frame(root, bg="#1c2127", height=40)
    title.grid(row=0, column=0, sticky="ew")
    tk.Label(title, text="Tarp ROI Studio", font=("Segoe UI", 15, "bold"), bg="#1c2127", fg=INK).pack(side="left", padx=16, pady=7)
    tk.Label(title, text="UAV Studio  /  ROI workspace  ·  1.4.0", font=("Segoe UI", 10), bg="#1c2127", fg="#a1adba").pack(side="right", padx=16)

    top = ttk.Frame(root, padding=(12, 8))
    top.grid(row=1, column=0, sticky="ew")
    top.columnconfigure(1, weight=1)
    commands = ttk.Frame(top)
    commands.grid(row=0, column=0, sticky="w")
    a.icons = []
    toolbar_buttons = []
    for text, command, glyph in (("영상 열기", a.open, "\ue8e5"), ("ROI 불러오기", a.restore, "\ue8b7"), ("저장", a.save, "\ue74e"), ("영상 추출", a.export_view, "\ueb9f"), ("ROI 추출", a.save, "\ue7a8")):
        icon = Image.new("RGBA", (26, 24))
        try:
            font_icon = ImageFont.truetype("segmdl2.ttf", 19)
            ImageDraw.Draw(icon).text((2, 2), glyph, font=font_icon, fill=INK)
        except OSError:
            pass
        a.icons.append(ImageTk.PhotoImage(icon))
        button = ttk.Button(commands, text=text, image=a.icons[-1], compound="left", command=command)
        button.pack(side="left", padx=(0, 5))
        tip(button, text)
        toolbar_buttons.append((button, text))
    steps = ttk.Frame(top)
    steps.grid(row=0, column=1, sticky="e")
    connected = hasattr(a, "session")
    for text, command, enabled in (("02 선정 영상" if connected else "01 영상·타프", a.open, True), ("03 ROI 승인" if connected else "02 ROI 설정", a.focus_roi, True), ("04 방사·Ed" if connected else "03 ELC 계수", (lambda: a.navigate(3)) if connected else None, connected), ("05 ELC·검증" if connected else "04 검증", (lambda: a.navigate(4)) if connected else None, connected)):
        b = ttk.Button(steps, text=text, command=command, style="Accent.TButton" if text.startswith("03" if connected else "02") else "TButton", state="normal" if enabled else "disabled")
        b.pack(side="left", padx=2)
        if not enabled:
            tip(b, "스튜디오 ELC 처리 연결 전 · 이 창에서는 계산하지 않음")

    body = ttk.Frame(root)
    body.grid(row=2, column=0, sticky="nsew")
    body.columnconfigure(1, weight=1)
    body.rowconfigure(0, weight=1)
    left = ttk.Frame(body, width=240, padding=(10, 8))
    left.grid(row=0, column=0, sticky="nsew")
    left.grid_propagate(False)
    left.columnconfigure(0, weight=1)
    left.rowconfigure(1, weight=1)
    ttk.Label(left, text="영상 / ROI", style="Head.TLabel").grid(row=0, column=0, sticky="w", pady=(2, 14))
    if connected:
        selected = ttk.Frame(left)
        selected.grid(row=0, column=0, sticky="ew")
        ttk.Label(selected, text="02 선정 영상", style="Head.TLabel").pack(anchor="w")
        shot_box = ttk.Combobox(selected, textvariable=a.shot, values=a.session.shots, state="readonly")
        shot_box.pack(fill="x", pady=(5, 8))
        shot_box.bind("<<ComboboxSelected>>", lambda e: a.open())
        ttk.Label(selected, text="ELC·Ed 기준 영상").pack(anchor="w")
        reference_box = ttk.Combobox(selected, textvariable=a.reference, values=a.session.shots, state="readonly")
        reference_box.pack(fill="x", pady=(5, 12))
        reference_box.bind("<<ComboboxSelected>>", lambda e: a.change_reference())
    a.tree = ttk.Treeview(left, show="tree", selectmode="browse")
    a.tree.grid(row=1, column=0, sticky="nsew")
    sy = ttk.Scrollbar(left, orient="vertical", command=a.tree.yview)
    sy.grid(row=1, column=1, sticky="ns")
    a.tree.configure(yscrollcommand=sy.set)
    a.tree.column("#0", width=200, minwidth=120)
    a.tree.tag_configure("calibration", foreground="#f3bd67")
    a.tree.tag_configure("validation", foreground="#82b6ff")
    a.listbox = ROIList(a, a.tree)
    a.listbox.delete(0, "end")
    a.tree.bind("<<TreeviewSelect>>", a.select_record)
    if not connected:
        ttk.Button(left, text="영상 열기", command=a.open).grid(row=2, column=0, sticky="ew", pady=(10, 4))
        ttk.Button(left, text="모의 영상", command=a.open_demo).grid(row=3, column=0, sticky="ew", pady=4)
    else:
        tracking_actions = ttk.Frame(left)
        tracking_actions.grid(row=2, column=0, sticky="ew", pady=(8, 4))
        tracking_actions.columnconfigure((0, 1), weight=1)
        tracking_button = ttk.Button(tracking_actions, text="추적", command=a.track_candidates, width=5)
        tracking_button.grid(row=0, column=0, sticky="ew", padx=(0, 2))
        tip(tracking_button, "기준 영상의 확정 ROI로 선정 후보 전체를 추적")
        review_button = ttk.Button(tracking_actions, text="검토", command=a.show_review_queue, width=5)
        review_button.grid(row=0, column=1, sticky="ew", padx=(2, 0))
        tip(review_button, "영상별 ROI 확인 대기·실패 목록")
        ttk.Button(left, text="승인 ROI → 04·05 전달", style="Accent.TButton", command=a.save).grid(row=3, column=0, sticky="ew", pady=4)
    tip(left, "현재 영상의 ROI 목록 · 다른 영상으로 전환하기 전에 저장")

    center = ttk.Frame(body)
    center.grid(row=0, column=1, sticky="nsew", padx=1)
    center.rowconfigure(1, weight=1)
    center.columnconfigure(0, weight=1)
    controls = ttk.Frame(center, padding=(10, 5))
    controls.grid(row=0, column=0, sticky="ew")
    ttk.Label(controls, text="원영상 B6" if connected else "Band").pack(side="left", padx=(0, 6))
    a.bands = ttk.Combobox(controls, textvariable=a.band, width=6, state="readonly")
    if not connected:
        a.bands.pack(side="left")
    a.bands.bind("<<ComboboxSelected>>", lambda e: a.change_band())
    for glyph, caption, command in (("＋", "확대", lambda: a.zoom(SimpleNamespace(x=a.canvas.winfo_width()/2, y=a.canvas.winfo_height()/2, delta=120))),
                                    ("−", "축소", lambda: a.zoom(SimpleNamespace(x=a.canvas.winfo_width()/2, y=a.canvas.winfo_height()/2, delta=-120))),
                                    ("⛶", "전체 영상 맞춤", a.fit), ("⌖", "선택 ROI 확대", a.focus_roi)):
        b = ttk.Button(controls, text=glyph, width=3, command=command)
        b.pack(side="right", padx=2)
        tip(b, caption)
    a.canvas = tk.Canvas(center, bg="#252929", highlightthickness=0)
    a.canvas.grid(row=1, column=0, sticky="nsew")
    legend = ttk.Frame(center, padding=(10, 6))
    legend.grid(row=2, column=0, sticky="ew")
    for text, color in (("□  외곽", "#f3bd67"), ("□  Trim", TEAL), ("□  검증용", "#82b6ff")):
        ttk.Label(legend, text=text, foreground=color).pack(side="left", padx=(0, 18))

    right = ttk.Frame(body, width=310)
    right.grid(row=0, column=2, sticky="nsew")
    right.grid_propagate(False)
    right.columnconfigure(0, weight=1)
    right.rowconfigure(0, weight=1)
    panel = tk.Canvas(right, bg=BG, highlightthickness=0, width=292)
    panel.grid(row=0, column=0, sticky="nsew")
    scrollbar = ttk.Scrollbar(right, orient="vertical", command=panel.yview)
    scrollbar.grid(row=0, column=1, sticky="ns")
    panel.configure(yscrollcommand=scrollbar.set)
    side = ttk.Frame(panel, padding=(10, 8))
    window = panel.create_window(0, 0, anchor="nw", window=side)
    panel.bind("<Configure>", lambda e: panel.itemconfigure(window, width=e.width))
    side.bind("<Configure>", lambda e: panel.configure(scrollregion=panel.bbox("all")))
    side.columnconfigure(1, weight=1)
    ttk.Label(side, text="선택 ROI", style="Head.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(2, 12))
    for row, label, var in ((1, "이름", a.name), (3, "타프 ID", a.tarp_id)):
        ttk.Label(side, text=label).grid(row=row, column=0, sticky="w", pady=6)
        if row == 3 and connected:
            control = ttk.Combobox(side, textvariable=var, values=list(a.session.library), width=15, state="readonly")
        else:
            control = ttk.Entry(side, textvariable=var, width=15)
        control.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=6)
    ttk.Label(side, text="용도").grid(row=2, column=0, sticky="w")
    roles = ttk.Frame(side)
    roles.grid(row=2, column=1, sticky="ew", padx=(8, 0))
    for label, value in (("계수 산출", "calibration"), ("검증", "validation")):
        ttk.Radiobutton(roles, text=label, value=value, variable=a.role).pack(side="left")
    ttk.Separator(side).grid(row=4, column=0, columnspan=2, sticky="ew", pady=12)
    ttk.Label(side, text="Trim (%)", style="Head.TLabel").grid(row=5, column=0, sticky="w")
    spin = ttk.Spinbox(side, from_=0, to=30, textvariable=a.margin, width=8, command=a.redraw)
    spin.grid(row=5, column=1, sticky="e")
    spin.bind("<Return>", lambda e: a.redraw())
    ttk.Scale(side, from_=0, to=30, variable=a.margin, command=lambda v: a.redraw()).grid(row=6, column=0, columnspan=2, sticky="ew", pady=(4, 10))
    ttk.Checkbutton(side, text="백분위 필터", variable=a.filter_enabled).grid(row=7, column=0, columnspan=2, sticky="w", pady=5)
    filters = ttk.Frame(side)
    filters.grid(row=8, column=0, columnspan=2, sticky="ew", pady=6)
    for label, var in (("P하한", a.p_low), ("P상한", a.p_high)):
        ttk.Label(filters, text=label).pack(side="left", padx=(0, 4))
        ttk.Spinbox(filters, from_=0, to=100, textvariable=var, width=4).pack(side="left", padx=(0, 10))
    a.pixel_counts = tk.StringVar(value="전체 —     Trim 후 —     최종 —")
    ttk.Label(side, textvariable=a.pixel_counts, wraplength=260, background="#1c2127", padding=9).grid(row=9, column=0, columnspan=2, sticky="ew", pady=10)
    ttk.Label(side, text="탐색 반경 (px)").grid(row=10, column=0, sticky="w", pady=3)
    ttk.Spinbox(side, textvariable=a.radius, from_=32, to=4096, increment=128, width=8).grid(row=10, column=1, sticky="e", pady=3)
    a.choice = ttk.Combobox(side, state="readonly", width=22)
    a.choice.grid(row=11, column=0, columnspan=2, sticky="ew", pady=5)
    a.choice.bind("<<ComboboxSelected>>", lambda e: a.choose_candidate())
    ttk.Button(side, text="다시 검출", command=a.detect).grid(row=12, column=0, columnspan=2, sticky="ew", pady=3)
    ttk.Separator(side).grid(row=13, column=0, columnspan=2, sticky="ew", pady=10)
    brushes = ttk.Frame(side)
    brushes.grid(row=14, column=0, columnspan=2, sticky="ew")
    for text, mode in (("경계", "none"), ("제외", "exclude"), ("복원", "restore")):
        ttk.Radiobutton(brushes, text=text, value=mode, variable=a.brush_mode).pack(side="left")
    ttk.Label(side, text="브러시 반경 (px)").grid(row=15, column=0, sticky="w", pady=6)
    ttk.Spinbox(side, from_=1, to=30, textvariable=a.brush_radius, width=6).grid(row=15, column=1, sticky="e")
    actions = ttk.Frame(right, padding=(10, 6))
    actions.grid(row=1, column=0, columnspan=2, sticky="ew")
    actions.columnconfigure(0, weight=1)
    actions.columnconfigure(1, weight=1)
    ttk.Checkbutton(actions, text="내부 화소 확인 (제외 없음 포함)", variable=a.pixel_reviewed).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 5))
    ttk.Button(actions, text="✓  ROI 확인 · 확정", command=a.confirm, style="Accent.TButton").grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 5))
    ttk.Button(actions, text="수정 취소", command=a.cancel_edit).grid(row=2, column=0, sticky="ew", padx=(0, 3))
    ttk.Button(actions, text="선택 삭제", command=a.delete).grid(row=2, column=1, sticky="ew", padx=(3, 0))

    notebook = ttk.Notebook(root, height=184)
    notebook.grid(row=3, column=0, sticky="ew", padx=10, pady=(4, 0))
    stats = ttk.Frame(notebook, padding=8)
    notebook.add(stats, text="ROI 통계")
    if not connected:
        for title in ("ELC 계수 · 연결 전", "타프 검증 · 연결 전"):
            notebook.add(ttk.Frame(notebook), text=title, state="disabled")
    a.stats_caption = tk.StringVar(value="원본 저장값 · ROI 선택 대기")
    ttk.Label(stats, textvariable=a.stats_caption).pack(anchor="w", pady=(0, 6))
    a.table = ttk.Treeview(stats, show="headings", height=3, style="Stats.Treeview")
    a.table.pack(fill="both", expand=True)
    scroll = ttk.Scrollbar(stats, orient="horizontal", command=a.table.xview)
    scroll.pack(fill="x")
    a.table.configure(xscrollcommand=scroll.set)
    a.progress = ttk.Progressbar(root, mode="indeterminate")
    a.progress.grid(row=4, column=0, sticky="ew", pady=(4, 0))
    footer = ttk.Frame(root, padding=(12, 5))
    footer.grid(row=5, column=0, sticky="ew")
    footer.columnconfigure(0, weight=1)
    a.cursor = tk.StringVar(value="")
    ttk.Label(footer, textvariable=a.status, wraplength=700).grid(row=0, column=0, sticky="w")
    ttk.Checkbutton(footer, text="작업 로그", variable=a.log_visible, command=a.toggle_log).grid(row=0, column=1, padx=10)
    ttk.Button(footer, text="결과 폴더", command=a.open_result).grid(row=0, column=2)
    ttk.Label(root, textvariable=a.cursor).grid(row=6, column=0, sticky="w", padx=12)
    def compact(event):
        if event.widget is not root:
            return
        left.configure(width=190 if root.winfo_width() < 1300 else 240)
        small = root.winfo_width() < 1400
        for button, text in toolbar_buttons:
            wanted = "" if small else text
            if button.cget("text") != wanted:
                button.configure(text=wanted, width=2 if small else -1)
    root.bind("<Configure>", compact, add=True)


def show_statistics(a, record, rows):
    a.table.delete(*a.table.get_children())
    columns = ["metric"]+[str(row["band"]) for row in rows]
    a.table.configure(columns=columns)
    a.table.heading("metric", text="항목 · 원본 저장 단위")
    a.table.column("metric", width=210, minwidth=190, stretch=False)
    for row in rows:
        key = str(row["band"])
        a.table.heading(key, text=f"Band {key}")
        a.table.column(key, width=135, minwidth=100, anchor="center")
    for key, caption in (("mean", "평균"), ("std", "표준편차"), ("count", "선택 화소 수")):
        a.table.insert("", "end", values=[caption]+[(str(r[key]) if key == "count" else f"{r[key]:.6g}") if r[key] is not None else "—" for r in rows])
    selected = next((r for r in rows if str(r["band"]) == a.band.get()), rows[0] if rows else None)
    if selected:
        a.pixel_counts.set(f"전체 영역  {selected['outer_count']:,}\nTrim 후 유효  {selected['before_count']:,}\n최종 선택  {selected['count']:,}  ·  Band {selected['band']}")
    rule = record.get("percentile_filter", {})
    mode = f"P{rule['low']:g}–P{rule['high']:g}" if rule.get("enabled") else "백분위 필터 없음"
    a.stats_caption.set(f"{'[모의 데이터]  ' if a.demo_source else ''}{a.raster.path.name}  ·  {record['name']}  ·  {mode}  ·  확정 ROI 기준")
    a.note(f"{record['name']} · {len(rows)}개 밴드 통계 완료")
