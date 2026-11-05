"""Manually drawn map hints, distinct from native calibration ROI approval."""
import base64
import io
import json
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk, ImageEnhance
from .tarp_popup import ImageViewport, _PopupBase, _async, ACCENT, MUTED


class DetailViewport(ImageViewport):
    """Overview coordinates stay fixed while visible native pixels are reloaded."""
    def __init__(self, parent, api, **kwargs):
        self.api=api;self.detail=None;self.detail_key=None;self.detail_timer=None
        self.request_key=None;self.on_detail=None
        self.display='tarp'
        super().__init__(parent,**kwargs)
        self.canvas.bind('<ButtonPress-3>',self._pan_start)
        self.canvas.bind('<B3-Motion>',self._move)
        self.canvas.bind('<ButtonRelease-3>',lambda e:setattr(self,'drag',None))

    def _pan_start(self,event):
        self.edit_token=None
        self.drag=(event.x,event.y,self.offset_x,self.offset_y,True)

    def zoom(self,factor,x=None,y=None):
        if not self.image:return
        x=self.canvas.winfo_width()/2 if x is None else x
        y=self.canvas.winfo_height()/2 if y is None else y
        ix,iy=self.canvas_to_image(x,y)
        self.scale=max(self.fit_scale*.5,min(self.fit_scale*256,self.scale*factor))
        self.offset_x=x-ix*self.scale;self.offset_y=y-iy*self.scale
        self.render()

    def _wheel(self,event):
        self.zoom(1.3 if event.delta>0 else 1/1.3,event.x,event.y)
        return 'break'

    def render(self):
        super().render()
        if not self.image:return
        w,h=self.size;cw,ch=self.canvas.winfo_width(),self.canvas.winfo_height()
        x0,y0=self.canvas_to_image(0,0);x1,y1=self.canvas_to_image(cw,ch)
        box=(max(0,x0/w),max(0,y0/h),min(1,x1/w),min(1,y1/h))
        if box[2]<=box[0] or box[3]<=box[1]:return
        key=tuple(round(v,9) for v in box)
        if self.detail_key==key and self.detail:
            im,b=self.detail
            left,top=self.image_to_canvas(b[0]*w,b[1]*h)
            width=max(1,round((b[2]-b[0])*w*self.scale));height=max(1,round((b[3]-b[1])*h*self.scale))
            tile=ImageEnhance.Brightness(im).enhance(self.brightness)
            self.detail_photo=ImageTk.PhotoImage(tile.resize((width,height),Image.Resampling.BILINEAR))
            self.canvas.create_image(left,top,anchor='nw',image=self.detail_photo)
            if self.overlay:self.overlay(self)
        elif self.scale>self.fit_scale*1.2 and key!=self.request_key:
            if self.detail_timer:self.after_cancel(self.detail_timer)
            self.request_key=key
            self.detail_timer=self.after(180,lambda:self._load_detail(key))

    def _load_detail(self,key):
        self.detail_timer=None
        def done(value):
            if self.request_key!=key:return
            self.detail=(Image.open(io.BytesIO(base64.b64decode(value['image']))).convert('RGB'),value['box'])
            self.detail_key=key;self.render()
            if self.on_detail:self.on_detail('원본 부분 영상 표시 중 · 휠 확대/축소 · 드래그 이동')
        def failed(error):
            if self.request_key==key and self.on_detail:self.on_detail('상세 영상 읽기 실패: '+error)
        display=self.display
        _async(self,lambda:self.api.get('/mosaic-detail',{'box':json.dumps(key),'display':display}),
               lambda value:done(value) if self.display==display else None,failed)


class TarpRegionsPopup(_PopupBase):
    def __init__(self,parent,api,context,overview,saved):
        super().__init__(parent,api.base_url,'타프별 영역 지정 · 확대 / 이동')
        self.context=context;self.saved=saved;self.regions={};self.dirty=False;self.ready=False
        self.active=tk.StringVar(value=context['tarps'][0]);self.mode=tk.StringVar(value='move')
        self._header('타프별 영역 직접 지정',context['label']+' · 모자이크 위치 힌트 / 원영상 ROI 승인은 다음 단계')
        tools=ttk.Frame(self,padding=(12,0,12,8));tools.pack(fill='x')
        ttk.Button(tools,text='＋ 확대',command=lambda:self.view.zoom(1.5)).pack(side='left')
        ttk.Button(tools,text='－ 축소',command=lambda:self.view.zoom(1/1.5)).pack(side='left',padx=4)
        ttk.Button(tools,text='전체 보기',command=lambda:self.view.fit()).pack(side='left')
        ttk.Radiobutton(tools,text='이동',variable=self.mode,value='move').pack(side='left',padx=(18,4))
        ttk.Radiobutton(tools,text='네 모서리 지정',variable=self.mode,value='draw').pack(side='left')
        ttk.Label(tools,text='휠: 확대/축소  ·  드래그: 이동  ·  꼭짓점 드래그: 수정').pack(side='left',padx=18)
        display_row=ttk.Frame(self,padding=(12,0,12,8));display_row.pack(fill='x')
        ttk.Label(display_row,text='화면 표시').pack(side='left')
        self.display_mode=tk.StringVar(value='아스팔트·밝은 타프 (전체 범위)')
        combo=ttk.Combobox(display_row,textvariable=self.display_mode,values=('아스팔트·밝은 타프 (전체 범위)','기존 모자이크 (P01–P96)'),width=31,state='readonly')
        combo.pack(side='left',padx=8);combo.bind('<<ComboboxSelected>>',lambda e:self.reload_display())
        ttk.Label(display_row,text='표시 밝기').pack(side='left',padx=(14,8))
        self.brightness=tk.DoubleVar(value=1)
        ttk.Scale(display_row,from_=.25,to=1.5,variable=self.brightness,length=250,
                  command=lambda value:self.view.set_brightness(value)).pack(side='left')
        ttk.Button(display_row,text='밝기 초기화',command=self.reset_brightness).pack(side='left',padx=8)
        ttk.Label(display_row,text='표시만 변경 · 원자료 / DN 포화는 그대로').pack(side='left',padx=10)
        body=ttk.Frame(self,padding=(12,0,12,10));body.pack(fill='both',expand=True)
        side=ttk.Frame(body,width=265);side.pack(side='right',fill='y',padx=(12,0));side.pack_propagate(False)
        ttk.Label(side,text='1  타프 이름 선택',style='Title.TLabel').pack(anchor='w',pady=(0,10))
        self.labels={}
        for name in context['tarps']:
            row=ttk.Frame(side);row.pack(fill='x',pady=5)
            ttk.Radiobutton(row,text=name,variable=self.active,value=name,command=self.choose).pack(side='left')
            label=ttk.Label(row,text='미지정');label.pack(side='right');self.labels[name]=label
        ttk.Label(side,text='2  네 모서리 지정',style='Title.TLabel').pack(anchor='w',pady=(18,8))
        ttk.Label(side,text='타프 둘레를 따라 네 번 클릭하세요.\n두 타프 이상 지정하면 검색할 수 있습니다.\n휠 확대 · 빈 곳 드래그 이동\n완성된 꼭짓점은 끌어서 고칩니다.',wraplength=250).pack(anchor='w')
        ttk.Button(side,text='선택 타프 확대',command=self.focus_tarp).pack(fill='x',pady=(12,4))
        ttk.Button(side,text='마지막 점 취소',command=self.undo).pack(fill='x',pady=4)
        ttk.Button(side,text='선택 타프 다시 그리기',command=self.clear).pack(fill='x',pady=4)
        ttk.Label(side,text='3  저장 후 영상 확인',style='Title.TLabel').pack(anchor='w',pady=(20,8))
        ttk.Label(side,text='위치가 겹치는 Photos를 목록으로 확인합니다. 타프가 없는 영상은 체크를 빼고 최종 선택을 확정하세요.',wraplength=250).pack(anchor='w')
        self.save_button=ttk.Button(side,text='영역 저장',command=self.save,state='disabled');self.save_button.pack(fill='x',pady=(16,4))
        self.search_button=ttk.Button(side,text='저장하고 겹치는 영상 보기',style='Primary.TButton',command=lambda:self.save(search=True),state='disabled')
        self.search_button.pack(fill='x',pady=4)
        from .button_guide import ButtonGuide
        self.guide = ButtonGuide(self)
        self.info=tk.StringVar(value='저장한 영역 확인 중')
        ttk.Label(side,textvariable=self.info,wraplength=250,foreground=ACCENT).pack(fill='x',pady=16)
        self.view=DetailViewport(body,self.api,click=self.clicked,hit_test=self.hit,edit_move=self.move_vertex)
        self.view.pack(side='left',fill='both',expand=True)
        self.view.on_detail=lambda text:self.info.set(text)
        self.view.set_image(overview);self.view.set_overlay(self.draw)
        self.protocol('WM_DELETE_WINDOW',self.close)
        # The header close button follows the same unsaved-edit guard.
        for child in self.winfo_children()[0].winfo_children():
            if isinstance(child,ttk.Button):child.configure(command=self.close)
        _async(self,lambda:self.api.get('/date-regions'),self.loaded,self.failed)
        self.reload_display()

    def reset_brightness(self):
        self.brightness.set(1);self.view.set_brightness(1)

    def reload_display(self):
        mode='tarp' if self.display_mode.get().startswith('아스팔트') else 'normal'
        self.view.display=mode;self.view.detail=None;self.view.detail_key=None;self.view.request_key=None
        self.info.set('원본 표시 범위 다시 읽는 중')
        _async(self,lambda:self.api.image('/mosaic.png',{'display':mode}),
               lambda image:self.view.set_image(image,fit=False) if self.view.display==mode else None,self.failed)

    def loaded(self,value):
        self.regions=value.get('regions',{});self.ready=True;self.refresh()
        self.info.set('타프 이름을 고르고 네 모서리를 지정하세요. 기존 위치는 자동 확정하지 않습니다.')
        if self.regions:self.focus_tarp()

    def failed(self,error):
        self.info.set(error);messagebox.showwarning('타프 영역',error,parent=self)

    def choose(self):
        self.mode.set('draw');self.view.render()
        self.info.set(self.active.get()+' · 네 모서리를 순서대로 클릭하세요.')

    def clicked(self,x,y):
        if not self.ready or self.mode.get()!='draw':return
        points=self.regions.setdefault(self.active.get(),[])
        if len(points)==4:
            self.info.set('꼭짓점을 드래그하거나 «선택 타프 다시 그리기»를 누르세요.');return
        w,h=self.view.size;points.append([x/w,y/h]);self.dirty=True;self.refresh()
        self.info.set(f'{self.active.get()} · {len(points)}/4 모서리 지정')

    def hit(self,x,y,scale):
        if self.mode.get()!='draw':return None
        w,h=self.view.size
        for i,(u,v) in enumerate(self.regions.get(self.active.get(),[])):
            if ((x-u*w)**2+(y-v*h)**2)**.5*scale<=12:return i
        return None

    def move_vertex(self,index,x,y):
        w,h=self.view.size
        self.regions[self.active.get()][index]=[min(1,max(0,x/w)),min(1,max(0,y/h))]
        self.dirty=True;self.refresh()

    def refresh(self):
        for name,label in self.labels.items():
            count=len(self.regions.get(name,[]));label.configure(text='4점 지정' if count==4 else f'{count}/4 점')
        complete=len(self.regions)>=2 and all(len(p)==4 for p in self.regions.values())
        valid=self.regions and all(len(p)==4 for p in self.regions.values())
        self.save_button.configure(state='normal' if self.ready and valid else 'disabled')
        self.search_button.configure(state='normal' if self.ready and complete else 'disabled')
        self.guide.show(self.search_button)
        self.view.render()

    def draw(self,view):
        w,h=view.size
        for i,(name,points) in enumerate(self.regions.items()):
            if not points:continue
            color=('#75d4bc','#ffd36a','#c3a2ff')[self.context['tarps'].index(name)%3]
            xy=[view.image_to_canvas(u*w,v*h) for u,v in points]
            flat=[v for p in xy for v in p]
            if len(xy)>1:
                view.canvas.create_line(*(flat+list(xy[0]) if len(xy)==4 else flat),fill=color,width=3 if name==self.active.get() else 2)
            for x,y in xy:view.canvas.create_oval(x-5,y-5,x+5,y+5,fill=color,outline='#111')
            view.canvas.create_text(xy[0][0]+8,xy[0][1]-12,text=name,fill=color,anchor='w',font=('Segoe UI',12,'bold'))

    def focus_tarp(self):
        p=self.regions.get(self.active.get(),[])
        if len(p)==4:
            w,h=self.view.size
            # Fit around this tarp without the overview's old 3x zoom ceiling.
            xs=[u*w for u,v in p];ys=[v*h for u,v in p]
            cw,ch=self.view.canvas.winfo_width(),self.view.canvas.winfo_height()
            self.view.scale=min(cw/max(1,max(xs)-min(xs)),ch/max(1,max(ys)-min(ys)))*.5
            self.view.offset_x=cw/2-(min(xs)+max(xs))/2*self.view.scale
            self.view.offset_y=ch/2-(min(ys)+max(ys))/2*self.view.scale
            self.view.render()

    def undo(self):
        p=self.regions.get(self.active.get(),[])
        if p:p.pop();self.dirty=True;self.refresh()

    def clear(self):
        self.regions.pop(self.active.get(),None);self.mode.set('draw');self.dirty=True;self.refresh()

    def save(self,search=False,close=False):
        self.save_button.configure(state='disabled');self.search_button.configure(state='disabled')
        payload=json.loads(json.dumps(self.regions))
        self.info.set('영역 저장 중')
        def done(result):
            self.dirty=self.regions!=payload
            self.info.set('날짜별 영역 저장 완료 · 원영상 ROI 승인은 별도입니다.')
            self.refresh();self.saved(result,search)
            if search or close:self.destroy()
        _async(self,lambda:self.api.post('/date-regions/save',{'regions':payload}),done,
               lambda e:(self.refresh(),self.failed(e)))

    def close(self):
        if self.dirty:
            choice=messagebox.askyesnocancel('영역 저장','변경한 타프 영역을 저장하고 닫을까요?',parent=self)
            if choice is None:return
            if choice:
                if not self.regions or any(len(p)!=4 for p in self.regions.values()):
                    self.failed('진행 중인 타프의 네 모서리를 완성한 뒤 저장하세요.');return
                self.save(close=True);return
        self.destroy()
