"""Native Tk popups for the flight-scoped tarp workflow."""
from __future__ import annotations

import io
import base64
import json
import os
import queue
import threading
import urllib.parse
import urllib.request
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageEnhance, ImageTk


BG = "#17191c"
CANVAS_BG = "#101315"
ACCENT = "#75d4bc"
ACTIVE = "#ffd36a"
MUTED = "#a4abb3"
DISPLAY_BANDS = ("RGB", "B2 Blue 475nm", "B3 Green 531nm", "B4 Green 560nm", "B6 Red 668nm", "B10 NIR 842nm")


def _display_band(value):
    return value.split()[0].removeprefix("B").lower()


class LocalApi:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def get(self, path: str, params: dict | None = None):
        if params:
            path += "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(self.base_url + path, timeout=60) as response:
            return json.load(response)

    def image(self, path: str, params: dict | None = None) -> Image.Image:
        if params:
            path += "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(self.base_url + path, timeout=120) as response:
            return Image.open(io.BytesIO(response.read())).convert("RGB")

    def post(self, path: str, payload: dict):
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.load(response)


def _error_text(exc: Exception) -> str:
    if hasattr(exc, "read"):
        try:
            body = json.loads(exc.read().decode("utf-8"))
            return str(body.get("error") or exc)
        except Exception:
            pass
    return str(exc)


def _async(widget, work, done=None, failed=None):
    result = queue.Queue(maxsize=1)

    def runner():
        try:
            result.put((True, work()))
        except Exception as exc:
            result.put((False, _error_text(exc)))

    def poll():
        if not widget.winfo_exists():
            return
        try:
            ok, value = result.get_nowait()
        except queue.Empty:
            widget.after(30, poll)
            return
        if ok:
            if done:
                done(value)
        else:
            (failed or (lambda text: messagebox.showwarning("자료 확인", text, parent=widget)))(value)

    widget.after(30, poll)
    threading.Thread(target=runner, daemon=True).start()


class ImageViewport(ttk.Frame):
    def __init__(self, parent, click=None, hit_test=None, edit_move=None):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, background=CANVAS_BG, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.source_image = None
        self.image = None
        self.tk_image = None
        self.scale = 1.0
        self.fit_scale = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self.click = click
        self.hit_test = hit_test
        self.edit_move = edit_move
        self.overlay = None
        self.drag = None
        self.edit_token = None
        self.needs_fit = False
        self.brightness = 1.0
        self.canvas.bind("<Configure>", self._configured)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._release)

    @property
    def size(self):
        return self.image.size if self.image else (0, 0)

    def set_image(self, image: Image.Image, fit=True):
        self.source_image = image.convert("RGB")
        self.image = ImageEnhance.Brightness(self.source_image).enhance(self.brightness)
        if fit:
            self.needs_fit = True
            self.after_idle(self._fit_when_ready)
        else:
            self.render()

    def set_brightness(self, value):
        self.brightness = min(2.0, max(0.2, float(value)))
        if self.source_image:
            self.image = ImageEnhance.Brightness(self.source_image).enhance(self.brightness)
            self.render()

    def _fit_when_ready(self):
        if not self.image or not self.winfo_exists():
            return
        if self.canvas.winfo_width() < 20 or self.canvas.winfo_height() < 20:
            self.after(30, self._fit_when_ready)
            return
        self.needs_fit = False
        self.fit()

    def set_overlay(self, callback):
        self.overlay = callback
        self.render()

    def fit(self):
        if not self.image:
            return
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        iw, ih = self.image.size
        self.fit_scale = min(cw / iw, ch / ih)
        self.scale = self.fit_scale
        self.offset_x = (cw - iw * self.scale) / 2
        self.offset_y = (ch - ih * self.scale) / 2
        self.render()

    def focus_polygon(self, points):
        if not self.image or len(points) < 4:
            return False
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        width = max(30, (max(xs) - min(xs)) * 2.5)
        height = max(30, (max(ys) - min(ys)) * 2.5)
        cw, ch = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        self.scale = min(cw / width, ch / height, max(self.fit_scale * 18, 3.0))
        self.offset_x = cw / 2 - cx * self.scale
        self.offset_y = ch / 2 - cy * self.scale
        self.render()
        return True

    def image_to_canvas(self, x, y):
        return self.offset_x + x * self.scale, self.offset_y + y * self.scale

    def canvas_to_image(self, x, y):
        return (x - self.offset_x) / self.scale, (y - self.offset_y) / self.scale

    def render(self):
        self.canvas.delete("all")
        if not self.image:
            self.canvas.create_text(
                max(1, self.canvas.winfo_width()) / 2,
                max(1, self.canvas.winfo_height()) / 2,
                text="영상 불러오는 중",
                fill=MUTED,
                font=("Malgun Gothic", 12),
            )
            return
        cw, ch = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        iw, ih = self.image.size
        x0 = max(0.0, (0 - self.offset_x) / self.scale)
        y0 = max(0.0, (0 - self.offset_y) / self.scale)
        x1 = min(float(iw), (cw - self.offset_x) / self.scale)
        y1 = min(float(ih), (ch - self.offset_y) / self.scale)
        if x1 > x0 and y1 > y0:
            box = (int(x0), int(y0), max(int(x0) + 1, int(x1 + 1)), max(int(y0) + 1, int(y1 + 1)))
            tile = self.image.crop(box)
            target = (max(1, round(tile.width * self.scale)), max(1, round(tile.height * self.scale)))
            tile = tile.resize(target, Image.Resampling.BILINEAR)
            self.tk_image = ImageTk.PhotoImage(tile)
            px, py = self.image_to_canvas(box[0], box[1])
            self.canvas.create_image(px, py, image=self.tk_image, anchor="nw")
        if self.overlay:
            self.overlay(self)

    def _configured(self, _event):
        if self.image:
            if self.needs_fit:
                self._fit_when_ready()
            else:
                self.render()

    def _wheel(self, event):
        if not self.image:
            return
        old = self.scale
        ix, iy = self.canvas_to_image(event.x, event.y)
        limit_low = max(self.fit_scale * 0.75, 0.02)
        limit_high = max(self.fit_scale * 18, 3.0)
        factor = 1.18 if event.delta > 0 else 1 / 1.18
        self.scale = min(limit_high, max(limit_low, old * factor))
        self.offset_x = event.x - ix * self.scale
        self.offset_y = event.y - iy * self.scale
        self.render()

    def _press(self, event):
        ix, iy = self.canvas_to_image(event.x, event.y)
        if self.hit_test:
            self.edit_token = self.hit_test(ix, iy, self.scale)
        if self.edit_token is None:
            self.drag = (event.x, event.y, self.offset_x, self.offset_y, False)

    def _move(self, event):
        ix, iy = self.canvas_to_image(event.x, event.y)
        if self.edit_token is not None and self.edit_move:
            self.edit_move(self.edit_token, ix, iy)
            self.render()
            return
        if not self.drag:
            return
        sx, sy, ox, oy, moved = self.drag
        dx, dy = event.x - sx, event.y - sy
        moved = moved or abs(dx) + abs(dy) > 5
        self.drag = (sx, sy, ox, oy, moved)
        if moved:
            self.offset_x, self.offset_y = ox + dx, oy + dy
            self.render()

    def _release(self, event):
        if self.edit_token is not None:
            self.edit_token = None
            return
        if not self.drag:
            return
        moved = self.drag[4]
        self.drag = None
        if not moved and self.click and self.image:
            ix, iy = self.canvas_to_image(event.x, event.y)
            if 0 <= ix < self.image.width and 0 <= iy < self.image.height:
                self.click(ix, iy)


class _PopupBase(tk.Toplevel):
    def __init__(self, parent, base_url, title):
        super().__init__(parent)
        self.api = LocalApi(base_url)
        self.title(title)
        width = min(1420, self.winfo_screenwidth() - 80)
        height = min(880, self.winfo_screenheight() - 100)
        self.geometry(f"{width}x{height}+{parent.winfo_rootx()+20}+{parent.winfo_rooty()+20}")
        self.minsize(1000, 680)
        self.configure(background=BG)
        self.transient(parent)
        self.focus_force()

    def _header(self, title, subtitle=""):
        frame = ttk.Frame(self, padding=(14, 10))
        frame.pack(fill="x")
        ttk.Button(frame, text="닫기", command=self.destroy).pack(side="right")
        ttk.Label(frame, text=title, style="Title.TLabel").pack(anchor="w")
        self.header_detail = ttk.Label(frame, text=subtitle, style="Subtitle.TLabel")
        self.header_detail.pack(anchor="w", pady=(2, 0))


class NativeSaturationPopup(_PopupBase):
    def __init__(self,parent,base_url,item,shot):
        super().__init__(parent,base_url,'원영상 타프 포화 빠른 확인')
        self.geometry('1000x760')
        self.item=item;self.corner=None;self.cropped=False;self.request_id=0
        self._header(shot+' · 원영상 DN 포화','빨강 = DN ≥ 65520. 두 점으로 타프 검사 상자를 지정하세요. 정식 ROI 승인과 별개입니다.')
        row=ttk.Frame(self,padding=(14,0));row.pack(fill='x')
        ttk.Label(row,text='원본 파일 밴드 번호').pack(side='left')
        self.band=tk.StringVar(value='1')
        combo=ttk.Combobox(row,textvariable=self.band,values=list(map(str,range(1,11))),state='readonly',width=4)
        combo.pack(side='left',padx=8);combo.bind('<<ComboboxSelected>>',lambda _e:self.load())
        ttk.Button(row,text='전체 영상 / 상자 초기화',command=self.load).pack(side='left')
        self.save_button=ttk.Button(row,text='이 검사 상자 기록 저장',command=self.save_check,state='disabled')
        self.save_button.pack(side='right')
        self.info=tk.StringVar(value='원영상 읽는 중')
        ttk.Label(self,textvariable=self.info,wraplength=940,padding=12).pack(fill='x')
        self.view=ImageViewport(self,click=self.clicked);self.view.pack(fill='both',expand=True,padx=14,pady=(0,14))
        self.load()

    def load(self,box=None):
        self.corner=None;self.request_id+=1;token=self.request_id
        self.loading=True
        self.current_box=box;self.save_button.configure(state='disabled')
        self.cropped=box is not None
        params=dict(id=self.item,band=self.band.get())
        if box:params['box']=json.dumps(box)
        self.info.set('원영상 전체 해상도로 포화 화소 계산 중')
        def done(value):
            if token!=self.request_id:return
            self.loading=False
            self.save_button.configure(state='normal' if box else 'disabled')
            self.view.set_image(Image.open(io.BytesIO(base64.b64decode(value['image']))))
            s=value['stats'];wave=s['wavelength_nm']
            label='타프 검사 상자 · 미승인' if box else '전체 원영상 · 타프 판정 전'
            self.info.set(f"{label} | {wave:g} nm | 포화 {s['saturated_pixels']:,}/{s['valid_pixels']:,} ({s['saturated_pct']:.4f}%) | 최대 DN {s['max_dn']:,}" if wave else
                          f"{label} | 파장 미확인 | 포화 {s['saturated_pct']:.4f}% | 최대 DN {s['max_dn']:,}")
        _async(self,lambda:self.api.get('/native-qc',params),done)

    def save_check(self):
        if self.loading or self.current_box is None:return
        data=dict(id=self.item,band=self.band.get(),box=self.current_box)
        _async(self,lambda:self.api.post('/native-qc/save',data),
               lambda result:self.info.set('날짜·회차별 검사 기록 저장 완료 · 정식 ROI 승인과 별개입니다.'))

    def clicked(self,x,y):
        if self.loading or self.cropped or not self.view.image:return
        w,h=self.view.size;point=(min(1,max(0,x/w)),min(1,max(0,y/h)))
        if self.corner is None:
            self.corner=point;self.info.set('첫 점 지정. 타프의 반대쪽 모서리를 클릭하세요.');return
        a,b=self.corner,point
        box=[min(a[0],b[0]),min(a[1],b[1]),max(a[0],b[0]),max(a[1],b[1])]
        if box[2]-box[0]>.001 and box[3]-box[1]>.001:self.load(box)


class CandidatePopup(_PopupBase):
    def __init__(self, parent, base_url, saved=None):
        super().__init__(parent, base_url, "타프 위치와 후보 영상")
        self.saved = saved
        self.context = None
        self.mosaic_image = None
        self.points = [None, None]
        self.circles = [[], []]
        self.radii = [12.0, 8.0]
        self.active_location = tk.IntVar(value=0)
        self.two_locations = tk.BooleanVar(value=False)
        self.outlying_tarp = tk.StringVar(value="PNU_36")
        self.route = []
        self.candidates = []
        self.pairs = []
        self.selected = set()
        self.applied_pairs = set()
        self.previewing = False
        self.preview_item = None
        self.regions = {}
        self.use_regions = tk.BooleanVar(value=False)
        self._header("02  타프 위치 · 후보 영상", "날짜마다 위치를 먼저 확인하세요. 후보 영상을 최종 확정해야 보정·ROI·ELC를 시작할 수 있습니다.")
        body = ttk.Frame(self, padding=(12, 0, 12, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.view = ImageViewport(body, click=self._clicked)
        self.view.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.view.set_overlay(self._draw_overlay)
        side = ttk.Frame(body, width=360)
        side.grid(row=0, column=1, sticky="nsew")
        side.grid_propagate(False)
        ttk.Button(side, text="모자이크 화면 맞춤", command=self._show_mosaic).pack(fill="x")
        self.region_button = ttk.Button(side,text="타프별 영역 직접 지정 · 확대/이동",style="Primary.TButton",command=self._edit_regions)
        self.region_button.pack(fill='x',pady=(6,0))
        ttk.Checkbutton(side,text='저장한 타프별 영역으로 영상 검색',variable=self.use_regions,
                        command=self._region_mode).pack(anchor='w',pady=4)
        position = ttk.Labelframe(side, text="타프 군집 위치", padding=9)
        position.pack(fill="x", pady=(8, 0))
        radius_row = ttk.Frame(position)
        radius_row.pack(fill="x")
        ttk.Label(radius_row, text="현재 위치 반경 (m)").pack(side="left")
        self.radius = tk.DoubleVar(value=12.0)
        radius_input = ttk.Spinbox(radius_row, from_=2, to=50, increment=1, textvariable=self.radius, width=7,
                                    command=self._refresh_circle)
        radius_input.pack(side="right")
        radius_input.bind("<FocusOut>", lambda _event: self._refresh_circle())
        radius_input.bind("<Return>", lambda _event: self._refresh_circle())
        ttk.Checkbutton(position, text="떨어진 타프 위치 추가", variable=self.two_locations,
                        command=self._toggle_location).pack(anchor="w", pady=(8, 2))
        location_row = ttk.Frame(position)
        location_row.pack(fill="x")
        for index, label in enumerate(("A · 두 타프", "B · 떨어진 타프")):
            button = ttk.Radiobutton(location_row, text=label, variable=self.active_location, value=index,
                                     command=self._switch_location)
            button.pack(side="left", padx=(0, 8))
            if index == 0:
                self.a_radio = button
        self.b_tarp_label = ttk.Label(position, text="B 위치의 타프")
        self.tarp_box = ttk.Combobox(position, textvariable=self.outlying_tarp, state="readonly")
        self.position_hint = ttk.Label(position, text="위치를 선택하고 모자이크를 클릭하면 해당 점을 바꿀 수 있습니다.",
                                       style="Subtitle.TLabel", wraplength=325)
        self.position_hint.pack(anchor="w", pady=(7, 5))
        self.search_button = ttk.Button(position, text="원 전체가 들어오는 영상 찾기", style="Primary.TButton",
                                        command=self._search, state="disabled")
        self.search_button.pack(fill="x")
        date_actions=ttk.Frame(position);date_actions.pack(fill='x',pady=(6,0))
        ttk.Button(date_actions,text='날짜 위치 저장',command=self._save_date).pack(side='left')
        ttk.Button(date_actions,text='날짜 위치 불러오기',command=self._load_date).pack(side='left',padx=4)
        result = ttk.Labelframe(side, text="현재 회차 Panel Photos", padding=8)
        result.pack(fill="both", expand=True, pady=(8, 0))
        tools = ttk.Frame(result)
        tools.pack(fill="x")
        ttk.Button(tools, text="전체 선택", command=self._select_all).pack(side="left")
        ttk.Button(tools, text="전체 해제", command=self._clear_all).pack(side="left", padx=4)
        self.band = tk.StringVar(value="6")
        ttk.Label(tools, text="미리보기 밴드").pack(side="left", padx=(10, 3))
        self.band_box = ttk.Combobox(tools, textvariable=self.band, values=tuple(str(i) for i in range(1, 11)),
                                     state="readonly", width=3)
        self.band_box.pack(side="left")
        self.band_box.bind("<<ComboboxSelected>>", lambda _event: self._reload_preview())
        self.tree = ttk.Treeview(result, columns=("pick", "shot", "area", "time", "ready"), show="headings", height=8)
        self.tree.heading("pick", text="선택")
        self.tree.heading("shot", text="촬영번호")
        self.tree.heading("area", text="위치")
        self.tree.heading("time", text="시각 KST")
        self.tree.heading("ready", text="포화 사전검사")
        self.tree.column("pick", width=52, anchor="center")
        self.tree.column("shot", width=95, anchor="center")
        self.tree.column("area", width=44, anchor="center")
        self.tree.column("time", width=75, anchor="center")
        self.tree.column("ready", width=90, anchor="center")
        self.tree.bind("<ButtonRelease-1>", self._tree_click)
        self.tree.bind("<Double-1>", self._preview)
        self.tree.bind('<KeyRelease-Up>',lambda e:self._preview())
        self.tree.bind('<KeyRelease-Down>',lambda e:self._preview())
        self.tree.bind('<space>',lambda e:self._toggle_pick(self.tree.focus()))
        self.pair_frame = ttk.Frame(result)
        ttk.Label(self.pair_frame, text="촬영시각·GPS 연결선 영상 쌍").pack(side="left")
        ttk.Button(self.pair_frame, text="선택한 쌍 적용", command=self._apply_pair).pack(side="right")
        self.pair_tree = ttk.Treeview(result, columns=("a", "b", "dt", "route"), show="headings", height=3)
        for col, label, width in (("a", "A 영상", 75), ("b", "B 영상", 75),
                                  ("dt", "시간차", 75), ("route", "경로", 75)):
            self.pair_tree.heading(col, text=label)
            self.pair_tree.column(col, width=width, anchor="center")
        self.pair_tree.bind("<Double-1>", lambda _event: self._apply_pair())
        footer=ttk.Frame(result);footer.pack(side='bottom',fill='x')
        self.progress = ttk.Progressbar(footer, style="Work.Horizontal.TProgressbar", maximum=1)
        self.progress.pack(fill="x")
        self.status = ttk.Label(footer, text="자료 연결 중", style="Subtitle.TLabel", wraplength=325)
        self.status.pack(anchor="w", pady=(5, 7))
        self.pick_status=ttk.Label(footer,text='☑ 포함 / ☐ 제외 · 타프가 없는 영상은 제외',wraplength=325)
        self.pick_status.pack(fill='x',pady=(0,4))
        self.confirm_button = ttk.Button(footer, text="체크한 영상 최종 확정", style="Primary.TButton", command=self._save)
        self.confirm_button.pack(fill="x")
        ttk.Button(footer,text='타프 이름·경계 확인 (선택 영상 자동 검사)',command=self._native_qc).pack(fill='x',pady=(4,0))
        ttk.Button(footer,text='저장된 자동 결과 확인 · 맞다 / 아니다',command=self._saved_batch).pack(fill='x',pady=(4,0))
        self.tree.pack(fill='both',expand=True,pady=6)
        from .button_guide import ButtonGuide
        self.guide = ButtonGuide(self)
        self.review_geometry = None
        self.guide.watch(self._guide_next)
        _async(self, self._load, self._loaded, self._failed)

    def _geometry_key(self):
        return json.dumps([self.points,self.radii,self.two_locations.get(),self.outlying_tarp.get(),
                           self.use_regions.get(),self.regions],sort_keys=True)

    def _guide_next(self):
        target = None
        if self.context:
            if not self.points[0] or (self.use_regions.get() and not self.regions):
                target = self.region_button
            elif not self.search_button.instate(['disabled']):
                if self.candidates and self.review_geometry == self._geometry_key():
                    if self.selected:target = self.confirm_button
                else:target = self.search_button
        self.guide.show(target)

    def _load(self):
        return self.api.get("/context"), self.api.image("/mosaic.png",{'display':'tarp'}), self.api.get("/state"), self.api.get("/route")

    def _loaded(self, values):
        self.context, self.mosaic_image, state, self.route = values
        self.tarp_box.configure(values=self.context["tarps"])
        role = "해당 회차 모자이크" if self.context["mosaic_role"] == "same_flight" else "대체 모자이크 · 위치 참조 전용"
        self.header_detail.configure(text=f"{self.context['label']}  ·  {role}  ·  {Path(self.context['mosaic']).name}")
        self.view.set_image(self.mosaic_image)
        self.radius.set(self.radii[0])
        self._toggle_location()
        self._region_mode()
        self.status.configure(text='위치 지정 전 · 위의 «타프별 영역 직접 지정»부터 시작하세요. 검색을 눌러야 후보가 표시됩니다.')
        # A current-flight search already has an explicit user location; restore it without a new search.
        locations=state.get('locations',[])
        if state.get('location_review_id') and locations:
            self.regions=state.get('regions') or {};self.use_regions.set(bool(self.regions))
            self.points=[None,None];self.radii=[12.,8.]
            for i,loc in enumerate(locations[:2]):
                self.points[i]=(loc['point']['u'],loc['point']['v']);self.radii[i]=loc['radius_m']
            self.two_locations.set(len(locations)==2);self.active_location.set(0);self.radius.set(self.radii[0])
            if len(locations)==2:self.outlying_tarp.set(locations[1]['tarps'][0])
            self._toggle_location();self._region_mode();self._show_mosaic();self._show_candidates(state)
            self.status.configure(text=f'저장된 타프 위치와 영상 {len(self.candidates)}장 복원 · 재검색 없이 이어서 확인하세요.')

    def _saved_batch(self):
        from .tarp_batch_qc_ui import BatchQcReview
        if not self.context:return
        records=sorted((Path(self.context['output'])/'cache').glob('batch_qc_*/batch.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        for path in records:
            value=json.loads(path.read_text(encoding='utf-8'))
            if set(value.get('selection_snapshot',[]))!=self.selected:continue
            if getattr(self,'batch_window',None) and self.batch_window.winfo_exists():self.batch_window.lift();return
            self.batch_window=BatchQcReview(self,self.api.base_url,path,self.context['tarps']);return
        self.status.configure(text='현재 체크한 영상과 일치하는 검사 결과가 없습니다. 타프 이름·경계 확인에서 자동 검사를 시작하세요.')

    def _edit_regions(self):
        if not self.context or not self.mosaic_image:return
        from .tarp_regions_ui import TarpRegionsPopup
        TarpRegionsPopup(self,self.api,self.context,self.mosaic_image,self._regions_saved)

    def _regions_saved(self,result,search=False):
        self.regions=result['regions'];self.use_regions.set(bool(result.get('complete')))
        if result.get('complete'):
            loc=result['locations'][0]
            self.points=[(loc['u'],loc['v']),None];self.radii=[loc['radius'],8.]
            self.two_locations.set(False);self.active_location.set(0);self.radius.set(loc['radius'])
            self._toggle_location();self._region_mode()
            self._show_mosaic()
            if search:self._search()
        else:self.status.configure(text='영역 일부 저장 · 타프 두 개 이상 지정하면 겹치는 영상 검색이 가능합니다.')

    def _region_mode(self):
        if self.use_regions.get() and len(self.regions)<2:
            self.use_regions.set(False)
            self.status.configure(text='타프별 영역 직접 지정에서 두 타프 이상 저장하세요.')
        self.search_button.configure(text='지정 영역과 겹치는 영상 보기' if self.use_regions.get() else '원 전체가 들어오는 영상 찾기')
        self.position_hint.configure(text='타프별 저장 영역 사용 중 · 수정은 위의 «직접 지정» 버튼' if self.use_regions.get() else '위치를 선택하고 모자이크를 클릭하면 해당 점을 바꿀 수 있습니다.')
        self.view.render()

    def _failed(self, error):
        self.search_button.configure(state="normal")
        self.status.configure(text=error)
        messagebox.showwarning("타프 위치·후보", error, parent=self)

    def _save_date(self):
        if not self.context or not self.points[0] or self.two_locations.get() and not self.points[1]:
            self._failed('모자이크에 타프 위치를 먼저 지정하세요.');return
        try:payload={'locations':self._requested_locations()}
        except (ValueError,tk.TclError) as exc:self._failed(str(exc));return
        _async(self,lambda:self.api.post('/date-location/save',payload),
               lambda r:self.status.configure(text=r['date']+' 위치 저장 · 같은 날짜 다른 회차에서 불러올 수 있습니다.'))

    def _load_date(self):
        def done(value):
            self.use_regions.set(False)
            locations=value['locations'];self.points=[None,None];self.circles=[[],[]]
            for i,loc in enumerate(locations):
                self.points[i]=(loc['u'],loc['v']);self.radii[i]=loc['radius']
            self.two_locations.set(len(locations)==2)
            if len(locations)==2:self.outlying_tarp.set(locations[1]['tarps'][0])
            self.active_location.set(0);self.radius.set(self.radii[0]);self._show_mosaic();self._toggle_location()
            if len(locations)==2:
                loc=locations[1]
                _async(self,lambda:self.api.get('/circle',{k:loc[k] for k in ('u','v','radius')}),lambda c:self._circle_ready(1,c))
            self.status.configure(text=value['date']+' 위치 불러옴 · 현재 회차 영상 검색과 육안 확인이 필요합니다.')
        _async(self,lambda:self.api.get('/date-location'),done)

    def _native_qc(self):
        item=self.tree.focus()
        if item and self.tree.exists(item):
            from .tarp_native_qc_ui import NativeTarpQcPopup
            if getattr(self,'native_qc_window',None) and self.native_qc_window.winfo_exists():
                self.native_qc_window.lift();return
            self.native_qc_window=NativeTarpQcPopup(self,self.api.base_url,item,self.tree.set(item,'shot'),self.context['tarps'],chosen_ids=self.selected)

    def _show_mosaic(self):
        self.previewing = False
        self.preview_item = None
        if self.mosaic_image:
            self.view.set_image(self.mosaic_image)
            self.view.set_overlay(self._draw_overlay)

    def _clicked(self, x, y):
        if self.previewing:
            self._show_mosaic()
            return
        if self.use_regions.get():return
        self.points[self.active_location.get()] = (x / self.mosaic_image.width, y / self.mosaic_image.height)
        self._refresh_circle()

    def _toggle_location(self):
        if not self.two_locations.get():
            self.active_location.set(0)
        self.tarp_box.configure(state="readonly" if self.two_locations.get() else "disabled")
        self.a_radio.configure(text="A · 두 타프" if self.two_locations.get() else "A · 전체 타프")
        if self.two_locations.get():
            self.b_tarp_label.pack(anchor="w", pady=(7, 0), before=self.position_hint)
            self.tarp_box.pack(fill="x", before=self.position_hint)
            self.tree.configure(height=5)
            self.pair_frame.pack(fill="x", before=self.tree)
            self.pair_tree.pack(fill="x", pady=(3, 5), before=self.tree)
        else:
            self.b_tarp_label.pack_forget()
            self.tarp_box.pack_forget()
            self.tree.configure(height=8)
            self.pair_frame.pack_forget()
            self.pair_tree.pack_forget()
        self._switch_location()

    def _switch_location(self):
        index = self.active_location.get()
        if index == 1 and not self.two_locations.get():
            self.active_location.set(0)
            index = 0
        self.radius.set(self.radii[index])
        self._refresh_circle()

    def _refresh_circle(self):
        try:
            radius = float(self.radius.get())
        except (ValueError, tk.TclError):
            return
        index = self.active_location.get()
        self.radii[index] = radius
        self.search_button.configure(state="normal" if 2 <= radius <= 50 and self.points[0]
                                     and (not self.two_locations.get() or self.points[1]) else "disabled")
        point = self.points[index]
        if point and 2 <= radius <= 50:
            u, v = point
            _async(self, lambda: self.api.get("/circle", {"u": u, "v": v, "radius": radius}),
                   lambda circle: self._circle_ready(index, circle))
        self.view.render()

    def _circle_ready(self, index, points):
        self.circles[index] = [(x / 1000 * self.mosaic_image.width, y / 1000 * self.mosaic_image.height) for x, y in points]
        self.view.render()

    def _draw_overlay(self, view):
        if self.previewing:
            return
        if self.use_regions.get():
            for name,points in self.regions.items():
                flat=[v for u,w in points for v in view.image_to_canvas(u*self.mosaic_image.width,w*self.mosaic_image.height)]
                if len(flat)>=6:
                    view.canvas.create_polygon(*flat,fill='',outline=ACCENT,width=2)
                    view.canvas.create_text(flat[0],flat[1]-12,text=name,fill=ACCENT)
            return
        if self.two_locations.get() and len(self.route) > 1:
            flat = []
            for row in self.route[::max(1, len(self.route)//350)]:
                flat.extend(view.image_to_canvas(row['u'] * self.mosaic_image.width,
                                                 row['v'] * self.mosaic_image.height))
            view.canvas.create_line(*flat, fill="#569c99", width=1)
        if self.two_locations.get() and all(self.points):
            ends = [view.image_to_canvas(u*self.mosaic_image.width, v*self.mosaic_image.height)
                    for u, v in self.points]
            view.canvas.create_line(*ends[0], *ends[1], fill="#f9c85c", width=2, dash=(5, 4))
        for index, point in enumerate(self.points[:2 if self.two_locations.get() else 1]):
            if not point:
                continue
            cx, cy = view.image_to_canvas(point[0] * self.mosaic_image.width, point[1] * self.mosaic_image.height)
            color = ACTIVE if index == 0 else "#f9c85c"
            view.canvas.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, fill=color, outline="#111", width=2)
            view.canvas.create_text(cx + 12, cy, text="AB"[index], fill=color, anchor="w")
            if self.circles[index]:
                flat = []
                for x, y in self.circles[index]:
                    flat.extend(view.image_to_canvas(x, y))
                view.canvas.create_line(*flat, fill=color, width=2, smooth=True)

    def _search(self):
        if not self.points[0] or self.two_locations.get() and not self.points[1]:
            return
        self.search_button.configure(state="disabled")
        self.request_geometry = self._geometry_key()
        self.status.configure(text="현재 회차 Panel Photos 검사 중")
        try:
            locations = self._requested_locations()
        except (ValueError, tk.TclError) as exc:
            self.search_button.configure(state="normal")
            self._failed(str(exc))
            return
        payload={'locations':locations,'regions':self.regions if self.use_regions.get() else {}}
        _async(self, lambda: self.api.post("/search", payload),
               lambda _value: self.after(250, self._poll_search), self._failed)

    def _requested_locations(self):
        self.radii[self.active_location.get()] = float(self.radius.get())
        names = self.context['tarps']
        separated = self.outlying_tarp.get()
        locations = []
        for index, point in enumerate(self.points[:2 if self.two_locations.get() else 1]):
            locations.append({'u': point[0], 'v': point[1], 'radius': self.radii[index],
                              'tarps': [separated] if index else [n for n in names if not self.two_locations.get() or n != separated]})
        return locations

    def _poll_search(self):
        _async(self, lambda: self.api.get("/state"), self._search_state, self._failed)

    def _search_state(self, state):
        total = max(1, int(state.get("total", 1)))
        done = int(state.get("done", 0))
        self.progress.configure(maximum=total, value=done)
        self.status.configure(text=f"영상 검사 {done}/{total}")
        if state.get("running"):
            self.after(350, self._poll_search)
        else:
            self.search_button.configure(state="normal")
            self._show_candidates(state)

    def _show_candidates(self, state):
        self.review_geometry = getattr(self,'request_geometry',self._geometry_key())
        self.candidates = list(state.get("candidates", []))
        self.pairs = list(state.get("pairs", []))
        allowed = {row["id"] for row in self.candidates if row.get('fit_coverage_complete',True)}
        self.selected = set(state.get('selected_ids', [])) & allowed
        self.applied_pairs = {i for i in state.get('selected_pairs', []) if isinstance(i, int) and 0 <= i < len(self.pairs)}
        if not self.selected and not state.get('selection_confirmed') and self.two_locations.get() and self.pairs:
            self.selected = {self.pairs[0]['a_id'], self.pairs[0]['b_id']}
        elif not self.selected and not state.get('selection_confirmed') and not self.two_locations.get():
            self.selected = allowed
        for item in self.tree.get_children():
            self.tree.delete(item)
        for row in self.candidates:
            ready = {'clear':'전체 포화 없음','review':'타프 확인','unknown':'검사 불가'}.get(row.get('saturation_qc',{}).get('status'),'미검사') if row.get('radiance_ready') else '원본 누락'
            stamp = row.get('capture_time_kst') or ''
            area=f"{len(row.get('full_tarps',[]))}/{len(self.context['tarps'])}" if state.get('regions') else ''.join(row.get('coverage_locations',['A']))
            self.tree.insert("", "end", iid=row["id"], values=("☑" if row['id'] in self.selected else "☐",
                             row["shot"], area,
                             stamp[11:19] if len(stamp) >= 19 else "미확인", ready))
        for item in self.pair_tree.get_children():
            self.pair_tree.delete(item)
        for index, pair in enumerate(self.pairs):
            self.pair_tree.insert('', 'end', iid=str(index), values=(pair['a_shot'], pair['b_shot'],
                                  f"{pair['delta_seconds']:.1f}s" if pair['delta_seconds'] is not None else '시간 없음',
                                  '같은 장' if pair['same_frame'] else '선 평행' if pair['route_match'] else '경로 확인'))
        error_count = len(state.get("errors", []))
        self.status.configure(text=f"겹치는 영상 {len(self.candidates)}장 · 2개 이상 포함 {len(allowed)}장 · 오류 {error_count}건\n"
                              "체크박스로 포함·제외하세요. 실제 타프 설치·포화는 확인이 필요합니다.")
        self._refresh_tree_marks()
        if self.candidates:
            first = self.candidates[0]["id"]
            self.tree.selection_set(first)
            self.tree.focus(first)
            self.after(60, lambda: self._preview_item(first))

    def _apply_pair(self):
        item = self.pair_tree.focus()
        if not item or int(item) >= len(self.pairs):
            return
        pair = self.pairs[int(item)]
        self.selected.update((pair['a_id'], pair['b_id']))
        self.applied_pairs.add(int(item))
        self._refresh_tree_marks()
        self._preview_item(pair['a_id'])
        self.status.configure(text=f"영상 쌍 {len(self.applied_pairs)}개 누적 · 영상 {len(self.selected)}장 선택 · 개별 제외 가능")

    def _tree_click(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        column = self.tree.identify_column(event.x)
        self.tree.selection_set(item)
        self.tree.focus(item)
        if column == "#1":
            self._toggle_pick(item)
        else:
            self._preview_item(item)

    def _toggle_pick(self,item):
        row=next((r for r in self.candidates if r['id']==item),None)
        if not row:return 'break'
        if not row.get('fit_coverage_complete',True):
            self.status.configure(text='2개 이상의 타프 영역이 완전히 포함된 영상을 선택하세요.')
            self._preview_item(item);return 'break'
        if item in self.selected:self.selected.remove(item)
        else:self.selected.add(item)
        self._refresh_tree_marks()
        return 'break'

    def _refresh_tree_marks(self):
        for row in self.candidates:
            if self.tree.exists(row["id"]):
                values = list(self.tree.item(row["id"], "values"))
                values[0] = "☑" if row["id"] in self.selected else "☐"
                self.tree.item(row["id"], values=values)
        self.applied_pairs = {i for i in self.applied_pairs if
                              {self.pairs[i]['a_id'], self.pairs[i]['b_id']}.issubset(self.selected)}
        self.pick_status.configure(text=f'☑ 포함 {len(self.selected)}장 / ☐ 제외 {len(self.candidates)-len(self.selected)}장 · 타프 미설치 영상 제외')

    def _select_all(self):
        self.selected = {row["id"] for row in self.candidates if row.get('fit_coverage_complete',True)}
        self._refresh_tree_marks()

    def _clear_all(self):
        self.selected.clear()
        self._refresh_tree_marks()

    def _preview(self, event=None):
        item = self.tree.identify_row(event.y) if event else self.tree.focus()
        if not item:
            return
        self._preview_item(item)

    def _reload_preview(self):
        if self.preview_item and self.tree.exists(self.preview_item):
            self._preview_item(self.preview_item)

    def _preview_item(self, item):
        self.preview_item = item
        band=self.band.get()
        self.status.configure(text="후보 영상 불러오는 중 · 모자이크로 돌아가려면 영상을 클릭하세요.")
        _async(self, lambda: self.api.image("/crop.png", {"id": item, "band": band}),
               lambda image: self._preview_ready(image, item) if self.preview_item==item and self.band.get()==band else None, self._failed)

    def _preview_ready(self, image, item):
        self.previewing = True
        self.view.set_overlay(None)
        self.view.set_image(image)
        self.status.configure(text=f"{self.tree.set(item, 'shot')} · 위치 {self.tree.set(item, 'area')} · Band {self.band.get()}\n"
                              "영상을 클릭하면 모자이크로 돌아갑니다.")

    def _save(self):
        if not self.selected:
            messagebox.showwarning("후보 선택", "사용할 영상을 한 장 이상 선택하세요.", parent=self)
            return
        try:
            locations = self._requested_locations()
        except (ValueError, tk.TclError) as exc:
            self._failed(str(exc))
            return
        payload = {"ids": sorted(self.selected), "selected_pairs": sorted(self.applied_pairs),
                   "method": "manual_centers", "locations": locations,
                   'regions':self.regions if self.use_regions.get() else {}}
        if not messagebox.askyesno('사용할 영상 최종 확정',f'포함 {len(self.selected)}장 · 제외 {len(self.candidates)-len(self.selected)}장\n\n타프를 깔기 전이나 치운 뒤의 영상은 체크를 뺐나요?\n체크한 영상만 저장하고 원영상 ROI 단계로 넘깁니다.',parent=self):return
        self.status.configure(text="선택 저장 중")
        _async(self, lambda: self.api.post("/save", payload), self._saved, self._failed)

    def _saved(self, result):
        self.status.configure(text=f"선택 {len(self.selected)}장 저장 완료")
        if self.saved:
            self.saved(result)
        if not self.saved: messagebox.showinfo(
            "후보 저장 완료",
            ("두 위치 영상을 저장했습니다. 03에서 위치별 타프 ROI를 설정합니다. "
             "서로 다른 시각의 영상을 합쳐 ELC 계수를 만드는 05단계는 아직 지원하지 않습니다." if result.get('split_locations') else
             "선택 후보를 저장했습니다. 03에서 원영상 ROI를 설정합니다."),
            parent=self,
        )
        parent, base_url = self.master, self.api.base_url
        self.destroy()
        if not self.saved:
            parent.after(0, lambda: RoiPopup(parent, base_url))


class RoiPopup(_PopupBase):
    def __init__(self, parent, base_url, reference_selected=None):
        super().__init__(parent, base_url, "타프 ROI 설정과 자동 추적")
        self.reference_selected = reference_selected
        self.context = None
        self.selection = None
        self.references = {}
        self.tarps = {}
        self.active_tarp = ""
        self.reference = tk.StringVar()
        self.default_width = tk.DoubleVar(value=65)
        self.default_height = tk.DoubleVar(value=45)
        self.brightness = tk.DoubleVar(value=100)
        self.display_band = tk.StringVar(value="RGB")
        self._image_request = 0
        self.width_value = tk.DoubleVar(value=65)
        self.height_value = tk.DoubleVar(value=45)
        self.job_id = None
        self.result_folder = None
        self.reference_synced = False
        self._header("03  타프 중심 · 경계 · 자동 추적", "native 원영상에서 ROI 좌표를 설정합니다.")
        body = ttk.Frame(self, padding=(12, 0, 12, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.view = ImageViewport(body, click=self._place_tarp)
        self.view.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.view.set_overlay(self._draw_tarps)
        side = ttk.Frame(body, width=365)
        side.grid(row=0, column=1, sticky="nsew")
        side.grid_propagate(False)
        top = ttk.Frame(side)
        top.pack(fill="x")
        ttk.Label(top, text="기준 영상").pack(side="left")
        self.reference_box = ttk.Combobox(top, textvariable=self.reference, state="readonly", width=13)
        self.reference_box.pack(side="left", padx=6, fill="x", expand=True)
        self.reference_box.bind("<<ComboboxSelected>>", lambda _event: self._change_reference())
        band_box = ttk.Combobox(top, textvariable=self.display_band, values=DISPLAY_BANDS, state="readonly", width=15)
        band_box.pack(side="right")
        band_box.bind("<<ComboboxSelected>>", lambda _event: self._request_image(fit=False))
        ttk.Label(top, text="밴드").pack(side="right", padx=(4, 3))
        ttk.Button(side, text="영상 화면 맞춤", command=self.view.fit).pack(fill="x", pady=(7, 0))
        self._brightness_control(side)
        tarp_box = ttk.Labelframe(side, text="타프별 중심과 대략적인 크기", padding=8)
        tarp_box.pack(fill="x", pady=(8, 0))
        default_row = ttk.Frame(tarp_box)
        default_row.pack(fill="x")
        ttk.Label(default_row, text="PNU 기본 가로 × 세로").pack(side="left")
        ttk.Spinbox(default_row, from_=2, to=300, textvariable=self.default_width, width=6).pack(side="left", padx=(8, 3))
        ttk.Label(default_row, text="×").pack(side="left")
        ttk.Spinbox(default_row, from_=2, to=300, textvariable=self.default_height, width=6).pack(side="left", padx=3)
        ttk.Button(tarp_box, text="기본 크기를 모든 PNU에 적용", command=self._apply_default).pack(fill="x", pady=(6, 7))
        self.tarp_buttons = ttk.Frame(tarp_box)
        self.tarp_buttons.pack(fill="x")
        size = ttk.Frame(tarp_box)
        size.pack(fill="x", pady=(7, 0))
        ttk.Label(size, text="현재 가로").pack(side="left")
        w = ttk.Spinbox(size, from_=2, to=300, textvariable=self.width_value, width=6, command=self._change_size)
        w.pack(side="left", padx=(4, 9))
        ttk.Label(size, text="세로").pack(side="left")
        h = ttk.Spinbox(size, from_=2, to=300, textvariable=self.height_value, width=6, command=self._change_size)
        h.pack(side="left", padx=4)
        w.bind("<KeyRelease>", lambda _event: self._change_size())
        h.bind("<KeyRelease>", lambda _event: self._change_size())
        actions = ttk.Frame(tarp_box)
        actions.pack(fill="x", pady=(7, 0))
        ttk.Button(actions, text="현재 타프 지우기", command=self._remove).pack(side="left")
        ttk.Button(actions, text="RGB·다중밴드 경계 찾기", style="Primary.TButton", command=self._boundaries).pack(side="right")
        ttk.Button(tarp_box, text="현재 영상 중심·경계 저장", style="Primary.TButton", command=self._save_seed).pack(fill="x", pady=(7, 0))
        track = ttk.Labelframe(side, text="남은 영상 자동 추적", padding=8)
        track.pack(fill="both", expand=True, pady=(8, 0))
        self.reference_status = ttk.Label(track, text="기준 ROI 확인 중", style="Subtitle.TLabel", wraplength=330)
        self.reference_status.pack(anchor="w")
        self.track_button = ttk.Button(track, text="자동 추적 시작", command=self._start_tracking, state="disabled")
        self.track_button.pack(fill="x", pady=(7, 5))
        self.track_progress = ttk.Progressbar(track, style="Work.Horizontal.TProgressbar", maximum=1)
        self.track_progress.pack(fill="x")
        self.status = ttk.Label(track, text="자료 연결 중", style="Subtitle.TLabel", wraplength=330)
        self.status.pack(anchor="w", pady=(6, 6))
        self.review_button = ttk.Button(track, text="추적 결과 앱에서 검수", command=self._open_review, state="disabled")
        self.review_button.pack(fill="x")
        _async(self, self._load, self._loaded, self._failed)

    def _brightness_control(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(7, 0))
        ttk.Label(row, text="표시 밝기").pack(side="left")
        self.brightness_label = ttk.Label(row, text="100%", style="Subtitle.TLabel", width=5)
        self.brightness_label.pack(side="right")
        ttk.Scale(row, from_=25, to=175, variable=self.brightness,
                  command=self._brightness_changed).pack(side="left", fill="x", expand=True, padx=8)

    def _brightness_changed(self, value):
        percent = round(float(value))
        self.brightness_label.configure(text=f"{percent}%")
        self.view.set_brightness(percent / 100)

    def _load(self):
        return self.api.get("/context"), self.api.get("/selection/latest")

    def _tarps_for(self, shot):
        row = next(r for r in self.selection['selected'] if r['shot'] == shot)
        locations = self.selection.get('locations') or []
        if not locations:
            return self.context['tarps']
        covered = set(row.get('coverage_locations') or [loc['id'] for loc in locations])
        return [name for name in self.context['tarps']
                if any(loc['id'] in covered and name in loc['tarps'] for loc in locations)]

    def _loaded(self, values):
        self.context, self.selection = values
        self.references = dict(self.selection.get("references", {}))
        shots = [row["shot"] for row in self.selection["selected"]]
        self.reference_box.configure(values=shots)
        initial = self.selection.get("seeds", {}).get("reference") or (shots[0] if shots else "")
        self.reference.set(initial)
        source = "native 원영상 RGB · 위치 좌표용" if self.context["source_kind"] == "raw_DN_boundary_only" else "방사보정 RGB"
        self.header_detail.configure(text=f"{self.context['label']}  ·  선택 영상 {len(shots)}장  ·  {source}")
        self._change_reference()
        self._update_ready()
        _async(self, lambda: self.api.get("/tracking/status"), self._tracking_state)

    def _failed(self, error):
        self.status.configure(text=error)
        messagebox.showwarning("타프 ROI", error, parent=self)

    def _change_reference(self):
        shot = self.reference.get()
        self.tarps = json.loads(json.dumps(self.references.get(shot, {})))
        self.active_tarp = self._tarps_for(shot)[0]
        self._refresh_tarp_buttons()
        self.status.configure(text=f"{shot} 불러오는 중")
        self._request_image(fit=True)

    def _request_image(self, fit):
        shot, band = self.reference.get(), _display_band(self.display_band.get())
        self._image_request += 1
        request = self._image_request
        def ready(image):
            if request != self._image_request or shot != self.reference.get():
                return
            self.view.set_image(image, fit=fit)
            self.status.configure(text=f"{shot} · {self.display_band.get()} · 타프 중심과 경계를 확인하세요.")
        _async(self, lambda: self.api.image("/radiance.png", {"shot": shot, "band": band}), ready, self._failed)

    def _refresh_tarp_buttons(self):
        for widget in self.tarp_buttons.winfo_children():
            widget.destroy()
        for name in self._tarps_for(self.reference.get()):
            item = self.tarps.get(name)
            suffix = " · 경계" if item and item.get("points") else " · 중심" if item else ""
            style = "Primary.TButton" if name == self.active_tarp else "TButton"
            ttk.Button(self.tarp_buttons, text=name + suffix, style=style,
                       command=lambda value=name: self._activate(value)).pack(fill="x", pady=2)
        item = self.tarps.get(self.active_tarp, {})
        self.width_value.set(item.get("width", item.get("size", self.default_width.get())))
        self.height_value.set(item.get("height", item.get("size", self.default_height.get())))
        self.view.render()

    def _activate(self, name):
        self.active_tarp = name
        self._refresh_tarp_buttons()

    def _place_tarp(self, x, y):
        width = float(self.width_value.get())
        height = float(self.height_value.get())
        self.tarps[self.active_tarp] = {"x": x, "y": y, "size": max(width, height), "width": width, "height": height}
        self._refresh_tarp_buttons()

    def _draw_tarps(self, view):
        for name, item in self.tarps.items():
            color = ACTIVE if name == self.active_tarp else ACCENT
            points = item.get("points")
            if points:
                flat = []
                for x, y in points:
                    flat.extend(view.image_to_canvas(x, y))
                view.canvas.create_polygon(*flat, outline=color, fill="", width=3)
            else:
                x0 = item["x"] - item.get("width", item["size"]) / 2
                y0 = item["y"] - item.get("height", item["size"]) / 2
                x1 = item["x"] + item.get("width", item["size"]) / 2
                y1 = item["y"] + item.get("height", item["size"]) / 2
                a, b = view.image_to_canvas(x0, y0), view.image_to_canvas(x1, y1)
                view.canvas.create_rectangle(a[0], a[1], b[0], b[1], outline=color, width=3, dash=(6, 4))
            cx, cy = view.image_to_canvas(item["x"], item["y"])
            view.canvas.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, outline="#111", fill=color)
            view.canvas.create_text(cx + 8, cy - 8, text=name, anchor="sw", fill=color,
                                    font=("Malgun Gothic", 10, "bold"))

    def _change_size(self):
        item = self.tarps.get(self.active_tarp)
        if not item:
            return
        try:
            item["width"] = float(self.width_value.get())
            item["height"] = float(self.height_value.get())
            item["size"] = max(item["width"], item["height"])
            item.pop("points", None)
            item.pop("status", None)
            self.view.render()
        except (ValueError, tk.TclError):
            pass

    def _apply_default(self):
        width, height = float(self.default_width.get()), float(self.default_height.get())
        for name, item in self.tarps.items():
            if name.startswith("PNU"):
                item.update(size=max(width, height), width=width, height=height)
                item.pop("points", None)
                item.pop("status", None)
        self._refresh_tarp_buttons()

    def _remove(self):
        self.tarps.pop(self.active_tarp, None)
        self._refresh_tarp_buttons()

    def _boundaries(self):
        if not self.tarps:
            self._failed("타프 중심을 먼저 지정하세요.")
            return
        payload = {"selection_file": self.selection["selection_file"], "reference": self.reference.get(), "tarps": self.tarps}
        self.status.configure(text="정합된 여러 밴드에서 공통 네 변을 찾는 중")
        _async(self, lambda: self.api.post("/roi/boundaries", payload), self._boundaries_ready, self._failed)

    def _boundaries_ready(self, result):
        lines = []
        for name, item in result["results"].items():
            if item.get("status") == "review":
                self.tarps[name].update(item)
                lines.append(name + " · 경계 추정")
            else:
                self.tarps[name].pop("points", None)
                self.tarps[name]["status"] = "failed"
                lines.append(name + " · 중심/크기 확인 필요")
        self._refresh_tarp_buttons()
        self.status.configure(text="\n".join(lines))

    def _save_seed(self):
        if set(self.tarps) != set(self._tarps_for(self.reference.get())) or any(
            len(self.tarps[name].get("points", [])) != 4 or self.tarps[name].get("status") == "failed"
            for name in self._tarps_for(self.reference.get())
        ):
            self._failed("모든 타프의 중심과 경계를 먼저 확인하세요.")
            return
        payload = {"selection_file": self.selection["selection_file"], "reference": self.reference.get(), "tarps": self.tarps}
        _async(self, lambda: self.api.post("/roi/seeds", payload), self._seed_saved, self._failed)

    def _seed_saved(self, _result):
        self.references[self.reference.get()] = json.loads(json.dumps(self.tarps))
        self.status.configure(text="현재 영상 중심·경계 저장 완료 · 다른 기준 영상도 저장하세요.")
        self._update_ready()

    def _update_ready(self):
        complete = []
        for shot, tarps in self.references.items():
            if all(len(tarps.get(name, {}).get("points", [])) == 4 and tarps[name].get("status") != "failed"
                   for name in self._tarps_for(shot)):
                complete.append(shot)
        groups = {tuple(self._tarps_for(row['shot'])) for row in self.selection['selected']}
        missing = [group for group in groups if sum(set(group).issubset(self._tarps_for(shot)) for shot in complete) < 2]
        detail = ' · 추가 기준 필요: ' + ', '.join('/'.join(group) for group in missing) if missing else ''
        self.reference_status.configure(text=f"경계 저장 기준 {len(complete)}장: {', '.join(complete) or '없음'}{detail}")
        self.track_button.configure(state="normal" if not missing and len(complete) >= 2 else "disabled")

    def _start_tracking(self):
        if not messagebox.askyesno("자동 추적", "저장된 기준 ROI를 사용해 나머지 선택 영상을 자동 추적할까요?\n결과는 검수 전 상태로 저장됩니다.", parent=self):
            return
        payload = {"selection_file": self.selection["selection_file"]}
        self.track_button.configure(state="disabled")
        _async(self, lambda: self.api.post("/tracking/start", payload), self._tracking_started, self._failed)

    def _tracking_started(self, state):
        self.job_id = state.get("job_id")
        self._tracking_state(state)

    def _tracking_state(self, state):
        total = max(1, int(state.get("total", 1)))
        self.track_progress.configure(maximum=total, value=int(state.get("done", 0)))
        self.status.configure(text=str(state.get("message", "대기")))
        if state.get("running"):
            self.job_id = state.get("job_id")
            self.after(500, lambda: _async(self, lambda: self.api.get("/tracking/status"), self._tracking_state, self._failed))
            return
        if state.get("status") == "complete" and state.get("job_id"):
            self.job_id = state["job_id"]
            self.result_folder = Path(self.context["output"]) / "web" / "tracking" / self.job_id
            if (self.result_folder / "tracking_results.json").is_file():
                self.review_button.configure(state="normal")
                if self.reference_selected and not self.reference_synced:
                    self.reference_synced = True
                    _async(self, lambda: self.api.get("/review/data", {"job": self.job_id}),
                           self._reference_ready, self._failed)
        self._update_ready()

    def _reference_ready(self, data):
        shot = data.get("reference_shot", "")
        if shot:
            self.reference_selected(shot, data.get("reference_selection", ""))

    def _open_review(self):
        if self.job_id:
            ReviewPopup(self, self.api.base_url, self.job_id, self.context, self.reference_selected)


class ReviewPopup(_PopupBase):
    def __init__(self, parent, base_url, job_id, context, reference_selected=None):
        super().__init__(parent, base_url, "타프 ROI 검수")
        self.reference_selected = reference_selected
        self.job_id = job_id
        self.context = context
        self.data = None
        self.shot = tk.StringVar()
        self.active_tarp = context["tarps"][0]
        self.points = {}
        self.collecting = False
        self.collected = []
        self.reference = tk.StringVar()
        self.trim = tk.DoubleVar(value=5)
        self.trim.trace_add('write', lambda *_: self._update_approval())
        self.percentile_low = tk.DoubleVar(value=5)
        self.percentile_high = tk.DoubleVar(value=95)
        self.brightness = tk.DoubleVar(value=100)
        self.display_band = tk.StringVar(value="RGB")
        self._image_request = 0
        self._header("ROI 수정 · 승인", "꼭짓점을 직접 끌어 수정하거나 네 점을 다시 지정할 수 있습니다.")
        body = ttk.Frame(self, padding=(12, 0, 12, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.view = ImageViewport(body, click=self._collect_point, hit_test=self._hit_vertex, edit_move=self._move_vertex)
        self.view.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.view.set_overlay(self._draw)
        side = ttk.Frame(body, width=350)
        side.grid(row=0, column=1, sticky="nsew")
        side.grid_propagate(False)
        shot_row = ttk.Frame(side)
        shot_row.pack(fill="x")
        ttk.Label(shot_row, text="영상").pack(side="left")
        band_box = ttk.Combobox(shot_row, textvariable=self.display_band, values=DISPLAY_BANDS, state="readonly", width=17)
        band_box.pack(side="right")
        band_box.bind("<<ComboboxSelected>>", lambda _event: self._request_image(fit=False))
        ttk.Label(shot_row, text="밴드").pack(side="right", padx=(4, 3))
        self.shot_box = ttk.Combobox(side, textvariable=self.shot, state="readonly")
        self.shot_box.pack(fill="x", pady=(3, 7))
        self.shot_box.bind("<<ComboboxSelected>>", lambda _event: self._change_shot())
        zoom_row = ttk.Frame(side)
        zoom_row.pack(fill="x")
        ttk.Button(zoom_row, text="전체 영상", command=self.view.fit).pack(side="left", fill="x", expand=True)
        ttk.Button(zoom_row, text="선택 타프 확대", command=self._zoom_active_tarp).pack(side="left", fill="x", expand=True, padx=(5, 0))
        brightness_row = ttk.Frame(side)
        brightness_row.pack(fill="x", pady=(7, 0))
        ttk.Label(brightness_row, text="표시 밝기").pack(side="left")
        self.brightness_label = ttk.Label(brightness_row, text="100%", style="Subtitle.TLabel", width=5)
        self.brightness_label.pack(side="right")
        ttk.Scale(brightness_row, from_=25, to=175, variable=self.brightness,
                  command=self._brightness_changed).pack(side="left", fill="x", expand=True, padx=8)
        self.tarp_buttons = ttk.Frame(side)
        self.tarp_buttons.pack(fill="x", pady=8)
        ttk.Button(side, text="현재 타프 네 점 다시 지정", command=self._reset_points).pack(fill="x")
        reference_box = ttk.Labelframe(side, text="승인과 통계", padding=8)
        reference_box.pack(fill="x", pady=(8, 0))
        ttk.Label(reference_box, text="자동 기준 영상 · 전체 관측 중간시각").pack(anchor="w")
        self.reference_box = ttk.Combobox(reference_box, textvariable=self.reference, state="readonly")
        self.reference_box.pack(fill="x", pady=(3, 7))
        row = ttk.Frame(reference_box)
        row.pack(fill="x")
        ttk.Button(row, text="수정 저장", command=lambda: self._save(False)).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="현재 영상 승인", style="Primary.TButton", command=lambda: self._save(True)).pack(side="left", fill="x", expand=True, padx=(5, 0))
        self.approve_all_button = ttk.Button(reference_box, text="전체 영상 일괄 승인", command=self._approve_all, state="disabled")
        self.approve_all_button.pack(fill="x", pady=(6, 0))
        pixel_row = ttk.Frame(reference_box)
        pixel_row.pack(fill="x", pady=(6, 0))
        self.pixel_button = ttk.Button(pixel_row, text="내부 화소 검사", command=self._inspect_pixels, state="disabled")
        self.pixel_button.pack(side="left", fill="x", expand=True)
        self.pixel_batch_button = ttk.Button(pixel_row, text="미승인 일괄 승인", command=self._approve_pixels_batch, state="disabled")
        self.pixel_batch_button.pack(side="left", fill="x", expand=True, padx=(5, 0))
        trim_row = ttk.Frame(reference_box)
        trim_row.pack(fill="x", pady=(9, 5))
        ttk.Label(trim_row, text="공간 Trim (%)").pack(side="left")
        ttk.Spinbox(trim_row, from_=0, to=20, textvariable=self.trim, width=6).pack(side="right")
        percentile_row = ttk.Frame(reference_box)
        percentile_row.pack(fill="x", pady=(0, 5))
        ttk.Label(percentile_row, text="값 필터 P").pack(side="left")
        ttk.Spinbox(percentile_row, from_=0, to=99, textvariable=self.percentile_low, width=5).pack(side="left", padx=(6, 3))
        ttk.Label(percentile_row, text="~ P").pack(side="left")
        ttk.Spinbox(percentile_row, from_=1, to=100, textvariable=self.percentile_high, width=5).pack(side="left", padx=(3, 0))
        self.stats_button = ttk.Button(reference_box, text="승인 ROI 통계 생성", command=self._statistics, state="disabled")
        self.stats_button.pack(fill="x")
        self.progress = ttk.Progressbar(reference_box, style="Work.Horizontal.TProgressbar", maximum=1)
        self.progress.pack(fill="x", pady=(7, 0))
        self.status = ttk.Label(side, text="추적 결과 불러오는 중", style="Subtitle.TLabel", wraplength=330)
        self.status.pack(anchor="w", pady=(8, 5))
        self.folder_button = ttk.Button(side, text="생성 결과 폴더 열기", command=self._open_folder, state="disabled")
        self.folder_button.pack(fill="x")
        _async(self, lambda: self.api.get("/review/data", {"job": job_id}), self._loaded, self._failed)

    def _brightness_changed(self, value):
        percent = round(float(value))
        self.brightness_label.configure(text=f"{percent}%")
        self.view.set_brightness(percent / 100)

    def _failed(self, error):
        self.status.configure(text=error)
        if self.data:
            self._update_approval()
        messagebox.showwarning("ROI 검수", error, parent=self)

    def _loaded(self, data):
        self.data = data
        shots = list(data["images"])
        self.shot_box.configure(values=shots)
        self.reference_box.configure(values=shots)
        self.shot.set(shots[0])
        self.reference.set(data.get("reference_shot") or "")
        saved_trim = self._saved_pixel_trim(data)
        if saved_trim is not None:
            self.trim.set(saved_trim)
        if self.reference.get() and self.reference_selected:
            self.reference_selected(self.reference.get(), data.get("reference_selection", ""))
        self._change_shot()
        self._update_approval()
        if self.context["source_kind"] == "raw_DN_boundary_only":
            self.stats_button.configure(text="원영상 ROI 분포 QC 생성")
        if not self.reference.get() and data.get("reference_error"):
            self.status.configure(text=data["reference_error"] + " 수동으로 기준 영상을 선택하세요.")

    def _change_shot(self):
        item = self.data["images"][self.shot.get()]
        self.points = json.loads(json.dumps(item["tarps"]))
        self.active_tarp = item.get('expected_tarps', self.context['tarps'])[0]
        self.collecting = False
        self._buttons()
        self.status.configure(text=f"{self.shot.get()} 불러오는 중")
        self._request_image(fit=True)
        self._update_approval()

    def _request_image(self, fit):
        shot, band = self.shot.get(), _display_band(self.display_band.get())
        self._image_request += 1
        request = self._image_request
        def ready(image):
            if request != self._image_request or shot != self.shot.get():
                return
            self.view.set_image(image, fit=fit)
            state = "승인됨" if self.data["images"][shot].get("approved") else "검수 필요"
            self.status.configure(text=f"{shot} · {self.display_band.get()} · {state}")
        _async(self, lambda: self.api.image("/radiance.png", {"shot": shot, "band": band}), ready, self._failed)

    def _buttons(self):
        for widget in self.tarp_buttons.winfo_children():
            widget.destroy()
        for name in self.data['images'][self.shot.get()].get('expected_tarps', self.context['tarps']):
            ttk.Button(self.tarp_buttons, text=name, style="Primary.TButton" if name == self.active_tarp else "TButton",
                       command=lambda value=name: self._activate(value)).pack(fill="x", pady=2)
        self.view.render()

    def _activate(self, name):
        self.active_tarp = name
        self.collecting = False
        self._buttons()
        self._zoom_active_tarp()

    def _zoom_active_tarp(self):
        if not self.view.focus_polygon(self.points.get(self.active_tarp) or []):
            self.status.configure(text=f"{self.active_tarp}: 확대할 경계가 없습니다. 네 점을 먼저 지정하세요.")

    def _draw(self, view):
        for name, points in self.points.items():
            if not points:
                continue
            color = ACTIVE if name == self.active_tarp else ACCENT
            flat = []
            for x, y in points:
                flat.extend(view.image_to_canvas(x, y))
            view.canvas.create_polygon(*flat, outline=color, fill="", width=3)
            for index, (x, y) in enumerate(points):
                cx, cy = view.image_to_canvas(x, y)
                radius = 6 if name == self.active_tarp else 3
                view.canvas.create_oval(cx-radius, cy-radius, cx+radius, cy+radius, fill=color, outline="#111")
                if name == self.active_tarp:
                    view.canvas.create_text(cx + 9, cy - 8, text=str(index + 1), fill=color, anchor="sw")
        if self.collecting:
            for x, y in self.collected:
                cx, cy = view.image_to_canvas(x, y)
                view.canvas.create_oval(cx-6, cy-6, cx+6, cy+6, fill="#ef929b", outline="#111")

    def _hit_vertex(self, x, y, scale):
        points = self.points.get(self.active_tarp) or []
        for index, (px, py) in enumerate(points):
            if ((x-px)**2 + (y-py)**2) ** .5 * scale <= 12:
                return index
        return None

    def _move_vertex(self, index, x, y):
        if self.active_tarp in self.points:
            self.points[self.active_tarp][index] = [x, y]

    def _reset_points(self):
        self.collecting = True
        self.collected = []
        self.status.configure(text=f"{self.active_tarp}: 꼭짓점 네 개를 순서대로 클릭하세요.")
        self.view.render()

    def _collect_point(self, x, y):
        if not self.collecting:
            return
        self.collected.append([x, y])
        if len(self.collected) == 4:
            self.points[self.active_tarp] = self.collected
            self.collecting = False
            self.status.configure(text=f"{self.active_tarp}: 네 점 지정 완료 · 저장 전")
        self.view.render()

    def _save(self, approved):
        payload = {"job_id": self.job_id, "shot": self.shot.get(), "tarps": self.points,
                   "approved": approved, "reference_shot": self.reference.get()}
        _async(self, lambda: self.api.post("/review/save", payload), lambda data: self._saved(data, approved), self._failed)

    def _saved(self, data, approved):
        self.data = data
        if self.reference.get() and self.reference_selected:
            self.reference_selected(self.reference.get(), data.get("reference_selection", "user_saved"))
        self.status.configure(text="현재 영상 ROI 승인 완료" if approved else "수정 저장 완료 · 미승인")
        self._update_approval()

    def _approve_all(self):
        if self.collecting:
            self._failed("현재 타프의 네 꼭짓점 지정을 마친 뒤 승인하세요.")
            return
        remaining = sum(not item.get("approved") for item in self.data["images"].values())
        if not messagebox.askyesno("전체 영상 일괄 승인", f"미승인 {remaining}장의 자동 추적 ROI를 한 번에 승인합니다.\n"
                                  "경계가 잘못된 영상은 먼저 수정해야 합니다. 계속할까요?", parent=self):
            return
        payload = {"job_id": self.job_id, "shot": self.shot.get(), "tarps": self.points,
                   "reference_shot": self.reference.get()}
        self.approve_all_button.configure(state="disabled")
        _async(self, lambda: self.api.post("/review/approve-all", payload), self._approved_all, self._failed)

    def _approved_all(self, data):
        self.data = data
        if self.reference.get() and self.reference_selected:
            self.reference_selected(self.reference.get(), data.get("reference_selection", "user_saved"))
        self.status.configure(text="전체 영상 ROI 일괄 승인 완료")
        self._update_approval()

    def _update_approval(self):
        if not self.data:
            return
        approved = sum(1 for item in self.data["images"].values() if item.get("approved"))
        total = len(self.data["images"])
        try:
            trim = float(self.trim.get())
        except tk.TclError:
            trim = -1
        pending = self._pending_pixel_masks(self.data, trim)
        needed = sum(tarp in item['tarps'] for item in self.data['images'].values()
                     for tarp in ('PNU_36', 'PNU_56'))
        masks = needed - len(pending)
        self.approve_all_button.configure(state="normal" if approved < total and self.reference.get() else "disabled")
        self.pixel_button.configure(state="normal" if self.data['images'][self.shot.get()].get('approved') else "disabled")
        self.pixel_batch_button.configure(text=f"미승인 {len(pending)}건 일괄 승인",
                                          state="normal" if approved == total and pending else "disabled")
        self.stats_button.configure(state="normal" if approved == total and not pending and self.reference.get() else "disabled")
        self.header_detail.configure(text=f"{self.data['label']}  ·  ROI {approved}/{total}  ·  내부 화소 {masks}/{needed}")

    @staticmethod
    def _pending_pixel_masks(data, trim):
        return [(shot, tarp) for shot, item in data['images'].items()
                for tarp in ('PNU_36', 'PNU_56') if tarp in item['tarps']
                and not (item.get('pixel_masks', {}).get(tarp, {}).get('trim_percent') == trim
                         and item.get('pixel_masks', {}).get(tarp, {}).get('polygon_saved_at_ns') == item.get('saved_at_ns'))]

    @staticmethod
    def _saved_pixel_trim(data):
        trims = {mask.get('trim_percent') for item in data['images'].values()
                 for mask in item.get('pixel_masks', {}).values()
                 if mask.get('polygon_saved_at_ns') == item.get('saved_at_ns')}
        return trims.pop() if len(trims) == 1 else None

    def _approve_pixels_batch(self):
        try:
            trim = float(self.trim.get())
        except (ValueError, tk.TclError):
            return self._failed("공간 Trim 값을 확인하세요.")
        pending = self._pending_pixel_masks(self.data, trim)
        if not pending:
            return
        if not messagebox.askyesno("내부 화소 일괄 승인",
                                   f"미승인 {len(pending)}건을 한 번에 승인합니다.\n"
                                   "기존 수정 후보는 유지하고, 후보가 없으면 새로 계산합니다.\n"
                                   "포화로 제외 후보가 0개인 타프도 '제외 없음'으로 승인합니다.\n"
                                   "제외 범위가 넓어 수동 판단이 필요한 후보는 건너뜁니다.\n"
                                   "원영상 포화 QC는 별도로 진행됩니다. 계속할까요?", parent=self):
            return
        self.pixel_batch_button.configure(state="disabled")
        self.status.configure(text=f"내부 화소 {len(pending)}건 일괄 승인 중")
        snapshot = json.loads(json.dumps(self.data))
        _async(self, lambda: self._approve_pixels_batch_work(snapshot, trim, pending),
               self._pixels_batch_done, self._failed)

    def _approve_pixels_batch_work(self, snapshot, trim, pending):
        proposals = Path(self.context['output']) / 'web' / 'tracking' / self.job_id / 'pixel_proposals'
        ready, deferred = [], []
        for shot, tarp in pending:
            item = snapshot['images'][shot]
            info = None
            for directory in sorted(proposals.glob(f'{shot}_{tarp}_*'), reverse=True):
                path = directory / 'proposal.json'
                if not path.is_file() or not (directory / 'exclude.png').is_file():
                    continue
                candidate = json.loads(path.read_text(encoding='utf-8'))
                if (candidate.get('shot') == shot and candidate.get('tarp') == tarp
                        and candidate.get('trim_percent') == trim
                        and candidate.get('polygon_saved_at_ns') == item.get('saved_at_ns')
                        and candidate.get('points') == item['tarps'][tarp]
                        and not candidate.get('approved')):
                    info = candidate
                    break
            if info is None:
                info = self.api.post('/review/pixels/propose',
                                     {'job_id': self.job_id, 'shot': shot, 'tarp': tarp, 'trim_percent': trim})
            if (info['status'] == 'manual_review' and info['excluded_pixels'] > 0
                    or info.get('excluded_pct', 0) > 25):
                deferred.append(f'{shot} {tarp}')
            else:
                ready.append(info['proposal_id'])
        for proposal_id in ready:
            self.api.post('/review/pixels/approve', {'job_id': self.job_id, 'proposal_id': proposal_id})
        return len(ready), deferred

    def _pixels_batch_done(self, result):
        approved, deferred = result
        note = f"내부 화소 {approved}건 일괄 승인 완료"
        if deferred:
            note += " · 개별 확인 필요: " + ', '.join(deferred)
        self.status.configure(text=note)
        self._pixel_approved()

    def _inspect_pixels(self):
        PixelMaskPopup(self, self.api.base_url, self.job_id, self.shot.get(), self.active_tarp,
                       float(self.trim.get()), self._pixel_approved)

    def _pixel_approved(self):
        _async(self, lambda: self.api.get('/review/data', {'job': self.job_id}), self._pixel_reloaded, self._failed)

    def _pixel_reloaded(self, data):
        self.data = data
        self._update_approval()

    def _statistics(self):
        try:
            payload = {"job_id": self.job_id, "trim_percent": float(self.trim.get()),
                       "percentile_low": float(self.percentile_low.get()),
                       "percentile_high": float(self.percentile_high.get())}
        except (ValueError, tk.TclError):
            self._failed("Trim과 값 필터 P 범위를 숫자로 입력하세요.")
            return
        self.stats_button.configure(state="disabled")
        _async(self, lambda: self.api.post("/review/statistics", payload), self._stats_started, self._failed)

    def _stats_started(self, state):
        self.stats_job = state.get("job_id")
        self._stats_state(state)

    def _stats_state(self, state):
        total = max(1, int(state.get("total", 1)))
        self.progress.configure(maximum=total, value=int(state.get("done", 0)))
        self.status.configure(text=str(state.get("message", "통계 생성 중")))
        if state.get("running"):
            self.after(500, lambda: _async(self, lambda: self.api.get("/tracking/status"), self._stats_state, self._failed))
        elif state.get("status") == "complete":
            self.result_folder = Path(self.context["output"]) / "web" / "tracking" / state["job_id"]
            self.folder_button.configure(state="normal")
            self._update_approval()
        else:
            self._update_approval()

    def _open_folder(self):
        if self.result_folder and self.result_folder.is_dir():
            os.startfile(str(self.result_folder))


class PixelMaskPopup(_PopupBase):
    def __init__(self, parent, base_url, job_id, shot, tarp, trim, on_approved):
        super().__init__(parent, base_url, "ROI 내부 화소 검사")
        self.parent_review = parent
        self.job_id, self.shot, self.tarp, self.trim = job_id, shot, tarp, trim
        self.on_approved = on_approved
        self.proposal = None
        self.sensitivity = tk.DoubleVar(value=3.0)
        self.brush = tk.IntVar(value=4)
        self.exclude = tk.BooleanVar(value=True)
        self._header(f"{shot} · {tarp}", "빨강: 제외 후보 · 청록: 승인된 타프 경계")
        body = ttk.Frame(self, padding=(12, 0, 12, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.view = ImageViewport(body, click=self._paint)
        self.view.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        side = ttk.Frame(body, width=270)
        side.grid(row=0, column=1, sticky="nsew")
        side.grid_propagate(False)
        self.shot_choice = tk.StringVar(value=shot)
        self.tarp_choice = tk.StringVar(value=tarp)
        ttk.Label(side, text="영상").pack(anchor="w")
        shots = [key for key, record in parent.data['images'].items() if record.get('approved')]
        shot_box = ttk.Combobox(side, textvariable=self.shot_choice, values=shots, state="readonly")
        shot_box.pack(fill="x", pady=(2, 8))
        shot_box.bind('<<ComboboxSelected>>', lambda _event: self._switch())
        ttk.Label(side, text="타프").pack(anchor="w")
        tarp_box = ttk.Combobox(side, textvariable=self.tarp_choice, values=parent.context['tarps'], state="readonly")
        tarp_box.pack(fill="x", pady=(2, 8))
        tarp_box.bind('<<ComboboxSelected>>', lambda _event: self._switch())
        ttk.Label(side, text=f"공간 Trim {trim:g}%").pack(anchor="w")
        ttk.Label(side, text="그림자 민감도").pack(anchor="w", pady=(12, 0))
        ttk.Spinbox(side, from_=1.5, to=6, increment=.5, textvariable=self.sensitivity, width=8).pack(anchor="w")
        ttk.Button(side, text="제외 후보 다시 계산", command=self._propose).pack(fill="x", pady=(8, 12))
        ttk.Checkbutton(side, text="클릭한 곳 제외", variable=self.exclude).pack(anchor="w")
        ttk.Label(side, text="해제하면 클릭한 곳 복원").pack(anchor="w")
        ttk.Label(side, text="브러시 반경 (화소)").pack(anchor="w", pady=(12, 0))
        ttk.Spinbox(side, from_=1, to=20, textvariable=self.brush, width=8).pack(anchor="w")
        self.count = ttk.Label(side, text="후보 계산 중", style="Subtitle.TLabel", wraplength=260)
        self.count.pack(anchor="w", pady=(15, 10))
        self.approve_button = ttk.Button(side, text="현재 제외 화소 승인", style="Primary.TButton",
                                         command=self._approve, state="disabled")
        self.approve_button.pack(fill="x")
        self.status = ttk.Label(side, text="", style="Subtitle.TLabel", wraplength=260)
        self.status.pack(anchor="w", pady=12)
        self._propose()

    def _failed(self, error):
        self.status.configure(text=str(error))
        messagebox.showwarning("ROI 내부 화소", str(error), parent=self)

    def _propose(self):
        self.proposal = None
        self.approve_button.configure(state="disabled")
        self.status.configure(text="후보 계산 중")
        payload = {'job_id': self.job_id, 'shot': self.shot, 'tarp': self.tarp,
                   'trim_percent': self.trim, 'sensitivity': float(self.sensitivity.get())}
        _async(self, lambda: self.api.post('/review/pixels/propose', payload), self._proposed, self._failed)

    def _proposed(self, result):
        self.proposal = result['proposal_id']
        self.count.configure(text=f"ROI {result['roi_pixels']}화소 · 제외 후보 {result['excluded_pixels']}화소 "
                                  f"({result['excluded_pct']}%)\n"
                                  f"판단 밴드: {', '.join('B'+str(b) for b in result['used_bands']) or '없음'}")
        message = ("자동 판단 주의: 포화·밴드 부족 또는 제외 범위 과다. 직접 확인하세요."
                   if result['status'] == 'manual_review' else "후보를 확인하고 필요하면 클릭으로 제외·복원하세요.")
        self.status.configure(text=message)
        self.approve_button.configure(state="normal")
        self.points = result['points']
        self._preview(fit=True)

    def _switch(self):
        self.shot, self.tarp = self.shot_choice.get(), self.tarp_choice.get()
        self.header_detail.configure(text=f'{self.shot} · {self.tarp} · 빨강: 제외 후보')
        self._propose()

    def _preview(self, fit=False):
        proposal = self.proposal
        def ready(image):
            if proposal == self.proposal:
                self.view.set_image(image, fit=fit)
                if fit:
                    self.after(150, lambda: self.view.focus_polygon(self.points))
        _async(self, lambda: self.api.image('/review/pixels/preview',
                                            {'job': self.job_id, 'proposal': proposal}), ready, self._failed)

    def _paint(self, x, y):
        if not self.proposal:
            return
        payload = {'job_id': self.job_id, 'proposal_id': self.proposal, 'x': x, 'y': y,
                   'radius': int(self.brush.get()), 'exclude': self.exclude.get()}
        _async(self, lambda: self.api.post('/review/pixels/edit', payload), self._painted, self._failed)

    def _painted(self, result):
        self.count.configure(text=f"ROI {result['roi_pixels']}화소 · 제외 {result['excluded_pixels']}화소 "
                                  f"({result['excluded_pct']}%)")
        self._preview()

    def _approve(self):
        if self.proposal:
            self.approve_button.configure(state="disabled")
            _async(self, lambda: self.api.post('/review/pixels/approve',
                                               {'job_id': self.job_id, 'proposal_id': self.proposal}),
                   self._approved, self._failed)

    def _approved(self, result):
        self.status.configure(text=f"{result['tarp']}: 제외 {result['excluded_pixels']}화소 승인 완료")
        self.on_approved()


def open_candidate_popup(parent, base_url, saved=None):
    return CandidatePopup(parent, base_url, saved=saved)


def open_roi_popup(parent, base_url, reference_selected=None):
    return RoiPopup(parent, base_url, reference_selected=reference_selected)
