"""Raw-folder entry point for the native ELC app; science stays in the existing engine."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from zoneinfo import ZoneInfo

import distribution
distribution.configure()
from general_studio import GeneralStudio, inspect_raw, destroy_owned_root
from legacy_studio import apply_studio_theme
from uav_pipeline.config import load_config, save_config
from uav_pipeline import assistant as ai
from uav_pipeline import general_project
from uav_pipeline.codex_assistant import find_codex
import studio_update


def inspect_input(raw, source_timezone='UTC'):
    report = inspect_raw(raw)
    result = {'report': report['report'], 'complete': report['complete'],
              'duplicates': bool(report['duplicates']), 'date': '', 'sample_dates': [],
              'source_timezone': source_timezone, 'raw': str(Path(raw).resolve())}
    if not report['complete'] or report['duplicates']:
        return result
    shots = report['complete']
    selected = sorted({shots[0], shots[len(shots)//2], shots[-1]})
    import exiftool
    paths = [str(report['files'][(shot, 1)]) for shot in selected]
    with exiftool.ExifToolHelper() as reader:
        metadata = reader.get_metadata(paths)
    dates = set()
    for meta in metadata:
        value = meta.get('EXIF:DateTimeOriginal') or meta.get('EXIF:CreateDate')
        if not value:
            raise ValueError('원본 EXIF 촬영시각이 없습니다. 파일 수정시각으로 대신하지 않습니다.')
        captured = datetime.strptime(str(value)[:19], '%Y:%m:%d %H:%M:%S')
        captured = captured.replace(tzinfo=ZoneInfo(source_timezone)).astimezone(ZoneInfo('Asia/Seoul'))
        dates.add(captured.date().isoformat())
    result['sample_dates'] = sorted(dates)
    if len(dates) == 1:
        result['date'] = next(iter(dates))
    result['report'] += '\n촬영일 확인: 처음·중간·마지막 촬영의 EXIF 표본 (전체 시간 검사는 처리 전 수행)'
    result['report'] += '\n한국시간 날짜: ' + ', '.join(sorted(dates))
    return result


def create_from_raw(report, work, site, session, date):
    raw = Path(report['raw']).resolve()
    output = Path(work).resolve()
    if raw == output or raw in output.parents or output in raw.parents:
        raise ValueError('원영상과 결과 폴더는 서로 포함되지 않는 별도 위치를 지정하세요.')
    if report['duplicates'] or not report['complete']:
        raise ValueError('중복을 해결하고 완전한 10밴드 촬영을 확인하세요.')
    project = distribution.create_project(str(output), date, session, site)
    cfg = load_config(project)
    cfg['preparation']['raw_directory'] = str(raw)
    cfg['preparation']['source_timezone'] = report['source_timezone']
    cfg['preparation']['shot_list_csv'] = str(general_project.save_shot_list(cfg, report['complete']))
    save_config(project, cfg)
    (project.parent/'raw_inventory.txt').write_text(report['report'], encoding='utf-8')
    return project


class StartWindow:
    def __init__(self, root):
        self.root, self.result, self.report = root, None, None
        self.events = queue.Queue()
        self.busy = False
        apply_studio_theme(root)
        root.title('UAV Studio | 원영상으로 시작')
        root.geometry('960x760')
        root.minsize(780, 680)
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(11, weight=1)
        ttk.Label(frame, text='UAV Tarp Studio', style='Title.TLabel').grid(row=0, column=0, columnspan=3, sticky='w')
        ttk.Label(frame, text='원영상 → 방사·센서·Ed 보정 → 타프 반사도', style='Subtitle.TLabel').grid(row=1, column=0, columnspan=3, sticky='w', pady=(4, 18))
        self.raw = tk.StringVar()
        self.work = tk.StringVar(value=str(Path.home()/'Documents/UAVStudioProjects'))
        self.date = tk.StringVar()
        self.site = tk.StringVar(value='Site')
        self.session = tk.StringVar(value='Session01')
        self.tz = tk.StringVar(value='UTC')
        for row, label, variable in ((2, '원영상 폴더', self.raw), (3, '프로젝트·결과 폴더', self.work)):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', padx=(0, 16), pady=8)
            ttk.Entry(frame, textvariable=variable).grid(row=row, column=1, sticky='ew')
            ttk.Button(frame, text='폴더 선택', command=lambda v=variable: self.browse(v)).grid(row=row, column=2, padx=(8, 0))
        ttk.Label(frame, text='EXIF 원본 시간대').grid(row=4, column=0, sticky='w', pady=8)
        ttk.Combobox(frame, textvariable=self.tz, values=('UTC','Asia/Seoul'), state='readonly', width=16).grid(row=4, column=1, sticky='w')
        ttk.Label(frame, text='MX Dual 원본 기준 UTC. 촬영일은 EXIF에서 읽고 한국시간으로 표시합니다.', style='Subtitle.TLabel').grid(row=5, column=0, columnspan=3, sticky='w', pady=(0, 12))
        for row, label, variable in ((6, '확인된 관측일', self.date), (7, '현장 이름', self.site), (8, '회차 이름', self.session)):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', pady=5)
            ttk.Entry(frame, textvariable=variable, width=26).grid(row=row, column=1, sticky='w')
        bar = ttk.Frame(frame)
        bar.grid(row=9, column=0, columnspan=3, sticky='ew', pady=12)
        self.scan_button = ttk.Button(bar, text='1. 원영상 자동 점검', command=self.scan)
        self.scan_button.pack(side='left')
        self.start_button = ttk.Button(bar, text='2. 점검한 촬영분으로 시작', style='Primary.TButton', command=self.create, state='disabled')
        self.start_button.pack(side='left', padx=8)
        self.status = tk.StringVar(value='한 날짜·한 회차의 원영상 폴더를 선택하세요. 원본은 변경하지 않습니다.')
        ttk.Label(frame, textvariable=self.status, wraplength=850, style='Subtitle.TLabel').grid(row=10, column=0, columnspan=3, sticky='w')
        self.details = tk.Text(frame, bg='#131518', fg='#edf0f2', wrap='word', relief='flat', height=8, font=('Malgun Gothic', 10))
        self.details.grid(row=11, column=0, columnspan=3, sticky='nsew', pady=10)
        self.details.insert('1.0', '실측 Ed·센서 gain·타프 반사도 자료는 다음 화면에서 연결합니다.\n타프 영역·그림자·포화 검토와 계수 승인을 거쳐 반사도를 생성합니다.\n정사화·모자이크는 외부 Metashape에서 진행합니다. BRDF 기능은 포함하지 않습니다.')
        self.details.configure(state='disabled')
        bottom = ttk.Frame(frame)
        bottom.grid(row=12, column=0, columnspan=3, sticky='ew')
        ttk.Button(bottom, text='기존 프로젝트 열기', command=self.open).pack(side='left')
        ttk.Button(bottom, text='인터넷 연결·업데이트 확인', command=lambda: UpdateWindow(root)).pack(side='right')
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.raw.trace_add('write', self.invalidate)
        self.tz.trace_add('write', self.invalidate)
        root.after(150, self.poll)

    def invalidate(self, *_):
        self.report = None
        self.start_button.configure(state='disabled')

    def browse(self, variable):
        if not self.busy:
            path = filedialog.askdirectory(parent=self.root)
            if path: variable.set(path)

    def scan(self):
        if self.busy: return
        raw, tz = self.raw.get().strip(), self.tz.get()
        self.busy = True
        self.scan_button.configure(state='disabled')
        self.start_button.configure(state='disabled')
        self.status.set('촬영 목록·누락·중복과 EXIF 표본을 읽고 있습니다…')
        def worker():
            try: self.events.put((raw, tz, inspect_input(raw, tz), None))
            except Exception as exc: self.events.put((raw, tz, None, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            raw, tz, report, error = self.events.get_nowait()
            self.busy = False
            self.scan_button.configure(state='normal')
            if raw == self.raw.get().strip() and tz == self.tz.get():
                self.report = report
                self.details.configure(state='normal')
                self.details.delete('1.0','end')
                self.details.insert('1.0', error or report['report'])
                self.details.configure(state='disabled')
                valid = bool(report and report['complete'] and not report['duplicates'] and report['date'])
                self.start_button.configure(state='normal' if valid else 'disabled')
                if report: self.date.set(report['date'])
                self.status.set('촬영일·회차·등록 대상을 확인하고 시작하세요.' if valid else '점검 내용을 확인하세요. 날짜가 섞이면 회차별 폴더로 지정하세요.')
        except queue.Empty: pass
        self.root.after(150, self.poll)

    def create(self):
        if not self.report or self.busy: return
        try:
            self.result = create_from_raw(self.report, self.work.get(), self.site.get(), self.session.get(), self.date.get())
            destroy_owned_root(self.root)
        except Exception as exc: messagebox.showerror('프로젝트 준비', str(exc), parent=self.root)

    def open(self):
        if self.busy: return
        path = filedialog.askopenfilename(parent=self.root, filetypes=(('프로젝트','*.json'),))
        if path:
            try:
                if not general_project.is_general(load_config(path)): raise ValueError('이 배포 앱의 project.json을 선택하세요.')
                self.result = Path(path)
                destroy_owned_root(self.root)
            except Exception as exc: messagebox.showerror('프로젝트 열기', str(exc), parent=self.root)

    def close(self):
        if self.busy: return
        destroy_owned_root(self.root)


class UpdateWindow:
    def __init__(self, parent, app=None):
        if app and app._busy():
            messagebox.showinfo('업데이트 대기', '진행 중인 처리·스캔이 끝난 후 업데이트하세요.', parent=parent)
            return
        if any(isinstance(child, tk.Toplevel) and child.winfo_viewable() for child in parent.winfo_children()):
            messagebox.showinfo('업데이트 대기', '열린 편집·검토 창을 저장하고 닫은 후 업데이트하세요.', parent=parent)
            return
        self.parent, self.app = parent, app
        self.info = None
        self.busy = False
        self.events = queue.Queue()
        self.window = tk.Toplevel(parent)
        self.window.title('인터넷 연결·업데이트')
        self.window.geometry('700x460')
        self.window.transient(parent)
        self.window.grab_set()
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        body = ttk.Frame(self.window, padding=20)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='앱 업데이트', style='Title.TLabel').pack(anchor='w')
        ttk.Label(body, text='GitHub에서 앱 버전·배포 파일만 조회합니다. 연구 자료는 전송하지 않습니다.', wraplength=650).pack(anchor='w', pady=10)
        self.status = tk.StringVar(value='확인 버튼을 누를 때만 인터넷에 연결합니다.')
        ttk.Label(body, textvariable=self.status, wraplength=650).pack(anchor='w', pady=8)
        self.notes = tk.Text(body, wrap='word', bg='#131518', fg='#edf0f2', height=10)
        self.notes.pack(fill='both', expand=True)
        buttons = ttk.Frame(body)
        buttons.pack(fill='x', pady=(16,0))
        self.check_button = ttk.Button(buttons, text='인터넷 연결·새 버전 확인', command=self.check)
        self.check_button.pack(side='left')
        self.install_button = ttk.Button(buttons, text='다운로드·검사', command=self.install, state='disabled')
        self.install_button.pack(side='left', padx=8)
        self.apply_button = ttk.Button(buttons, text='적용·다시 시작', command=self.apply, state='disabled')
        self.apply_button.pack(side='right')
        self.window.after(150, self.poll)

    def job(self, kind, function):
        if self.busy: return
        self.busy = True
        self.check_button.configure(state='disabled')
        self.install_button.configure(state='disabled')
        def worker():
            try: self.events.put((kind, function(), None))
            except Exception as exc: self.events.put((kind, None, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def check(self):
        self.status.set('GitHub 연결·버전 확인 중…')
        self.job('check', studio_update.check_update)

    def install(self):
        if self.info:
            self.status.set('다운로드 → SHA256 검증 → 새 버전 환경 검사 중…')
            self.job('install', lambda: studio_update.prepare_update(self.info))

    def poll(self):
        try:
            kind, result, error = self.events.get_nowait()
            self.busy = False
            self.check_button.configure(state='normal')
            if error:
                self.status.set('연결 또는 검사 실패. 현재 버전은 유지됩니다.')
                self.notes.insert('end', error+'\n')
            elif kind == 'check':
                self.info = result
                self.status.set(f"현재 {result['current']} / 최신 {result['version']}" + (' · 새 버전 있음' if result['newer'] else ' · 최신 버전'))
                self.notes.delete('1.0','end'); self.notes.insert('1.0',result['notes'])
                self.install_button.configure(state='normal' if result['newer'] else 'disabled')
            else:
                self.status.set('새 버전 검사 완료. 적용하면 앱을 닫고 새 버전으로 다시 시작합니다.')
                self.apply_button.configure(state='normal')
        except queue.Empty: pass
        self.window.after(150, self.poll)

    def apply(self):
        if self.app and self.app._busy():
            messagebox.showwarning('업데이트 대기','처리가 끝난 후 적용하세요.',parent=self.window); return
        if self.app and not self.app.save(quiet=True): return
        if not messagebox.askyesno('업데이트 적용','현재 작업을 저장하고 다시 시작할까요? 이전 버전과 프로젝트는 보존됩니다.',parent=self.window): return
        subprocess.Popen([sys.executable,'-B',str(Path(studio_update.__file__)), '--activate',self.info['version'],'--wait-pid',str(os.getpid())],
            cwd=studio_update.ROOT, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        destroy_owned_root(self.parent)

    def close(self):
        if not self.busy: self.window.destroy()


class DesktopStudio(GeneralStudio):
    def __init__(self, root, config_path):
        super().__init__(root, config_path)
        root.title('UAV Tarp Studio | 타프 반사도 · '+studio_update.channel()['version'])

    def _build(self):
        super()._build()
        bar = ttk.Frame(self.identity)
        bar.pack(fill='x', pady=8)
        ttk.Button(bar, text='다음 단계 진행', style='Primary.TButton', command=self.next_step).pack(side='left')
        ttk.Button(bar, text='관련 실측 자료 찾기', command=self.assistant.scan).pack(side='left', padx=8)
        ttk.Button(bar, text='Codex 준비·연결', command=self.prepare_codex).pack(side='left')
        ttk.Button(bar, text='인터넷 연결·업데이트 확인', command=lambda: UpdateWindow(self.root,self)).pack(side='right')
        self.root.title('UAV Tarp Studio | 타프 반사도')

    def next_step(self):
        if self._busy(): return
        current = self.pre_steps.index(self.pre_steps.select())
        if current == 0:
            if not self._var('preparation.shot_list_csv').get():
                if self._inventory: self.register_complete()
                else: self.scan_sources()
                return
            missing = [label for key,label in (('tarp_reflectance_csv','타프 실측 반사도'),('gain_csv','센서 gain'),('ed_csv','실측 Ed')) if not self._var('preparation.'+key).get()]
            if missing:
                messagebox.showinfo('필요한 실측 자료',' · '.join(missing)+' 자료를 연결하세요. 관련 실측 자료 찾기로 후보를 찾을 수 있습니다.',parent=self.root)
                return
            if self.save(quiet=True): self.pre_steps.select(1)
        elif current == 1:
            if self.save_candidates():
                self.pre_steps.select(2)
                self.open_tarp_roi_workflow()
        else:
            record = self._easy_record()
            self.run_easy_elc('finish' if record.get('status') == 'awaiting_review' else 'prepare')

    def run(self, stage):
        allowed = {'scan_raw':self.scan_sources, 'next_step':self.next_step,
            'roi':self.open_tarp_roi_workflow, 'prepare_elc':lambda:self.run_easy_elc('prepare'),
            'finish_elc':lambda:self.run_easy_elc('finish')}
        if stage not in allowed: raise ValueError('등록되지 않은 작업입니다.')
        if not self._busy(): allowed[stage]()

    def prepare_codex(self):
        if find_codex():
            self.assistant.grid()
            self.assistant.settings()
            return
        if not messagebox.askyesno('Codex 설치','Codex 실행 파일이 없습니다. 공식 OpenAI 배포본을 다운로드하고 SHA256을 검사해 앱 전용 폴더에 설치할까요? 이후 본인 계정으로 로그인합니다.',parent=self.root): return
        self._append('[Codex] 공식 배포본 다운로드·검사 중\n')
        # Tk updates stay on the main thread.
        events = queue.Queue()
        def install():
            try: events.put((studio_update.install_codex(),None))
            except Exception as exc: events.put((None,str(exc)))
        def poll():
            try:
                result,error=events.get_nowait()
                if error: messagebox.showerror('Codex 설치',error,parent=self.root)
                else:
                    self._append('[Codex] '+result+' 설치 확인\n')
                    self.assistant.grid(); self.assistant.settings()
            except queue.Empty: self.root.after(200,poll)
        threading.Thread(target=install,daemon=True).start(); self.root.after(200,poll)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project',type=Path)
    parser.add_argument('--smoke',action='store_true')
    args=parser.parse_args()
    project=args.project
    if not project:
        root=tk.Tk()
        window=StartWindow(root)
        if args.smoke:
            root.after(1200,lambda:destroy_owned_root(root))
        root.mainloop(); project=window.result
    if project:
        root=tk.Tk(); app=DesktopStudio(root,project)
        if args.smoke: root.after(1500,lambda:destroy_owned_root(root))
        root.mainloop()


if __name__=='__main__':
    try: main()
    except Exception:
        import traceback
        distribution.STATE.mkdir(parents=True,exist_ok=True)
        (distribution.STATE/'desktop_error.log').write_text(traceback.format_exc(),encoding='utf-8')
        raise
