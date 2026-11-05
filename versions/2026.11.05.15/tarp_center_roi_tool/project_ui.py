"""ROI editor mode owned by an existing Studio project and stage-02 selection."""
import copy
import json
from pathlib import Path
import sys
import tkinter as tk
from tkinter import messagebox, ttk

from app import App

ENGINE = Path(__file__).resolve().parent.parent / "UAV_Preprocessing_Studio_20260924" / "engine"
sys.path.insert(0, str(ENGINE))
from uav_pipeline.roi_studio_bridge import Session


def start_project(root, config_path, geometry=None):
    """Only a compact progress view while existing automation runs off the UI thread."""
    import queue
    import threading
    from uav_pipeline.roi_auto_prepare import prepare
    from uav_pipeline.progress_ui import begin,update,finish
    root.title('UAV Studio · 03 자동 ROI 준비')
    root.geometry(geometry or '1100x900')
    root.withdraw()
    begin(root,'ROI · Trim · 그림자 제외 · 포화 검사',anchor=geometry)
    events=queue.Queue()
    def work():
        try:events.put(('done',prepare(config_path,lambda text:events.put(('progress',text)))))
        except Exception as exc:events.put(('error',str(exc)))
    def poll():
        try:
            while True:
                kind,value=events.get_nowait()
                if kind=='progress':update(root,value)
                elif kind=='done':
                    finish(root)
                    ProjectApp(root,config_path,geometry=geometry,session=value)
                    return
                else:
                    finish(root);root.deiconify()
                    frame=ttk.Frame(root,padding=30);frame.pack(fill='both',expand=True)
                    ttk.Label(frame,text='자동 준비를 완료하지 못했습니다: '+value,wraplength=900).pack(anchor='w')
                    ttk.Button(frame,text='다시 시도',command=lambda:(frame.destroy(),start_project(root,config_path,geometry))).pack(anchor='w')
                    return
        except queue.Empty:pass
        root.after(100,poll)
    threading.Thread(target=work,daemon=True).start();root.after(100,poll)


class ProjectApp(App):
    def __init__(self, root, config_path, results_first=True, geometry=None, session=None):
        self.results_geometry = geometry
        self.session = session or Session(config_path)
        self.shot = tk.StringVar(root, value=self.session.reference)
        self.reference = tk.StringVar(root, value=self.session.reference)
        self.loaded_shot = None
        self.loading_shot = None
        self.review_window = None
        self.select_after_load = None
        self.proposal_rejections = {}
        super().__init__(root)
        self.root.title(f"UAV Studio · 03 ROI · {self.session.context['label']}")
        if results_first:
            self.root.withdraw()
            self.root.after(100, self.show_review_queue)
        else:
            self.root.after(100, self.open)

    def open(self, path=None):
        if self.busy or not self.discard_pending():
            if self.loaded_shot or self.loading_shot:
                self.shot.set(self.loaded_shot or self.loading_shot)
            return
        self.dirty = False
        self.loading_shot = self.shot.get()
        super().open(self.session.files[self.shot.get() + "_6"])

    def before_image_read(self):
        # Preview rechecks selected sources/configuration, without rescanning unrelated raw files.
        # Save, approval and publish still use the default full registry check.
        self.session.assert_current(scan_registry=False)

    def image_loaded(self):
        self.loaded_shot = self.loading_shot
        self.shot.set(self.loaded_shot)
        self.records = copy.deepcopy(self.session.images.get(self.loaded_shot, []))
        self.current_strokes = []
        self.pixel_reviewed.set(False)
        self.role.set("calibration" if self.loaded_shot == self.reference.get() else "validation")
        self.refresh_list()
        self.redraw()
        self.note(f"02 선택 영상 {self.loaded_shot} · native Band6 · ROI {len(self.records)}개 자동 복원")
        if self.records:
            import numpy as np
            points=np.concatenate([r['outer'] for r in self.records])
            extent=np.maximum(30,np.ptp(points,axis=0))*1.8
            self.scale=min(self.canvas.winfo_width()/extent[0],self.canvas.winfo_height()/extent[1],20)
            self.ox,self.oy=np.array([self.canvas.winfo_width(),self.canvas.winfo_height()])/2-points.mean(0)*self.scale
            self.redraw()
            self.note('왼쪽 「전체 확인」 → 틀린 영상만 「아니다」 표시 → 「일괄 승인」. ROI를 다시 그릴 필요 없습니다.')
        if self.select_after_load:
            name, self.select_after_load = self.select_after_load, None
            for index, record in enumerate(self.records):
                if record["tarp_id"] == name:
                    self.listbox.selection_set(index)
                    self.select_record()
                    self.focus_roi()
                    break

    def persist(self):
        self.session.images[self.loaded_shot] = copy.deepcopy(self.records)
        self.session.reference = self.reference.get()
        self.session.last_manifest = None
        self.dirty = False
        self.session.save_draft()
        self.note(f"{self.loaded_shot} ROI {len(self.records)}개 프로젝트 자동 저장 · 전달 대기")
        self.refresh_review_queue()

    def track_candidates(self):
        if self.busy or not self.discard_pending():
            return
        if not messagebox.askyesno("후보 전체 추적", "기준 영상의 확정 ROI로 다른 선정 영상을 추적합니다.\n"
                                  "기존 확정 ROI는 유지하고 새 후보는 미승인으로 저장합니다. 계속할까요?", parent=self.root):
            return
        def done(results):
            self.session.accept_tracking(results)
            self.current = self.edit_index = None
            self.pending_changed = False
            self.records = copy.deepcopy(self.session.images.get(self.loaded_shot, []))
            self.refresh_list()
            self.redraw()
            self.note("추적 완료 · 검토 목록에서 경계·내부 화소를 확인하세요.")
            self.show_review_queue()
        self.job("기준 ROI → 후보 전체 추적", lambda: self.session.propose_tracking(self.progress_notes.put), done)

    def show_review_queue(self):
        if self.review_window and self.review_window.winfo_exists():
            self.review_window.lift()
            self.refresh_review_queue()
            return
        window = self.review_window = tk.Toplevel(self.root)
        window.title(f"UAV Studio · ROI 자동 결과 · {self.session.context['label']}")
        window.geometry(self.results_geometry or f"1100x900+{self.root.winfo_rootx()+20}+{self.root.winfo_rooty()+20}")
        window.rowconfigure(1, weight=1)
        window.columnconfigure(0, weight=1)
        self.review_tree = ttk.Treeview(window, columns=("shot", "tarp", "state", "reason"), show="headings", selectmode="browse",height=4)
        for key, text, width in (("shot", "영상", 110), ("tarp", "타프", 100), ("state", "상태", 100), ("reason", "확인 사항", 400)):
            self.review_tree.heading(key, text=text)
            self.review_tree.column(key, width=width)
        header = ttk.Frame(window, padding=10)
        header.grid(row=0, column=0, columnspan=2, sticky='ew')
        self.result_summary = tk.StringVar()
        ttk.Label(header, textvariable=self.result_summary, font=('Malgun Gothic', 15, 'bold')).pack(anchor='w')
        ttk.Label(header, text='타프 이름·경계·빨간 제외 화소를 확인하세요. ← → 키로 이동할 수 있습니다.').pack(anchor='w', pady=(5, 0))
        nav=ttk.Frame(header);nav.pack(fill='x',pady=(10,0))
        ttk.Button(nav,text='◀ 이전',command=lambda:self.show_result(self.result_index-1)).pack(side='left')
        self.result_choice=tk.StringVar()
        chooser=ttk.Combobox(nav,textvariable=self.result_choice,values=self.session.shots,state='readonly',width=16)
        chooser.pack(side='left',padx=10);chooser.bind('<<ComboboxSelected>>',lambda e:self.show_result(self.session.shots.index(self.result_choice.get())))
        ttk.Button(nav,text='다음 ▶',command=lambda:self.show_result(self.result_index+1)).pack(side='left')
        self.result_position=tk.StringVar();ttk.Label(nav,textvariable=self.result_position).pack(side='left',padx=12)
        window.bind('<Left>',lambda e:self.show_result(self.result_index-1))
        window.bind('<Right>',lambda e:self.show_result(self.result_index+1))
        scroll = ttk.Scrollbar(window, command=self.review_tree.yview)
        self.review_tree.configure(yscrollcommand=scroll.set)
        from PIL import Image,ImageDraw,ImageTk,ImageFont
        from core import Raster,stretch
        import numpy as np
        holder=ttk.Frame(window);holder.grid(row=1,column=0,columnspan=2,sticky='nsew')
        cards=ttk.Frame(holder);cards.pack(fill='both',expand=True)
        cards.columnconfigure(0,weight=1);cards.rowconfigure(0,weight=1)
        self.result_cards=[];self.result_index=0
        self.proposal_photos=[];self.proposal_rejections={}
        self.proposal_snapshot={}
        for i,shot in enumerate(self.session.shots):
            card=ttk.Frame(cards,padding=8);card.grid(row=0,column=0,sticky='nsew');card.grid_remove();self.result_cards.append(card)
            records=self.session.images.get(shot,[])
            entry=next((e for e in getattr(self.session,'auto_result',{}).get('entries',[]) if e['shot']==shot),{})
            if not records:
                ttk.Label(card,text='자동 결과 보류',font=('Malgun Gothic',20,'bold')).pack(pady=70)
                ttk.Label(card,text=entry.get('error',self.session.import_holds.get(shot,'타프 경계를 찾지 못했습니다.')),wraplength=900).pack()
                ttk.Label(card,text='아니다(제외)를 누르면 정상 영상만 남겨 다음으로 진행합니다.').pack(pady=15)
                reject=tk.BooleanVar(value=False);self.proposal_rejections[shot]=reject
                self.proposal_snapshot[shot]=json.dumps(records,sort_keys=True)
                ttk.Button(card,text='아니다(이 영상 제외) · 다음',command=lambda s=shot:self.decide_result(s,True)).pack(pady=15)
                ttk.Checkbutton(card,text='다음 단계에서 제외',variable=reject).pack()
                reject.trace_add('write',lambda *_:self.refresh_review_queue())
                continue
            raster=Raster(self.session.files[shot+'_6']);points=np.concatenate([r['outer'] for r in records])
            x0,y0=np.maximum(0,np.floor(points.min(0)-12)).astype(int);x1,y1=np.minimum([raster.width,raster.height],np.ceil(points.max(0)+12)).astype(int)
            data,valid=raster.read(1,(int(x0),int(y0),int(x1-x0),int(y1-y0)))
            preview_label='기준 원영상'
            # Use a less clipped native band for pixel review, keeping all outlines in reference coordinates.
            if records[0].get('proposal_source'):
                try:
                    import cv2
                    batch=json.loads(Path(records[0]['proposal_source']).read_text(encoding='utf-8'))
                    entry=next(e for e in batch['entries'] if e['shot']==shot)
                    qc=json.loads(Path(entry['result_path']).read_text(encoding='utf-8'))
                    rows=[r for r in qc['rows'] if not r.get('error')]
                    row=max(rows,key=lambda r:(r['usable_tarps'],r['wavelength_nm']==740))
                    name=next(iter(qc['regions']));target=np.float32(qc['regions'][name])*[raster.width,raster.height]-[x0,y0]
                    transform=cv2.getPerspectiveTransform(np.float32(row['tarps'][name]['native_points']),target.astype('float32'))
                    display=Raster(entry['source_files'][row['native_suffix']-1]);raw,raw_valid=display.read(1)
                    data=cv2.warpPerspective(raw,transform,(int(x1-x0),int(y1-y0)),flags=cv2.INTER_NEAREST)
                    valid=cv2.warpPerspective(raw_valid.astype('uint8'),transform,(int(x1-x0),int(y1-y0)),flags=cv2.INTER_NEAREST)>0
                    preview_label=f"{row['wavelength_nm']:g} nm 미리보기";display.ds=None
                except (OSError,ValueError,KeyError,StopIteration):pass
            image=Image.fromarray(stretch(data,valid)).convert('RGB');draw=ImageDraw.Draw(image)
            for record in records:
                for key,color in [('outer','#ffb02e'),('inner','#00d8ed')]:
                    p=np.asarray(record[key])-[x0,y0];draw.line([tuple(v) for v in np.vstack([p,p[0]])],fill=color,width=1)
                for px,py,r,remove in record.get('exclusion_strokes',[]):
                    if remove:draw.point((round(px-.5)-x0,round(py-.5)-y0),fill='#ff667c')
            scale=min(900/image.width,330/image.height);image=image.resize((round(image.width*scale),round(image.height*scale)),Image.Resampling.NEAREST)
            draw=ImageDraw.Draw(image)
            for record in records:
                p=(np.asarray(record['outer']).mean(0)-[x0,y0])*scale
                draw.text(tuple(p),record['tarp_id'],font=ImageFont.truetype('malgun.ttf',14),fill='#fff04c',stroke_width=1,stroke_fill='#111111')
            photo=ImageTk.PhotoImage(image);self.proposal_photos.append(photo);ttk.Label(card,image=photo).pack()
            ttk.Label(card,text=preview_label+f" · Trim {records[0].get('margin_percent_shortest_altitude_per_edge', 0):g}%").pack()
            bands=entry.get('bands',[])
            usable=[b['wavelength_nm'] for b in bands if not b.get('error') and b['usable_tarps']>=2]
            held=[b['wavelength_nm'] for b in bands if b.get('error') or b['usable_tarps']<2]
            text=('포화 기준 충족 '+str(len(usable))+'/10 밴드 · 보류 '+', '.join(str(v)+' nm' for v in held)) if bands else '포화 검사 결과 없음 · 다음 단계 진행 전 확인 필요'
            ttk.Label(card,text=text,wraplength=950).pack(pady=3)
            warnings=[r['tarp_id'] for r in records if r.get('pixel_proposal',{}).get('status')=='review_required']
            if warnings:ttk.Label(card,text='그림자 제외 확인 필요: '+', '.join(warnings)).pack()
            if entry.get('names_require_review'):ttk.Label(card,text='타프 이름은 자동 추정입니다. 영상의 이름 표시를 확인하세요.').pack()
            if entry.get('tracking',{}).get('method')=='two_approved_reference_consensus':
                ttk.Label(card,text='두 승인 영상에서 연결한 경계입니다. 외곽선이 타프와 맞는지 확인하세요.').pack()
            approved=all(r.get('reviewed') and r.get('pixel_reviewed') for r in records)
            ttk.Label(card,text='확인 완료 · 다시 승인할 필요 없음' if approved else '결과 확인 대기').pack()
            reject=tk.BooleanVar(value=any(r.get('user_review')=='rejected_in_results' for r in records));self.proposal_rejections[shot]=reject
            self.proposal_snapshot[shot]=json.dumps(records,sort_keys=True)
            buttons=ttk.Frame(card);buttons.pack(pady=6)
            ttk.Button(buttons,text='맞다 · 다음 영상',command=lambda s=shot:self.decide_result(s,False)).pack(side='left',padx=8)
            ttk.Button(buttons,text='아니다(제외) · 다음 영상',command=lambda s=shot:self.decide_result(s,True)).pack(side='left',padx=8)
            ttk.Checkbutton(card,text='다음 단계에서 제외',variable=reject).pack()
            reject.trace_add('write', lambda *_: self.refresh_review_queue())
            raster.ds=None
        footer=ttk.Frame(window,padding=8);footer.grid(row=2,column=0,columnspan=2,sticky='ew')
        ttk.Label(footer,text='노랑: 타프 외곽 · 하늘색: Trim 후 사용 영역 · 빨강: 자동 제외 화소 후보. 잘못된 영상만 표시하세요.').pack(anchor='w')
        self.result_action = ttk.Button(footer,text='결과가 맞다 · 확인하고 다음으로 진행',command=self.accept_proposals)
        self.result_action.pack(fill='x',pady=5)
        from uav_pipeline.button_guide import ButtonGuide
        self.result_guide = ButtonGuide(window)
        ttk.Label(footer,textvariable=self.status,wraplength=1300).pack(anchor='w')
        ttk.Button(footer, text="현재 영상 상세 보기", command=self.open_review_item).pack(anchor='e')
        window.protocol('WM_DELETE_WINDOW', self.close_results)
        self.review_tree.bind("<Double-1>", lambda e: self.open_review_item())
        self.refresh_review_queue()
        self.show_result(0)

    def show_result(self,index):
        if self.busy or not self.result_cards:return
        self.result_cards[self.result_index].grid_remove()
        self.result_index=max(0,min(len(self.result_cards)-1,index))
        self.result_cards[self.result_index].grid()
        self.result_choice.set(self.session.shots[self.result_index])
        self.result_position.set(f'{self.result_index+1} / {len(self.result_cards)}')

    def decide_result(self,shot,rejected):
        self.proposal_rejections[shot].set(rejected)
        self.show_result(self.result_index+1)

    def accept_proposals(self):
        if self.busy:
            return
        self.session.assert_current()
        auto=getattr(self.session,'auto_result',None)
        if isinstance(auto,dict):
            blocked=[e['shot'] for e in auto.get('entries',[]) if e['shot'] in self.session.shots
                     and not (e['shot'] in self.proposal_rejections and self.proposal_rejections[e['shot']].get())
                     and (e.get('error') or not any(not b.get('error') and b['usable_tarps']>=2 for b in e.get('bands',[])))]
            if blocked:
                messagebox.showinfo('보류 영상',', '.join(blocked)+'\n자동 검사를 통과하지 못했습니다. 상세 확인하거나 아니다(제외)로 표시하세요.',parent=self.review_window)
                return
        for shot,rejected in self.proposal_rejections.items():
            if not rejected.get() and json.dumps(self.session.images.get(shot,[]),sort_keys=True)!=self.proposal_snapshot[shot]:
                messagebox.showwarning('ROI 변경','미리보기를 연 뒤 ROI가 변경됐습니다. 검토 창을 다시 열어 확인하세요.',parent=self.review_window);return
        accepted=[]
        for shot,rejected in self.proposal_rejections.items():
            if rejected.get():
                # A new explicit rejection must revoke an older approval too.
                for record in self.session.images.get(shot,[]):
                    record['reviewed']=False;record['pixel_reviewed']=False
                    record['user_review']='rejected_in_results'
                continue
            for record in self.session.images.get(shot,[]):
                record['reviewed']=True;record['pixel_reviewed']=True
                record['user_review']='explicit_bulk_boundary_and_pixel_review'
            accepted.append(shot)
        self.session.save_draft();self.records=copy.deepcopy(self.session.images.get(self.loaded_shot,[]))
        self.proposal_snapshot={s:json.dumps(self.session.images.get(s,[]),sort_keys=True) for s in self.proposal_rejections}
        excluded={s for s,v in self.proposal_rejections.items() if v.get()}
        if excluded:
            self.session.exclude_rejected(excluded)
            # The confirmed subset has its own selection identity; the old selection/draft remain in history.
            self.review_window.destroy();self.review_window=None;self.show_review_queue()
            self.save(next_step=4)
            return
        self.refresh_list();self.redraw();self.refresh_review_queue()
        pending=[s for s in self.session.shots if not self.session.images.get(s) or any(not r.get('reviewed') or not r.get('pixel_reviewed') for r in self.session.images[s])]
        if pending:
            self.note(f'{len(accepted)}장 승인 저장 · {len(pending)}장 보류. 보류 영상은 수정하거나 02에서 제외한 뒤 진행하세요.')
            messagebox.showinfo('ROI 확인 저장',f'{len(accepted)}장 확인을 저장했습니다.\n보류 영상: '+', '.join(pending)+'\n이 영상들은 자동 승인하지 않았습니다.',parent=self.review_window)
        else:
            self.save(next_step=4)

    def close_results(self):
        if self.busy:
            return
        self.review_window.destroy()
        if self.root.state() == 'withdrawn':
            self.root.destroy()

    def refresh_review_queue(self):
        if not self.review_window or not self.review_window.winfo_exists():
            return
        self.review_tree.delete(*self.review_tree.get_children())
        missing=[s for s in self.session.shots if not self.session.images.get(s)]
        approved=[s for s in self.session.shots if self.session.images.get(s) and all(r.get('reviewed') and r.get('pixel_reviewed') for r in self.session.images[s])]
        count=sum(len(self.session.images.get(s,[])) for s in self.session.shots)
        self.result_summary.set(f'선택 {len(self.session.shots)}장 · ROI {count}개 · 확인 완료 {len(approved)}장 · 자동 경계 없음 {len(missing)}장')
        if any(r.get('trim_change') for records in self.session.images.values() for r in records):
            boundaries=sum(bool(self.session.images.get(s)) and all(r.get('reviewed') for r in self.session.images[s]) for s in self.session.shots)
            self.result_summary.set(f'선택 {len(self.session.shots)}장 · ROI {count}개 · 외곽선 확인 {boundaries}장 · 내부 확인 대기 {len(self.session.shots)-len(approved)}장')
        if missing:
            self.note('보류: '+', '.join(missing)+' — 해당 영상에서 아니다(제외)를 누르거나 상세 보기로 확인하세요.')
            self.result_action.configure(text='정상 결과 확인 저장 · 보류 영상은 유지')
        else:
            self.result_action.configure(text='확인한 결과로 다음 단계 진행' if len(approved)==len(self.session.shots) else '결과가 맞다 · 확인하고 다음으로 진행')
        already_saved = missing and len(approved)+len(missing)==len(self.session.shots) and not any(v.get() for v in self.proposal_rejections.values())
        self.result_action.configure(state='disabled' if already_saved else 'normal')
        if already_saved:
            self.result_action.configure(text='정상 결과는 이미 확인·저장됨 · 보류 영상 처리 필요')
        excluded=sum(v.get() for v in self.proposal_rejections.values())
        if excluded:self.result_action.configure(text=f'아니다 {excluded}장 제외 · 나머지 결과 확인하고 다음',state='normal')
        self.result_guide.show(self.result_action)
        expected = self.session.tracking.get("expected_tarps", [r["tarp_id"] for r in self.session.images.get(self.reference.get(), [])])
        for shot in self.session.shots:
            records = {r["tarp_id"]: r for r in self.session.images.get(shot, [])}
            for name in dict.fromkeys([*expected, *records]):
                record = records.get(name)
                diagnostics = self.session.tracking.get("results", {}).get(shot, {}).get(name, {})
                if record and record.get("reviewed") and record.get("pixel_reviewed"):
                    continue
                state = "직접 지정 필요" if record is None else "확인 대기"
                self.review_tree.insert("", "end", values=(shot, name, state, diagnostics.get("reason", "경계·내부 화소 확인")))

    def open_review_item(self):
        if self.busy:
            return
        selected=self.review_tree.selection()
        if selected:
            shot, name, *_ = self.review_tree.item(selected[0], "values")
        else:
            shot, name = self.session.shots[self.result_index], None
        self.root.geometry(f'+{self.review_window.winfo_x()}+{self.review_window.winfo_y()}')
        self.root.deiconify()
        self.root.lift()
        self.shot.set(shot)
        self.select_after_load = name
        self.open()

    def change_reference(self):
        if self.busy:
            self.reference.set(self.session.reference)
            return
        self.session.reference = self.reference.get()
        self.session.save_draft()
        self.session.last_manifest = None
        self.session.tracking = {}
        self.session.save_draft()
        self.note(f"ELC·Ed 기준: {self.reference.get()} · ROI 용도를 확인한 뒤 다시 전달하세요.")

    def confirm(self):
        previous = self.current
        super().confirm()
        if previous is not None and self.current is None:
            self.persist()

    def delete(self):
        before = len(self.records)
        super().delete()
        if len(self.records) != before:
            self.persist()

    def save(self, next_step=None):
        if self.busy:
            return
        if self.current is not None and self.edit_index is not None and not self.pending_changed:
            self.cancel_edit()
        if self.current is not None:
            messagebox.showinfo("ROI 확정", "현재 수정 중인 ROI를 먼저 확정하거나 수정 취소하세요.")
            return
        self.session.save_draft()
        self.publish_next_step=next_step
        def done(path):
            self.last_output = Path(path).parent
            self.dirty = False
            self.note("승인 ROI 전달 완료 · 04·05에서 같은 기준·native ROI 사용")
            if next_step in (3, 4):
                self.session.navigate(next_step)
                self.root.destroy()
        self.job("10밴드 native 정합 · 승인 ROI 전달 중",
                 lambda: self.session.publish(self.progress_notes.put), done)

    def handle_job_error(self,error):
        import re
        match=re.search(r'^(IMG_\d+) band (\d+): 정합 후보 모두 실패',error)
        if not match:return False
        from uav_pipeline.progress_ui import center_window,ProgressPopup
        shot,band=match.groups()
        parent=self.review_window if self.review_window is not None and self.review_window.winfo_exists() else self.root
        window=tk.Toplevel(parent);window.title('UAV Studio · 정합 실패')
        window.configure(bg='#14171b');window.resizable(False,False)
        window.transient(parent)
        blocked=self.session.registration_exclusion_reason(shot)
        text=f'{shot} · 원본 밴드 {band} 정합 실패'
        tk.Label(window,text=text,font=('Malgun Gothic',13,'bold'),bg='#14171b',fg='#ffc66d').pack(anchor='w',padx=18,pady=(16,10))
        detail=('타프 위치와 ROI 확인 결과는 저장되어 있습니다.\n'
                '이 영상 전체를 대상에서 제외하고 남은 영상으로 다시 진행할 수 있습니다.')
        tk.Label(window,text=detail,wraplength=484,justify='left',font=('Malgun Gothic',10),bg='#14171b',fg='#edf2f7').pack(anchor='w',padx=18)
        status=tk.StringVar(value=blocked or '제외 사유와 이전 선택 목록을 기록합니다.')
        tk.Label(window,textvariable=status,wraplength=484,justify='left',font=('Malgun Gothic',10),bg='#14171b',fg='#b5bec8').pack(anchor='w',padx=18,pady=12)
        row=tk.Frame(window,bg='#14171b');row.pack(side='bottom',fill='x',padx=18,pady=16)
        def exclude():
            try:
                self.session.exclude_rejected({shot},reason=error)
            except Exception as exc:
                status.set(str(exc));return
            window.destroy()
            self.records=copy.deepcopy(self.session.images.get(self.loaded_shot,[]))
            if self.review_window is not None and self.review_window.winfo_exists():self.review_window.destroy()
            self.review_window=None;self.show_review_queue()
            self.save(next_step=getattr(self,'publish_next_step',3))
        tk.Button(row,text='해당 영상 제외하고 계속',command=exclude,state='disabled' if blocked else 'normal',
                  bg='#75d5b6',fg='#10171a',activebackground='#95e5cd',relief='flat',padx=12,pady=7).pack(side='left')
        tk.Button(row,text='돌아가기',command=window.destroy,bg='#2b3239',fg='#edf2f7',relief='flat',padx=12,pady=7).pack(side='right')
        center_window(window,parent,520,260);window.update_idletasks()
        ProgressPopup.dark_titlebar(window)
        window.grab_set();window.lift()
        return True

    def restore(self):
        self.session = Session(self.session.config_path)
        self.open()

    def navigate(self, step):
        if self.busy:
            return
        if self.pending_changed:
            messagebox.showinfo("ROI 수정 중", "현재 수정한 ROI를 확정하거나 수정 취소한 뒤 이동하세요.")
            return
        if not self.session.last_manifest:
            self.save(next_step=step)
            return
        try:
            self.session.navigate(step)
            self.root.destroy()
        except Exception as exc:
            messagebox.showwarning("단계 연결", str(exc))
