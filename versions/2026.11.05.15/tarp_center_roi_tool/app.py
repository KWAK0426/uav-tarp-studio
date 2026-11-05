"""Small standalone center-click tarp ROI editor; no external service required."""
from pathlib import Path
import argparse
import os
import sys
import json
import traceback
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
from PIL import Image, ImageTk
from core import Raster, inset, valid_quad, export, stretch, statistics

VERSION = "1.3.0"


def cv_inside(points, point):
    import cv2
    return cv2.pointPolygonTest(np.float32(points), tuple(map(float, point)), False) >= 0


def user_dir():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TarpCenterROI"


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"Tarp ROI Studio   {VERSION}")
        root.geometry("1500x960")
        root.minsize(1100, 780)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)
        ttk.Style().configure("TLabel", font=("Malgun Gothic", 11))
        ttk.Style().configure("TButton", font=("Malgun Gothic", 11), padding=5)
        self.raster = self.preview = None
        self.records, self.candidates = [], []
        self.current = None
        self.center = None
        self.edit_index = None
        self.crop_cache = None
        self.proposal_band = 1
        self.drag = None
        self.scale, self.ox, self.oy = 1, 0, 0
        self.busy = False
        self.dirty = False
        self.pending_changed = False
        self.last_output = None
        self.demo_source = False
        self.last_directory = ""
        self.stage = tk.StringVar(value="1  영상 선택     /     2  ROI 확인     /     3  결과 저장")
        self.queue = queue.Queue()
        self.progress_notes = queue.Queue()
        self.band = tk.StringVar(value="1")
        self.radius = tk.IntVar(value=512)
        self.margin = tk.DoubleVar(value=8)
        self.name = tk.StringVar(value="Tarp_01")
        self.status = tk.StringVar(value="영상 대기")
        self.path_label = tk.StringVar(value="")
        self.log_visible = tk.BooleanVar(value=False)
        self.role = tk.StringVar(value="calibration")
        self.tarp_id = tk.StringVar(value="")
        self.filter_enabled = tk.BooleanVar(value=True)
        self.p_low = tk.DoubleVar(value=5)
        self.p_high = tk.DoubleVar(value=95)
        self.pixel_reviewed = tk.BooleanVar(value=False)
        self.brush_mode = tk.StringVar(value="none")
        self.brush_radius = tk.IntVar(value=4)
        self.current_strokes = []
        def changed(*args):
            if self.current is not None:
                self.pending_changed = self.dirty = True
        for variable in (self.name, self.role, self.tarp_id, self.filter_enabled, self.p_low, self.p_high):
            variable.trace_add("write", changed)
        def trim_changed(*args):
            changed()
            if self.current is not None:
                self.pixel_reviewed.set(False)
        self.margin.trace_add("write", trim_changed)
        self.stats_rows = []
        self.stats_record = None
        self.settings_path = user_dir() / "settings.json"
        try:
            settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
            self.last_directory = settings.get("last_directory", "")
            self.radius.set(max(32, min(4096, int(settings.get("radius", 512)))))
            self.margin.set(max(0, min(30, float(settings.get("margin", 8)))))
        except (OSError, ValueError, TypeError):
            pass
        from studio_ui import build
        build(self)
        self.log_window = tk.Toplevel(root)
        self.log_window.title("Tarp ROI · 작업 로그")
        self.log_window.geometry("850x280")
        self.log_window.withdraw()
        self.log_window.protocol("WM_DELETE_WINDOW", self.hide_log)
        self.log = tk.Text(self.log_window, font=("Malgun Gothic", 10), state="disabled", bg="#11161b", fg="#edf2f7", insertbackground="#edf2f7")
        self.log.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self.click)
        self.canvas.bind("<B1-Motion>", self.drag_vertex)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "drag", None))
        self.canvas.bind("<Button-2>", self.pan_start)
        self.canvas.bind("<B2-Motion>", self.pan)
        self.canvas.bind("<MouseWheel>", self.zoom)
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<Motion>", self.cursor_position)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self.callback_error
        self.poll_after = root.after(100, self.poll)

    def cursor_position(self, event):
        if self.raster is not None:
            x, y = self.xy(event)
            self.cursor.set(f"x {x:.1f}   y {y:.1f}   |   확대 {self.scale*100:.0f}%")

    def toggle_log(self):
        if self.log_visible.get():
            self.log_window.deiconify()
        else:
            self.log_window.withdraw()

    def hide_log(self):
        self.log_visible.set(False)
        self.log_window.withdraw()

    def callback_error(self, cls, exc, tb):
        self.note("오류: " + str(exc))
        try:
            user_dir().mkdir(parents=True, exist_ok=True)
            with (user_dir()/"errors.log").open("a", encoding="utf-8") as stream:
                stream.write("".join(traceback.format_exception(cls, exc, tb)))
        except OSError:
            pass
        messagebox.showerror("오류", f"{exc}\n\n원본 영상은 변경되지 않았습니다.")

    def help(self):
        messagebox.showinfo("사용 안내", "1. 영상을 열고 타프 중앙을 클릭합니다.\n"
            "2. 노란 외곽선과 청록 내부 ROI를 확인합니다.\n"
            "3. 꼭짓점을 끌어 수정한 뒤 확인 · 확정합니다.\n"
            "4. 저장하면 좌표, 마스크, 밴드별 원본값 통계가 생성됩니다.\n\n"
            "휠: 확대 / 휠 버튼 누르고 끌기: 이동\n"
            "내부 여유 8%: 가장 짧은 높이의 8%를 각 변에서 제외\n"
            "검출 실패 시 다른 밴드·중앙·탐색 반경으로 다시 시도\n\n"
            "통계는 저장값 그대로입니다. radiance 변환·ELC는 수행하지 않습니다.\n"
            "미확정 수정은 저장되지 않습니다. ROI 복원은 같은 원본에만 가능합니다.\n"
            "자동 후보는 정답이 아니며, 그림자·포화·밴드 정합은 별도 확인 대상입니다.")

    def open_demo(self):
        if self.busy:
            return
        from demo import create_demo
        path, _ = create_demo(user_dir()/"demo")
        self.open(str(path))

    def open_result(self):
        if self.last_output and Path(self.last_output).is_dir():
            os.startfile(str(self.last_output))
        else:
            messagebox.showinfo("결과 폴더", "먼저 ROI 결과를 저장하세요.")

    def focus_roi(self):
        if self.raster is None:
            return
        points = self.current
        if points is None and self.records:
            points = np.array(self.records[-1]["outer"])
        if points is None:
            return
        extent = np.maximum(16, np.ptp(points, axis=0)) * 2
        self.scale = min(self.canvas.winfo_width()/extent[0], self.canvas.winfo_height()/extent[1], 24)
        self.ox, self.oy = np.array([self.canvas.winfo_width(), self.canvas.winfo_height()])/2-np.mean(points, axis=0)*self.scale
        self.redraw()

    def note(self, text):
        self.status.set(text)
        if hasattr(self,'session'):
            from uav_pipeline.progress_ui import update
            update(self,text)
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S  ") + text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def job(self, title, work, done):
        if self.busy:
            return
        self.busy = True
        if hasattr(self,'session'):
            from uav_pipeline.progress_ui import begin
            parent=getattr(self,'review_window',None)
            begin(self,title,parent=parent if parent is not None and parent.winfo_exists() else self.root)
        self.bands.configure(state="disabled")
        self.note(title)
        self.progress.start(12)
        def run():
            try:
                self.queue.put((done, work(), None))
            except Exception as exc:
                self.queue.put((done, None, str(exc)))
        threading.Thread(target=run, daemon=True).start()

    def poll(self):
        try:
            while True:
                self.note(self.progress_notes.get_nowait())
        except queue.Empty:
            pass
        try:
            done, result, error = self.queue.get_nowait()
            self.busy = False
            self.bands.configure(state="readonly")
            self.progress.stop()
            if hasattr(self,'session'):
                from uav_pipeline.progress_ui import finish
                finish(self)
            if error:
                self.note("실패: " + error)
                if hasattr(self,'handle_job_error') and self.handle_job_error(error):
                    return
                detail=error
                if '정합 후보 모두 실패' in error:
                    detail=("밴드 간 타프 위치 맞추기에 실패해 다음 단계로 전달하지 못했습니다.\n\n"
                            "확정한 타프 위치·선택 영상·ROI 확인 결과는 저장되어 있습니다. 다시 지정할 필요 없습니다.\n"
                            "같은 상태에서 재시도하면 동일하게 실패합니다. 해당 영상의 정합을 수정하거나, "
                            "검증용 영상이라면 결과 목록에서 제외한 뒤 진행해야 합니다.\n\n"+error)
                messagebox.showerror("작업 실패", detail)
            else:
                done(result)
        except queue.Empty:
            pass
        finally:
            try:
                if self.root.winfo_exists():
                    self.poll_after = self.root.after(100, self.poll)
            except tk.TclError:
                pass

    def discard_ok(self):
        return not self.dirty or messagebox.askyesno("저장 확인", "저장하지 않은 변경을 버릴까요?")

    def discard_pending(self):
        return not self.pending_changed or messagebox.askyesno("수정 확인", "확정하지 않은 꼭짓점 수정을 버릴까요?")

    def cancel_edit(self):
        if self.busy:
            return
        self.current = self.edit_index = None
        self.pending_changed = False
        self.candidates = []
        self.choice.set("")
        self.choice["values"] = []
        self.redraw()

    def open(self, path=None):
        if self.busy or not self.discard_ok():
            return
        path = path or filedialog.askopenfilename(initialdir=self.last_directory or None, filetypes=[("영상", "*.tif *.tiff *.png *.jpg *.jpeg"), ("모든 파일", "*.*")])
        if not path:
            return
        def read():
            if hasattr(self,'before_image_read'):self.before_image_read()
            raster = Raster(path)
            return raster, raster.preview(raster.bands[0])
        def loaded(result):
            self.raster, array = result
            self.last_directory = str(self.raster.path.parent)
            self.demo_source = self.raster.ds.GetMetadataItem("SYNTHETIC_DEMO") == "YES"
            self.crop_cache = None
            self.preview = Image.fromarray(array)
            self.bands["values"] = self.raster.bands
            self.band.set(str(self.raster.bands[0]))
            self.records, self.candidates = [], []
            self.current = self.center = self.edit_index = None
            self.pending_changed = False
            self.choice.set("")
            self.choice["values"] = []
            self.dirty = False
            self.refresh_list()
            self.path_label.set(str(self.raster.path))
            self.fit()
            self.stage.set(("[모의 데이터]  " if self.demo_source else "") + "1  영상 로드 완료     /     2  ROI 확인 중     /     3  저장 대기")
            self.note(f"로드 완료: {self.raster.width} × {self.raster.height} px / {len(self.raster.bands)}개 밴드")
            if hasattr(self, "image_loaded"):
                self.image_loaded()
        self.job("영상 로드 중", read, loaded)

    def change_band(self):
        if self.busy or self.raster is None:
            return
        band = int(self.band.get())
        def loaded(array):
            self.crop_cache = None
            self.preview = Image.fromarray(array)
            self.redraw()
            self.note(f"밴드 {band} 표시 완료")
            if self.stats_rows and self.stats_record:
                from studio_ui import show_statistics
                show_statistics(self, self.stats_record, self.stats_rows)
        self.job("밴드 로드 중", lambda: self.raster.preview(band), loaded)

    def fit(self):
        if self.raster is None:
            return
        self.scale = .96 * min(self.canvas.winfo_width()/self.raster.width, self.canvas.winfo_height()/self.raster.height)
        self.ox = (self.canvas.winfo_width() - self.raster.width*self.scale)/2
        self.oy = (self.canvas.winfo_height() - self.raster.height*self.scale)/2
        self.redraw()

    def xy(self, event):
        return np.array([(event.x-self.ox)/self.scale, (event.y-self.oy)/self.scale])

    def redraw(self):
        self.canvas.delete("all")
        if self.preview is None:
            return
        cw, ch = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        sx = self.preview.width / self.raster.width / self.scale
        sy = self.preview.height / self.raster.height / self.scale
        image = self.preview.transform((cw, ch), Image.Transform.AFFINE, (sx, 0, -self.ox*sx, 0, sy, -self.oy*sy), resample=Image.Resampling.BILINEAR, fillcolor=37)
        if self.scale > self.preview.width / self.raster.width and not self.busy:
            x0 = max(0, int(-self.ox / self.scale))
            y0 = max(0, int(-self.oy / self.scale))
            x1 = min(self.raster.width, int(np.ceil((cw-self.ox)/self.scale)))
            y1 = min(self.raster.height, int(np.ceil((ch-self.oy)/self.scale)))
            if x1 > x0 and y1 > y0:
                window = (x0, y0, x1-x0, y1-y0)
                key = (int(self.band.get()), window, cw, ch)
                if self.crop_cache is None or self.crop_cache[0] != key:
                    data, valid = self.raster.read(key[0], window, max_side=max(cw, ch))
                    crop = Image.fromarray(stretch(data, valid)) if valid.any() else Image.new("L", (data.shape[1], data.shape[0]), 37)
                    self.crop_cache = (key, crop)
                crop = self.crop_cache[1]
                fx, fy = crop.width/window[2]/self.scale, crop.height/window[3]/self.scale
                image = crop.transform((cw, ch), Image.Transform.AFFINE,
                                       (fx, 0, (-self.ox-x0*self.scale)*fx, 0, fy, (-self.oy-y0*self.scale)*fy),
                                       resample=Image.Resampling.BILINEAR, fillcolor=37)
        self.photo = ImageTk.PhotoImage(image)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        def polygon(points, color, dash=None):
            coords = np.asarray(points)*self.scale + [self.ox, self.oy]
            self.canvas.create_polygon(*coords.flatten(), outline=color, fill="", width=2, dash=dash)
        for record in self.records:
            polygon(record["outer"], "#ffb02e" if record.get("role", "calibration") == "calibration" else "#2685ff")
            polygon(record["inner"], "#00d8ed")
            for px,py,radius,excluded in record.get('exclusion_strokes',[]):
                if not excluded:continue
                px,py=px*self.scale+self.ox,py*self.scale+self.oy
                size=max(.5,radius)*self.scale
                self.canvas.create_rectangle(px-size,py-size,px+size,py+size,fill='#ff667c',outline='')
            x, y = np.min(record["outer"], axis=0)*self.scale+[self.ox, self.oy]
            self.canvas.create_text(x+4, max(12, y-12), text=record["name"], anchor="w", fill="white", font=("Malgun Gothic", 10, "bold"))
        if self.current is not None:
            polygon(self.current, "#ffca53", (5, 3))
            try:
                polygon(inset(self.current, self.margin.get()), "#00d8ed")
            except (ValueError, tk.TclError, np.linalg.LinAlgError):
                pass
            for x, y in self.current*self.scale + [self.ox, self.oy]:
                self.canvas.create_oval(x-5, y-5, x+5, y+5, fill="#ffca53", outline="black")
            for x, y, radius, excluded in self.current_strokes:
                x, y, r = x*self.scale+self.ox, y*self.scale+self.oy, radius*self.scale
                self.canvas.create_oval(x-r, y-r, x+r, y+r, outline="#ff667c" if excluded else "#75d5b6", width=2)

    def click(self, event):
        if self.busy or self.raster is None:
            return
        p = self.xy(event)
        if self.current is not None and self.brush_mode.get() != "none":
            if cv_inside(self.current, p):
                if not 1 <= self.brush_radius.get() <= 30:
                    messagebox.showwarning("브러시 반경", "브러시 반경은 1~30화소입니다.")
                    return
                self.current_strokes.append([float(p[0]), float(p[1]), self.brush_radius.get(), self.brush_mode.get() == "exclude"])
                self.pixel_reviewed.set(False)
                self.pending_changed = self.dirty = True
                self.redraw()
            return
        if self.current is not None:
            distances = np.linalg.norm(self.current-p, axis=1)*self.scale
            if distances.min() < 13:
                self.drag = int(distances.argmin())
                return
        if not (0 <= p[0] < self.raster.width and 0 <= p[1] < self.raster.height):
            return
        if not self.discard_pending():
            return
        self.pending_changed = False
        self.center = p.tolist()
        self.current_strokes = []
        self.pixel_reviewed.set(False)
        self.edit_index = None
        index = 1
        while f"Tarp_{index:02d}" in [r["name"] for r in self.records]:
            index += 1
        self.name.set(f"Tarp_{index:02d}")
        self.detect()

    def detect(self):
        if self.center is None or self.busy:
            return
        if not self.discard_pending():
            return
        self.pending_changed = False
        try:
            radius, band = self.radius.get(), int(self.band.get())
            if not 32 <= radius <= 4096:
                raise ValueError("탐색 반경: 32~4096 px")
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("입력 확인", str(exc))
            return
        self.current = None
        self.candidates = []
        self.proposal_band = band
        self.choice.set("")
        self.choice["values"] = []
        self.redraw()
        def done(candidates):
            self.candidates = candidates
            self.choice["values"] = [f"후보 {i+1} / 경계 {c['min_edge_contrast_u8']:.0f}" for i, c in enumerate(candidates)]
            if candidates:
                self.choice.current(0)
                self.choose_candidate()
                self.note(f"{len(candidates)}개 후보. 외곽: 노랑 / 내부 ROI: 청록. 미확정")
            else:
                self.note("명확한 사각형 경계 없음. 밴드·중앙 위치·탐색 반경을 바꿔 재시도하세요.")
        self.job("중앙 주변 경계 검출 중", lambda: self.raster.propose(self.center, band, radius), done)

    def choose_candidate(self):
        if not self.busy and 0 <= self.choice.current() < len(self.candidates):
            self.current = np.array(self.candidates[self.choice.current()]["points"], np.float32)
            self.dirty = True
            self.pending_changed = True
            self.redraw()

    def drag_vertex(self, event):
        if self.drag is None or self.current is None or self.busy:
            return
        p = self.current.copy()
        p[self.drag] = np.clip(self.xy(event), [0, 0], [self.raster.width, self.raster.height])
        if valid_quad(p):
            self.current = p
            self.dirty = True
            self.pending_changed = True
            self.pixel_reviewed.set(False)
            self.redraw()

    def pan_start(self, event):
        self.pan_at = (event.x, event.y)

    def pan(self, event):
        if hasattr(self, "pan_at"):
            self.ox += event.x-self.pan_at[0]
            self.oy += event.y-self.pan_at[1]
            self.pan_at = (event.x, event.y)
            self.redraw()

    def zoom(self, event):
        if self.raster is None:
            return
        p = self.xy(event)
        self.scale = np.clip(self.scale*(1.2 if event.delta > 0 else 1/1.2), .0001, 24)
        self.ox, self.oy = np.array([event.x, event.y])-p*self.scale
        self.redraw()

    def confirm(self):
        if self.busy or self.current is None:
            return
        try:
            name = self.name.get().strip()
            if not name:
                raise ValueError("타프 이름이 필요합니다.")
            if any(r["name"] == name for i, r in enumerate(self.records) if i != self.edit_index):
                raise ValueError("이미 사용 중인 타프 이름입니다.")
            low, high = self.p_low.get(), self.p_high.get()
            if not 0 <= low < high <= 100:
                raise ValueError("백분위는 0 ≤ P하한 < P상한 ≤ 100 이어야 합니다.")
            if hasattr(self, "session") and not self.pixel_reviewed.get():
                raise ValueError("내부 화소를 확인하고 확인 체크를 선택하세요. 제외 화소가 없어도 확인이 필요합니다.")
            record = {"name": name, "outer": self.current.tolist(), "inner": inset(self.current, self.margin.get()).tolist(),
                      "margin_percent_shortest_altitude_per_edge": self.margin.get(), "detection_band": self.proposal_band,
                      "center": self.center, "reviewed": True, "role": self.role.get(), "tarp_id": self.tarp_id.get().strip(),
                      "pixel_reviewed": self.pixel_reviewed.get(), "exclusion_strokes": list(self.current_strokes),
                      "percentile_filter": {"enabled": self.filter_enabled.get(), "low": low, "high": high}}
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("ROI 확인", str(exc))
            return
        if self.edit_index is None:
            self.records.append(record)
        else:
            if self.records[self.edit_index].get("tracking"):
                record["tracking"] = dict(self.records[self.edit_index]["tracking"], status="user_reviewed")
            self.records[self.edit_index] = record
        self.current = self.edit_index = None
        self.pending_changed = False
        self.candidates = []
        self.choice.set("")
        self.choice["values"] = []
        self.dirty = True
        self.refresh_list()
        self.redraw()
        self.note(f"{name} 확정 / 총 {len(self.records)}개. 아직 파일에 저장하지 않았습니다.")
        self.stage.set(("[모의 데이터]  " if self.demo_source else "") + f"1  영상 로드 완료     /     2  ROI {len(self.records)}개 확정     /     3  저장 대기")
        self.update_statistics(record)

    def refresh_list(self):
        self.listbox.delete(0, "end")
        for r in self.records:
            self.listbox.insert("end", r["name"])
        self.stats_rows = []
        self.stats_record = None
        if hasattr(self, "table"):
            self.table.delete(*self.table.get_children())
            self.stats_caption.set("원본 저장값 · ROI 선택 대기")
            self.pixel_counts.set("전체 —     Trim 후 —     최종 —")

    def select_record(self, event=None):
        if self.busy or not self.listbox.curselection():
            return
        if not self.discard_pending():
            return
        self.pending_changed = False
        self.edit_index = self.listbox.curselection()[0]
        r = self.records[self.edit_index]
        self.current = np.array(r["outer"], np.float32)
        self.proposal_band = r["detection_band"]
        self.candidates = []
        self.choice.set("")
        self.choice["values"] = []
        self.center = r["center"]
        self.name.set(r["name"])
        self.margin.set(r["margin_percent_shortest_altitude_per_edge"])
        self.role.set(r.get("role", "calibration"))
        self.tarp_id.set(r.get("tarp_id", ""))
        rule = r.get("percentile_filter", {})
        self.filter_enabled.set(rule.get("enabled", False))
        self.p_low.set(rule.get("low", 5))
        self.p_high.set(rule.get("high", 95))
        self.pixel_reviewed.set(r.get("pixel_reviewed", False))
        self.current_strokes = list(r.get("exclusion_strokes", []))
        self.pending_changed = False
        self.redraw()
        self.update_statistics(r)

    def update_statistics(self, record):
        if self.busy or self.raster is None:
            return
        def done(rows):
            from studio_ui import show_statistics
            self.stats_rows = rows
            self.stats_record = record
            show_statistics(self, record, rows)
        self.job("ROI 밴드별 통계 계산 중", lambda: statistics(self.raster, record), done)

    def export_view(self):
        if self.preview is None or self.busy:
            return
        path = filedialog.asksaveasfilename(defaultextension=".png", filetypes=[("표시 영상 PNG", "*.png")])
        if path:
            from PIL import ImageDraw
            image = self.preview.convert("RGB")
            draw = ImageDraw.Draw(image)
            ratio = np.array([image.width/self.raster.width, image.height/self.raster.height])
            for r in self.records:
                for kind, color in (("outer", "#ffb02e"), ("inner", "#00d8ed")):
                    pts = [tuple(p) for p in np.asarray(r[kind])*ratio]
                    draw.line(pts+[pts[0]], fill=color, width=2)
            image.save(path)
            self.note(f"표시용 PNG 저장 완료 (정량 원본 아님): {path}")

    def delete(self):
        if self.busy or not self.listbox.curselection():
            return
        del self.records[self.listbox.curselection()[0]]
        self.current = self.edit_index = None
        self.dirty = True
        self.refresh_list()
        self.redraw()

    def save(self):
        if self.busy:
            return
        if not self.records:
            messagebox.showinfo("저장", "저장할 확정 ROI가 없습니다.")
            return
        if self.current is not None:
            messagebox.showinfo("확정 필요", "현재 편집 중인 ROI를 먼저 확인 · 확정하세요.")
            return
        folder = filedialog.askdirectory(title="ROI 결과 저장 폴더")
        if not folder:
            return
        folder = Path(folder) / (self.raster.path.stem + "_roi_" + time.strftime("%Y%m%d_%H%M%S"))
        if folder.exists():
            messagebox.showerror("저장 확인", "같은 시각의 저장 폴더가 있습니다. 잠시 후 다시 저장하세요.")
            return
        def done(path):
            self.dirty = False
            self.last_output = Path(path).parent
            self.note(f"저장 완료: {path}")
            self.stage.set(("[모의 데이터]  " if self.demo_source else "") + f"1  영상 로드 완료     /     2  ROI {len(self.records)}개 확정     /     3  저장 완료")
        self.job("ROI · 밴드별 원본 화소 통계 저장 중", lambda: export(self.raster, self.records, folder), done)

    def restore(self):
        if self.busy:
            return
        if self.raster is None:
            messagebox.showinfo("원본 영상", "먼저 ROI를 작성했던 원본 영상을 열어주세요.")
            return
        if not self.discard_ok():
            return
        path = filedialog.askopenfilename(filetypes=[("ROI JSON", "*.json")])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            source = self.raster.path
            if data.get("schema") != "center_tarp_roi_v1" or Path(data["source"]).resolve() != source or (data["width"], data["height"]) != (self.raster.width, self.raster.height):
                raise ValueError("현재 원본 영상과 ROI 좌표계가 일치하지 않습니다.")
            if (data["source_size"], data["source_mtime_ns"]) != (source.stat().st_size, source.stat().st_mtime_ns):
                raise ValueError("ROI 저장 이후 원본 파일이 변경되었습니다.")
            for r in data["rois"]:
                if not valid_quad(r["outer"]) or not valid_quad(r["inner"]):
                    raise ValueError("잘못된 ROI 좌표입니다.")
            self.records = data["rois"]
            self.current = self.edit_index = None
            self.dirty = False
            self.refresh_list()
            self.redraw()
            self.note(f"{len(self.records)}개 ROI 복원 완료")
        except Exception as exc:
            messagebox.showerror("불러오기 실패", str(exc))

    def close(self):
        if self.busy:
            messagebox.showinfo("처리 중", "현재 작업이 끝난 뒤 닫아주세요.")
        elif self.discard_ok():
            try:
                self.settings_path.parent.mkdir(parents=True, exist_ok=True)
                self.settings_path.write_text(json.dumps({"last_directory": self.last_directory, "radius": self.radius.get(), "margin": self.margin.get()}), encoding="utf-8")
            except (OSError, ValueError, tk.TclError):
                pass
            self.root.destroy()


if __name__ == "__main__":
    sys.modules.setdefault("app", sys.modules[__name__])
    parser = argparse.ArgumentParser()
    parser.add_argument("image", nargs="?")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--studio-config", type=Path)
    parser.add_argument("--geometry")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import app_identity
    app_identity.set_identity()
    root = tk.Tk()
    app_identity.apply(root)
    if args.studio_config:
        try:
            from project_ui import start_project
            app = start_project(root, args.studio_config, geometry=args.geometry)
        except Exception as exc:
            messagebox.showerror("스튜디오 ROI 연결", str(exc))
            root.destroy()
            sys.exit(1)
    else:
        app = App(root)
    if args.self_test:
        from demo import run_self_test
        sys.exit(run_self_test(app, args.output or user_dir()/"self_test"))
    if args.image:
        root.after(100, lambda: app.open(args.image))
    if args.smoke:
        root.after(1500, root.destroy)
    root.mainloop()
