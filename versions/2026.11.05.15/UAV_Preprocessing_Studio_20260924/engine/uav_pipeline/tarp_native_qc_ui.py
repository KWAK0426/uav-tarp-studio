"""Fast, explicitly unapproved native tarp saturation review."""
import base64
import io
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image
from .tarp_popup import _PopupBase, ImageViewport, _async, ACCENT


class NativeTarpQcPopup(_PopupBase):
    def __init__(self,parent,base_url,item,shot,names,seed=None,on_review=None,chosen_ids=None):
        super().__init__(parent,base_url,'타프별 10밴드 포화 사전검사')
        self.item=item;self.names=names;self.regions={};self.result=None;self.original=None
        self.previewing=False;self.busy=False;self.active=tk.StringVar(value=names[0])
        self.proposals=[];self.proposal_record=None;self.reviewed=tk.BooleanVar(value=True)
        self.seed=seed;self.on_review=on_review
        self.chosen_ids=None if chosen_ids is None else list(chosen_ids)
        self._header(shot+' · 타프별 10밴드 포화',
            '원본 파일 6번 영상에서 두 타프 이상 지정 · 빨강: DN ≥ 65520 · 정식 ROI 승인과 별개')
        body=ttk.Frame(self,padding=12);body.pack(fill='both',expand=True)
        side=ttk.Frame(body,width=390);side.pack(side='right',fill='y',padx=(12,0));side.pack_propagate(False)
        for name in names:ttk.Radiobutton(side,text=name,variable=self.active,value=name,command=self.reference).pack(anchor='w',pady=4)
        ttk.Label(side,text='둘레를 따라 네 모서리를 클릭하세요.\n휠 확대 · 드래그 이동 · 꼭짓점 드래그 수정',wraplength=370).pack(fill='x',pady=10)
        ttk.Button(side,text='모자이크 위치에서 경계 자동 제안',command=self.propose).pack(fill='x',pady=4)
        ttk.Checkbutton(side,text='제안 경계와 타프 이름 확인',variable=self.reviewed,command=self.refresh).pack(anchor='w')
        ttk.Button(side,text='선택 타프 다시 지정',command=self.clear).pack(fill='x',pady=4)
        ttk.Button(side,text='기준 원영상으로 돌아가기',command=self.reference).pack(fill='x',pady=4)
        self.run_button=ttk.Button(side,text='이름·경계 확정 → 자동 검사',style='Primary.TButton',command=self.run,state='disabled')
        self.run_button.pack(fill='x',pady=10)
        ttk.Button(side,text='저장된 일괄 검사 결과 보기',command=self.open_saved_batch).pack(fill='x',pady=4)
        self.status=tk.StringVar(value='원영상 읽는 중')
        ttk.Label(side,textvariable=self.status,wraplength=370,foreground=ACCENT).pack(fill='x',pady=10)
        self.tree=ttk.Treeview(side,columns=('wave',*names,'usable'),show='headings',height=10)
        for col,label in [('wave','nm'),*[(n,n.replace('PNU_','')+' 포화%') for n in names],('usable','유효 수')]:
            self.tree.heading(col,text=label);self.tree.column(col,width=70,anchor='center')
        self.tree.pack(fill='x');self.tree.bind('<<TreeviewSelect>>',self.show_band)
        ttk.Label(side,text='행 클릭: 밴드별 원본과 변환 경계 확인\n포화율 1% 이상인 타프는 제외. 모든 밴드에 타프가 2개 이상 남아야 포화 기준을 통과합니다.\n\n정합 실패는 보류합니다. 통과해도 Ed·실측 반사도·정식 ROI 검토가 필요합니다.',wraplength=370).pack(fill='x',pady=14)
        self.view=ImageViewport(body,click=self.clicked,hit_test=self.hit,edit_move=self.move_vertex)
        self.view.pack(side='left',fill='both',expand=True);self.view.set_overlay(self.draw)
        self.view.canvas.bind('<ButtonPress-3>',lambda e:setattr(self.view,'drag',(e.x,e.y,self.view.offset_x,self.view.offset_y,True)))
        self.view.canvas.bind('<B3-Motion>',self.view._move)
        self.view.canvas.bind('<ButtonRelease-3>',lambda e:setattr(self.view,'drag',None))
        _async(self,lambda:self.api.get('/native-qc',{'id':item,'band':6}),self.loaded,self.failed)

    def loaded(self,value):
        self.original=Image.open(io.BytesIO(base64.b64decode(value['image']))).convert('RGB')
        self.view.set_image(self.original);self.status.set(f"원본 파일 6 · {value['stats']['wavelength_nm']} nm · 타프 두 개 이상 지정하세요.")
        if self.seed:
            self.regions=self.seed;self.reviewed.set(False);self.refresh()
            w,h=self.view.size
            self.view.focus_polygon([(u*w,v*h) for p in self.regions.values() for u,v in p])
            self.status.set('이름·경계가 자동으로 연결됐습니다. 경계를 확인하고 잘못 잡힌 꼭짓점만 수정하세요.')
        else:self.propose()

    def propose(self):
        if self.busy:return
        if self.regions:
            self.status.set('직접 지정한 영역을 보존했습니다. 자동 제안은 빈 작업창에서 시작하세요.');return
        self.busy=True;self.refresh();self.status.set('모자이크 위치를 원영상에 연결하고 타프 경계를 찾는 중…')
        def work():
            state=self.api.get('/state');context=self.api.get('/context')
            row=next(r for r in state['candidates'] if r['id']==self.item)
            request=dict(row=row,state=state,names=self.names,context=str(Path(context['output'])/'context.json'))
            result=subprocess.run([sys.executable,'-B',str(Path(__file__).with_name('tarp_native_proposal.py'))],
                input=json.dumps(request),capture_output=True,text=True,encoding='utf-8',timeout=90,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            data=json.loads(result.stdout)
            if result.returncode or data.get('error'):raise ValueError(data.get('error') or result.stderr[-500:])
            folder=Path(context['output'])/'cache/native_tarp_proposals';folder.mkdir(parents=True,exist_ok=True)
            record=folder/(row['shot']+'_'+uuid4().hex[:8]+'.json')
            record.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
            return data,record
        def done(value):
            self.busy=False
            if self.regions:return  # Never overwrite edits made while a request was pending.
            data,self.proposal_record=value;self.regions=data['regions'];self.proposals=data['candidates']
            self.reviewed.set(False);self.reference();self.refresh()
            w,h=self.view.size
            points=[(u*w,v*h) for polygon in (list(self.regions.values())+self.proposals) for u,v in polygon]
            if points:self.view.focus_polygon(points)
            self.status.set('경계 제안 완료 · 타프 이름을 선택하고 해당 경계 안을 한 번 클릭하세요. 이름·경계 확인 후 검사합니다.' if self.proposals else '모자이크 타프별 영역 연결 완료 · 경계와 이름을 확인한 뒤 검사하세요.')
        _async(self,work,done,self.failed)

    def failed(self,error):
        self.busy=False;self.refresh();self.status.set(error);messagebox.showwarning('포화 사전검사',error,parent=self)

    def reference(self):
        if self.busy:return
        if self.previewing and self.original:self.view.set_image(self.original)
        self.previewing=False;self.view.render()

    def refresh(self):
        complete=sum(len(p)==4 for p in self.regions.values())
        self.run_button.configure(state='normal' if not self.busy and self.reviewed.get() and complete>=2 and all(len(p)==4 for p in self.regions.values()) else 'disabled')
        self.view.render()

    def changed(self):
        self.result=None
        if self.proposal_record:
            self.reviewed.set(False)
            self.proposal_record.with_name(self.proposal_record.stem+'_draft.json').write_text(
                json.dumps(dict(regions=self.regions,roi_approved=False),ensure_ascii=False,indent=2),encoding='utf-8')
        for item in self.tree.get_children():self.tree.delete(item)
        self.status.set(' / '.join(f'{n}: {len(self.regions.get(n,[]))}/4점' for n in self.names));self.refresh()

    def clicked(self,x,y):
        if self.busy or self.previewing:return
        if self.proposals:
            import cv2
            import numpy as np
            w,h=self.view.size
            for polygon in self.proposals:
                if cv2.pointPolygonTest(np.asarray(polygon,'float32'),(x/w,y/h),False)>=0:
                    if any(p==polygon for n,p in self.regions.items() if n!=self.active.get()):
                        self.status.set('이미 다른 타프에 배정한 경계입니다. 이름을 확인하세요.');return
                    self.regions[self.active.get()]=[p[:] for p in polygon];self.changed();return
        p=self.regions.setdefault(self.active.get(),[])
        if len(p)<4:
            w,h=self.view.size;p.append([x/w,y/h]);self.changed()

    def hit(self,x,y,scale):
        if self.busy or self.previewing:return None
        w,h=self.view.size
        for i,(u,v) in enumerate(self.regions.get(self.active.get(),[])):
            if ((x-u*w)**2+(y-v*h)**2)**.5*scale<12:return i
        return None

    def move_vertex(self,i,x,y):
        if self.busy:return
        w,h=self.view.size;self.regions[self.active.get()][i]=[min(1,max(0,x/w)),min(1,max(0,y/h))];self.changed()

    def clear(self):
        if self.busy:return
        self.reference();self.regions.pop(self.active.get(),None);self.changed()

    def draw(self,view):
        if self.previewing:return
        w,h=view.size
        for number,polygon in enumerate(self.proposals,1):
            xy=[view.image_to_canvas(u*w,v*h) for u,v in polygon]
            view.canvas.create_polygon(*[v for p in xy for v in p],fill='',outline='#ffd36a',width=2,dash=(4,3))
            view.canvas.create_text(xy[0][0],xy[0][1]-12,text=str(number),fill='#ffd36a')
        for name,points in self.regions.items():
            xy=[view.image_to_canvas(u*w,v*h) for u,v in points]
            if len(xy)>1:view.canvas.create_line(*[v for p in (xy+[xy[0]] if len(xy)==4 else xy) for v in p],fill=ACCENT,width=2)
            for x,y in xy:view.canvas.create_oval(x-4,y-4,x+4,y+4,fill=ACCENT)
            if xy:view.canvas.create_text(xy[0][0],xy[0][1]-12,text=name,fill=ACCENT)

    def run(self):
        if not self.reviewed.get():return
        if self.proposal_record:
            proposal=json.loads(self.proposal_record.read_text(encoding='utf-8'))
            if any([path,Path(path).stat().st_size,Path(path).stat().st_mtime_ns]!=signature
                   for signature in proposal['source_signature'] for path in [signature[0]]):
                self.reviewed.set(False);self.refresh();self.status.set('입력 영상이 변경됐습니다. 기존 제안을 검사에 사용할 수 없습니다.');return
            record=dict(regions=self.regions,source_proposal=str(self.proposal_record),boundary_reviewed=True,roi_approved=False)
            review=self.proposal_record.with_name(self.proposal_record.stem+'_review_'+uuid4().hex[:6]+'.json')
            review.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
        self.busy=True;self.refresh();self.status.set('10개 원본 밴드 정합 및 전체 타프 영역 포화 계산 중…')
        _async(self,lambda:self.api.post('/native-tarp-qc',{'id':self.item,'regions':self.regions}),self.completed,self.failed)

    def completed(self,result):
        self.busy=False;self.result=result;self.status.set(result['summary']);self.refresh()
        for item in self.tree.get_children():self.tree.delete(item)
        for row in result['rows']:
            cells=[row['wavelength_nm'] or '?']+[f"{row['tarps'][n]['saturated_pct']:.3f}" if n in row['tarps'] else '—' for n in self.names]
            self.tree.insert('', 'end',iid=str(row['native_suffix']),values=(*cells,'보류' if row.get('error') else row['usable_tarps']))
        if self.on_review:self.on_review(result)
        else:self.run_batch()

    def run_batch(self):
        if self.busy or not self.result or not self.reviewed.get():return
        state=self.api.get('/state');context=self.api.get('/context')
        selected=set(state.get('selected_ids',[]) if self.chosen_ids is None else self.chosen_ids)
        rows=[r for r in state['candidates'] if r['id'] in selected]
        if not rows:
            self.status.set('위치 화면에서 검사할 영상을 체크한 뒤 경계 확인 창을 열어주세요. 제외한 영상은 검사하지 않습니다.');return
        reference=next(r for r in state['candidates'] if r['id']==self.item)
        folder=Path(context['output'])/'cache'/('batch_qc_'+uuid4().hex[:12])
        request=dict(reference=reference,candidates=rows,regions=self.regions,source_signature=self.result['source_signature'],names=self.names,context=str(Path(context['output'])/'context.json'),output=str(folder))
        request_path=folder.with_suffix('.request.json')
        request_path.write_text(json.dumps(request,ensure_ascii=False),encoding='utf-8')
        self.busy=True;self.refresh();self.status.set(f'{len(rows)}장 자동 추적·10밴드 검사 중. 경계·이름을 다시 입력할 필요 없습니다.')
        def work():
            process=subprocess.run([sys.executable,'-B',str(Path(__file__).with_name('tarp_batch_qc.py')),str(request_path)],capture_output=True,encoding='utf-8',errors='replace',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if process.returncode:raise ValueError(process.stderr[-800:] or '일괄 검사 실패')
            return folder/'batch.json'
        def done(path):
            self.busy=False;self.refresh();self.status.set('일괄 검사 완료 · 자동 경계와 다음 조치를 확인하세요.')
            self.show_batch(path)
        _async(self,work,done,self.failed)

    def open_saved_batch(self):
        context=self.api.get('/context')
        records=sorted((Path(context['output'])/'cache').glob('batch_qc_*/batch.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        if records:self.show_batch(records[0])
        else:self.status.set('저장된 일괄 검사 결과가 없습니다. 기준 영상 검사 후 자동 추적을 실행하세요.')

    def show_batch(self,path):
        from .tarp_batch_qc_ui import BatchQcReview
        if getattr(self,'batch_window',None) and self.batch_window.winfo_exists():
            self.batch_window.lift();return
        self.batch_window=BatchQcReview(self,self.api.base_url,path,self.names)

    def show_band(self,event=None):
        if not self.result or not self.tree.selection():return
        row=next(r for r in self.result['rows'] if str(r['native_suffix'])==self.tree.selection()[0])
        if row.get('error'):self.status.set(row['error'])
        else:
            from .tarp_batch_qc import action
            self.status.set(action(row))
        if row.get('preview'):
            self.previewing=True;self.view.set_image(Image.open(io.BytesIO(base64.b64decode(row['preview']))))
