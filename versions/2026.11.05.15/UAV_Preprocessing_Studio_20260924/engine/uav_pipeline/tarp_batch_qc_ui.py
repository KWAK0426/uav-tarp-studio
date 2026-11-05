"""Saved batch diagnostics: explicit per-image boundary review, no ELC approvals."""
import json
import base64
import io
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk
from .tarp_popup import _PopupBase, _async
from .tarp_batch_qc import signature


def boundary_preview(entry):
    """Show the actual named native boundary; never invent missing geometry."""
    from osgeo import gdal
    gdal.UseExceptions()
    if entry.get('error'):raise ValueError(entry['error'])
    qc=json.loads(Path(entry['result_path']).read_text(encoding='utf-8'))
    row=next(r for r in qc['rows'] if r['native_suffix']==6)
    if row.get('error'):raise ValueError(row['error'])
    ds=gdal.Open(entry['source_files'][5])
    im=Image.open(io.BytesIO(base64.b64decode(row['preview']))).convert('RGB')
    points=[p for tarp in row['tarps'].values() for p in tarp['native_points']]
    sx,sy=im.width/ds.RasterXSize,im.height/ds.RasterYSize
    box=(max(0,min(p[0] for p in points)*sx-16),max(0,min(p[1] for p in points)*sy-16),
         min(im.width,max(p[0] for p in points)*sx+16),min(im.height,max(p[1] for p in points)*sy+16))
    crop=im.crop(box);scale=min(250/crop.width,170/crop.height)
    return crop.resize((max(1,round(crop.width*scale)),max(1,round(crop.height*scale))),Image.Resampling.NEAREST)


def record_decisions(value, accepted, rejected):
    """Explicit user decisions affect diagnostic boundaries only, never scientific approval."""
    by_id={e['id']:e for e in value['entries']}
    # Check every accepted source before mutating any record.
    for item in accepted:
        entry=by_id[item]
        if entry.get('error') or not entry.get('result_path'):
            raise ValueError(entry['shot']+': 자동 제안 실패 — 맞다고 확인할 수 없습니다.')
        if signature(entry['source_files'])!=entry.get('source_signature'):
            raise ValueError(entry['shot']+': 입력 영상이 변경됐습니다. 다시 검사하세요.')
    for item in accepted | rejected:
        entry=by_id[item]
        entry.update(reviewed=item in accepted,review_scope='native_suffix6_boundary_only',
                     user_boundary_decision='yes' if item in accepted else 'no',
                     boundary_decision_time=time.strftime('%Y-%m-%dT%H:%M:%S'))


class BatchQcReview(_PopupBase):
    def __init__(self,parent,base_url,path,names):
        super().__init__(parent,base_url,'선택 영상 자동 추적·포화 결과')
        self.path=Path(path);self.names=names;self.value=json.loads(self.path.read_text(encoding='utf-8'))
        self.child=None
        self._header('자동 결과 확인','앱이 먼저 찾고 검사합니다. 아래 미리보기에서 틀린 것만 「아니다」로 표시하세요.')
        body=ttk.Frame(self,padding=14);body.pack(fill='both',expand=True)
        ttk.Label(body,text='포화율 1% 이상인 타프는 해당 밴드에서 제외합니다. 타프가 두 개 미만이면 다른 영상을 선택하세요.\n대체 영상도 없으면 해당 밴드 산출을 보류합니다. 통과한 영상은 ROI·그림자 제외·Ed 검토로 진행합니다.',wraplength=1050).pack(fill='x',pady=8)
        self.tree=ttk.Treeview(body,columns=('shot','review','bands','action'),show='headings',height=4)
        for name,label,width in [('shot','영상',120),('review','경계 확인',130),('bands','포화 기준 충족 밴드',280),('action','다음 조치',430)]:
            self.tree.heading(name,text=label);self.tree.column(name,width=width)
        self.tree.pack(fill='x')
        holder=ttk.Frame(body);holder.pack(fill='both',expand=True,pady=8)
        self.canvas=tk.Canvas(holder,highlightthickness=0,background='#17191c')
        scroll=ttk.Scrollbar(holder,orient='vertical',command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set);scroll.pack(side='right',fill='y');self.canvas.pack(side='left',fill='both',expand=True)
        self.cards=ttk.Frame(self.canvas);self.card_window=self.canvas.create_window((0,0),window=self.cards,anchor='nw')
        self.cards.bind('<Configure>',lambda e:self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>',lambda e:self.canvas.itemconfigure(self.card_window,width=e.width))
        self.rejected={};self.preview_images=[];self.available=set()
        self.status=tk.StringVar(value='자동 경계 미리보기를 불러오는 중…')
        ttk.Label(body,textvariable=self.status,wraplength=1050).pack(fill='x',pady=12)
        self.accept_button=ttk.Button(body,text='아니다로 표시한 것 외에는 모두 맞다',command=self.accept_visible,state='disabled')
        self.accept_button.pack(fill='x',pady=4)
        ttk.Button(body,text='선택 영상 자세히 보기 / 경계 수정',command=self.open_entry).pack(fill='x',pady=4)
        self.tree.bind('<Double-1>',lambda e:self.open_entry())
        self.fill()
        _async(self,self.load_previews,self.show_previews,lambda error:self.status.set(str(error)))

    def load_previews(self):
        results=[]
        for entry in self.value['entries']:
            try:results.append((entry,boundary_preview(entry),None))
            except Exception as exc:results.append((entry,None,str(exc)))
        return results

    def show_previews(self,results):
        for child in self.cards.winfo_children():child.destroy()
        self.preview_images=[];self.available=set();self.rejected={}
        for i,(entry,preview,error) in enumerate(results):
            card=ttk.LabelFrame(self.cards,text=entry['shot'],padding=6)
            card.grid(row=i//4,column=i%4,sticky='nsew',padx=4,pady=4)
            self.cards.columnconfigure(i%4,weight=1)
            if preview:
                image=ImageTk.PhotoImage(preview);self.preview_images.append(image)
                ttk.Label(card,image=image).pack()
                reject=tk.BooleanVar(value=entry.get('user_boundary_decision')=='no')
                self.rejected[entry['id']]=reject;self.available.add(entry['id'])
                ttk.Checkbutton(card,text='아니다 — 경계 수정 필요',variable=reject).pack(anchor='w')
            else:ttk.Label(card,text='자동 확인 불가\n'+error,wraplength=220).pack(fill='x')
            ttk.Button(card,text='자세히 / 수정',command=lambda n=i:self.select_entry(n)).pack(fill='x')
        self.accept_button.configure(state='normal' if self.available else 'disabled')
        self.status.set(f'{len(self.available)}장 자동 경계 표시 · {len(results)-len(self.available)}장 보류. 틀린 것만 표시하고 한 번에 확인하세요.')

    def select_entry(self,index):
        self.tree.selection_set(str(index));self.open_entry()

    def accept_visible(self):
        rejected={i for i,v in self.rejected.items() if v.get()}
        try:record_decisions(self.value,self.available-rejected,rejected)
        except (ValueError,OSError) as exc:
            self.status.set(str(exc));return
        self.save();self.fill()
        self.status.set(f'{len(self.available-rejected)}장 경계 확인 저장 · {len(rejected)}장 수정 요청. 포화로 사용할 수 없는 밴드는 계속 제외 후보로 유지됩니다.')

    def save(self):
        temporary=self.path.with_suffix('.tmp');temporary.write_text(json.dumps(self.value,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(self.path)

    def fill(self):
        for iid in self.tree.get_children():self.tree.delete(iid)
        for i,entry in enumerate(self.value['entries']):
            bands=[str(int(b['wavelength_nm'])) for b in entry.get('bands',[]) if b.get('wavelength_nm') and not b.get('error') and b['usable_tarps']>=2]
            note=entry.get('error') or ('일부 밴드: 다른 영상 선택 또는 제외' if len(bands)<10 else '경계 확인 후 ROI·그림자·Ed 검토')
            self.tree.insert('', 'end',iid=str(i),values=(entry['shot'],'확인 완료' if entry.get('reviewed') else '확인 필요',', '.join(bands)+' nm' if bands else '보류',note))

    def open_entry(self):
        if not self.tree.selection():return
        if self.child and self.child.winfo_exists():self.child.lift();return
        entry=self.value['entries'][int(self.tree.selection()[0])]
        if entry.get('source_signature') and signature(entry['source_files'])!=entry['source_signature']:
            messagebox.showwarning('입력 변경','원영상이 변경됐습니다. 일괄 검사를 다시 실행하세요.',parent=self);return
        from .tarp_native_qc_ui import NativeTarpQcPopup
        def reviewed(qc):
            from .tarp_batch_qc import action,report
            record=self.path.parent/(entry['shot']+'_reviewed_qc.json')
            record.write_text(json.dumps(qc,ensure_ascii=False),encoding='utf-8')
            entry.update(regions=qc['regions'],source_signature=qc['source_signature'],result_path=str(record),reviewed=True,summary=qc['summary'],bands=[dict(wavelength_nm=b['wavelength_nm'],usable_tarps=b['usable_tarps'],error=b.get('error'),action=action(b)) for b in qc['rows']])
            entry.pop('error',None)
            entry.pop('user_boundary_decision',None)
            self.save()
            report(self.value,self.path.parent);self.fill();self.status.set(entry['shot']+' 경계 확인·재검사 저장 완료. 정식 ROI·ELC 승인은 별도입니다.')
            _async(self,self.load_previews,self.show_previews,lambda error:self.status.set(str(error)))
        self.child=NativeTarpQcPopup(self,self.api.base_url,entry['id'],entry['shot'],self.names,seed=entry.get('regions'),on_review=reviewed)
