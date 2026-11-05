"""Folder-based RAMSES Ed import; reuse the validated measured-wavelength reader."""
import csv
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import threading
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import distribution
distribution.configure()
from uav_pipeline.ramses_ed import _read


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def convert_folder(source, output, timezone, bands, start_kst='', end_kst='', progress=None):
    if timezone not in ('Asia/Seoul', 'UTC'):
        raise ValueError('원자료의 시간대를 먼저 선택하세요.')
    source, output = Path(source).resolve(), Path(output).resolve()
    if bool(start_kst) != bool(end_kst):
        raise ValueError('시간 범위는 시작·종료를 모두 입력하거나 둘 다 비워두세요.')
    start = datetime.strptime(start_kst, '%Y-%m-%d %H:%M:%S') if start_kst else None
    end = datetime.strptime(end_kst, '%Y-%m-%d %H:%M:%S') if end_kst else None
    if start and start > end:
        raise ValueError('종료 시각은 시작 시각 이후여야 합니다.')
    if not source.is_dir():
        raise ValueError('Ed 원자료 폴더를 선택하세요.')
    files = sorted(p for p in source.rglob('*') if p.is_file() and p.suffix.lower() == '.dat')
    if not files:
        raise ValueError('하위 폴더를 포함해 RAMSES .dat 파일이 없습니다.')
    rows, audit, errors, copies = {}, [], [], 0
    for number, path in enumerate(files, 1):
        if progress: progress(number - 1, len(files))
        record = dict(source_file=str(path), sha256='', status='', detail='')
        try:
            raw = path.read_bytes()
            record['sha256'] = hashlib.sha256(raw).hexdigest()
            text = raw.decode('utf-8', errors='replace')
            unit = re.search(r'^Unit2\s*=\s*(.+)$', text, re.MULTILINE)
            if unit and 'sr' in unit.group(1).lower():
                record.update(status='skipped_radiance', detail='방사휘도 자료: Ed 변환 대상 아님')
                audit.append(record)
                continue
            row = _read(path, bands)
            if hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
                raise ValueError('읽는 동안 원자료가 변경되었습니다. 다시 실행하세요.')
            measured = datetime.fromisoformat(row['datetime_kst']).replace(tzinfo=ZoneInfo(timezone))
            row['datetime_kst'] = measured.astimezone(ZoneInfo('Asia/Seoul')).replace(tzinfo=None).isoformat(sep=' ')
            if start and not start <= datetime.fromisoformat(row['datetime_kst']) <= end:
                record.update(status='outside_window', detail=row['datetime_kst'])
                audit.append(record)
                continue
            if any(float(row[f'Band{band}_{name}']) <= 0 for band, name in bands.items()):
                raise ValueError('0 이하의 Ed가 있습니다. 센서·측정 상태를 확인하세요.')
            stamp = row['datetime_kst']
            if stamp in rows:
                if rows[stamp][1] != record['sha256']:
                    raise ValueError(f'같은 시각({stamp})의 서로 다른 자료입니다. 사용할 센서·원자료 폴더를 구분하세요.')
                record.update(status='duplicate_copy', detail='동일 파일 사본: 한 번만 사용')
                copies += 1
            else:
                rows[stamp] = (row, record['sha256'])
                record.update(status='used', detail=stamp)
        except (OSError, ValueError) as exc:
            record.update(status='error', detail=str(exc))
            errors.append(str(exc))
        audit.append(record)
    run = output / ('Ed_Import_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid4().hex[:6])
    run.mkdir(parents=True, exist_ok=False)
    write_csv(run/'source_audit.csv', audit)
    days = {}
    if not errors:
        for stamp, (row, _) in sorted(rows.items()):
            days.setdefault(stamp[:10], []).append(dict(cycle='', list_kind='RAMSES_irradiance_measured', **row))
    results = []
    for day, records in days.items():
        csv_path = run/day/f'Ed_{day}_10band.csv'
        write_csv(csv_path, records)
        results.append(dict(date=day, count=len(records), start_kst=records[0]['datetime_kst'],
                            end_kst=records[-1]['datetime_kst'], csv=str(csv_path),
                            sha256=hashlib.sha256(csv_path.read_bytes()).hexdigest()))
    result = dict(schema=1, source=str(source), source_timezone=timezone, output_timezone='Asia/Seoul',
                  method='nearest_measured_wavelength_no_srf', bands=bands, folder=str(run),
                  converter_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  reader_sha256=hashlib.sha256(Path(_read.__code__.co_filename).read_bytes()).hexdigest(),
                  input_files=len(files), identical_copies=copies,
                  start_kst=start_kst, end_kst=end_kst,
                  outside_window=sum(r['status']=='outside_window' for r in audit),
                  skipped_radiance=sum(r['status']=='skipped_radiance' for r in audit),
                  errors=errors, dates=results)
    if results:
        write_csv(run/'date_index.csv', results)
    (run/'manifest.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if progress: progress(len(files), len(files))
    return result


def converted_date(result, date):
    """Only attach an unchanged successful CSV for the current project date."""
    if result['errors']:
        raise ValueError('Ed 원자료 확인 필요: ' + result['errors'][0] + '\n검사 기록: ' + result['folder'])
    matches = [entry for entry in result['dates'] if entry['date'] == date]
    if len(matches) != 1:
        found = ', '.join(entry['date'] for entry in result['dates']) or '없음'
        raise ValueError(f'촬영일 {date}의 Ed가 없습니다. 변환 날짜: {found}. 원자료 폴더와 시간대를 확인하세요.')
    entry = matches[0]
    if hashlib.sha256(Path(entry['csv']).read_bytes()).hexdigest() != entry['sha256']:
        raise ValueError('생성 후 Ed CSV가 변경되었습니다. 원자료에서 다시 변환하세요.')
    return entry


class EdImportWindow:
    def __init__(self, parent, output, bands, connect=None, date=''):
        self.root = tk.Toplevel(parent)
        self.root.title('Ed 원자료 폴더 → 날짜별 CSV')
        self.root.geometry('940x650')
        self.root.transient(parent)
        self.root.grab_set()
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.bands, self.connect, self.date = bands, connect, date
        self.busy, self.result = False, None
        self.events = queue.Queue()
        self.source = tk.StringVar(self.root)
        self.output = tk.StringVar(self.root, value=str(output))
        self.tz = tk.StringVar(self.root, value='선택 필요')
        self.start = tk.StringVar(self.root)
        self.end = tk.StringVar(self.root)
        self.inputs = []
        self.status = tk.StringVar(self.root, value='RAMSES .dat 지원 · 하위 폴더 포함 · 원자료는 그대로 보존합니다.')
        body = ttk.Frame(self.root, padding=20); body.pack(fill='both', expand=True)
        body.columnconfigure(1, weight=1); body.rowconfigure(5, weight=1)
        for row, label, variable in [(0, 'Ed 원자료 폴더', self.source), (1, '정리 결과 저장 폴더', self.output)]:
            ttk.Label(body, text=label).grid(row=row, column=0, sticky='w', pady=8)
            entry = ttk.Entry(body, textvariable=variable)
            entry.grid(row=row, column=1, sticky='ew', padx=10); self.inputs.append(entry)
            ttk.Button(body, text='폴더 선택', command=lambda v=variable:self.browse(v)).grid(row=row, column=2)
        ttk.Label(body, text='Ed 원자료 시간대').grid(row=2, column=0, sticky='w')
        self.timezone_box = ttk.Combobox(body, textvariable=self.tz, values=('Asia/Seoul', 'UTC'), state='readonly')
        self.timezone_box.grid(row=2, column=1, sticky='w', padx=10)
        range_box = ttk.Frame(body); range_box.grid(row=3, column=0, columnspan=3, sticky='ew', pady=12)
        ttk.Label(range_box, text='시간 범위 (선택·한국시간)').pack(side='left')
        for label, variable in (('시작', self.start), ('종료', self.end)):
            ttk.Label(range_box, text=label).pack(side='left', padx=(6,0))
            entry = ttk.Entry(range_box, textvariable=variable, width=22)
            entry.pack(side='left', padx=6); self.inputs.append(entry)
        ttk.Label(range_box, text='YYYY-MM-DD HH:MM:SS\n둘 다 비우면 전체 기간').pack(side='left')
        self.run_button = ttk.Button(body, text='1. 폴더 점검 · 날짜별 CSV 생성', command=self.run)
        self.run_button.grid(row=4, column=0, sticky='w', pady=8)
        ttk.Label(body, text='시간대는 직접 확인 · 날짜별 시간순 정리 자동\n시간 보간·평균 없음 · 촬영 시각 매칭은 별도 검사').grid(row=4, column=1, columnspan=2, sticky='w', padx=10)
        self.table = ttk.Treeview(body, columns=('date','count','start','end'), show='headings', height=8)
        for key, label, width in [('date','날짜',130),('count','측정 수',90),('start','시작 (한국시간)',220),('end','종료 (한국시간)',220)]:
            self.table.heading(key, text=label); self.table.column(key, width=width)
        self.table.grid(row=5, column=0, columnspan=3, sticky='nsew')
        ttk.Label(body, textvariable=self.status, wraplength=870).grid(row=6, column=0, columnspan=3, sticky='w', pady=12)
        self.connect_button = ttk.Button(body, text='2. 선택 날짜 CSV를 현재 회차에 연결', command=self.attach, state='disabled')
        self.connect_button.grid(row=7, column=0, columnspan=2, sticky='w')
        ttk.Button(body, text='결과 폴더 열기', command=self.open_output).grid(row=8, column=0, sticky='w', pady=6)
        ttk.Button(body, text='닫기', command=self.close).grid(row=7, column=2, sticky='e')
        self.root.after(150, self.poll)
        for variable in (self.source, self.output, self.tz, self.start, self.end):
            variable.trace_add('write', self.invalidate)

    def invalidate(self, *_):
        if self.busy: return
        self.result = None
        self.connect_button.configure(state='disabled')
        for item in self.table.get_children(): self.table.delete(item)
        self.status.set('입력이 변경되었습니다. CSV 생성을 다시 실행하세요.')

    def open_output(self):
        if self.result: os.startfile(self.result['folder'])

    def browse(self, variable):
        if self.busy: return
        folder = filedialog.askdirectory(parent=self.root)
        if folder: variable.set(folder)

    def run(self):
        if self.busy: return
        if not self.source.get().strip() or not self.output.get().strip() or self.tz.get() not in ('Asia/Seoul','UTC'):
            messagebox.showwarning('입력 확인', '원자료 폴더·결과 폴더·원자료 시간대를 모두 지정하세요.', parent=self.root)
            return
        self.busy = True; self.result = None
        self.run_button.configure(state='disabled'); self.connect_button.configure(state='disabled')
        for entry in self.inputs: entry.configure(state='disabled')
        self.timezone_box.configure(state='disabled')
        for item in self.table.get_children(): self.table.delete(item)
        args = self.source.get(), self.output.get(), self.tz.get(), self.bands, self.start.get().strip(), self.end.get().strip()
        self.status.set('원자료를 읽고 있습니다. 파일 수에 따라 시간이 걸릴 수 있습니다…')
        def worker():
            try: self.events.put((convert_folder(*args), None))
            except Exception as exc: self.events.put((None, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            self.result, error = self.events.get_nowait()
            self.busy = False; self.run_button.configure(state='normal')
            for entry in self.inputs: entry.configure(state='normal')
            self.timezone_box.configure(state='readonly')
            if error:
                self.status.set(error); messagebox.showerror('Ed 정리 실패', error, parent=self.root)
            else:
                result = self.result
                for entry in result['dates']:
                    self.table.insert('', 'end', iid=entry['date'], values=(entry['date'],entry['count'],entry['start_kst'],entry['end_kst']))
                if result['errors']:
                    text = f"오류 {len(result['errors'])}개로 CSV 생성을 보류했습니다.\n" + '\n'.join(result['errors'][:3])
                    messagebox.showwarning('원자료 확인 필요', text+'\n전체 내역: '+result['folder']+'/source_audit.csv', parent=self.root)
                else:
                    text = f"날짜 {len(result['dates'])}개 저장 · 동일 사본 {result['identical_copies']}개 통합 · 방사휘도 {result['skipped_radiance']}개 제외"
                    if not result['dates']: text = '변환 가능한 Ed가 없습니다. 원자료 종류를 확인하세요.'
                self.status.set(text+'\n저장 위치: '+result['folder'])
                if self.connect and result['dates']:
                    self.connect_button.configure(state='normal')
                    if self.table.exists(self.date): self.table.selection_set(self.date)
        except queue.Empty:
            pass
        self.root.after(150, self.poll)

    def attach(self):
        chosen = self.table.selection()
        if not chosen: return
        entry = next(row for row in self.result['dates'] if row['date']==chosen[0])
        if entry['date'] != self.date:
            messagebox.showwarning('촬영일 불일치', f'현재 회차의 촬영일은 {self.date}입니다. 같은 날짜를 선택하세요.', parent=self.root)
            return
        try:
            if hashlib.sha256(Path(entry['csv']).read_bytes()).hexdigest() != entry['sha256']:
                raise ValueError('생성 후 CSV가 변경되었습니다. 다시 정리하세요.')
        except (OSError, ValueError) as exc:
            messagebox.showerror('CSV 확인', str(exc), parent=self.root)
            return
        if self.connect(entry['csv']):
            messagebox.showinfo('Ed 연결 완료', '현재 회차에 연결했습니다. 촬영 시각별 최근접 5초 이내 매칭은 처리 전 검사합니다.', parent=self.root)
            self.close()

    def close(self):
        if self.busy:
            messagebox.showinfo('정리 중', 'CSV 정리가 끝난 뒤 닫아주세요.', parent=self.root)
            return
        self.root.destroy()
