"""Raw-folder entry point for the native ELC app; science stays in the existing engine."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import re
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
from uav_pipeline.button_guide import ButtonGuide
from uav_pipeline.progress_ui import center_studio_popups
from uav_pipeline.codex_assistant import find_codex
import studio_update
import app_identity


def inventory_guidance(report, raw):
    """Explain inventory problems without picking or deleting ambiguous files."""
    summary = (f"완전한 촬영 {len(report['complete']):,}개 · "
               f"밴드 누락·빈 파일 {len(report['incomplete']):,}개 · "
               f"중복 촬영번호·밴드 {len(report['duplicates']):,}개")
    if report['duplicates']:
        root = Path(raw).resolve()
        branches = set()
        for _, _, paths in report['duplicates']:
            for path in paths:
                relative = Path(path).resolve().relative_to(root)
                if len(relative.parts) > 1:
                    branches.add(relative.parts[0])
        observation = [name for name in sorted(branches) if 'observation' in name.lower()]
        panel = [name for name in branches if 'panel' in name.lower()]
        reason = '서로 다른 위치에 같은 촬영번호·밴드의 파일이 있어 처리 대상을 정할 수 없습니다.'
        action = '원영상 폴더에서 한 회차의 실제 촬영 영상만 들어 있는 하위 폴더를 선택한 뒤 다시 점검하세요.'
        if len(observation) == 1 and panel:
            reason = '관측 영상과 패널 영상 폴더를 함께 읽어 같은 촬영번호·밴드가 중복되었습니다.'
            action = (f"현재 원영상 폴더 안의 다음 폴더를 선택하세요:\n{observation[0]}\n"
                      '패널 촬영 폴더는 포함하지 말고, 원영상 자동 점검을 다시 누르세요.')
        return summary + '\n\n시작할 수 없는 이유\n' + reason + '\n\n조치 방법\n' + action + '\n\n원본 파일을 삭제하거나 이름을 바꿀 필요는 없습니다.'
    if not report['complete']:
        return summary + '\n\n10개 밴드가 모두 있는 촬영을 찾지 못했습니다.\n원영상 폴더와 파일 복사 완료 여부를 확인하고 다시 점검하세요.'
    if report['incomplete']:
        return summary + '\n\n누락되거나 비어 있는 밴드가 있는 촬영은 대상에서 제외됩니다.\n전체 촬영이 필요하면 누락 파일을 복사한 뒤 다시 점검하세요.\n완전한 촬영만 진행하려면 촬영일·회차를 확인하고 시작하세요.'
    return summary + '\n\n촬영일·회차·결과 폴더를 확인한 뒤 시작하세요.'


def inspect_input(raw, source_timezone='UTC'):
    report = inspect_raw(raw)
    result = {'report': report['report'], 'summary': inventory_guidance(report, raw),
              'incomplete_count': len(report['incomplete']), 'complete': report['complete'],
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
    else:
        result['summary'] = result['summary'].split('\n', 1)[0] + '\n\n촬영일 표본이 서로 다릅니다: ' + ', '.join(sorted(dates)) + '\n한 날짜·한 회차의 원영상 폴더로 좁혀 다시 점검하세요.'
    result['report'] += '\n촬영일 확인: 처음·중간·마지막 촬영의 EXIF 표본 (전체 시간 검사는 처리 전 수행)'
    result['report'] += '\n한국시간 날짜: ' + ', '.join(sorted(dates))
    return result


def folder_date(raw):
    """A unique valid folder date is a hint, never acquisition metadata."""
    dates = set()
    for token in re.findall(r'(?<!\d)(?:20\d{2}[-_]\d{2}[-_]\d{2}|20\d{6}|\d{6})(?!\d)', raw):
        compact = token.replace('-', '').replace('_', '')
        if len(compact) == 6:
            compact = '20' + compact
        try:
            dates.add(datetime.strptime(compact, '%Y%m%d').date().isoformat())
        except ValueError:
            pass
    return next(iter(dates)) if len(dates) == 1 else ''


def create_from_raw(report, work, site, session, date):
    raw = Path(report['raw']).resolve()
    output = Path(work).resolve()
    if raw == output or raw in output.parents or output in raw.parents:
        raise ValueError('원영상과 결과 폴더는 서로 포함되지 않는 별도 위치를 지정하세요.')
    if report['duplicates'] or not report['complete']:
        raise ValueError('중복을 해결하고 완전한 10밴드 촬영을 확인하세요.')
    if not report.get('date') or date != report['date']:
        raise ValueError('입력한 관측일이 EXIF에서 확인한 촬영일과 다릅니다. 날짜와 EXIF 원본 시간대를 확인하세요.')
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
        app_identity.apply(root)
        center_studio_popups(root)
        self.root, self.result, self.report = root, None, None
        self.events = queue.Queue()
        self.busy = False
        apply_studio_theme(root)
        root.title('UAV Tarp Studio | 새 작업 시작 · '+studio_update.channel()['version'])
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
        self.auto_date = tk.BooleanVar(value=True)
        self.date_hint = tk.StringVar(value='폴더 날짜를 먼저 표시하고, 자동 점검 후 EXIF 촬영일로 확인합니다.')
        self.site = tk.StringVar(value='Site')
        self.session = tk.StringVar(value='Session01')
        self.tz = tk.StringVar(value='UTC')
        folders = []
        for row, label, variable in ((2, '원영상 폴더', self.raw), (3, '프로젝트·결과 폴더', self.work)):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', padx=(0, 16), pady=8)
            ttk.Entry(frame, textvariable=variable).grid(row=row, column=1, sticky='ew')
            button = ttk.Button(frame, text='폴더 선택', command=lambda v=variable: self.browse(v))
            button.grid(row=row, column=2, padx=(8, 0))
            folders.append(button)
        ttk.Label(frame, text='EXIF 원본 시간대').grid(row=4, column=0, sticky='w', pady=8)
        ttk.Combobox(frame, textvariable=self.tz, values=('UTC','Asia/Seoul'), state='readonly', width=16).grid(row=4, column=1, sticky='w')
        ttk.Label(frame, textvariable=self.date_hint, style='Subtitle.TLabel').grid(row=5, column=0, columnspan=3, sticky='w', pady=(0, 12))
        for row, label, variable in ((6, '관측일', self.date), (7, '현장 이름', self.site), (8, '회차 이름', self.session)):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', pady=5)
            entry = ttk.Entry(frame, textvariable=variable, width=26)
            entry.grid(row=row, column=1, sticky='w')
            if variable is self.date:
                self.date_entry = entry
                entry.configure(state='readonly')
        ttk.Checkbutton(frame, text='날짜 자동 입력', variable=self.auto_date, command=self.refresh_date).grid(row=6, column=2, sticky='w')
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
        self.details_button = ttk.Button(bottom, text='상세 점검 로그 보기', command=self.toggle_scan_details, state='disabled')
        self.details_button.pack(side='left', padx=8)
        ttk.Button(bottom, text='Ed 원자료 → CSV', command=self.import_ed).pack(side='left', padx=4)
        self.scan_summary = self.scan_log = ''
        self.showing_scan_log = False
        ttk.Button(bottom, text='인터넷 연결·업데이트 확인', command=lambda: UpdateWindow(root)).pack(side='right')
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.raw.trace_add('write', self.invalidate)
        self.tz.trace_add('write', self.invalidate)
        root.after(150, self.poll)
        guide = ButtonGuide(root)
        def guide_start():
            button = self.scan_button if self.start_button.instate(['disabled']) else self.start_button
            if not self.work.get().strip():button = folders[1]
            if not self.raw.get().strip():button = folders[0]
            guide.show(None if self.busy else button)
        guide.watch(guide_start)

    def invalidate(self, *_):
        self.report = None
        self.start_button.configure(state='disabled')
        self.refresh_date()
        self.details_button.configure(state='disabled')

    def refresh_date(self):
        self.date_entry.configure(state='readonly' if self.auto_date.get() else 'normal')
        if not self.auto_date.get():
            self.date_hint.set('직접 입력: YYYY-MM-DD · 시작 전 EXIF 촬영일(한국시간)과 일치하는지 확인합니다.')
            return
        confirmed = (self.report or {}).get('date', '')
        guessed = folder_date(self.raw.get())
        self.date.set(confirmed or guessed)
        if confirmed:
            note = ' · 폴더 날짜와 달라 EXIF 날짜를 적용했습니다.' if guessed and guessed != confirmed else ''
            self.date_hint.set('EXIF 촬영일 확인 완료 (한국시간)' + note)
        elif guessed:
            self.date_hint.set('폴더 날짜 자동 입력 · 원영상 자동 점검으로 실제 촬영일을 확인하세요.')
        else:
            self.date_hint.set('폴더 날짜를 특정할 수 없습니다. 자동 점검으로 확인하거나 자동 입력을 끄고 직접 입력하세요.')

    def toggle_scan_details(self):
        self.showing_scan_log = not self.showing_scan_log
        self.details.configure(state='normal')
        self.details.delete('1.0', 'end')
        self.details.insert('1.0', self.scan_log if self.showing_scan_log else self.scan_summary)
        self.details.configure(state='disabled')
        self.details_button.configure(text='원인·조치 안내 보기' if self.showing_scan_log else '상세 점검 로그 보기')

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
                self.scan_log = error or report['report']
                self.scan_summary = ('점검을 완료하지 못했습니다.\n' + error + '\n\n원영상 폴더와 드라이브 연결 상태를 확인한 뒤 다시 점검하세요.') if error else report['summary']
                self.showing_scan_log = False
                self.details_button.configure(state='normal', text='상세 점검 로그 보기')
                self.details.configure(state='normal')
                self.details.delete('1.0','end')
                self.details.insert('1.0', self.scan_summary)
                self.details.configure(state='disabled')
                valid = bool(report and report['complete'] and not report['duplicates'] and report['date'])
                self.start_button.configure(state='normal' if valid else 'disabled')
                self.refresh_date()
                self.status.set('촬영일·회차·등록 대상을 확인하고 시작하세요.' if valid else '시작 전 조치가 필요합니다. 아래 원인과 조치 방법을 확인하세요.')
                if not valid or report.get('incomplete_count', 0):
                    messagebox.showwarning('원영상 점검 · 조치 안내', self.scan_summary, parent=self.root)
        except queue.Empty: pass
        self.root.after(150, self.poll)

    def create(self):
        if not self.report or self.busy: return
        try:
            self.result = create_from_raw(self.report, self.work.get(), self.site.get(), self.session.get(), self.date.get())
            destroy_owned_root(self.root)
        except Exception as exc: messagebox.showerror('프로젝트 준비', str(exc), parent=self.root)

    def import_ed(self):
        if self.busy: return
        from ed_import import EdImportWindow
        EdImportWindow(self.root, self.work.get(), distribution.preparation_template()['band_names'])

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
    def _load_to_form(self):
        super()._load_to_form()
        timezone = self._var('preparation.ramses_source_timezone')
        if not timezone.get(): timezone.set('Asia/Seoul')

    def _path_row(self, parent, row, label, key, kind="file", managed=False):
        controls = super()._path_row(parent,row,label,key,kind,managed)
        self.guide_paths.setdefault(key,[]).append(controls[1])
        if key == 'preparation.ed_csv':
            controls[1].configure(text='CSV 선택')
            button = ttk.Button(parent, text='원자료 폴더 → 자동 연결', command=self.import_ed_folder)
            button.grid(row=row, column=3, padx=5)
            self.guide_paths[key].insert(0, button)
            tz = self._var('preparation.ramses_source_timezone')
            if not tz.get(): tz.set('Asia/Seoul')
            zone = ttk.Frame(parent); zone.grid(row=row, column=4, padx=4)
            ttk.Label(zone, text='Ed 시간대').pack(side='left')
            ttk.Combobox(zone, textvariable=tz, values=('Asia/Seoul','UTC'),
                         width=10, state='readonly').pack(side='left', padx=3)
        return controls

    def import_ed_folder(self):
        if not self._idle(): return
        source = filedialog.askdirectory(parent=self.root, title='RAMSES Ed 원자료 폴더 · 하위 폴더 포함')
        if not source: return
        if not self.save(quiet=True): return
        from ed_import import convert_folder, converted_date
        from uav_pipeline.progress_ui import ProgressPopup
        date = self.cfg['general']['date']
        timezone = self._var('preparation.ramses_source_timezone').get()
        output = Path(self.cfg['general']['project_root'])/'Ed'
        bands = dict(self.cfg['band_names'])
        self._ed_import_busy = True
        events = queue.Queue()
        popup = ProgressPopup(self.root, 'Ed CSV 생성 · 현재 날짜 자동 연결')
        popup.grab_set()
        def worker():
            try:
                result = convert_folder(source, output, timezone, bands,
                    progress=lambda done,total:events.put(('progress',(done,total))))
                events.put(('done',converted_date(result,date)))
            except Exception as exc: events.put(('error',str(exc)))
        def poll():
            try:
                while True:
                    kind, value = events.get_nowait()
                    if kind == 'progress':
                        popup.update_progress(f'{date} · RAMSES 원자료 읽기', *value)
                        continue
                    self._ed_import_busy = False
                    popup.destroy()
                    if kind == 'error':
                        messagebox.showerror('Ed 자동 연결 실패',value,parent=self.root)
                    elif self.connect_ed_csv(value['csv']):
                        self.scan_status.set(f"Ed 자동 연결 완료 · {value['date']} · {value['count']:,}개 실측 · {value['start_kst']} ~ {value['end_kst']} KST")
                    return
            except queue.Empty: self.root.after(100,poll)
        threading.Thread(target=worker,daemon=True).start()
        self.root.after(100,poll)

    def __init__(self, root, config_path):
        app_identity.apply(root)
        center_studio_popups(root)
        self.guide_paths = {}
        self.archive_state = ''
        self.guide_record = {}
        self.viewed_report = None
        super().__init__(root, config_path)
        if self.cfg['preparation'].get('tarp_validation_approval'):
            self.easy_full.set(not self.elc_only.get())
        root.title('UAV Tarp Studio | 타프 반사도 · '+studio_update.channel()['version'])
        from completed_result import pointer
        if pointer(self.cfg).is_file():
            self.archive_state = 'checking'
            self.pre_steps.select(4)
            root.after(300, self.check_completed_result)
        self.button_guide = ButtonGuide(root)
        self.guide_buttons = {}
        def index(parent):
            for child in parent.winfo_children():
                if isinstance(child,ttk.Button):
                    self.guide_buttons.setdefault(str(child['text']),[]).append(child)
                index(child)
        index(root)
        self.button_guide.watch(self.refresh_guide)

    def refresh_easy_status(self):
        super().refresh_easy_status()
        self.guide_record = self._easy_record()
        if not self.guide_record and self.cfg['preparation'].get('tarp_validation_approval'):
            self.easy_status.configure(text='검증 승인 저장됨 · 전체 촬영분 처리 준비')

    def guide_action(self, step):
        if self._busy() or self.location_cancel_button.instate(['!disabled']):
            return None, '현재 작업이 끝나면 다음 버튼이 표시됩니다.'
        def path(key, message):
            return self.guide_paths.get(key,[]), message
        if step == 0:
            if not self._value('preparation.raw_directory'):
                return path('preparation.raw_directory','원영상 폴더를 선택하세요.')
            if not self._value('preparation.ed_csv'):
                return path('preparation.ed_csv','Ed 시간대를 확인하고 원자료 폴더를 선택하세요. 현재 날짜 CSV를 자동 생성·연결합니다. 기존 CSV도 선택할 수 있습니다.')
            if not self._value('preparation.shot_list_csv'):
                if self._inventory and self._inventory_source == self._value('preparation.raw_directory'):
                    if self._inventory['duplicates'] or not self._inventory['complete']:
                        return path('preparation.raw_directory','점검 결과를 확인하고 원영상 폴더를 다시 선택하세요.')
                    return self.register_button,'완전한 촬영분을 등록하세요.'
                return self.scan_button,'원영상을 스캔하세요.'
            return '다음 단계 진행','02에서 타프 위치를 지정하세요.'
        if step == 1:
            if not self._value('tarp_workflow.reference_mosaic'):
                if not self._value('tarp_workflow.photos_override'):
                    return path('tarp_workflow.photos_override','현재 회차 Photos 폴더를 선택하세요.')
                return self.location_mosaic_button,'위치 확인용 모자이크를 생성하세요.'
            if self._saved_candidates:
                return '다음 단계 진행','확정한 영상의 ROI를 확인하세요.'
            return '모자이크에서 타프 위치 지정 · 겹치는 영상 확인','타프 위치와 겹치는 영상을 확인하세요.'
        if step in (2,3):
            if not self._value('preparation.approved_roi_manifest'):
                if step==3:return '다음 단계 진행','03으로 돌아가 타프 ROI를 확인하세요.'
                if not self._saved_candidates:return '다음 단계 진행','02에서 겹치는 영상을 먼저 확정하세요.'
                return '자동 ROI 결과 보기','자동으로 잡은 타프 영역을 확인하세요.'
            return '다음 단계 진행','승인한 ROI로 반사도 단계에 진행하세요.'
        record = self.guide_record
        status = record.get('status')
        if status=='awaiting_review':
            return '2. 계수 확인·반사도 생성','계수를 확인하고 반사도를 생성하세요.'
        if self.easy_full.get() and self.cfg['preparation'].get('tarp_validation_approval') and not (record.get('full') and status=='complete'):
            if status=='failed' and '공간 부족' in record.get('error',''):
                return '승인한 밴드로 전체 반사도 산출',record['error']
            return '승인한 밴드로 전체 반사도 산출','검증을 승인했습니다. 용량·Ed 검사 후 전체 촬영분을 처리하세요.'
        if status=='complete':
            if self.viewed_report == record.get('report'):
                if record.get('full'):
                    return None,'전체 처리 완료 · 결과 폴더에서 반사도를 확인하세요.'
                return '검증 결과 승인 · 전체 처리 허용','검증 오차를 허용할 수 있으면 전체 처리를 승인하세요.'
            return '품질 보고서','독립 검증 오차를 확인하세요.'
        if self.archive_state=='ready':
            return '독립 검증 보고서','저장된 결과의 독립 검증 오차를 확인하세요.'
        if self.archive_state=='viewed':
            return '검증 결과 승인 · 전체 처리 허용','검증 오차를 허용할 수 있으면 전체 처리를 승인하세요.'
        if self.archive_state=='error':
            return '결과 다시 확인','저장된 결과의 연결 상태를 다시 확인하세요.'
        if not self._value('preparation.approved_roi_manifest'):
            return '다음 단계 진행','03에서 타프 ROI를 먼저 확인하세요.'
        return '선택 영상 · 정상 밴드 반사도 자동 산출','승인한 ROI로 선택 영상의 반사도를 산출하세요.'

    def refresh_guide(self):
        target, hint = self.guide_action(self.pre_steps.index(self.pre_steps.select()))
        if isinstance(target,str):
            target = self.guide_buttons.get(target,[])
        if isinstance(target,list):
            target = next((b for b in target if b.winfo_ismapped() and b.instate(['!disabled'])),None)
        self.button_guide.show(target)
        if self.guide_hint.get()!=hint:self.guide_hint.set(hint)

    def open_easy_report(self):
        report = self.guide_record.get('report')
        if super().open_easy_report():self.viewed_report = report

    def _build(self):
        super()._build()
        bar = ttk.Frame(self.identity)
        bar.pack(fill='x', pady=8)
        self.guide_hint = tk.StringVar()
        ttk.Label(self.identity,textvariable=self.guide_hint,foreground='#ff9397').pack(anchor='w')
        ttk.Button(bar, text='다음 단계 진행', style='Primary.TButton', command=self.next_step).pack(side='left')
        ttk.Button(bar, text='관련 실측 자료 찾기', command=self.assistant.scan).pack(side='left', padx=8)
        ttk.Button(bar, text='Codex 준비·연결', command=self.prepare_codex).pack(side='left')
        ttk.Button(bar, text='Ed 원자료 → CSV', command=self.import_ed).pack(side='left', padx=8)
        ttk.Button(bar, text='인터넷 연결·업데이트 확인', command=lambda: UpdateWindow(self.root,self)).pack(side='right')
        self.root.title('UAV Tarp Studio | 타프 반사도')
        auto=ttk.Frame(self.easy_status.master)
        auto.pack(fill='x',before=self.easy_status.master.winfo_children()[0],pady=(0,8))
        ttk.Button(auto,text='선택 영상 · 정상 밴드 반사도 자동 산출',style='Primary.TButton',
                   command=lambda:self.run_easy_elc('auto_selected')).pack(side='left')
        ttk.Label(auto,text='포화 검사 → 방사·센서·Ed → 반사도 → 타프 검증').pack(side='left',padx=8)
        ttk.Button(self.easy_status.master,text='승인한 밴드로 전체 반사도 산출',
                   command=lambda:self.run_easy_elc('auto_full')).pack(anchor='w',pady=(4,6))
        completed = ttk.Labelframe(self.easy_status.master.master, text='저장된 부분 밴드 반사도', padding=12)
        completed.pack(fill='x', after=self.easy_status.master, pady=(0,12))
        self.completed_status = tk.StringVar(value='별도 실행에서 완료된 부분 밴드 결과를 검증하여 연결할 수 있습니다.')
        ttk.Label(completed,textvariable=self.completed_status,wraplength=1000).pack(anchor='w')
        actions=ttk.Frame(completed);actions.pack(fill='x',pady=(10,0))
        ttk.Button(actions,text='완료 결과 폴더 연결',command=self.choose_completed_result).pack(side='left')
        ttk.Button(actions,text='결과 다시 확인',command=self.check_completed_result).pack(side='left',padx=6)
        ttk.Button(actions,text='반사도 폴더 열기',command=lambda:self.check_completed_result('reflectance_directory')).pack(side='left',padx=6)
        ttk.Button(actions,text='독립 검증 보고서',command=lambda:self.check_completed_result('report')).pack(side='left')

    def _busy(self):
        return getattr(self,'_result_busy',False) or getattr(self,'_ed_import_busy',False) or super()._busy()

    def choose_completed_result(self):
        if not self._idle(): return
        folder=filedialog.askdirectory(parent=self.root,title='완료된 부분 밴드 실행 폴더',initialdir=self.cfg['general']['project_root'])
        if folder:self.check_completed_result(run=folder)

    def check_completed_result(self, action=None, run=None):
        if not self._idle(): return
        import copy
        from completed_result import connect,load,label
        from uav_pipeline.progress_ui import ProgressPopup
        cfg=copy.deepcopy(self.cfg)
        for key in ('raw_directory','gps_path','ed_csv','gain_csv','tarp_reflectance_csv','reference_shot',
                    'source_timezone','time_band','max_ed_delta_seconds','shot_list_csv'):
            variable=self.vars.get('preparation.'+key)
            if variable is not None:
                value=variable.get().strip();old=cfg['preparation'].get(key)
                cfg['preparation'][key]=type(old)(value) if type(old) in (int,float) else value
        events=queue.Queue();self._result_busy=True
        popup=ProgressPopup(self.root,'완료 결과 출처 확인 중')
        self.completed_status.set('날짜·회차·ROI·교정 자료·반사도 파일을 확인 중…')
        def work():
            try:events.put((connect(cfg,run) if run else load(cfg),None))
            except Exception as exc:events.put((None,str(exc)))
        def poll():
            try:
                result,error=events.get_nowait()
            except queue.Empty:
                self.root.after(150,poll);return
            self._result_busy=False;popup.destroy()
            if error:
                self.archive_state = 'error'
                self.completed_status.set('완료 결과 연결 보류 · '+error)
            else:
                self._verified_connected_result = result
                self.completed_status.set(label(result,self.cfg['band_names']))
                if action:os.startfile(result[action])
                if action=='report':self.archive_state='viewed'
                elif self.archive_state!='viewed' or run:self.archive_state='ready'
        threading.Thread(target=work,daemon=True).start();self.root.after(150,poll)

    def import_ed(self):
        if not self._idle(): return
        from ed_import import EdImportWindow
        EdImportWindow(self.root, Path(self.cfg['general']['project_root'])/'Ed',
                       self.cfg['band_names'], self.connect_ed_csv, self.cfg['general']['date'])

    def connect_ed_csv(self, path):
        if not self._idle(): return False
        variable = self._var('preparation.ed_csv')
        previous = variable.get()
        variable.set(path)
        if self.save(quiet=True): return True
        variable.set(previous)
        return False

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
            if not self._value('tarp_workflow.reference_mosaic'):
                messagebox.showinfo('먼저 위치 확인용 모자이크 준비', '02 맨 위에서 현재 회차 Photos 폴더를 선택하고 모자이크 생성·연결을 누르세요. 이미 모자이크가 있으면 해당 파일을 선택하면 됩니다.', parent=self.root)
                return
            if not self._saved_candidates:
                self.open_tarp_workflow_dn()
                return
            if self.save_candidates():
                self.pre_steps.select(2)
                self.open_tarp_roi_workflow()
        elif current in (2,3):
            if self._value('preparation.approved_roi_manifest'):
                self.pre_steps.select(4)
            elif self._saved_candidates:
                self.pre_steps.select(2)
                self.open_tarp_roi_workflow()
            else:self.pre_steps.select(1)
        else:
            if not self._value('preparation.approved_roi_manifest'):
                self.pre_steps.select(2 if self._saved_candidates else 1)
                return
            target, _ = self.guide_action(4)
            if isinstance(target,str):
                button = next((b for b in self.guide_buttons.get(target,[])
                               if b.winfo_ismapped() and b.instate(['!disabled'])),None)
                if button is not None:button.invoke()

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
    parser.add_argument('--geometry', help='Optional initial window geometry for a chosen monitor')
    parser.add_argument('--smoke',action='store_true')
    args=parser.parse_args()
    if not app_identity.acquire_instance():
        return
    project=args.project
    if not project:
        root=tk.Tk()
        window=StartWindow(root)
        if args.geometry: root.geometry(args.geometry)
        if args.smoke:
            root.after(1200,lambda:destroy_owned_root(root))
        root.mainloop(); project=window.result
    if project:
        root=tk.Tk(); app=DesktopStudio(root,project)
        if args.geometry: root.geometry(args.geometry)
        if args.smoke: root.after(1500,lambda:destroy_owned_root(root))
        root.mainloop()


if __name__=='__main__':
    try: main()
    except Exception:
        import traceback
        distribution.STATE.mkdir(parents=True,exist_ok=True)
        (distribution.STATE/'desktop_error.log').write_text(traceback.format_exc(),encoding='utf-8')
        raise
